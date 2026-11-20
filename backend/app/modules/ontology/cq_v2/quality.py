"""CQ 质量准入：六维评分、硬门槛、四关 Pipeline、参数化模板与测试实例校验。

设计红线：
- 硬门槛与结构约束由规则引擎独占，LLM 不得覆盖；
- 可执行性评「能否转成执行计划」，与当前知识库有没有数据无关；
- 标准答案必须来自独立来源，禁止 LLM 自生成自验证。
"""
from __future__ import annotations

import re
from collections import Counter

from . import models as m

VAGUE_WORDS = ("怎么样", "情况", "经营情况", "相关", "有什么关系", "最",
               "是否合适", "大概", "可能", "一些", "经营", "整体", "如何评价",
               "观点", "偏好", "掌握哪些", "哪些技能")
COMPOUND_HINTS = ("、", "；", "并且", "以及", "同时", "还有", "吗？")
ANSWER_SOURCE_TYPES = ("authoritative_record", "business_system",
                       "human_annotation", "llm_draft_unverified")
GOLD_SOURCE_TYPES = ("authoritative_record", "business_system", "human_annotation")


# ---------------------------------------------------------------- 评分
def score_dimensions(dims: dict) -> tuple[float, str]:
    """0-4 分 → 100 分制；缺失维度按 0 分处理（不猜）。"""
    total = 0.0
    for key, weight in m.QUALITY_WEIGHTS.items():
        try:
            d = float(as_number((dims or {}).get(key, 0)))
        except (TypeError, ValueError):
            d = 0.0
        total += m.clamp01(d / 4.0) * weight
    score = round(total, 2)
    return score, m.grade_of(score)


def as_number(v):
    return v if isinstance(v, (int, float)) else 0


def hard_gates(dims: dict, score: float | None = None) -> dict:
    """三个硬门槛 + 总分门槛。门槛未过，总分再高也不准入。"""
    d = dims or {}
    t = as_number(d.get("target", 0))
    c = as_number(d.get("clarity", 0))
    v = as_number(d.get("verifiability", 0))
    s = float(score if score is not None else score_dimensions(d)[0])
    gates = {
        "target_not_zero": t != 0,
        "clarity_ge_3": c >= 3,
        "verifiability_ge_2": v >= 2,
        "score_ge_80": s >= 80,
    }
    gates["passed"] = all(gates.values())
    gates["failed"] = [k for k in ("target_not_zero", "clarity_ge_3",
                                   "verifiability_ge_2", "score_ge_80")
                       if not gates[k]]
    return gates


# ---------------------------------------------------------------- 四关检查
def semantic_check(cq: dict, existing: list[dict] | None = None) -> dict:
    """第一关：明确性、歧义、重复、业务价值。"""
    issues: list[str] = []
    q = (cq.get("question") or cq.get("question_template") or "").strip()
    template = cq.get("question_template") or ""
    if len(q) < 6:
        issues.append("问题过短，无法判断业务语义")
    for w in VAGUE_WORDS:
        if w in q:
            issues.append(f"命中宽泛词「{w}」，答案边界不清")
            break
    if sum(1 for h in COMPOUND_HINTS if h in q) >= 1 and q.count("？") > 1:
        issues.append("一条问句包含多个疑问，应拆成多条 CQ")
    if not (cq.get("business_purpose") or "").strip():
        issues.append("缺少 business_purpose，无法判断是否对应真实业务决策")
    if not (cq.get("capability_ids") or []):
        issues.append("未关联业务能力（capability_ids）")
    if not (cq.get("scope_constraints") or {}):
        issues.append("缺少 scope_constraints（业务范围/集合范围/比较口径）")
    if not (cq.get("temporal_constraints") or {}):
        issues.append("缺少 temporal_constraints（时间口径）")
    if template:
        unresolved = m.unresolved_params(template)
        if unresolved and not cq.get("sample_question"):
            issues.append(f"模板变量 {unresolved} 缺少可执行的 sample_question")
    dup = duplicate_check(cq, existing or [])
    if dup:
        issues.append(f"疑似与 {dup['code']} 重复（相似度 {dup['similarity']}）")
    return {"stage": "semantic", "passed": not issues, "issues": issues,
            "duplicate": dup}


def duplicate_check(cq: dict, existing: list[dict], threshold: float = 0.82) -> dict | None:
    def tokens(s):
        return Counter(ch for ch in (s or "") if "\u4e00" <= ch <= "\u9fff")
    a = tokens(cq.get("question") or cq.get("question_template") or "")
    if not a:
        return None
    for other in existing:
        if other.get("id") == cq.get("id"):
            continue
        b = tokens(other.get("question") or "")
        if not b:
            continue
        inter = sum((a & b).values())
        union = sum((a | b).values())
        sim = round(inter / union, 3) if union else 0.0
        if sim >= threshold:
            return {"code": other.get("code", ""), "similarity": sim}
    return None


