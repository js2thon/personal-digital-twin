"""消息中心 API：企业 IM消息接管、回复草稿、逐条确认与受控外发。

铁律：扫描/起草只读；任何外发必须先经本人逐条确认，且默认只返回 dry-run 预览。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import current_actor
from app.modules.messaging import service as messaging

router = APIRouter()


class ScanIn(BaseModel):
    days: int = 7
    max_conversations: int = 60    # 全量接管：覆盖时间窗内所有有活动的会话
    max_items: int = 100
    include_direct: bool = True    # False 时仅群聊
    incremental: bool = True       # True 时只重读上次扫描后有新消息的会话


class ReviewIn(BaseModel):
    action: str = "approve"          # approve | reject
    edited_text: str = ""            # 本人修改后的定稿
    reviewer: str = "owner"


class SendIn(BaseModel):
    confirm: bool = False            # 必须为 True：本人逐条确认
    live: bool = False               # 是否请求真实外发（仍受服务端总开关约束）
    actor: str = "owner"


@router.get("/messages/config")
def messages_config():
    return messaging.config()


@router.get("/messages/scan/status")
def scan_status():
    return messaging.scan_status()


@router.post("/messages/scan")
def start_scan(body: ScanIn | None = None):
    body = body or ScanIn()
    return messaging.start_scan(body.days, body.max_conversations, body.max_items,
                                body.include_direct, body.incremental)


@router.get("/messages/search")
def search_messages(q: str = "", days: int = 30, limit: int = 10):
    """全局搜索：消息关键字、联系人、群名称、会议、日历。"""
    return messaging.search_all(q, days, limit)


@router.get("/messages/replies")
def list_replies(status: str = "pending", limit: int = 50, conversation_id: str = ""):
    return messaging.list_replies(status, limit, conversation_id)


@router.post("/messages/replies/{reply_id}/review")
def review_reply(reply_id: str, body: ReviewIn, request: Request):
    # 身份由服务端注入：请求体里的 reviewer 不再被信任
    actor = current_actor(request)
    try:
        return messaging.review_reply(reply_id, body.action,
                                      body.edited_text, actor)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/messages/replies/{reply_id}/send")
def send_reply(reply_id: str, body: SendIn, request: Request):
    actor = current_actor(request)
    try:
        return messaging.send_reply(reply_id, body.confirm, body.live, actor)
    except ValueError as e:
        raise HTTPException(403, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/messages/replies/{reply_id}")
def get_reply(reply_id: str):
    row = messaging.get_reply(reply_id)
    if not row:
        raise HTTPException(404, "reply not found")
    return row
