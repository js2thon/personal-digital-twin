"""安全审计查询端点（SIL-A07 / P2 收口）。

全部端点要求 owner 级主体：审计记录含主体、路径与命中样本，属敏感出口。
`verify` 与 `flush` 会触发落库，同样纳入 owner 闸门。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app import security_audit as sa
from app.api.v1.cq_v2 import require_owner

router = APIRouter()


def _guard(request: Request) -> None:
    require_owner(request)


@router.get("/security/audit/recent")
def recent(request: Request, limit: int = 50):
    """最近安全事件（内存读模型，启动时已从库水合，跨重启不清零）。"""
    _guard(request)
    events = sa.recent(limit)
    return {"events": events, "count": len(events), "source": "hydrated_memory"}


@router.get("/security/audit/stats")
def stats(request: Request):
    """审计通道自证：队列 / 刷盘 / 库行数 / 线程 / 水合状态。"""
    _guard(request)
    return sa.stats()


@router.get("/security/audit/query")
def query(request: Request, event_type: str | None = None,
          trace_id: str | None = None, limit: int = 100):
    """直接查权威副本 `audit.semantic_audit_logs`（跨进程、跨重启）。"""
    _guard(request)
    rows = sa.query(event_type=event_type, trace_id=trace_id, limit=limit)
    return {"rows": [_row(r) for r in rows], "count": len(rows),
            "source": sa.TABLE}


def _row(r: dict) -> dict:
    return {k: (str(v) if k in ("ts",) and v is not None else v)
            for k, v in r.items()}


@router.get("/security/audit/verify")
def verify(request: Request):
    """一次性对账：强制刷盘后校验队列清空且库可查。"""
    _guard(request)
    return sa.verify()


@router.post("/security/audit/flush")
def flush(request: Request, limit: int = 2000):
    """强制同步刷盘（正常由常驻线程每 1s 自动执行）。"""
    _guard(request)
    written = sa.flush(limit)
    if written == 0 and sa.stats().get("queue_size"):
        raise HTTPException(503, sa.stats().get("last_error") or "审计刷盘失败")
    return {"flushed": written, "stats": sa.stats()}
