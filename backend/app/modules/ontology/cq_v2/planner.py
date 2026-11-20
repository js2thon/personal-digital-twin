"""CQ Parser → CQ Query Planner：结构化、可校验、版本化的查询计划 AST。

工程原则：
- LLM 只负责理解问题与提出计划，不生成任意 SQL / Cypher 执行；
- 计划必须过 Schema、权限、复杂度校验；
- 每次执行记录 Plan 版本、Schema 版本、数据快照与证据来源；
- Retrieval Executor 是正式执行器，不是 LLM 兜底。
"""
from __future__ import annotations

from . import models as m


def parse_cq(cq: dict) -> dict:
    """意图、实体、约束、时间范围识别（确定性，不调用 LLM）。"""
    q = cq.get("question_template") or cq.get("question") or ""
    temporal = m.as_dict(cq.get("temporal_constraints"))
    scope = m.as_dict(cq.get("scope_constraints"))
    return {
        "intent": cq.get("intent", "lookup"),
        "variables": m.unresolved_params(q),
        "subject_type": cq.get("subject_type", ""),
        "target_type": cq.get("target_type", ""),
        "relations": list(cq.get("required_relations") or []),
        "attributes": list(cq.get("required_attributes") or []),
        "as_of": temporal.get("as_of", "current"),
        "time_window": temporal.get("window", ""),
        "scope": scope,
        "answer_shape": cq.get("answer_shape", "entity"),
        "min_evidence": int(cq.get("min_evidence") or 1),
    }


def build_plan(cq: dict, registry=None, permission_scopes: list[str] | None = None) -> dict:
    """生成 AST；不包含任何可执行 SQL/Cypher 文本。"""
    p = parse_cq(cq)
    steps: list[dict] = [{"op": "resolve_entity", "type": p["subject_type"],
                          "side": cq.get("subject_side", "source"),
                          "params": {v: "${%s}" % v for v in p["variables"]}}]

    if p["attributes"]:
        steps.append({"op": "filter_attribute", "attributes": p["attributes"]})
    if p["intent"] == "path":
        steps.append({"op": "traverse", "relations": p["relations"],
                      "max_hops": min(m.MAX_HOPS, len(p["relations"]) or m.MAX_HOPS),
                      "direction": cq.get("subject_side", "source")})
    elif p["relations"]:
        steps.append({"op": "traverse", "relations": p["relations"],
                      "max_hops": 1, "direction": cq.get("subject_side", "source")})
    if p["intent"] == "aggregate":
        steps.append({"op": "aggregate", "group_by": p["scope"].get("group_by", ""),
                      "measure": "count"})
    if p["intent"] == "compare":
        steps.append({"op": "compare", "metric": p["scope"].get("metric", ""),
                      "ordering": m.as_dict(cq.get("acceptance_criteria")).get("ordering", "desc")})
    if p["intent"] == "boolean":
        steps.append({"op": "exists", "relations": p["relations"],
                      "attributes": p["attributes"]})
    if p["intent"] == "temporal" or p["as_of"] != "current":
        steps.append({"op": "asof", "as_of": p["as_of"],
                      "window": p["time_window"],
                      "valid_time": True, "transaction_time": True})
    if p["intent"] in m.DECISION_INTENTS:
        steps.append({"op": "decide", "rules": m.as_dict(
            cq.get("acceptance_criteria")).get("rules", [])})
    # 制度条款、合同约定、项目背景等不适合全部转成图关系 → 正式检索执行器
    steps.append({"op": "retrieve", "sources": p["scope"].get("sources", []),
                  "mandatory": bool(p["scope"].get("sources"))})
    steps.append({"op": "validate", "min_evidence": p["min_evidence"],
                  "require_source": True, "check_conflict": True,
                  "check_permission": True, "check_temporal": True})

    # 执行器链：事实查询始终执行；有关系叠加图查询，时序叠加时间切片，
    # 检索执行器是正式成员（制度条款/合同约定不必全部转成图关系）
    chain = ["fact"]
    if p["relations"]:
        chain.append("graph")
    if p["as_of"] != "current" or p["intent"] == "temporal":
        chain.append("temporal")
    chain.append("retrieval")
    if p["intent"] in m.DECISION_INTENTS:
        chain.append("decision")
    chain.append("validator")
    schema_version = registry.current_version() if registry else (cq.get("schema_version") or "")
    ast = {
        "plan_version": "QP-" + m.checksum({"cq": cq.get("code"), "v": cq.get("version"),
                                            "schema": schema_version, "steps": steps})[:10],
        "cq_code": cq.get("code", ""), "cq_version": int(cq.get("version") or 1),
        "schema_version": schema_version, "intent": p["intent"],
        "steps": steps, "executor_chain": chain,
        "complexity": {"steps": len(steps),
                       "max_hops": max([s.get("max_hops", 0) for s in steps] or [0]),
                       "estimated_rows": p["scope"].get("estimated_rows", 100)},
        "permissions": {"tenant_scoped": True,
                        "required_scopes": permission_scopes or [],
                        "enforced_at": "execution"},
        "provenance": {"plan_version_required": True,
                       "schema_version_required": True,
                       "data_snapshot_required": True,
                       "evidence_refs_required": True},
    }
    ast["checksum"] = m.checksum({k: v for k, v in ast.items() if k != "checksum"})
    return ast


