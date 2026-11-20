"""版本冻结与可追溯（V1.3 语义层方案 · 能力 5 · 验收 3）。

任务启动时把「当前 published 的 ontology_version + 完整语义 Schema 快照 +
服务端上下文」冻结进 `ontology.runtime_task_snapshots`（同一 task_id 首次
冻结后不可变，ON CONFLICT DO NOTHING）。查询/决策/CQ 执行均可通过
snapshot_id 还原到当次冻结的具体版本，而不是「现在的最新版本」。
"""
from __future__ import annotations

from . import store


def _published_ontology_version() -> str:
    """优先读 ontology.ontology_versions 的 published 版本（与运行时同一口径）。"""
    try:
        row = store.db.fetch_one(
            "SELECT version FROM ontology.ontology_versions "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1")
        if row and row.get("version"):
            return str(row["version"])
    except Exception:  # noqa: BLE001
        pass
    return "v1.0.0"


def current_version() -> dict:
    schema = store.load_semantic_schema() or {}
    return {
        "ontology_version": _published_ontology_version(),
        "schema_version": schema.get("schema_version", "sem-1.0.0"),
        "has_published_schema": bool(schema),
    }


def freeze(task_id: str, schema: dict, *, tenant_id: str = "default",
           server_context: dict | None = None, created_by: str = "system") -> dict:
    """冻结任务快照（幂等不可变）。返回快照 + immutable / already_frozen 标记。"""
    ov = _published_ontology_version()
    sv = schema.get("schema_version", "sem-1.0.0")
    before = get(task_id, tenant_id)
    if before:
        return {**before, "immutable": True, "already_frozen": True,
                "note": "同一 task_id 已冻结，返回首次快照（版本不可变）"}
    store.db.execute(
        "INSERT INTO ontology.runtime_task_snapshots "
        "(task_id, tenant_id, ontology_version, schema_version, schema_snapshot, "
        " server_context, created_by) "
        "VALUES (:tid, :t, :ov, :sv, CAST(:snap AS jsonb), CAST(:ctx AS jsonb), :by) "
        "ON CONFLICT (tenant_id, task_id) DO NOTHING",
        {"tid": task_id, "t": tenant_id, "ov": ov, "sv": sv,
         "snap": store.jdump(schema), "ctx": store.jdump(server_context or {}),
         "by": created_by},
    )
    return {**get(task_id, tenant_id), "immutable": True, "already_frozen": False}


def get(task_id: str, tenant_id: str = "default") -> dict | None:
    row = store.db.fetch_one(
        "SELECT id, task_id, tenant_id, ontology_version, schema_version, "
        " schema_snapshot, server_context, frozen_at FROM ontology.runtime_task_snapshots "
        "WHERE task_id = :tid AND tenant_id = :t",
        {"tid": task_id, "t": tenant_id})
    return store._decode(row) if row else None


def resolve(task_id: str, tenant_id: str = "default") -> dict:
    """按任务快照还原：返回冻结当时的语义 Schema 与版本（供查询/决策绑定）。"""
    snap = get(task_id, tenant_id)
    if not snap:
        raise LookupError(f"任务快照 {task_id} 不存在")
    schema = store.jload(snap.get("schema_snapshot"), {}) or {}
    return {
        "task_id": task_id,
        "ontology_version": snap["ontology_version"],
        "schema_version": snap["schema_version"],
        "frozen_at": str(snap.get("frozen_at") or ""),
        "schema": schema,
        "bound": True,
        "note": "查询/决策已还原到任务启动时冻结的 ontology_version",
    }


def list_snapshots(tenant_id: str = "default", limit: int = 50) -> list[dict]:
    rows = store.db.fetch_all(
        "SELECT id, task_id, ontology_version, schema_version, frozen_at "
        "FROM ontology.runtime_task_snapshots WHERE tenant_id = :t "
        "ORDER BY frozen_at DESC LIMIT :lim", {"t": tenant_id, "lim": limit})
    return [{**r, "id": str(r["id"]), "frozen_at": str(r.get("frozen_at") or "")}
            for r in rows]
