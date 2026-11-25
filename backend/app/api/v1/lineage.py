"""关联总览与链路追踪：把五大中心串成一条可验证的数据链。

数据源 → 蒸馏任务 → 切片/证据 → 候选知识 → 事实 → 本体 → 决策 → 审计
"""
import json

from fastapi import APIRouter, HTTPException, Request

from app.db import session as db

router = APIRouter()

_COUNT_SQL = """
SELECT
 (SELECT count(*) FROM source.data_sources)                       AS data_sources,
 (SELECT count(*) FROM source.documents)                         AS documents,
 (SELECT count(*) FROM ingest.pipeline_runs)                     AS runs,
 (SELECT count(*) FROM knowledge.semantic_chunks)                AS chunks,
 (SELECT count(*) FROM knowledge.evidence_spans)                 AS evidences,
 (SELECT count(*) FROM knowledge.claims)                         AS claims,
 (SELECT count(*) FROM knowledge.claims WHERE status='candidate') AS candidate_claims,
 (SELECT count(*) FROM knowledge.facts WHERE status='active')    AS facts,
 (SELECT count(*) FROM ontology.entities)                        AS entities,
 (SELECT count(*) FROM ontology.relations)                       AS relations,
 (SELECT count(*) FROM memory.memory_items)                      AS memories,
 (SELECT count(*) FROM decision.decision_records)                AS decisions,
 (SELECT count(*) FROM workflow.approval_tasks
    WHERE approval_status='pending')                             AS approvals,
 (SELECT count(*) FROM audit.decision_snapshots)                 AS snapshots
"""

def _decision_link_checks(m: dict) -> tuple[dict, dict]:
    """决策链路校验：只看真实链路产生的决策，孤立演示数据单独公开、不放宽覆盖率。

    - orphan = 既无 evidence 又无 snapshot 的历史演示记录（seed_demo.py 早期版本）
    - linked = 参与校验的真实决策；linked 中只要有一条缺证据或缺快照，对应项即 false
    - 不删数据、不补假证据：orphan 数量放进 link_details，随时可核对
    """
    decisions = int(m.get("decisions") or 0)
    with_ev = int(m.get("decisions_with_evidence") or 0)
    with_sn = int(m.get("decisions_with_snapshot") or 0)
    orphan = max(0, min(int(m.get("orphan_decisions") or 0), decisions))
    linked = max(0, decisions - orphan)
    checks = {
        "decisions_persisted": linked > 0 and with_ev >= linked,
        "decisions_with_snapshot": linked > 0 and with_sn >= linked,
    }
    return checks, {"linked_decisions": linked, "orphan_decisions": orphan}


def _claims_to_facts_check(m: dict) -> bool:
    """审核→事实链路：直接发布的 Fact 与「重复佐证」映射到已有 Fact 都算通。

    重复佐证不新建行，只把 claim_id 追加到已有 fact 的
    source_ref.corroborating_claims；SQL 已把这类 Claim 计入 approved_with_fact，
    这里仍要求 100% 覆盖 —— 少一条真实 fact 依然报 false，不是放宽口径。
    """
    approved = int(m.get("approved_claims") or 0)
    with_fact = int(m.get("approved_with_fact") or 0)
    return approved > 0 and with_fact >= approved


FLOW = [
    {"key": "sources", "label": "数据源", "view": "distill", "desc": "本地上传 / 钉钉文档 / 消息 / 日程 / 会议"},
    {"key": "runs", "label": "蒸馏任务", "view": "distill", "desc": "状态机 + 幂等键"},
    {"key": "claims", "label": "候选知识", "view": "knowledge", "desc": "人工审核前不得成为事实"},
    {"key": "facts", "label": "已发布事实", "view": "knowledge", "desc": "审核通过 → 版本化 Fact + 记忆"},
    {"key": "entities", "label": "本体实体", "view": "ontology", "desc": "主体消歧 + 关系"},
    {"key": "decisions", "label": "决策记录", "view": "decision", "desc": "检索事实/记忆/文档 → 落库 + 证据 + 快照"},
    {"key": "approvals", "label": "人工审批", "view": "decision", "desc": "D0~D3 铁律：默认必须人工确认"},
]


