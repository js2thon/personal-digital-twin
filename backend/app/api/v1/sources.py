"""数据接入 API：/sources"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import session as db
from app.modules.audit import service as audit

router = APIRouter()


class SourceCreate(BaseModel):
    source_type: str  # chat_export/email/file/note/meeting/web
    source_name: str
    connector_config: dict = {}


@router.post("/sources")
def create_source(body: SourceCreate):
    sid = db.insert_returning_id(
        "source.data_sources",
        {
            "source_type": body.source_type,
            "source_name": body.source_name,
            "connector_config": json.dumps(body.connector_config),
        },
    )
    audit.log_access("user", "create_source", "data_source", sid)
    return {"id": sid}


@router.get("/sources")
def list_sources():
    return db.fetch_all(
        "SELECT id, source_type, source_name, enabled, created_at "
        "FROM source.data_sources ORDER BY created_at DESC"
    )


# 已实现连接器的来源类型 → DWS 数据源种类
_DWS_KIND = {
    "dws_document": "document", "dws_chat": "chat",
    "dws_calendar": "calendar", "dws_minutes": "minutes",
}


@router.get("/sources/outbox")
def list_outbox(limit: int = 20):
    """Outbox 事件流水：同步请求与处理结果都可查，不再是没人消费的死表。"""
    return db.fetch_all(
        "SELECT id, event_type, aggregate_id, payload, processed, created_at "
        "FROM ingest.outbox_events ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(limit, 100))},
    )


@router.post("/sources/{source_id}/sync")
def sync_source(source_id: str):
    """真实执行一次数据源刷新（只读 DWS 列表），并把结果写入 Outbox。

    此前只插一条 `source.sync_requested` 就返回 sync_scheduled，但没有任何消费者，
    是假接口。现在：已接入的 DWS 来源立即刷新；未接入的类型明确报错，不假装排队。
    """
    row = db.fetch_one(
        "SELECT id, source_type, source_name, enabled FROM source.data_sources "
        "WHERE id = :id", {"id": source_id}
    )
    if not row:
        raise HTTPException(404, "source not found")
    if not row["enabled"]:
        raise HTTPException(400, "source disabled")
    kind = _DWS_KIND.get(str(row["source_type"]))
    if not kind:
        raise HTTPException(
            400,
            f"数据源类型「{row['source_type']}」尚未配置连接器，无法同步；"
            "本地上传请直接在控制台上传文件。",
        )
    try:
        from app.modules.dws_source import streams

        result = streams.list_items(kind, days=7, limit=30, all_pages=False)
    except Exception as e:  # noqa: BLE001  DWS 不可用时返回可读错误
        raise HTTPException(502, f"DWS 同步失败：{type(e).__name__}: {str(e)[:200]}")
    payload = {
        "source_id": source_id, "source_type": row["source_type"],
        "kind": kind, "items": result.get("count", 0),
        "direct_count": result.get("direct_count"),
    }
    with db.transaction():
        event_id = db.insert_returning_id(
            "ingest.outbox_events",
            {"event_type": "source.synced", "aggregate_id": source_id,
             "payload": json.dumps(payload, ensure_ascii=False), "processed": True},
        )
        audit.log_access("user", "source_synced", "data_source", source_id, payload)
    return {"status": "synced", "source_id": source_id, "kind": kind,
            "items": payload["items"], "outbox_event_id": event_id,
            "note": "本地无独立 Worker，同步已在本次请求内真实完成并标记为已处理"}
