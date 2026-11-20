"""CQ 驱动本体工程 V2.0 API（增量路由，与 V1.2 /ontology/cq 并存）。

V1.2 端点语义完全不变；V2.0 全部挂在 /ontology/v2 下。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import current_actor
from app.modules.ontology import cq_v2

router = APIRouter()


def _audit_owner_deny(request: Request, reason: str) -> None:
    """越权写本体：落持久化审计（P2 收口），审计失败不影响 403。"""
    try:
        from app import security_audit

        security_audit.record(
            "owner_gate_deny", rule="require_owner", severity="block",
            detail={"reason": reason,
                    "path": getattr(getattr(request, "url", None), "path", None)},
        )
    except Exception:  # noqa: BLE001
        pass


def require_owner(request: Request) -> dict:
    """本体治理写操作的权限闸门（SIL-A07 · 未授权修改防线）。

    优先复用 AC-01 全局中间件已算好的 `request.state.principal`（唯一判定
    入口）；中间件未运行时回落原「按头解析」逻辑，保证被直接调用与升级前
    行为一致。本机回环默认 actor=owner；显式非 owner 级上下文一律 403。
    fail-closed：头不可解析即拒绝，并落一条 owner_gate_deny 审计。
    """
    from app import policy as pol

    cached = getattr(getattr(request, "state", None), "principal", None)
    if isinstance(cached, dict) and cached:
        principal = cached
    else:
        h = request.headers
        perm_raw = (h.get("x-permission-context") or "").strip()
        perm: dict = {}
        if perm_raw:
            try:
                perm = json.loads(perm_raw)
                if not isinstance(perm, dict):
                    raise ValueError
            except (ValueError, TypeError):
                _audit_owner_deny(request, "X-Permission-Context 不可解析")
                raise HTTPException(403, "X-Permission-Context 不可解析，拒绝本体写操作") from None
        principal = pol.principal_from({
            "tenant_id": (h.get("x-tenant-id") or "default").strip() or "default",
            "user_id": (h.get("x-user-id") or "").strip(),
            "actor": current_actor(request) or "owner",
            "permission_context": perm or {"role": "owner", "scope": ["owner"]},
        })
    if not pol.is_owner_class(principal):
        _audit_owner_deny(request, "非 owner 级主体尝试本体写操作")
        raise HTTPException(403, "本体治理写操作要求 owner 级主体（role=owner）")
    return principal


def _engine():
    return cq_v2.get_engine()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class BootstrapPayload(BaseModel):
    force: bool = False
    auto_admit_actor: str | None = None


class CQPayload(BaseModel):
    code: str | None = None
    question: str = ""
    question_template: str | None = None
    intent: str = "lookup"
    business_domain: str | None = None
    business_purpose: str | None = None
    capability_ids: list[str] | None = None
    subject_type: str | None = None
    target_type: str | None = None
    subject_side: str = "source"
    answer_shape: str = "entity"
    required_relations: list[str] | None = None
    required_attributes: list[str] | None = None
    scope_constraints: dict | None = None
    temporal_constraints: dict | None = None
    acceptance_criteria: dict | None = None
    evidence_requirements: dict | None = None
    expected_answer: dict | None = None
    evaluation_method: str | None = None
    cq_kind: str = "business"
    risk_level: str = "normal"
    cq_weight: float = 1.0
    priority: int = 2
    owner: str | None = None


# ---------------------------------------------------------------- 基础
@router.get("/ontology/v2/meta")
def meta():
    return _engine().meta()


@router.post("/ontology/v2/bootstrap")
def bootstrap(payload: BootstrapPayload, request: Request = None):
    require_owner(request)
    return _call(_engine().bootstrap, force=payload.force,
                 auto_admit_actor=payload.auto_admit_actor)


@router.get("/ontology/v2/four-layers")
def four_layers():
    return _engine().four_layers()


@router.get("/ontology/v2/metrics")
def metrics():
    return _engine().metrics()


@router.get("/ontology/v2/audit")
def audit():
    return _engine().audit()


# ---------------------------------------------------------------- 持久化（P1-5）
@router.get("/ontology/v2/persistence")
def persistence():
    """状态清单：存储模式、是否持久化、各表规模、Registry/审计/基线规模。"""
    return _engine().persistence_status()


@router.post("/ontology/v2/persistence/hydrate")
def persistence_hydrate(force: bool = False, request: Request = None):
    require_owner(request)
    """从库内权威副本水合内存态；force=true 时覆盖已有内存数据。"""
    return _call(_engine().restore, force)


@router.post("/ontology/v2/persistence/flush")
def persistence_flush(request: Request = None):
    require_owner(request)
    """强制全量回写（含表数据），用于漂移修复或停机前固化。"""
    return _call(_engine().flush)


@router.get("/ontology/v2/persistence/verify")
def persistence_verify():
    """内存读模型 vs 库内副本对账；不可比时如实返回原因而不是假装一致。"""
    return _engine().verify_persistence()


# ---------------------------------------------------------------- 业务能力
@router.get("/ontology/v2/capabilities")
def capabilities():
    return _engine().capabilities()


@router.post("/ontology/v2/capabilities")
def create_capability(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().create_capability, payload)


@router.post("/ontology/v2/capabilities/{cap_id}/cqs")
def link_cq(cap_id: str, payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().link_cq, cap_id, payload.get("cq_id", ""),
                 float(payload.get("weight") or 1.0))


@router.get("/ontology/v2/requirements")
def requirements(cq_id: str | None = None):
    return _engine().requirements(cq_id)


# ---------------------------------------------------------------- CQ 与质量准入
@router.get("/ontology/v2/cqs")
def list_cqs(admitted_only: bool = False):
    eng = _engine()
    rows = eng.cqs()
    if admitted_only:
        from app.modules.ontology.cq_v2 import quality as q
        rows = [r for r in rows if q.admitted(r)]
    return rows


@router.post("/ontology/v2/cqs")
def create_cq(payload: CQPayload, request: Request = None):
    require_owner(request)
    return _call(_engine().create_cq, payload.model_dump(exclude_none=True))


@router.get("/ontology/v2/cqs/{cq_id}")
def get_cq(cq_id: str):
    row = _engine().get_cq(cq_id)
    if not row:
        raise HTTPException(404, "CQ 不存在")
    return row


@router.patch("/ontology/v2/cqs/{cq_id}")
def update_cq(cq_id: str, payload: dict):
    return _call(_engine().update_cq, cq_id, payload)


@router.post("/ontology/v2/cqs/{cq_id}/quality-check")
def quality_check(cq_id: str, payload: dict | None = None):
    return _call(_engine().quality_check, cq_id, (payload or {}).get("llm_dims"))


@router.post("/ontology/v2/cqs/{cq_id}/admit")
def admit_cq(cq_id: str, request: Request = None):
    require_owner(request)
    actor = current_actor(request) or "业务负责人"
    return _call(_engine().admit_cq, cq_id, actor)


@router.get("/ontology/v2/cqs/{cq_id}/instances")
def instances(cq_id: str):
    return _engine().instances(cq_id)


@router.post("/ontology/v2/cqs/{cq_id}/instances")
def add_instance(cq_id: str, payload: dict, request: Request = None):
    require_owner(request)
    payload = {**payload, "cq_id": cq_id}
    return _call(_engine().add_instance, payload)


# ---------------------------------------------------------------- Query Plan / 执行
@router.post("/ontology/v2/cqs/{cq_id}/plan")
def build_plan(cq_id: str, payload: dict | None = None):
    return _call(_engine().build_plan, cq_id, (payload or {}).get("scopes"))


@router.post("/ontology/v2/plan/validate")
def validate_plan(payload: dict):
    return _engine().validate_plan(payload.get("plan") or {},
                                   payload.get("permission_ctx") or {})


@router.post("/ontology/v2/cqs/{cq_id}/run")
def run_cq(cq_id: str, payload: dict | None = None):
    p = payload or {}
    return _call(_engine().run, cq_id, p.get("snapshot"), p.get("permission_ctx"))


@router.get("/ontology/v2/coverage")
def coverage(only_admitted: bool = True):
    return _engine().coverage_run(only_admitted=only_admitted)


@router.get("/ontology/v2/gaps")
def gaps():
    return _engine().gaps()


@router.get("/ontology/v2/portfolio")
def portfolio():
    return _engine().portfolio()


# ---------------------------------------------------------------- Schema 治理
@router.get("/ontology/v2/schema")
def schema():
    eng = _engine()
    return {"schema_version": eng.registry.current_version(),
            "schema": eng.registry.current(), "migrations": eng.registry.migrations,
            "pins": eng.registry.pins}


@router.get("/ontology/v2/schema/linkml")
def linkml():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(_engine().registry_linkml())


@router.post("/ontology/v2/schema/publish")
def schema_publish(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().registry_publish, payload.get("schema") or {},
                 current_actor(request) or payload.get("actor") or "owner")


@router.post("/ontology/v2/schema/rollback")
def schema_rollback(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().registry_rollback, payload.get("version", ""),
                 current_actor(request) or "owner")


@router.post("/ontology/v2/schema/pin")
def schema_pin(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().registry_pin, payload.get("cq_id", ""),
                 payload.get("version"))


# ---------------------------------------------------------------- LinkML 单一来源
@router.get("/ontology/v2/schema/linkml/roundtrip")
def linkml_roundtrip():
    """导出→导入自证：建模语义是否无损（不一致时列出逐段差异）。"""
    return _call(_engine().linkml_roundtrip)


@router.post("/ontology/v2/schema/linkml/import")
def linkml_import(payload: dict, request: Request = None):
    require_owner(request)
    """把 LinkML 当建模输入：默认 dry_run（只算 diff/兼容性/迁移计划）。"""
    text = payload.get("yaml") or payload.get("linkml") or ""
    if not text.strip():
        raise HTTPException(400, "缺少 yaml 内容")
    actor = current_actor(request) or payload.get("actor") or "owner"
    return _call(_engine().linkml_import, text, actor, bool(payload.get("dry_run", True)))


@router.post("/ontology/v2/schema/versions/sync")
def schema_versions_sync(request: Request = None):
    require_owner(request)
    """把 Registry 版本对齐到 schema_registry 与 ontology_versions 两张关系表。"""
    eng = _engine()
    return {"schemas": _call(eng.persist_schema_versions),
            "runtime": _call(eng.sync_runtime_versions)}


# ---------------------------------------------------------------- 双轨对账
@router.get("/ontology/v2/registry/tracks")
def registry_tracks():
    """JSONB 副本 vs 规范化关系表逐表对账（缺哪些、多哪些，都给键）。"""
    return _call(_engine().registry_tracks)


@router.post("/ontology/v2/registry/tracks/sync")
def registry_tracks_sync(prune: bool = False, request: Request = None):
    require_owner(request)
    """以副本为准补齐关系表；prune=true 时顺带清理关系表里的多余行。"""
    return _call(_engine().registry_sync_tracks, prune)


# ---------------------------------------------------------------- 派生表一致性
@router.get("/ontology/v2/derived/status")
def derived_status():
    """能力关联与需求项是否指向真实 CQ（孤儿行会撞外键，必须先于写关系表修）。"""
    return _call(_engine().derived_status)


@router.post("/ontology/v2/derived/rebuild")
def derived_rebuild(dry_run: bool = True, request: Request = None):
    require_owner(request)
    """按当前 CQ 重建派生表：默认 dry run，只给计划不动数据。"""
    return _call(_engine().rebuild_derived, dry_run)


# ---------------------------------------------------------------- SHACL 写入侧
@router.get("/ontology/v2/shapes")
def shapes():
    """当前生效的 Shape（shapes.yaml 覆盖 > Registry 派生）。"""
    from app.modules.ontology import shacl

    return shacl.current_shapes()


@router.post("/ontology/v2/shapes/validate")
def shapes_validate(payload: dict):
    """对一条事实载荷做只读校验，不写库。"""
    from app.modules.ontology import shacl

    fact = payload.get("fact") or payload
    return shacl.validate(fact, subject_type=payload.get("subject_type"),
                          object_type=payload.get("object_type"))


@router.get("/ontology/v2/shapes/stats")
def shapes_stats():
    from app.modules.ontology import shacl

    return {"stats": shacl.stats(), "recent": shacl.recent(10),
            "shapes": {"version": shacl.current_shapes().get("shapes_version"),
                       "schema_version": shacl.current_shapes().get("schema_version")}}


@router.get("/ontology/v2/shapes/audit")
def shapes_audit(limit: int = 200):
    """抽样体检既有事实：只读，给出违规分布与样本。"""
    from app.modules.ontology import shacl

    return _call(shacl.audit_facts, max(1, min(int(limit or 200), 2000)))


@router.post("/ontology/v2/mapping/propose")
def mapping_propose(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().mapping_propose, payload.get("term", ""),
                 payload.get("cq_id", ""), payload.get("examples"),
                 payload.get("actor", "llm"))


@router.get("/ontology/v2/proposals")
def proposals():
    return _engine().proposals()


@router.post("/ontology/v2/proposals/{proposal_id}/approve")
def proposal_approve(proposal_id: str, request: Request = None):
    require_owner(request)
    return _call(_engine().proposal_approve, proposal_id,
                 current_actor(request) or "业务负责人")


# ---------------------------------------------------------------- 决策与演进
@router.post("/ontology/v2/decisions/run")
def decision_run(payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().decision_run, payload.get("cq_id", ""),
                 payload.get("evidence_ctx"), payload.get("rules"))


@router.post("/ontology/v2/decisions/{decision_id}/approve")
def decision_approve(decision_id: str, payload: dict, request: Request = None):
    require_owner(request)
    return _call(_engine().decision_approve, decision_id,
                 current_actor(request) or "业务负责人",
                 payload.get("decision_value"),
                 bool(payload.get("authorized_execution")))


@router.post("/ontology/v2/evolution/cycle")
def evolution_cycle(request: Request, payload: dict | None = None):
    require_owner(request)
    actor = current_actor(request) or (payload or {}).get("actor") or "业务负责人"
    return _engine().evolution_cycle(actor)
