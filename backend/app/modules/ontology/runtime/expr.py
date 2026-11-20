"""受限表达式求值器：只放行纯算术 / 比较 / 布尔 / 白名单函数，禁用 eval 与 exec。

为什么要自己写：草稿里的约束 expression 与函数 logic 都是用户输入，直接 eval 等于
把任意 Python（``__import__('os').system(...)``）交给画布输入框。本模块用 ast 白名单
求值，语法树里出现任何未放行的节点类型立即拒绝。

放行：Constant / Name / BinOp(+ - * / // % **) / UnaryOp(+ - not) / BoolOp(and or)
      / Compare(== != < <= > >= in "not in" is "is not"，支持链式) / IfExp
      / List / Tuple / Dict / Set / Subscript / Call(仅白名单函数)
禁止：Attribute（含 ``a.b``）/ Lambda / 推导式 / f-string / 海象 / 星号展开 / await
      / yield / 切片外的复杂下标运算链 / 任何 dunder 名字

三条防滥用上限（构造性拒绝服务）：表达式长度、AST 节点数、幂次右操作数、
正则 pattern 长度；任一超限即 ExprError，不做「尽力求值」。

口径（不臆造）：
1. 字符串 vs 数字比较时才做类型对齐（业务数据里 ``level`` 常是 ``"3"``）；
   两侧同为字符串一律按字符串比较，避免 ``"03" == "3"`` 被误判为真。
2. 引用了 scope 里不存在的名字 → 抛 ``EXPR_UNKNOWN_NAME``，由调用方决定是
   「缺属性」还是「表达式不完整」，绝不静默补 None。
3. 除以 0 / 类型不兼容等运行时错误统一包成 ``EXPR_RUNTIME``，附带可读原因。
"""
from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any, Iterable

# ---------------------------------------------------------------- 上限
MAX_LEN = 2000          # 表达式字符数
MAX_NODES = 400         # AST 节点数
MAX_DEPTH = 40          # 递归深度
MAX_POW = 12            # 幂次右操作数上限（挡住 10**100000）
MAX_PATTERN = 200       # matches() 的正则长度
MAX_PATTERN_INPUT = 500  # matches() 的被测字符串长度


class ExprError(ValueError):
    """表达式不可解析 / 不可求值。code 用于调用方分支，不用于前端展示。"""

    def __init__(self, message: str, *, code: str = "EXPR_ERROR", name: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.name = name

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "name": self.name}


# ---------------------------------------------------------------- 白名单函数
def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _lower(v: Any) -> Any:
    return None if v is None else _s(v).lower()


def _upper(v: Any) -> Any:
    return None if v is None else _s(v).upper()


def _strip(v: Any) -> Any:
    return None if v is None else _s(v).strip()


def _contains(hay: Any, needle: Any) -> bool:
    if hay is None:
        return False
    if isinstance(hay, (list, tuple, set, dict)):
        return needle in hay
    return _s(needle) in _s(hay)


def _startswith(v: Any, p: Any) -> bool:
    return False if v is None else _s(v).startswith(_s(p))


def _endswith(v: Any, p: Any) -> bool:
    return False if v is None else _s(v).endswith(_s(p))


def _matches(v: Any, pattern: Any) -> bool:
    if v is None:
        return False
    p = _s(pattern)
    if not p:
        raise ExprError("matches() 的 pattern 为空", code="EXPR_BAD_ARG")
    if len(p) > MAX_PATTERN:
        raise ExprError(f"matches() 的 pattern 超过 {MAX_PATTERN} 字符上限",
                        code="EXPR_TOO_LONG")
    text = _s(v)
    if len(text) > MAX_PATTERN_INPUT:
        text = text[:MAX_PATTERN_INPUT]
    try:
        return re.search(p, text) is not None
    except re.error as exc:
        raise ExprError(f"matches() 的正则非法：{exc}", code="EXPR_BAD_REGEX") from None


def _coalesce(*args: Any) -> Any:
    for a in args:
        if a is not None and not (isinstance(a, str) and a.strip() == ""):
            return a
    return None


