"""链路详情：把一次请求经过的完整链路快照落库，供页面逐层回放。

与 trace.py（轻量节点状态）不同，本模块记录的是「证据级」完整明细：
哪些数据被扫描、哪些词项与实体参与查询理解、命中哪些本体维度与关系、
召回哪些事实/记忆/文档（含信任级、置信度、生效区间）、生成最终用了哪些证据。

四层 stages 结构（页面按此渲染）：
  data       数据层 · 输入与查询理解（输入、检索范围、词项、本体 mention）
  ontology   本体层 · 实体与维度（命中的实体、维度、关系链）
  knowledge  知识层 · 召回明细（事实 / 记忆 / 文档逐条）
  generation 生成层 · 证据与产出（进入提示词的证据块、答案、引用、充分性）
"""
from __future__ import annotations

import json

from app.db import session as db

_KINDS = ("qa", "reply", "decision")

_DDL = """
CREATE TABLE IF NOT EXISTS workflow.pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind VARCHAR(20) NOT NULL,
    title VARCHAR(300),
    question TEXT,
    status VARCHAR(30),
    nodes JSONB DEFAULT '[]'::jsonb,
    stages JSONB DEFAULT '[]'::jsonb,
    duration_ms INT,
    created_at TIMESTAMPTZ DEFAULT now()
)
"""


def ensure_schema() -> None:
    db.execute(_DDL)


def record_run(kind: str, title: str, question: str, status: str,
               nodes: list, stages: list, duration_ms: int | None) -> str | None:
    """落库一次链路快照；失败不影响主流程。"""
    if kind not in _KINDS:
        kind = "qa"
    try:
        return str(db.insert_returning_id(
            "workflow.pipeline_runs",
            {
                "kind": kind,
                "title": str(title or "")[:300],
                "question": str(question or ""),
                "status": str(status or "")[:30],
                "nodes": json.dumps(nodes or [], ensure_ascii=False, default=str),
                "stages": json.dumps(stages or [], ensure_ascii=False, default=str),
                "duration_ms": duration_ms,
            },
        ))
    except Exception:  # noqa: BLE001  追踪是增强能力，绝不阻塞主链路
        return None


def list_runs(kind: str | None = None, limit: int = 50) -> list[dict]:
    where, binds = "", {}
    if kind in _KINDS:
        where = "WHERE kind = :kind"
        binds["kind"] = kind
    binds["limit"] = max(1, min(int(limit or 50), 200))
    return db.fetch_all(
        "SELECT id, kind, title, question, status, nodes, duration_ms, created_at "
        f"FROM workflow.pipeline_runs {where} "
        "ORDER BY created_at DESC LIMIT :limit", binds
    )


def get_run(run_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT * FROM workflow.pipeline_runs WHERE id = CAST(:id AS uuid)",
        {"id": str(run_id)},
    )


# ---------------------------------------------------------------- 本体明细查询

def entity_relations(entity_ids: list[str]) -> list[dict]:
    """给定实体 id，查它们参与的关系链（作为主体或客体），供本体层展示。"""
    ids = [i for i in (entity_ids or []) if i]
    if not ids:
        return []
    try:
        return db.fetch_all(
            "SELECT r.relation_type, r.status, r.valid_from, "
            "       s.canonical_name AS source_name, "
            "       t.canonical_name AS target_name, t.entity_type AS target_type "
            "FROM ontology.relations r "
            "JOIN ontology.entities s ON s.id = r.source_entity_id "
            "JOIN ontology.entities t ON t.id = r.target_entity_id "
            "WHERE r.source_entity_id = ANY(CAST(:ids AS uuid[])) "
            "   OR r.target_entity_id = ANY(CAST(:ids AS uuid[])) "
            "ORDER BY r.status, r.relation_type LIMIT 50",
            {"ids": ids},
        )
    except Exception:  # noqa: BLE001  关系表未就绪时不影响链路
        return []


# ---------------------------------------------------------------- stages 构建器

