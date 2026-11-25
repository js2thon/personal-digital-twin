"""统一本体发布门禁：工作台摘要与真正的发布 API 共用同一套判定。"""
from __future__ import annotations

import math
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

    all_issues = list(job.get("issues") or [])
    errors = [x for x in all_issues if str(x.get("level", "")).lower() == "error"]
    warnings = [x for x in all_issues if str(x.get("level", "")).lower() == "warning"]
    if errors:
        add("MAPPING_CONTRACT_ERRORS", "映射契约或本体校验仍存在阻断错误", count=len(errors))
    for issue in warnings:
        add("PIPELINE_WARNING", str(issue.get("msg") or issue.get("message") or "存在非阻断警告"),
            severity="warning")
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
        # A non-null but empty/malformed payload must never satisfy a mandatory CQ gate.
        valid_cq = isinstance(cq, dict)
        items = cq.get("items") if valid_cq else None
        item_failed = item_accuracy = None
        if isinstance(items, list) and items:
            if any(not isinstance(item, dict) or not isinstance(item.get("pass"), bool)
                   for item in items):
                valid_cq = False
            else:
                item_failed = sum(1 for item in items if not item["pass"])
                item_accuracy = (len(items) - item_failed) / len(items)

        failed = cq.get("failed", cq.get("failed_count")) if valid_cq else None
        accuracy = cq.get("accuracy") if valid_cq else None
        if failed is None:
            failed = item_failed
        if accuracy is None:
            accuracy = item_accuracy

        if (isinstance(failed, bool) or not isinstance(failed, int) or failed < 0
                or isinstance(accuracy, bool) or not isinstance(accuracy, (int, float))
                or not math.isfinite(float(accuracy)) or not 0 <= float(accuracy) <= 1):
            valid_cq = False

        total = cq.get("total", cq.get("total_count")) if valid_cq else None
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int) or total <= 0 or failed > total:
                valid_cq = False
        if item_failed is not None and (failed != item_failed
                                        or abs(float(accuracy) - item_accuracy) > 1e-9):
            valid_cq = False

        if not valid_cq:
            add("CQ_INVALID_RESULT", "CQ 结果缺少有效的通过率/失败数，或汇总与逐题结果不一致")
        else:
            if failed:
                add("CQ_FAILED", "存在未通过的本体能力问题（CQ）", count=failed)
            if float(accuracy) < 0.95:
                add("CQ_ACCURACY_LOW", "CQ 通过率低于 95%", count=1)

    # 规范报告是门禁的权威输入；即使调用方漏写 job.issues，也不能绕过。
    standards = job.get("standards_report") or {}
    standards_findings = standards.get("findings") or []
    standards_warnings = [x for x in standards_findings if x.get("severity") == "warning"]
    standards_errors = [x for x in standards_findings if x.get("severity") == "error"]
    if standards_errors and not errors:
        add("MODELING_STANDARDS_ERRORS", "本体建模规范存在阻断错误", count=len(standards_errors))
    for item in standards_warnings:
        add(item.get("code", "MODELING_STANDARD_WARNING"),
            item.get("message", "存在非阻断本体规范提示"), severity="warning")
    blockers = [x for x in findings if x["severity"] == "error"]
    warnings_out = [x for x in findings if x["severity"] != "error"]
    state = "BLOCKED" if any(x["code"] in {"MAPPING_CONTRACT_ERRORS", "MODELING_STANDARDS_ERRORS"} for x in blockers) else (
        "REVIEW_REQUIRED" if blockers else "READY")
    return {
        "ready": not blockers,
        "state": state,
        "blockers": blockers,
        "warnings": warnings_out,
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
