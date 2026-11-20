"""CQ 驱动的企业本体建设与持续演进 V2.0（增量模块，不改动 V1.2 cq.py）。

零外部依赖即可运行核心逻辑；默认 MemoryStore，设 CQ_V2_STORE=postgres
切换到真实持久化。API / CLI / 测试统一通过 get_engine() 获取门面。
"""
from __future__ import annotations

from . import (capability, decision, engine, evolution, mapping, models,
               planner, quality, registry, service, store)
from .service import CQEngine, demo_snapshot

_engine: CQEngine | None = None


def get_engine() -> CQEngine:
    global _engine
    if _engine is None:
        _engine = CQEngine()
    return _engine


def reset_engine() -> CQEngine:
    global _engine
    _engine = CQEngine()
    return _engine


__all__ = [
    "CQEngine", "get_engine", "reset_engine", "demo_snapshot",
    "models", "quality", "capability", "registry", "mapping", "planner",
    "engine", "decision", "evolution", "service", "store",
]
