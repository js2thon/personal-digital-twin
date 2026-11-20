"""多路混合检索：结构化事实 + 个人记忆 + 文档切片（词法召回 + 可选向量重排）。

修复要点（此前是「假检索」）：
- 事实/记忆原来是 `ORDER BY recorded_at DESC LIMIT 6`，与问题完全无关；
- 语义分支依赖 pgvector，但本地库 embedding 列为 TEXT，降级又是
  `content ILIKE '%整句问题%'`，中文问题必然 0 命中；
- 结果导致证据充分性检查恒为 sufficient、拒答与承诺检查形同虚设。

现在三路都按问题词项相关性打分召回；无命中即返回空，让上层正确判定证据不足。
向量列 `embedding_vec` 为可选增强：无 pgvector、无 embedding 服务时词法检索独立工作。
"""
from __future__ import annotations

import json
import time

from app.config import settings
from app.db import session as db
from app.modules.llm.client import llm_client
from app.modules.retrieval import lexical

# 实体表 TTL 缓存（detect_mentions 用）
_ENT_CACHE: dict = {"ts": 0.0, "rows": None}

INTENT_RETRIEVERS = {
    "facts": ["structured_facts", "semantic"],
    "memories": ["memory", "semantic"],
    "documents": ["ragflow", "semantic"],
    "preferences": ["memory", "structured_facts"],
    "all": ["structured_facts", "memory", "semantic", "ragflow"],
}

_MIN_SCORE = 2.0  # 至少命中一个实义短语（2.6/1.8）或累计多个 2-gram；过滤巧合噪音
_vector_col_ready = False


def ensure_vector_column() -> bool:
    """本地库无 pgvector 时用 TEXT 列存 JSON 向量，幂等补列。

    返回 False 表示列不可用（此时检索 SQL 用 NULL 占位，纯词法检索照常工作）。
    """
    global _vector_col_ready
    if _vector_col_ready:
        return True
    try:
        db.execute(
            "ALTER TABLE knowledge.semantic_chunks "
            "ADD COLUMN IF NOT EXISTS embedding_vec TEXT"
        )
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='knowledge' AND table_name='semantic_chunks' "
            "AND column_name='embedding_vec'"
        )
        _vector_col_ready = bool(row)
    except Exception:  # noqa: BLE001  老库/权限不足时纯词法检索仍可用
        return False
    return bool(_vector_col_ready)


def _rows_with_score(sql: str, params: dict, terms: list[dict], column: str,
                     prefix: str, top_k: int) -> list[dict]:
    expr, binds = lexical.score_expr(terms, column, prefix)
    if not binds:
        return []
    query = (
        f"SELECT * FROM (SELECT base.*, {expr} AS relevance FROM ({sql}) base) scored "
        "WHERE relevance >= :min_score ORDER BY relevance DESC, recorded_at DESC "
        "LIMIT :k"
    )
    binds.update(params or {})
    binds.update({"min_score": _MIN_SCORE, "k": top_k})
    return db.fetch_all(query, binds)


_FACT_BOOST = 4.0   # 事实的值只有几个字，2-gram 权重需放大才够得着 _MIN_SCORE