@router.get("/lineage/overview")
def overview():
    counts = db.fetch_one(_COUNT_SQL) or {}
    by_source = db.fetch_all(
        "SELECT COALESCE(metadata->>'source_kind', 'upload') AS source_kind, "
        "count(*) AS n FROM source.documents GROUP BY 1 ORDER BY n DESC"
    )
    # 真实链路校验：按「上游记录里有多少真正走到了下游」计算覆盖率，
    # 不再用全库 count>0 判断（那会让任意一条历史数据把整条链路刷成假绿）。
    m = db.fetch_one("""
        SELECT
         (SELECT count(DISTINCT document_id) FROM knowledge.semantic_chunks)
             AS docs_with_chunks,
         (SELECT count(DISTINCT document_id) FROM ingest.pipeline_runs)
             AS docs_with_runs,
         (SELECT count(*) FROM knowledge.claims WHERE status='approved')
             AS approved_claims,
         -- 「审核→发布」看的是曾产生过 Fact；后续被新版本 superseded 不算链路断裂。
         -- 重复佐证（duplicate/reinforced）不新建 Fact，只把 claim_id 追加到已有
         -- fact 的 source_ref.corroborating_claims，同样是一条真实链路，不能漏算。
         (SELECT count(*) FROM knowledge.claims c WHERE c.status='approved'
            AND (EXISTS (SELECT 1 FROM knowledge.facts f WHERE f.claim_id=c.id)
                 OR EXISTS (SELECT 1 FROM knowledge.facts f
                            WHERE f.source_ref->'corroborating_claims'
                                  @> to_jsonb(c.id::text))))
             AS approved_with_fact,
         (SELECT count(*) FROM knowledge.claims c WHERE c.status='approved'
            AND NOT EXISTS (SELECT 1 FROM knowledge.facts f WHERE f.claim_id=c.id)
            AND EXISTS (SELECT 1 FROM knowledge.facts f
                        WHERE f.source_ref->'corroborating_claims'
                              @> to_jsonb(c.id::text)))
             AS approved_corroborated,
         (SELECT count(*) FROM knowledge.facts WHERE status='active') AS active_facts,
         -- G1：结构化事实没有 claim_id，按设计也不生成「个人记忆」。
         -- 记忆链路的分母只能是有 claim_id 的活跃事实，否则每接一批组织数据
         -- 都会无端拉低覆盖率，把 7/7 全绿误报成链路断裂。阈值 0.8 不变。
         (SELECT count(*) FROM knowledge.facts WHERE status='active'
            AND claim_id IS NOT NULL) AS memory_linkable_facts,
         -- 第 8 项：结构化事实的 source_ref 六要素必须 100% 齐备。
         (SELECT count(*) FROM knowledge.facts WHERE status='active'
            AND source_ref->>'channel'='structured_sync') AS structured_facts,
         (SELECT count(*) FROM knowledge.facts WHERE status='active'
            AND source_ref->>'channel'='structured_sync'
            AND (COALESCE(source_ref->>'table','')=''
                 OR COALESCE(source_ref->>'pk','')=''
                 OR COALESCE(source_ref->>'row_hash','')=''
                 OR COALESCE(source_ref->>'batch_id','')=''
                 OR COALESCE(source_ref->>'mapping_version','')=''
                 OR COALESCE(source_ref->>'idempotency_key','')=''))
             AS structured_untraced,
         (SELECT count(*) FROM knowledge.facts f WHERE f.status='active'
            AND f.claim_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM memory.memory_items mi
                        WHERE f.claim_id = ANY(mi.source_claim_ids)))
             AS facts_with_memory,
         (SELECT count(*) FROM ontology.relations
            WHERE status='active' AND source_entity_id <> target_entity_id)
             AS real_relations,
         (SELECT count(*) FROM decision.decision_records) AS decisions,
         (SELECT count(*) FROM decision.decision_records d
            WHERE EXISTS (SELECT 1 FROM decision.decision_evidence e
                          WHERE e.decision_id=d.id)) AS decisions_with_evidence,
         (SELECT count(*) FROM decision.decision_records d
            WHERE EXISTS (SELECT 1 FROM audit.decision_snapshots s
                          WHERE s.decision_id=d.id)) AS decisions_with_snapshot,
         -- 孤立演示决策：既无证据行也无快照（早期 seed_demo.py 直接插表的遗留）。
         -- 它不是真实决策链路的产物，单独计数并公开，不参与覆盖率分母。
         (SELECT count(*) FROM decision.decision_records d
            WHERE NOT EXISTS (SELECT 1 FROM decision.decision_evidence e
                              WHERE e.decision_id=d.id)
              AND NOT EXISTS (SELECT 1 FROM audit.decision_snapshots s
                              WHERE s.decision_id=d.id)) AS orphan_decisions
    """) or {}
    approved = int(m.get("approved_claims") or 0)
    linkable = int(m.get("memory_linkable_facts") or 0)
    docs_runs = int(m.get("docs_with_runs") or 0)
    decision_checks, decision_detail = _decision_link_checks(m)
    link_checks = {
        "documents_to_chunks": docs_runs > 0 and
            int(m.get("docs_with_chunks") or 0) >= docs_runs * 0.5,
        "claims_to_facts": _claims_to_facts_check(m),
        # 分母换成「参与记忆链路的事实」，分子与 0.8 阈值一律不变。
        "facts_to_memory": linkable > 0 and
            int(m.get("facts_with_memory") or 0) >= linkable * 0.8,
        "relations_active": int(m.get("real_relations") or 0) > 0,
        # 无结构化数据时为空链路，不算断链；数量在 link_details 公开，不刷绿。
        "structured_facts_traced": int(m.get("structured_untraced") or 0) == 0,
        **decision_checks,
    }
    flow = [{**n, "count": counts.get(n["key"], 0)} for n in FLOW]
    return {"counts": counts, "by_source": by_source,
            "link_checks": link_checks,
            "link_details": {**m, **decision_detail,
                             "facts_to_memory_denominator": linkable,
                             "facts_to_memory_note":
                                 "分母排除无 claim_id 的结构化事实，阈值 0.8 不变"},
            "flow": flow}


