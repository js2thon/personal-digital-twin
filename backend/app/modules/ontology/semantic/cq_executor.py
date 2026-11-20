"""CQ 工程化执行（V1.3 语义层方案 · 能力 3 · 验收 1/4/5）。

补齐方案「必须完成」的四条：
  1) 生产禁止 MemoryStore —— 全部走 PostgreSQL（沿用 cq_v2.store 闸门，
     本模块只落 PG；startup fail-fast 已存在，不重复造）；
  2) 完整 CRUD + 启动健康检查 —— store.ensure_schema / ping / counts；
  3) 执行结果持久化 + 幂等键 —— cq_semantic_runs + cq_idempotency；
  4) 权限与 DataSpace 过滤 + 评估体系打通 —— policy.filter_rows + 回归集。

**强制服务端上下文**：tenant / identity / space / ontology_version / policy
一律由 `build_server_context()` 从全局中间件注入的 principal 与语义上下文
派生；请求体里若伪造这些字段，会被显式剥离并在结果中如实披露，不参与判定。

**确定性**：核心 `_execute_core` 是纯函数（答案只由 schema + 已授权数据 +
parameters 决定，不含时间戳/随机数/LLM），保证「同键同结果」与「回归稳定」。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from . import store, versioning
from .constraints import validate_instance, validate_relation
from .reasoning import (descendants, expand as reasoning_expand,
                        instances_matching_type)
from .rules import apply_rules

# 请求体里这些字段一律视为「客户端无权指定」，服务端覆盖 + 披露
_FORBIDDEN_BODY_KEYS = ("tenant_id", "user_id", "actor", "role", "scope",
                        "space_id", "ontology_version", "schema_version",
                        "policy", "permission_context", "principal")


def build_server_context(principal: dict | None,
                         semantic_context: dict | None,
                         *, schema: dict | None = None,
                         task_snapshot_id: str | None = None,
                         body: dict | None = None) -> dict:
    """从服务端可信来源构造上下文；返回 (context, 被剥离的伪造字段列表)。"""
    p = principal or {}
    sc = semantic_context or {}
    stripped = [k for k in _FORBIDDEN_BODY_KEYS if k in (body or {})]

    cur = versioning.current_version()
    ov = cur["ontology_version"]
    sv = cur["schema_version"]
    bound = False
    if task_snapshot_id:
        try:
            frozen = versioning.resolve(task_snapshot_id, str(p.get("tenant_id") or "default"))
            ov, sv, bound = frozen["ontology_version"], frozen["schema_version"], True
        except LookupError:
            pass  # 未找到快照时回落当前 published，如实标注 bound=False

    ctx = {
        "tenant_id": str(p.get("tenant_id") or "default"),
        "user_id": str(p.get("user_id") or ""),
        "actor": str(p.get("actor") or sc.get("actor") or "owner"),
        "role": str(p.get("role") or (sc.get("permission_context") or {}).get("role") or "owner"),
        "scope": list(p.get("scope") or (sc.get("permission_context") or {}).get("scope") or []),
        "space_id": str(sc.get("space_id") or p.get("space_id") or "default"),
        "ontology_version": ov,
        "schema_version": sv,
        "schema": schema or {},
        "policy": {"engine": "app/policy.decide", "fail_closed": True,
                   "data_space_filter": True},
        "task_snapshot_id": task_snapshot_id,
        "version_bound": bound,
        "source": "global_middleware",
    }
    return {"context": ctx, "stripped_body_keys": stripped,
            "body_trusted": False,
            "note": "上下文全部由服务端注入；" + (
                "已剥离请求体伪造字段 " + ", ".join(stripped) if stripped
                else "请求体未含越权字段")}


def _public_meta(built: dict) -> dict:
    """只暴露上下文自证字段，不把含完整 schema 的 context 塞进响应。"""
    return {"stripped_body_keys": built["stripped_body_keys"],
            "body_trusted": built["body_trusted"],
            "note": built["note"], "context_source": built["context"]["source"]}


# ---------------------------------------------------------------- 确定性查询
def _evidence(rows: list[dict], key: str = "id", limit: int = 20) -> list[str]:
    return [str(r.get(key)) for r in rows[:limit] if r.get(key) is not None]


def _execute_core(schema: dict, cq: dict, parameters: dict,
                  instances: list[dict], relations: list[dict],
                  server_ctx: dict) -> dict:
    """纯确定性执行：答案只由已授权数据 + schema + parameters 决定。"""
    query = (parameters.get("query") or "overview").strip()
    params = parameters.get("parameters") or {}
    answer: Any = None
    evidence: list[str] = []
    state = "answered"

    if query == "type_hierarchy":
        t = (params.get("type") or cq.get("subject_type") or "Person").strip()
        answer = {"type": t, "ancestors": _ancestors(schema, t),
                  "descendants": descendants(schema, t)}
    elif query == "instances_by_type":
        t = (params.get("type") or cq.get("subject_type") or "Person").strip()
        rows = instances_matching_type(schema, instances, t)
        answer = {"type": t, "count": len(rows),
                  "instances": [{"id": r.get("id"), "name": r.get("canonical_name"),
                                 "type": r.get("type_name")} for r in rows[:50]]}
        evidence = _evidence(rows)
        if not rows:
            state = "unanswerable"
    elif query == "relations_by_type":
        rt = (params.get("relation_type") or "").strip()
        rows = [r for r in relations if not rt or r.get("relation_type") == rt]
        answer = {"relation_type": rt or "*", "count": len(rows),
                  "relations": [{"id": r.get("id"), "source": r.get("source_id"),
                                 "target": r.get("target_id"),
                                 "type": r.get("relation_type"),
                                 "derived": bool(r.get("derived"))} for r in rows[:50]]}
        evidence = _evidence(rows)
        if not rows:
            state = "unanswerable"
    elif query == "reasoning_expand":
        asserted = [r for r in relations if not r.get("derived")]
        answer = reasoning_expand(schema, asserted,
                                  max_depth=params.get("max_depth"))
        evidence = _evidence(asserted)
    elif query == "constraint_check":
        target = params.get("target") or {}
        kind = params.get("kind") or "instance"
        report = (validate_instance(schema, target) if kind == "instance"
                  else validate_relation(schema, target))
        answer = {"kind": kind, "report": report}
    elif query == "schema_snapshot":
        answer = {"schema_version": schema.get("schema_version"),
                  "ontology_version": schema.get("ontology_version"),
                  "entity_types": sorted((schema.get("entity_types") or {}).keys()),
                  "relation_types": sorted((schema.get("relation_types") or {}).keys()),
                  "business_rules": len(schema.get("business_rules") or [])}
    else:  # overview：真实数据概览，不编造指标
        answer = {
            "cq_code": cq.get("code"), "question": cq.get("question"),
            "authorized_instances": len(instances),
            "authorized_relations": len(relations),
            "ontology_version": server_ctx["ontology_version"],
            "schema_version": server_ctx["schema_version"],
        }
        if not instances and not relations:
            state = "unanswerable"

    # 规则匹配结果进入 Evidence/Applied Rules（不改写数据）
    rule_hit = apply_rules(schema.get("business_rules") or [],
                           {"operation": "cq_execute", "query": query,
                            "cq_code": cq.get("code"),
                            "relation_type": params.get("relation_type"),
                            "visibility": "organization"})
    return {
        "state": state, "answer": answer, "evidence_refs": evidence,
        "applied_rules": rule_hit["applied_rules"],
        "derived_candidates": rule_hit["derived_candidates"],
        "query": query,
    }


def _ancestors(schema: dict, t: str) -> list[str]:
    from .constraints import ancestors
    return ancestors(schema, t)


def _fingerprint(core: dict) -> str:
    """核心结果的规范化指纹（不含 run_id / 时间戳），用于回归稳定性比对。"""
    canon = json.dumps(core, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 数据获取（带权限）
def _authorized_data(tenant_id: str, space_id: str,
                     principal: dict) -> tuple[list[dict], list[dict]]:
    from app import policy as pol

    inst = store.list_instances(tenant_id, "space_id = :sp", {"sp": space_id}, limit=1000)
    rels = store.list_relations(tenant_id, "space_id = :sp", {"sp": space_id}, limit=2000)
    # 逐行过 ABAC（与读路径同一套 decide 规则）；语义表默认 organization 可见
    inst = [dict(r, visibility=r.get("visibility") or "organization",
                 created_by=r.get("created_by") or "system")
            for r in inst]
    rels = [dict(r, visibility=r.get("visibility") or "organization",
                 created_by=r.get("created_by") or "system")
            for r in rels]
    return (pol.filter_rows(inst, principal), pol.filter_rows(rels, principal))


def _load_cq(cq_id: str) -> dict:
    """CQ 元数据来自真实 competency_questions 表（code 或 uuid 均可）。"""
    row = store.db.fetch_one(
        "SELECT id, code, question, subject_type, intent FROM ontology.competency_questions "
        "WHERE code = :c OR id::text = :c LIMIT 1", {"c": cq_id})
    if not row:
        raise LookupError(f"CQ {cq_id} 不存在（competency_questions 无此记录）")
    return {"id": str(row["id"]), "code": row["code"],
            "question": row.get("question"), "subject_type": row.get("subject_type"),
            "intent": row.get("intent")}


# ---------------------------------------------------------------- 对外执行
def execute(cq_id: str, parameters: dict, idempotency_key: str | None,
            *, principal: dict | None = None,
            semantic_context: dict | None = None,
            schema: dict | None = None,
            task_snapshot_id: str | None = None,
            client_body: dict | None = None) -> dict:
    """执行一条 CQ：服务端上下文 → 幂等 → 确定性执行 → 持久化 → 决策 Trace。

    返回结构稳定：同 (tenant, cq_id, key) 重放返回首次的字节级一致 result，
    并带 replay 标记。client_body 是原始请求体（含顶层伪造字段），仅用于
    「越权字段剥离」自证，绝不参与上下文构造。
    异常：LookupError（CQ 不存在）、RuntimeError（库不可用）。
    """
    if not store.ping():
        raise RuntimeError("PostgreSQL 不可用：CQ 执行拒绝降级到内存态（验收 1）")
    # 顶层伪造字段 + parameters 内的伪造字段都纳入检测（双覆盖）
    scan_body = dict(client_body or {})
    scan_body.update(parameters or {})
    built = build_server_context(principal, semantic_context, schema=schema,
                                 task_snapshot_id=task_snapshot_id,
                                 body=scan_body)
    ctx = built["context"]
    ctx["schema"] = schema or store.load_semantic_schema() or {}
    tenant = ctx["tenant_id"]
    key = (idempotency_key or "").strip() or hashlib.sha256(
        json.dumps({"c": cq_id, "p": parameters}, sort_keys=True,
                   default=str, ensure_ascii=False).encode("utf-8")).hexdigest()[:40]

    # 幂等命中：直接返回首次结果（不改库、不新增 run）
    hit = store.db.fetch_one(
        "SELECT run_id, result FROM ontology.cq_idempotency "
        "WHERE tenant_id = :t AND cq_id = :c AND idempotency_key = :k",
        {"t": tenant, "c": cq_id, "k": key})
    if hit:
        result = store.jload(hit["result"], {})
        return {**result, "idempotent_replay": True, **_public_meta(built),
                "idempotency_key": key}

    cq = _load_cq(cq_id)
    instances, relations = _authorized_data(tenant, ctx["space_id"], principal or {})
    core = _execute_core(ctx["schema"], cq, parameters, instances, relations, ctx)

    run_id = f"run-{uuid.uuid4().hex[:20]}"
    result = {
        "run_id": run_id, "cq_id": cq_id, "cq_code": cq.get("code"),
        "state": core["state"], "answer": core["answer"],
        "evidence_refs": core["evidence_refs"],
        "applied_rules": core["applied_rules"],
        "derived_candidates": core["derived_candidates"],
        "query": core["query"],
        "ontology_version": ctx["ontology_version"],
        "schema_version": ctx["schema_version"],
        "space_id": ctx["space_id"],
        "version_bound": ctx["version_bound"],
        "task_snapshot_id": task_snapshot_id,
        "server_context": {k: ctx[k] for k in
                           ("tenant_id", "user_id", "actor", "role", "space_id",
                            "ontology_version", "schema_version", "policy",
                            "version_bound", "source")},
        "deterministic": True,
        "idempotency_key": key, "idempotent_replay": False,
        "fingerprint": _fingerprint(core),
    }

    with store.db.transaction():
        store.db.execute(
            "INSERT INTO ontology.cq_semantic_runs "
            "(run_id, tenant_id, cq_id, ontology_version, schema_version, space_id, "
            " parameters, server_context, state, answer, evidence_refs, applied_rules, "
            " version_bound, idempotency_key, created_by) VALUES "
            "(:rid, :t, :c, :ov, :sv, :sp, CAST(:p AS jsonb), CAST(:ctx AS jsonb), "
            " :st, CAST(:ans AS jsonb), CAST(:ev AS jsonb), CAST(:rules AS jsonb), :vb, :k, :by)",
            {"rid": run_id, "t": tenant, "c": cq_id,
             "ov": ctx["ontology_version"], "sv": ctx["schema_version"],
             "sp": ctx["space_id"], "p": store.jdump(parameters),
             "ctx": store.jdump(result["server_context"]), "st": core["state"],
             "ans": store.jdump(core["answer"]), "ev": store.jdump(core["evidence_refs"]),
             "rules": store.jdump(core["applied_rules"]),
             "vb": bool(ctx["version_bound"]), "k": key,
             "by": ctx["actor"]})
        store.db.execute(
            "INSERT INTO ontology.cq_idempotency "
            "(idempotency_key, tenant_id, cq_id, run_id, result) "
            "VALUES (:k, :t, :c, :rid, CAST(:r AS jsonb)) "
            "ON CONFLICT (tenant_id, cq_id, idempotency_key) DO NOTHING",
            {"k": key, "t": tenant, "c": cq_id, "rid": run_id,
             "r": store.jdump(result)})
        # 决策 Trace：展示应用了哪些本体规则 / CQ 结果（验收 5）
        store.db.execute(
            "INSERT INTO ontology.decision_rule_trace "
            "(run_id, tenant_id, cq_id, ontology_version, applied_rules, cq_results, "
            " derived_candidates) VALUES (:rid, :t, :c, :ov, CAST(:ar AS jsonb), "
            " CAST(:cr AS jsonb), CAST(:dc AS jsonb)) "
            "ON CONFLICT (run_id) DO NOTHING",
            {"rid": run_id, "t": tenant, "c": cq_id,
             "ov": ctx["ontology_version"], "ar": store.jdump(core["applied_rules"]),
             "cr": store.jdump({"cq_code": cq.get("code"), "state": core["state"],
                                "answer": core["answer"]}),
             "dc": store.jdump(core["derived_candidates"])})

    return {**result, **_public_meta(built), "idempotency_key": key}


def get_run(run_id: str, tenant_id: str = "default") -> dict | None:
    row = store.db.fetch_one(
        "SELECT * FROM ontology.cq_semantic_runs WHERE run_id = :r AND tenant_id = :t",
        {"r": run_id, "t": tenant_id})
    return store._decode(row) if row else None


def decision_trace(run_id: str, tenant_id: str = "default") -> dict | None:
    row = store.db.fetch_one(
        "SELECT * FROM ontology.decision_rule_trace WHERE run_id = :r AND tenant_id = :t",
        {"r": run_id, "t": tenant_id})
    return store._decode(row) if row else None


def list_runs(tenant_id: str = "default", limit: int = 50) -> list[dict]:
    try:
        rows = store.db.fetch_all(
            "SELECT run_id, cq_id, state, ontology_version, version_bound, "
            " idempotency_key, created_at FROM ontology.cq_semantic_runs "
            "WHERE tenant_id = :t ORDER BY created_at DESC LIMIT :lim",
            {"t": tenant_id, "lim": limit})
    except Exception:  # noqa: BLE001  存量表未补列时降级查询（不因展示列 500）
        rows = store.db.fetch_all(
            "SELECT run_id, cq_id, state, ontology_version, "
            " idempotency_key, created_at FROM ontology.cq_semantic_runs "
            "WHERE tenant_id = :t ORDER BY created_at DESC LIMIT :lim",
            {"t": tenant_id, "lim": limit})
    return [{**r, "version_bound": bool(r.get("version_bound", False)),
             "created_at": str(r.get("created_at") or "")} for r in rows]
