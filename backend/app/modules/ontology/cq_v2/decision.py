"""Decision Evaluation Layer：决策型 / 建议型 CQ 的独立评估层。

边界：决策型 CQ 不复用知识查询执行器；MVP 只产出「建议 + 依据 + 置信度 +
风险 + 不确定性」，人工确认后才可进入授权执行。Jev 决策模型不替代本体、
事实查询或业务规则引擎。
"""
from __future__ import annotations

from . import models as m


def _match(cond: dict, ctx: dict) -> bool:
    field, op, val = cond.get("field"), cond.get("op"), cond.get("value")
    actual = (ctx or {}).get(field)
    if actual is None:
        return False
    try:
        if op == "<=":
            return float(actual) <= float(val)
        if op == ">=":
            return float(actual) >= float(val)
        if op == "<":
            return float(actual) < float(val)
        if op == ">":
            return float(actual) > float(val)
        if op == "in":
            return actual in (val if isinstance(val, list) else [val])
    except (TypeError, ValueError):
        return False
    return actual == val


def evaluate_rules(rules: list[dict], ctx: dict) -> list[dict]:
    hits = []
    for r in rules or []:
        conds = r.get("when") or {}
        ok = all(_match({"field": k, **v} if isinstance(v, dict) else
                        {"field": k, "op": "eq", "value": v}, ctx)
                 for k, v in conds.items())
        hits.append({"code": r.get("code", ""), "matched": bool(ok),
                     "then": r.get("then", ""), "weight": float(r.get("weight") or 0),
                     "note": r.get("note", "")})
    return hits


def evaluate(cq: dict, evidence_ctx: dict | None = None,
             rules: list[dict] | None = None) -> dict:
    """输出建议而非裁定；置信度必须结合证据覆盖与规则命中，不用 LLM 自评。"""
    ctx = evidence_ctx or {}
    rule_list = rules or m.as_dict(cq.get("acceptance_criteria")).get("rules") or []
    hits = evaluate_rules(rule_list, ctx)
    matched = [h for h in hits if h["matched"]]
    votes: dict[str, float] = {}
    for h in matched:
        votes[h["then"]] = votes.get(h["then"], 0.0) + h["weight"]
    suggestion = max(votes, key=votes.get) if votes else "needs_more_evidence"
    total_w = sum(float(r.get("weight") or 0) for r in rule_list) or 1.0
    hit_w = sum(h["weight"] for h in matched)
    evidence = m.as_dict(ctx.get("evidence_summary"))
    ev_rate = m.clamp01(evidence.get("valid", 0) / max(1, evidence.get("total", 1)))
    confidence = round(0.6 * m.clamp01(hit_w / total_w) + 0.4 * ev_rate, 3)

    risk_flags = []
    if ev_rate < 0.5:
        risk_flags.append("evidence_insufficient")
    if any(h.get("then") != suggestion and h["matched"] for h in matched):
        risk_flags.append("conflicting_rules")
    if ctx.get("has_conflict"):
        risk_flags.append("fact_conflict")
    if ctx.get("source_expired"):
        risk_flags.append("stale_source")
    if cq.get("risk_level") in ("high", "critical"):
        risk_flags.append("high_risk_decision")

    rationale = [{"code": h["code"], "note": h["note"] or h["then"],
                  "weight": h["weight"]} for h in matched]
    return {
        "id": m.uid(), "cq_id": cq.get("id"), "run_id": m.uid(),
        "inputs": ctx, "rules_applied": hits, "suggestion": suggestion,
        "rationale": rationale, "confidence": confidence,
        "risk_flags": risk_flags,
        "uncertainty": {"unmatched_rules": [h["code"] for h in hits if not h["matched"]],
                        "evidence_rate": ev_rate,
                        "note": "建议不构成裁定，须经人工确认"},
        "approval_status": "pending", "approved_by": "", "approved_at": "",
        "created_at": m.now(),
    }


def approve(decision: dict, actor: str, decision_value: str | None = None,
            authorized_execution: bool = False) -> dict:
    """MVP：人工确认；默认不开放自动执行。"""
    if not actor or actor in ("llm", "system"):
        raise ValueError("决策确认必须由人完成")
    if decision.get("approval_status") not in ("pending", "approved"):
        raise ValueError("该决策已结束，不可重复确认")
    if "fact_conflict" in (decision.get("risk_flags") or []):
        raise ValueError("存在未解决的事实冲突，须先裁决再确认")
    out = {"approval_status": "approved", "approved_by": actor,
           "approved_at": m.now()}
    if authorized_execution:
        if decision.get("confidence", 0) < 0.8:
            raise ValueError("置信度不足，不允许授权执行")
        if "high_risk_decision" in (decision.get("risk_flags") or []):
            raise ValueError("高风险决策不允许授权自动执行")
        out["approval_status"] = "executed"
    if decision_value:
        out["final_decision"] = decision_value
    return out
