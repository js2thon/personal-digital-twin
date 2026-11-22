"""统一本体发布门禁：工作台摘要与真正的发布 API 共用同一套判定。"""
from __future__ import annotations

from typing import Any


def evaluate(job: dict, *, require_cq: bool = False) -> dict[str, Any]:
    draft = job.get("draft") or {}
    schema = draft.get("schema") or {}
    contract = job.get("contract") or {}
    materialized = job.get("materialized") or {}
    findings: list[dict] = []

    def add(code: str, message: str, *, severity: str = "error", count: int = 1):
        findings.append({"code": code, "message": message, "severity": severity, "count": count})

    if not schema.get("entity_types"):
        add("EMPTY_ONTOLOGY", "本体至少需要一个对象类型")
    types = schema.get("entity_types") or {}
    for name, rel in (schema.get("relation_types") or {}).items():
        if rel.get("source") not in types or rel.get("target") not in types:
            add("INVALID_RELATION_ENDPOINT", f"关系 {name} 的源/目标对象类型不存在")
    for name, attr in (schema.get("attributes") or {}).items():
        if attr.get("owner") not in types:
            add("INVALID_PROPERTY_OWNER", f"属性 {name} 的所属对象类型不存在")

    errors = [x for x in (job.get("issues") or [])
              if str(x.get("level", "")).lower() == "error"]
    if errors:
        add("MAPPING_CONTRACT_ERRORS", "映射契约仍存在阻断错误", count=len(errors))
    if not contract.get("mappings"):
        add("MAPPING_NOT_DEFINED", "尚未生成可执行映射契约")
    if not materialized or not materialized.get("data_scope"):
        add("PREVIEW_NOT_GENERATED", "尚未生成带数据范围标记的映射预览")

    pending = list(draft.get("review") or [])
    if pending:
        add("HUMAN_REVIEW_PENDING", "仍有待人工审核的本体候选", count=len(pending))

    mat_errors = list(materialized.get("errors") or [])
    if mat_errors:
        add("MATERIALIZATION_ERRORS", "预览物化发现数据错误", count=len(mat_errors))
    dangling = list(materialized.get("dangling") or [])
    if dangling:
        add("DANGLING_REFERENCES", "预览数据存在悬空关系引用", count=len(dangling))

    cq = job.get("cq_results")
    if require_cq and cq is None:
        add("CQ_NOT_EXECUTED", "发布策略要求 CQ 验收，但尚未执行")
    elif cq is not None:
        failed = cq.get("failed", cq.get("failed_count", 0)) or 0
        accuracy = cq.get("accuracy")
        if failed:
            add("CQ_FAILED", "存在未通过的本体能力问题（CQ）", count=int(failed))
        if accuracy is not None and float(accuracy) < 0.95:
            add("CQ_ACCURACY_LOW", "CQ 通过率低于 95%", count=1)

    blockers = [x for x in findings if x["severity"] == "error"]
    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": [x for x in findings if x["severity"] != "error"],
        "checks": {
            "ontology_schema": bool(types),
            "mapping_contract": bool(contract.get("mappings")) and not errors,
            "human_review": not pending,
            "preview_integrity": not mat_errors and not dangling,
            "cq": cq is not None,
        },
        "policy": {"require_cq": require_cq, "cq_min_accuracy": 0.95},
    }


def assert_publishable(job: dict, *, require_cq: bool = False) -> dict:
    report = evaluate(job, require_cq=require_cq)
    if not report["ready"]:
        messages = "; ".join(x["message"] for x in report["blockers"])
        raise ValueError(f"本体发布门禁未通过：{messages}")
    return report
