"""本体数据映射 API。

仅保留被本体工作台数据 Pipeline 使用的字段映射、映射版本审批、
数据集预览/执行与字段血缘接口。旧版本体建模画布 API 已下线。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class MappingSuggestIn(BaseModel):
    """数据集字段元数据 + 本体语义 Schema，生成可解释候选（不写库）。"""
    columns: list[dict]
    ontology_schema: dict | None = None
    min_score: float = 0.25
    top_k: int = 3


class MappingValidateIn(BaseModel):
    """人工确认后的字段映射；仅校验结构，不执行数据写入。"""
    mappings: list[dict]
    ontology_schema: dict | None = None

class DatasetInspectIn(BaseModel):
    """CSV/XLSX 文件以 base64 传入；限制 10 MiB。"""
    file_name: str
    content_base64: str
    sheet_name: str | None = None
    ontology_schema: dict | None = None


class DatasetPreviewIn(DatasetInspectIn):
    """使用人工确认的映射生成实例化预览；不会写库。"""
    mappings: list[dict]
    preview_limit: int = 20


class MappingSetCreateIn(BaseModel):
    """保存映射草稿快照；创建新版本，不覆盖历史版本。"""
    name: str
    mappings: list[dict]
    ontology_schema: dict | None = None


class DatasetExecuteIn(DatasetInspectIn):
    """仅允许已批准映射版本执行，写入本体实例库。"""
    mapping_set_id: str
    max_rows: int = 500
    sync_mode: str = "append"
    source_id_column: str | None = None

class DatasetStreamExecuteIn(DatasetInspectIn):
    """完整文件快照按 <=500 行分批执行；缺失记录仅支持显式软停用。"""
    mapping_set_id: str
    snapshot_id: str
    sync_mode: str = "upsert"
    source_id_column: str | None = None
    batch_size: int = 500
    reconcile_missing: bool = False


class DatabaseInspectIn(BaseModel):
    """通过服务端白名单连接配置发现数据库元数据；不接收连接串或 SQL。"""
    schema_name: str | None = None
    table_names: list[str] = []
    sample_rows: int = 5
    ontology_schema: dict | None = None



def _mapping_schema(schema: dict | None) -> dict:
    if schema:
        return schema
    from app.modules.ontology.semantic import schema_def as SD
    return SD.to_schema_dict(SD.default_semantic_schema())


@router.post("/ontology/mapping/suggest")
def mapping_suggest(payload: MappingSuggestIn, request: Request):
    """字段元数据/样例值 → 本体属性候选 + 证据 + 置信等级；纯计算。"""
    from app.modules.ontology import mapping_pipeline as MP
    if not payload.columns:
        raise HTTPException(400, "columns 不能为空")
    if not 0 <= payload.min_score <= 1:
        raise HTTPException(400, "min_score 必须在 0 到 1 之间")
    result = MP.suggest_mappings(
        payload.columns, _mapping_schema(payload.ontology_schema),
        min_score=payload.min_score, top_k=payload.top_k)
    result["principal"] = _principal(request).get("user_id", "anonymous")
    return result


@router.post("/ontology/mapping/validate")
def mapping_validate(payload: MappingValidateIn, request: Request):
    """校验人工确认的字段映射；不触发连接器、SQL 或业务数据写入。"""
    from app.modules.ontology import mapping_pipeline as MP
    result = MP.validate_mappings(payload.mappings, _mapping_schema(payload.ontology_schema))
    result["principal"] = _principal(request).get("user_id", "anonymous")
    return result


def _read_dataset_payload(file_name: str, content_base64: str, sheet_name: str | None):
    import base64
    from app.modules.ontology import mapping_pipeline as MP
    if len(content_base64) > 14 * 1024 * 1024:
        raise HTTPException(413, "文件编码后超过上传限制")
    try:
        raw = base64.b64decode(content_base64, validate=True)
        return MP.read_tabular_file(file_name, raw, sheet_name)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, f"数据集解析失败：{exc}") from exc


@router.post("/ontology/mapping/dataset/inspect")
def dataset_inspect(payload: DatasetInspectIn, request: Request):
    """解析 CSV/XLSX，返回字段画像和本体映射候选；只读。"""
    from app.modules.ontology import mapping_pipeline as MP
    dataset = _read_dataset_payload(payload.file_name, payload.content_base64, payload.sheet_name)
    schema = _mapping_schema(payload.ontology_schema)
    report = MP.suggest_mappings(dataset["columns"], schema)
    return {"dataset": {k: v for k, v in dataset.items() if k != "rows"},
            "mapping_suggestions": report, "principal": _principal(request).get("user_id", "anonymous")}


@router.post("/ontology/mapping/preview-file")
def mapping_preview_file(payload: DatasetPreviewIn, request: Request):
    """读取 CSV/XLSX 并按已确认映射生成实例、校验报告和字段血缘；不写库。"""
    from app.modules.ontology import mapping_pipeline as MP
    dataset = _read_dataset_payload(payload.file_name, payload.content_base64, payload.sheet_name)
    schema = _mapping_schema(payload.ontology_schema)
    result = MP.preview_rows(dataset["rows"], payload.mappings, schema,
                             source_name=payload.file_name, limit=payload.preview_limit)
    result["dataset"] = {k: v for k, v in dataset.items() if k != "rows"}
    result["principal"] = _principal(request).get("user_id", "anonymous")
    return result


@router.post("/ontology/mapping/sets")
def mapping_set_create(payload: MappingSetCreateIn, request: Request):
    """保存映射草稿为不可变版本快照；不触发数据写入。"""
    from app.modules.ontology import mapping_pipeline as MP
    from app.modules.ontology import mapping_registry as MR
    principal = _principal(request)
    name = payload.name.strip()
    if not name or len(name) > 200:
        raise HTTPException(400, "映射方案名称长度须为 1-200")
    schema = _mapping_schema(payload.ontology_schema)
    validation = MP.validate_mappings(payload.mappings, schema)
    if not validation["valid"]:
        raise HTTPException(400, {"message": "映射定义未通过校验", "issues": validation["issues"]})
    from app.modules.ontology.semantic import versioning as OV
    current = OV.current_version()
    result = MR.create_version(
        tenant_id=str(principal.get("tenant_id") or "default"),
        name=name,
        definition={"mappings": payload.mappings, "ontology_schema": schema},
        schema_version=str(schema.get("schema_version") or current.get("schema_version") or ""),
        ontology_version=str(schema.get("ontology_version") or current.get("ontology_version") or ""),
        created_by=str(principal.get("user_id") or "anonymous"))
    return result


@router.post("/ontology/mapping/sets/{mapping_id}/approve")
def mapping_set_approve(mapping_id: str, request: Request):
    """经当前已认证用户显式确认后，将草稿版本标记为 approved。"""
    from app.modules.ontology import mapping_registry as MR
    principal = _principal(request)
    result = MR.approve(
        mapping_id, tenant_id=str(principal.get("tenant_id") or "default"),
        approved_by=str(principal.get("user_id") or "anonymous"))
    if not result:
        raise HTTPException(404, "映射草稿不存在、非当前租户或已不是 draft 状态")
    return result


@router.post("/ontology/mapping/sets/{mapping_id}/retire")
def mapping_set_retire(mapping_id: str, request: Request):
    """停用映射版本（draft/approved → retired）。不物理删除：映射版本是写入的审计依据；
    停用后执行器只接受 approved，因此该版本不能再用于正式写入。"""
    from app.modules.ontology import mapping_registry as MR
    principal = _principal(request)
    result = MR.retire(
        mapping_id, tenant_id=str(principal.get("tenant_id") or "default"),
        retired_by=str(principal.get("user_id") or "anonymous"))
    if not result:
        raise HTTPException(404, "映射版本不存在、非当前租户或已停用")
    return result


@router.get("/ontology/mapping/sets")
def mapping_set_list(request: Request, name: str | None = None, limit: int = 50):
    """列出当前租户的映射版本及审批状态。"""
    from app.modules.ontology import mapping_registry as MR
    principal = _principal(request)
    return {"items": MR.list_versions(
        tenant_id=str(principal.get("tenant_id") or "default"),
        name=name, limit=limit)}


@router.get("/ontology/mapping/sets/{mapping_id}")
def mapping_set_get(mapping_id: str, request: Request):
    """读取当前租户的映射快照，包含绑定的本体 Schema。"""
    from app.modules.ontology import mapping_registry as MR
    principal = _principal(request)
    result = MR.get(mapping_id, tenant_id=str(principal.get("tenant_id") or "default"))
    if not result:
        raise HTTPException(404, "映射版本不存在")
    return result


@router.get("/ontology/mapping/sets/{mapping_id}/impact/{target_mapping_id}")
def mapping_set_impact(mapping_id: str, target_mapping_id: str, request: Request):
    """比较两个映射版本的影响范围；只读 Dry-run，不执行重投影。"""
    from app.modules.ontology import mapping_registry as MR
    from app.modules.ontology.mapping_impact import analyze
    tenant_id = str(_principal(request).get("tenant_id") or "default")
    old = MR.get(mapping_id, tenant_id=tenant_id)
    new = MR.get(target_mapping_id, tenant_id=tenant_id)
    if not old or not new:
        raise HTTPException(404, "映射版本不存在或不属于当前租户")
    if old.get("name") != new.get("name"):
        raise HTTPException(400, "只能比较同一映射方案的不同版本")
    return analyze(tenant_id=tenant_id, old_mapping=old, new_mapping=new)


@router.post("/ontology/mapping/dataset/execute")
def dataset_execute(payload: DatasetExecuteIn, request: Request):
    """以已批准映射版本原子化写入本体实例，并持久化字段级血缘。"""
    import base64
    import hashlib
    from app.modules.ontology import mapping_execution as ME
    principal = _principal(request)
    tenant_id = str(principal.get("tenant_id") or "default")
    actor = str(principal.get("user_id") or "anonymous")
    if payload.max_rows < 1 or payload.max_rows > ME.MAX_EXECUTION_ROWS:
        raise HTTPException(400, f"max_rows 必须在 1-{ME.MAX_EXECUTION_ROWS} 之间")
    try:
        if len(payload.content_base64) > 14 * 1024 * 1024:
            raise HTTPException(413, "文件编码后超过上传限制")
        raw = base64.b64decode(payload.content_base64, validate=True)
        from app.modules.ontology import mapping_pipeline as MP
        parsed = MP.read_tabular_file(payload.file_name, raw, payload.sheet_name)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, f"数据集解析失败：{exc}") from exc
    try:
        result = ME.execute_approved_dataset(
            tenant_id=tenant_id, mapping_set_id=payload.mapping_set_id,
            rows=parsed["rows"], source_name=payload.file_name,
            source_sha256=hashlib.sha256(raw).hexdigest(), actor=actor,
            max_rows=payload.max_rows, sync_mode=payload.sync_mode,
            source_id_column=payload.source_id_column)
        result["dataset"] = {k: v for k, v in parsed.items() if k != "rows"}
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"数据实例化失败，事务已回滚：{exc}") from exc


@router.post("/ontology/mapping/dataset/execute-stream")
def dataset_execute_stream(payload: DatasetStreamExecuteIn, request: Request):
    """完整源快照分批入库；支持稳定源键 upsert、批次检查点和可选软停用对账。"""
    import base64
    import hashlib
    from app.modules.ontology import ingestion_batches as IB
    from app.modules.ontology import mapping_pipeline as MP
    principal = _principal(request)
    tenant_id = str(principal.get("tenant_id") or "default")
    actor = str(principal.get("user_id") or "anonymous")
    if payload.batch_size < 1 or payload.batch_size > 500:
        raise HTTPException(400, "batch_size 必须在 1-500 之间")
    if payload.sync_mode not in ("append", "upsert"):
        raise HTTPException(400, "sync_mode 仅支持 append 或 upsert")
    if payload.sync_mode == "upsert" and not payload.source_id_column:
        raise HTTPException(400, "upsert 模式必须指定 source_id_column")
    if payload.reconcile_missing and payload.sync_mode != "upsert":
        raise HTTPException(400, "缺失记录对账仅支持 upsert 模式")
    try:
        if len(payload.content_base64) > 14 * 1024 * 1024:
            raise HTTPException(413, "文件编码后超过上传限制")
        raw = base64.b64decode(payload.content_base64, validate=True)
        parsed = MP.read_tabular_file(payload.file_name, raw, payload.sheet_name)
        result = IB.execute_approved_dataset_stream(
            tenant_id=tenant_id, mapping_set_id=payload.mapping_set_id,
            rows=parsed["rows"], source_name=payload.file_name,
            snapshot_id=payload.snapshot_id, actor=actor,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            sync_mode=payload.sync_mode, source_id_column=payload.source_id_column,
            batch_size=payload.batch_size, reconcile_missing=payload.reconcile_missing)
        result["dataset"] = {k: v for k, v in parsed.items() if k != "rows"}
        return result
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(422, f"分批数据摄取失败：{type(exc).__name__}") from exc


@router.get("/ontology/mapping/ingestion-runs/{run_id}")
def ingestion_run_get(run_id: str, request: Request):
    """查询当前租户的数据接入批次状态。"""
    from app.modules.ontology import mapping_execution as ME
    tenant_id = str(_principal(request).get("tenant_id") or "default")
    result = ME.get_run(run_id, tenant_id=tenant_id)
    if not result:
        raise HTTPException(404, "接入批次不存在")
    return result


@router.get("/ontology/mapping/instances/{instance_id}/lineage")
def instance_lineage_get(instance_id: str, request: Request):
    """查询本体实例逐属性来源血缘。"""
    from app.modules.ontology import mapping_execution as ME
    tenant_id = str(_principal(request).get("tenant_id") or "default")
    return {"instance_id": instance_id,
            "items": ME.get_instance_lineage(instance_id, tenant_id=tenant_id)}

@router.get("/ontology/mapping/connectors")
def database_connector_list(request: Request):
    """列出当前租户可用的服务端数据库连接配置（不返回连接串/凭据）。"""
    from app.modules.ontology import database_connector as DC
    tenant_id = str(_principal(request).get("tenant_id") or "default")
    try:
        return {"items": DC.list_profiles(tenant_id=tenant_id), "read_only": True}
    except DC.ConnectorConfigError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/ontology/mapping/connectors/{profile_name}/inspect")
def database_connector_inspect(profile_name: str, payload: DatabaseInspectIn,
                                request: Request):
    """发现白名单表结构、读取有限样本，并生成字段到本体属性的映射建议。"""
    from app.modules.ontology import database_connector as DC
    from app.modules.ontology import mapping_pipeline as MP
    principal = _principal(request)
    tenant_id = str(principal.get("tenant_id") or "default")
    if payload.sample_rows < 0 or payload.sample_rows > DC.MAX_SAMPLE_ROWS:
        raise HTTPException(400, f"sample_rows 必须在 0-{DC.MAX_SAMPLE_ROWS} 之间")
    try:
        result = DC.inspect_database(
            profile_name=profile_name, tenant_id=tenant_id,
            schema_name=payload.schema_name, table_names=payload.table_names,
            sample_rows=payload.sample_rows)
        schema = _mapping_schema(payload.ontology_schema)
        for table in result["tables"]:
            columns = []
            for column in table["columns"]:
                columns.append({
                    "name": column["name"], "data_type": column["data_type"],
                    "samples": [row.get(column["name"]) for row in table["sample_rows"]],
                })
            table["mapping_suggestions"] = MP.suggest_mappings(columns, schema)
        result["principal"] = principal.get("user_id", "anonymous")
        return result
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except DC.ConnectorConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        # Do not echo SQLAlchemy exception text: it can contain host/user details.
        raise HTTPException(502, f"数据库只读检查失败：{type(exc).__name__}") from exc


def _principal(request: Request) -> dict:
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, dict) else {}


