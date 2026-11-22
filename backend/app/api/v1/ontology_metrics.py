"""Tenant-scoped ontology metric registry and safe preview API."""
from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from app.db import session as db
from app.modules.ontology.runtime import metric_engine as ME

router = APIRouter()


def _principal(request: Request) -> dict:
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, dict) else {}


def _tenant(request: Request) -> tuple[str, str]:
    p = _principal(request)
    return str(p.get("tenant_id") or "default"), str(p.get("user_id") or "anonymous")


def ensure_schema() -> None:
    db.execute("CREATE SCHEMA IF NOT EXISTS ontology")
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.metric_definitions (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id VARCHAR(120) NOT NULL,
      metric_id VARCHAR(160) NOT NULL,
      name VARCHAR(240) NOT NULL,
      version INTEGER NOT NULL,
      status VARCHAR(20) NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','validated','approved','published','deprecated')),
      definition JSONB NOT NULL,
      checksum VARCHAR(64) NOT NULL,
      ontology_version VARCHAR(120),
      created_by VARCHAR(160) NOT NULL,
      approved_by VARCHAR(160),
      published_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, metric_id, version)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_metric_defs_tenant_status
                  ON ontology.metric_definitions (tenant_id, status, metric_id, version DESC)""")


class DefinitionsIn(BaseModel):
    items: list[dict] = Field(default_factory=list)
    ontology_version: str | None = None


class PreviewIn(DefinitionsIn):
    data: dict = Field(default_factory=dict)


class SaveIn(BaseModel):
    definition: dict
    ontology_version: str | None = None


class StorePreviewIn(BaseModel):
    metric_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=5000, ge=1, le=10000)


@router.get("/ontology/metrics/catalog")
def catalog(request: Request):
    from app.modules.ontology.runtime.metrics import METRIC_CATALOG
    return {"items": METRIC_CATALOG, "additional_algorithms": sorted(ME.AGGREGATIONS)}


@router.post("/ontology/metrics/discover")
def discover(payload: dict, request: Request):
    return ME.discover(payload.get("spec") or {})


@router.post("/ontology/metrics/validate")
def validate(payload: DefinitionsIn, request: Request):
    return ME.validate(payload.items)


@router.post("/ontology/metrics/preview")
def preview(payload: PreviewIn, request: Request):
    return ME.preview(payload.items, payload.data)


@router.post("/ontology/metrics/preview-from-store")
def preview_from_store(payload: StorePreviewIn, request: Request):
    """Execute published metrics against complete, permission-filtered semantic instances."""
    ensure_schema()
    tenant, _ = _tenant(request)
    rows = db.fetch_all(
        "SELECT DISTINCT ON (metric_id) metric_id, name, version, definition, checksum, ontology_version "
        "FROM ontology.metric_definitions WHERE tenant_id=:t AND status='published' "
        "ORDER BY metric_id, version DESC", {"t": tenant})
    published = {}
    for row in rows:
        definition = row.get("definition")
        if isinstance(definition, str):
            definition = json.loads(definition)
        if isinstance(definition, dict):
            published[row["metric_id"]] = {
                "definition": definition, "version": row["version"],
                "checksum": row["checksum"], "name": row["name"],
                "ontology_version": row.get("ontology_version"),
            }
    if not published:
        raise HTTPException(404, "当前租户没有已发布指标")

    requested = list(dict.fromkeys(str(x).strip() for x in payload.metric_ids if str(x).strip()))
    if requested:
        missing = [mid for mid in requested if mid not in published]
        if missing:
            raise HTTPException(404, {"message": "指标不存在或尚未发布", "metric_ids": missing})
        selected, pending = set(), list(requested)
        while pending:
            mid = pending.pop()
            if mid in selected:
                continue
            selected.add(mid)
            deps = published[mid]["definition"].get("dependencies") or []
            for dep in deps:
                if dep not in published:
                    raise HTTPException(409, {
                        "message": "已发布指标依赖缺失或未发布",
                        "metric_id": mid, "dependency": dep})
                pending.append(dep)
        metric_ids = sorted(selected)
    else:
        metric_ids = sorted(published)
    definitions = [published[mid]["definition"] for mid in metric_ids]
    validation = ME.validate(definitions)
    if not validation["valid"]:
        raise HTTPException(409, {
            "message": "已发布指标依赖图未通过运行时校验",
            "validation": validation})

    # Use the same tenant-scoped semantic store as the existing ingestion pipeline.
    from app.modules.ontology.semantic import store as semantic_store
    from app.modules.ontology.semantic import service as semantic_service
    from app import policy as pol
    semantic_service.ensure_ready()
    total_row = db.fetch_one(
        "SELECT count(*) AS n FROM ontology.entity_instances "
        "WHERE tenant_id=:t AND status='active'", {"t": tenant})
    total = int(total_row["n"]) if total_row else 0
    if total > payload.limit:
        return {
            "ok": False, "complete": False, "execution_source": "ontology.entity_instances",
            "records_available": total, "records_limit": payload.limit,
            "items": [], "lineage": [],
            "reason": "实例数量超过本次安全预览上限；为避免部分数据产生误导性指标，本次不计算。",
        }
    instances = semantic_store.list_instances(
        tenant_id=tenant, where="status = 'active'", limit=payload.limit)
    visible = pol.filter_rows(instances, _principal(request))
    if len(visible) != total:
        return {
            "ok": False, "complete": False, "execution_source": "ontology.entity_instances",
            "records_evaluated": len(visible), "items": [], "lineage": [],
            "reason": "当前用户的数据权限只能访问部分实例；为避免不完整口径，本次不计算。",
        }
    data_versions = sorted({str(x.get("ontology_version") or "") for x in visible})
    if len(data_versions) > 1:
        return {
            "ok": False, "complete": False, "execution_source": "ontology.entity_instances",
            "records_evaluated": len(visible), "items": [], "lineage": [],
            "reason": "语义实例库包含多个本体版本；需先完成版本迁移或按版本隔离计算。",
            "ontology_versions": data_versions,
        }
    data_version = data_versions[0] if data_versions else ""
    mismatched = {
        mid: published[mid]["ontology_version"] for mid in metric_ids
        if published[mid]["ontology_version"] and data_version
        and str(published[mid]["ontology_version"]) != data_version
    }
    if mismatched:
        raise HTTPException(409, {
            "message": "指标定义版本与实例数据本体版本不一致",
            "data_ontology_version": data_version,
            "metric_ontology_versions": mismatched,
        })
    data = ME.semantic_instances_to_data(visible)
    result = ME.preview(definitions, data)
    result.update({
        "complete": True,
        "data_ontology_version": data_version or None,
        "execution_source": "ontology.entity_instances",
        "records_evaluated": len(visible),
        "metric_versions": {mid: published[mid]["version"] for mid in metric_ids},
        "metric_checksums": {mid: published[mid]["checksum"] for mid in metric_ids},
        "ontology_version": getattr(request.state, "semantic_context", None)
            and getattr(request.state.semantic_context, "schema_version", None),
    })
    return result


@router.get("/ontology/metrics")
def list_metrics(request: Request, metric_id: str | None = None, limit: int = 100):
    ensure_schema()
    tenant, _ = _tenant(request)
    rows = db.fetch_all(
        "SELECT metric_id, name, version, status, definition, checksum, ontology_version, "
        "created_by, approved_by, published_at, created_at FROM ontology.metric_definitions "
        "WHERE tenant_id=:t AND (:m IS NULL OR metric_id=:m) "
        "ORDER BY metric_id, version DESC LIMIT :n",
        {"t": tenant, "m": metric_id, "n": max(1, min(limit, 500))})
    for row in rows:
        if isinstance(row.get("definition"), str):
            row["definition"] = json.loads(row["definition"])
    return {"items": rows}


@router.post("/ontology/metrics")
def save_metric(payload: SaveIn, request: Request):
    ensure_schema()
    tenant, actor = _tenant(request)
    definition = dict(payload.definition)
    if definition.get("kind") in ("derived", "composite"):
        try:
            definition["dependencies"] = ME._formula_refs(str(definition.get("formula") or ""))
        except ValueError as exc:
            raise HTTPException(400, f"指标公式无效：{exc}") from exc
    mid = str(definition.get("metric_id") or "").strip()
    name = str(definition.get("name") or "").strip()
    if not mid or not name:
        raise HTTPException(400, "metric_id 和 name 必填")
    # Validate this definition together with the latest tenant-scoped dependency versions.
    current = db.fetch_all(
        "SELECT DISTINCT ON (metric_id) metric_id, definition FROM ontology.metric_definitions "
        "WHERE tenant_id=:t AND status IN ('validated','approved','published') "
        "ORDER BY metric_id, version DESC", {"t": tenant})
    defs = []
    for row in current:
        d = row["definition"]
        defs.append(json.loads(d) if isinstance(d, str) else d)
    defs = [x for x in defs if x.get("metric_id") != mid] + [definition]
    report = ME.validate(defs)
    if not report["valid"]:
        raise HTTPException(400, {"message": "指标定义未通过校验", "report": report})
    from app.modules.ontology.runtime.metric_engine import checksum
    with db.transaction():
        db.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(:k,0)) AS locked",
                     {"k": f"{tenant}:{mid}"})
        row = db.fetch_one(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM ontology.metric_definitions "
            "WHERE tenant_id=:t AND metric_id=:m", {"t": tenant, "m": mid})
        version = int(row["v"])
        result = db.fetch_one(
            "INSERT INTO ontology.metric_definitions "
            "(tenant_id,metric_id,name,version,status,definition,checksum,ontology_version,created_by) "
            "VALUES (:t,:m,:n,:v,'validated',CAST(:d AS jsonb),:c,:ov,:by) "
            "RETURNING id,metric_id,name,version,status,checksum,ontology_version,created_by,created_at",
            {"t": tenant, "m": mid, "n": name, "v": version,
             "d": json.dumps(definition, ensure_ascii=False, sort_keys=True),
             "c": checksum(definition), "ov": payload.ontology_version, "by": actor})
    return {"item": result, "validation": report}


@router.post("/ontology/metrics/{metric_id}/approve")
def approve(metric_id: str, request: Request):
    ensure_schema()
    tenant, actor = _tenant(request)
    row = db.fetch_one(
        "UPDATE ontology.metric_definitions SET status='approved',approved_by=:by "
        "WHERE tenant_id=:t AND metric_id=:m AND status='validated' "
        "AND version=(SELECT MAX(version) FROM ontology.metric_definitions "
        "WHERE tenant_id=:t AND metric_id=:m) "
        "RETURNING metric_id,version,status,approved_by",
        {"t": tenant, "m": metric_id, "by": actor})
    if not row:
        raise HTTPException(404, "当前租户没有可审批的 validated 指标版本")
    return {"item": row}


@router.post("/ontology/metrics/{metric_id}/publish")
def publish(metric_id: str, request: Request):
    ensure_schema()
    tenant, actor = _tenant(request)
    latest = db.fetch_all(
        "SELECT DISTINCT ON (metric_id) metric_id, status, definition "
        "FROM ontology.metric_definitions WHERE tenant_id=:t "
        "ORDER BY metric_id, version DESC", {"t": tenant})
    latest_by_id = {x["metric_id"]: x for x in latest}
    candidate = latest_by_id.get(metric_id)
    if not candidate or candidate.get("status") != "approved":
        raise HTTPException(404, "只有已审批的最新指标版本可以发布")
    definition = candidate.get("definition") or {}
    if isinstance(definition, str):
        definition = json.loads(definition)
    missing = [dep for dep in (definition.get("dependencies") or [])
               if not latest_by_id.get(dep) or latest_by_id[dep].get("status") != "published"]
    if missing:
        raise HTTPException(409, {"message": "依赖指标必须先发布", "dependencies": missing})
    row = db.fetch_one(
        "UPDATE ontology.metric_definitions SET status='published',published_at=now() "
        "WHERE tenant_id=:t AND metric_id=:m AND status='approved' "
        "AND version=(SELECT MAX(version) FROM ontology.metric_definitions "
        "WHERE tenant_id=:t AND metric_id=:m) "
        "RETURNING metric_id,version,status,published_at",
        {"t": tenant, "m": metric_id})
    if not row:
        raise HTTPException(404, "只有已审批的最新指标版本可以发布")
    return {"item": row}