def _facts(terms: list[dict], top_k: int,
           principal: dict | None = None) -> list[dict]:
    """结构化事实检索：值 / 主体名 / 谓词三路分别打分。

    主体名来自 `ontology.entities` 归一并 join 出来，命中它也计分——问「某某丙负责什么」
    时靠的就是实体名，而不是谓词或值。事实短（谓词英文 + 短值），长短语几乎匹配不上，
    所以权重放大 `_FACT_BOOST`，让一个实义 2 字词即可召回，再用 top_k 收敛噪音。
    """
    if not terms:
        return []
    parts: list[str] = []
    binds: dict = {}
    for i, t in enumerate(terms):
        key = f"fk{i}"
        binds[key] = "%" + str(t.get("term") or "").replace("%", "").replace("_", "") + "%"
        w = float(t.get("weight") or 0) * _FACT_BOOST
        parts.append(f"(CASE WHEN COALESCE(base.object_value::text,'') ILIKE :{key} "
                     f"THEN {w:.2f} ELSE 0.0 END)")
        parts.append(f"(CASE WHEN COALESCE(base.subject_name,'') ILIKE :{key} "
                     f"THEN {w * 2.0:.2f} ELSE 0.0 END)")
        parts.append(f"(CASE WHEN COALESCE(base.predicate,'') ILIKE :{key} "
                     f"THEN {w * 0.6:.2f} ELSE 0.0 END)")
    # G2：restricted / 未知可见性一律不进通用问答召回（fail-closed）。
    # P1-4：叠加租户 / 归属逐点过滤（principal 为 AC-01 上下文归一化主体）；
    # principal 为 None 时保持 owner 级白名单行为（内部调用不受影响）。
    params: dict = {}
    if principal is not None:
        from app.policy import read_clause

        vis_clause = " AND " + read_clause("f", principal, params)
    else:
        from app.visibility import retrievable_clause

        vis_clause = f" AND {retrievable_clause('f')}"

    sql = (
        "SELECT f.id AS fact_id, f.predicate, f.object_value, f.valid_from, f.valid_to, "
        "f.trust_tier, f.confidence, f.recorded_at, f.created_by, "
        "e.id AS subject_id, e.canonical_name AS subject_name, e.entity_type "
        "FROM knowledge.facts f LEFT JOIN ontology.entities e ON e.id = f.subject_id "
        f"WHERE f.status = 'active'{vis_clause}"
    )
    query = (
        "SELECT * FROM (SELECT base.*, " + " + ".join(parts) + " AS relevance "
        "FROM (" + sql + ") base) scored "
        "WHERE relevance >= :min_score ORDER BY relevance DESC, recorded_at DESC LIMIT :k"
    )
    binds.update({"min_score": _MIN_SCORE, "k": top_k, **params})
    return db.fetch_all(query, binds)


def _memories(terms: list[dict], top_k: int,
              principal: dict | None = None) -> list[dict]:
    if not terms:
        return []
    params: dict = {}
    tenant_clause = ""
    if principal is not None:
        params["p_tenant"] = str(principal.get("tenant_id"))
        tenant_clause = " AND COALESCE(m.tenant_id, 'default') = :p_tenant"
    sql = (
        "SELECT m.id, m.memory_type, m.content, m.confidence, m.created_at, "
        "m.created_at AS recorded_at FROM memory.memory_items m "
        "WHERE m.status IN ('candidate','confirmed')" + tenant_clause
    )
    return _rows_with_score(
        sql, params, terms, "COALESCE(base.content::text,'')", "mk", top_k
    )


def _tenant_id_of(principal: dict | None) -> str:
    """向量通道的租户键：principal 缺失时归 default，绝不省略过滤。"""
    if not principal:
        return "default"
    return str(principal.get("tenant_id") or "default")


def _lexical_document_rows(terms: list[dict], top_k: int,
                           principal: dict | None) -> list[dict]:
    """词法召回：score_expr >= _MIN_SCORE 的语义保持原样不变。"""
    if not terms:
        return []
    col = ("COALESCE(c.content,'') || ' ' || COALESCE(d.title,'')")
    expr, binds = lexical.score_expr(terms, col, "dk")
    doc_binds: dict = {}
    tenant_clause = ""
    if principal is not None:
        doc_binds["p_tenant"] = str(principal.get("tenant_id"))
        tenant_clause = " AND COALESCE(c.tenant_id, 'default') = :p_tenant"
    query = (
        "SELECT c.id, c.document_id, c.content, c.metadata, "
        "d.title, c.created_at AS recorded_at, " + expr + " AS relevance "
        "FROM knowledge.semantic_chunks c "
        "LEFT JOIN source.documents d ON d.id = c.document_id "
        "WHERE " + expr + " >= :min_score" + tenant_clause +
        " ORDER BY relevance DESC, c.created_at DESC LIMIT :k"
    )
    binds.update(doc_binds)
    binds.update({"min_score": _MIN_SCORE, "k": max(top_k * 5, 20)})
    return db.fetch_all(query, binds)


