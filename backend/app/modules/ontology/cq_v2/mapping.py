"""Schema Mapping：概念标准化、同义词、父子类型、复用判断与六类 Proposal。

在生成新类型 Proposal 之前先做映射消歧，避免「部门/组织/事业部/业务单元」
被 LLM 当成四种实体类型，造成本体膨胀。
"""
from __future__ import annotations

from . import models as m

SYNONYMS = {
    "OrganizationUnit": ("部门", "组织", "事业部", "业务单元", "组织单元", "科室", "中心"),
    "Person": ("员工", "人员", "人", "同事", "职工"),
    "Project": ("项目", "项目组", "课题"),
    "Policy": ("制度", "规则", "办法", "规定", "条款"),
    "Document": ("文档", "文件", "材料", "附件"),
    "System": ("系统", "平台", "应用"),
    "Risk": ("风险", "风险项", "隐患"),
}
# 过于宽泛的概念：优先拆分为更具体的类型，而不是直接新建
BROAD_TERMS = ("对象", "事物", "信息", "内容", "东西", "数据", "资源")
DEPRECATED_TYPES = ("Org", "Department", "BusinessUnit")
# 父子类型判断：命中已有父类的用分类属性区分，不新建类型
PARENT_CHILD = {
    "项目经理": "Person", "开发工程师": "Person", "审批人": "Person",
    "子项目": "Project", "里程碑": "Project",
    "合同": "Document", "会议纪要": "Document",
    "部门": "OrganizationUnit", "事业部": "OrganizationUnit",
}
OPERATION_LABELS = {
    "reuse": "复用现有类型", "extend": "扩展现有类型（分类属性）",
    "merge": "合并重复概念", "split": "拆分过于宽泛的类型",
    "create": "创建新类型", "deprecate": "废弃旧类型",
}


def normalize_term(term: str) -> tuple[str, list[str]]:
    """概念标准化：返回 (canonical, 命中的同义词列表)。"""
    t = (term or "").strip()
    for canonical, words in SYNONYMS.items():
        if t == canonical or t in words:
            return canonical, list(words)
    if t in PARENT_CHILD:
        return PARENT_CHILD[t], [t]
    return t, []


def map_concept(term: str, registry=None, examples: list[str] | None = None,
                cq_id: str = "") -> dict:
    """决定 Reuse / Extend / Merge / Split / Create / Deprecate。"""
    examples = examples or []
    canonical, synonyms = normalize_term(term)
    current = registry.current() if registry else {}
    types = current.get("entity_types") or {}
    # 父子类型与同义词归一要区分：「项目经理」是 Person 的子类（Extend），
    # 「部门」只是 OrganizationUnit 的业务叫法（Reuse）
    synonym_terms = {w for words in SYNONYMS.values() for w in words}
    is_child = term in PARENT_CHILD and term not in synonym_terms
    reasons: list[str] = []

    if term in DEPRECATED_TYPES or canonical in DEPRECATED_TYPES:
        op, target = "deprecate", canonical
        reasons.append("该命名已被统一目录废弃，应迁移到标准类型")
    elif term in BROAD_TERMS:
        op, target = "split", ""
        reasons.append("概念过于宽泛，应先拆成具体业务类型")
    elif term in types and canonical in types and term != canonical:
        op, target = "merge", canonical
        reasons.append(f"已存在以业务叫法命名的旧类型 {term}，应合并到 {canonical}")
    elif is_child:
        op, target = "extend", PARENT_CHILD[term]
        reasons.append(f"是 {target} 的子类，建议用分类属性区分而非新建类型")
    elif canonical in types:
        op, target = "reuse", canonical
        reasons.append(f"已在 Schema Registry 登记为 {canonical}，无需新增类型")
    elif synonyms and len(synonyms) > 1:
        op, target = "merge", canonical
        reasons.append(f"多个业务叫法归一到 {canonical}，避免重复建类型")
    else:
        op, target = "create", canonical
        reasons.append("未命中现有类型/同义词表，需评估新建必要性")

    necessity = {
        "has_cq_basis": bool(cq_id),
        "example_count": len(examples),
        "min_examples": 3,
        "allowed": bool(cq_id) and len(examples) >= 3,
        "verdict": ("可以提出新建" if bool(cq_id) and len(examples) >= 3
                    else "证据不足，仅允许作为待补证据的草稿"),
    }
    if op == "create" and not necessity["allowed"]:
        reasons.append("新类型必须带 CQ 依据与至少 3 条真实例句（红线）")
    return {
        "term": term, "canonical": canonical, "synonyms": synonyms,
        "operation": op, "operation_label": OPERATION_LABELS[op],
        "target_ref": target, "reason": "；".join(reasons),
        "necessity": necessity, "examples": examples, "cq_id": cq_id,
    }


def propose(term: str, registry=None, cq_id: str = "",
            examples: list[str] | None = None, actor: str = "llm",
            counter: int = 1) -> dict:
    mapped = map_concept(term, registry, examples, cq_id)
    return {
        "id": m.uid(), "code": f"SP-{counter:04d}", "cq_id": cq_id or None,
        "term": term, "operation": mapped["operation"],
        "target_ref": mapped["target_ref"] or None,
        "reason": mapped["reason"], "examples": examples or [],
        "synonyms": mapped["synonyms"], "necessity": mapped["necessity"],
        "status": "pending", "proposed_by": actor,
        "schema_version": registry.current_version() if registry else "",
        "created_at": m.now(),
    }


def approve(proposal: dict, actor: str, registry=None) -> dict:
    """提案只落 proposal；发布 Schema 版本仍需单独的人审动作。"""
    if not actor or actor in ("llm", "system"):
        raise ValueError("Proposal 必须由人审核")
    if proposal.get("operation") == "create" and not (proposal.get("necessity") or {}).get("allowed"):
        raise ValueError("新建类型缺少 CQ 依据或 3 条真实例句，不予通过")
    return {"status": "approved", "reviewed_by": actor, "reviewed_at": m.now()}
