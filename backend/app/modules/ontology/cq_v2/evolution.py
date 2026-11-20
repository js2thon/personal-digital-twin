"""CQ-Driven Ontology Evolution：集合质量、演进闭环与运行审计。

从「建一套本体」升级为「持续满足业务能力」：
定义能力/CQ → 推导需求 → 建设知识 → 执行 CQ 测试 → 分析能力缺口
→ 生成优化提案 → 重评估形成新基线。
"""
from __future__ import annotations

import math

from . import models as m

LOOP_STEPS = (
    ("define_capabilities", "定义业务能力与 CQ"),
    ("derive_requirements", "推导本体与知识需求"),
    ("build_knowledge", "建设本体与知识"),
    ("run_cq_tests", "执行 CQ 测试"),
    ("analyze_gaps", "分析业务能力缺口"),
    ("generate_proposals", "生成优化提案"),
    ("re_evaluate", "重新评估并形成新基线"),
)


def _entropy(counts: dict) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    return round(-sum((c / total) * math.log2(c / total) for c in counts.values() if c), 3)


def portfolio_quality(cqs: list[dict], capabilities: list[dict],
                      links: list[dict], requirements: list[dict],
                      registry=None, admitted_only: bool = True) -> dict:
    """集合质量五项检查；不设意图配比或每域条数配额。"""
    from . import quality as q
    rows = [c for c in cqs if (not admitted_only or q.admitted(c))]
    schema = registry.current() if registry else {}
    cap_with_cq = {lk["capability_id"] for lk in links
                   if any(c["id"] == lk["cq_id"] for c in rows)}
    intent_counts = {i: 0 for i in m.INTENTS}
    for c in rows:
        if c.get("intent") in intent_counts:
            intent_counts[c["intent"]] += 1
    used_entities = {c.get("subject_type") for c in rows if c.get("subject_type")} | \
                    {c.get("target_type") for c in rows if c.get("target_type")}
    used_rels = {r for c in rows for r in (c.get("required_relations") or [])}
    all_entities = set((schema.get("entity_types") or {}).keys())
    all_rels = set((schema.get("relation_types") or {}).keys())
    difficulty = {"simple": 0, "complex": 0, "cross_domain": 0}
    for c in rows:
        n = len(c.get("required_relations") or []) + len(c.get("required_attributes") or [])
        domains = {x.get("business_domain") for x in capabilities
                   if x.get("id") in (c.get("capability_ids") or [])}
        if len(domains) > 1 or c.get("intent") in m.DECISION_INTENTS:
            difficulty["cross_domain"] += 1
        elif n >= 3 or c.get("intent") in ("path", "compare", "temporal"):
            difficulty["complex"] += 1
        else:
            difficulty["simple"] += 1
    findings = []
    if capabilities and len(cap_with_cq) / len(capabilities) < 0.5:
        findings.append("业务能力覆盖不足一半，需检查是否漏采需求")
    dist = {k: v for k, v in intent_counts.items() if v}
    if rows and len(dist) == 1:
        findings.append("意图分布异常集中，触发人工复核（不自动判不合格）")
    return {
        "admitted_total": len(rows),
        "business_coverage": round(len(cap_with_cq) / len(capabilities), 3) if capabilities else 0.0,
        "intent_distribution": dist,
        "intent_entropy": _entropy(dist),
        "entity_coverage": {"used": sorted(used_entities),
                            "ratio": round(len(used_entities & all_entities) /
                                           len(all_entities), 3) if all_entities else 0.0},
        "relation_coverage": {"used": sorted(used_rels),
                              "ratio": round(len(used_rels & all_rels) /
                                             len(all_rels), 3) if all_rels else 0.0},
        "difficulty_distribution": difficulty,
        "coverage_matrix": coverage_matrix(rows),
        "findings": findings,
        "rubric_version": m.RUBRIC_VERSION,
        "generated_at": m.now(),
    }


def coverage_matrix(cqs: list[dict]) -> dict:
    """业务域 × 问题类型的规划视图；空格是「暂无需求」，不是缺陷。"""
    domains = sorted({c.get("business_domain") or "未分类" for c in cqs})
    kinds = ("查询", "关联", "聚合", "时序", "决策")
    intent_map = {"lookup": "查询", "boolean": "查询", "path": "关联",
                  "aggregate": "聚合", "compare": "聚合", "temporal": "时序",
                  "decision": "决策", "recommendation": "决策"}
    matrix = {d: {k: 0 for k in kinds} for d in domains}
    for c in cqs:
        k = intent_map.get(c.get("intent"), "查询")
        matrix[c.get("business_domain") or "未分类"][k] += 1
    return {"domains": domains, "kinds": list(kinds), "cells": matrix,
            "note": "规划矩阵，不要求每个业务域填满所有格子"}


def audit_entry(step: str, detail: dict | None = None, actor: str = "system") -> dict:
    return {"step": step, "at": m.now(), "actor": actor,
            "detail": detail or {}, "checksum": m.checksum({"step": step,
                                                            "detail": detail or {}})}


def run_loop(handlers: dict, actor: str = "system") -> dict:
    """执行 7 步演进闭环；任一步失败即停止并记录，不假装完成。"""
    run_id = m.uid()
    steps, audits = [], []
    status = "completed"
    for key, label in LOOP_STEPS:
        fn = handlers.get(key)
        if not callable(fn):
            result = {"skipped": True, "reason": "未提供该步骤处理器"}
        else:
            try:
                result = fn() or {}
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
                status = "failed"
        step = {"key": key, "label": label, "result": result}
        steps.append(step)
        audits.append(audit_entry(key, {"ok": "error" not in result}, actor))
        if status == "failed":
            break
    before = (steps[3]["result"].get("baseline_before") if len(steps) > 3 else {}) or {}
    after = (steps[-1]["result"].get("baseline_after") if steps else {}) or {}
    gap_summary = (steps[4]["result"].get("summary") if len(steps) > 4 else {}) or {}
    proposals = (steps[5]["result"].get("count") if len(steps) > 5 else 0) or 0
    coverage_after = (steps[3]["result"].get("coverage") or {}) if len(steps) > 3 else {}
    return {
        "run_id": run_id, "status": status, "steps": steps, "audit": audits,
        "baseline_before": before, "baseline_after": after,
        "gap_summary": gap_summary, "proposals_generated": proposals,
        "capability_coverage": coverage_after.get("coverage"),
        "accuracy_high_risk": coverage_after.get("high_risk_accuracy"),
        "evidence_rate": coverage_after.get("evidence_rate"),
        "started_at": steps[0]["result"].get("at", m.now()) if steps else m.now(),
        "finished_at": m.now(),
    }
