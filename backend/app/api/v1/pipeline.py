"""链路详情 API：按次回放 数据层 → 本体层 → 知识层 → 生成层 的完整快照。"""
import uuid

from fastapi import APIRouter

from app.db import session as db
from app.modules.twin import pipeline

router = APIRouter()


@router.get("/pipeline/runs")
def list_runs(kind: str | None = None, limit: int = 50):
    rows = pipeline.list_runs(kind=kind, limit=limit)
    for r in rows:
        r["id"] = str(r.get("id"))
        r["created_at"] = str(r.get("created_at") or "")[:19]
        # 列表只带节点摘要，详情再拉全量
        r["node_count"] = len(r.pop("nodes", []) or [])
    return rows


@router.get("/pipeline/runs/{run_id}")
def get_run(run_id: str):
    try:
        uuid.UUID(run_id)
    except ValueError:
        return {"error": "run_id 不是合法 UUID"}
    row = pipeline.get_run(run_id)
    if not row:
        return {"error": "链路记录不存在"}
    row["id"] = str(row["id"])
    row["created_at"] = str(row.get("created_at") or "")[:19]
    return row


@router.delete("/pipeline/runs/{run_id}")
def delete_run(run_id: str):
    try:
        uuid.UUID(run_id)
    except ValueError:
        return {"error": "run_id 不是合法 UUID"}
    db.execute("DELETE FROM workflow.pipeline_runs WHERE id = CAST(:id AS uuid)",
               {"id": run_id})
    return {"deleted": run_id}
