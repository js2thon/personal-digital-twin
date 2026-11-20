"""CQ 执行与评估：四类执行器 + Answer Validator + 五维覆盖 + 五态 + 六类缺口。

关键判断（评审第 6 点）：有数据不等于有答案，有答案也不等于答案可信。
- 有关系但没证据 → 不能算完整回答；
- 有证据但已过期 → 不能算当前问题可回答；
- 两个矛盾事实 → conflict，不任选其一；
- 有答案但用户无权访问 → restricted。
"""
from __future__ import annotations

from . import models as m

EXECUTORS = ("fact", "graph", "temporal", "retrieval", "decision", "validator")


# ---------------------------------------------------------------- 执行器
def fact_executor(cq: dict, plan: dict, snap: dict) -> dict:
    """事实 / 属性 / 聚合 / 存在性查询。"""
    facts = snap.get("facts") or []
    attrs = snap.get("attributes") or {}
    entities = set(snap.get("entities", {}).get(cq.get("subject_type"), []) or [])
    needed_rel = set(cq.get("required_relations") or [])
    needed_attr = set(cq.get("required_attributes") or [])
    covered, values, ev = set(), [], []

    for f in facts:
        sid = f.get("subject_id")
        if sid not in entities:
            continue
        pred = f.get("predicate")
        if needed_rel and pred not in needed_rel:
            continue
        if needed_attr and pred not in needed_attr:
            continue
        covered.add(sid)
        values.append({"subject_id": sid, "predicate": pred,
                       "value": f.get("object"), "fact": f})
        ev.extend(f.get("evidence_refs") or [])

    for sid, bag in attrs.items():
        if sid not in entities:
            continue
        hit = [a for a in needed_attr if a in bag]
        if hit:
            covered.add(sid)
            for a in hit:
                values.append({"subject_id": sid, "predicate": a, "value": bag[a],
                               "fact": {"evidence_refs": bag.get("_evidence", [])}})
    if cq.get("intent") == "aggregate":
        values = [{"count": len(covered)}]
    if cq.get("intent") == "boolean":
        values = [{"exists": bool(covered)}]
    return {"values": values, "covered_ids": sorted(covered),
            "total_ids": len(entities), "evidence_refs": ev}


def graph_executor(cq: dict, plan: dict, snap: dict) -> dict:
    """关系 / 路径 / 关联查询（≤3 跳，带环保护由 plan 复杂度校验保证）。"""
    edges = snap.get("relations") or []
    entities = set(snap.get("entities", {}).get(cq.get("subject_type"), []) or [])
    needed = set(cq.get("required_relations") or [])
    covered, values, ev = set(), [], []
    for e in edges:
        if needed and e.get("type") not in needed:
            continue
        a, b = e.get("source"), e.get("target")
        if a in entities or b in entities:
            covered.add(a if a in entities else b)
            values.append({"path": [a, e.get("type"), b]})
            ev.extend(e.get("evidence") or [])
    return {"values": values, "covered_ids": sorted(covered),
            "total_ids": len(entities), "evidence_refs": ev}


def temporal_executor(cq: dict, plan: dict, snap: dict) -> dict:
    """时间双轨：Valid Time（业务有效）+ Transaction Time（系统记录）。"""
    as_of = (m.as_dict(cq.get("temporal_constraints")).get("as_of") or "current")
    facts = snap.get("facts") or []
    entities = set(snap.get("entities", {}).get(cq.get("subject_type"), []) or [])
    covered, values, ev, stale = set(), [], [], 0
    for f in facts:
        if f.get("subject_id") not in entities:
            continue
        vf, vt = f.get("valid_from"), f.get("valid_to")
        if as_of != "current" and vf and str(as_of) < str(vf):
            stale += 1
            continue
        if as_of != "current" and vt and str(as_of) > str(vt):
            stale += 1
            continue
        covered.add(f["subject_id"])
        values.append({"subject_id": f["subject_id"], "value": f.get("object"),
                       "valid_from": vf, "valid_to": vt,
                       "transaction_time": f.get("recorded_at")})
        ev.extend(f.get("evidence_refs") or [])
    return {"values": values, "covered_ids": sorted(covered),
            "total_ids": len(entities), "evidence_refs": ev,
            "as_of": as_of, "stale_count": stale}


