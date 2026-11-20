"""知识蒸馏服务：Claim/Evidence 抽取 + 实体消歧。
被 Celery Worker 的 EXTRACTING / ENTITY_RESOLUTION / PUBLISHING 阶段调用。"""
import json

from app.config import settings
from app.db import session as db
from app.modules.llm.client import llm_client
from app.modules.distillation.prompts import EXTRACT_SYSTEM, ENTITY_RESOLVE_SYSTEM


def extract_claims(chunk: dict) -> list[dict]:
    """对单个切片做 Claim 抽取，返回模型输出的主张列表。

    异常不再吞掉：端点故障（httpx.HTTPError）与模型输出非 JSON
    （LLMJsonError）分别向上抛，由流水线按切片统计并给出可诊断的原因码。
    """
    result = llm_client.chat_json(EXTRACT_SYSTEM, chunk["content"], strict=True)
    if isinstance(result, dict):
        result = [result]
    for claim in result or []:
        claim.setdefault("confidence", 0.5)
        claim["evidence"] = {
            "content": chunk["content"],
            "locator": chunk.get("locator", {}),
        }
    return result or []


def resolve_entity(entity_hint: dict) -> dict:
    """实体消歧：优先精确/别名匹配，退化走 LLM 判断；合并走重定向不删除。"""
    name = (entity_hint.get("name") or "").strip()
    etype = entity_hint.get("entity_type") or "Person"
    if not name:
        return {}

    known = db.fetch_all(
        "SELECT id, canonical_name, aliases FROM ontology.entities "
        "WHERE entity_type = :t AND status = 'active' LIMIT 200",
        {"t": etype},
    )
    for ent in known:
        aliases = ent["aliases"] if isinstance(ent["aliases"], list) else []
        if ent["canonical_name"] == name or name in aliases:
            return {"match_id": str(ent["id"]), "canonical_name": ent["canonical_name"]}

    known_desc = json.dumps(
        [{"id": str(e["id"]), "name": e["canonical_name"]} for e in known],
        ensure_ascii=False,
    )
    outcome = llm_client.chat_json(
        ENTITY_RESOLVE_SYSTEM,
        f"实体：{name}（类型 {etype}）\n已知实体：{known_desc}",
    )
    if not isinstance(outcome, dict):
        outcome = {}

    if outcome.get("match_id"):
        # LLM 幻觉防护：match_id 必须真实存在才允许挂别名；
        # JSON 解析失败（chat_json 失败返回 []）时绝不能静默新建实体。
        exists = db.fetch_one(
            "SELECT id FROM ontology.entities WHERE id = CAST(:id AS uuid)",
            {"id": str(outcome["match_id"])},
        )
        if exists:
            db.execute(
                "UPDATE ontology.entities SET aliases = aliases || CAST(:a AS jsonb) "
                "WHERE id = :id",
                {"a": json.dumps([name]), "id": outcome["match_id"]},
            )
            return {"match_id": str(outcome["match_id"]),
                    "canonical_name": outcome.get("canonical_name") or name}

    # 兜底：LLM 不可用 / 返回无效 match_id 时，按名称做一次确定性匹配，
    # 匹配不上才创建新实体，避免每次蒸馏都为同一人名新建重复实体。
    again = db.fetch_one(
        "SELECT id, canonical_name FROM ontology.entities "
        "WHERE entity_type = :t AND status = 'active' "
        "  AND canonical_name = :n LIMIT 1",
        {"t": etype, "n": name},
    )
    if again:
        return {"match_id": str(again["id"]), "canonical_name": again["canonical_name"]}

    new_id = db.insert_returning_id(
        "ontology.entities",
        {
            "entity_type": etype,
            "canonical_name": outcome.get("canonical_name", name),
            "aliases": json.dumps([name]),
            "ontology_version_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    return {"match_id": str(new_id), "canonical_name": name}


def _existing_claim(document_id: str, claim: dict) -> str | None:
    """同一文档 + 同一主张已发布过则复用，避免重新蒸馏产生重复 Claim/Evidence。"""
    row = db.fetch_one(
        "SELECT c.id FROM knowledge.claims c "
        "JOIN knowledge.claim_evidence ce ON ce.claim_id = c.id "
        "JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        "WHERE es.document_id = :d AND c.claim_type = :t "
        "AND c.predicate IS NOT DISTINCT FROM :p "
        # jsonb 等值比较对 key 顺序不敏感；::text 逐字节比较依赖序列化顺序，
        # LLM 输出 key 顺序稍有变化即误判不重复，导致重蒸馏产生重复 Claim。
        "AND c.subject = CAST(:s AS jsonb) AND c.object_value = CAST(:o AS jsonb) LIMIT 1",
        {
            "d": document_id,
            "t": claim.get("claim_type", "fact"),
            "p": claim.get("predicate"),
            "s": json.dumps(claim.get("subject") or {}, ensure_ascii=False),
            "o": json.dumps(claim.get("object_value") or {}, ensure_ascii=False),
        },
    )
    return str(row["id"]) if row else None


def _object_entity_id(claim: dict, source_id: str) -> str | None:
    """从 object_value 里找已在库中登记的实体，生成有语义的关系边。

    不再写 source=target 的 `extracted_from` 自环——那种关系只借
    evidence_claim_id 挂证据，本体中心展示出来没有任何语义。
    """
    obj = claim.get("object_value")
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("entity") or obj.get("target")
    if not isinstance(name, str) or not name.strip():
        return None
    row = db.fetch_one(
        "SELECT id FROM ontology.entities "
        "WHERE canonical_name = :n AND status = 'active' AND id != CAST(:s AS uuid) LIMIT 1",
        {"n": name.strip(), "s": source_id},
    )
    return str(row["id"]) if row else None


def _evidence_span_id(document_id: str, evidence: dict) -> str:
    """L12：同一文档里内容+定位完全相同的证据只存一份。

    一个切片常抽出多条 Claim，旧实现会为每个 Claim 复制一整段全文进
    evidence_spans；这里改为先按 (document_id, md5(content), locator) 命中复用，
    md5 用于走表达式索引，content 二次比较避免哈希碰撞误复用。
    """
    content = evidence.get("content") or ""
    locator = json.dumps(evidence.get("locator") or {})
    existing = db.fetch_one(
        "SELECT id FROM knowledge.evidence_spans "
        "WHERE document_id = CAST(:d AS uuid) "
        "  AND md5(content) = md5(:c) AND content = :c "
        "  AND locator = CAST(:l AS jsonb) LIMIT 1",
        {"d": document_id, "c": content, "l": locator},
    )
    if existing:
        return str(existing["id"])
    return str(db.insert_returning_id(
        "knowledge.evidence_spans",
        {"document_id": document_id, "content": content, "locator": locator,
         "evidence_type": "quote"},
    ))


def publish_claim(claim: dict, document_id: str, subject_entity_id: str | None,
                  low_confidence: bool = False) -> str:
    """单事务写入 Claim + Evidence + 关联（不自动转 Fact，等待人工审核）。

    幂等：同一文档里完全相同的主张不重复插入；关系只在能解析出真实客体时写入。
    L15：low_confidence=True 的 Claim 标为 review_priority='low_confidence'，
    审核队列据此前排展示 —— 阈值不再是只统计不消费的假动作。
    """
    with db.transaction():
        dup = _existing_claim(document_id, claim)
        if dup:
            return dup

        evidence_id = _evidence_span_id(document_id, claim["evidence"])
        claim_id = db.insert_returning_id(
            "knowledge.claims",
            {
                "claim_type": claim.get("claim_type", "fact"),
                "subject": json.dumps(claim.get("subject") or {}),
                "predicate": claim.get("predicate"),
                "object_value": json.dumps(claim.get("object_value") or {}),
                "confidence": claim.get("confidence", 0.5),
                "status": "candidate",
                "review_priority": "low_confidence" if low_confidence else "normal",
                "extraction_version": f"{settings.pipeline_version}/{settings.prompt_version}",
            },
        )
        db.execute(
            "INSERT INTO knowledge.claim_evidence (claim_id, evidence_id, support_type) "
            "VALUES (:c, :e, 'direct')",
            {"c": claim_id, "e": evidence_id},
        )
        if subject_entity_id:
            target_id = _object_entity_id(claim, subject_entity_id)
            if target_id:
                db.execute(
                    "INSERT INTO ontology.relations "
                    "(source_entity_id, relation_type, target_entity_id, "
                    " evidence_claim_id, status) "
                    "VALUES (:s, :rt, :t, :claim, 'candidate')",
                    {"s": subject_entity_id,
                     "rt": str(claim.get("predicate") or "related_to")[:100],
                     "t": target_id, "claim": claim_id},
                )
        return claim_id