def ontology_mapping_check(cq: dict, schema: dict | None = None) -> dict:
    """第二关：本体映射。未命中的概念进 Proposal，不直接改 Schema。"""
    issues, unregistered = [], []
    schema = schema or {}
    types = set((schema.get("entity_types") or {}).keys())
    rels = set((schema.get("relation_types") or {}).keys())
    st = cq.get("subject_type") or ""
    if not st:
        issues.append("缺少 subject_type")
    elif types and st not in types:
        unregistered.append({"kind": "entity", "ref": st})
        issues.append(f"主体类型 {st} 未在 Schema Registry 登记")
    for rel in cq.get("required_relations") or []:
        if rels and rel not in rels:
            unregistered.append({"kind": "relation", "ref": rel})
            issues.append(f"关系类型 {rel} 未登记")
    for attr in cq.get("required_attributes") or []:
        if not (schema.get("attributes") or {}).get(attr):
            unregistered.append({"kind": "attribute", "ref": attr})
            issues.append(f"属性 {attr} 未在 Schema Registry 登记")
    return {"stage": "ontology_mapping", "passed": not issues,
            "issues": issues, "unregistered": unregistered}


def verifiability_check(cq: dict, instances: list[dict] | None = None) -> dict:
    """第三关：能否定义标准答案、证据要求与客观验收规则。"""
    issues = []
    if not (cq.get("acceptance_criteria") or {}):
        issues.append("缺少 acceptance_criteria，无法客观验收")
    if not (cq.get("evidence_requirements") or {}):
        issues.append("缺少 evidence_requirements，答案不可溯源")
    if cq.get("cq_kind") == "test" or cq.get("evaluation_method"):
        if not (cq.get("expected_answer") or {}):
            issues.append("测试型 CQ 必须提供 expected_answer 或答案约束")
    bad_sources = []
    for inst in instances or []:
        ok, reason = validate_instance(inst, cq)
        if not ok:
            bad_sources.append({"code": inst.get("instance_code", ""), "reason": reason})
            issues.append(f"实例 {inst.get('instance_code','')} 不合法：{reason}")
    return {"stage": "verifiability", "passed": not issues,
            "issues": issues, "bad_instances": bad_sources}


def validate_instance(inst: dict, cq: dict | None = None) -> tuple[bool, str]:
    """标准答案来源白名单 + 模板变量闭合 + 答案非空。"""
    src = inst.get("source_type", "")
    if src not in ANSWER_SOURCE_TYPES:
        return False, f"未知答案来源 {src}"
    if inst.get("status") == "active" and src not in GOLD_SOURCE_TYPES:
        return False, "LLM 草稿答案未经人工确认，不得作为正式标准答案"
    if not (inst.get("expected_answer") or {}):
        return False, "expected_answer 为空"
    if not (inst.get("acceptance_criteria") or {}):
        return False, "acceptance_criteria 为空"
    if inst.get("status") == "active" and not (inst.get("annotated_by") or ""):
        return False, "正式标准答案必须记录 annotated_by"
    template = (cq or {}).get("question_template") or ""
    if template:
        missing = [p for p in m.unresolved_params(template)
                   if p not in (inst.get("params") or {})]
        if missing:
            return False, f"模板变量未绑定：{missing}"
    return True, ""


# ---------------------------------------------------------------- 六维初评
def infer_dimensions(cq: dict, semantic: dict, mapping: dict,
                     verifiable: dict, llm_dims: dict | None = None) -> dict:
    """规则引擎给出保守初评；LLM 只能在语义维度提议，硬门槛仍由规则判定。"""
    d = {k: 0 for k in m.QUALITY_DIMENSIONS}
    d["target"] = 4 if (cq.get("business_purpose") and cq.get("capability_ids")) else (
        2 if cq.get("business_purpose") else 0)
    d["clarity"] = 4 if not semantic["issues"] else (2 if len(semantic["issues"]) <= 2 else 1)
    d["verifiability"] = 4 if not verifiable["issues"] else (
        2 if (cq.get("acceptance_criteria") or {}) else 1)
    d["ontology_fit"] = 4 if not mapping["issues"] else (
        2 if mapping["unregistered"] else 1)
    d["representativeness"] = 4 if (cq.get("question_template") and
                                    cq.get("capability_ids")) else 2
    # 可执行性 = 能否生成明确执行计划，与「库里有没有数据」无关
    d["executability"] = 4 if (cq.get("intent") in m.INTENTS and cq.get("subject_type")
                               and cq.get("answer_shape")) else 1
    if llm_dims:
        for key in ("target", "clarity", "representativeness"):
            if key in llm_dims:
                suggested = m.clamp01(as_number(llm_dims[key]) / 4.0) * 4
                # 规则更严格时以规则为准（LLM 不能放宽硬门槛相关维度）
                d[key] = min(d[key], round(suggested))
    return d


