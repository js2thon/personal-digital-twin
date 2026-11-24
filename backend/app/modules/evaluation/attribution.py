"""本体问答失败归因：规则优先，证据不足时明确返回 unknown。"""
from __future__ import annotations

from typing import Any


def attribute(result: dict) -> dict:
    actual = result.get("actual") or {}
    expected = result.get("expected") or {}
    answer = str(result.get("answer_text") or "")
    sparql = str(result.get("sparql") or "")
    rows = actual.get("rows") or []
    engine = str(result.get("engine") or "")
    error = str(result.get("error") or "")
    intent = str(actual.get("intent") or "")
    expected_intent = str(result.get("expected_intent") or "")

    candidates: list[tuple[str, float, str]] = []
    if expected_intent and intent and expected_intent != intent:
        candidates.append(("F1", 0.90, f"意图不匹配：期望 {expected_intent}，实际 {intent}"))
    elif not intent and not error and not sparql and engine not in {"guard", "template"}:
        candidates.append(("F1", 0.75, "问答结果未返回意图信息"))

    if error or not actual.get("ok"):
        candidates.append(("F2", 0.95, error or "问答引擎返回失败"))
    elif not sparql and engine not in {"guard", "template"}:
        candidates.append(("F2", 0.72, "未生成可执行 SPARQL"))

    ontology_gap_terms = ("本体里没有", "本体中没有", "缺少该指标", "超出本体", "没有能回答")
    if any(term in answer for term in ontology_gap_terms) or engine == "guard":
        candidates.append(("F3", 0.90, "问答引擎明确提示本体能力或属性缺口"))

    expected_positive = (
        bool(expected.get("values")) or bool(expected.get("rows")) or
        (expected.get("type") == "min_rows" and int(expected.get("value", 0)) > 0) or
        (expected.get("type") == "ask_boolean" and expected.get("value") is True)
    )
    if actual.get("ok") and not rows and expected_positive and engine != "guard":
        candidates.append(("F5", 0.78, "查询可执行但结果为空，黄金期望包含正向结果"))

    if actual.get("ok") and rows and not result.get("passed"):
        candidates.append(("F6", 0.55, "查询返回非空结果，但与黄金期望不一致"))

    if not candidates:
        return {
            "primary_code": "unknown", "secondary_codes": [],
            "confidence": 0.0,
            "evidence": ["现有执行记录不足以确定失败原因"],
        }

    candidates.sort(key=lambda item: item[1], reverse=True)
    primary = candidates[0]
    return {
        "primary_code": primary[0],
        "secondary_codes": [item[0] for item in candidates[1:]],
        "confidence": primary[1],
        "evidence": [item[2] for item in candidates],
    }