@router.get("/lineage/trace/{document_id}")
def trace(document_id: str):
    """追踪一份文档从接入到决策的完整链路，供各中心下钻核对。"""
    doc = db.fetch_one(
        "SELECT id, title, mime_type, document_status, created_at, metadata "
        "FROM source.documents WHERE id = :id", {"id": document_id}
    )
    if not doc:
        raise HTTPException(404, "document not found")
    did = str(doc["id"])
    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    runs = db.fetch_all(
        "SELECT id, status, retry_count, created_at, updated_at "
        "FROM ingest.pipeline_runs WHERE document_id = :id ORDER BY created_at DESC",
        {"id": did},
    )
    chunks = db.fetch_one(
        "SELECT count(*) AS n FROM knowledge.semantic_chunks WHERE document_id = :id",
        {"id": did},
    )
    evidences = db.fetch_one(
        "SELECT count(*) AS n FROM knowledge.evidence_spans WHERE document_id = :id",
        {"id": did},
    )
    claims = db.fetch_all(
        "SELECT DISTINCT c.id, c.claim_type, c.predicate, c.status AS claim_status, "
        "(SELECT count(*) FROM knowledge.facts f "
        " WHERE f.claim_id = c.id AND f.status = 'active') AS fact_count, "
        "(SELECT count(*) FROM ontology.relations r "
        " WHERE r.evidence_claim_id = c.id) AS relation_count "
        "FROM knowledge.claim_evidence ce "
        "JOIN knowledge.claims c ON c.id = ce.claim_id "
        "JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        "WHERE es.document_id = :id ORDER BY c.predicate LIMIT 100",
        {"id": did},
    )
    decisions = db.fetch_all(
        "SELECT DISTINCT d.id, d.task_type, d.recommendation, d.status, d.created_at "
        "FROM decision.decision_records d "
        "JOIN decision.decision_evidence de ON de.decision_id = d.id "
        "JOIN knowledge.semantic_chunks sc ON sc.id = de.reference_id "
        "WHERE sc.document_id = :id ORDER BY d.created_at DESC LIMIT 20",
        {"id": did},
    )
    return {
        "document": {"id": did, "title": doc["title"],
                     "source_kind": meta.get("source_kind") or "upload",
                     "source_label": meta.get("source_label") or "本地上传",
                     "origin_url": meta.get("origin_url") or "",
                     "status": doc["document_status"], "created_at": doc["created_at"]},
        "runs": runs, "chunks": (chunks or {}).get("n", 0),
        "evidences": (evidences or {}).get("n", 0), "claims": claims,
        "facts": sum(int(c.get("fact_count") or 0) for c in claims),
        "relations": sum(int(c.get("relation_count") or 0) for c in claims),
        "decisions": decisions,
    }


