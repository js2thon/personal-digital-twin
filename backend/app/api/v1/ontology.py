"""本体 API：本体版本、实体画像、观察写入、争议裁决、扩展建议、发布版本。"""
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import current_actor
from app.db import session as db
from app.modules.ontology import graph as og
from app.modules.ontology import quality
from app.modules.ontology import workbench as wb

router = APIRouter()


# ---------------------------------------------------------------- 数据源发现与已审核映射执行
def _principal(request: Request) -> dict:
    principal = getattr(getattr(request, "state", None), "principal", None)
    return principal if isinstance(principal, dict) else {}


def _tenant_actor(request: Request) -> tuple[str, str]:
    principal = _principal(request)
    return (str(principal.get("tenant_id") or "default"),
            str(principal.get("user_id") or current_actor(request) or "anonymous"))


class DatabaseInspectIn(BaseModel):
    profile_name: str = Field(min_length=1, max_length=64)
    schema_name: str | None = None
    table_names: list[str] = Field(default_factory=list)
    sample_rows: int = Field(default=5, ge=0, le=100)


class DatabaseIngestIn(BaseModel):
    profile_name: str = Field(min_length=1, max_length=64)
    schema_name: str | None = None
    table_name: str = Field(min_length=1, max_length=200)
    primary_key: str = Field(min_length=1, max_length=200)
    mapping_set_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=200)
    batch_size: int = Field(default=500, ge=1, le=500)
    reconcile_missing: bool = False


@router.get("/ontology/data-sources/database/profiles")
def database_profiles(request: Request):
    from app.modules.ontology import database_connector
    tenant, _ = _tenant_actor(request)
    return {"items": database_connector.list_profiles(tenant_id=tenant)}


@router.post("/ontology/data-sources/database/inspect")
def database_inspect(payload: DatabaseInspectIn, request: Request):
    from app.modules.ontology import database_connector
    tenant, _ = _tenant_actor(request)
    try:
        return database_connector.inspect_database(
            profile_name=payload.profile_name, tenant_id=tenant,
            schema_name=payload.schema_name, table_names=payload.table_names,
            sample_rows=payload.sample_rows)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/ontology/data-sources/database/ingest")
def database_ingest(payload: DatabaseIngestIn, request: Request):
    from app.modules.ontology.ingestion_batches import execute_approved_database_snapshot
    tenant, actor = _tenant_actor(request)
    try:
        return execute_approved_database_snapshot(
            tenant_id=tenant, mapping_set_id=payload.mapping_set_id,
            profile_name=payload.profile_name, schema_name=payload.schema_name,
            table_name=payload.table_name, primary_key=payload.primary_key,
            snapshot_id=payload.snapshot_id, actor=actor,
            batch_size=payload.batch_size,
            reconcile_missing=payload.reconcile_missing)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/ontology/schema")
def get_schema(version: str | None = None):
    if version:
        row = db.fetch_one(
            "SELECT version, schema_content, status FROM ontology.ontology_versions "
            "WHERE version = :v",
            {"v": version},
        )
    else:
        row = db.fetch_one(
            "SELECT version, schema_content, status FROM ontology.ontology_versions "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1"
        )
    if not row:
        raise HTTPException(404, "ontology version not found")
    return row


@router.get("/ontology/entities")
def list_entities(entity_type: str | None = None):
    sql = ("SELECT e.id, e.entity_type, e.canonical_name, e.aliases, e.status, "
           "(SELECT count(*) FROM knowledge.claims c "
           " WHERE c.subject->>'name' = e.canonical_name) AS claim_count, "
           "(SELECT count(*) FROM knowledge.facts f "
           " WHERE f.subject_id = e.id AND f.status = 'active') AS fact_count "
           "FROM ontology.entities e WHERE e.status IN ('active', 'candidate')")
    params: dict = {}
    if entity_type:
        sql += " AND e.entity_type = :t"
        params["t"] = entity_type
    sql += " ORDER BY claim_count DESC, e.created_at DESC LIMIT 200"
    return db.fetch_all(sql, params)


