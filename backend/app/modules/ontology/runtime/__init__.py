"""本体运行时：把建模草稿编译为可执行 IR 并运行（纯函数内核，零 I/O）。

- expr       受限表达式求值（AST 白名单，禁 eval/exec）
- metrics    19 种标准指标算法 + 2 口径补充
- compiler   spec → 可执行 IR + 体检结论（唯一可执行性口径来源）
- engine     执行 IR：指标值 / 派生回写 / 行动触发 / 约束违规 / 不可执行项如实披露
"""
from . import expr, metrics, compiler, engine

__all__ = ["expr", "metrics", "compiler", "engine"]
