"""字段级脱敏（SIL-A07 · 敏感字段泄漏防线）。

evidence / 检索输出里的原文片段可能携带个人信息（手机号、身份证、
银行卡、邮箱）。owner 级主体看原文；非 owner 级主体输出侧统一打码。
规则保守：只打高置信模式，避免把正常业务文本改得不可用。
"""
from __future__ import annotations

import re

# 高置信模式：误伤率低、泄漏代价高。
_PATTERNS: tuple[tuple[str, str], ...] = (
    # 手机号：1[3-9] 开头 11 位（前后非数字，避免命中更长的数字串）
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: m.group(0)[:3] + "****" + m.group(0)[-2:]),
    # 身份证：15/18 位（含校验位 X）
    (r"(?<!\d)\d{15}(?!\d)|(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
     r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
     lambda m: m.group(0)[:4] + "**********" + m.group(0)[-2:]),
    # 银行卡：13~19 位数字（前面已被身份证/手机号规则吃掉的不会再进这里）
    (r"(?<!\d)\d{13,19}(?!\d)", lambda m: "*" * 6 + m.group(0)[-4:]),
    # 邮箱：保留域名首段，打码本地部分
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
     lambda m: "***@" + m.group(0).split("@", 1)[1]),
)

_COMPILED = tuple((re.compile(p), fn) for p, fn in _PATTERNS)


def redact_text(text: str) -> str:
    """对文本做高置信敏感字段打码；空值原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pat, fn in _COMPILED:
        out = pat.sub(fn, out)
    return out


def redact_obj(obj, fields: tuple[str, ...] = ("excerpt", "content", "document_title")):
    """对输出对象里的指定文本字段打码（原样返回新对象，不改入参）。"""
    if isinstance(obj, dict):
        out = dict(obj)
        for f in fields:
            if isinstance(out.get(f), str):
                out[f] = redact_text(out[f])
        return out
    return obj