def retrieval_executor(cq: dict, plan: dict, snap: dict) -> dict:
    """文档 / 语义证据检索：正式执行器，不是 LLM 兜底。"""
    docs = snap.get("documents") or []
    sources = m.as_dict(cq.get("scope_constraints")).get("sources") or []
    hits = [d for d in docs if not sources or d.get("source") in sources]
    return {"values": [{"doc": d.get("id"), "excerpt": d.get("text", "")[:120]}
                       for d in hits],
            "covered_ids": [], "total_ids": len(docs),
            "evidence_refs": [e for d in hits for e in (d.get("evidence_refs") or [])],
            "retrieved": len(hits)}


# ---------------------------------------------------------------- 答案验证
def answer_validator(cq: dict, exec_out: dict, snap: dict,
                     permission_ctx: dict | None = None) -> dict:
    """证据校验、冲突检查、权限检查、答案完整性。"""
    ctx = permission_ctx or {}
    refs = exec_out.get("evidence_refs") or []
    refset, covered = set(refs), set(exec_out.get("covered_ids") or [])
    catalog = snap.get("evidence_catalog") or {}
    valid, expired, low_authority = [], 0, 0
    for r in refs:
        meta = catalog.get(r, {})
        if meta.get("expired"):
            expired += 1
        elif float(meta.get("authority", 1.0)) < 0.5:
            low_authority += 1
        else:
            valid.append(r)
    used = set(cq.get("required_relations") or []) | \
           set(cq.get("required_attributes") or [])
    # 冲突/受限只针对「本次执行实际用到」且谓词相关的事实与证据，不全库扫描
    def related(item):
        pred = item.get("predicate")
        if pred is not None and used and pred not in used:
            return False
        return (item.get("subject_id") in covered
                or bool(refset & set(item.get("evidence_refs") or []))
                or bool(refset & set(item.get("evidence") or [])))
    facts = snap.get("facts") or []
    conflicts = [f for f in facts if f.get("conflict") and related(f)]
    restricted = [f for f in facts
                  if f.get("restricted") and not ctx.get("is_admin") and related(f)]
    denied = [d for d in (snap.get("documents") or [])
              if d.get("restricted") and not ctx.get("is_admin") and related(d)]
    min_ev = int(cq.get("min_evidence") or 1)
    return {
        "evidence_total": len(refs), "evidence_valid": len(valid),
        "evidence_expired": expired, "evidence_low_authority": low_authority,
        "evidence_ok": len(valid) >= min_ev,
        "conflicts": conflicts, "has_conflict": bool(conflicts),
        "restricted": restricted + denied, "has_restricted": bool(restricted or denied),
        "permission_checked_at_execution": bool(ctx.get("tenant_id")),
    }


# ---------------------------------------------------------------- 五维覆盖
def five_dimension_coverage(cq: dict, exec_out: dict, validation: dict,
                            registry=None, expected: dict | None = None) -> dict:
    total = max(1, int(exec_out.get("total_ids") or 0))
    covered = len(exec_out.get("covered_ids") or [])
    # 1 Schema：需要的类型/关系是否已登记
    needed = ([cq.get("subject_type")] if cq.get("subject_type") else []) + \
             list(cq.get("required_relations") or [])
    if registry and needed:
        ok = sum(1 for n in needed if registry.validate_ref(
            "relation" if n in (cq.get("required_relations") or []) else "entity", n))
        schema = ok / len(needed)
    else:
        schema = 1.0 if needed else 0.0
    # 2 Data：事实/属性是否覆盖主体
    data = covered / total if exec_out.get("total_ids") is not None else 0.0
    # 3 Evidence：有效证据（排除过期、低可信）
    ev_total = max(1, validation["evidence_total"])
    evidence = min(1.0, validation["evidence_valid"] / ev_total) if validation["evidence_total"] else 0.0
    if validation["has_conflict"]:
        evidence *= 0.5
    # 4 Answer：有独立标准答案才判正确；没有就不冒充正确
    if expected:
        actual = [v.get("value") for v in exec_out.get("values") or []]
        want = expected.get("value")
        answer = 1.0 if (want in actual or (want is None and actual)) else 0.0
        answer_basis = "gold_answer"
    else:
        answer = round(min(data, evidence), 3)
        answer_basis = "proxy_data_x_evidence"
    # 5 Temporal：时间有效性
    temporal = 1.0
    if exec_out.get("as_of") not in (None, "current"):
        stale = int(exec_out.get("stale_count") or 0)
        temporal = round(max(0.0, 1 - stale / max(1, stale + covered)), 3)
        # 只有历史时点问题才因过期证据扣时效分；当前问题已在 evidence 维度扣过，不重复
        if validation["evidence_expired"]:
            temporal = round(temporal * 0.5, 3)
    dims = {
        "schema": m.clamp01(schema), "data": m.clamp01(data),
        "evidence": m.clamp01(evidence), "answer": m.clamp01(answer),
        "temporal": m.clamp01(temporal),
    }
    dims["answer_basis"] = answer_basis
    return dims