def _fact_row(f: dict) -> dict:
    """知识层事实行：主体/谓词/值 + 信任元数据（全部来自本体与 facts 表）。"""
    return {
        "subject": f.get("subject_name") or f.get("subject") or "（无主体）",
        "predicate": f.get("predicate") or "-",
        "value": f.get("object_value") if f.get("object_value") is not None
                 else f.get("value"),
        "trust_tier": f.get("trust_tier"),
        "confidence": f.get("confidence"),
        "valid_from": str(f.get("valid_from") or "")[:19],
        "valid_to": str(f.get("valid_to") or "")[:19] or None,
        "relevance": round(float(f.get("relevance") or 0), 2) if f.get("relevance") is not None else None,
    }


def _mention_entities(mentions: list) -> list[dict]:
    """把 detect_mentions 的结果补全成实体明细（类型 / 别名）。"""
    out = []
    for m in mentions or []:
        if isinstance(m, dict):
            out.append({
                "name": m.get("term") or m.get("name"),
                "entity_id": str(m.get("entity_id") or "") or None,
                "entity_type": m.get("entity_type"),
                "aliases": m.get("aliases") or [],
            })
        else:
            out.append({"name": str(m), "entity_id": None,
                        "entity_type": None, "aliases": []})
    return out


def stages_from_qa(question: str, state: dict, fr: dict) -> list[dict]:
    """问答链路四层快照。state 为图最终 state，fr 为 final_response。"""
    retrieval_report = fr.get("retrieval") or {}
    diagnostics = state.get("retrieval_diagnostics") or {}
    facts = state.get("retrieved_facts") or []
    memories = state.get("retrieved_memories") or []
    documents = state.get("retrieved_documents") or []
    mentions = retrieval_report.get("mention_entities") \
        or _mention_entities(retrieval_report.get("mentions"))
    entity_ids = [m["entity_id"] for m in mentions if m.get("entity_id")]
    entity_ids += [str(f.get("subject_id")) for f in facts if f.get("subject_id")]
    # 去重保序
    seen: set[str] = set()
    entity_ids = [x for x in entity_ids if x and not (x in seen or seen.add(x))]

    dims: dict[str, int] = {}
    for f in facts:
        p = f.get("predicate") or "(未命名维度)"
        dims[p] = dims.get(p, 0) + 1

    return [
        {
            "stage": "data", "title": "数据层 · 输入与查询理解",
            "fields": {
                "输入问题": question,
                "任务类型": state.get("task_type") or "qa",
                "检索范围": "、".join(state.get("retrieval_scope") or []),
                "检索词项": (diagnostics.get("terms")
                             or retrieval_report.get("terms") or []),
                "向量重排": "启用" if diagnostics.get("vector_used") else "未启用（纯词法）",
            },
            "mentions": mentions,
        },
        {
            "stage": "ontology", "title": "本体层 · 实体与维度",
            "entities": [
                {"name": m.get("name"), "entity_type": m.get("entity_type"),
                 "aliases": m.get("aliases") or []}
                for m in mentions if m.get("name")
            ],
            "dimensions": [{"predicate": k, "count": v} for k, v in dims.items()],
            "relations": entity_relations(entity_ids),
        },
        {
            "stage": "knowledge", "title": "知识层 · 召回明细",
            "facts": [_fact_row(f) for f in facts],
            "memories": [{
                "type": m.get("memory_type"), "content": str(m.get("content") or "")[:300],
                "confidence": m.get("confidence"),
                "relevance": round(float(m.get("relevance") or 0), 2) if m.get("relevance") is not None else None,
            } for m in memories],
            "documents": [{
                "title": d.get("title") or "(未命名文档)",
                "content": str(d.get("content") or "")[:300],
                "score": d.get("score"), "via": d.get("via"),
            } for d in documents],
        },
        {
            "stage": "generation", "title": "生成层 · 证据与产出",
            "evidence_count": fr.get("evidence_count") if "evidence_count" in fr
                              else state.get("evidence_count"),
            "sufficiency": fr.get("evidence_sufficiency")
                           or state.get("evidence_sufficiency"),
            "citations": fr.get("citations") or [],
            "answer": str(fr.get("answer") or "")[:2000],
            "degraded": bool(fr.get("degraded")),
        },
    ]


