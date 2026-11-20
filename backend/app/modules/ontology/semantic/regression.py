"""CQ 回归集（V1.3 语义层方案 · 能力 6 · 验收 4）。

「至少一个核心 CQ 回归集可重复执行，结果稳定」：
- 回归集 = 若干 (cq_id, parameters) 用例；
- 每次执行逐条跑确定性 `_execute_core`，把有序 (fingerprint) 汇总成集合指纹；
- 与上一次同 set_id 的指纹比对 → stable；结果落 cq_regression_runs 可追溯。

设计：回归执行直接走确定性核心（不写幂等 run、不引入时间戳），因此同一
schema + 同一已授权数据下重复执行指纹必然一致；这正好度量「结果稳定」。
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import store, versioning
from .cq_executor import _execute_core, _fingerprint, _load_cq, _authorized_data, build_server_context

# 内置核心回归集（用真实种子 CQ-001，查询全部是确定性、无数据源也可跑）
CORE_SET_ID = "core-semantic-v1"
CORE_CASES = [
    {"cq_id": "CQ-001", "parameters": {"query": "schema_snapshot"}},
    {"cq_id": "CQ-001", "parameters": {"query": "type_hierarchy",
                                        "parameters": {"type": "Person"}}},
    {"cq_id": "CQ-001", "parameters": {"query": "constraint_check",
                                        "parameters": {"kind": "instance",
                                                       "target": {"type_name": "Employee",
                                                                  "properties": {"employee_no": "E10001"}}}}},
]


def ensure_core_set() -> dict:
    """启动/维护时播种内置核心回归集（幂等）。"""
    store.db.execute(
        "INSERT INTO ontology.cq_regression_sets (set_id, tenant_id, name, cases) "
        "VALUES (:id, 'default', :n, CAST(:c AS jsonb)) "
        "ON CONFLICT (set_id) DO NOTHING",
        {"id": CORE_SET_ID, "n": "语义层核心回归集（Schema/继承/约束）",
         "c": store.jdump(CORE_CASES)})
    return get_set(CORE_SET_ID)


def upsert_set(set_id: str, name: str, cases: list[dict],
               tenant_id: str = "default") -> dict:
    if not cases:
        raise ValueError("回归集 cases 不能为空")
    store.db.execute(
        "INSERT INTO ontology.cq_regression_sets (set_id, tenant_id, name, cases) "
        "VALUES (:id, :t, :n, CAST(:c AS jsonb)) "
        "ON CONFLICT (set_id) DO UPDATE SET name = EXCLUDED.name, cases = EXCLUDED.cases",
        {"id": set_id, "t": tenant_id, "n": name, "c": store.jdump(cases)})
    return get_set(set_id)


def get_set(set_id: str) -> dict | None:
    row = store.db.fetch_one(
        "SELECT set_id, name, cases, created_at FROM ontology.cq_regression_sets "
        "WHERE set_id = :id", {"id": set_id})
    return store._decode(row) if row else None


def list_sets() -> list[dict]:
    rows = store.db.fetch_all(
        "SELECT set_id, name, cases FROM ontology.cq_regression_sets ORDER BY created_at DESC")
    return [{**r, "case_count": len(store.jload(r["cases"], []) or [])}
            for r in rows]


def run_set(set_id: str, *, principal: dict | None = None,
            schema: dict | None = None,
            task_snapshot_id: str | None = None,
            tenant_id: str = "default") -> dict:
    """执行回归集：逐条确定性执行 → 汇总指纹 → 与上次比对稳定 → 落库。"""
    s = get_set(set_id)
    if not s:
        raise LookupError(f"回归集 {set_id} 不存在")
    cases = store.jload(s["cases"], []) or []
    built = build_server_context(principal, None, schema=schema,
                                 task_snapshot_id=task_snapshot_id)
    ctx = built["context"]
    ctx["schema"] = schema or store.load_semantic_schema() or {}
    ov = ctx["ontology_version"]

    instances, relations = _authorized_data(tenant_id, ctx["space_id"], principal or {})
    case_results: list[dict] = []
    for case in cases:
        cq_id = case.get("cq_id") or "CQ-001"
        try:
            cq = _load_cq(cq_id)
        except LookupError:
            # 未登记的 CQ 也如实记录，不编造答案
            case_results.append({"cq_id": cq_id, "state": "unanswerable",
                                 "reason": "CQ 未登记", "fingerprint": ""})
            continue
        core = _execute_core(ctx["schema"], cq, case.get("parameters") or {},
                             instances, relations, ctx)
        case_results.append({"cq_id": cq_id, "state": core["state"],
                             "fingerprint": _fingerprint(core)})
    combined = hashlib.sha256(json.dumps(
        [c["fingerprint"] for c in case_results], ensure_ascii=False
    ).encode("utf-8")).hexdigest()

    prev = store.db.fetch_one(
        "SELECT result_fingerprint FROM ontology.cq_regression_runs "
        "WHERE set_id = :s AND tenant_id = :t ORDER BY created_at DESC LIMIT 1",
        {"s": set_id, "t": tenant_id})
    prev_fp = prev["result_fingerprint"] if prev else None
    stable = bool(prev_fp) and prev_fp == combined
    run_id = str(uuid.uuid4())
    store.db.execute(
        "INSERT INTO ontology.cq_regression_runs "
        "(id, set_id, tenant_id, ontology_version, result_fingerprint, stable, case_count) "
        "VALUES (CAST(:id AS uuid), :s, :t, :ov, :fp, :st, :n)",
        {"id": run_id, "s": set_id, "t": tenant_id, "ov": ov,
         "fp": combined, "st": stable, "n": len(case_results)})
    return {
        "set_id": set_id, "run_id": run_id, "case_count": len(case_results),
        "result_fingerprint": combined,
        "previous_fingerprint": prev_fp,
        "stable": stable,
        "baseline_established": prev_fp is None,
        "ontology_version": ov,
        "cases": case_results,
        "note": ("首次执行建立基线，stable=False 属预期；再次执行指纹一致即 stable=True"
                 if prev_fp is None else
                 "与上次指纹一致，结果稳定" if stable
                 else "与上次指纹不一致：Schema/数据/规则发生变化，需人工复核"),
    }


def list_runs(set_id: str | None = None, tenant_id: str = "default",
              limit: int = 30) -> list[dict]:
    where = "AND set_id = :s" if set_id else ""
    p = {"t": tenant_id, "lim": limit}
    if set_id:
        p["s"] = set_id
    rows = store.db.fetch_all(
        "SELECT id, set_id, ontology_version, result_fingerprint, stable, "
        " case_count, created_at FROM ontology.cq_regression_runs "
        "WHERE tenant_id = :t " + where + " ORDER BY created_at DESC LIMIT :lim", p)
    return [{**r, "id": str(r["id"]), "created_at": str(r.get("created_at") or "")}
            for r in rows]
