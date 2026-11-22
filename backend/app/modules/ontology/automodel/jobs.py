"""建模任务持久化：ontology.automodel_jobs（断点续跑、审核、发布留痕）。

每个阶段的产物落 JSONB；DataFrame 不入库（只存画像与契约），重跑物化时按 source 重新读取。
无 DB（单测/离线）时退回进程内字典，接口一致。内存缓存与持久化查询均按 tenant 隔离。
"""
from __future__ import annotations

import json
import threading
import time

# Cache keys must include tenant: job IDs are globally unique in normal operation,
# but authorization must never depend on UUID collision resistance.
_MEM: dict[tuple[str, str], dict] = {}
_LOCK = threading.Lock()
_ensured = False

DDL = [
    "CREATE SCHEMA IF NOT EXISTS ontology",
    """CREATE TABLE IF NOT EXISTS ontology.automodel_jobs (
        id VARCHAR(40) PRIMARY KEY,
        tenant_id VARCHAR(64) NOT NULL DEFAULT 'default',
        source_id VARCHAR(120) NOT NULL,
        source_kind VARCHAR(20) NOT NULL,
        source_ref JSONB NOT NULL DEFAULT '{}',          -- 文件路径 / 加密后的连接串引用，不存明文
        status VARCHAR(30) NOT NULL,
        stage VARCHAR(30) NOT NULL,
        artifact JSONB NOT NULL DEFAULT '{}',            -- profiles / draft / contract / issues / stats / metrics
        review JSONB NOT NULL DEFAULT '[]',
        schema_version VARCHAR(120),
        published_by VARCHAR(120),
        error TEXT,
        created_by VARCHAR(120) NOT NULL DEFAULT 'owner',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_automodel_jobs_tenant ON ontology.automodel_jobs (tenant_id, created_at DESC)",
    # 保留历史索引；正式写入已由受控 mapping_execution 负责。
    "CREATE INDEX IF NOT EXISTS idx_entities_external_ids ON ontology.entities "
    "USING gin ((properties->'external_ids'))",
]


def _db():
    try:
        from app.db import session as db
        return db
    except Exception:  # noqa: BLE001
        return None


def ensure_schema() -> bool:
    global _ensured
    db = _db()
    if db is None:
        return False
    if not _ensured:
        for ddl in DDL:
            db.execute(ddl)
        _ensured = True
    return True


def _slim(job: dict) -> dict:
    """artifact 只存可序列化、可复核的部分（物化明细不入库，只留统计与样本）。"""
    mat = job.get("materialized") or {}
    return {
        "profiles": job.get("profiles"), "draft": job.get("draft"),
        "contract": job.get("contract"), "issues": job.get("issues"),
        "stages": job.get("stages"), "metrics": job.get("metrics"),
        "materialized": {"stats": mat.get("stats"), "errors": (mat.get("errors") or [])[:50],
                         "dangling": (mat.get("dangling") or [])[:50]},
        "written": job.get("written"),
        "config": job.get("config"), "actions_log": (job.get("actions_log") or [])[-500:],
        "dataset_id": job.get("dataset_id"), "design_layout": job.get("design_layout"),
    }


def save(job: dict, tenant: str = "default", actor: str = "owner",
         source_ref: dict | None = None) -> dict:
    job["updated_at"] = time.time()
    cache_key = (str(tenant), str(job["id"]))
    with _LOCK:
        _MEM[cache_key] = job
    if not ensure_schema():
        return job
    db = _db()
    stage = next(reversed(job.get("stages") or {"created": 1}))
    params = {"id": job["id"], "t": tenant, "s": job["source"], "k": job.get("source_kind", "file"),
              "ref": json.dumps(source_ref or job.get("source_ref") or {}, ensure_ascii=False),
              "st": job["status"], "stage": stage,
              "a": json.dumps(_slim(job), ensure_ascii=False, default=str),
              "r": json.dumps((job.get("draft") or {}).get("review") or [], ensure_ascii=False, default=str),
              "v": job.get("schema_version"), "pb": job.get("published_by"),
              "e": job.get("error"), "cb": actor}
    db.execute(
        "INSERT INTO ontology.automodel_jobs (id, tenant_id, source_id, source_kind, source_ref, status, "
        " stage, artifact, review, schema_version, published_by, error, created_by) VALUES "
        "(:id, :t, :s, :k, CAST(:ref AS jsonb), :st, :stage, CAST(:a AS jsonb), CAST(:r AS jsonb), "
        " :v, :pb, :e, :cb) "
        "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, stage=EXCLUDED.stage, "
        " artifact=EXCLUDED.artifact, review=EXCLUDED.review, schema_version=EXCLUDED.schema_version, "
        " published_by=EXCLUDED.published_by, error=EXCLUDED.error, updated_at=now() "
        "WHERE ontology.automodel_jobs.tenant_id=EXCLUDED.tenant_id", params)
    return job


def get(job_id: str, tenant: str = "default") -> dict | None:
    cache_key = (str(tenant), str(job_id))
    with _LOCK:
        cached = _MEM.get(cache_key)
    if cached is not None:
        return cached
    if not ensure_schema():
        return None
    row = _db().fetch_one(
        "SELECT * FROM ontology.automodel_jobs WHERE id=:i AND tenant_id=:t", {"i": job_id, "t": tenant})
    if not row:
        return None
    art = row["artifact"] if isinstance(row["artifact"], dict) else json.loads(row["artifact"] or "{}")
    ref = row["source_ref"] if isinstance(row["source_ref"], dict) else json.loads(row["source_ref"] or "{}")
    job = {"id": row["id"], "source": row["source_id"], "source_kind": row["source_kind"],
           "source_ref": ref, "status": row["status"], "schema_version": row.get("schema_version"),
           "published_by": row.get("published_by"), "error": row.get("error"), **art}
    with _LOCK:
        _MEM[cache_key] = job
    return job


def listing(tenant: str = "default", limit: int = 50) -> list[dict]:
    if not ensure_schema():
        with _LOCK:
            jobs = [j for (t, _), j in _MEM.items() if t == str(tenant)]
            return [{"id": j["id"], "source": j["source"], "status": j["status"]}
                    for j in jobs[-max(1, min(limit, 200)):]]
    return _db().fetch_all(
        "SELECT id, source_id, source_kind, status, stage, schema_version, created_at, updated_at, "
        " jsonb_array_length(review) AS needs_review FROM ontology.automodel_jobs "
        "WHERE tenant_id=:t ORDER BY created_at DESC LIMIT :n", {"t": tenant, "n": max(1, min(limit, 200))})