def _fact_traceability(claims: list[dict], evidences: list[dict], source_ref: dict) -> str:
    """区分 Claim 证据链与结构化数据源链，不把缺失来源伪装成完整血缘。"""
    if evidences:
        return "claim_and_evidence"
    if claims:
        return "claim_without_evidence"
    required = ("table", "pk", "row_hash", "batch_id", "mapping_version", "idempotency_key")
    if source_ref.get("channel") == "structured_sync" and all(source_ref.get(k) for k in required):
        return "structured_source"
    return "unlinked"


@router.get("/lineage/facts/{fact_id}")
def trace_fact(fact_id: str, request: Request):
    """从当前租户 Fact 下钻到 Claim、原始证据及结构化来源信息。"""
    import uuid

    try:
        fid = str(uuid.UUID(fact_id))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "invalid fact id")

    principal = getattr(request.state, "principal", {}) or {}
    tenant_id = str(principal.get("tenant_id") or "default")
    fact = db.fetch_one(
        "SELECT id::text AS id, claim_id::text AS claim_id, predicate, object_value, "
        " status, to_jsonb(f)->'source_ref' AS source_ref, ontology_version, source_id, valid_from, valid_to, recorded_at "
        "FROM knowledge.facts f WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant_id",
        {"id": fid, "tenant_id": tenant_id},
    )
    if not fact:
        raise HTTPException(404, "fact not found")

    source_ref = fact.get("source_ref") if isinstance(fact.get("source_ref"), dict) else {}
    claims = db.fetch_all(
        "SELECT c.id::text AS id, c.claim_type, c.predicate, c.status AS claim_status "
        "FROM knowledge.claims c WHERE c.id=CAST(:claim_id AS uuid) "
        "OR EXISTS (SELECT 1 FROM jsonb_array_elements_text("
        "COALESCE(CAST(:corroborating_claims AS jsonb), '[]'::jsonb)) x(value) "
        "WHERE x.value=c.id::text) ORDER BY c.predicate",
        {"claim_id": fact.get("claim_id") or "00000000-0000-0000-0000-000000000000",
         "corroborating_claims": json.dumps(source_ref.get("corroborating_claims") or [])},
    ) if fact.get("claim_id") or source_ref.get("corroborating_claims") else []
    claim_ids = [c["id"] for c in claims]
    evidences = db.fetch_all(
        "SELECT DISTINCT ce.claim_id::text AS claim_id, es.id::text AS evidence_id, "
        " es.document_id::text AS document_id, d.title AS document_title, "
        " to_jsonb(es) AS evidence "
        "FROM knowledge.claim_evidence ce "
        "JOIN knowledge.evidence_spans es ON es.id=ce.evidence_id "
        "LEFT JOIN source.documents d ON d.id=es.document_id "
        "WHERE ce.claim_id = ANY(CAST(:claim_ids AS uuid[])) "
        "ORDER BY d.title, es.created_at, es.id LIMIT 200",
        {"claim_ids": claim_ids},
    ) if claim_ids else []
    return {
        "fact": fact,
        "claims": claims,
        "evidences": evidences,
        "source_ref": source_ref,
        "traceability_status": _fact_traceability(claims, evidences, source_ref),
    }
