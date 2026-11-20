"""结构化数据接入 API：映射契约 / 确定性断言 / 同步 / 异常复核。

与文档蒸馏并列的旁路。所有写身份由服务端注入（app.auth），
契约必须 published 且请求显式 confirm=true 才会写知识层。
"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.auth import current_actor
from app.modules.structured import autodraft as ad
from app.modules.structured import contracts as ct
from app.modules.structured import files as fl
from app.modules.structured import service as svc

router = APIRouter()

# 与解析器共用同一份上限，避免接口层与 files.py 两处口径漂移。
MAX_ROWS = fl.MAX_ROWS
_MAX_BYTES = fl.MAX_BYTES
_CHUNK = 1 << 20


class ContractIn(BaseModel):
    name: str
    source_ref: dict = Field(default_factory=dict)
    mapping: dict = Field(default_factory=dict)
    note: str = ""


class RowsIn(BaseModel):
    rows: list[dict] = Field(default_factory=list)
    upload_id: str = ""
    dry_run: bool = True
    confirm: bool = False


class ReviewIn(BaseModel):
    action: str = "resolve"
    note: str = ""


class SuggestIn(BaseModel):
    rows: list[dict] = Field(default_factory=list)
    upload_id: str = ""
    use_llm: bool = True


class AdhocIn(BaseModel):
    """一次性导入：不带 contract_id，规则由当次 mapping 携带。"""
    rows: list[dict] = Field(default_factory=list)
    upload_id: str = ""
    mapping: dict = Field(default_factory=dict)
    dry_run: bool = True
    confirm: bool = False


def _payload(body: RowsIn) -> tuple[list[dict], dict | None]:
    """行来源二选一：粘贴 rows 或引用上传快照 upload_id，不允许混用。"""
    if body.upload_id:
        if body.rows:
            raise HTTPException(400, "rows 与 upload_id 只能二选一，避免两份数据混用")
        meta, rows = svc.load_upload_rows(body.upload_id)
        if not meta:
            raise HTTPException(400, "上传快照不存在或已被清除，请重新上传")
        if len(rows) > MAX_ROWS:
            raise HTTPException(400, _row_ceiling_msg(len(rows)))
        return rows, meta
    if not isinstance(body.rows, list):
        raise HTTPException(400, "rows 必须是数组")
    if len(body.rows) > MAX_ROWS:
        raise HTTPException(400, _row_ceiling_msg(len(body.rows)))
    return body.rows, None


def _row_ceiling_msg(n: int) -> str:
    return (f"共 {n} 行，超过单文件硬上限 {MAX_ROWS} 行（该上限只用于保护服务进程内存）；"
            f"常规文件超过 {fl.BATCH_ROWS} 行会自动分批，请把文件拆成多个再上传")


def _raise(result: dict) -> dict:
    if result.get("error"):
        raise HTTPException(400, "；".join(result.get("errors") or [result["error"]]))
    return result


@router.get("/structured/contracts")
def list_contracts(limit: int = 50, include_archived: bool = False):
    return ct.list_contracts(limit, include_archived=include_archived)


@router.delete("/structured/contracts/{contract_id}")
def delete_contract(contract_id: str, request: Request, purge: bool = False):
    """删除契约：零同步留痕才物理删除，有留痕降级为标记删除（保留追溯）。

    purge=true 表示明确要求物理删除，此时若存在留痕直接 400，不做静默降级。
    """
    try:
        result = ct.delete_contract(contract_id, current_actor(request), purge=purge)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not result:
        raise HTTPException(404, "契约不存在")
    return result


@router.post("/structured/contracts")
def create_contract(body: ContractIn, request: Request):
    return _raise(ct.create_contract(name=body.name, source_ref=body.source_ref,
                                     mapping=body.mapping, note=body.note,
                                     actor=current_actor(request)))


@router.get("/structured/contracts/{contract_id}")
def get_contract(contract_id: str):
    row = ct.get_contract(contract_id)
    if not row:
        raise HTTPException(404, "契约不存在")
    return row


@router.post("/structured/contracts/{contract_id}/publish")
def publish_contract(contract_id: str, request: Request):
    return _raise(ct.publish_contract(contract_id, current_actor(request)))


@router.post("/structured/contracts/{contract_id}/preview")
def preview(contract_id: str, body: RowsIn, request: Request):
    contract, err = ct.published_contract_or_error(contract_id)
    if err:
        raise HTTPException(400, "；".join(err["errors"]))
    rows, upload = _payload(body)
    return svc.preview(contract, rows, current_actor(request), upload)


@router.post("/structured/contracts/{contract_id}/sync")
def sync(contract_id: str, body: RowsIn, request: Request):
    contract, err = ct.published_contract_or_error(contract_id)
    if err:
        raise HTTPException(400, "；".join(err["errors"]))
    rows, upload = _payload(body)
    if body.dry_run or not body.confirm:
        return svc.preview(contract, rows, current_actor(request), upload)
    return _raise(svc.sync(contract, rows, current_actor(request), confirm=True,
                           upload=upload))


# ---------------------------------------------------------------- 一次性导入（默认路径）
#
# 与上面的 contracts/* 流程的区别只有一点：不需要事先建并发布契约。
# 规则由当次请求的 mapping 携带（由 /structured/suggest 从数据推导、本人确认后回传），
# 服务端把它落成一条 ephemeral 契约作为追溯锚点，然后走完全相同的断言与写入逻辑。
# 断言层、幂等键、可见性、分批、异常判定与定时同步逐字相同，没有第二条写路径。


@router.post("/structured/suggest")
def suggest(body: SuggestIn, request: Request):
    """按真实数据推导映射建议（不写库）：主键/名称列/类型/敏感列全部自动判定。"""
    rows, upload = _payload(body)
    source = {"system": "file"}
    order = None
    if upload:
        source["table"] = str(upload.get("file_name") or "")
        order = [str(c) for c in (upload.get("columns") or [])] or None
    result = ad.suggest_mapping(rows=rows, source_ref=source, use_llm=body.use_llm,
                                column_order=order)
    if result.get("error"):
        raise HTTPException(400, "；".join(result.get("errors") or [result["error"]]))
    result["upload"] = {"upload_id": str(upload.get("id") or ""),
                        "file_name": str(upload.get("file_name") or "")} if upload else None
    return result


def _adhoc_contract(body: AdhocIn, actor: str) -> dict:
    contract = ct.get_or_create_ephemeral(mapping=body.mapping, actor=actor)
    if contract.get("error"):
        raise HTTPException(400, "；".join(contract.get("errors") or [contract["error"]]))
    return contract


@router.post("/structured/adhoc/preview")
def adhoc_preview(body: AdhocIn, request: Request):
    rows, upload = _payload(body)
    actor = current_actor(request)
    return svc.preview(_adhoc_contract(body, actor), rows, actor, upload)


@router.post("/structured/adhoc/sync")
def adhoc_sync(body: AdhocIn, request: Request):
    rows, upload = _payload(body)
    actor = current_actor(request)
    contract = _adhoc_contract(body, actor)
    if body.dry_run or not body.confirm:
        return svc.preview(contract, rows, actor, upload)
    return _raise(svc.sync(contract, rows, actor, confirm=True, upload=upload))


# ---------------------------------------------------------------- 文件上传（表格 / MD / JSON）


async def _read_limited(file: UploadFile, limit: int = _MAX_BYTES) -> bytes:
    """分块读取并在超限时立刻中止：不把上限之外的内容读进内存。"""
    buf = bytearray()
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(
                413, f"文件超过上限 {limit // (1024 * 1024)} MiB，请拆分后上传")
    return bytes(buf)


@router.post("/structured/uploads")
async def upload_file(request: Request,
                      file: UploadFile = File(...),
                      contract_id: str = Form(""),
                      sheet: str = Form(""),
                      delimiter: str = Form("")):
    """上传即解析：只落快照，不写知识层；写入仍需契约已发布 + confirm。"""
    raw = await _read_limited(file, _MAX_BYTES)
    return _raise(svc.save_upload(file.filename or "upload", raw, current_actor(request),
                                  contract_id=contract_id, sheet=sheet,
                                  delimiter=delimiter or None))


@router.get("/structured/uploads")
def list_uploads(limit: int = 20):
    return svc.list_uploads(limit)


@router.get("/structured/uploads/{upload_id}")
def get_upload(upload_id: str, include_rows: bool = False):
    row = svc.get_upload(upload_id, include_rows)
    if not row:
        raise HTTPException(404, "上传快照不存在或已被清除")
    return row


@router.get("/structured/uploads/{upload_id}/rows")
def get_upload_rows(upload_id: str, page: int = 1, page_size: int = 10):
    """画布「数据采样/数据处理」实时预览：按页取真实快照行（默认 10 行）。"""
    row = svc.page_upload_rows(upload_id, page, page_size)
    if not row:
        raise HTTPException(404, "上传快照不存在或已被清除")
    return row


@router.delete("/structured/uploads/{upload_id}")
def delete_upload(upload_id: str, request: Request):
    return _raise(svc.delete_upload(upload_id, current_actor(request)))


@router.get("/structured/formats")
def formats():
    """页面与文档共用同一份格式说明，避免两处口径漂移。"""
    return {"supported": list(fl.SUPPORTED),
            "labels": {k: fl.FORMAT_LABEL[k] for k in fl.SUPPORTED},
            "max_bytes": fl.MAX_BYTES, "max_megabytes": fl.MAX_BYTES // (1024 * 1024),
            "max_rows": fl.MAX_ROWS, "max_columns": fl.MAX_COLUMNS,
            "max_cells": fl.MAX_CELLS,
            "batch_rows": fl.BATCH_ROWS,
            "note": "文件只作为行的载体；解析结果仍走映射契约与确定性断言，"
                    "不做列名语义猜测，写入需契约已发布并显式确认。"
                    f"超过 {fl.BATCH_ROWS} 行由服务端自动分批（逐批事务、行序不变），"
                    f"不截断；超过 {fl.MAX_BYTES // (1024 * 1024)} MiB 返回 413、"
                    f"超过 {fl.MAX_ROWS} 行、{fl.MAX_COLUMNS} 列或 "
                    f"{fl.MAX_CELLS:,} 个单元格（行 × 列）返回 400。"}


@router.get("/structured/sync-runs")
def list_sync_runs(contract_id: str | None = None, limit: int = 20):
    return svc.list_sync_runs(contract_id, limit)


@router.get("/structured/anomalies")
def list_anomalies(contract_id: str | None = None, status: str | None = None,
                   limit: int = 100):
    return svc.list_anomalies(contract_id, status, limit)


@router.post("/structured/anomalies/{anomaly_id}/review")
def review_anomaly(anomaly_id: str, body: ReviewIn, request: Request):
    return _raise(svc.review_anomaly(anomaly_id, body.action, body.note,
                                     current_actor(request)))


@router.get("/structured/reasons")
def reasons():
    return [ct.reason_meta(code) for code in ct.REASON_META]


@router.get("/structured/diagnostics")
def diagnostics():
    return svc.diagnostics()


@router.get("/structured/template")
def template():
    """控制台引导用的最小切片契约与示例行（组织关系，不含绩效）。"""
    return {
        "source_ref": {"system": "hr", "table": "dw_employee", "data_source_id": ""},
        "mapping": {
            "entity": {"entity_type": "Person", "pk_column": "employee_id",
                       "name_column": "employee_name", "external_id_prefix": "hr:"},
            "fields": [
                {"column": "dept_name", "predicate": "belongs_to_department",
                 "object_kind": "literal", "visibility": "organization",
                 "valid_from_column": "effective_from", "required": True},
                {"column": "job_title", "predicate": "job_title",
                 "object_kind": "literal", "visibility": "organization", "required": False},
                {"column": "manager_id", "predicate": "reports_to",
                 "object_kind": "entity_ref", "target_entity_type": "Person",
                 "target_name_column": "manager_name", "relation_type": "reports_to",
                 "visibility": "organization", "required": False},
            ],
            "sensitivity": {"default": "organization",
                            "restricted_columns": ["performance_score"]},
            "allowed_values": {},
            "expect_columns": ["employee_id", "employee_name", "dept_name",
                               "job_title", "manager_id", "manager_name",
                               "effective_from"],
        },
        "rows": [
            {"employee_id": "E1001", "employee_name": "本人", "dept_name": "数据管理部",
             "job_title": "负责人", "manager_id": "", "manager_name": "",
             "effective_from": "2024-01-01"},
            {"employee_id": "E1002", "employee_name": "某某丙", "dept_name": "数据管理部",
             "job_title": "分析师", "manager_id": "E1001", "manager_name": "本人",
             "effective_from": "2025-07-01"},
        ],
        "note": "绩效字段默认 restricted，不落库；此为最小切片示例，同步前请先 preview。",
    }
