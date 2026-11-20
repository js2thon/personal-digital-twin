"""本体建模 · 数据清洗规则（声明式 DSL）纯函数解析与校验。

定位：这是「建模画布 → 清洗节点」的**规则契约**，不是执行器。
dry-run 只推演配置，完整验证只校验规则语法；真正的数据改写仍由
运行时数据管道负责，绝不用示例数据冒充清洗结果。

语法（每行一条，`#` 后为注释）：
    op(field, field2, option=value, ...)

内置算子（可扩展，未知算子直接报错，不静默通过）：
    trim(fields...)                        去前后空格
    drop_null(fields...)                   字段为空则剔除整行
    dedup(field, keep=first|last, case=sensitive|insensitive)  按字段去重
    normalize_date(field, format=%Y-%m-%d) 日期归一
    map_values(field, 旧值=新值, ...)       值映射归一
    fill_default(field, 默认值)            空值补默认
    lower(fields...) / upper(fields...)    大小写归一
    regex_replace(field, 正则, 替换串)      正则替换
    strip_chars(field, 字符集)             去掉首尾指定字符
    replace(field, 旧串, 新串)             字面量替换

兼容：旧草稿里的中文逗号描述（如「去前后空格,空值剔除」）标记为 legacy，
只留痕、不参与机器校验，不会在报告里被伪装成「已校验」。

零 I/O、零外部依赖，可单测。
"""
from __future__ import annotations

import re
from typing import Any

FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
OPT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# key: (最少字段数, 最多字段数(None=不限), 允许的关键字选项, 选项枚举, 说明)
OPS: dict[str, dict[str, Any]] = {
    "trim": {"min": 1, "max": None, "opts": {}, "desc": "去前后空格"},
    "drop_null": {"min": 1, "max": None, "opts": {}, "desc": "字段为空则剔除整行"},
    "dedup": {
        "min": 1, "max": 1,
        "opts": {"keep": ("first", "last"), "case": ("sensitive", "insensitive")},
        "desc": "按字段去重（keep=first 保留首条 / last 保留末条）",
    },
    "normalize_date": {
        "min": 1, "max": 1, "opts": {"format": None},
        "desc": "日期归一（format 如 %Y-%m-%d）",
    },
    "map_values": {"min": 1, "max": 1, "opts": {}, "desc": "值映射（旧值=新值）"},
    "fill_default": {"min": 1, "max": 1, "opts": {}, "desc": "空值补默认"},
    "lower": {"min": 1, "max": None, "opts": {}, "desc": "转小写"},
    "upper": {"min": 1, "max": None, "opts": {}, "desc": "转大写"},
    "regex_replace": {"min": 1, "max": 1, "opts": {}, "desc": "正则替换"},
    "strip_chars": {"min": 1, "max": 1, "opts": {}, "desc": "去掉首尾指定字符"},
    "replace": {"min": 1, "max": 1, "opts": {}, "desc": "字面量替换"},
}

# 可直接粘贴到画布「清洗规则」框的示例（与示例工作流的员工花名册对齐）
CLEAN_RULE_EXAMPLES = """trim(employee_no,full_name,email)              # 去前后空格
dedup(employee_no, keep=first)                 # 按工号去重，保留首条
drop_null(employee_no,full_name)               # 必填字段为空则剔除整行
normalize_date(entry_date, format=%Y-%m-%d)    # 日期归一
map_values(department, 研发一部=研发, 研发二部=研发)  # 部门值归一
fill_default(level, P0)                        # 空职级补默认
lower(email)                                   # 邮箱转小写"""

LEGACY_MARKERS = ("去前后空格", "空值剔除", "日期归一", "去重", "去噪", "补全", "归一", "清洗")


def _strip_comment(line: str) -> str:
    """去掉行尾 # 注释（引号内的 # 保留）。"""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == "\\" and i + 1 < len(line):
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _split_args(body: str) -> list[str]:
    """按逗号切分参数，支持引号包裹的逗号与空串。"""
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for i, ch in enumerate(body):
        if quote:
            if ch == "\\" and i + 1 < len(body) and body[i + 1] in (quote, "\\"):
                buf.append(body[i + 1])
                continue
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == ",":
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf).strip())
    return [x for x in out if x] if len(out) == 1 and not out[0] else out