@router.get("/ontology/relations")
def list_relations(status: str | None = None):
    """关系链：同时返回 candidate/active，并带上证据 Claim 的审核状态。"""
    sql = (
        "SELECT r.id, r.relation_type, r.status, r.evidence_claim_id, "
        "s.canonical_name AS source_name, t.canonical_name AS target_name, "
        "c.status AS claim_status, c.predicate, "
        "COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind "
        "FROM ontology.relations r "
        "JOIN ontology.entities s ON s.id = r.source_entity_id "
        "JOIN ontology.entities t ON t.id = r.target_entity_id "
        "LEFT JOIN knowledge.claims c ON c.id = r.evidence_claim_id "
        "LEFT JOIN LATERAL ("
        "  SELECT d2.metadata FROM knowledge.claim_evidence ce "
        "  JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        "  JOIN source.documents d2 ON d2.id = es.document_id "
        "  WHERE ce.claim_id = c.id LIMIT 1"
        ") d ON TRUE "
    )
    params: dict = {}
    if status:
        sql += "WHERE r.status = :s "
        params["s"] = status
    sql += "ORDER BY r.status, r.valid_from DESC NULLS LAST LIMIT 200"
    return db.fetch_all(sql, params)


class ProposalIn(BaseModel):
    proposal: dict   # {entity_type, definition, examples}
    rationale: str = ""


@router.post("/ontology/proposals")
def submit_proposal(body: ProposalIn):
    """本体扩展走候选流程：LLM 提出 → Schema 校验 → 冲突检查 → 人工审核 → 新版本发布。"""
    pid = db.insert_returning_id(
        "ontology.extension_proposals",
        {"proposal": json.dumps(body.proposal, ensure_ascii=False),
         "rationale": body.rationale},
    )
    return {"id": pid, "status": "pending"}


@router.get("/ontology/dimensions")
def ontology_dimensions():
    """元本体：当前在跟踪哪些维度、各覆盖多少实体与观测、有多少争议。"""
    from app.modules.ontology import service as ontology

    return ontology.dimensions()


@router.get("/ontology/entities/{entity_id}/profile")
def entity_profile(entity_id: str, asof: str | None = None,
                   min_confidence: float | None = None,
                   include_relations: bool = True,
                   request: Request = None):  # noqa: RUF013  FastAPI 注入
    """实体画像：维度 → 当前值 + 历史段 + 争议值；asof 支持时间旅行。

    P1-4：按请求上下文（X-Tenant-Id / X-User-Id / X-Permission-Context）
    做逐点过滤；无头调用按 owner 级处理（与升级前一致）。
    """
    from app.modules.ontology import service as ontology
    from app.modules.semantic_runtime.context import from_request
    from app.policy import principal_from

    principal = None
    if request is not None:
        try:
            principal = principal_from(from_request(request))
        except HTTPException:
            principal = None  # 上下文缺失按 owner 级回落，行为与升级前一致
    profile = ontology.entity_profile(entity_id, asof=asof,
                                      min_confidence=min_confidence,
                                      include_relations=include_relations,
                                      principal=principal)
    if not profile:
        raise HTTPException(404, "entity not found")
    return profile


@router.get("/ontology/conflicts")
def ontology_conflicts(limit: int = 50):
    """争议事实：低信任写入没有覆盖当前值，而是挂起等本人裁决。"""
    from app.modules.ontology import service as ontology

    return ontology.list_conflicts(limit=limit)


class ResolveIn(BaseModel):
    action: str          # accept | reject


@router.post("/ontology/conflicts/{fact_id}/resolve")
def resolve_conflict(fact_id: str, body: ResolveIn, request: Request):
    from app.modules.audit import service as audit
    from app.modules.ontology import service as ontology

    actor = current_actor(request)
    try:
        out = ontology.resolve_conflict(fact_id, body.action, actor=actor)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    audit.log_access(actor, f"ontology.conflict.{body.action}", "knowledge.fact",
                     fact_id, out)
    return out


class ObservationIn(BaseModel):
    """本体观察写入（GBrain 的 ontology_propose 等价物）。

    默认按 agent 信任级写入：与本人已确认的当前值冲突时不会覆盖，
    而是落为 contested 等待裁决。只有 writer="owner" 才具备取代权。
    """
    predicate: str
    object_value: dict | list | str | float | int | bool | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    entity_type: str = "concept"
    valid_from: str | None = None
    confidence: float | None = None
    writer: str = "agent"
    visibility: str = "owner"
    source: dict = Field(default_factory=dict)
    create_entity: bool = False


