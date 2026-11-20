"""企业 IM（钉钉）消息接管服务。

安全契约（任何路径都不得绕过）：
  1. 收件与草稿生成只调用 DWS 的 effect=read 命令，绝不发送任何消息。
  2. 草稿落库状态恒为 pending；必须由本人调用 review(approve) 才能变成 approved。
  3. 真实外发需同时满足：status=approved + 请求体 confirm=True +
     settings.reply_send_enabled=True + live=True。
     默认 reply_send_enabled=False，此时「发送」只执行 dws --dry-run 预览
     （返回 willSend=false），不会外发。
  4. 每条外发带稳定幂等键，并写入审计日志与发送结果快照。

CLI 依据：dws chat +messages-reply 的 Safety 为
  effect=write / risk=medium / confirmation=user_required，
  帮助明确要求「Do not use --yes until the user explicitly confirms this operation」——
  因此只有本人明确确认过（confirm=True）时才追加 -y。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timedelta

from app.config import settings
from app.db import session as db
from app.modules.audit import service as audit
from app.modules.dws_source.client import DwsError, _is_authenticated, _run

_DDL = """
CREATE TABLE IF NOT EXISTS workflow.message_replies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id VARCHAR(400) NOT NULL UNIQUE,
    conversation_id VARCHAR(400),
    conversation_name VARCHAR(500),
    conversation_type VARCHAR(20) DEFAULT 'group',
    sender VARCHAR(300),
    sender_id VARCHAR(300),
    incoming_text TEXT,
    incoming_time VARCHAR(60),
    history JSONB DEFAULT '[]',
    trigger_reason VARCHAR(100),
    draft_text TEXT,
    final_text TEXT,
    unconfirmed_points JSONB DEFAULT '[]',
    evidence JSONB DEFAULT '[]',
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    reviewed_by VARCHAR(200),
    reviewed_at TIMESTAMPTZ,
    send_mode VARCHAR(20),
    send_result JSONB,
    sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
)
"""
_INDEX_DDL = ("CREATE INDEX IF NOT EXISTS idx_message_replies_status "
              "ON workflow.message_replies (status, created_at DESC)")
_MIGRATIONS = [
    # 本地老库升级（CREATE TABLE IF NOT EXISTS 不会补列）
    "ALTER TABLE workflow.message_replies ADD COLUMN IF NOT EXISTS "
    "conversation_type VARCHAR(20) DEFAULT 'group'",
    "ALTER TABLE workflow.message_replies ADD COLUMN IF NOT EXISTS "
    "history JSONB DEFAULT '[]'",
    # 链路追踪：草稿生成经过哪些节点、本体语义是否参与，逐条落库供页面点亮
    "ALTER TABLE workflow.message_replies ADD COLUMN IF NOT EXISTS "
    "trace JSONB DEFAULT '[]'",
    # 会话与扫描状态持久化：此前都是进程内存，重启即丢
    "CREATE TABLE IF NOT EXISTS workflow.twin_sessions ("
    " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
    " session_id VARCHAR(200) NOT NULL,"
    " request_id VARCHAR(100) NOT NULL UNIQUE,"
    " message TEXT, answer JSONB, task_type VARCHAR(50),"
    " created_at TIMESTAMPTZ DEFAULT NOW())",
    "CREATE INDEX IF NOT EXISTS idx_twin_sessions_session "
    "ON workflow.twin_sessions (session_id, created_at DESC)",
    "CREATE TABLE IF NOT EXISTS workflow.scan_state ("
    " id SMALLINT PRIMARY KEY DEFAULT 1,"
    " state JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())",
]

_ensured = False
_ensure_lock = threading.Lock()

# 后台扫描状态（单进程 API 内共享）
_scan: dict = {
    "running": False, "phase": "idle", "found": 0, "created": 0,
    "skipped": 0, "error": None, "warnings": [],
    "started_at": None, "finished_at": None,
}
_scan_lock = threading.Lock()

_QUESTION_RE = re.compile(
    r"[?？]|请|麻烦|能否|可否|是否|确认|回复|看下|帮忙|协助|尽快|提交|反馈|"
    r"意见|安排|什么时候|多久|需要你|麻烦你|跟进|"
    # 群聊里最常见的「无问号但有明确诉求」说法，此前会漏判成不需要回复，
    # 导致该群最新消息被跳过、界面上一直停留在更早的旧消息。
    r"更新|进展|同步|汇总|整理|填报|补充|发我|给我|通知|催|处理下|"
    r"今天|明天|本周|这周|下班前|几点| deadline|milestone"
)
# 纯确认类短句：本身就是「已处理」，不为它生成草稿（避免把旧消息顶成最新）
_ACK_RE = re.compile(
    r"^(?:收到|好的?|嗯+|哦+|ok|okay|了解|明白|已阅|谢谢|感谢|辛苦|辛苦了|赞|行|可以|没问题)"
    r"[!！。.\s，,~～]*$",
    re.IGNORECASE,
)
# 系统/通知类会话不接管：不是人发的，生成草稿只是噪音
_SYSTEM_CHAT_RE = re.compile(r"^(?:工作通知|系统消息|服务通知|群助手|钉钉团队|应用消息)")


def ensure_schema() -> None:
    """幂等建表：本机内嵌 PG 已存在旧库，不能依赖重新执行 init SQL。"""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        db.execute(_DDL)
        db.execute(_INDEX_DDL)
        for stmt in _MIGRATIONS:
            try:
                db.execute(stmt)
            except Exception:  # noqa: BLE001  单条迁移失败不阻断服务启动
                pass
        _ensured = True


def _clean(s) -> str:
    from app.modules.document_parser.adapters import clean_text

    return clean_text(str(s or ""))


def _set_scan(**kw) -> None:
    with _scan_lock:
        _scan.update(kw)
        snapshot = dict(_scan)
    # 只在阶段切换 / 扫描结束时落库，避免每条草稿都写一次
    if "phase" in kw or "running" in kw:
        try:
            db.execute(
                "INSERT INTO workflow.scan_state (id, state) "
                "VALUES (1, CAST(:s AS jsonb)) "
                "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, "
                "updated_at = NOW()",
                {"s": json.dumps(snapshot, ensure_ascii=False, default=str)},
            )
        except Exception:  # noqa: BLE001  状态持久化失败不影响扫描本身
            pass


def scan_status() -> dict:
    ensure_schema()
    with _scan_lock:
        current = dict(_scan)
    if current.get("phase") == "idle":
        row = db.fetch_one("SELECT state FROM workflow.scan_state WHERE id = 1")
        if row and isinstance(row.get("state"), dict):
            restored = dict(row["state"])
            restored["running"] = False
            restored["restored_from_db"] = True
            return restored
    return current


# ------------------------------------------------------------------ 收件

def _self_identity() -> dict:
    try:
        return _is_authenticated() or {}
    except Exception:  # noqa: BLE001
        return {}


def _is_mine(sender: str, me: dict) -> bool:
    name = str(me.get("user_name") or "").strip()
    uid = str(me.get("user_id") or "").strip()
    if not (name or uid):
        return False
    return (name and name in sender) or (uid and uid == sender)


def _at_me(text: str, me: dict) -> bool:
    """是否 @ 到我（群聊里 @我 一律视为需要本人回应，不再看关键词）。"""
    name = str(me.get("user_name") or "").strip()
    if not name or "@" not in (text or ""):
        return False
    return bool(re.search(r"@\s*" + re.escape(name), text))


def _normalize(m: dict, reason: str, me: dict, conv_name: str = "",
               conv_type: str = "") -> dict | None:
    """把 DWS 不同命令的消息结构统一成草稿输入。"""
    mid = m.get("messageId") or m.get("openMessageId") or m.get("id")
    if not mid:
        return None
    conv_id = m.get("conversationId") or m.get("openConversationId") or ""
    if isinstance(conv_id, dict):
        conv_id = conv_id.get("id") or ""
    name = conv_name or m.get("conversation") or ""
    conv_obj = m.get("conversation") if isinstance(m.get("conversation"), dict) else {}
    if isinstance(name, dict):
        name = name.get("name") or ""
    raw_type = str(conv_type or m.get("conversationType")
                   or conv_obj.get("conversationType") or "").lower()
    ctype = "direct" if raw_type in ("direct", "p2p", "single", "private", "1") else "group"
    text = _clean(m.get("text") or m.get("content") or "")
    sender = _clean(m.get("sender") or "")
    t = m.get("createTime") or m.get("time") or ""
    mine = _is_mine(sender, me)
    # 纯图片/文件占位、纯 @ 提及、空文本、自己发的都不代回
    stripped = re.sub(r"\[[^\]]*\]\([^)]*\)", "", text).strip()
    mentions_only = bool(re.fullmatch(r"(?:@[^\s@]+\s*)+", stripped))
    meaningful = bool(stripped) and not stripped.startswith("[") and not mentions_only
    at_me = _at_me(text, me)
    ack = bool(_ACK_RE.match(stripped))
    # 群聊只挑提问/请求；但只要 @我 或本身就是单聊，就一律进入待确认草稿，
    # 避免「最新一条只是催进度、没有问号」这类真需求被关键词规则静默丢弃。
    needs = (not mine) and meaningful and (
        at_me or ctype == "direct" or reason.startswith("direct")
        or bool(_QUESTION_RE.search(text))
    )
    return {
        "message_id": str(mid),
        "conversation_id": _clean(conv_id),
        "conversation_name": _clean(name) or "(未命名会话)",
        "conversation_type": ctype,
        "sender": sender or "未知发送人",
        "sender_id": _clean(m.get("senderId") or ""),
        "incoming_text": text,
        "incoming_time": _clean(t),
        "trigger_reason": reason,
        "needs_reply": bool(needs),
        "at_me": at_me,
        "ack": ack,
        "meaningful": meaningful,
    }


def _conv_type(conv: dict) -> str:
    v = str((conv or {}).get("conversationType") or "").lower()
    return "direct" if v in ("direct", "p2p", "single", "private", "1") else "group"


def _read_messages(cid: str, limit: int = 10) -> list[dict]:
    d = _run(["chat", "+chat-messages", "--group", str(cid),
              "--limit", str(limit), "-f", "json"], 90)
    return [m for m in (d.get("messages") or []) if isinstance(m, dict)]


def _to_ms(value) -> float | None:
    """把 DWS 的多种时间格式统一成毫秒时间戳。

    chat-list 的 lastMsgCreateAt 实际是 ISO 串（2026-11-16T07:00:12.448+08:00），
    老代码只认纯数字，导致 days 时间窗完全失效（永远判定为「最近」）。
    """
    s = str(value or "").strip()
    if not s:
        return None
    if s.isdigit():
        ts = int(s)
        return float(ts * 1000 if ts < 10_000_000_000 else ts)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp() * 1000
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace(" ", "T")).timestamp() * 1000
        except ValueError:
            return None


def _is_recent(last, cutoff_ms: float) -> bool:
    ms = _to_ms(last)
    if ms is None:
        return True  # 时间格式未知时不误杀
    return ms >= cutoff_ms


def _history_lines(msgs: list[dict], me: dict, limit: int = 20) -> list[dict]:
    """把会话最近消息压成给草稿展示/提示词用的紧凑结构（旧→新）。"""
    out: list[dict] = []
    for m in msgs[:limit]:
        if not isinstance(m, dict):
            continue
        text = _clean(m.get("text") or m.get("content") or "")
        if not text:
            continue
        out.append({
            "sender": _clean(m.get("sender") or "未知")
                      + ("（我）" if _is_mine(_clean(m.get("sender") or ""), me) else ""),
            "text": text[:300],
            "time": _clean(m.get("createTime") or m.get("time") or ""),
        })
    out.reverse()   # DWS 返回新→旧；展示与提示词都按时间正序
    return out


def _collect(days: int, max_conversations: int, max_items: int,
             include_direct: bool = True, progress=None,
             since_ms: float | None = None) -> tuple[list[dict], list[str]]:
    """只读收集全部会话的**最新**一条待处理消息（群聊 + 单聊）。

    规则（最新优先，避免旧消息顶替最新消息）：
      1. 每个会话读最近 20 条；最新一条是本人发的 → 视为已回复，跳过（自聊会话例外）。
      2. 最新一条对方消息若 @我 / 是单聊 / 命中请求词 → 直接作为接管对象。
      3. 不含请求词、但也不是「收到/好的」这类纯确认 → 仍按最新消息接管
         （trigger_reason=latest_group / latest_direct），保证界面看到的是会话真正的最新消息。
      4. since_ms = 上次扫描完成时间，之后无新消息的会话直接跳过（增量同步，扫描更快）。
    所有命令都是 effect=read；收集结果只是草稿输入，不会发送任何消息。
    """
    me = _self_identity()
    found: list[dict] = []
    seen: set[str] = set()
    warnings: list[str] = []

    types = "group,p2p" if include_direct else "group"
    try:
        listing = _run(
            ["chat", "+chat-list", "--types", types, "--page-size", "100",
             "--page-all", "--page-limit", "10", "-f", "json"], 120,
        )
        chats = listing.get("chats") or []
    except DwsError as e:
        return [], [f"会话列表读取失败：{str(e)[:160]}"]

    cutoff = (datetime.now() - timedelta(days=days)).timestamp() * 1000
    checked = 0
    for conv in chats:
        if len(found) >= max_items or checked >= max_conversations:
            break
        cid = conv.get("openConversationId") if isinstance(conv, dict) else None
        if not cid:
            continue
        conv_name = str(conv.get("name") or "")
        if _SYSTEM_CHAT_RE.match(conv_name):
            continue   # 工作通知/系统消息等非人会话不接管
        if not _is_recent(conv.get("lastMsgCreateAt"), cutoff):
            continue   # 时间窗外的会话不接管
        last_seen = _to_ms(conv.get("lastMsgCreateAt"))
        if since_ms and last_seen and last_seen <= since_ms:
            continue   # 上次扫描之后该会话没有新消息（增量跳过）
        checked += 1
        if progress:
            progress(checked, len(chats))
        ctype = _conv_type(conv)
        # 自聊会话（给自己发消息/文件，会话名即本人姓名）：
        # 消息全部由本人发出，正常规则会视为「已回复」跳过；这里改为同样生成草稿，
        # 会话名标注（我自己），供用户在草稿区直接看到并可让分身代为整理回复。
        self_chat = ctype == "direct" and _is_mine(conv.get("name") or "", me)
        try:
            msgs = _read_messages(cid, 20)
        except DwsError as e:
            warnings.append(
                f"会话消息读取失败（{conv.get('name') or cid}）：{str(e)[:80]}")
            continue
        history = _history_lines(msgs, me, 20)
        # 取「该会话最新一条对方的有效消息」为接管对象：
        #   - 本人发的 → 已回复（自聊会话例外）；
        #   - 纯图片/文件/纯@ → 跳过看更早一条；
        #   - 「收到/好的」这类纯确认 → 不占接管位，继续看稍早一条。
        # 不再用关键词决定「要不要看更早的」，否则关键词不命中时会把几天前的
        # 旧消息顶成最新（群消息看起来像没同步）。
        target = None
        for m in msgs:
            c = _normalize(m, "latest_unanswered", me,
                           conv_name=(conv.get("name") or ""), conv_type=ctype)
            if not c:
                continue
            if _is_mine(c["sender"], me):
                target = c if self_chat else None
                break          # 本人发的：自聊接管，其它会话视为已回复
            if not c["meaningful"] or c["ack"]:
                continue
            c["trigger_reason"] = ("at_me" if c["at_me"]
                                   else ("latest_direct" if ctype == "direct"
                                         else "latest_group"))
            target = c
            break
        if target and target["message_id"] not in seen and not _exists(target["message_id"]):
            if self_chat and "（我自己）" not in target["conversation_name"]:
                target["conversation_name"] += "（我自己）"
            seen.add(target["message_id"])
            target["history"] = history
            found.append(target)
    return found, warnings


# ------------------------------------------------------------------ 起草

def _make_draft(c: dict) -> dict:
    """复用数字分身的 communication 图：检索个人知识 + 认知画像 + 承诺检查。

    会话最近 20 条历史消息一并注入提示词，让回复贴合上下文而不是孤立看最后一条。
    """
    evidence: list[dict] = []
    unconfirmed: list[str] = []
    draft = ""
    state: dict = {}
    history = c.get("history") or []
    hist_text = ""
    if history:
        hist_text = "\n\n最近消息记录（旧→新，供理解上下文，最后一条是待回复消息）：\n" + "\n".join(
            f"{i}. {h['sender']}: {h['text']}" for i, h in enumerate(history, 1)
        )
    try:
        from app.modules.twin.orchestrator import run_task

        state = run_task("communication", {
            "request_id": str(uuid.uuid4()),
            "user_id": "owner",
            "input_message": (
                f"会话：{c['conversation_name']}\n"
                f"发送人：{c['sender']}\n"
                f"时间：{c['incoming_time']}\n"
                f"待回复消息：{c['incoming_text']}"
                + hist_text + "\n\n"
                "请结合以上对话上下文，并检索我的知识库与个人画像，"
                "代我起草一条简短、得体的钉钉引用回复。"
            ),
        })
        fr = state.get("final_response") or {}
        draft = _clean(fr.get("draft") or "").strip()
        unconfirmed = [_clean(x) for x in (fr.get("unconfirmed_points") or []) if x]
        for f in (state.get("retrieved_facts") or [])[:3]:
            evidence.append({"type": "fact",
                             "quote": _clean(f.get("object_value") or f.get("content") or "")[:220]})
        for d in (state.get("retrieved_documents") or [])[:3]:
            evidence.append({"type": "document",
                             "source": _clean(d.get("title") or d.get("source_id") or ""),
                             "quote": _clean(d.get("content") or "")[:220]})
    except Exception as e:  # noqa: BLE001  LLM/检索不可用时不能让扫描整体失败
        unconfirmed.append(f"草稿生成失败（{type(e).__name__}）：{str(e)[:180]}")
    if not draft:
        unconfirmed.append("模型未生成草稿，请本人手动填写后再确认")
    d = {"draft_text": draft, "unconfirmed_points": unconfirmed,
         "evidence": evidence, "trace": state.get("trace") or []}
    try:
        # 链路详情落库：草稿怎么来的，四层快照全部可回放
        from app.modules.twin import pipeline as pipe

        nodes = state.get("trace") or []
        pipe.record_run(
            "reply", c.get("conversation_name") or "", c.get("incoming_text") or "",
            "pending", nodes,
            pipe.stages_from_reply(c, d, state),
            max((n.get("ms") for n in nodes
                 if isinstance(n, dict) and isinstance(n.get("ms"), int)),
                default=None),
        )
    except Exception:  # noqa: BLE001
        pass
    return d


def _exists(message_id: str) -> bool:
    return bool(db.fetch_one(
        "SELECT 1 FROM workflow.message_replies WHERE message_id = :m",
        {"m": message_id},
    ))


def _insert(c: dict, d: dict) -> str:
    rid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO workflow.message_replies "
        "(id, message_id, conversation_id, conversation_name, conversation_type, "
        " sender, sender_id, incoming_text, incoming_time, history, trigger_reason, draft_text, "
        " unconfirmed_points, evidence, trace, status) "
        "VALUES (:id, :mid, :cid, :cname, :ctype, :sender, :sid, :txt, :t, "
        " CAST(:hist AS jsonb), :reason, :draft, CAST(:unc AS jsonb), CAST(:ev AS jsonb), "
        " CAST(:trace AS jsonb), 'pending')",
        {
            "id": rid, "mid": c["message_id"], "cid": c["conversation_id"],
            "cname": c["conversation_name"],
            "ctype": c.get("conversation_type") or "group", "sender": c["sender"],
            "sid": c["sender_id"], "txt": c["incoming_text"],
            "t": c["incoming_time"],
            "hist": json.dumps(c.get("history") or [], ensure_ascii=False, default=str),
            "reason": c["trigger_reason"],
            "draft": d["draft_text"],
            "unc": json.dumps(d["unconfirmed_points"], ensure_ascii=False, default=str),
            "ev": json.dumps(d["evidence"], ensure_ascii=False, default=str),
            "trace": json.dumps(d.get("trace") or [], ensure_ascii=False, default=str),
        },
    )
    return rid


def _scan_worker(days: int, max_conversations: int, max_items: int,
                 include_direct: bool, since_ms: float | None = None) -> None:
    try:
        ensure_schema()
        _set_scan(phase="collecting", created=0, skipped=0, found=0,
                  scanned=0, total_conversations=0, warnings=[], error=None)

        def _progress(done: int, total: int) -> None:
            _set_scan(scanned=done, total_conversations=total)

        candidates, warnings = _collect(
            days, max_conversations, max_items, include_direct,
            progress=_progress, since_ms=since_ms,
        )
        _set_scan(phase="drafting", found=len(candidates), warnings=warnings)
        created = skipped = 0
        for c in candidates:
            if _exists(c["message_id"]):
                skipped += 1
            else:
                _insert(c, _make_draft(c))
                created += 1
            _set_scan(created=created, skipped=skipped)
        _set_scan(phase="done")
    except Exception as e:  # noqa: BLE001
        _set_scan(phase="error", error=f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        _set_scan(running=False, finished_at=datetime.now().isoformat(timespec="seconds"))


def start_scan(days: int = 7, max_conversations: int = 5,
               max_items: int = 8, include_direct: bool = True,
               incremental: bool = True) -> dict:
    """异步启动一次收件+起草；立即返回，前端轮询 scan/status。

    include_direct=True 时额外覆盖最近有活动的单聊会话（已读/无提问词也纳入草稿）。
    incremental=True 时只重读「上次扫描之后有新消息」的会话，让页面每次进入都能
    快速同步到最新消息；留 10 分钟重叠窗口，避免扫描期间到达的消息被漏掉。
    """
    ensure_schema()
    since_ms = None
    if incremental:
        try:
            row = db.fetch_one("SELECT state FROM workflow.scan_state WHERE id = 1")
            prev = ((row or {}).get("state") or {}) if row else {}
            done_ms = _to_ms(prev.get("finished_at"))
            if done_ms:
                since_ms = done_ms - 10 * 60 * 1000
        except Exception:  # noqa: BLE001  取不到水位就退回全量扫描
            since_ms = None
    with _scan_lock:
        if _scan["running"]:
            return {"started": False, "reason": "already_running"}
        _scan.update(running=True, phase="queued", found=0, created=0,
                     skipped=0, error=None, warnings=[],
                     started_at=datetime.now().isoformat(timespec="seconds"),
                     finished_at=None)
    t = threading.Thread(
        target=_scan_worker,
        args=(max(1, min(days, 90)), max(0, min(max_conversations, 200)),
              max(1, min(max_items, 300)), bool(include_direct), since_ms),
        daemon=True,
    )
    t.start()
    return {"started": True}


# ------------------------------------------------------------------ 查询

_COLS = (
    "id, message_id, conversation_id, conversation_name, conversation_type, sender, sender_id, "
    "incoming_text, incoming_time, history, trigger_reason, draft_text, final_text, "
    "unconfirmed_points, evidence, trace, status, reviewed_by, reviewed_at, "
    "send_mode, send_result, sent_at, created_at"
)


def list_replies(status: str = "pending", limit: int = 50,
                 conversation_id: str = "") -> list[dict]:
    ensure_schema()
    # 按消息时间降序：incoming_time 通常是 DWS 的毫秒时间戳；
    # 提取其中的数字部分排序，取不到时间戳的排到最后，再按创建时间兜底。
    sort_key = ("NULLIF(regexp_replace(COALESCE(incoming_time, ''), '[^0-9]', '', 'g'), '')"
                "::numeric")
    cond = "WHERE (:s = 'all' OR status = :s) "
    params: dict = {"s": status or "pending", "l": max(1, min(limit, 200))}
    if (conversation_id or "").strip():
        # 指定会话（搜索结果点联系人/群名跳转时使用）
        cond += "AND conversation_id = :cid "
        params["cid"] = conversation_id.strip()
    return db.fetch_all(
        f"SELECT {_COLS} FROM workflow.message_replies "
        f"{cond}"
        f"ORDER BY {sort_key} DESC NULLS LAST, created_at DESC LIMIT :l",
        params,
    )


def get_reply(reply_id: str) -> dict | None:
    ensure_schema()
    return db.fetch_one(
        f"SELECT {_COLS} FROM workflow.message_replies WHERE id = :id",
        {"id": reply_id},
    )


# ------------------------------------------------------------------ 全局搜索

def _slim_dws_item(x: dict) -> dict:
    """DWS 条目瘦身：只保留展示字段，raw 对象可能很大且含敏感内容。

    会话类条目（chat-list）的会话 ID 存在 node_id（即钉钉 openConversationId，
    与本地草稿表 conversation_id 同源），这里统一输出为 conversation_id 供前端跳转。
    """
    return {"id": x.get("id") or x.get("node_id") or "",
            "conversation_id": x.get("node_id") or x.get("conversation_id") or "",
            "conversation_type": x.get("conversation_type") or "",
            "name": x.get("name") or "",
            "summary": x.get("summary") or "", "update_time": x.get("update_time") or ""}


def search_all(q: str, days: int = 30, limit: int = 10) -> dict:
    """消息中心全局搜索：消息/草稿、联系人、群聊、会议、日历。

    本地草稿与历史消息走数据库；联系人与群名称用 DWS chat-list（带缓存）过滤；
    会议走 minutes +search；日历取时间窗内日程后按标题/地点过滤。
    任一外部源失败不影响其它分类，失败原因放入 warnings。
    """
    q = (q or "").strip()
    if not q:
        return {"query": "", "sections": [], "warnings": []}
    like = f"%{q}%"
    lim = max(1, min(limit, 50))
    by_key: dict[str, dict] = {}
    warnings: list[str] = []

    # 消息与草稿（本地库：原文 / 草稿 / 定稿 / 会话名 / 最近 20 条历史）
    try:
        rows = db.fetch_all(
            "SELECT id, conversation_id, conversation_name, conversation_type, sender, sender_id, "
            " incoming_text, incoming_time, draft_text, final_text, status "
            "FROM workflow.message_replies "
            "WHERE incoming_text ILIKE :q OR COALESCE(draft_text,'') ILIKE :q "
            "   OR COALESCE(final_text,'') ILIKE :q OR conversation_name ILIKE :q "
            "   OR history::text ILIKE :q "
            "ORDER BY created_at DESC LIMIT :l",
            {"q": like, "l": lim},
        )
        by_key["messages"] = {"key": "messages", "label": "消息 / 草稿", "items": rows}
    except Exception as e:  # noqa: BLE001
        warnings.append(f"消息检索失败：{type(e).__name__}: {str(e)[:120]}")

    try:
        from app.modules.dws_source import streams as dws_streams

        # 联系人（单聊）与群聊：chat-list 自带 TTL 缓存，按名称过滤
        chats = dws_streams.list_items("chat", days=max(1, min(days, 365)), limit=1)
        chat_items = chats.get("items") or []
        low = q.lower()

        def _hit(x: dict) -> bool:
            return low in str(x.get("name") or "").lower()

        my_name = str(_self_identity().get("user_name") or "").strip()

        def _contact(x: dict) -> dict:
            it = _slim_dws_item(x)
            # 自聊会话（会话名即本人姓名）：标注「我自己」，点击后进单聊草稿区
            if my_name and my_name in it["name"]:
                it["conversation_type"] = "self"
            return it

        contacts = [_contact(x) for x in chat_items
                    if x.get("conversation_type") == "direct" and _hit(x)][:lim]
        groups = [_slim_dws_item(x) for x in chat_items
                  if x.get("conversation_type") == "group" and _hit(x)][:lim]
        by_key["contacts"] = {"key": "contacts", "label": "联系人（单聊）", "items": contacts}
        by_key["groups"] = {"key": "groups", "label": "群聊", "items": groups}

        # 会议听记：minutes +search 关键词全文检索
        try:
            mins = dws_streams.list_items("minutes", query=q, limit=lim)
            by_key["minutes"] = {"key": "minutes", "label": "会议听记",
                                 "items": [_slim_dws_item(x) for x in (mins.get("items") or [])][:lim]}
        except Exception as e:  # noqa: BLE001
            warnings.append(f"会议检索失败：{str(e)[:120]}")

        # 日历：时间窗内日程按标题/地点过滤
        try:
            cals = dws_streams.list_items("calendar", days=max(7, min(days, 90)), limit=1)
            cal_items = [_slim_dws_item(x) for x in (cals.get("items") or [])
                         if _hit(x) or low in str(x.get("summary") or "").lower()][:lim]
            by_key["calendar"] = {"key": "calendar", "label": "日历日程", "items": cal_items}
        except Exception as e:  # noqa: BLE001
            warnings.append(f"日历检索失败：{str(e)[:120]}")
    except Exception as e:  # noqa: BLE001  DWS 整体不可用时本地结果仍可用
        warnings.append(f"钉钉数据源不可用：{str(e)[:120]}")

    # 展示优先级：联系人 → 群聊 → 消息/草稿 → 会议 → 日历
    order = ("contacts", "groups", "messages", "minutes", "calendar")
    sections = [by_key[k] for k in order if k in by_key]

    return {"query": q, "sections": sections, "warnings": warnings}


def config() -> dict:
    return {
        "send_enabled": bool(settings.reply_send_enabled),
        "mode": "live" if settings.reply_send_enabled else "dry_run",
        "policy": "真实外发已开启：草稿经本人确认后即可发送，控制台无弹窗确认；",
        "coverage": {
            "scope": "全部群聊与单聊：每个会话取最新一条对方消息（@我或单聊一律接管，其余按最新优先）",
            "skip": "最新一条是本人发言的会话视为已回复；已生成过草稿的消息自动去重",
            "history": "每个草稿携带该会话最近 20 条历史消息，供展示与回复生成",
            "mode": "增量同步：进入消息中心自动重扫「上次扫描后有新消息」的会话，非实时常驻监听",
            "dws_list": "chat-list 显式带 --types group,p2p",
        },
        "cli_safety": "dws chat +messages-reply: effect=write, confirmation=user_required",
        "ai_tag": bool(settings.reply_ai_tag),
        "ai_tag_note": "发送角标为钉钉固定文案「通过AI发送」，平台不支持自定义文案；"
                       "在 .env 设 REPLY_AI_TAG=false 可不带角标发送",
        "note": "开启真实外发需在 .env 设置 REPLY_SEND_ENABLED=true 并重启服务；"
                "开启后每条仍需本人在控制台逐条确认。",
    }


# ------------------------------------------------------------------ 确认

def review_reply(reply_id: str, action: str, edited_text: str = "",
                 reviewer: str = "owner") -> dict:
    """本人逐条确认/忽略草稿。approve 后可编辑定稿；此步骤绝不发送。"""
    ensure_schema()
    row = get_reply(reply_id)
    if not row:
        raise ValueError("reply not found")
    if action not in ("approve", "reject", "reopen"):
        raise ValueError("action must be approve / reject / reopen")
    if action == "reopen":
        # 误忽略或想重新编辑时恢复为待确认；不删除任何历史，审计仍完整。
        if row["status"] not in ("rejected", "approved"):
            raise ValueError(f"reply status is {row['status']}, cannot reopen")
        db.execute(
            "UPDATE workflow.message_replies SET status = 'pending', "
            "send_mode = NULL, send_result = NULL, reviewed_by = NULL, "
            "reviewed_at = NULL WHERE id = :id",
            {"id": reply_id},
        )
        audit.log_access(reviewer, "reply_reopened", "message_reply", reply_id,
                         {"from_status": row["status"]})
        return {"id": reply_id, "status": "pending", "sent": False}
    if row["status"] not in ("pending", "approved"):
        raise ValueError(f"reply status is {row['status']}, cannot review")

    if action == "reject":
        db.execute(
            "UPDATE workflow.message_replies SET status = 'rejected', "
            "reviewed_by = :r, reviewed_at = NOW() WHERE id = :id",
            {"r": reviewer, "id": reply_id},
        )
        audit.log_access(reviewer, "reply_rejected", "message_reply", reply_id,
                         {"message_id": row["message_id"]})
        return {"id": reply_id, "status": "rejected", "sent": False}

    final = _clean(edited_text).strip() or _clean(row.get("draft_text")).strip()
    if not final:
        raise ValueError("定稿内容为空，请先填写回复内容")
    db.execute(
        "UPDATE workflow.message_replies SET status = 'approved', final_text = :f, "
        "reviewed_by = :r, reviewed_at = NOW() WHERE id = :id",
        {"f": final, "r": reviewer, "id": reply_id},
    )
    audit.log_access(reviewer, "reply_approved", "message_reply", reply_id,
                     {"message_id": row["message_id"], "final_length": len(final)})
    return {"id": reply_id, "status": "approved", "sent": False,
            "final_text": final}


# ------------------------------------------------------------------ 外发

def send_reply(reply_id: str, confirm: bool = False,
               live: bool = False, actor: str = "owner") -> dict:
    """受控外发。默认（或条件不足）只跑 --dry-run 预览，不会发送任何消息。"""
    ensure_schema()
    row = get_reply(reply_id)
    if not row:
        raise ValueError("reply not found")
    if row["status"] == "sent":
        raise ValueError("reply already sent")
    if row["status"] != "approved":
        raise ValueError("必须先由本人确认草稿（status=approved）")
    if not confirm:
        raise ValueError("缺少显式确认（confirm=true）")

    from app.modules.policy import engine as policy

    verdict = policy.check_tool("send_message")
    if verdict["verdict"] == "denied":
        raise ValueError("工具白名单禁止外发消息")
    audit.log_access(actor, "reply_tool_check", "message_reply", reply_id, verdict)

    text = _clean(row.get("final_text") or row.get("draft_text") or "").strip()
    cid = row.get("conversation_id") or ""
    mid = row.get("message_id") or ""
    if not text or not mid:
        raise ValueError("定稿内容或消息标识缺失，无法发送")

    ctype = str(row.get("conversation_type") or "group").lower()
    allow_live = bool(settings.reply_send_enabled and live)
    if allow_live:
        # 安全闸门：本人已在控制台逐条确认（status=approved）+ 请求级 confirm=true。
        # 历史上的「先预览再发送」与前端弹窗确认均已按需求移除。
        # 原子抢占：并发重复点「发送」时只有一个请求能拿到发送权
        claimed = db.fetch_one(
            "WITH c AS (UPDATE workflow.message_replies "
            " SET send_mode = 'live_pending' "
            " WHERE id = CAST(:id AS uuid) AND status = 'approved' "
            "   AND (send_mode IS NULL OR send_mode NOT IN ('live','live_pending')) "
            " RETURNING 1) "
            "SELECT count(*) AS n FROM c", {"id": reply_id},
        )
        if not (claimed and claimed["n"]):
            raise ValueError("该草稿正在发送中或已发送，请勿重复提交")

    # dws chat +messages-reply：单聊推荐省略 --group，由 --message-id 定位原消息；
    # 群聊则显式带会话 ID。此前单聊也强制 --group，可能无法通过会话归属校验。
    args = ["chat", "+messages-reply"]
    if ctype != "direct":
        if not cid:
            raise ValueError("群聊草稿缺少会话标识，无法发送")
        args += ["--group", cid]
    args += ["--message-id", mid, "--content", text]
    if allow_live:
        # CLI 要求用户明确确认后才可用 -y；此处 confirm=True 即该确认。
        args += ["--idempotency-key",
                 hashlib.sha256(reply_id.encode()).hexdigest()[:32],
                 "--ai-tag", "true" if settings.reply_ai_tag else "false",
                 "-y"]
    else:
        args += ["--dry-run"]
    args += ["-f", "json"]

    audit.log_access(actor, "reply_send_attempt", "message_reply", reply_id,
                     {"mode": "live" if allow_live else "dry_run",
                      "conversation_id": cid, "message_id": mid,
                      "text_length": len(text)})
    try:
        result = _run(args, 90)
    except Exception as e:  # noqa: BLE001
        err = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        if allow_live:
            # 失败后恢复为可重试状态，不把草稿锁死在 failed
            db.execute(
                "UPDATE workflow.message_replies SET status = 'approved', "
                "send_mode = NULL, send_result = CAST(:r AS jsonb) WHERE id = :id",
                {"r": json.dumps(err, ensure_ascii=False), "id": reply_id},
            )
        raise RuntimeError(f"DWS 发送调用失败：{err['error']}")

    if not allow_live:
        will_send = bool(result.get("willSend"))
        preview = {
            "will_send": will_send,
            "dry_run": bool(result.get("dryRun", True)),
            "transport": result.get("transport"),
            "referenced_message": result.get("referencedMessage"),
            "text": text,
        }
        db.execute(
            "UPDATE workflow.message_replies SET send_mode = 'dry_run', "
            "send_result = CAST(:r AS jsonb) WHERE id = :id",
            {"r": json.dumps(preview, ensure_ascii=False, default=str), "id": reply_id},
        )
        audit.log_access(actor, "reply_dry_run", "message_reply", reply_id, preview)
        return {"id": reply_id, "mode": "dry_run", "will_send": will_send,
                "status": "approved", "preview": preview}

    db.execute(
        "UPDATE workflow.message_replies SET status = 'sent', send_mode = 'live', "
        "send_result = CAST(:r AS jsonb), sent_at = NOW() WHERE id = :id",
        {"r": json.dumps(result, ensure_ascii=False, default=str), "id": reply_id},
    )
    audit.log_access(actor, "reply_sent", "message_reply", reply_id,
                     {"message_id": result.get("messageId"),
                      "delivery_status": result.get("deliveryStatus")})
    return {"id": reply_id, "mode": "live", "status": "sent",
            "message_id": result.get("messageId"), "result": result}
