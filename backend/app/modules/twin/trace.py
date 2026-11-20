"""链路追踪：把一次请求在各节点的生效状态记录成 trace。

设计目标：页面上能把「数据 → 知识 → 本体语义 → 生成」这条链逐步点亮，
每个节点给出 状态 / 说明 / 耗时，而不是只看到一个最终答案。

状态词表：
  ok        该节点正常生效（有产出、有命中）
  empty     正常执行但没有命中（不是错误，如实呈现）
  skipped   条件跳过（如首轮证据已足够，未触发补充检索）
  degraded  降级运行（LLM 不可用等，仍给出兜底产出）
  warn      通过但带提醒（如引用编号被剔除）
  blocked   被护栏拦下（证据不足拒答 / 必须人工审批）
  pending   等待人工确认（回复草稿一律经本人确认后外发）
"""
from __future__ import annotations

import contextvars
import time

# 起点时间不放 state：LangGraph 按 TypedDict 过滤未知 key，
# 放进 state 会在节点里被丢掉，导致每个节点的耗时都是 None。
_T0: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "trace_t0", default=None)


def start(state: dict) -> None:
    state["trace"] = []
    _T0.set(time.perf_counter())


def mark(state: dict, node: str, status: str, detail: str = "",
         **data) -> None:
    """在节点末尾调用。node 用中文名，直接用于前端展示。"""
    t0 = _T0.get()
    ms = round((time.perf_counter() - t0) * 1000) if t0 is not None else None
    entry: dict = {"node": node, "status": status, "detail": detail, "ms": ms}
    if data:
        entry.update(data)
    state.setdefault("trace", []).append(entry)
