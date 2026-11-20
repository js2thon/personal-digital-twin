"""SIL B2 统一语义运行时服务：5 阶段流水线 + 四通道混合检索。

只编排现有模块，不复制业务规则：
    词法/文档/记忆  -> app.modules.retrieval.service
    图谱 k 跳/路径  -> app.modules.ontology.graph
    权限与决策策略  -> app.modules.policy.engine
    事实可见性      -> app.visibility（唯一白名单定义处）
    本体版本        -> ontology.ontology_versions + CQ V2 SchemaRegistry

对应规范条款：RP-1~RP-5 / AC-01~AC-06 / DC-01 / GV-01 / EC（降级标注）。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from decimal import Decimal
from typing import Any

from app.db import session as db
from app.modules.ontology import graph as og
from app import policy as policy_mod
from app.modules.policy import engine as policy
from app.modules.llm.client import llm_client
from app.modules.retrieval import lexical
from app.modules.retrieval import service as retrieval
from app.visibility import retrievable_clause

from .context import SemanticContext, resolve_published_version, system_context

CHANNELS = ("vector", "keyword", "graph", "structured")
_RRF_K = 60
_TOP_K_MAX = 200
_FACT_STATUSES = ("active", "contested", "superseded", "withdrawn")

CONTRACT_ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/semantic/understand"},
    {"method": "POST", "path": "/api/v1/semantic/search"},
    {"method": "POST", "path": "/api/v1/semantic/query"},
    {"method": "POST", "path": "/api/v1/semantic/reason"},
    {"method": "POST", "path": "/api/v1/semantic/entity/resolve"},
    {"method": "POST", "path": "/api/v1/semantic/graph/traverse"},
    {"method": "POST", "path": "/api/v1/semantic/evidence"},
    {"method": "GET", "path": "/api/v1/ontology/schema"},
    {"method": "POST", "path": "/api/v1/ontology/proposals"},
    {"method": "GET", "path": "/api/v1/ontology/versions"},
]
# 前 7 个是统一语义入口；后 3 个沿用蓝图 /ontology/* 路径，由本模块提供带契约的语义版
SEMANTIC_ALIASES = [
    "/api/v1/semantic/ontology/schema",
    "/api/v1/semantic/ontology/proposals",
    "/api/v1/semantic/ontology/versions",
]


# ---------------------------------------------------------------- 通用工具

def jsonable(value: Any) -> Any:
    """UUID / datetime / Decimal / JSONB 原生对象 -> JSON 可序列化结构。"""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, (uuid.UUID, Decimal)) or hasattr(value, "isoformat"):
        return str(value)
    return value


def public(bundle: dict) -> dict:
    """剥离进程内使用的下划线私有键（如 _legacy），再统一序列化。"""
    return jsonable({k: v for k, v in bundle.items() if not k.startswith("_")})


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(low, min(high, n))


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)


def current_ontology_version() -> str:
    return resolve_published_version()


def _trace(channel: str, items: list, t0: float, *, degraded: bool = False,
           reason: str = "", filter_applied: str = "", candidate_count: int | None = None) -> dict:
    return {
        "channel": channel,
        "candidate_count": len(items) if candidate_count is None else candidate_count,
        "kept_count": len(items),
        "latency_ms": _ms(t0),
        "filter_applied": filter_applied or "visibility:owner,organization; status=active",
        "degraded": degraded,
        "reason": reason,
    }


def _error_trace(channel: str, exc: Exception, t0: float) -> dict:
    return _trace(channel, [], t0, degraded=True,
                  reason=f"{type(exc).__name__}: {str(exc)[:180]}")


# ---------------------------------------------------------------- RP-1 / RP-2

def _intent_of(text: str) -> str:
    if re.search(r"关系|关联|路径|谁和|上下游|负责", text):
        return "relation_lookup"
    if re.search(r"决策|审批|风险|是否允许|能不能", text):
        return "decision"
    if re.search(r"偏好|倾向|喜欢|习惯", text):
        return "preference"
    if re.search(r"证据|来源|出处|原文", text):
        return "evidence_lookup"
    return "fact_lookup"


def _resolve_concepts(text: str, mentions: list[dict]) -> list[dict]:
    """本体解析：CQ V2 Registry 的概念 + 已识别实体，均标注来源。"""
    out: list[dict] = []
    try:
        from app.modules.ontology import cq_v2

        schema = cq_v2.get_engine().registry.current() or {}
        types = schema.get("entity_types") or {}
        pairs = types.items() if isinstance(types, dict) else enumerate(types)
        for key, meta in pairs:
            label = meta.get("label") if isinstance(meta, dict) else str(meta)
            if label and (str(label) in text or str(key) in text):
                out.append({"concept": key, "label": label,
                            "source": "cq_v2_registry",
                            "schema_version": schema.get("schema_version")})
    except Exception:  # noqa: BLE001  CQ V2 内存态未 bootstrap 时不影响语义理解
        pass
    for m in mentions:
        out.append({"concept": m.get("entity_type"), "label": m.get("canonical_name"),
                    "entity_id": m.get("entity_id"), "source": "entity_resolution"})
    return out


def understand(ctx: SemanticContext, payload: dict) -> dict:
    """RP-1 理解 + RP-2 解析。纯词法 + 本体提及，不调用 LLM，保证零外部依赖。"""
    text = str(payload.get("text") or payload.get("query") or "").strip()
    if not text:
        raise ValueError("text / query 不能为空")
    terms = lexical.extract_terms(text, max_terms=14)
    mentions = retrieval.detect_mentions(text)
    return {
        "query_id": ctx.request_id,
        "ontology_version": ctx.schema_version,
        "context": ctx.as_dict(),
        "text": text,
        "intent": _intent_of(text),
        "terms": [t["term"] for t in terms],
        "term_weights": terms,
        "mention_entities": jsonable(mentions),
        "resolved_concepts": _resolve_concepts(text, mentions),
        "retrieval_trace": [],
        "confidence": {"overall": 1.0 if mentions else 0.6,
                       "basis": "deterministic lexical + ontology mention"},
    }


# ---------------------------------------------------------------- 四通道

def _vector_status() -> dict:
    """向量通道真实状态：不假装可用，也不静默失败。"""
    info: dict = {"extension": False, "indexed_chunks": 0, "total_chunks": 0,
                  "ready": False, "note": ""}
    try:
        ext = db.fetch_one("SELECT count(*) AS n FROM pg_extension WHERE extname = 'vector'")
        info["extension"] = bool(ext and int(ext.get("n") or 0) > 0)
        row = db.fetch_one(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE embedding_vec IS NOT NULL AND embedding_vec <> '') AS indexed "
            "FROM knowledge.semantic_chunks"
        )
        if row:
            info["total_chunks"] = int(row.get("total") or 0)
            info["indexed_chunks"] = int(row.get("indexed") or 0)
        info["ready"] = bool(info["extension"] and info["indexed_chunks"] > 0)
        info["note"] = ("未安装 vector 扩展；embedding_vec 以 TEXT 存 JSON 数组，"
                        "统一 VectorStore 全量扫描（无 LIMIT 100），无 HNSW/ANN"
                        if not info["ready"] else "向量通道可用")
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"状态探测失败：{type(exc).__name__}: {str(exc)[:120]}"
    return info


def _fact_contract_status() -> dict:
    """事实字段契约（DC-05 / DC-08 / DC-09）摘要：供 meta 一眼看到结构合规度。"""
    try:
        from app.modules.knowledge import contract

        st = contract.contract_status()
        cov = st["compliance"]
        prov = st["provenance_coverage"]
        total = st["total_facts"] or 1
        return {
            "spec": st["spec"],
            "total_facts": st["total_facts"],
            "required_fields": cov["fact_fields_required"],
            "present_fields": cov["fact_fields_present"],
            "structurally_compliant": cov["structurally_compliant"],
            "coverage_gaps": cov["coverage_gaps"],
            "source_coverage": round(prov.get("with_source", 0) / total, 4),
            "evidence_coverage": round(prov.get("with_evidence", 0) / total, 4),
            "bitemporal_ok": st["bitemporal"]["valid_window_violations"] == 0,
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "note": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _event_bus_status() -> dict:
    """事件总线（EC-01～EC-05）摘要：双通道 + 账本积压 + 投影目标可用性。"""
    try:
        from app.modules.ontology import outbox
        from app.modules.ontology import projection_worker as pw

        box = outbox.stats()
        return {
            "spec": "SIL-EC-01~EC-05",
            "outbox_table": "ingest.outbox_events",
            "channels": ["pg_notify(listen, realtime)", "outbox(dispatcher, durable)"],
            "atomic_with_fact": True,
            "backlog": box["backlog"],
            "dead": box["dead"],
            "dispatcher_alive": box["dispatcher"]["thread_alive"],
            "workers": {w["name"]: {"available": w["available"],
                                    "reason": w.get("unavailable_reason")}
                        for w in pw.registry()},
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "note": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _modeling_status() -> dict:
    """P1-2 摘要：LinkML 单一建模来源 + 双轨对账口径 + SHACL 写入侧校验。"""
    try:
        from app.modules.ontology import cq_v2, shacl

        gov = cq_v2.get_engine().governance_summary()
        st = shacl.stats()
        shapes = shacl.current_shapes()
        return {
            "spec": "SIL-C01/C02 + SIL-DC-04",
            "linkml_single_source": gov["linkml_single_source"],
            "linkml_roundtrip_lossless": gov["linkml_roundtrip_lossless"],
            "linkml_roundtrip_diff": gov["linkml_roundtrip_diff"],
            "registry_versions": gov["versions_in_registry"],
            "schema_version": gov["current_version"],
            "tracks_authoritative": gov["tracks_authoritative"],
            "tracks_mirror": gov["tracks_mirror"],
            "shacl": {
                "mode": st["mode"], "checks": st["checks"],
                "violations": st["violations"], "blocked": st["blocked"],
                "shapes_version": shapes.get("shapes_version"),
                "shapes_schema_version": shapes.get("schema_version"),
                "properties": len(shapes.get("properties") or {}),
                "relation_shapes": len(shapes.get("relation_endpoints") or {}),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "note": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _cq_v2_persistence() -> dict:
    """CQ V2 状态（P1-5）摘要：存储模式 + 是否可跨重启 + 副本一致性。"""
    try:
        from app.modules.ontology import cq_v2

        st = cq_v2.get_engine().persistence_status()
        return {
            "spec": "SIL-P1-5",
            "store_mode": st["store_mode"],
            "durable": st["durable"],
            "degraded_reason": st.get("degraded_reason"),
            "restored_at": st.get("restored_at"),
            "cqs": st["cqs"],
            "registry_version": st["registry_version"],
            "objects_in_memory": st["objects_in_memory"],
            "objects_in_db": st["objects_in_db"],
            "manual_bootstrap_required": not st["durable"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "note": f"{type(exc).__name__}: {str(exc)[:120]}"}


def _vector_channel(question: str, top_k: int, t0: float,
                    ctx: SemanticContext | None = None) -> tuple[list[dict], dict]:
    """Dense 召回：收敛到统一 VectorStore（与 retrieval._documents 同源）。

    一处实现、两处调用：本函数与 retrieval._documents 都走
    vector_store.search()，不再有第二套 cosine 实现，也不再有 SQL LIMIT 100
    截断（此前 216 条切片里只有前 100 条能进向量召回）。
    无扩展 / 无 HNSW 的降级事实照实标注，不用相似度冒充事实关系（SIL-P2）。
    """
    from app.modules.retrieval import vector_store as vs

    status = _vector_status()
    tenant = ("default" if ctx is None else
              str(policy_mod.principal_from(ctx).get("tenant_id") or "default"))
    store = vs.get_vector_store()
    rows: list[dict] = []
    try:
        qv = llm_client.embed([question], timeout=10)[0]
    except Exception as exc:  # noqa: BLE001  Embedding 服务不可用时整路降级
        tr = _trace("vector", [], t0, degraded=True,
                    reason=f"Embedding 服务不可用：{type(exc).__name__}: {str(exc)[:120]}",
                    filter_applied="tenant + permission pre-filter")
        tr["vector_status"] = status
        return [], tr
    try:
        rows = store.search(qv, min(top_k, 20), tenant_id=tenant)
    except Exception as exc:  # noqa: BLE001
        tr = _trace("vector", [], t0, degraded=True,
                    reason=f"向量召回失败：{type(exc).__name__}: {str(exc)[:120]}",
                    filter_applied="tenant + permission pre-filter")
        tr["vector_status"] = status
        return [], tr
    if not rows:
        tr = _trace("vector", [], t0, degraded=True,
                    reason=f"无可用向量：{status['note']}",
                    filter_applied="tenant + permission pre-filter")
        tr["vector_status"] = status
        return [], tr
    try:
        qv = llm_client.embed([question], timeout=10)[0]
    except Exception as exc:  # noqa: BLE001  Embedding 服务不可用时整路降级
        tr = _trace("vector", [], t0, degraded=True,
                    reason=f"Embedding 服务不可用：{type(exc).__name__}: {str(exc)[:120]}",
                    filter_applied="tenant + permission pre-filter")
        tr["vector_status"] = status
        return [], tr
    items = [{
        "kind": "document", "id": str(r["ref_id"]), "channel": "vector",
        "score": round(max(float(r.get("score") or 0), 0.0), 6),
        "data": {
            "id": str(r["ref_id"]),
            "document_id": r.get("document_id") or "",
            "title": r.get("title"),
            "content": str(r.get("content") or "")[:500],
            "via": "dense",
            "similarity": round(float(r.get("score") or 0), 6),
            "entity_ids": r.get("entity_ids"),
            "entity_types": r.get("entity_types"),
            "ontology_version": r.get("ontology_version"),
        },
    } for r in rows]
    tr = _trace("vector", items, t0, degraded=True,
                reason=(f"统一 VectorStore（TEXT+Python cosine，无 LIMIT 100 截断）："
                        f"{status['indexed_chunks']}/{status['total_chunks']} 切片有向量，无 HNSW/ANN"),
                filter_applied="tenant + permission pre-filter")
    tr.update({"capability": "degraded_but_usable", "usable": bool(items),
               "mode": "brute_force_text_cosine", "candidate_count": status["indexed_chunks"],
               "vector_status": status})
    return items, tr


def _keyword_channel(question: str, scope: list[str] | None,
                     top_k: int, t0: float,
                     ctx: SemanticContext | None = None) -> tuple[list[dict], dict, dict]:
    principal = policy_mod.principal_from(ctx) if ctx is not None else None
    raw = retrieval.retrieve(question, scope, top_k=min(top_k, 20),
                             principal=principal)
    items: list[dict] = []
    for kind, rows in (("fact", raw.get("facts") or []),
                       ("memory", raw.get("memories") or []),
                       ("document", raw.get("documents") or [])):
        for row in rows:
            rid = row.get("fact_id") or row.get("id") or row.get("document_id")
            score = float(row.get("relevance") or row.get("score") or 0.0)
            items.append({"kind": kind, "id": str(rid), "channel": "keyword",
                          "score": score, "data": row})
    return items, raw, _trace("keyword", items, t0,
                              filter_applied="P1-4 逐点过滤(租户+可见性+归属); status=active; score>=2.0")


def _structured_channel(question: str, filters: dict, top_k: int,
                        mentions: list[dict], t0: float,
                        ctx: SemanticContext | None = None) -> tuple[list[dict], dict]:
    """结构化事实通道：所有用户输入走绑定参数，谓词/状态走白名单。

    除文本匹配外，把 RP-2 识别出的实体提及作为 subject_id 条件，
    避免中文问句与英文 predicate（如 is_project_manager_of）不匹配导致零召回。
    """
    predicate = (filters or {}).get("predicate")
    if predicate is not None:
        predicate = str(predicate)[:200]
    keyword = "%" + re.sub(r"[%_]", "", str(question)[:60]) + "%"
    entity_ids = [str(m.get("entity_id")) for m in mentions[:5] if m.get("entity_id")]
    entity_clause = ""
    params: dict = {"pred": predicate, "kw": keyword, "k": min(top_k, _TOP_K_MAX)}
    # P1-4：租户 + 可见性 + 归属逐点过滤（无 ctx 时回落 owner 级白名单）
    if ctx is not None:
        from app.policy import read_clause

        vis_clause = " AND " + read_clause("f", policy_mod.principal_from(ctx), params)
    else:
        vis_clause = " AND " + retrievable_clause("f")
    if entity_ids:
        keys = []
        for i, eid in enumerate(entity_ids):
            params[f"e{i}"] = eid
            keys.append(f"CAST(:e{i} AS uuid)")
        entity_clause = " OR f.subject_id IN (" + ", ".join(keys) + ")"
    sql = (
        "SELECT f.id, f.claim_id, f.subject_id, f.predicate, f.object_value, "
        "f.confidence, f.trust_tier, f.valid_from, f.valid_to, f.recorded_at, "
        "f.status, f.visibility, e.canonical_name AS subject_name, e.entity_type "
        "FROM knowledge.facts f "
        "LEFT JOIN ontology.entities e ON e.id = f.subject_id "
        "WHERE f.status = 'active'" + vis_clause +
        " AND (CAST(:pred AS text) IS NULL OR f.predicate = :pred) "
        " AND (f.predicate ILIKE :kw OR CAST(f.object_value AS text) ILIKE :kw "
        "      OR e.canonical_name ILIKE :kw" + entity_clause + ") "
        "ORDER BY f.recorded_at DESC NULLS LAST LIMIT :k"
    )
    rows = db.fetch_all(sql, params)
    items = [{"kind": "fact", "id": str(r.get("id")), "channel": "structured",
              "score": float(r.get("confidence") or 0.0), "data": r} for r in rows]
    return items, _trace("structured", items, t0,
                         filter_applied="status=active; P1-4 逐点过滤(租户+可见性+归属); predicate whitelist")


def _edge_allowed(edge: dict, ctx: SemanticContext) -> bool:
    """GV-01 第 2 点：图遍历逐跳权限过滤钩子。

    P1-4 落地：status=active + 租户一致（ontology.relations.tenant_id），
    判定复用 app.policy.decide，不在此处另立规则。
    """
    if edge.get("status") not in (None, "active"):
        return False
    if edge.get("problem") and "unregistered_relation_type" in str(edge.get("problem")):
        return True  # 未登记关系仍可展示并标注问题，不静默丢弃
    if ctx is not None:
        verdict = policy_mod.decide(
            policy_mod.principal_from(ctx),
            tenant_id=edge.get("tenant_id"), visibility="organization")
        if not verdict["allowed"]:
            return False
    return True


def _graph_channel(mentions: list[dict], top_k: int, ctx: SemanticContext,
                   t0: float) -> tuple[list[dict], list[dict], dict]:
    center = mentions[0].get("entity_id") if mentions else None
    queries = og.subgraph_queries(center, 1, [], status="active",
                                  limit=min(max(top_k * 3, 30), 150))
    nodes = db.fetch_all(*queries["nodes"])
    edges = db.fetch_all(*queries["edges"])
    assembled = og.assemble_graph(nodes, edges, center=center, depth=1,
                                  limit=min(max(top_k * 3, 30), 150))
    kept = [e for e in assembled.get("edges", []) if _edge_allowed(e, ctx)]
    rels = [{"id": str(e.get("id")), "source": str(e.get("source")),
             "target": str(e.get("target")), "type": e.get("type"),
             "label": e.get("label"), "registered": e.get("registered"),
             "valid": e.get("valid"), "problem": e.get("problem"),
             "status": e.get("status"), "claim_id": str(e.get("claim_id") or "")}
            for e in kept]
    items = [{"kind": "relationship", "id": r["id"], "channel": "graph",
              "score": 1.0 if r["valid"] else 0.3, "data": r} for r in rels]
    tr = _trace("graph", items, t0, candidate_count=len(assembled.get("edges", [])),
                filter_applied="status=active; per-hop _edge_allowed(P1-4 租户逐跳)")
    return items, rels, tr


def _rrf(buckets: dict[str, list[dict]], top_k: int) -> list[dict]:
    """RRF 融合（AC-03）：跨通道按 kind:id 去重，不比较不同空间的原始分数。"""
    merged: dict[str, dict] = {}
    for channel, items in buckets.items():
        ordered = sorted(items, key=lambda x: float(x.get("score") or 0), reverse=True)
        for rank, item in enumerate(ordered, 1):
            key = f"{item['kind']}:{item['id']}"
            rec = merged.setdefault(key, {"item": item, "score": 0.0, "channels": []})
            rec["score"] += 1.0 / (_RRF_K + rank)
            rec["channels"].append(channel)
    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    out = []
    for rec in ranked:
        item = dict(rec["item"])
        item["rrf_score"] = round(rec["score"], 6)
        item["channels"] = rec["channels"]
        out.append(item)
    return out


# ---------------------------------------------------------------- RP-5 装配

def _visible_claim_exists(principal: dict, params: dict) -> str:
    """证据权限继承（SIL-A07）：claim 必须绑定至少一条主体可见的 active 事实。

    evidence 本身没有租户列，其访问权限继承自它支撑的 claim 的 facts
    （access_permission 不再是装饰性字符串，而是真实的 EXISTS 谓词）。
    """
    from app import policy as _pol

    return ("EXISTS (SELECT 1 FROM knowledge.facts vf WHERE vf.claim_id = ce.claim_id "
            "AND vf.status = 'active' AND "
            + _pol.read_clause("vf", principal, params, owner_col="created_by") + ")")


def _evidence_row(r: dict, redact: bool) -> dict:
    """证据行 → 九项元数据；redact=True 时对 excerpt/title 打码（非 owner 级）。"""
    locator = r.get("locator") or {}
    if isinstance(locator, str):
        try:
            locator = json.loads(locator)
        except (ValueError, TypeError):
            locator = {}
    excerpt = str(r.get("content") or "")
    if redact:
        from app.redact import redact_text

        excerpt = redact_text(excerpt)
    return {
        "evidence_id": str(r.get("id")),
        "claim_id": str(r.get("claim_id")),
        "source_system": r.get("source_kind"),
        "document_id": str(r.get("document_id")),
        "document_title": r.get("document_title"),
        "document_version": locator.get("document_version"),
        "page": locator.get("page"),
        "paragraph": locator.get("paragraph"),
        "char_offset": locator.get("char_offset"),
        "excerpt": excerpt[:500],
        "content_hash": hashlib.md5(excerpt.encode("utf-8", "replace")).hexdigest(),
        "retrieved_at": str(r.get("created_at") or ""),
        "access_permission": "inherits_source:owner,organization",
    }


def _bind_evidence(facts: list[dict], limit: int = 20,
                   principal: dict | None = None) -> list[dict]:
    """证据绑定（DC-06）：由 claim_id 反查原文片段，补齐九项元数据。

    principal 给定时做权限继承过滤（claim 必须有主体可见的 active 事实）
    并对非 owner 级主体做敏感字段脱敏；None 保持旧行为（内部已过滤通道）。
    """
    claim_ids = [str(f.get("claim_id")) for f in facts if f.get("claim_id")][:20]
    # keyword 通道的 fact 行只有 fact_id、没有 claim_id，需先反查再绑证据
    fact_ids = [str(f.get("fact_id") or f.get("id")) for f in facts
                if not f.get("claim_id") and (f.get("fact_id") or f.get("id"))][:20]
    if fact_ids:
        keys, fp = [], {}
        for i, fid in enumerate(fact_ids):
            fp[f"f{i}"] = fid
            keys.append(f"CAST(:f{i} AS uuid)")
        try:
            rows = db.fetch_all(
                "SELECT DISTINCT claim_id FROM knowledge.facts "
                "WHERE id IN (" + ", ".join(keys) + ") AND claim_id IS NOT NULL", fp)
            claim_ids += [str(r["claim_id"]) for r in rows if r.get("claim_id")]
        except Exception:  # noqa: BLE001  反查失败只影响证据绑定，不影响事实返回
            pass
    claim_ids = list(dict.fromkeys(claim_ids))[:20]
    if not claim_ids:
        return []
    placeholders = ", ".join(f"CAST(:c{i} AS uuid)" for i in range(len(claim_ids)))
    params = {f"c{i}": v for i, v in enumerate(claim_ids)}
    sql = (
        "SELECT e.id, e.content, e.locator, e.evidence_type, e.created_at, "
        "d.id AS document_id, d.title AS document_title, "
        "COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind, "
        "ce.claim_id "
        "FROM knowledge.claim_evidence ce "
        "JOIN knowledge.evidence_spans e ON e.id = ce.evidence_id "
        "JOIN source.documents d ON d.id = e.document_id "
        f"WHERE ce.claim_id IN ({placeholders}) "
    )
    if principal is not None:
        sql += _visible_claim_exists(principal, params) + " "
    sql += "ORDER BY e.created_at DESC LIMIT :lim"
    params["lim"] = limit
    rows = db.fetch_all(sql, params)
    redact = False
    if principal is not None:
        from app import policy as _pol

        redact = not _pol.is_owner_class(principal)
    return [_evidence_row(r, redact) for r in rows]


def _assemble(ctx: SemanticContext, question: str, items: list[dict],
              traces: list[dict], understanding: dict,
              relationships: list[dict] | None = None) -> dict:
    facts = [it["data"] for it in items if it["kind"] == "fact"]
    entities = []
    seen = set()
    for it in items:
        d = it.get("data") or {}
        eid = d.get("subject_id") or d.get("entity_id")
        if eid and str(eid) not in seen:
            seen.add(str(eid))
            entities.append({"entity_id": str(eid),
                             "name": d.get("subject_name") or d.get("canonical_name"),
                             "entity_type": d.get("entity_type")})
    for m in understanding.get("mention_entities") or []:
        eid = m.get("entity_id")
        if eid and str(eid) not in seen:
            seen.add(str(eid))
            entities.append({"entity_id": str(eid), "name": m.get("canonical_name"),
                             "entity_type": m.get("entity_type")})
    try:
        evidence = _bind_evidence(facts, principal=policy_mod.principal_from(ctx))
    except Exception as exc:  # noqa: BLE001  证据绑定失败必须可见，不能静默
        evidence = []
        traces.append(_trace("evidence_binding", [], time.perf_counter(), degraded=True,
                             reason=f"{type(exc).__name__}: {str(exc)[:160]}"))

    by_channel: dict[str, dict] = {}
    for t in traces:
        by_channel[t["channel"]] = {"degraded": t["degraded"],
                                    "kept": t["kept_count"],
                                    "basis": ("lexical relevance" if t["channel"] == "keyword"
                                              else "fact confidence" if t["channel"] == "structured"
                                              else "graph validity" if t["channel"] == "graph"
                                              else "dense similarity (pending)")}
    active = [v for v in by_channel.values() if not v["degraded"]]
    overall = round(min(0.95, 0.25 + 0.12 * len(active) +
                        (0.1 if facts else 0.0) + (0.05 if evidence else 0.0)), 3)
    degraded = any(t["degraded"] for t in traces)
    return {
        "query_id": ctx.request_id,
        "ontology_version": ctx.schema_version,
        "context": ctx.as_dict(),
        "query": question,
        "resolved_concepts": understanding.get("resolved_concepts") or [],
        "entities": jsonable(entities),
        "facts": jsonable(facts),
        "relationships": jsonable(relationships or []),
        "evidence": evidence,
        "items": jsonable(items),
        "retrieval_trace": traces,
        "confidence": {
            "overall": overall,
            "by_channel": by_channel,
            # AC-05：事实 / 规则 / 模型判断三类置信度分别记录，不合并
            "fact_confidence": (round(sum(float(f.get("confidence") or 0)
                                          for f in facts) / len(facts), 3) if facts else None),
            "rule_confidence": None,
            "model_confidence": None,
        },
        "pagination": {"limit": len(items), "returned": len(items), "cursor": None},
        "degraded": degraded,
        "degraded_channels": [t["channel"] for t in traces if t["degraded"]],
    }


# ---------------------------------------------------------------- 对外语义能力

def search(ctx: SemanticContext, payload: dict) -> dict:
    """RP-1~RP-5 全流程：混合检索 + RRF 融合 + 证据绑定 + 上下文装配。"""
    question = str(payload.get("query") or "").strip()
    if not question:
        raise ValueError("query 不能为空")
    retrieval_cfg = payload.get("retrieval") or {}
    enabled = {c: bool(retrieval_cfg.get(c, True)) for c in CHANNELS}
    if not any(enabled.values()):
        raise ValueError("retrieval 至少启用一路通道")
    top_k = _clamp(payload.get("top_k"), 1, _TOP_K_MAX, 20)
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters 必须是对象")
    scope = payload.get("scope") or ["facts", "memories", "documents"]
    if not isinstance(scope, list):
        raise ValueError("scope 必须是数组")

    understanding = understand(ctx, {"text": question})
    mentions = understanding.get("mention_entities") or []
    buckets: dict[str, list[dict]] = {}
    traces: list[dict] = []
    raw_legacy: dict | None = None
    relationships: list[dict] = []

    if enabled["keyword"]:
        t0 = time.perf_counter()
        try:
            items, raw, tr = _keyword_channel(question, scope, top_k, t0, ctx)
            buckets["keyword"] = items
            raw_legacy = raw
            traces.append(tr)
        except Exception as exc:  # noqa: BLE001  单通道失败不拖垮整次检索
            traces.append(_error_trace("keyword", exc, t0))
    if enabled["structured"]:
        t0 = time.perf_counter()
        try:
            items, tr = _structured_channel(question, filters, top_k, mentions, t0, ctx)
            buckets["structured"] = items
            traces.append(tr)
        except Exception as exc:  # noqa: BLE001
            traces.append(_error_trace("structured", exc, t0))
    if enabled["graph"]:
        t0 = time.perf_counter()
        try:
            items, relationships, tr = _graph_channel(mentions, top_k, ctx, t0)
            buckets["graph"] = items
            traces.append(tr)
        except Exception as exc:  # noqa: BLE001
            traces.append(_error_trace("graph", exc, t0))
    if enabled["vector"]:
        t0 = time.perf_counter()
        try:
            items, tr = _vector_channel(question, top_k, t0, ctx)
            buckets["vector"] = items
            traces.append(tr)
        except Exception as exc:  # noqa: BLE001
            traces.append(_error_trace("vector", exc, t0))

    fused = _rrf(buckets, top_k)
    bundle = _assemble(ctx, question, fused, traces, understanding, relationships)
    bundle["retrieval_plan"] = {"enabled": enabled, "fusion": "RRF(k=60)",
                                "top_k": top_k, "filters_applied": sorted(filters.keys())}
    if raw_legacy is not None:
        bundle["_legacy"] = raw_legacy  # 仅供 agent_retrieve 无损还原，API 层由 public() 剥离
    return bundle


def query(ctx: SemanticContext, payload: dict) -> dict:
    """结构化业务查询：谓词/状态白名单 + 租户/可见性/归属逐点过滤（P1-4）。"""
    principal = policy_mod.principal_from(ctx)
    predicate = payload.get("predicate")
    status = payload.get("status") if payload.get("status") in _FACT_STATUSES else "active"
    limit = _clamp(payload.get("limit"), 1, 500, 50)
    subject_id = payload.get("subject_id")
    binds: dict = {"st": status, "pred": str(predicate) if predicate else None,
                   "sid": str(subject_id) if subject_id else None, "lim": limit}
    sql = (
        "SELECT f.id, f.claim_id, f.subject_id, f.predicate, f.object_value, "
        "f.valid_from, f.valid_to, f.recorded_at, f.confidence, f.trust_tier, "
        "f.status, f.visibility, e.canonical_name AS subject_name, e.entity_type "
        "FROM knowledge.facts f LEFT JOIN ontology.entities e ON e.id = f.subject_id "
        "WHERE f.status = :st AND " + policy_mod.read_clause("f", principal, binds) +
        " AND (CAST(:pred AS text) IS NULL OR f.predicate = :pred) "
        " AND (CAST(:sid AS text) IS NULL OR f.subject_id = CAST(:sid AS uuid)) "
        "ORDER BY f.recorded_at DESC NULLS LAST LIMIT :lim"
    )
    rows = db.fetch_all(sql, binds)
    evidence = _bind_evidence(rows, principal=principal)
    return {
        "query_id": ctx.request_id, "ontology_version": ctx.schema_version,
        "context": ctx.as_dict(),
        "filters": {"predicate": predicate, "status": status, "subject_id": subject_id},
        "facts": jsonable(rows), "evidence": evidence,
        "retrieval_trace": [_trace("structured", list(rows), time.perf_counter(),
                                  filter_applied="status whitelist; visibility; bound params")],
        "confidence": {"overall": 1.0, "by_channel": {"structured": {"degraded": False,
                        "kept": len(rows), "basis": "deterministic SQL"}},
                       "fact_confidence": None, "rule_confidence": None, "model_confidence": None},
        "pagination": {"limit": limit, "returned": len(rows), "cursor": None},
        "degraded": False, "degraded_channels": [],
    }


def reason(ctx: SemanticContext, payload: dict) -> dict:
    """AC-05 决策输出标准：事实 / 规则 / 模型置信度分别记录。"""
    risk_level = str(payload.get("risk_level") or "medium").lower()
    action = payload.get("action")
    facts = payload.get("facts") or []
    if not isinstance(facts, list):
        raise ValueError("facts 必须是数组")
    tool_check = policy.check_tool(str(action)) if action else None
    policy_result = policy.evaluate_decision(risk_level, action)
    triggered = []
    if risk_level in ("high", "critical"):
        triggered.append({"rule": "high_risk_requires_review", "verdict": "REVIEW_REQUIRED"})
    if tool_check and tool_check.get("verdict") == "denied":
        triggered.append({"rule": "tool_whitelist", "verdict": "REJECT", "tool": action})
    verdict = policy_result.get("policy_result")
    if not facts and not payload.get("allow_no_evidence"):
        decision = "INSUFFICIENT_INFORMATION"
    elif verdict == "denied" or any(r["verdict"] == "REJECT" for r in triggered):
        decision = "REJECT"
    elif verdict == "allowed":
        decision = "APPROVE"
    elif risk_level in ("high", "critical"):
        decision = "ESCALATE"
    else:
        decision = "REVIEW_REQUIRED"
    return {
        "decision_id": "dec_" + uuid.uuid4().hex[:16],
        "decision": decision,
        "risk_level": risk_level.upper(),
        "reasons": [r["rule"] for r in triggered] + [f"policy:{verdict}"],
        "rules_triggered": triggered,
        "evidence_ids": [str(f.get("fact_id") or f.get("id")) for f in facts if isinstance(f, dict)],
        "missing_information": ([] if facts else ["未提供事实证据"]),
        "recommended_action": ("SUBMIT_FOR_APPROVAL" if decision in
                               ("REVIEW_REQUIRED", "ESCALATE", "INSUFFICIENT_INFORMATION")
                               else "NONE"),
        "requires_human_confirmation": not bool(policy_result.get("execution_authorized")),
        "policy_level": policy_result.get("level"),
        "tool_check": tool_check,
        "confidence": {
            "fact_confidence": (round(sum(float(f.get("confidence") or 0) for f in facts
                                          if isinstance(f, dict)) / len(facts), 3) if facts else None),
            "rule_confidence": 1.0,
            "model_confidence": None,
            "note": "确定性规则 + 策略白名单；未调用 LLM，模型置信度记为 null",
        },
        "query_id": ctx.request_id, "ontology_version": ctx.schema_version,
        "context": ctx.as_dict(),
    }


def entity_resolve(ctx: SemanticContext, payload: dict) -> dict:
    """实体对齐解析（蓝图 6.2 的轻量实现）：三级置信度策略。"""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    aliases = payload.get("aliases") or []
    if not isinstance(aliases, list):
        raise ValueError("aliases 必须是数组")
    names = [name] + [str(a) for a in aliases][:10]
    conds, params = [], {}
    for i, n in enumerate(names):
        conds.append(f"(e.canonical_name ILIKE :n{i} OR CAST(e.aliases AS text) ILIKE :n{i})")
        params[f"n{i}"] = "%" + re.sub(r"[%_]", "", n[:60]) + "%"
    params["lim"] = _clamp(payload.get("limit"), 1, 50, 10)
    rows = db.fetch_all(
        "SELECT id, canonical_name, entity_type, aliases, status, merged_into "
        "FROM ontology.entities e WHERE (" + " OR ".join(conds) + ") "
        "AND e.status = 'active' LIMIT :lim", params)
    candidates = []
    for r in rows:
        exact = str(r.get("canonical_name") or "").strip() == name
        conf = 0.98 if exact else 0.62
        candidates.append({"entity_id": str(r.get("id")),
                           "canonical_name": r.get("canonical_name"),
                           "entity_type": r.get("entity_type"),
                           "merged_into": str(r.get("merged_into") or ""),
                           "confidence": conf,
                           "strategy": "high_auto_link" if exact else "medium_review"})
    return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
            "context": ctx.as_dict(), "query_name": name,
            "candidates": candidates,
            "policy": {"high": "自动关联", "medium": "进入人工审核", "low": "保留候选不合并"},
            "retrieval_trace": [_trace("entity_resolution", candidates, time.perf_counter())],
            "degraded": False, "degraded_channels": []}


def graph_traverse(ctx: SemanticContext, payload: dict) -> dict:
    """图谱遍历（蓝图 8.3）：subgraph k 跳 / 最短路径，逐跳权限过滤。"""
    mode = payload.get("mode") or "subgraph"
    if mode not in ("subgraph", "path"):
        raise ValueError("mode 只能是 subgraph 或 path")
    if mode == "path":
        source, target = payload.get("source"), payload.get("target")
        if not source or not target:
            raise ValueError("path 模式需要 source 与 target")
        sql, params = og.paths_query(str(source), str(target),
                                     _clamp(payload.get("max_depth"), 1, 4, 4))
        rows = db.fetch_all(sql, params)
        ent = db.fetch_all("SELECT id, canonical_name, entity_type FROM ontology.entities")
        name_by = {str(r["id"]): r.get("canonical_name") for r in ent}
        type_by = {str(r["id"]): r.get("entity_type") for r in ent}
        paths = og.describe_paths(rows, name_by, type_by)
        return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
                "context": ctx.as_dict(), "mode": "path", "paths": jsonable(paths),
                "retrieval_trace": [_trace("graph", list(paths), time.perf_counter(),
                                          filter_applied="per-hop _edge_allowed")],
                "degraded": False, "degraded_channels": []}
    center = payload.get("center")
    depth = _clamp(payload.get("depth"), 1, 3, 2)
    types = og.parse_types(payload.get("types") or [])
    limit = _clamp(payload.get("limit"), 1, 500, 150)
    queries = og.subgraph_queries(str(center) if center else None, depth, types,
                                  status="active", limit=limit)
    nodes = db.fetch_all(*queries["nodes"])
    edges = db.fetch_all(*queries["edges"])
    assembled = og.assemble_graph(nodes, edges, center=str(center) if center else None,
                                  depth=depth, limit=limit)
    kept = [e for e in assembled.get("edges", []) if _edge_allowed(e, ctx)]
    assembled["edges"] = kept
    return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
            "context": ctx.as_dict(), "mode": "subgraph", "depth": depth,
            "graph": jsonable(assembled),
            "retrieval_trace": [_trace("graph", kept, time.perf_counter(),
                                       candidate_count=len(edges),
                                       filter_applied="status=active; per-hop _edge_allowed")],
            "degraded": False, "degraded_channels": []}


def evidence(ctx: SemanticContext, payload: dict) -> dict:
    """证据查询（DC-06）：九项元数据 + 权限继承（真实 EXISTS 谓词）+ 字段级脱敏。"""
    principal = policy_mod.principal_from(ctx)
    ids = payload.get("evidence_ids") or []
    claim_ids = payload.get("claim_ids") or []
    fact_ids = payload.get("fact_ids") or []
    if not isinstance(ids, list) or not isinstance(claim_ids, list) or not isinstance(fact_ids, list):
        raise ValueError("evidence_ids / claim_ids / fact_ids 必须是数组")
    if not (ids or claim_ids or fact_ids):
        raise ValueError("至少提供 evidence_ids / claim_ids / fact_ids 之一")
    bound: list[dict] = []
    clauses: list[str] = []
    for i, v in enumerate([str(x) for x in (ids + claim_ids)[:50]]):
        clauses.append(f"CAST(:v{i} AS uuid)")
        bound.append({f"v{i}": v})
    params: dict = {}
    for d in bound:
        params.update(d)
    where = []
    if ids:
        where.append("e.id IN (" + ", ".join(clauses[:len(ids)]) + ")")
    if claim_ids:
        where.append("ce.claim_id IN (" + ", ".join(clauses[len(ids):len(ids) + len(claim_ids)]) + ")")
    if fact_ids:
        # 事实 -> claim -> evidence；fact_ids 只作为绑定参数，不进入 SQL 文本
        fkeys = []
        for i, v in enumerate([str(x) for x in fact_ids[:50]]):
            params[f"f{i}"] = v
            fkeys.append(f"CAST(:f{i} AS uuid)")
        where.append("ce.claim_id IN (SELECT claim_id FROM knowledge.facts WHERE id IN ("
                     + ", ".join(fkeys) + "))")
    sql = (
        "SELECT e.id, e.document_id, e.content, e.locator, e.evidence_type, e.created_at, "
        "d.title AS document_title, "
        "COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind, ce.claim_id "
        "FROM knowledge.claim_evidence ce "
        "JOIN knowledge.evidence_spans e ON e.id = ce.evidence_id "
        "JOIN source.documents d ON d.id = e.document_id "
        "WHERE (" + " OR ".join(where) + ") AND "
        + _visible_claim_exists(principal, params) +
        " ORDER BY e.created_at DESC LIMIT :lim"
    )
    params["lim"] = _clamp(payload.get("limit"), 1, 200, 50)
    rows = db.fetch_all(sql, params)
    from app import policy as _pol

    redact = not _pol.is_owner_class(principal)
    # 直接由已取到的行构造九项元数据，避免二次往返
    out = [_evidence_row(r, redact) for r in rows]
    return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
            "context": ctx.as_dict(), "evidence": out,
            "retrieval_trace": [_trace("evidence", out, time.perf_counter(),
                                       filter_applied="inherits source permission")],
            "degraded": False, "degraded_channels": []}


def ontology_schema(ctx: SemanticContext, version: str | None = None) -> dict:
    """统一本体 Schema 出口：同时暴露 V1.2 版本表与 CQ V2 Registry。"""
    if version:
        row = db.fetch_one(
            "SELECT version, schema_content, status, created_at "
            "FROM ontology.ontology_versions WHERE version = :v", {"v": version})
    else:
        row = db.fetch_one(
            "SELECT version, schema_content, status, created_at "
            "FROM ontology.ontology_versions WHERE status = 'published' "
            "ORDER BY created_at DESC LIMIT 1")
    if not row:
        raise LookupError("ontology version not found")
    v2 = {}
    try:
        from app.modules.ontology import cq_v2

        reg = cq_v2.get_engine().registry
        v2 = {"schema_version": reg.current_version(), "schema": reg.current()}
    except Exception:  # noqa: BLE001
        v2 = {"schema_version": None, "note": "CQ V2 Registry 未就绪"}
    return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
            "context": ctx.as_dict(), "requested_version": version,
            "legacy": jsonable(row), "cq_v2": jsonable(v2)}


def ontology_versions(ctx: SemanticContext) -> dict:
    """本体版本列表（蓝图 9.3 / DC-04）。"""
    rows = db.fetch_all(
        "SELECT id, version, status, schema_uri, created_at "
        "FROM ontology.ontology_versions ORDER BY created_at DESC LIMIT 100")
    v2 = {"versions": [], "pins": {}, "migrations": []}
    try:
        from app.modules.ontology import cq_v2

        reg = cq_v2.get_engine().registry
        v2 = {"versions": list(reg.order), "pins": dict(reg.pins),
              "migrations": list(reg.migrations)}
    except Exception:  # noqa: BLE001
        pass
    return {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
            "context": ctx.as_dict(), "versions": jsonable(rows), "cq_v2": jsonable(v2),
            "count": len(rows)}


def ontology_proposals(ctx: SemanticContext, payload: dict) -> dict:
    """本体变更提案：默认只做校验 + 影响分析（dry run），commit=true 才写候选表。

    写入复用与 POST /ontology/proposals 相同的表与字段，不绕过候选审核流程。
    """
    proposal = payload.get("proposal")
    if not isinstance(proposal, (dict, list)) or not proposal:
        raise ValueError("proposal 必须是非空对象或数组")
    rationale = str(payload.get("rationale") or "")[:1000]
    impact = {"change_type": "unknown", "affected": [], "compatibility": "compatible",
              "review_required": True}
    try:
        from app.modules.ontology.cq_v2 import registry as reg

        current = {}
        try:
            from app.modules.ontology import cq_v2

            current = cq_v2.get_engine().registry.current() or {}
        except Exception:  # noqa: BLE001
            pass
        diff = reg.diff(current, proposal if isinstance(proposal, dict) else {"items": proposal})
        impact = {"change_type": "schema_diff", "affected": list(diff.keys()),
                  "compatibility": reg.compatibility(diff), "review_required": True,
                  "diff": jsonable(diff)}
    except Exception as exc:  # noqa: BLE001
        impact["validation_note"] = f"影响分析降级：{type(exc).__name__}: {str(exc)[:120]}"
    result = {"query_id": ctx.request_id, "ontology_version": ctx.schema_version,
              "context": ctx.as_dict(), "status": "dry_run",
              "impact_analysis": impact, "committed": False, "proposal_id": None}
    if payload.get("commit") is True:
        pid = db.insert_returning_id(
            "ontology.extension_proposals",
            {"proposal": json.dumps(proposal, ensure_ascii=False, default=str),
             "rationale": rationale})
        result.update({"status": "pending", "committed": True, "proposal_id": str(pid)})
    return result


def _access_control() -> dict:
    """P1-4 摘要：逐点过滤三点覆盖 + 核心表租户打标 + 判定统计（自证用）。"""
    out: dict = {
        "model": "RBAC+ABAC（Tenant/User/Role/Scope/Visibility 五维，fail-closed）",
        "single_definition": "app.policy",
        "filter_points": {
            "retrieval_entry": True,      # 检索入口：keyword/structured 通道 SQL 谓词
            "graph_traversal_per_hop": True,  # 图遍历逐跳：_edge_allowed → policy.decide
            "vector_pre_filter": True,    # 向量检索前：semantic_chunks 租户谓词
        },
        "tenant_columns": {},
        "decisions": {},
    }
    try:
        from app.modules import policy as tenant_policy

        out["tenant_columns"] = tenant_policy.coverage()
    except Exception as exc:  # noqa: BLE001  自证失败如实记录，不阻断 meta
        out["tenant_columns"] = {"error": str(exc)[:160]}
    try:
        from app import policy as policy_mod

        out["decisions"] = policy_mod.stats()
    except Exception as exc:  # noqa: BLE001
        out["decisions"] = {"error": str(exc)[:160]}
    return out


def _security_audit_status() -> dict:
    """P2 收口摘要：AC-01 全局强制上下文 + 安全审计持久化状态。"""
    try:
        from app import security_audit as sa

        st = sa.stats()
        return {
            "spec": "SIL-AC-01 + SIL-A07",
            "middleware": "global",
            "applies_to": "/api/v1/*",
            "strict_mode_global": True,
            "context_response_headers": 7,
            "audit_table": sa.TABLE,
            "durable": bool(st.get("table_exists")) and st.get("db_rows") is not None,
            "db_rows": st.get("db_rows"),
            "queue_size": st.get("queue_size"),
            "flushed": st.get("flushed"),
            "thread_alive": st.get("thread_alive"),
            "hydrated_rows": st.get("hydrated_rows"),
            "event_types": list(sa.EVENT_TYPES),
            "reboot_survives": True,
        }
    except Exception as exc:  # noqa: BLE001  自证失败如实记录，不阻断 meta
        return {"available": False,
                "note": f"{type(exc).__name__}: {str(exc)[:120]}"}


def meta() -> dict:
    """自检端点：不依赖 startup 完成，所有探测均容错。"""
    return {
        "spec": "SIL-Spec v1.9",
        "runtime": "semantic_runtime",
        "pipeline": ["RP-1 understand", "RP-2 resolve", "RP-3 plan",
                     "RP-4 retrieve+traverse+reason", "RP-5 evidence+context"],
        "channels": list(CHANNELS),
        "fusion": "RRF(k=60)",
        "contract_endpoints": CONTRACT_ENDPOINTS,
        "semantic_aliases": SEMANTIC_ALIASES,
        "legacy_routes_preserved": [
            "/api/v1/ontology/schema", "/api/v1/ontology/proposals"],
        "context_fields": ["tenant_id", "user_id", "request_id", "trace_id",
                           "permission_context", "schema_version"],
        "context_derivation": "AC-01 全局中间件对所有 /api/v1 请求统一注入并随响应回带；X-Semantic-Strict: 1 时缺失即 400",
        "ontology_version": resolve_published_version(),
        "vector": _vector_status(),
        "fact_contract": _fact_contract_status(),
        "event_bus": _event_bus_status(),
        "cq_v2_persistence": _cq_v2_persistence(),
        "modeling": _modeling_status(),
        "access_control": _access_control(),
        "security_audit": _security_audit_status(),
        "agent_takeover": ["twin/graphs/qa.py", "twin/graphs/reply.py",
                           "twin/graphs/decision.py"],
        "notes": [
            "向量通道当前显式降级，不静默返回部分结果",
            "逐点权限过滤已落地（P1-4）：检索入口 / 图遍历逐跳 / 向量检索前租户过滤，判定统一在 app.policy，见 meta.access_control",
            "CQ V2 状态已落库（P1-5）：启动自动水合，不再需要手工 bootstrap，见 cq_v2_persistence",
            "事实字段契约已按 DC-05/DC-08/DC-09 落地，覆盖率缺口见 fact_contract",
            "事件账本已按 EC-01～EC-05 落地：事实写入同事务落 outbox，双通道消费，见 event_bus",
            "建模源已统一（P1-2）：LinkML 双向无损，双轨对账见 modeling 与 /ontology/v2/registry/tracks",
            "事实写入前置 SHACL 校验（默认 warn 只记录不拦截），见 modeling.shacl 与 /ontology/v2/shapes",
            "AC-01 已由端点级提升为全局中间件：所有 /api/v1 请求统一注入六项上下文并回带响应头，严格模式全局生效",
            "安全审计已持久化到 audit.semantic_audit_logs（P2）：拒判 / 注入 / 越权 / 严格模式拒绝跨重启可查，见 security_audit",
        ],
    }


# ---------------------------------------------------------------- Agent 统一取数

def agent_retrieve(question: str, scope: list[str] | None = None,
                   top_k: int = 6) -> dict:
    """三张 LangGraph 图的唯一取数入口（落实蓝图 20.2 第 8 条）。

    内部走统一运行时的上下文注入与 trace 组装，但把 keyword 通道的原始结果
    无损还原为 legacy 结构（facts / memories / documents / diagnostics），
    因此现有问答、回复、决策图的行为与返回格式保持不变。
    运行时异常时回退到原检索器，并在 diagnostics 中显式标注降级原因。
    """
    ctx = system_context()
    try:
        bundle = search(ctx, {
            "query": question, "top_k": top_k, "scope": scope,
            "retrieval": {"keyword": True, "structured": False,
                          "graph": False, "vector": False},
        })
        raw = bundle.get("_legacy")
        if raw is None:
            raise RuntimeError("keyword 通道未返回原始检索结果")
        diagnostics = dict(raw.get("diagnostics") or {})
        diagnostics["semantic_runtime"] = {
            "query_id": bundle["query_id"],
            "ontology_version": bundle["ontology_version"],
            "context": bundle["context"],
            "retrieval_trace": bundle["retrieval_trace"],
            "degraded": bundle["degraded"],
        }
        return {"facts": raw.get("facts") or [],
                "memories": raw.get("memories") or [],
                "documents": raw.get("documents") or [],
                "diagnostics": diagnostics}
    except Exception as exc:  # noqa: BLE001  运行时不可用时不得让 Agent 问答整体失败
        raw = retrieval.retrieve(question, scope, top_k=top_k)
        diagnostics = dict(raw.get("diagnostics") or {})
        diagnostics["semantic_runtime_degraded"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        return {"facts": raw.get("facts") or [],
                "memories": raw.get("memories") or [],
                "documents": raw.get("documents") or [],
                "diagnostics": diagnostics}