def validate_plan(plan: dict, registry=None, permission_ctx: dict | None = None) -> dict:
    """Schema / 权限 / 复杂度校验。返回 (validated, notes)。"""
    notes: list[dict] = []
    ctx = permission_ctx or {}

    def note(level, msg):
        notes.append({"level": level, "message": msg})

    raw = str(plan)
    for key in m.FORBIDDEN_PLAN_KEYS:
        if f"'{key}'" in raw or f'"{key}"' in raw:
            note("error", f"计划包含禁止字段 {key}：LLM 不得生成任意 SQL/Cypher")
    steps = plan.get("steps") or []
    if not steps:
        note("error", "计划为空")
    if len(steps) > m.MAX_PLAN_STEPS:
        note("error", f"步骤数 {len(steps)} 超过上限 {m.MAX_PLAN_STEPS}")
    for i, st in enumerate(steps):
        if st.get("op") not in m.PLAN_OPS:
            note("error", f"步骤 {i} 的 op 非法：{st.get('op')}")
        if st.get("max_hops", 0) > m.MAX_HOPS:
            note("error", f"步骤 {i} 跳数 {st['max_hops']} 超过上限 {m.MAX_HOPS}")
    if not any(s.get("op") == "validate" for s in steps):
        note("error", "计划缺少 validate 步骤（证据/冲突/权限/时效校验）")
    if not (plan.get("permissions") or {}).get("tenant_scoped"):
        note("error", "计划未声明租户隔离")
    if not ctx.get("tenant_id"):
        note("error", "权限上下文缺少 tenant_id")
    granted = set(ctx.get("scopes") or [])
    missing = set((plan.get("permissions") or {}).get("required_scopes") or []) - granted
    if missing:
        note("error", f"缺少权限范围：{sorted(missing)}")
    # checksum 只覆盖 AST 本体；服务层附加的审核/落库元数据不参与摘要
    core = {k: v for k, v in plan.items()
            if k not in ("checksum", "validated", "validation_notes",
                         "id", "created_at", "updated_at")}
    expect = m.checksum(core)
    if plan.get("checksum") != expect:
        note("error", "计划 checksum 不一致，可能被篡改")
    if registry:
        for st in steps:
            if st.get("op") == "traverse":
                for rel in st.get("relations") or []:
                    if not registry.validate_ref("relation", rel):
                        note("warning", f"关系 {rel} 未登记，需走 Schema Proposal")
            if st.get("op") == "resolve_entity" and st.get("type"):
                if not registry.validate_ref("entity", st["type"]):
                    note("warning", f"实体类型 {st['type']} 未登记，需走 Schema Proposal")
    if any(n["level"] == "warning" for n in notes):
        note("info", "warning 不阻断执行，但会产出 Schema Gap")
    errors = [n for n in notes if n["level"] == "error"]
    return {"validated": not errors, "notes": notes,
            "errors": errors, "warnings": [n for n in notes if n["level"] == "warning"]}


def executor_chain(plan: dict) -> list[str]:
    return list(plan.get("executor_chain") or [])
