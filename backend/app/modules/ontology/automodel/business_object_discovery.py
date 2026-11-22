"""业务对象发现：在表结构生成 Schema 之前识别业务对象候选、跨表归并线索与身份证据。
本阶段只提出可解释候选，不在证据不足时自动合并对象，避免错误合并污染语义层。
"""
from __future__ import annotations

import re
from collections import defaultdict


_CANONICAL = {
    "Person": {"employee", "employees", "staff", "user", "users", "person", "people", "member", "人员", "员工"},
    "OrganizationUnit": {"dept", "department", "departments", "org", "organization", "unit", "部门", "组织"},
    "Project": {"project", "projects", "项目"},
    "Document": {"document", "documents", "doc", "file", "文档"},
    "System": {"system", "systems", "app", "application", "系统"},
    "Skill": {"skill", "skills", "技能"},
}


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-z一-龥]+", "", str(value or "").lower())


def _object_type(table: str) -> tuple[str, float]:
    token = _norm(table)
    for kind, names in _CANONICAL.items():
        if token in {_norm(x) for x in names}:
            return kind, 0.95
    return "".join(x.capitalize() for x in re.split(r"[_\W]+", table) if x) or "Thing", 0.62


def _identity(profile) -> list[str]:
    keys = list(getattr(profile, "primary_key", None) or [])
    if keys:
        return keys
    columns = getattr(profile, "columns", {}) or {}
    return [name for name in columns if re.search(r"(^id$|_id$|_no$|_code$|工号|编码)", name, re.I)]


def discover(profiles: dict, existing_schema: dict | None = None) -> dict:
    """生成业务对象候选目录与跨表归并建议；输出稳定、可序列化的证据。"""
    existing = (existing_schema or {}).get("entity_types") or {}
    candidates = []
    groups = defaultdict(list)
    for table, profile in profiles.items():
        if getattr(profile, "is_junction", False):
            continue
        kind, confidence = _object_type(table)
        identity = _identity(profile)
        columns = list((getattr(profile, "columns", {}) or {}).keys())
        comments = [getattr(c, "comment", "") for c in (getattr(profile, "columns", {}) or {}).values()]
        candidates.append({
            "table": table, "object_type": kind, "confidence": confidence,
            "identity_candidates": identity, "columns": columns,
            "business_description": getattr(profile, "comment", "") or "",
            "evidence": {
                "declared_primary_key": bool(getattr(profile, "primary_key", None)
                                              and not getattr(profile, "pk_inferred", False)),
                "foreign_key_count": len(getattr(profile, "foreign_keys", []) or []),
                "column_comment_count": sum(bool(x) for x in comments),
                "row_count": getattr(profile, "row_count", None),
            },
            "existing_object": kind in existing,
        })
        groups[kind].append(table)

    merge_candidates = []
    for kind, tables in groups.items():
        if len(tables) < 2:
            continue
        for index, left in enumerate(tables):
            for right in tables[index + 1:]:
                lp, rp = profiles[left], profiles[right]
                li, ri = set(_identity(lp)), set(_identity(rp))
                lc = set((getattr(lp, "columns", {}) or {}).keys())
                rc = set((getattr(rp, "columns", {}) or {}).keys())
                shared = sorted(lc & rc)
                identity_overlap = sorted(li & ri)
                score = min(1.0, 0.45 + 0.25 * bool(identity_overlap) +
                            0.20 * (len(shared) / max(1, min(len(lc), len(rc)))))
                merge_candidates.append({
                    "object_type": kind, "tables": [left, right],
                    "confidence": round(score, 3), "shared_columns": shared[:30],
                    "identity_overlap": identity_overlap,
                    "decision": "review_merge" if score >= 0.65 else "keep_separate",
                    "reason": ("同类业务对象且存在共享字段/身份线索；需审核身份语义后归并"
                               if score >= 0.65 else "仅类型名称相似，缺少足够身份证据，不建议自动归并"),
                })
    return {
        "version": "1",
        "status": "completed",
        "candidate_count": len(candidates),
        "merge_candidate_count": len(merge_candidates),
        "candidates": candidates,
        "merge_candidates": merge_candidates,
        "policy": {
            "auto_merge": False,
            "reason": "跨表归并会改变业务对象身份边界；当前仅输出证据与审核建议，不自动合并。",
        },
    }