@router.post("/ontology/observations")
def propose_observation(body: ObservationIn, request: Request):
    from app.modules.audit import service as audit
    from app.modules.ontology import service as ontology

    actor = current_actor(request)
    subject_id: str | None = None
    if body.entity_id:
        row = db.fetch_one(
            "SELECT id FROM ontology.entities WHERE id = CAST(:id AS uuid)",
            {"id": body.entity_id},
        )
        if not row:
            raise HTTPException(404, "entity not found")
        subject_id = str(row["id"])
    elif body.entity_name:
        row = db.fetch_one(
            "SELECT id FROM ontology.entities WHERE canonical_name = :n LIMIT 1",
            {"n": body.entity_name},
        )
        if row:
            subject_id = str(row["id"])
        elif body.create_entity:
            subject_id = db.insert_returning_id(
                "ontology.entities",
                {"entity_type": body.entity_type, "canonical_name": body.entity_name,
                 "status": "candidate"},
            )
        else:
            raise HTTPException(404, f"entity not found: {body.entity_name}；"
                                     "如需新建请传 create_entity=true")
    if not subject_id:
        raise HTTPException(400, "entity_id 或 entity_name 至少提供一个")

    out = ontology.write_fact(
        predicate=body.predicate,
        object_value=body.object_value,
        subject_id=subject_id,
        valid_from=body.valid_from,
        confidence=body.confidence,
        writer=body.writer,
        visibility=body.visibility,
        source_ref={**body.source, "kind": "observation", "actor": actor},
    )
    audit.log_access(actor, "ontology.observe", "knowledge.fact",
                     out.get("fact_id"), {**out, "predicate": body.predicate})
    return {"entity_id": subject_id, **out}


@router.post("/ontology/maintenance/backfill-trust")
def backfill_trust():
    """一次性回填历史事实的信任级/置信度/来源快照/生效时间（幂等，不改事实内容）。"""
    from app.modules.ontology import service as ontology

    return {"status": "ok", **ontology.backfill_trust()}


@router.post("/ontology/maintenance/restore-superseded")
def restore_superseded(apply: bool = False):
    """恢复被旧规则误取代的多值事实；默认只预览，apply=true 才执行（可审计、不删行）。"""
    from app.modules.ontology import service as ontology

    return {"status": "ok", **ontology.restore_superseded(apply=apply)}


@router.get("/ontology/diagnostics")
def ontology_diagnostics():
    """本体自检：结构/回填/判定矩阵/时间旅行/基数/多值并存，一次给出结论。"""
    from app.modules.ontology import service as ontology

    return ontology.diagnostics()


@router.post("/ontology/publish")
def publish(new_version: str):
    """发布本体版本：draft 必须存在，发布时旧 published 版本一并置 deprecated。

    兼容性最小检查：只允许「新增字段/新增类型」，任何字段或实体类型被移除
    （相对当前 published 版本）都拒绝发布，需人工评估迁移。
    """
    import json as _json

    new_row = db.fetch_one(
        "SELECT id, version, schema_content FROM ontology.ontology_versions "
        "WHERE version = :v AND status = 'draft'", {"v": new_version}
    )
    if not new_row:
        raise HTTPException(400, "draft version not found: " + new_version)

    current = db.fetch_one(
        "SELECT id, schema_content FROM ontology.ontology_versions "
        "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1"
    )
    if not current:
        raise HTTPException(400, "no published ontology to derive from")

    def _keys(content) -> set:
        try:
            data = content if isinstance(content, dict) else _json.loads(content or "{}")
            return set(data.keys())
        except Exception:  # noqa: BLE001
            return set()

    removed = _keys(current["schema_content"]) - _keys(new_row["schema_content"])
    if removed:
        raise HTTPException(
            409, "compatibility check failed, removed fields: " + ", ".join(sorted(removed))
        )

    # 单事务：新版本置 published + 旧版本置 deprecated（此前允许多版本同时 published）
    with db.transaction():
        db.execute(
            "UPDATE ontology.ontology_versions SET status = 'published' "
            "WHERE version = :v AND status = 'draft'",
            {"v": new_version},
        )
        db.execute(
            "UPDATE ontology.ontology_versions SET status = 'deprecated' "
            "WHERE status = 'published' AND version <> :v",
            {"v": new_version},
        )
    return {"published": new_version,
            "note": "draft passed compatibility check; previous published version deprecated"}