def suggest_rewrite(cq: dict, stages: list[dict]) -> list[str]:
    out = []
    for st in stages:
        for issue in st.get("issues", []):
            if "宽泛词" in issue:
                out.append("把宽泛表述换成可判定的实体/关系/指标，并写入 scope_constraints")
            elif "多个疑问" in issue:
                out.append("拆成多条单一 answer_shape 的 CQ")
            elif "business_purpose" in issue:
                out.append("补 business_purpose：说明谁在什么决策中使用该答案")
            elif "capability_ids" in issue:
                out.append("关联至少一个 Business Capability")
            elif "scope_constraints" in issue:
                out.append("补集合范围、比较口径与排序规则")
            elif "temporal_constraints" in issue:
                out.append("补 as_of / 时间窗，明确当前还是历史时点")
            elif "acceptance_criteria" in issue:
                out.append("定义可复算的验收规则（精确匹配/集合匹配/规则判定）")
            elif "evidence_requirements" in issue:
                out.append("定义证据类型、最少条数与可溯源要求")
            elif "未登记" in issue or "未登记" in str(issue):
                out.append("走 Schema Mapping 生成 Proposal，审核后再发布新版本")
            elif "重复" in issue:
                out.append("与已有 CQ 合并为同一参数化模板，用 subject_side 翻转")
    return list(dict.fromkeys(out))


def four_gates(cq: dict, existing: list[dict] | None = None,
               schema: dict | None = None, instances: list[dict] | None = None,
               llm_dims: dict | None = None) -> dict:
    """四关串联。任一关失败回退 draft 并给出可操作建议，不静默丢弃。"""
    s1 = semantic_check(cq, existing)
    s2 = ontology_mapping_check(cq, schema)
    s3 = verifiability_check(cq, instances)
    stages = [s1, s2, s3]
    dims = infer_dimensions(cq, s1, s2, s3, llm_dims)
    score, grade = score_dimensions(dims)
    gates = hard_gates(dims, score)
    s4 = {"stage": "scoring", "passed": gates["passed"],
          "dimensions": dims, "weights": m.QUALITY_WEIGHTS,
          "quality_score": score, "grade": grade, "hard_gates": gates,
          "issues": ([] if gates["passed"] else
                     [f"硬门槛未过：{', '.join(gates['failed'])}"])}
    stages.append(s4)
    passed = all(s["passed"] for s in stages[:3]) and gates["passed"]
    return {
        "cq_id": cq.get("id"), "code": cq.get("code"),
        "passed": passed,
        "machine_passed": all(s["passed"] for s in stages[:3]),
        "stages": stages, "dimensions": dims, "quality_score": score,
        "grade": grade, "hard_gates": gates,
        "rubric_version": m.RUBRIC_VERSION,
        "suggestions": suggest_rewrite(cq, stages),
        "review_status": "machine_passed" if passed else "pending",
        "assessed_by": "rule_engine",
        "assessed_at": m.now(),
    }


def admit(cq: dict, assessment: dict, actor: str, force: bool = False) -> dict:
    """人工签字是最后一道关；机器不能写 human_approved。"""
    if not actor or actor in ("llm", "rule_engine", "system"):
        raise ValueError("准入必须由业务负责人签字（actor 不能是机器）")
    if not assessment.get("machine_passed") and not force:
        raise ValueError("机器四关未全过，不能进入人工准入")
    if not force and not assessment.get("hard_gates", {}).get("passed"):
        raise ValueError("硬门槛未过，不能准入")
    return {"review_status": "human_approved", "admitted_by": actor,
            "admitted_at": m.now(), "reviewed_at": m.now(),
            "version": int(cq.get("version") or 1) + 1}


def admitted(cq: dict) -> bool:
    gates = m.as_dict(cq.get("hard_gates"))
    return bool(cq.get("review_status") == "human_approved"
                and cq.get("status") != "retired"
                and (cq.get("quality_score") or 0) >= 80
                and gates.get("passed", False))