def decide_state(dims: dict, validation: dict, exec_out: dict) -> str:
    """五态：conflict / restricted 优先于 answered。"""
    if validation.get("has_restricted"):
        return "restricted"
    if validation.get("has_conflict"):
        return "conflict"
    core = (dims["schema"] + dims["data"] + dims["evidence"] +
            dims["answer"] + dims["temporal"]) / 5
    if core >= 0.8 and validation.get("evidence_ok"):
        return "answered"
    if core > 0 or (exec_out.get("values") or []):
        return "partial"
    return "unanswerable"


def weighted_coverage(items: list[dict]) -> dict:
    """Coverage = Σ w_i·I(answered_i) / Σ w_i；w_i 来自 CQ 业务优先级。"""
    num = sum(float(i.get("cq_weight") or 1.0) for i in items
              if i.get("state") == "answered")
    den = sum(float(i.get("cq_weight") or 1.0) for i in items)
    return {"numerator": round(num, 3), "denominator": round(den, 3),
            "coverage": round(num / den, 3) if den else 0.0,
            "answered": sum(1 for i in items if i.get("state") == "answered"),
            "total": len(items)}


def layered_gates(items: list[dict], min_core: float = 0.70,
                  min_normal: float = 0.60, min_evidence: float = 0.90) -> dict:
    """70% 不是所有场景统一硬门槛：核心/普通/高风险/证据分开看。"""
    def cov(rows):
        return weighted_coverage(rows)["coverage"]
    core = [i for i in items if i.get("risk_level") in ("high", "critical")
            or i.get("priority") == 1]
    normal = [i for i in items if i not in core]
    ev = [float(i.get("dimensions", {}).get("evidence") or 0) for i in items]
    ev_rate = round(sum(ev) / len(ev), 3) if ev else 0.0
    return {
        "core_cq_coverage": cov(core), "core_min": min_core,
        "normal_cq_coverage": cov(normal), "normal_min": min_normal,
        "evidence_traceability": ev_rate, "evidence_min": min_evidence,
        "high_risk_conflicts": sum(1 for i in core if i.get("state") == "conflict"),
        "passed": (cov(core) >= min_core and cov(normal) >= min_normal
                   and ev_rate >= min_evidence
                   and not any(i.get("state") == "conflict" for i in core)),
    }