# ---------------------------------------------------------------- 知识图谱
# 图在 PostgreSQL 上实现（Neo4j 冻结）。四个端点全部只读：
#   /ontology/relation-types  关系类型目录 + 实际用量（暴露未登记类型）
#   /ontology/graph           k 跳子图（带环保护）或全图
#   /ontology/graph/stats     图谱质量：孤立点、连通分量、度数前五
#   /ontology/graph/paths     两实体之间的简单路径（≤4 跳）

def _as_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(400, f"{field} must be a uuid") from None


@router.get("/ontology/relation-types")
def list_relation_types():
    rows = db.fetch_all(
        "SELECT relation_type, count(*) AS n FROM ontology.relations "
        "WHERE status = 'active' GROUP BY relation_type"
    )
    usage = {r["relation_type"]: int(r["n"]) for r in rows}
    types = og.relation_catalog(usage)
    return {
        "types": types,
        "families": og.FAMILY_LABELS,
        "summary": {
            "registered": sum(1 for t in types if t["registered"]),
            "used": sum(1 for t in types if t["used"] > 0),
            "unregistered_used": [t["type"] for t in types if not t["registered"]],
        },
    }


@router.get("/ontology/graph")
def get_graph(entity_id: str | None = None, depth: int = 2, types: str | None = None,
              status: str = "active", limit: int = og.DEFAULT_NODES,
              hide_isolated: bool = False):
    """k 跳子图。entity_id 为空时返回全图（受 limit 限制）。"""
    if entity_id:
        entity_id = _as_uuid(entity_id, "entity_id")
    if status not in ("active", "candidate", "deprecated", "any"):
        raise HTTPException(400, "status must be active/candidate/deprecated/any")
    kind_list = og.parse_types(types)
    queries = og.subgraph_queries(entity_id, depth, kind_list, status, limit)
    node_sql, params = queries["nodes"]
    edge_sql, edge_params = queries["edges"]
    node_rows = db.fetch_all(node_sql, params)
    edge_rows = db.fetch_all(edge_sql, edge_params)
    return og.assemble_graph(node_rows, edge_rows, entity_id, og.clamp_depth(depth),
                             hide_isolated, og.clamp_limit(limit))


@router.get("/ontology/graph/stats")
def graph_stats():
    entities = db.fetch_all(
        "SELECT id, canonical_name, entity_type FROM ontology.entities "
        "WHERE status IN ('active', 'candidate') LIMIT 20000"
    )
    relations = db.fetch_all(
        "SELECT source_entity_id, target_entity_id, relation_type, status "
        "FROM ontology.relations LIMIT 50000"
    )
    stats = og.shape_stats(entities, relations)
    stats["catalog_size"] = len(og.RELATION_TYPES)
    return stats


@router.get("/ontology/graph/paths")
def graph_paths(source: str, target: str, max_depth: int = og.MAX_PATH_DEPTH):
    """两个实体之间的简单路径。用来回答「他怎么连到那个项目」。"""
    src = _as_uuid(source, "source")
    dst = _as_uuid(target, "target")
    sql, params = og.paths_query(src, dst, max_depth)
    rows = db.fetch_all(sql, params)
    if not rows:
        return {"paths": [], "source": src, "target": dst}
    entities = db.fetch_all(
        "SELECT id, canonical_name, entity_type FROM ontology.entities LIMIT 20000"
    )
    name_by_id = {str(e["id"]): e["canonical_name"] for e in entities}
    type_by_id = {str(e["id"]): e["entity_type"] for e in entities}
    return {"paths": og.describe_paths(rows, name_by_id, type_by_id),
            "source": src, "target": dst}


# ---------------------------------------------------------------- 本体工作台
# 四个能力：模式层可视化 / 规则校验 / 测试验证 / 编辑（实体与关系）。
# 写操作统一走 workbench 的校验入口——页面拦住的和接口拦住的是同一套规则。

def _wb_error(exc: "wb.WorkbenchError") -> HTTPException:
    status = 409 if exc.code in ("duplicate", "cardinality_conflict", "name_conflict") else 400
    return HTTPException(status, {"code": exc.code, "message": exc.message, **exc.detail})


