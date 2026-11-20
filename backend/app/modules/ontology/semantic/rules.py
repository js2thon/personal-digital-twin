"""业务规则引擎（V1.3 语义层方案 · 能力 4 · 最小实现）。

条件 → 触发约束或衍生事实候选。规则执行结果进入 Decision 的
Evidence / Applied Rules（确定性输入），**不直接改写权威 Fact**。

条件 DSL（务实、够用，不引入规则引擎框架）：
  {"field": "operation", "op": "eq", "value": "write_relation"}
  {"all": [<cond>...]} / {"any": [<cond>...]} / {"not": <cond>}
  op: eq / ne / in / contains / gt / gte / lt / lte / exists
field 支持点路径（如 "context.visibility"），从扁平或嵌套 context 取值。

action.rule_type:
  - integrity      → 产出确定性约束（violations，可阻断写入）
  - derivation     → 产出衍生事实候选（derived_candidates，仅证据）
  - authorization  → 产出可见性/授权约束（violations，可阻断）
"""
from __future__ import annotations

from typing import Any


def _get(context: dict, field: str) -> Any:
    if field in context:
        return context[field]
    cur: Any = context
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _compare(actual: Any, op: str, expected: Any) -> bool:
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "in":
            return actual in (expected if isinstance(expected, (list, tuple, set)) else [expected])
        if op == "contains":
            return expected in (actual if isinstance(actual, (list, tuple, str, dict)) else [])
        if op == "exists":
            return (actual is not None) == bool(expected)
        if op in ("gt", "gte", "lt", "lte"):
            if actual is None:
                return False
            a, b = (float(actual), float(expected))
            return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    except (TypeError, ValueError):
        return False
    return False


def match_condition(condition: dict | None, context: dict) -> bool:
    """单条条件求值；空条件视为不匹配（fail-closed，避免「空规则全触发」）。"""
    if not condition:
        return False
    if "all" in condition:
        return all(match_condition(c, context) for c in (condition["all"] or []))
    if "any" in condition:
        return any(match_condition(c, context) for c in (condition["any"] or []))
    if "not" in condition:
        return not match_condition(condition["not"], context)
    field = condition.get("field") or ""
    op = condition.get("op") or "eq"
    return _compare(_get(context, field), op, condition.get("value"))


def _coerce(rule: Any) -> dict:
    if hasattr(rule, "model_dump"):
        return rule.model_dump(mode="python")
    return dict(rule or {})


def apply_rules(rules: list, context: dict,
                schema: dict | None = None) -> dict:
    """按 priority 升序执行启用规则，返回 Decision 可直接消费的证据结构。

    返回：{evaluated, matched, applied_rules, violations, derived_candidates,
           constraints, note}
    """
    normalized = [_coerce(r) for r in (rules or [])]
    normalized.sort(key=lambda r: int(r.get("priority") or 100))
    applied: list[dict] = []
    violations: list[dict] = []
    candidates: list[dict] = []
    matched = 0
    for rule in normalized:
        if rule.get("enabled") is False:
            continue
        if not match_condition(rule.get("condition"), context):
            continue
        matched += 1
        action = rule.get("action") or {}
        rtype = action.get("rule_type") or rule.get("rule_type") or "integrity"
        entry = {
            "code": rule.get("name") or rule.get("code") or "RULE",
            "rule_type": rtype,
            "matched": True,
            "priority": int(rule.get("priority") or 100),
            "action": action,
        }
        applied.append(entry)
        if rtype == "derivation":
            candidates.append({
                "code": entry["code"],
                "kind": action.get("emit") or "derived_fact_candidate",
                "facts_written": False,          # 关键承诺：不改写权威 Fact
                "note": action.get("note", ""),
            })
        else:  # integrity / authorization 都产出确定性约束
            violations.append({
                "code": entry["code"],
                "rule_type": rtype,
                "assert": action.get("assert") or action.get("effect") or "deny",
                "message": action.get("message", ""),
                "severity": "violation" if action.get("effect") == "deny"
                            or rtype == "authorization" else "warning",
            })
    return {
        "evaluated": len(normalized),
        "matched": matched,
        "applied_rules": applied,
        "violations": violations,
        "derived_candidates": candidates,
        "constraints": [v for v in violations if v["severity"] == "violation"],
        "note": "规则结果仅作 Decision 的 Evidence/Applied Rules；衍生项为候选，不改写权威 Fact",
    }


def evaluate_for_write(schema: dict, operation: str,
                       payload: dict) -> dict:
    """写入路径的规则上下文构造 + 执行（供 OntologyService 调用）。"""
    context = {
        "operation": operation,
        "relation_type": payload.get("relation_type"),
        "source_type": payload.get("source_type"),
        "target_type": payload.get("target_type"),
        "visibility": payload.get("visibility", "organization"),
        "properties": payload.get("properties") or {},
    }
    rules = (schema or {}).get("business_rules") or []
    return apply_rules(rules, context, schema)


def validate_rule_payload(rule: dict) -> dict:
    """规则登记时的确定性校验（返回违规列表，供 API 转 400）。"""
    errs: list[str] = []
    if not (rule.get("name") or "").strip():
        errs.append("规则缺少 name")
    if rule.get("rule_type") not in ("integrity", "derivation", "authorization"):
        errs.append("rule_type 须为 integrity / derivation / authorization")
    if not isinstance(rule.get("condition"), dict) or not rule.get("condition"):
        errs.append("condition 必须是非空对象（空条件 fail-closed 不生效）")
    if not isinstance(rule.get("action"), dict) or not rule.get("action"):
        errs.append("action 必须是非空对象")
    return {"valid": not errs, "errors": errs}
