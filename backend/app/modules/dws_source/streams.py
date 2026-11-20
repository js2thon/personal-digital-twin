"""DWS 多源接入：钉钉文档 / 会话消息 / 日历日程 / 会议听记。

统一契约（前端与蒸馏入口共用）：
    list_items(kind, ...)   -> {"kind", "items", "total", "count", "auth", ...}
    fetch_text(kind, item)  -> {"text", "engine", "warning"}

每个 item 都带 node_id / name / extension / doc_url / update_time /
readable / engine / source_kind / summary / raw，前端一张表即可渲染四种数据源。
所有命令均为只读（effect=read），不会发送、修改或删除钉钉侧数据。
"""
import json
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from . import client as dws
from .client import DwsError, _is_authenticated, _parser_engines, _run

KINDS = {
    "document": "钉钉文档",
    "chat": "企业 IM消息",
    "calendar": "日历日程",
    "minutes": "会议听记",
}

_ENGINE = {
    "document": "DWS 文档读取 + 解析适配层",
    "chat": "dws chat +chat-messages / +at-me",
    "calendar": "dws calendar +agenda",
    "minutes": "dws minutes +detail（摘要/关键词/行动项）",
}


def _clean(s) -> str:
    from app.modules.document_parser.adapters import clean_text

    return clean_text(str(s or ""))


def _fmt_time(v) -> str:
    """把毫秒时间戳 / ISO 字符串 / 日历 {dateTime,date} 统一成可读时间。"""
    if v is None or v == "":
        return ""
    if isinstance(v, dict):
        v = v.get("dateTime") or v.get("date") or v.get("timestamp") or ""
    s = str(v).strip()
    if s.isdigit():
        try:
            ts = int(s)
            if ts > 10_000_000_000:  # 毫秒
                ts /= 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError):
            return s
    return s.replace("T", " ")[:19]


def _base_item(kind: str, node_id: str, name: str, **kw) -> dict:
    item = {
        "node_id": node_id,
        "name": _clean(name) or "(未命名)",
        "extension": kind,
        "doc_url": kw.get("doc_url") or "",
        "update_time": kw.get("update_time") or "",
        "readable": True,
        "engine": _ENGINE[kind],
        "source_kind": kind,
        "source_label": KINDS[kind],
        "summary": kw.get("summary") or "",
        "raw": kw.get("raw") or {},
    }
    return item


# ---------------------------------------------------------------- 列表

def _document_items(query: str, limit: int, all_pages: bool) -> dict:
    r = dws.list_documents(query or " ", limit, None, all_pages, 10)
    items = []
    for x in r.get("items") or []:
        i = _base_item("document", x.get("node_id") or "", x.get("name") or "",
                       doc_url=x.get("doc_url"), update_time=x.get("update_time"),
                       summary=x.get("engine"), raw=x)
        i["extension"] = x.get("extension") or "document"
        i["readable"] = bool(x.get("readable", True))
        i["engine"] = x.get("engine") or _ENGINE["document"]
        items.append(i)
    return {"items": items, "total": r.get("total", len(items)),
            "count": len(items), "truncated": r.get("truncated", False),
            "pages_fetched": r.get("pages_fetched"),
            "auth": r.get("auth"), "engines": r.get("engines")}


def _chat_items(days: int) -> dict:
    """会话列表默认包含群聊 + 单聊。

    dws chat +chat-list 未传 --types 时默认只返回群聊，实测会漏掉 56 个单聊会话；
    这里显式传 group,p2p 并标注会话类型，供蒸馏列表与消息中心共用。
    """
    items = [
        _base_item("chat", "atme:recent", f"最近 {days} 天 @我 的消息",
                   summary="汇总群聊与单聊中 @我 的消息", raw={"atme": True, "days": days})
    ]
    items[0]["conversation_type"] = "mixed"
    data = _run(["chat", "+chat-list", "--types", "group,p2p", "--page-size", "100",
                 "--page-all", "--page-limit", "10", "-f", "json"], 150)
    chats = data.get("chats") or []
    n_direct = 0
    for c in chats:
        cid = c.get("openConversationId")
        if not cid:
            continue
        ctype = str(c.get("conversationType") or "group").lower()
        direct = ctype in ("direct", "p2p", "single", "private", "1")
        if direct:
            n_direct += 1
        name = c.get("name") or "(未命名会话)"
        item = _base_item(
            "chat", cid, name,
            update_time=_fmt_time(c.get("lastMsgCreateAt")),
            summary=("单聊" if direct else "群聊") + f" · {c.get('memberCount') or '-'} 人",
            raw=c)
        item["conversation_type"] = "direct" if direct else "group"
        items.append(item)
    return {"items": items, "total": len(items), "count": len(items),
            "truncated": False, "direct_count": n_direct,
            "group_count": len(chats) - n_direct}