def _isnull(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _notnull(v: Any) -> bool:
    return not _isnull(v)


def _num(v: Any) -> float | int | None:
    """把值当数字解析；bool 不算数字（避免 True 被当 1 求和）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if re.fullmatch(r"[+-]?\d+", s):
            try:
                return int(s)
            except ValueError:
                return None
        try:
            f = float(s)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _num_or(v: Any, default: Any = 0) -> Any:
    n = _num(v)
    return default if n is None else n


def _rounded(v: Any, digits: Any = 0) -> Any:
    n = _num(v)
    if n is None:
        return None
    try:
        return round(n, int(_num(digits) or 0))
    except (TypeError, ValueError):
        return round(n)


def _safe_div(a: Any, b: Any) -> Any:
    na, nb = _num(a), _num(b)
    if na is None or nb is None or nb == 0:
        return None
    return na / nb


_ALLOWED_FUNCS: dict[str, Any] = {
    "len": len,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "round": _rounded,
    "int": lambda v: int(_num(v) or 0),
    "float": lambda v: float(_num(v) or 0.0),
    "str": lambda v: "" if v is None else str(v),
    "bool": lambda v: bool(v),
    "lower": _lower,
    "upper": _upper,
    "strip": _strip,
    "contains": _contains,
    "startswith": _startswith,
    "endswith": _endswith,
    "matches": _matches,
    "coalesce": _coalesce,
    "isnull": _isnull,
    "notnull": _notnull,
    "number": _num,
    "div": _safe_div,
}

def _safe_plain_div(a: Any, b: Any) -> Any:
    """普通除法：保留 Python 语义，但 0 除给可读错误。"""
    if b == 0:
        raise ExprError("表达式里出现除以 0", code="EXPR_DIV_ZERO")
    return a / b


_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: _safe_plain_div, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}

_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.List, ast.Tuple, ast.Dict, ast.Set, ast.Subscript,
    ast.Call, ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
    ast.In, ast.NotIn,
)


def _validate(node: ast.AST, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ExprError(f"表达式嵌套超过 {MAX_DEPTH} 层上限", code="EXPR_TOO_DEEP")
    if not isinstance(node, _ALLOWED_NODES):
        raise ExprError(f"表达式包含不允许的语法：{type(node).__name__}",
                        code="EXPR_FORBIDDEN")
    if isinstance(node, ast.Name):
        if node.id.startswith("__"):
            raise ExprError("表达式不允许使用 __dunder__ 名字", code="EXPR_FORBIDDEN")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, (int, float)):
            if abs(right.value) > MAX_POW:
                raise ExprError(f"幂次超过 {MAX_POW} 上限", code="EXPR_TOO_LARGE")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExprError("只能调用白名单函数，不允许方法/属性调用",
                            code="EXPR_FORBIDDEN")
        if node.func.id.startswith("__"):
            raise ExprError("表达式不允许调用 __dunder__ 名字", code="EXPR_FORBIDDEN")
        if node.func.id not in _ALLOWED_FUNCS:
            raise ExprError(f"函数 {node.func.id}() 不在白名单内", code="EXPR_UNKNOWN_FUNC")
        if node.keywords:
            raise ExprError("白名单函数不支持关键字参数", code="EXPR_FORBIDDEN")
    for child in ast.iter_child_nodes(node):
        _validate(child, depth + 1)


def parse(text: str) -> ast.Expression:
    """解析并做白名单校验；失败抛 ExprError。"""
    s = str(text or "").strip()
    if not s:
        raise ExprError("表达式为空", code="EXPR_EMPTY")
    if len(s) > MAX_LEN:
        raise ExprError(f"表达式超过 {MAX_LEN} 字符上限", code="EXPR_TOO_LONG")
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"表达式语法错误：{exc.msg}（第 {exc.offset} 字符）",
                        code="EXPR_SYNTAX") from None
    count = sum(1 for _ in ast.walk(tree))
    if count > MAX_NODES:
        raise ExprError(f"表达式节点数 {count} 超过 {MAX_NODES} 上限", code="EXPR_TOO_LARGE")
    _validate(tree.body)
    return tree


def references(text: str) -> list[str]:
    """列出表达式引用的自由变量名（不含白名单函数名、不含字面量）。"""
    tree = parse(text)
    out: list[str] = []
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in _ALLOWED_FUNCS or node.id in seen:
                continue
            seen.add(node.id)
            out.append(node.id)
    return sorted(out)


# ---------------------------------------------------------------- 求值
def _cmp_pair(a: Any, op: ast.AST, b: Any) -> bool:
    """比较时只在「字符串 vs 数字」这一种边界上做类型对齐。"""
    if isinstance(a, str) and not isinstance(b, str) and not isinstance(b, bool):
        na = _num(a)
        if na is None:
            raise ExprError(f"无法把 {a!r} 当作数字与 {b!r} 比较", code="EXPR_TYPE")
        a = na
    elif isinstance(b, str) and not isinstance(a, str) and not isinstance(a, bool):
        nb = _num(b)
        if nb is None:
            raise ExprError(f"无法把 {b!r} 当作数字与 {a!r} 比较", code="EXPR_TYPE")
        b = nb
    fn = _CMP_OPS[type(op)]
    try:
        return bool(fn(a, b))
    except TypeError as exc:
        raise ExprError(f"比较类型不兼容：{type(a).__name__} 与 {type(b).__name__}（{exc}）",
                        code="EXPR_TYPE") from None


def _eval(node: ast.AST, scope: dict[str, Any], depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        raise ExprError(f"表达式嵌套超过 {MAX_DEPTH} 层上限", code="EXPR_TOO_DEEP")

    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        raise ExprError(f"表达式引用了不存在的名字：{node.id}",
                        code="EXPR_UNKNOWN_NAME", name=node.id)
    if isinstance(node, ast.List):
        return [_eval(e, scope, depth + 1) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, scope, depth + 1) for e in node.elts)
    if isinstance(node, ast.Set):
        return {_eval(e, scope, depth + 1) for e in node.elts}
    if isinstance(node, ast.Dict):
        return {_eval(k, scope, depth + 1): _eval(v, scope, depth + 1)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, scope, depth + 1)
        right = _eval(node.right, scope, depth + 1)
        if isinstance(node.op, ast.Pow):
            r = _num(right)
            if r is not None and abs(r) > MAX_POW:
                raise ExprError(f"幂次超过 {MAX_POW} 上限", code="EXPR_TOO_LARGE")
            return left ** right
        fn = _BIN_OPS[type(node.op)]
        try:
            return fn(left, right)
        except ExprError:
            raise
        except TypeError as exc:
            raise ExprError(f"运算符类型不兼容：{type(left).__name__} 与 "
                            f"{type(right).__name__}（{exc}）", code="EXPR_TYPE") from None
    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, scope, depth + 1)
        fn = _UNARY_OPS[type(node.op)]
        try:
            return fn(val)
        except TypeError as exc:
            raise ExprError(f"一元运算类型不兼容：{type(val).__name__}",
                            code="EXPR_TYPE") from None
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            out: Any = True
            for e in node.values:
                out = _eval(e, scope, depth + 1)
                if not bool(out):
                    return out
            return out
        out = False
        for e in node.values:
            out = _eval(e, scope, depth + 1)
            if bool(out):
                return out
        return out
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope, depth + 1)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, scope, depth + 1)
            if not _cmp_pair(left, op, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return (_eval(node.body, scope, depth + 1) if bool(_eval(node.test, scope, depth + 1))
                else _eval(node.orelse, scope, depth + 1))
    if isinstance(node, ast.Subscript):
        target = _eval(node.value, scope, depth + 1)
        key = _eval(node.slice, scope, depth + 1)
        try:
            return target[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExprError(f"下标取值失败：{exc}", code="EXPR_RUNTIME") from None
    if isinstance(node, ast.Call):
        fn = _ALLOWED_FUNCS[node.func.id]  # type: ignore[union-attr]
        args = [_eval(a, scope, depth + 1) for a in node.args]
        try:
            return fn(*args)
        except ExprError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExprError(f"{node.func.id}() 参数不合法：{exc}",
                            code="EXPR_BAD_ARG") from None
    raise ExprError(f"表达式包含不允许的语法：{type(node).__name__}", code="EXPR_FORBIDDEN")


def evaluate(text_or_tree: str | ast.Expression, scope: dict[str, Any] | None = None) -> Any:
    """求值；text_or_tree 可以是字符串或已解析的 AST（便于批量复用解析结果）。"""
    tree = text_or_tree if isinstance(text_or_tree, ast.Expression) else parse(text_or_tree)
    return _eval(tree.body, dict(scope or {}))


def try_bool(text_or_tree: str | ast.Expression, scope: dict[str, Any] | None = None) -> bool:
    """按布尔口径求值。字符串 "false"/"0"/""/"no" 视为假（业务数据里常见）。"""
    v = evaluate(text_or_tree, scope)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "n", "否")
    if isinstance(v, (list, dict, set, tuple)):
        return bool(v)
    return bool(v)


def coercion_note() -> str:
    """给「能力自述」用的口径说明（MCP / 文档共用，避免两处漂移）。"""
    return ("表达式求值走 ast 白名单（无 eval）：支持 + - * / // % **、比较（含链式）、"
            "and/or/not、if-else、列表/元组/字典/集合、下标，以及白名单函数 "
            + "、".join(sorted(_ALLOWED_FUNCS)) + "。不支持属性访问 a.b、lambda、推导式。")


def allowed_functions() -> list[str]:
    return sorted(_ALLOWED_FUNCS)


def self_check() -> dict[str, Any]:
    """纯函数自检：正例求值 + 反例拒绝（eval 逃逸必须全部被挡住）。"""
    cases = [
        ("level >= 1", {"level": "3"}, True, None),
        ("level >= 1", {"level": "0"}, False, None),
        ("a + b", {"a": 1, "b": 2}, 3, None),
        ("if_else", None, None, None),
        ("len(xs) > 2", {"xs": [1, 2, 3]}, True, None),
        ("matches(email, '^[^@]+@[^@]+$')", {"email": "a@b.c"}, True, None),
        ("'03' == '3'", {}, False, None),
        ("'3' == 3", {}, True, None),
        ("coalesce(a, 'x')", {"a": None}, "x", None),
        ("__import__('os')", {}, None, "EXPR_FORBIDDEN"),
        ("(1).__class__", {}, None, "EXPR_FORBIDDEN"),
        ("[x for x in y]", {"y": [1]}, None, "EXPR_FORBIDDEN"),
        ("lambda: 1", {}, None, "EXPR_FORBIDDEN"),
        ("10 ** 100000", {}, None, "EXPR_ERROR"),
        ("missing > 1", {}, None, "EXPR_UNKNOWN_NAME"),
        ("1 in [1,2]", {}, True, None),
    ]
    failures: list[str] = []
    for text, scope, expect, err in cases:
        if text == "if_else":
            continue
        try:
            got = evaluate(text, scope or {})
        except ExprError as exc:
            if err is None:
                failures.append(f"{text} 意外报错 {exc.code}: {exc}")
            elif exc.code != err and err != "EXPR_ERROR":
                failures.append(f"{text} 报错码 {exc.code} != {err}")
            continue
        if err is not None:
            failures.append(f"{text} 本应被拒绝（{err}），实际返回 {got!r}")
        elif got != expect:
            failures.append(f"{text} 期望 {expect!r} 实际 {got!r}")
    # if-else 用 AST 走一遍，避免和上面表驱动混淆
    try:
        v = evaluate("1 if 2 > 1 else 0", {})
        if v != 1:
            failures.append(f"if-else 期望 1 实际 {v!r}")
    except ExprError as exc:
        failures.append(f"if-else 报错 {exc}")
    return {"pass": not failures, "failures": failures,
            "cases": len(cases) + 1, "allowed_functions": len(_ALLOWED_FUNCS)}
