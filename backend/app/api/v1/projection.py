"""SIL-EC-01～EC-05 投影与事件账本 API。

六条端点，全部围绕「派生数据可对账、可重放、可重建」：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET  | `/projection/workers`    | 投影目标清单（含不可用原因与覆盖率） |
| GET  | `/projection/outbox`     | 账本状态 + 事件流水 |
| POST | `/projection/outbox/drain`   | 手动消费一批（运维/测试用） |
| POST | `/projection/outbox/replay`  | 失败/死信事件重投（默认 dry run） |
| GET  | `/projection/reconcile`  | 只读对账：积压 + 漂移 + 一致性判定 |
| POST | `/projection/rebuild`    | 按 worker 全量重建（默认 dry run） |
"""
from fastapi import APIRouter, HTTPException

from app.modules.ontology import outbox
from app.modules.ontology import projection_worker as pw

router = APIRouter()


@router.get("/projection/workers")
def list_workers():
    """投影目标清单。不可用的目标必须给出原因，不允许静默缺失（EC-04）。"""
    return {"workers": pw.registry(), "state": pw.state()}


@router.get("/projection/outbox")
def get_outbox(limit: int = 20, status: str | None = None):
    if status and status not in outbox.STATUSES:
        raise HTTPException(400, f"status 必须是 {list(outbox.STATUSES)} 之一")
    return {"stats": outbox.stats(), "events": outbox.recent(limit=limit, status=status)}


@router.post("/projection/outbox/drain")
def drain_outbox(limit: int = 100):
    """手动消费一批事件。幂等：重复调用不会产生重复投影。"""
    if limit < 1 or limit > 500:
        raise HTTPException(400, "limit 需在 1～500 之间")
    outbox.ensure_schema()
    return outbox.drain(limit=limit)


@router.post("/projection/outbox/replay")
def replay_outbox(dry_run: bool = True, limit: int = 200,
                  status: str = "failed,dead"):
    """把失败 / 死信事件重置为 pending。默认 dry run，先看条数再执行。"""
    statuses = tuple(s.strip() for s in status.split(",") if s.strip())
    if not statuses:
        raise HTTPException(400, "status 不能为空")
    bad = [s for s in statuses if s not in outbox.STATUSES]
    if bad:
        raise HTTPException(400, f"不支持的状态 {bad}；可选 {list(outbox.STATUSES)}")
    if any(s == outbox.PENDING for s in statuses):
        raise HTTPException(400, "pending 事件本身就在队列中，无需重放")
    return outbox.replay(statuses=statuses, limit=limit, dry_run=dry_run)


@router.get("/projection/reconcile")
def reconcile(limit: int = 50):
    """只读对账：账本积压、死信、投影漂移与 in_sync 判定。"""
    return pw.reconcile(limit=max(1, min(limit, 200)))


@router.post("/projection/rebuild")
def rebuild_projection(worker: str = "graph", dry_run: bool = True):
    """全量重建派生数据。图的重建是「先算目标态、再补缺删余」，无空窗。"""
    names = {w["name"] for w in pw.registry()}
    if worker not in names:
        raise HTTPException(404, f"未知投影目标 {worker}；可选 {sorted(names)}")
    return pw.rebuild(worker=worker, dry_run=dry_run)
