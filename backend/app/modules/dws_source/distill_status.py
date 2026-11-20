"""DWS 列表条目的蒸馏状态索引。

用途：前端「我的钉钉数据」列表把已完成蒸馏的条目按钮改成置灰的「已蒸馏」，
并禁止再次提交；进行中的条目显示「蒸馏中…」同样置灰。

判定口径：
- COMPLETED                      -> completed（已蒸馏）
- PENDING ~ PUBLISHING / RETRY_WAIT -> active（蒸馏中）
- FAILED / NEEDS_REVIEW / REVIEW_DISMISSED / CANCELLED -> none（允许重新蒸馏；
  NEEDS_REVIEW 请优先在「蒸馏任务状态 → 复核」里处置，重蒸只是其中一种动作）

匹配键（按数据源种类隔离，避免跨源误判）：
- external_ref：DWS 节点的 node_id（钉钉文档/会话/日程/会议 ID）
- origin_url：钉钉原文链接
- 规范化标题：兼容早期导入、metadata 里没有 external_ref 的文档

同一条目有多个键命中时取「已完成 > 进行中」；本地上传等非 DWS 来源不参与匹配。
"""
import re
import time

from app.db import session as db

DWS_KINDS = {"document", "chat", "calendar", "minutes"}

# 索引 TTL 缓存：蒸馏状态变化频率低（秒级），列表每 5 秒轮询一次，
# 全量 JOIN 扫描没必要每次都做；蒸馏提交/删除后会主动失效。
_INDEX_TTL = 5.0
_index_cache: dict = {"ts": 0.0, "index": None}


def invalidate_index() -> None:
    """蒸馏提交 / 任务删除后调用，立即失效索引缓存。"""
    _index_cache["ts"] = 0.0
    _index_cache["index"] = None

_ACTIVE_STATUSES = {
    "PENDING", "INGESTING", "PARSING", "CHUNKING", "EXTRACTING", "VALIDATING",
    "ENTITY_RESOLUTION", "KNOWLEDGE_REVIEW", "PUBLISHING", "RETRY_WAIT",
}
_DONE_STATUSES = {"COMPLETED"}
_RANK = {"none": 0, "active": 1, "completed": 2}

_EXT_RE = re.compile(
    r"\.(adoc|axls|docx?|xlsx?|pptx?|pdf|txt|md|csv|html?|json)$", re.I
)
_WS_RE = re.compile(r"\s+")


def norm_title(title) -> str:
    """标题归一化：小写、去常见扩展名、去所有空白。"""
    s = _EXT_RE.sub("", str(title or "").strip().lower())
    return _WS_RE.sub("", s)


def _key(kind: str, field: str, value: str) -> str:
    return f"{kind}|{field}:{value}"


def item_keys(kind: str, item: dict) -> list[str]:
    """由前端传来的精简条目生成候选匹配键。"""
    item = item or {}
    kind = kind if kind in DWS_KINDS else "document"
    keys: list[str] = []
    ref = str(item.get("node_id") or "").strip()
    if ref:
        keys.append(_key(kind, "ref", ref))
    url = str(item.get("doc_url") or "").strip()
    if url:
        keys.append(_key(kind, "url", url))
    title = norm_title(item.get("name"))
    if title:
        keys.append(_key(kind, "title", title))
    return keys


def _build_index() -> dict[str, str]:
    """扫描「文档 × 蒸馏任务」，生成 key -> 状态 的索引。"""
    rows = db.fetch_all(
        "SELECT d.title, d.metadata, r.status "
        "FROM source.documents d "
        "JOIN ingest.pipeline_runs r ON r.document_id = d.id"
    )
    index: dict[str, str] = {}
    for row in rows:
        status = str(row.get("status") or "").upper()
        if status in _DONE_STATUSES:
            state = "completed"
        elif status in _ACTIVE_STATUSES:
            state = "active"
        else:
            continue  # 失败 / 待人工复核：允许重试
        meta = row.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        raw_kind = str(meta.get("source_kind") or "").strip().lower()
        if raw_kind and raw_kind not in DWS_KINDS:
            continue  # 本地上传等来源不映射到 DWS 条目
        kind = raw_kind or "document"  # 早期无来源元数据的文档按钉钉文档兜底
        keys = []
        ref = str(meta.get("external_ref") or "").strip()
        if ref:
            keys.append(_key(kind, "ref", ref))
        url = str(meta.get("origin_url") or "").strip()
        if url:
            keys.append(_key(kind, "url", url))
        title = norm_title(row.get("title"))
        if title:
            keys.append(_key(kind, "title", title))
        for key in keys:
            if _RANK[state] > _RANK.get(index.get(key, "none"), 0):
                index[key] = state
    return index


def states_for(kind: str, items: list) -> dict:
    """返回与 items 一一对应的状态数组，供前端渲染操作列。"""
    kind = kind if kind in DWS_KINDS else "document"
    now = time.monotonic()
    if _index_cache["index"] is None or now - _index_cache["ts"] > _INDEX_TTL:
        _index_cache["index"] = _build_index()
        _index_cache["ts"] = now
    index = _index_cache["index"]
    states: list[str] = []
    for item in items or []:
        state = "none"
        for key in item_keys(kind, item if isinstance(item, dict) else {}):
            candidate = index.get(key, "none")
            if _RANK[candidate] > _RANK[state]:
                state = candidate
        states.append(state)
    return {
        "kind": kind,
        "states": states,
        "counts": {
            "total": len(states),
            "completed": states.count("completed"),
            "active": states.count("active"),
        },
    }