def stages_from_reply(conv: dict, draft: dict, state: dict) -> list[dict]:
    """回复起草链路四层快照（conv 为会话信息，draft 为草稿生成结果）。"""
    facts = state.get("retrieved_facts") or []
    documents = state.get("retrieved_documents") or []
    memories = state.get("retrieved_memories") or []
    diagnostics = state.get("retrieval_diagnostics") or {}

    mention_names: list[str] = []
    for f in facts:
        n = f.get("subject_name")
        if n and n not in mention_names:
            mention_names.append(n)
    dims: dict[str, int] = {}
    for f in facts:
        p = f.get("predicate") or "(未命名维度)"
        dims[p] = dims.get(p, 0) + 1

    return [
        {
            "stage": "data", "title": "数据层 · 输入与查询理解",
            "fields": {
                "会话": conv.get("conversation_name"),
                "发送人": conv.get("sender"),
                "消息时间": str(conv.get("incoming_time") or ""),
                "待回复消息": str(conv.get("incoming_text") or "")[:400],
                "上下文条数": len(conv.get("history") or []),
                "触发原因": conv.get("trigger_reason"),
            },
            "mentions": _mention_entities([]),
        },
        {
            "stage": "ontology", "title": "本体层 · 实体与维度",
            "entities": [{"name": n} for n in mention_names],
            "dimensions": [{"predicate": k, "count": v} for k, v in dims.items()],
            "relations": [],
        },
        {
            "stage": "knowledge", "title": "知识层 · 召回明细",
            "facts": [_fact_row(f) for f in facts],
            "memories": [{
                "type": m.get("memory_type"), "content": str(m.get("content") or "")[:300],
                "confidence": m.get("confidence"),
            } for m in memories],
            "documents": [{
                "title": d.get("title") or "(未命名文档)",
                "content": str(d.get("content") or "")[:300],
                "score": d.get("score"), "via": d.get("via"),
            } for d in documents],
        },
        {
            "stage": "generation", "title": "生成层 · 草稿与承诺检查",
            "sufficiency": state.get("evidence_sufficiency"),
            "draft": str(draft.get("draft_text") or "")[:2000],
            "unconfirmed_points": draft.get("unconfirmed_points") or [],
            "requires_human_review": True,
        },
    ]


def stages_from_decision(task_type: str, context: dict,
                         result: dict, evidence: list) -> list[dict]:
    """决策链路四层快照。"""
    facts = result.get("retrieved_facts") or []
    documents = result.get("retrieved_documents") or []
    memories = result.get("retrieved_memories") or []
    rec = result.get("recommendation") or {}
    pol = result.get("policy_result") or {}

    mention_names: list[str] = []
    for f in facts:
        n = f.get("subject_name")
        if n and n not in mention_names:
            mention_names.append(n)
    dims: dict[str, int] = {}
    for f in facts:
        p = f.get("predicate") or "(未命名维度)"
        dims[p] = dims.get(p, 0) + 1

    return [
        {
            "stage": "data", "title": "数据层 · 输入与业务上下文",
            "fields": {
                "决策任务": task_type,
                "业务上下文": json.dumps(context or {}, ensure_ascii=False),
                "决策模式": context.get("decision_mode") or "human_confirm",
            },
            "mentions": _mention_entities([]),
        },
        {
            "stage": "ontology", "title": "本体层 · 实体与维度",
            "entities": [{"name": n} for n in mention_names],
            "dimensions": [{"predicate": k, "count": v} for k, v in dims.items()],
            "relations": [],
        },
        {
            "stage": "knowledge", "title": "知识层 · 证据召回",
            "facts": [_fact_row(f) for f in facts],
            "memories": [{
                "type": m.get("memory_type"), "content": str(m.get("content") or "")[:300],
                "confidence": m.get("confidence"),
            } for m in memories],
            "documents": [{
                "title": d.get("title") or "(未命名文档)",
                "content": str(d.get("content") or "")[:300],
                "score": d.get("score"), "via": d.get("via"),
            } for d in documents],
        },
        {
            "stage": "generation", "title": "生成层 · 建议与策略",
            "recommendation": rec.get("recommendation"),
            "confidence": rec.get("confidence"),
            "risk_level": rec.get("risk_level"),
            "reasoning": rec.get("reasoning") or [],
            "policy_result": pol.get("policy_result"),
            "policy_reason": pol.get("reason"),
            "evidence": evidence or [],
        },
    ]