def _vector_document_rows(question: str, top_k: int, principal: dict | None,
                          diagnostics: dict, store) -> list[dict]:
    """向量独立召回：不经过词法门槛。网关异常时静默返回 []，纯词法零阻塞。"""
    try:
        qv = llm_client.embed([question], timeout=15)[0]
    except Exception as e:  # noqa: BLE001
        diagnostics["vector_error"] = f"{type(e).__name__}: {str(e)[:100]}"
        return []
    try:
        # tenant_id 强制传入（VectorStore.search 入口还会再校验一次）
        return store.search(qv, max(top_k * 5, 20),
                            tenant_id=_tenant_id_of(principal))
    except Exception as e:  # noqa: BLE001
        diagnostics["vector_error"] = f"{type(e).__name__}: {str(e)[:100]}"
        return []


def _rerank_candidates(question: str, items: list[dict], diagnostics: dict) -> list[dict]:
    """RRF 候选二次精排 + 无覆盖拦截。

    网关可用时：用 cross-encoder 相关性分重排并过滤低分候选，让"语料没覆盖"
    的问题返回空而不是硬塞 5 条无关文档。网关异常/未配置时静默退回 RRF 原序，
    并在 diagnostics 里如实标注，绝不因为精排失败而阻断主检索。
    """
    diagnostics.update({"rerank_used": False, "rerank_candidates": len(items),
                        "rerank_kept": None, "rerank_top_score": None})
    if not getattr(settings, "rerank_model", "") or not items:
        return items
    try:
        texts = [f"{str(it.get('title') or '')}\n{str(it.get('content') or '')}"[:1200]
                 for it in items]
        scored = llm_client.rerank(question, texts, timeout=8)
    except Exception as e:  # noqa: BLE001
        diagnostics["rerank_error"] = f"{type(e).__name__}: {str(e)[:100]}"
        return items
    if not scored:
        diagnostics["rerank_error"] = "gateway returned no results"
        return items
    min_score = float(getattr(settings, "rerank_min_score", 0.05) or 0.0)
    # 只按 top1 做全局拒答，不逐条过滤：实测相关文档最低 rerank=0.0545，
    # 而无覆盖 query 的最高分仅 0.0273；逐条设门槛会误杀分数偏低但正确的文档
    # （DOC-S05/S08），全局门槛则能把两者干净分开。
    if float(scored[0][1]) < min_score:
        diagnostics.update({
            "rerank_used": True, "rerank_rejected": True,
            "rerank_kept": 0, "rerank_top_score": round(float(scored[0][1]), 6),
            "rerank_min_score": min_score,
        })
        return []
    kept: list[dict] = []
    for idx, score in scored:
        if idx < 0 or idx >= len(items):
            continue
        item = dict(items[idx])
        item["rerank_score"] = round(score, 6)
        item["score"] = round(score, 4)  # 统一量纲：精排后 score 即相关性分
        item["via"] = f"{item.get('via') or 'lexical'}+rerank"
        kept.append(item)
    diagnostics.update({
        "rerank_used": True,
        "rerank_rejected": False,
        "rerank_kept": len(kept),
        "rerank_top_score": round(float(scored[0][1]), 6),
        "rerank_min_score": min_score,
    })
    return kept