def _calendar_items(days: int) -> dict:
    now = datetime.now().astimezone()
    start = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
    end = (now + timedelta(days=days)).replace(microsecond=0).isoformat()
    data = _run(["calendar", "+agenda", "--start", start, "--end", end,
                 "--limit", "100", "-f", "json"], 90)
    events = (data.get("data") or {}).get("events") or []
    items = []
    for e in events:
        eid = e.get("eventId") or ""
        title = e.get("summary") or e.get("title") or "(无标题日程)"
        if isinstance(title, dict):
            title = title.get("title") or title.get("name") or "(无标题日程)"
        items.append(_base_item(
            "calendar", eid, title,
            update_time=_fmt_time(e.get("start")),
            summary=f"{_fmt_time(e.get('start'))} ~ {_fmt_time(e.get('end'))}"
                    + (f" · {e.get('location')}" if e.get("location") else "")
                    + (f" · {e.get('status')}" if e.get("status") else ""),
            raw=e))
    return {"items": items, "total": len(items), "count": len(items),
            "truncated": False, "window": f"{start} ~ {end}"}


def _minutes_items(query: str, limit: int) -> dict:
    if query and query.strip() and query.strip() != " ":
        args = ["minutes", "+search", "--query", query.strip(), "--scope", "all",
                "--limit", str(min(max(limit, 1), 50)), "--page-all", "-f", "json"]
    else:
        args = ["minutes", "+list-all", "--limit", str(min(max(limit, 1), 50)),
                "--page-all", "-f", "json"]
    data = _run(args, 150)
    mins = (data.get("data") or {}).get("minutes") or []
    items = []
    for m in mins:
        uid = m.get("taskUuid") or ""
        if not uid:
            continue
        items.append(_base_item(
            "minutes", uid, m.get("title") or "(无标题听记)",
            doc_url=m.get("url"), update_time=_fmt_time(m.get("startTime")),
            summary=f"{_fmt_time(m.get('startTime'))} · {m.get('orgName') or ''}",
            raw=m))
    return {"items": items, "total": len(items), "count": len(items),
            "truncated": not (data.get("data") or {}).get("complete", True)}


# 列表结果 TTL 缓存：全量钉钉文档抓取要 10–30 秒，频繁切换数据源不应反复重抓。
# 只读缓存；写入/蒸馏状态由独立的 distill-status 接口实时查库，不受缓存影响。
_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_CACHE_TTL = 120.0
_CACHE_MAX_ENTRIES = 128  # L10：query/days 组合无限多，必须有容量上限


def _cache_put(key: tuple, now: float, result: dict) -> None:
    _CACHE.pop(key, None)
    _CACHE[key] = (now, result)
    for stale, (ts, _) in list(_CACHE.items()):
        if now - ts > _CACHE_TTL:
            _CACHE.pop(stale, None)
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def list_items(kind: str = "document", query: str = "", days: int = 7,
               limit: int = 30, all_pages: bool = True,
               refresh: bool = False) -> dict:
    """列出指定数据源的可蒸馏条目；失败时抛 DwsError 由 API 层转 502。"""
    if kind not in KINDS:
        raise DwsError(f"不支持的数据源类型：{kind}")
    key = (kind, query, days, limit, all_pages)
    now = time.time()
    if not refresh:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return {**hit[1], "cached": True}
    if kind == "document":
        out = _document_items(query, limit, all_pages)
    elif kind == "chat":
        out = _chat_items(max(1, min(days, 365)))
    elif kind == "calendar":
        out = _calendar_items(max(1, min(days, 90)))
    else:
        out = _minutes_items(query, limit)
    result = {"kind": kind, "label": KINDS[kind], "engine": _ENGINE[kind],
              "auth": out.get("auth") or _is_authenticated(),
              "engines": out.get("engines") or _parser_engines(),
              "cached": False,
              **{k: v for k, v in out.items() if k not in ("auth", "engines")}}
    _cache_put(key, now, result)
    return result


# ---------------------------------------------------------------- 正文

def _messages_to_text(title: str, msgs: list) -> str:
    lines = [f"# {title}", "", f"共 {len(msgs)} 条消息。", ""]
    for m in msgs:
        t = _fmt_time(m.get("createTime") or m.get("time"))
        sender = m.get("sender") or "未知发送人"
        text = _clean(m.get("text") or "")
        conv = m.get("conversation") or ""
        if isinstance(conv, dict):
            conv = conv.get("name") or ""
        if not text.strip():
            continue
        lines.append(f"- [{t}] {sender}" + (f"（{conv}）" if conv else "") + f"：{text}")
    return "\n".join(lines)