class RelationIn(BaseModel):
    source_entity_id: str
    relation_type: str = Field(min_length=1, max_length=100)
    target_entity_id: str
    note: str | None = None


class EntityPatch(BaseModel):
    canonical_name: str | None = Field(default=None, max_length=200)
    entity_type: str | None = Field(default=None, max_length=60)
    aliases: list[str] | None = None


@router.get("/ontology/schema/graph")
def schema_graph():
    """模式层视图：实体类型 + 关系类型允许的类型对（带实际用量）。"""
    return wb.schema_graph()


@router.get("/ontology/validate")
def validate_ontology():
    """规则校验：对真实数据跑 R1~R10，并附规则引擎自测结论。"""
    return wb.validate()


@router.get("/ontology/validate/selftest")
def validate_selftest():
    """测试验证：用内置正反例证明确规则会触发、也会放过。"""
    return quality.selftest()


@router.get("/ontology/relations/drift")
def relations_drift():
    """事实→关系的漂移：只算差集，不落库。"""
    return wb.relation_drift()


@router.post("/ontology/relations/reconcile")
def relations_reconcile(dry_run: bool = True):
    """用事实底座重建图上的边。默认 dry-run，只报告不动手。"""
    return wb.reconcile_relations(dry_run=dry_run)


@router.post("/ontology/relations")
def create_relation(payload: RelationIn):
    src = _as_uuid(payload.source_entity_id, "source_entity_id")
    dst = _as_uuid(payload.target_entity_id, "target_entity_id")
    try:
        return wb.create_relation(src, payload.relation_type.strip(), dst, payload.note)
    except wb.WorkbenchError as exc:
        raise _wb_error(exc) from None


@router.delete("/ontology/relations/{relation_id}")
def delete_relation(relation_id: str):
    try:
        return wb.delete_relation(_as_uuid(relation_id, "relation_id"))
    except wb.WorkbenchError as exc:
        raise _wb_error(exc) from None


@router.patch("/ontology/entities/{entity_id}")
def patch_entity(entity_id: str, payload: EntityPatch):
    try:
        return wb.update_entity(_as_uuid(entity_id, "entity_id"),
                                payload.canonical_name, payload.entity_type, payload.aliases)
    except wb.WorkbenchError as exc:
        raise _wb_error(exc) from None


# ---------------------------------------------------------------- 图 ↔ 本体同步
@router.get("/ontology/sync/status")
def sync_status():
    """同步状态一屏：边按来源分桶、事实/实体基数、三类孤儿计数。"""
    from app.modules.ontology import sync as graph_sync

    return graph_sync.status()


@router.get("/ontology/sync/events")
def sync_events_status():
    """事实变更实时同步的自检：监听线程、触发器、已处理事件数、启动补偿结果。"""
    from app.modules.ontology import fact_events

    return fact_events.stats()


@router.get("/ontology/sync/orphans")
def sync_orphans(limit: int = 50):
    """三类对不上的边（只读）：事实已撤/实体已停用/人工边无事实支撑。"""
    from app.modules.ontology import sync as graph_sync

    return graph_sync.orphans(limit=max(1, min(500, limit)))


@router.post("/ontology/sync")
def run_sync(dry_run: bool = True):
    """一键对齐：事实→边对账 + 修孤儿边 + 给人工边补事实。

    默认 dry-run：先看「会加几条、会删几条」，再显式 `dry_run=false` 落库。
    """
    from app.modules.ontology import sync as graph_sync

    return graph_sync.sync_all(dry_run=dry_run)


@router.post("/ontology/sync/fact/{fact_id}")
def sync_one_fact(fact_id: str):
    """单条事实重投影：active 建/续边，非 active 停边。修数据用。"""
    from app.modules.ontology import sync as graph_sync

    return graph_sync.sync_fact(_as_uuid(fact_id, "fact_id"))


@router.post("/ontology/sync/entity/{entity_id}")
def sync_one_entity(entity_id: str):
    """单个实体变更后重算它的边（改名/改类型/停用）。"""
    from app.modules.ontology import sync as graph_sync

    return graph_sync.sync_entity(_as_uuid(entity_id, "entity_id"))