def _documents(question: str, terms: list[dict], top_k: int,
               diagnostics: dict, principal: dict | None = None) -> list[dict]:
    """三路中的文档路：词法召回 + 向量独立召回 → RRF 融合 → 精排。

    关键修复（向量本体 P0）：不再"先词法过滤再向量重排"。
    词法 0 命中（terms 为空）时向量通道照样独立召回。
    无向量 / 网关异常时走纯词法分支，输出与改造前逐行一致。
    """
    from app.modules.embeddings.service import EMBEDDING_VERSION
    from app.modules.retrieval import vector_store as vs

    if not str(question or "").strip():
        return []
    lex_rows = _lexical_document_rows(terms, top_k, principal)
    store = vs.get_vector_store()
    backend = str(getattr(settings, "vector_backend", "auto") or "auto").lower()
    diagnostics["vector_backend"] = store.backend
    if backend == "pgvector" and not vs.extension_available():
        diagnostics["vector_backend_fallback"] = "pgvector 未就绪，已降级 text"
    vec_rows = _vector_document_rows(question, top_k, principal, diagnostics, store)

    has_vec = bool(vec_rows)
    diagnostics.update({
        "vector_used": has_vec,  # 兼容旧字段：现表示"向量参与召回"
        "vector_recall_used": has_vec,
        "vector_candidates": len(vec_rows),
        "vector_top_sim": round(float(vec_rows[0]["score"]), 6) if vec_rows else None,
        "embedding_model": settings.embedding_model if has_vec else None,
        "embedding_version": EMBEDDING_VERSION if has_vec else None,
        "fusion": "rrf" if has_vec else "lexical_only",
        "rrf_k": 60,
    })

    if not has_vec:
        # 逐行兼容：顺序 / score / via 与改造前一致
        return [{
            "id": str(r["id"]), "document_id": str(r["document_id"]),
            "content": r["content"], "title": r.get("title") or "",
            "score": round(float(r.get("relevance") or 0), 2),
            "via": "lexical",
        } for r in lex_rows[:top_k]]

    lex_items = [{
        "id": str(r["id"]), "ref_id": str(r["id"]),
        "document_id": str(r["document_id"]),
        "content": r["content"], "title": r.get("title") or "",
        "score": float(r.get("relevance") or 0),
        "via": "lexical",
    } for r in lex_rows]
    vec_items = [{
        "id": str(r["id"]), "ref_id": str(r["id"]),
        "document_id": r.get("document_id") or "",
        "content": r.get("content") or "", "title": r.get("title") or "",
        "score": float(r.get("score") or 0),
        "via": "vector",
        "similarity": r.get("similarity"),
        "entity_ids": r.get("entity_ids"),
        "entity_types": r.get("entity_types"),
        "ontology_version": r.get("ontology_version"),
    } for r in vec_rows]
    cand_n = max(int(getattr(settings, "rerank_candidates", 20) or 20), top_k)
    fused_all = vs.rrf_fuse({"lexical": lex_items, "vector": vec_items}, cand_n)
    fused = _rerank_candidates(question, fused_all, diagnostics)

    out = []
    for f in fused[:top_k]:
        channels = f.get("fusion_channels") or []
        via = ("fused(lexical+vector)" if len(channels) > 1
               else ("vector" if "vector" in channels else "lexical"))
        out.append({
            "id": f["id"], "document_id": f["document_id"],
            "content": f["content"], "title": f["title"],
            "score": round(float(f["score"]), 2),
            "via": via,
            "rrf_score": f["rrf_score"],
            "rerank_score": f.get("rerank_score"),
            "reranked": f.get("rerank_score") is not None,
            "fusion_channels": channels,
            "similarity": f.get("similarity"),
            "entity_ids": f.get("entity_ids"),
            "entity_types": f.get("entity_types"),
            "ontology_version": f.get("ontology_version"),
        })
    return out