def parse_rule_line(line: str, line_no: int = 1) -> dict[str, Any]:
    """解析单行规则；永不抛异常，错误写进 err 字段供报告展示。"""
    raw = str(line or "").strip()
    base: dict[str, Any] = {
        "line": line_no, "raw": raw, "kind": "empty", "op": "", "fields": [],
        "options": {}, "args": [], "err": "",
    }
    if not raw or raw.startswith("#"):
        return base

    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$", raw, re.S)
    if not m:
        if any(c in raw for c in LEGACY_MARKERS):
            return {**base, "kind": "legacy", "err": ""}
        return {**base, "kind": "invalid", "err": "规则需写成 op(field, ...) 形式"}
    op, body = m.group(1), m.group(2)
    tokens = _split_args(body)
    spec = OPS.get(op)
    if spec is None:
        return {**base, "kind": "invalid", "op": op, "err": f"未知清洗算子：{op}"}

    fields: list[str] = []
    args: list[str] = []
    options: dict[str, str] = {}
    for tok in tokens:
        km = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", tok, re.S)
        if km and km.group(1) in spec["opts"]:
            options[km.group(1)] = km.group(2).strip()
            continue
        if not fields and FIELD_RE.match(tok):
            fields.append(tok)
        else:
            args.append(tok)

    errs: list[str] = []
    n = len(fields)
    if n < spec["min"]:
        errs.append(f"{op} 至少需要 {spec['min']} 个字段，当前 {n} 个")
    if spec["max"] is not None and n > spec["max"]:
        errs.append(f"{op} 最多 {spec['max']} 个字段，当前 {n} 个")
    for f in fields:
        if not FIELD_RE.match(f):
            errs.append(f"字段名不是合法标识符：{f}")

    for key, allowed in spec["opts"].items():
        if key not in options:
            continue
        val = options[key]
        if allowed and val not in allowed:
            errs.append(f"{op} 的 {key} 只能是 {'/'.join(allowed)}，当前 {val or '(空)'}")

    if op == "map_values" and not args:
        errs.append("map_values 至少需要一组 旧值=新值")
    if op == "map_values":
        for a in args:
            if "=" not in a:
                errs.append(f"值映射需写成 旧值=新值：{a}")
    if op in ("regex_replace", "replace", "strip_chars", "fill_default") and not args:
        errs.append(f"{op} 缺少参数值")

    return {
        **base, "kind": "rule", "op": op, "fields": fields,
        "options": options, "args": args, "err": "；".join(errs),
    }


def parse_clean_rules(text: str | None) -> list[dict[str, Any]]:
    out = []
    for i, line in enumerate(str(text or "").splitlines(), 1):
        rule = parse_rule_line(_strip_comment(line), i)
        if rule["kind"] != "empty":
            out.append(rule)
    return out


def validate_clean_rules(text: str | None, known_fields: list[str] | None = None) -> dict[str, Any]:
    """校验规则文本；known_fields 非空时额外检查字段是否在上游列里。"""
    rules = parse_clean_rules(text)
    errors = [{"line": r["line"], "message": r["err"]} for r in rules if r["kind"] == "invalid" or r["err"]]
    legacy = [r["line"] for r in rules if r["kind"] == "legacy"]
    unknown: list[dict[str, Any]] = []
    if known_fields:
        allowed = {str(x).strip() for x in known_fields if str(x).strip()}
        for r in rules:
            if r["kind"] != "rule":
                continue
            for f in r["fields"]:
                if f not in allowed:
                    unknown.append({"line": r["line"], "field": f})
    return {
        "ok": not errors,
        "total": len(rules),
        "structured": sum(1 for r in rules if r["kind"] == "rule"),
        "legacy_lines": legacy,
        "unknown_fields": unknown,
        "rules": rules,
        "errors": errors,
        "note": (
            ("语法错误 " + str(len(errors)) + " 处；" if errors else "语法校验通过；")
            + (f"{len(legacy)} 条旧式描述仅留痕、未机器校验；" if legacy else "")
            + (f"{len(unknown)} 个字段不在上游列清单；" if unknown else "")
            + "规则为声明式配置，dry-run 不改写数据"
        ),
    }


def describe_rules(text: str | None) -> list[str]:
    """给试运行/报告用的简短摘要，不编造执行结果。"""
    out = []
    for r in parse_clean_rules(text):
        if r["kind"] == "rule":
            tail = "".join(f", {k}={v}" for k, v in r["options"].items())
            tail += "".join(f", {a}" for a in r["args"])
            out.append(f"{r['op']}({','.join(r['fields'])}{tail})")
        elif r["kind"] == "legacy":
            out.append(f"[旧式描述] {r['raw']}")
    return out