def _chat_text(item: dict) -> str:
    raw = item.get("raw") or {}
    if raw.get("atme") or str(item.get("node_id", "")).startswith("atme:"):
        days = int(raw.get("days") or 7)
        d = _run(["chat", "+at-me", "--days", str(days), "--limit", "50",
                  "--page-all", "--page-limit", "20", "-f", "json"], 120)
        msgs = d.get("messages") or d.get("items") or []
        return _messages_to_text(item.get("name") or "@我 的消息", msgs)
    cid = item.get("node_id")
    d = _run(["chat", "+chat-messages", "--group", str(cid), "--limit", "100",
              "--page-all", "--page-limit", "10", "--max-items", "200",
              "-f", "json"], 150)
    msgs = d.get("messages") or []
    return _messages_to_text(f"会话：{item.get('name')}", msgs)


def _calendar_text(item: dict) -> str:
    e = item.get("raw") or {}
    lines = [
        f"# 日程：{e.get('summary') or e.get('title') or item.get('name')}", "",
        f"- 开始：{_fmt_time(e.get('start'))}",
        f"- 结束：{_fmt_time(e.get('end'))}",
        f"- 地点：{e.get('location') or '未指定'}",
        f"- 状态：{e.get('status') or '未指定'}",
        f"- 日程 ID：{e.get('eventId') or ''}", "",
        "## 说明", "",
        "本条来自钉钉日历，可作为时间线事件、项目节点与参与关系的证据。",
    ]
    # 尝试补充参会人（失败不影响主流程）
    eid = e.get("eventId")
    if eid:
        try:
            d = _run(["calendar", "+attendee-list", "--event", str(eid),
                      "-f", "json"], 45)
            atts = ((d.get("data") or {}).get("attendees")
                    or (d.get("data") or {}).get("items")
                    or d.get("attendees") or [])
            names = [str(a.get("name") or a.get("displayName") or a.get("userId") or "")
                     for a in atts if isinstance(a, dict)]
            names = [n for n in names if n]
            if names:
                lines += ["", "## 参会人", "", "、".join(names[:50])]
        except DwsError:
            pass
    return "\n".join(lines)


def _minutes_text(item: dict) -> str:
    uid = item.get("node_id")
    d = _run(["minutes", "+detail", "--id", str(uid), "--artifacts",
              "basic,summary,keywords,todos", "-f", "json"], 180)
    basic = ((d.get("basic") or {}).get("result") or {})
    summ = ((d.get("summary") or {}).get("result") or {})
    kws = ((d.get("keywords") or {}).get("result") or {}).get("keywords") or []
    todos = ((d.get("todos") or {}).get("result") or {})
    todo_items = todos.get("items") or todos.get("actions") or []

    lines = [f"# 会议纪要：{basic.get('title') or item.get('name')}", "",
             f"- 开始：{_fmt_time(basic.get('startTime') or item.get('update_time'))}",
             f"- 结束：{_fmt_time(basic.get('endTime'))}",
             f"- 时长：{round(int(basic.get('duration') or 0) / 60000)} 分钟"
             if str(basic.get("duration") or "").isdigit() else "",
             f"- 原文：{basic.get('url') or item.get('doc_url') or ''}", ""]
    if kws:
        lines += ["## 关键词", "", "、".join(_clean(k) for k in kws if k), ""]
    full = _clean(summ.get("fullSummary") or summ.get("summary") or "")
    if full:
        lines += ["## 会议摘要", "", full, ""]
    if todo_items:
        lines += ["## 行动项", ""]
        for t in todo_items[:50]:
            if isinstance(t, dict):
                desc = t.get("content") or t.get("text") or t.get("title") or json.dumps(t, ensure_ascii=False)
                owner = t.get("owner") or t.get("assignee") or t.get("executor") or ""
                lines.append(f"- {_clean(desc)}" + (f"（负责人：{_clean(owner)}）" if owner else ""))
            else:
                lines.append(f"- {_clean(t)}")
    return "\n".join(x for x in lines if x is not None)


def fetch_text(kind: str, item: dict) -> dict:
    """读取某条目正文，统一返回 {text, engine, warning}。"""
    item = item or {}
    if kind not in KINDS:
        raise DwsError(f"不支持的数据源类型：{kind}")
    if kind == "document":
        return dws.fetch_document_text(
            str(item.get("node_id") or ""),
            str(item.get("extension") or "adoc"),
            str(item.get("name") or "钉钉文档"),
        )
    try:
        if kind == "chat":
            text = _chat_text(item)
        elif kind == "calendar":
            text = _calendar_text(item)
        else:
            text = _minutes_text(item)
    except DwsError:
        raise
    except Exception as e:  # noqa: BLE001
        raise DwsError(f"{KINDS[kind]}正文读取失败：{type(e).__name__}: {e}")
    text = _clean(text)
    if len(text.strip()) < 20:
        raise DwsError(f"{KINDS[kind]}未读取到有效正文（可能无权限或内容为空）")
    return {"text": text, "engine": _ENGINE[kind], "warning": None}
