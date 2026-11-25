"""
Ontology Studio V2 FastAPI 路由骨架。

前缀建议：/api/v1/studio-v2

注册示例（在 backend/app/api/v1/__init__.py 或 router 聚合处）：

    from app.api.v1.studio_v2 import router as studio_v2_router
    api_router.include_router(studio_v2_router)

依赖：pandas, networkx, rdflib, openpyxl（读 xlsx）, openai（可选问答）
逻辑严格复用 app.modules.ontology.studio_v2.core / service。
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from app.modules.ontology.studio_v2.core import (
    ask_llm,
    execute_ontology_tool,
    find_shortest_path,
    instance_graph_payload,
    quality_report,
    schema_graph_payload,
)
from app.modules.ontology.studio_v2.service import StudioV2Store, default_store

router = APIRouter(prefix="/studio-v2", tags=["ontology-studio-v2"])


def _store() -> StudioV2Store:
    return default_store


# ---------------------------------------------------------------------
# Pydantic 请求体
# ---------------------------------------------------------------------
class ManualLink(BaseModel):
    name: str
    source: str
    target: str
    source_col: str
    description: str = "手动指定"


class BuildFromRecordsRequest(BaseModel):
    """JSON 方式建本体（不经文件上传）。tables: {表名: [行对象, ...]}"""
    tables: Dict[str, List[Dict[str, Any]]]
    primary_keys: Optional[Dict[str, str]] = None
    manual_links: Optional[List[ManualLink]] = None
    name: str = ""
    session_id: Optional[str] = None


class ObjectCreateRequest(BaseModel):
    type_name: str
    data: Dict[str, Any] = Field(default_factory=dict)


class ObjectUpdateRequest(BaseModel):
    type_name: str
    primary_key: str
    updates: Dict[str, Any]


class ObjectDeleteRequest(BaseModel):
    type_name: str
    primary_key: str


class PathRequest(BaseModel):
    source_node: str
    target_node: str


class ToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    question: str
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o-mini"
    mode: str = Field(
        default="retrieve",
        description="retrieve | sample | full（降级上下文模式；工具调用请用 /ask/tools）",
    )
    max_objects: Optional[int] = 30


# ---------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------
@router.get("/sessions")
def list_sessions() -> Dict[str, Any]:
    return {"sessions": _store().list_sessions()}


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return sess.summary()


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> Dict[str, Any]:
    ok = _store().delete(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return {"deleted": True, "session_id": session_id}


# ---------------------------------------------------------------------
# 桥接：Studio V2 会话 → automodel 任务（打通 指标/问答/评测/版本 流水线）
# ---------------------------------------------------------------------
@router.post("/build/from-dataset/{dataset_id}")
def build_from_dataset(dataset_id: str) -> Dict[str, Any]:
    """从「1 数据源」落库的数据集直接建 Studio V2 会话（自动建模）。

    数据集（TableSet）→ 每张表一个 DataFrame → AutoOntologyBuilder
    自动推断主键与外键关系 → 生成会话。之后可「导入流水线」生成 automodel job。
    """
    from app.modules.ontology.automodel import versions as am_versions

    ts = am_versions.load_dataset(dataset_id, "default")
    if ts is None:
        raise HTTPException(404, f"数据集不存在: {dataset_id}")
    if not ts.tables:
        raise HTTPException(400, "数据集没有表数据")

    tables: Dict[str, pd.DataFrame] = {t.name: t.df for t in ts.tables.values()}
    name = f"dataset:{dataset_id[:8]}"
    sess = _store().build_from_dataframes(tables, name=name)
    sess.meta["source_info"] = {"kind": "dataset", "dataset_id": dataset_id, "name": name}
    for mapping in sess.ontology.data_mapping.values():
        mapping["source_kind"] = "dataset"
        mapping["dataset_id"] = dataset_id
    return {"ok": True, "session_id": sess.session_id, "name": name,
            "dataset_id": dataset_id, **sess.summary()}


@router.post("/sessions/{session_id}/to-automodel")
def session_to_automodel(session_id: str) -> Dict[str, Any]:
    """把 Studio V2 会话导入 automodel 流水线。

    会话里的表数据 → TableSet → pipeline.run（profile/discovery/draft/
    auto_fix/map/validate/materialize/metrics 全阶段）→ 生成 automodel job。
    之后指标、问答、评测、版本全部走 /automodel/jobs/{job_id}/* 既有接口。
    """
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    from app.modules.ontology.automodel import connectors, pipeline as am_pipeline

    # 1) 会话表 → TableSet（表名做安全清洗，与 connectors.from_files 一致）
    tables: Dict[str, pd.DataFrame] = {}
    for type_name, info in sess.ontology.object_types.items():
        objs = sess.ontology.objects.get(type_name, {})
        rows = []
        pk = info.get("primary_key", "")
        for pk_value, obj in objs.items():
            row = {pk: pk_value, **{k: v for k, v in obj.items() if k != pk}}
            rows.append(row)
        tables[type_name] = pd.DataFrame(rows)

    source_id = "studio_v2"
    ts = connectors.TableSet(source_id=source_id, kind="file")
    for name, df in tables.items():
        safe = connectors._table_name(name)
        df.columns = [connectors._table_name(str(c)) for c in df.columns]
        ts.tables[safe] = connectors.Table(name=safe, df=df, row_count=len(df))

    # 2) 跑完整 automodel 流水线（含指标生成）
    job = am_pipeline.run(ts, existing_schema=None, llm=None)

    # 3) 落库（与 /automodel/upload 同款持久化路径）
    from app.modules.ontology.automodel import jobs as am_jobs, versions as am_versions

    am_jobs.save(job, "default", "owner", {"kind": "studio_v2", "session_id": session_id})
    ds = am_versions.save_dataset(ts, name=f"studio-v2:{sess.name or session_id[:8]}",
                                  tenant="default", actor="owner")
    job["dataset_id"] = ds.get("id")
    am_jobs.save(job, "default", "owner", {"kind": "studio_v2", "session_id": session_id})
    am_versions.link_job(job["id"], ds.get("id"))

    return {
        "ok": True,
        "job_id": job["id"],
        "status": job.get("status"),
        "stats": (job.get("draft") or {}).get("stats"),
        "metrics": (job.get("metrics") or {}).get("stats"),
        "dataset_id": ds.get("id"),
    }


# ---------------------------------------------------------------------
# 建模：上传文件 / JSON 记录
# ---------------------------------------------------------------------
def _read_upload(file: UploadFile) -> pd.DataFrame:
    name = (file.filename or "data").lower()
    raw = file.file.read()
    bio = io.BytesIO(raw)
    if name.endswith(".csv"):
        try:
            return pd.read_csv(bio)
        except UnicodeDecodeError:
            bio.seek(0)
            return pd.read_csv(bio, encoding="gb18030")
    if name.endswith((".xlsx", ".xls", ".xlsm")):
        return pd.read_excel(bio)
    raise HTTPException(
        status_code=400,
        detail=f"不支持的文件格式：{file.filename}（仅 CSV / Excel）",
    )


@router.post("/build/upload")
async def build_from_upload(
    files: List[UploadFile] = File(..., description="多表 CSV/Excel"),
    primary_keys_json: str = Form("{}", description='主键映射 JSON，如 {"员工":"工号"}'),
    manual_links_json: str = Form("[]", description="手动关系 JSON 数组"),
    name: str = Form(""),
    session_id: Optional[str] = Form(None),
) -> Dict[str, Any]:
    """
    多文件上传 → AutoOntologyBuilder.from_dataframes（与 V2 同源）。
    表名 = 文件名去扩展名。
    """
    try:
        primary_keys = json.loads(primary_keys_json or "{}")
        manual_links_raw = json.loads(manual_links_json or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"JSON 解析失败: {exc}") from exc

    if not isinstance(primary_keys, dict):
        raise HTTPException(status_code=400, detail="primary_keys_json 须为对象")
    if not isinstance(manual_links_raw, list):
        raise HTTPException(status_code=400, detail="manual_links_json 须为数组")

    tables: Dict[str, pd.DataFrame] = {}
    for f in files:
        table_name = (f.filename or "table").rsplit(".", 1)[0]
        tables[table_name] = _read_upload(f)

    if not tables:
        raise HTTPException(status_code=400, detail="未收到有效文件")

    try:
        sess = _store().build_from_dataframes(
            tables,
            primary_keys=primary_keys or None,
            manual_links=manual_links_raw or None,
            name=name,
            session_id=session_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sess.meta["source_info"] = {"kind": "uploaded_files", "files": [f.filename or "table" for f in files]}
    return {"ok": True, **sess.summary()}


@router.post("/build/records")
def build_from_records(body: BuildFromRecordsRequest) -> Dict[str, Any]:
    """JSON 行记录建本体。"""
    if not body.tables:
        raise HTTPException(status_code=400, detail="tables 不能为空")

    frames = {
        name: pd.DataFrame(rows) for name, rows in body.tables.items()
    }
    links = [m.model_dump() for m in (body.manual_links or [])]
    try:
        sess = _store().build_from_dataframes(
            frames,
            primary_keys=body.primary_keys,
            manual_links=links or None,
            name=body.name,
            session_id=body.session_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    sess.meta["source_info"] = {"kind": "records", "tables": list(frames.keys())}
    return {"ok": True, **sess.summary()}


@router.post("/infer-primary-keys")
async def infer_primary_keys(
    files: List[UploadFile] = File(...),
) -> Dict[str, Any]:
    """仅推断主键，不建会话。"""
    tables: Dict[str, pd.DataFrame] = {}
    for f in files:
        table_name = (f.filename or "table").rsplit(".", 1)[0]
        tables[table_name] = _read_upload(f)
    pks = _store().infer_primary_keys(tables)
    return {
        "primary_keys": pks,
        "columns": {n: list(df.columns.astype(str)) for n, df in tables.items()},
        "row_counts": {n: len(df) for n, df in tables.items()},
    }


# ---------------------------------------------------------------------
# 摘要 / Schema / 实例图 / 质量 / 路径
# ---------------------------------------------------------------------
@router.get("/sessions/{session_id}/summary")
def session_summary(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session_id": session_id,
        "summary_markdown": sess.ontology.summary(),
        **sess.summary(),
    }


@router.get("/sessions/{session_id}/schema-graph")
def get_schema_graph(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return schema_graph_payload(sess.ontology)


@router.get("/sessions/{session_id}/instance-graph")
def get_instance_graph(
    session_id: str,
    types: Optional[str] = Query(
        None, description="逗号分隔实体类型；省略则全部"
    ),
    max_nodes: int = Query(500, ge=10, le=10000),
    search: str = Query(""),
    focus_node: Optional[str] = Query(None),
    hops: int = Query(1, ge=1, le=5),
) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    selected = [t.strip() for t in types.split(",") if t.strip()] if types else None
    return instance_graph_payload(
        sess.ontology,
        selected_types=selected,
        max_nodes=max_nodes,
        search_text=search,
        focus_node=focus_node,
        hops=hops,
    )


@router.get("/sessions/{session_id}/quality")
def get_quality(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    report = quality_report(sess.ontology)
    # NetworkX 自环/重复边可能含不可 JSON 的结构，做一次安全转换
    report["self_loops"] = [
        list(x) if isinstance(x, tuple) else x for x in report["self_loops"]
    ]
    report["duplicate_edges"] = [
        list(x) if isinstance(x, tuple) else x for x in report["duplicate_edges"]
    ]
    return report


@router.post("/sessions/{session_id}/path")
def post_path(session_id: str, body: PathRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = find_shortest_path(sess.ontology, body.source_node, body.target_node)
    if not path:
        return {
            "found": False,
            "source": body.source_node,
            "target": body.target_node,
            "message": "未找到路径",
        }
    steps = []
    for left, right in zip(path[:-1], path[1:]):
        edge_data = sess.ontology.links.get_edge_data(left, right) or {}
        if not edge_data:
            edge_data = sess.ontology.links.get_edge_data(right, left) or {}
        relations = [
            str(d.get("relation_type") or k) for k, d in edge_data.items()
        ]
        steps.append({"from": left, "to": right, "relations": relations})
    return {
        "found": True,
        "length": len(path) - 1,
        "path": path,
        "steps": steps,
    }


@router.get("/sessions/{session_id}/objects/{type_name}")
def list_objects(
    session_id: str,
    type_name: str,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if type_name not in sess.ontology.object_types:
        raise HTTPException(status_code=404, detail=f"未知类型: {type_name}")
    items = list(sess.ontology.objects.get(type_name, {}).values())
    total = len(items)
    page = items[offset: offset + limit]
    return {
        "type": type_name,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": page,
    }


# ---------------------------------------------------------------------
# Action：创建 / 更新 / 删除
# ---------------------------------------------------------------------
@router.post("/sessions/{session_id}/objects")
def create_object(session_id: str, body: ObjectCreateRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        pk = sess.ontology.create_object(body.type_name, body.data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if pk is None:
        raise HTTPException(status_code=400, detail=f"未知类型: {body.type_name}")
    sess.touch()
    return {
        "ok": True,
        "type": body.type_name,
        "primary_key": pk,
        "data": sess.ontology.objects[body.type_name][pk],
    }


@router.patch("/sessions/{session_id}/objects")
def update_object(session_id: str, body: ObjectUpdateRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        ok = sess.ontology.update_object(
            body.type_name, body.primary_key, body.updates
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="对象不存在")
    sess.touch()
    return {
        "ok": True,
        "type": body.type_name,
        "primary_key": body.primary_key,
        "data": sess.ontology.objects[body.type_name][str(body.primary_key)],
    }


@router.delete("/sessions/{session_id}/objects")
def delete_object(session_id: str, body: ObjectDeleteRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    ok = sess.ontology.delete_object(body.type_name, body.primary_key)
    if not ok:
        raise HTTPException(status_code=404, detail="对象不存在")
    sess.touch()
    return {"ok": True, "type": body.type_name, "primary_key": body.primary_key}


@router.get("/sessions/{session_id}/actions")
def list_actions(session_id: str, limit: int = Query(50, ge=1, le=500)) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    log = sess.ontology.actions_log
    return {"total": len(log), "items": log[-limit:]}


# ---------------------------------------------------------------------
# Schema 编辑（图编辑器）：编辑后自动保存 + 重建实例边
# ---------------------------------------------------------------------
class SchemaEditOp(BaseModel):
    op: str = Field(description="create_class | update_class | delete_class | add_property | update_property | delete_property | create_relation | update_relation | delete_relation")
    ref: str = ""
    type_name: str = ""
    primary_key: str = ""
    properties: List[str] = Field(default_factory=list)
    property_name: str = ""
    property_type: str = ""
    source_table: str = ""
    source_column: str = ""
    clear_source_mapping: bool = False
    label: str = ""
    description: str = ""
    source_property: str = ""
    new_name: str = ""
    source: str = ""
    target: str = ""
    target_property: str = ""


class SchemaEditRequest(BaseModel):
    ops: List[SchemaEditOp]


class CanvasLayoutRequest(BaseModel):
    positions: Dict[str, Dict[str, float]] = Field(default_factory=dict)


@router.get("/sessions/{session_id}/layout")
def get_canvas_layout(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"positions": sess.meta.get("canvas_layout", {}), "storage": "process_session"}


@router.post("/sessions/{session_id}/layout")
def save_canvas_layout(session_id: str, body: CanvasLayoutRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    valid_ids = {node["id"] for node in schema_graph_payload(sess.ontology)["nodes"]
                 if node.get("kind") == "class"}
    positions = {}
    for node_id, point in body.positions.items():
        if node_id not in valid_ids:
            continue
        x, y = point.get("x"), point.get("y")
        if x is None or y is None or not (-100000 <= x <= 100000) or not (-100000 <= y <= 100000):
            raise HTTPException(400, f"画布坐标无效：{node_id}")
        positions[node_id] = {"x": float(x), "y": float(y)}
    sess.meta["canvas_layout"] = positions
    sess.touch()
    return {"ok": True, "saved": len(positions), "storage": "process_session"}


@router.post("/sessions/{session_id}/compare-dataset/{dataset_id}")
def compare_source_dataset(session_id: str, dataset_id: str) -> Dict[str, Any]:
    """Compare a newly uploaded dataset with the source schema without mutating the ontology."""
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from app.modules.ontology.automodel import versions as am_versions
    dataset = am_versions.load_dataset(dataset_id, "default")
    if dataset is None:
        raise HTTPException(404, f"数据集不存在：{dataset_id}")
    current = sess.ontology.data_mapping
    incoming = {table.name: table for table in dataset.tables.values()}
    added_tables = sorted(set(incoming) - set(current))
    removed_tables = sorted(set(current) - set(incoming))
    table_diffs = []
    for table_name in sorted(set(current) & set(incoming)):
        old = current[table_name]
        table = incoming[table_name]
        new_columns = [str(c) for c in table.df.columns]
        old_columns = list(old.get("columns", []))
        old_types = old.get("column_types", {})
        new_types = {str(col): str(dtype) for col, dtype in table.df.dtypes.items()}
        table_diffs.append({
            "table": table_name,
            "added_columns": [c for c in new_columns if c not in old_columns],
            "removed_columns": [c for c in old_columns if c not in new_columns],
            "type_changes": [{"column": c, "before": old_types.get(c), "after": new_types.get(c)}
                             for c in new_columns if c in old_types and old_types.get(c) != new_types.get(c)],
            "rows_before": old.get("row_count", 0),
            "rows_after": len(table.df),
        })
    current_dataset_id = sess.meta.get("source_info", {}).get("dataset_id")
    dataset_changed = bool(current_dataset_id and current_dataset_id != dataset_id)
    changed = bool(dataset_changed or added_tables or removed_tables or any(
        d["added_columns"] or d["removed_columns"] or d["type_changes"] or d["rows_before"] != d["rows_after"]
        for d in table_diffs
    ))
    return {
        "ok": True, "requires_review": changed, "mutated": False,
        "dataset_changed": dataset_changed,
        "current_dataset_id": current_dataset_id,
        "incoming_dataset_id": dataset_id,
        "added_tables": added_tables, "removed_tables": removed_tables,
        "table_diffs": table_diffs,
        "message": "发现上游数据变化，已生成差异报告；本次比较不会自动修改本体或映射。" if changed else "数据结构与记录数未发现变化。",
    }


@router.get("/sessions/{session_id}/source-lineage")
def get_source_lineage(session_id: str) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    graph = schema_graph_payload(sess.ontology)
    return {
        "session_id": session_id,
        "source_info": sess.meta.get("source_info", {}),
        "tables": list(sess.ontology.data_mapping.values()),
        "objects": [n for n in graph["nodes"] if n.get("kind") == "class"],
        "properties": [n for n in graph["nodes"] if n.get("kind") == "property"],
        "relations": [e for e in graph["edges"] if e.get("kind") == "objectProperty"],
    }


@router.post("/sessions/{session_id}/schema/edit")
def schema_edit(session_id: str, body: SchemaEditRequest) -> Dict[str, Any]:
    """图编辑器提交的批量 Schema 编辑：应用后自动保存（重建实例边）并返回新摘要。"""
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    onto = sess.ontology
    done: List[str] = []
    for o in body.ops:
        if o.op == "create_class":
            name = o.type_name or o.ref
            if not name or not o.primary_key:
                raise HTTPException(400, "新增业务对象必须提供 type_name 和 primary_key")
            if name in onto.object_types:
                raise HTTPException(409, f"对象已存在：{name}")
            props = list(o.properties)
            if o.primary_key not in props:
                props.insert(0, o.primary_key)
            onto.add_object_type(name, o.primary_key, props, o.description)
            done.append(f"新增对象 {name}")
        elif o.op == "update_class":
            if onto.update_object_type(o.ref, label=o.label, description=o.description):
                done.append(f"更新类 {o.ref}")
        elif o.op == "delete_class":
            if onto.remove_object_type(o.ref):
                done.append(f"删除类 {o.ref}")
        elif o.op == "add_property":
            type_name = o.type_name or o.ref
            if onto.add_property(type_name, o.property_name, source_column=o.source_column,
                                 property_type=o.property_type, description=o.description):
                done.append(f"新增属性 {type_name}.{o.property_name}")
        elif o.op == "update_property":
            if onto.update_property(o.type_name, o.property_name, new_name=o.new_name,
                                    source_column=o.source_column, property_type=o.property_type,
                                    description=o.description, clear_source_mapping=o.clear_source_mapping):
                done.append(f"更新属性 {o.type_name}.{o.property_name}")
        elif o.op == "delete_property":
            if onto.remove_property(o.type_name, o.property_name):
                done.append(f"删除属性 {o.type_name}.{o.property_name}")
        elif o.op == "create_relation":
            name = o.new_name or o.ref
            if not name or o.source not in onto.object_types or o.target not in onto.object_types:
                raise HTTPException(400, "新增关系必须指定有效的名称、起点和终点对象")
            if name in onto.link_types:
                raise HTTPException(409, f"关系已存在：{name}")
            if o.source_property and o.source_property not in onto.object_types[o.source].get("properties", []):
                raise HTTPException(400, f"起点属性不存在：{o.source}.{o.source_property}")
            if o.target_property and o.target_property not in onto.object_types[o.target].get("properties", []):
                raise HTTPException(400, f"终点属性不存在：{o.target}.{o.target_property}")
            onto.add_link_type(name, o.source, o.target, o.description,
                               source_property=o.source_property, target_property=o.target_property)
            done.append(f"新增关系 {name}")
        elif o.op == "update_relation":
            current = onto.link_types.get(o.ref)
            if current is None:
                continue
            source_type = o.source or current.get("source")
            target_type = o.target or current.get("target")
            if source_type not in onto.object_types or target_type not in onto.object_types:
                raise HTTPException(400, "关系起点或终点对象不存在")
            if o.source_property and o.source_property not in onto.object_types[source_type].get("properties", []):
                raise HTTPException(400, f"起点属性不存在：{source_type}.{o.source_property}")
            if o.target_property and o.target_property not in onto.object_types[target_type].get("properties", []):
                raise HTTPException(400, f"终点属性不存在：{target_type}.{o.target_property}")
            if onto.update_link_type(
                o.ref,
                description=o.description,
                source_property=o.source_property,
                source=o.source,
                target=o.target,
                target_property=o.target_property,
                new_name=o.new_name,
            ):
                done.append(f"更新关系 {o.new_name or o.ref}")
        elif o.op == "delete_relation":
            if onto.remove_link_type(o.ref):
                done.append(f"删除关系 {o.ref}")
    if not done:
        raise HTTPException(400, "没有可执行的编辑操作")
    # 自动保存：重建实例边 + 刷新会话时间戳
    onto.rebuild_links()
    sess.touch()
    return {"ok": True, "done": done, **sess.summary()}


# ---------------------------------------------------------------------
# 工具调用 / 问答
# ---------------------------------------------------------------------
@router.post("/sessions/{session_id}/tools")
def run_tool(session_id: str, body: ToolCallRequest) -> Dict[str, Any]:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    result = execute_ontology_tool(sess.ontology, body.name, body.arguments)
    return {"tool": body.name, "result": result}


@router.post("/sessions/{session_id}/ask")
def ask(session_id: str, body: AskRequest) -> Dict[str, Any]:
    """降级模式问答（检索/抽样上下文塞进 prompt）。"""
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    answer = ask_llm(
        body.question,
        sess.ontology,
        api_key=body.api_key,
        base_url=body.base_url,
        model=body.model,
        max_objects=body.max_objects,
        context_mode=body.mode,
    )
    return {"answer": answer, "mode": body.mode}


class AskToolsRequest(BaseModel):
    """Tool Calling 问答（v3 默认模式：按需调用 search/get/neighbors/path/count）。"""

    question: str
    api_key: str = ""
    base_url: str = ""
    model: str = "gpt-4o-mini"
    max_rounds: int = 6


@router.post("/sessions/{session_id}/ask/tools")
def ask_tools(session_id: str, body: AskToolsRequest) -> Dict[str, Any]:
    """Tool Calling 问答：流式聚合为完整回答后返回。"""
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    from app.modules.ontology.studio_v2.core import ask_llm_with_tools

    status_parts: list[str] = []
    token_parts: list[str] = []
    error_parts: list[str] = []
    for kind, text in ask_llm_with_tools(
        body.question,
        sess.ontology,
        api_key=body.api_key,
        base_url=body.base_url,
        model=body.model,
        max_rounds=body.max_rounds,
    ):
        if kind == "status":
            status_parts.append(text)
        elif kind == "token":
            token_parts.append(text)
        else:
            error_parts.append(text)
    return {
        "answer": "".join(token_parts) or "".join(error_parts) or "模型没有返回文本。",
        "status": "".join(status_parts),
        "error": "".join(error_parts) or None,
        "mode": "tools",
    }


# ---------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------
@router.get("/sessions/{session_id}/export.json")
def export_json(session_id: str) -> Response:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = sess.ontology.export_json()
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="ontology_{session_id[:8]}.json"'
        },
    )


@router.get("/sessions/{session_id}/export.owl")
def export_owl(session_id: str) -> Response:
    try:
        sess = _store().require(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        payload = sess.ontology.export_owl()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"OWL 导出失败: {exc}") from exc
    return Response(
        content=payload,
        media_type="application/rdf+xml",
        headers={
            "Content-Disposition": f'attachment; filename="ontology_{session_id[:8]}.owl"'
        },
    )


@router.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "module": "ontology-studio-v2",
        "sessions": len(_store().list_sessions()),
        "source": "ontology_studio_v2.py core extraction",
    }