def detect_mentions(question: str) -> list[dict]:
    """识别问题里出现的已知实体（含别名），作为高权重检索词。

    这是本体层参与「查询理解」的入口，不只是给事实贴标签。没有它，「某某丙负责什么」
    里人名只有 2-gram 权重 0.55，会被「事情」这类常见词的 2.6 压过去，
    结果召回一堆别人的事实——人名的分量必须由本体来给。
    """
    q = str(question or "")
    if not q:
        return []
    try:
        # 实体表 30 秒 TTL 缓存：实体更新频率低，每次问答全量拉 500 行不划算
        global _ENT_CACHE
        now = time.monotonic()
        if _ENT_CACHE["rows"] is None or now - _ENT_CACHE["ts"] > 30.0:
            _ENT_CACHE["rows"] = db.fetch_all(
                "SELECT id, canonical_name, entity_type, aliases FROM ontology.entities "
                "WHERE status = 'active' LIMIT 500"
            )
            _ENT_CACHE["ts"] = now
        rows = _ENT_CACHE["rows"]
    except Exception:  # noqa: BLE001  本体表未就绪时退化为纯词法检索
        return []
    hits: list[dict] = []
    for r in rows:
        names = [r.get("canonical_name")] + list(r.get("aliases") or [])
        for n in names:
            n = str(n or "").strip()
            if len(n) >= 2 and n in q:
                hits.append({"term": n, "weight": 3.0, "mention": True,
                             "entity_id": str(r.get("id")),
                             "entity_type": r.get("entity_type"),
                             "canonical_name": r.get("canonical_name"),
                             "aliases": list(r.get("aliases") or [])})
                break
    return hits


def retrieve(question: str, scope: list[str] | None = None, top_k: int = 6,
             principal: dict | None = None) -> dict:
    """三通道混合检索。

    P1-4：principal 传入时逐点过滤（事实：租户+可见性+归属；文档：向量检索前
    租户过滤）；None 保持 owner 级行为（进程内图/管线调用不受影响）。
    """
    scope = scope or ["facts", "memories", "documents"]
    enabled = set()
    for s in scope:
        enabled.update(INTENT_RETRIEVERS.get(s, ["semantic"]))

    terms = lexical.extract_terms(question)
    mentions = detect_mentions(question)
    if mentions:
        # 实体名置顶：同一 mention 不再参与 2-gram 重复计分
        seen = {m["term"] for m in mentions}
        terms = mentions + [t for t in terms if t["term"] not in seen]
    diagnostics = {
        "terms": [t["term"] for t in terms],
        "mentions": [m["term"] for m in mentions],
        "mention_entities": mentions,   # 完整实体明细，供链路详情回放
        "vector_used": False,
        "vector_recall_used": False,
        "vector_candidates": 0,
        "vector_top_sim": None,
        "embedding_model": None,
        "embedding_version": None,
        "fusion": "lexical_only",
        "rrf_k": 60,
        "vector_backend": "text",
        "rerank_used": False,
        "rerank_candidates": 0,
        "rerank_kept": None,
        "rerank_top_score": None,
    }
    result: dict[str, list[dict]] = {"facts": [], "memories": [], "documents": []}

    if "structured_facts" in enabled:
        result["facts"] = _facts(terms, top_k, principal)
    if "memory" in enabled:
        result["memories"] = _memories(terms, top_k, principal)
    if "semantic" in enabled:
        ensure_vector_column()
        from app.modules.retrieval import vector_store as vs
        vs.ensure_columns()
        result["documents"] = _documents(question, terms, top_k, diagnostics,
                                         principal)
    if "ragflow" in enabled and settings.ragflow_base_url:
        result["documents"].extend(_ragflow_search(question, top_k))

    diagnostics["hits"] = {k: len(v) for k, v in result.items()}
    result["diagnostics"] = diagnostics
    return result


def _ragflow_search(question: str, top_k: int) -> list[dict]:
    """RAGFlow Adapter：仅通过官方 HTTP API 交互，绝不直接操作其内部数据库。"""
    import httpx

    try:
        resp = httpx.post(
            f"{settings.ragflow_base_url}/api/v1/retrieval",
            headers={"Authorization": f"Bearer {settings.ragflow_api_key}"},
            json={"question": question, "top_k": top_k},
            timeout=30,
        )
        resp.raise_for_status()
        chunks = resp.json().get("data", {}).get("chunks", [])
        return [
            {"id": c.get("id"), "content": c.get("content", ""),
             "title": "", "via": "ragflow", "score": 0}
            for c in chunks
        ]
    except Exception:  # noqa: BLE001
        return []
