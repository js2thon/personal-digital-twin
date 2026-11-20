"""向量存储抽象（向量本体 P0：零依赖 TEXT-JSON + 纯 Python 余弦）。

一处实现、两处调用：
  - retrieval.service._documents()       —— 向量作为独立召回通道
  - semantic_runtime.service._vector_channel() —— 原暴力 cosine 通道收敛到此

关键约束（不可协商）：
  1. search() 强制 tenant_id，调用方无法省略（GV-01 租户过滤）；
  2. 无向量 / 网关异常时返回空列表，调用方纯词法零阻塞；
  3. 本体坐标（entity_ids / entity_types / ontology_version）作为一等过滤条件，
     让"向量"能按"本体"约束——这是消除"两张皮"的存储侧基础；
  4. 本模块只管存储与相似度，不调用 embedding 服务（查询向量由调用方生成）。
"""
from __future__ import annotations

import json
from typing import Protocol, TypedDict

from app.config import settings
from app.db import session as db
from app.modules.retrieval import lexical

_RRF_K = 60


class VectorItem(TypedDict):
    ref_id: str
    kind: str
    text: str
    vec: list[float]
    model: str
    version: str
    tenant_id: str


class VectorStore(Protocol):
    def upsert(self, items: list[VectorItem]) -> int: ...

    def search(self, vec: list[float], top_k: int, *, tenant_id: str,
               kinds: list[str] | None = None,
               filters: dict | None = None) -> list[dict]: ...

    def delete(self, ref_ids: list[str]) -> int: ...

    def stats(self) -> dict: ...


# ---------------------------------------------------------------- 幂等 DDL

_COLUMN_DDL = (
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS embedding_model varchar(64)",
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS embedding_version varchar(32)",
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS embedded_at timestamptz",
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS entity_ids jsonb NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS entity_types text[] NOT NULL DEFAULT '{}'::text[]",
    "ALTER TABLE knowledge.semantic_chunks ADD COLUMN IF NOT EXISTS ontology_version varchar(32)",
)

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_entity_types "
    "ON knowledge.semantic_chunks USING gin (entity_types)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_state "
    "ON knowledge.semantic_chunks (embedding_model, embedding_version) "
    "WHERE embedding_vec IS NULL",
)

