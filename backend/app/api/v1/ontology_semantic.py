"""Ontology 语义层增强 API（V1.3 语义层方案 · Phase A/B/C，全新增量路由）。

所有端点挂在 /ontology/semantic/* 下，与既有 /ontology/*、/ontology/v2/* 零冲突。
- 写端点：复用 cq_v2.require_owner（SIL-A07 fail-closed）+ 全局中间件 principal；
- 读端点：principal 来自中间件，行级过滤走 app/policy.filter_rows；
- CQ execute：强制服务端上下文（build_server_context 剥离请求体伪造字段），
  按权限过滤，幂等键，结果持久化，决策 Trace。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.modules.ontology.semantic import (alignment, metrics, regression,
                                            store, versioning)
from app.modules.ontology.semantic import constraints as C
from app.modules.ontology.semantic import reasoning as R
from app.modules.ontology.semantic import rules as RU
from app.modules.ontology.semantic import schema_def as SD
from app.modules.ontology.semantic import service as SEM
from app.modules.ontology.semantic.constraints import ConstraintViolation

router = APIRouter()


# ---------------------------------------------------------------- 基础设施
def _ensure() -> None:
    """惰性初始化：按库可查（不用进程内标志，避免误判），缺表则幂等建表+播种。"""
    try:
        row = store.db.fetch_one(
            "SELECT 1 AS ok FROM ontology.semantic_schemas "
            "WHERE status = 'published' LIMIT 1")
        if row:
            return
    except Exception:  # noqa: BLE001
        pass
    SEM.ensure_ready()


def _principal(request: Request) -> dict:
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, dict) else {}


def _require_owner(request: Request) -> dict:
    """语义本体写操作的通用权限闸门，不依赖 CQ 子系统。"""
    from app.policy import is_owner_class

    principal = _principal(request)
    if not principal or not is_owner_class(principal):
        raise HTTPException(403, "本体治理写操作要求 owner 级主体")
    return principal


def _semantic_context(request: Request, space_id: str | None = None) -> dict:
    c = getattr(getattr(request, "state", None), "semantic_context", None)
    out: dict = {}
    if c is not None:
        for k in ("tenant_id", "user_id", "actor", "request_id", "trace_id",
                  "schema_version", "permission_context"):
            v = getattr(c, k, None) if not isinstance(c, dict) else c.get(k)
            if v is not None:
                out[k] = v
        if not isinstance(c, dict) and hasattr(c, "permission_context"):
            out["permission_context"] = c.permission_context
    # space_id 只能来自服务端可信 query（DataSpace 选择），不接受 body 注入
    out["space_id"] = space_id or "default"
    return out


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ConstraintViolation as exc:
        return JSONResponse(status_code=400, content={
            "detail": str(exc), "error": "constraint_violation",
            "violations": (exc.report or {}).get("violations", [])})
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None


def _schema(request: Request) -> dict:
    _ensure()
    return SEM.load_schema()


# ---------------------------------------------------------------- Payload
class InstancePayload(BaseModel):
    id: str = ""                 # 带 id = 更新模式（触发状态机 old→new 校验）；空 = 新建
    type_name: str
    canonical_name: str = ""
    properties: dict = {}
    source_fact_ids: list[str] = []
    space_id: str = "default"


class RelationPayload(BaseModel):
    source_id: str
    relation_type: str
    target_id: str
    source_type: str = ""
    target_type: str = ""
    auto_reasoning: bool = True
    source_fact_ids: list[str] = []   # 证据回链：派生自哪些 facts


class RulePayload(BaseModel):
    name: str
    rule_type: str = "integrity"
    condition: dict = {}
    action: dict = {}
    priority: int = 100
    enabled: bool = True


class EdgesPayload(BaseModel):
    edges: list[dict] = []
    max_depth: int | None = None


class ValidatePayload(BaseModel):
    kind: str = "instance"           # instance | relation
    target: dict = {}


class FreezePayload(BaseModel):
    task_id: str


class RegressionSetPayload(BaseModel):
    set_id: str
    name: str
    cases: list[dict] = []


# ---------------------------------------------------------------- 读：健康/Schema
@router.get("/ontology/semantic/health")
def health(request: Request):
    _ensure()
    cur = versioning.current_version()
    return {"service": "ontology-semantic", "durable": True, "store": "postgres",
            "db_ok": store.ping(), "counts": store.counts(),
            "ontology_version": cur["ontology_version"],
            "schema_version": cur["schema_version"]}


@router.get("/ontology/semantic/schema")
def get_schema(request: Request):
    _ensure()
    s = SEM.load_schema()
    ent = s.get("entity_types") or {}
    rel = s.get("relation_types") or {}
    inheritance = [{k: (v or {}).get("parent", "") for k, v in ent.items()
                    if (v or {}).get("parent")}]
    characteristics = {k: (v or {}).get("characteristics", {})
                       for k, v in rel.items()
                       if (v or {}).get("characteristics") and
                       any((v.get("characteristics") or {}).get(x)
                           for x in ("symmetric", "transitive", "inverse_of"))}
    return {"schema_version": s.get("schema_version"),
            "ontology_version": s.get("ontology_version"),
            "source": s.get("source"),
            "entity_types": sorted(ent.keys()), "relation_types": sorted(rel.keys()),
            "inheritance": inheritance, "characteristics": characteristics,
            "business_rules": len(s.get("business_rules") or []),
            "constraints": s.get("constraints", {}),
            "versions": store.list_schema_versions()}


@router.get("/ontology/semantic/schema/json-schema")
def definition_json_schema(request: Request):
    """导出「Schema 定义本身」的 JSON Schema（Schema-as-Code 契约）。"""
    return {"json_schema": SD.to_json_schema(),
            "note": "Pydantic 2 model_json_schema() 生成，可用于外部契约校验"}


@router.get("/ontology/semantic/schema/instance/{type_name}")
def instance_schema(type_name: str, request: Request):
    _ensure()
    s = SEM.load_schema()
    if type_name not in (s.get("entity_types") or {}):
        raise HTTPException(404, f"实体类型 {type_name} 未定义")
    return SD.instance_json_schema(s, type_name)


@router.get("/ontology/semantic/inheritance")
def inheritance(request: Request):
    _ensure()
    s = SEM.load_schema()
    out = {}
    for name in (s.get("entity_types") or {}):
        out[name] = {"ancestors": C.ancestors(s, name),
                     "descendants": R.descendants(s, name)}
    return {"inheritance": out}


# ---------------------------------------------------------------- 读：实例/关系/违规
@router.get("/ontology/semantic/instances")
def list_instances(request: Request, limit: int = 200):
    _ensure()
    from app import policy as pol
    p = _principal(request)
    rows = store.list_instances(str(p.get("tenant_id") or "default"), limit=limit)
    rows = [dict(r, visibility="organization", created_by=r.get("created_by") or "system")
            for r in rows]
    return {"items": pol.filter_rows(rows, p), "total": len(rows)}


@router.get("/ontology/semantic/relations")
def list_relations(request: Request, limit: int = 500):
    _ensure()
    from app import policy as pol
    p = _principal(request)
    rows = store.list_relations(str(p.get("tenant_id") or "default"), limit=limit)
    rows = [dict(r, visibility="organization", created_by=r.get("created_by") or "system")
            for r in rows]
    return {"items": pol.filter_rows(rows, p), "total": len(rows)}


@router.post("/ontology/semantic/relations/derive-from-facts")
def derive_relations_from_facts(payload: dict, request: Request):
    """facts → 语义关系派生：默认 dry-run 只返回候选；commit=true 才落库。

    仅派生「主体与对象都能精确对齐到已落库实例」的证据型关系，
    携带 source_fact_ids 回链；解析不到的进 unresolved 照实返回，不编造。
    """
    _ensure()
    p = _require_owner(request)
    from app.modules.ontology.semantic import fact_derive
    return fact_derive.derive(tenant_id=str(p.get("tenant_id") or "default"),
                              commit=bool(payload.get("commit")))


@router.get("/ontology/semantic/violations")
def violations(request: Request):
    """验收 6 后端数据：类型违规边清单（前端据此红色高亮）。"""
    _ensure()
    p = _principal(request)
    svc = SEM.OntologyService(tenant_id=str(p.get("tenant_id") or "default"),
                              actor=str(p.get("actor") or "system"))
    return svc.violations()


# ---------------------------------------------------------------- 读：推理/规则/约束校验
@router.post("/ontology/semantic/reasoning/expand")
def reasoning_expand(payload: EdgesPayload, request: Request):
    _ensure()
    return R.expand(SEM.load_schema(), payload.edges, max_depth=payload.max_depth)


@router.post("/ontology/semantic/constraints/validate")
def constraints_validate(payload: ValidatePayload, request: Request):
    _ensure()
    s = SEM.load_schema()
    if payload.kind == "instance":
        rep = C.validate_instance(s, payload.target)
    elif payload.kind == "relation":
        rep = C.validate_relation(s, payload.target)
    else:
        raise HTTPException(400, "kind 须为 instance / relation")
    return rep


@router.get("/ontology/semantic/rules")
def list_rules(request: Request):
    _ensure()
    p = _principal(request)
    return {"items": store.list_rules(str(p.get("tenant_id") or "default")),
            "schema_rules": len((SEM.load_schema().get("business_rules") or []))}


@router.post("/ontology/semantic/rules/trigger")
def rules_trigger(payload: dict, request: Request):
    """规则触发（纯计算，不写库）：结果含 Applied Rules / 衍生候选。"""
    _ensure()
    ctx = payload.get("context") or {}
    rules = payload.get("rules") or (SEM.load_schema().get("business_rules") or [])
    return RU.apply_rules(rules, ctx, SEM.load_schema())


# ---------------------------------------------------------------- 写：统一入口
@router.post("/ontology/semantic/instances")
def write_instance(payload: InstancePayload, request: Request):
    _ensure()
    p = require_owner(request)
    svc = SEM.OntologyService(tenant_id=str(p.get("tenant_id") or "default"),
                              actor=str(p.get("actor") or "system"))
    return _call(svc.write_instance, payload.model_dump())


@router.post("/ontology/semantic/relations")
def write_relation(payload: RelationPayload, request: Request):
    """验收 2：不符合类型约束的关系被拒绝，返回 400 + 逐条违规。"""
    _ensure()
    p = require_owner(request)
    svc = SEM.OntologyService(tenant_id=str(p.get("tenant_id") or "default"),
                              actor=str(p.get("actor") or "system"))
    d = payload.model_dump()
    auto = d.pop("auto_reasoning", True)
    return _call(svc.write_relation, d, auto_reasoning=auto)


@router.post("/ontology/semantic/rules")
def register_rule(payload: RulePayload, request: Request):
    _ensure()
    p = require_owner(request)
    d = payload.model_dump()
    check = RU.validate_rule_payload(d)
    if not check["valid"]:
        raise HTTPException(400, "; ".join(check["errors"]))
    store.upsert_rule(d, tenant_id=str(p.get("tenant_id") or "default"))
    return {"created": True, "rule": d}


# ---------------------------------------------------------------- 版本冻结（验收 3）
@router.post("/ontology/semantic/snapshots/freeze")
def freeze_snapshot(payload: FreezePayload, request: Request):
    _ensure()
    p = require_owner(request)
    schema = SEM.load_schema()
    context = {
        **_semantic_context(request),
        "ontology_version": versioning.current_version()["ontology_version"],
        "schema_version": schema.get("schema_version"),
        "policy": {"engine": "app/policy.decide", "fail_closed": True},
        "source": "global_middleware",
    }
    return versioning.freeze(payload.task_id, schema,
                             tenant_id=str(p.get("tenant_id") or "default"),
                             server_context=context,
                             created_by=str(p.get("actor") or "system"))


@router.get("/ontology/semantic/snapshots")
def list_snapshots(request: Request):
    _ensure()
    p = _principal(request)
    return {"items": versioning.list_snapshots(str(p.get("tenant_id") or "default"))}


@router.get("/ontology/semantic/snapshots/{task_id}")
def resolve_snapshot(task_id: str, request: Request):
    _ensure()
    p = _principal(request)
    return _call(versioning.resolve, task_id, str(p.get("tenant_id") or "default"))


# ---------------------------------------------------------------- 回归集（验收 4）
@router.get("/ontology/semantic/regression/sets")
def regression_sets(request: Request):
    _ensure()
    return {"items": regression.list_sets(), "core_set_id": regression.CORE_SET_ID}


@router.post("/ontology/semantic/regression/sets")
def create_regression_set(payload: RegressionSetPayload, request: Request):
    _ensure()
    require_owner(request)
    return _call(regression.upsert_set, payload.set_id, payload.name, payload.cases)


@router.post("/ontology/semantic/regression/sets/{set_id}/run")
def run_regression(set_id: str, request: Request, task_snapshot_id: str | None = None):
    _ensure()
    p = _principal(request)
    return _call(regression.run_set, set_id, principal=p, schema=SEM.load_schema(),
                 task_snapshot_id=task_snapshot_id,
                 tenant_id=str(p.get("tenant_id") or "default"))


@router.get("/ontology/semantic/regression/runs")
def regression_runs(request: Request, set_id: str | None = None):
    _ensure()
    p = _principal(request)
    return {"items": regression.list_runs(set_id, str(p.get("tenant_id") or "default"))}


# ---------------------------------------------------------------- 对齐 / 指标 / 维护
@router.get("/ontology/semantic/alignment/{instance_id}")
def alignment_trace(instance_id: str, request: Request):
    _ensure()
    p = _principal(request)
    return _call(alignment.trace, instance_id, str(p.get("tenant_id") or "default"))


@router.get("/ontology/semantic/metrics")
def get_metrics(request: Request):
    _ensure()
    p = _principal(request)
    return metrics.compute(str(p.get("tenant_id") or "default"), SEM.load_schema())


@router.post("/ontology/semantic/maintenance/ensure-ready")
def ensure_ready(request: Request):
    _ensure()
    require_owner(request)
    return SEM.ensure_ready()


# ---------------------------------------------------------------- P0-2 影响分析 / P0-1 状态机
@router.get("/ontology/semantic/selftest")
def semantic_selftest(request: Request):
    """P0 新增能力的内置可评估自检（不写库）：影响分析 + 状态机各一组样本。"""
    from app.modules.ontology.semantic import impact, state_machine
    return {"impact": impact.self_check(), "state_machine": state_machine.self_check()}


class ImpactIn(BaseModel):
    root_id: str
    max_depth: int = 3
    relation_types: list[str] = []
    directions: dict[str, str] = {}
    max_nodes: int = 200
    source: str = "graph"       # graph=图谱实体+active关系（汇报链完整）；semantic=语义层实例关系


@router.post("/ontology/semantic/impact")
def impact_analysis(payload: ImpactIn, request: Request):
    """P0-2 影响分析：从某实体出发沿关系多跳追溯受影响对象（只读）。

    默认 source=graph：用 ontology.entities + active relations，覆盖完整汇报链；
    source=semantic：用语义层实例 + 非派生关系。转移方向用 impact.DEFAULT_DIRECTION
    或 payload.directions 覆盖。结果含完整影响路径，path_query_accuracy 照实披露。
    """
    from app.modules.ontology.semantic import impact
    _ensure()
    p = _principal(request)
    tenant = str(p.get("tenant_id") or "default")
    if payload.source == "semantic":
        nodes = store.list_instances(tenant, limit=2000)
        edges = store.list_relations(tenant, "derived = false", limit=5000)
    else:
        nodes, edges = store.graph_for_impact(tenant)
    return impact.analyze_impact(
        payload.root_id, nodes, edges,
        max_depth=payload.max_depth,
        directions=payload.directions or None,
        relation_types=payload.relation_types or None,
        max_nodes=payload.max_nodes,
    )


@router.post("/ontology/semantic/state-machine/preview")
def state_machine_preview(payload: dict, request: Request):
    """P0-1 状态转换预览（纯计算，不写库）：校验 old→new 是否合法。

    用代码里的权威 Schema（SD.default_semantic_schema）而非库快照：状态机定义
    是代码契约，且预览不依赖 ensure_ready 的播种时机。
    """
    from app.modules.ontology.semantic import state_machine
    schema = SD.to_schema_dict(SD.default_semantic_schema())
    return state_machine.validate_transition(
        schema,
        str(payload.get("type_name") or ""),
        str(payload.get("attribute") or ""),
        payload.get("new_value"),
        old_value=payload.get("old_value"),
        has_old=bool(payload.get("has_old", True)),
    )


@router.get("/ontology/semantic/vector/stats")
def vector_stats(request: Request):
    """向量本体自证（只读）：后端 / 覆盖率 / 模型 / stale 计数。

    覆盖率是当前真实切片数与向量数之比；stale 指有向量但无 model 标记的
    历史向量。标注集未建，不输出任何 Recall 类指标（方案 §4.6）。
    """
    from app.modules.retrieval import vector_store

    vector_store.ensure_columns()
    stats = vector_store.get_vector_store().stats()
    p = _principal(request)
    return {**stats, "tenant_id": p.get("tenant_id") or "default"}
