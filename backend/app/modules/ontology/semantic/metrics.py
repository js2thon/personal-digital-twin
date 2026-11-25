"""可评估（V1.3 语义层方案 · 能力 6 · 验收自证）。

三项语义层指标，全部照实计算、无数据时不编造（返回 None + 说明）：
  1) constraint_violation_rate  —— 写入约束拒绝次数 / 总写入次数（语义写入审计）；
  2) relation_type_validity_rate —— 已落库权威边中通过类型约束的比率；
  4) fact_alignment_consistency —— 实例↔Fact 一致性比率（alignment 模块）。

另有 path_query_accuracy 的口径披露：现有 graph.py 的路径查询是「枚举」而非
「带标准答案的正确率」，因此本模块返回 available=False + 原因，不用默认值
填充掩盖（遵循项目「覆盖率缺口照实披露」约定）。
"""
from __future__ import annotations

from . import store, alignment
from .constraints import validate_relation


def compute(tenant_id: str = "default", schema: dict | None = None) -> dict:
    schema = schema or store.load_semantic_schema() or {}
    return {
        "constraint_violation_rate": _write_violation_rate(tenant_id),
        "relation_type_validity_rate": _relation_validity(tenant_id, schema),
        "fact_alignment_consistency": alignment.consistency_metrics(tenant_id),
        "path_query_accuracy": {
            "available": False, "value": None,
            "reason": "现有 graph.py 只枚举简单路径，缺标准答案集；"
                      "不构造带标签的路径正确率，避免用默认值掩盖缺口",
        },
        "known_gaps": ["path_query_accuracy 需人工标注路径回归集（方案 Phase C 后续）"],
    }


def _write_violation_rate(tenant_id: str) -> dict:
    try:
        row = store.db.fetch_one(
            "SELECT count(*) FILTER (WHERE outcome = 'rejected')::int AS rejected, "
            " count(*)::int AS total FROM ontology.semantic_write_audit "
            "WHERE tenant_id = :t", {"t": tenant_id})
        total = int(row["total"]) if row else 0
        rejected = int(row["rejected"]) if row else 0
        return {"total_writes": total, "rejected": rejected,
                "rate": round(rejected / total, 4) if total else None,
                "note": "无写入记录时 rate=None（照实，不填 0）"}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)[:120]}


def _relation_validity(tenant_id: str, schema: dict) -> dict:
    edges = store.list_relations(tenant_id, "derived = false", limit=2000)
    if not edges:
        return {"checked": 0, "valid": 0, "rate": None,
                "note": "无权威边，rate=None"}
    valid = 0
    violations: list[dict] = []
    for e in edges:
        rep = validate_relation(schema, e)
        if rep["conforms"]:
            valid += 1
        else:
            violations.append({"id": e.get("id"),
                               "relation_type": e.get("relation_type"),
                               "violations": rep["violations"][:3]})
    return {"checked": len(edges), "valid": valid,
            "rate": round(valid / len(edges), 4),
            "violation_edges": violations[:20],
            "violation_count": len(violations)}
