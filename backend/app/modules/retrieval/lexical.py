"""中文友好的词法召回层（不依赖 pgvector / zhparser / 外部分词器）。

背景：本地内嵌 PostgreSQL 的 semantic_chunks.embedding 是 TEXT（无 vector 扩展），
原检索只能 `content ILIKE '%整句问题%'`，中文问题几乎必然 0 命中。
这里把问题拆成「短语 + 汉字 2-gram + 英文/数字词」三类词项，
在 SQL 里按命中词项加权打分，保证相关证据能被召回并按相关性排序。

设计取舍：中文没有内置分词器时，2-gram 召回率高但略有噪音，
因此强短语权重远高于 2-gram，并叠加可选 embedding 余弦做二次重排。
"""
from __future__ import annotations

import re

_CJK = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CJK}A-Za-z0-9]+")
_CJK_RUN_RE = re.compile(rf"[{_CJK}]+")
_EN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}|[0-9]{2,}")

# 疑问/功能词：切短语时直接丢弃，避免「什么」「帮我」把召回打歪
_STOP_PHRASES = (
    "什么", "怎么", "怎样", "如何", "为什么", "是否", "可否", "能否", "哪些", "哪个",
    "多少", "什么时候", "我的", "请问", "帮我", "麻烦", "一下", "关于", "有关", "知道",
    "告诉", "我们", "以及", "进行", "相关", "情况", "内容", "问题", "时候", "需要",
    "希望", "想问", "查一下", "看看", "请", "吗", "呢", "了", "的", "是", "在", "有",
    "和", "与", "及", "这个", "那个", "你", "我", "他", "它", "说", "讨论", "什么内容",
)
_EN_STOP = {"the", "and", "for", "with", "what", "how", "why", "are", "is", "my",
            "please", "about", "this", "that", "from", "json", "true", "false", "null"}

_SPLIT_RE = re.compile("|".join(re.escape(p) for p in sorted(_STOP_PHRASES, key=len, reverse=True)))


def extract_terms(question: str, max_terms: int = 14) -> list[dict]:
    """返回 [{"term": str, "weight": float}]，按权重降序。"""
    text = str(question or "")[:600]
    # JSON 上下文里形如 "business_context": {...} 的键名会把召回打歪，先剥掉引号与键名
    text = re.sub(r'"[A-Za-z_]+"\s*:', " ", text)
    found: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        term = term.strip()
        if len(term) < 2 or term in found:
            return
        found[term] = max(found.get(term, 0.0), weight)

    # 1) 英文/数字词（AppKey、SOW、2026 …）
    for m in _EN_RE.finditer(text):
        w = m.group(0).lower()
        if w not in _EN_STOP:
            add(w, 1.8)

    # 2) 中文短语：按标点与疑问词切开，保留 2~12 字的实义片段
    for raw in _TOKEN_RE.findall(text):
        for piece in _SPLIT_RE.split(raw):
            if not piece:
                continue
            cjk = "".join(_CJK_RUN_RE.findall(piece))
            if len(cjk) >= 2:
                add(cjk[:12], 2.6 if len(cjk) <= 8 else 2.0)
            for en in _EN_RE.findall(piece):
                if en.lower() not in _EN_STOP:
                    add(en, 1.8)

    # 3) 汉字 2-gram 兜底（覆盖语序变化、切词不准）
    for run in _CJK_RUN_RE.findall(text):
        for i in range(0, len(run) - 1):
            gram = run[i:i + 2]
            if not any(ch in gram for ch in "么吗呢什怎"):
                add(gram, 0.55)

    ranked = sorted(found.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    return [{"term": t, "weight": round(w, 2)} for t, w in ranked[:max_terms]]


def score_expr(terms: list[dict], column: str, prefix: str = "kw") -> tuple[str, dict]:
    """生成 SQL 打分表达式与绑定参数。column 可以是任意 SQL 文本表达式。"""
    if not terms:
        return "0.0", {}
    parts: list[str] = []
    params: dict = {}
    for i, t in enumerate(terms):
        key = f"{prefix}{i}"
        parts.append(
            f"(CASE WHEN {column} ILIKE :{key} THEN {t['weight']:.2f} ELSE 0.0 END)"
        )
        # term 只含中文/字母/数字/下划线，ILIKE 通配符不会从问题里混入
        params[key] = "%" + t["term"].replace("%", "").replace("_", "") + "%"
    return "(" + " + ".join(parts) + ")", params


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