_VECTOR_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS ontology.vector_index (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          varchar(64)  NOT NULL DEFAULT 'default',
    kind               varchar(32)  NOT NULL,
    ref_id             text         NOT NULL,
    text_hash          varchar(64)  NOT NULL,
    text_preview       text,
    dim                int          NOT NULL,
    embedding_model    varchar(64)  NOT NULL,
    embedding_version  varchar(32)  NOT NULL,
    vec                text,
    status             varchar(16)  NOT NULL DEFAULT 'ready',
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, kind, ref_id, embedding_model, embedding_version)
)
"""

_VECTOR_INDEX_IDX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_vec_kind "
    "ON ontology.vector_index (tenant_id, kind, status)",
)

_columns_ready = False


def ensure_columns() -> bool:
    """幂等补齐 6 个本体/向量元数据列 + 2 索引 + 本体向量表。

    独立 ALTER（CREATE TABLE IF NOT EXISTS 不会给存量表加列），
    每组单独容错：一项失败不影响其余，也不影响检索主流程。
    """
    global _columns_ready
    if _columns_ready:
        return True
    for stmt in (*_COLUMN_DDL, *_INDEX_DDL, _VECTOR_INDEX_DDL, *_VECTOR_INDEX_IDX_DDL):
        try:
            db.execute(stmt)
        except Exception:  # noqa: BLE001  老库/权限不足时降级为无本体坐标
            continue
    try:
        row = db.fetch_one(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='knowledge' AND table_name='semantic_chunks' "
            "AND column_name='entity_types'"
        )
        _columns_ready = bool(row)
    except Exception:  # noqa: BLE001
        _columns_ready = False
    return bool(_columns_ready)


def extension_available() -> bool:
    """探测 pgvector 扩展是否存在（P1 用；P0 不依赖）。"""
    try:
        row = db.fetch_one("SELECT count(*) AS n FROM pg_extension WHERE extname = 'vector'")
        return bool(row and int(row.get("n") or 0) > 0)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 融合

def rrf_fuse(buckets: dict[str, list[dict]], top_k: int, *, k: int = _RRF_K) -> list[dict]:
    """RRF 倒数排名融合：跨通道不比较原始分数，只按名次累加 1/(k+rank)。

    每条 item 用 ref_id（或 id）去重；score 保留原通道分值供展示，
    排序依据 rrf_score。channels 记录该证据被哪些通道召回。
    """
    merged: dict[str, dict] = {}
    for channel, items in buckets.items():
        if not items:
            continue
        ordered = sorted(
            items,
            key=lambda x: float(x.get("score") if x.get("score") is not None
                                else (x.get("relevance") or 0)),
            reverse=True,
        )
        for rank, item in enumerate(ordered, 1):
            key = str(item.get("ref_id") or item.get("id") or "")
            if not key:
                continue
            rec = merged.setdefault(key, {"item": item, "score": 0.0, "channels": []})
            rec["score"] += 1.0 / (k + rank)
            if channel not in rec["channels"]:
                rec["channels"].append(channel)
    ranked = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]
    out: list[dict] = []
    for rec in ranked:
        item = dict(rec["item"])
        item["rrf_score"] = round(rec["score"], 6)
        item["fusion_channels"] = rec["channels"]
        out.append(item)
    return out


# ---------------------------------------------------------------- P0 实现

class TextColumnCosineStore:
    """零依赖实现：读 embedding_vec（JSON 数组）→ 纯 Python cosine 排序。

    当前 216 切片的规模下，全表线性扫描毫秒级；不设 LIMIT 100，
    所有有向量的切片都参与召回。
    """

    backend = "text"

    def search(self, vec: list[float], top_k: int, *, tenant_id: str,
               kinds: list[str] | None = None,
               filters: dict | None = None) -> list[dict]:
        # GV-01：租户过滤在入口强制，调用方无法省略
        if not tenant_id:
            raise ValueError("VectorStore.search() 必须提供 tenant_id")
        ensure_columns()  # 惰性兜底：单测/未跑 bootstrap 时也能用
        if not vec:
            return []
        where = [
            "c.embedding_vec IS NOT NULL", "c.embedding_vec <> ''",
            "COALESCE(c.tenant_id, 'default') = :p_tenant",
        ]
        params: dict = {"p_tenant": str(tenant_id)}
        filters = filters or {}
        if filters.get("entity_types"):
            where.append("c.entity_types && CAST(:p_types AS text[])")
            params["p_types"] = list(filters["entity_types"])
        if filters.get("ontology_version"):
            where.append("c.ontology_version = :p_ov")
            params["p_ov"] = str(filters["ontology_version"])
        sql = (
            "SELECT c.id, c.document_id, c.content, c.metadata, c.embedding_vec, "
            "c.embedding_model, c.embedding_version, c.entity_ids, c.entity_types, "
            "c.ontology_version, d.title "
            "FROM knowledge.semantic_chunks c "
            "LEFT JOIN source.documents d ON d.id = c.document_id "
            "WHERE " + " AND ".join(where)
        )
        try:
            rows = db.fetch_all(sql, params)
        except Exception:  # noqa: BLE001  无向量列/库不可用时静默降级
            return []
        scored: list[tuple[float, dict]] = []
        for r in rows:
            try:
                raw = r.get("embedding_vec")
                cand = json.loads(raw) if isinstance(raw, str) else raw
                if not isinstance(cand, list) or not cand:
                    continue
                sim = float(lexical.cosine(vec, cand))
                if sim <= 0:
                    continue
                scored.append((sim, r))
            except (ValueError, TypeError, IndexError):
                continue
        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[dict] = []
        for sim, r in scored[:max(int(top_k), 1)]:
            out.append({
                "ref_id": str(r["id"]),
                "kind": "document",
                "id": str(r["id"]),
                "document_id": str(r.get("document_id") or ""),
                "title": r.get("title") or "",
                "content": r.get("content") or "",
                "score": round(sim, 6),
                "similarity": round(sim, 6),
                "via": "vector",
                "embedding_model": r.get("embedding_model"),
                "embedding_version": r.get("embedding_version"),
                "entity_ids": r.get("entity_ids") or [],
                "entity_types": list(r.get("entity_types") or []),
                "ontology_version": r.get("ontology_version"),
            })
        return out

    def upsert(self, items: list[VectorItem]) -> int:
        """本体对象向量入口（P2 填充 ontology.vector_index，P0 预留）。

        向量是派生数据：写入失败不影响权威本体对象，调用方自行决定重试。
        """
        if not items:
            return 0
        ensure_columns()
        import hashlib
        rows = []
        for it in items:
            rows.append({
                "tenant": str(it.get("tenant_id") or "default"),
                "kind": str(it.get("kind") or ""),
                "ref": str(it.get("ref_id") or ""),
                "hash": hashlib.sha256((it.get("text") or "").encode("utf-8")).hexdigest(),
                "preview": (it.get("text") or "")[:200],
                "dim": len(it.get("vec") or []),
                "model": str(it.get("model") or ""),
                "version": str(it.get("version") or ""),
                "vec": json.dumps(it.get("vec") or []),
            })
        try:
            db.executemany(
                "INSERT INTO ontology.vector_index "
                "(tenant_id, kind, ref_id, text_hash, text_preview, dim, "
                " embedding_model, embedding_version, vec) "
                "VALUES (:tenant, :kind, :ref, :hash, :preview, :dim, "
                ":model, :version, CAST(:vec AS text)) "
                "ON CONFLICT (tenant_id, kind, ref_id, embedding_model, embedding_version) "
                "DO UPDATE SET text_hash=EXCLUDED.text_hash, "
                "text_preview=EXCLUDED.text_preview, dim=EXCLUDED.dim, "
                "vec=EXCLUDED.vec, status='ready', updated_at=now()",
                rows,
            )
            return len(rows)
        except Exception:  # noqa: BLE001
            return 0

    def delete(self, ref_ids: list[str]) -> int:
        if not ref_ids:
            return 0
        try:
            with db.transaction():
                for ref in ref_ids:
                    db.execute("DELETE FROM ontology.vector_index WHERE ref_id = :r", {"r": str(ref)})
            return len(ref_ids)
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict:
        """覆盖率 / 模型分布 / stale 计数，供自证端点只读输出。"""
        ensure_columns()
        info: dict = {
            "backend": self.backend,
            "extension_available": extension_available(),
            "chunks": {"total": 0, "embedded": 0, "coverage": 0.0, "stale": 0},
            "model": {"name": None, "version": None},
            "vector_recall": {"available": False},
        }
        try:
            row = db.fetch_one(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE embedding_vec IS NOT NULL AND embedding_vec <> '') AS embedded, "
                "count(*) FILTER (WHERE embedding_vec IS NOT NULL AND embedding_vec <> '' "
                "  AND (embedding_model IS NULL OR embedding_model = '')) AS stale, "
                "max(embedding_model) FILTER (WHERE embedding_model IS NOT NULL) AS model "
                "FROM knowledge.semantic_chunks"
            )
            if row:
                total = int(row.get("total") or 0)
                embedded = int(row.get("embedded") or 0)
                info["chunks"] = {
                    "total": total,
                    "embedded": embedded,
                    "coverage": round(embedded / total, 4) if total else 0.0,
                    "stale": int(row.get("stale") or 0),
                }
                if row.get("model"):
                    info["model"]["name"] = str(row["model"])
            vrow = db.fetch_one(
                "SELECT embedding_version FROM knowledge.semantic_chunks "
                "WHERE embedding_version IS NOT NULL AND embedding_version <> '' LIMIT 1"
            )
            if vrow:
                info["model"]["version"] = str(vrow["embedding_version"])
            info["vector_recall"]["available"] = embedded > 0
        except Exception:  # noqa: BLE001
            pass
        return info


_TEXT_STORE = TextColumnCosineStore()


def get_vector_store() -> VectorStore:
    """工厂：auto/text 走 P0 实现；pgvector 配置未落地时静默降级到 text。

    降级原因写入 diagnostics.vector_backend_fallback（由调用方记录），
    不假装 pgvector 已启用。
    """
    backend = str(getattr(settings, "vector_backend", "auto") or "auto").lower()
    if backend == "pgvector" and extension_available():
        # P1 才实现 PgVectorStore；当前即使配置了也保持 text 行为，绝不假装可用
        return _TEXT_STORE
    return _TEXT_STORE


__all__ = [
    "VectorStore", "VectorItem", "TextColumnCosineStore", "get_vector_store",
    "ensure_columns", "extension_available", "rrf_fuse", "_RRF_K",
]