# ---------------------------------------------------------------- 六类缺口
def detect_gaps(cq: dict, exec_out: dict, validation: dict,
                dims: dict, registry=None, snap: dict | None = None) -> list[dict]:
    snap = snap or {}
    gaps: list[dict] = []

    def add(kind, ref, note, action):
        sev = m.GAP_SEVERITY[kind]
        impact = 2.0 if cq.get("risk_level") in ("high", "critical") else 1.0
        weight = float(cq.get("cq_weight") or 1.0)
        gaps.append({
            "id": m.uid(), "cq_id": cq.get("id"), "kind": kind, "ref": ref,
            "note": note, "severity": sev, "business_impact": impact,
            "cq_weight": weight,
            "gap_priority": round(impact * weight * sev, 2),
            "action": action,
        })

    if not exec_out.get("total_ids"):
        add("entity", cq.get("subject_type") or "",
            "主体实体在库中不存在", {"kind": "extract_entity", "label": "实体抽取与对齐"})
    for rel in cq.get("required_relations") or []:
        if registry and not registry.validate_ref("relation", rel):
            add("schema", rel, "关系类型未登记", {"kind": "schema_proposal", "label": "Schema 提案"})
        elif not any(v.get("path") or v.get("predicate") == rel
                     for v in exec_out.get("values") or []):
            add("relation", rel, "两端实体存在但关系无实例",
                {"kind": "build_relation", "label": "事实审核与关系构建"})
    for attr in cq.get("required_attributes") or []:
        if not any(v.get("predicate") == attr for v in exec_out.get("values") or []):
            add("attribute", attr, "必要属性未抽取",
                {"kind": "add_attribute", "label": "补充属性抽取"})
    if not validation.get("evidence_ok"):
        add("evidence", "min_evidence", "证据不足/过期/低可信",
            {"kind": "rebind_evidence", "label": "重新检索并补充证据"})
    if dims.get("schema", 1) < 1:
        add("schema", "unregistered", "本体缺少概念或约束",
            {"kind": "schema_proposal", "label": "Schema Proposal"})
    # 数据源/授权缺口独立归类；只检查本 CQ 声明需要的数据源，避免噪声
    declared = set(m.as_dict(cq.get("scope_constraints")).get("sources") or [])
    status = snap.get("source_status") or {}
    for src in sorted(declared):
        state = status.get(src, "unknown")
        if state in ("not_connected", "unauthorized", "sync_failed", "expired", "unknown"):
            add("source_access", src, f"数据源状态：{state}",
                {"kind": "request_access", "label": "数据接入 / 权限申请"})
    return sorted(gaps, key=lambda g: g["gap_priority"], reverse=True)


# ---------------------------------------------------------------- 单条执行编排
def run_cq(cq: dict, plan: dict, snap: dict, registry=None,
           expected: dict | None = None, permission_ctx: dict | None = None) -> dict:
    """四执行器 → 答案验证 → 五维覆盖 → 五态 → 六类缺口。"""
    ctx = permission_ctx or {}
    outs = {
        "fact": fact_executor(cq, plan, snap),
        "graph": graph_executor(cq, plan, snap),
        "temporal": temporal_executor(cq, plan, snap),
        "retrieval": retrieval_executor(cq, plan, snap),
    }
    chain = plan.get("executor_chain") or ["fact", "retrieval", "validator"]
    merged = {"values": [], "covered_ids": [], "total_ids": 0,
              "evidence_refs": [], "as_of": "current", "stale_count": 0}
    for name in chain:
        o = outs.get(name)
        if not o:
            continue
        merged["values"].extend(o.get("values") or [])
        for cid in o.get("covered_ids") or []:
            if cid not in merged["covered_ids"]:
                merged["covered_ids"].append(cid)
        if name != "retrieval":
            merged["total_ids"] = max(merged["total_ids"], int(o.get("total_ids") or 0))
        merged["evidence_refs"].extend(o.get("evidence_refs") or [])
        if o.get("as_of") and o["as_of"] != "current":
            merged["as_of"] = o["as_of"]
            merged["stale_count"] = o.get("stale_count", 0)
    validation = answer_validator(cq, merged, snap, ctx)
    dims = five_dimension_coverage(cq, merged, validation, registry, expected)
    state = decide_state(dims, validation, merged)
    gaps = detect_gaps(cq, merged, validation, dims, registry, snap)
    return {
        "cq_id": cq.get("id"), "code": cq.get("code"),
        "state": state, "state_label": m.STATE_LABELS[state],
        "dimensions": {k: v for k, v in dims.items() if k in m.COVERAGE_DIMENSIONS},
        "answer_basis": dims.get("answer_basis"),
        "weighted_score": round(sum(dims[k] for k in m.COVERAGE_DIMENSIONS) / 5, 3),
        "cq_weight": float(cq.get("cq_weight") or 1.0),
        "risk_level": cq.get("risk_level", "normal"),
        "priority": cq.get("priority", 2),
        "values": merged["values"][:50], "covered": len(merged["covered_ids"]),
        "total": merged["total_ids"], "validation": validation,
        "gaps": gaps,
        "provenance": {
            "plan_version": plan.get("plan_version"),
            "schema_version": plan.get("schema_version"),
            "cq_version": cq.get("version"),
            "data_snapshot_id": snap.get("data_snapshot_id"),
            "model_version": ctx.get("model_version", ""),
            "evidence_refs": merged["evidence_refs"],
        },
    }
