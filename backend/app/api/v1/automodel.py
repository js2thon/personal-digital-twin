"""自动建模 API：上传文件 / 数据库连接 → 自动本体 → 审核 → 发布 → 写库 → 指标 → 问答。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /automodel/upload            | 上传 CSV/Excel/JSON/Parquet（multipart，多文件）→ 建任务并跑到 metrics |
| POST | /automodel/database          | 数据库连接 → 建任务（连接串只在内存，source_ref 只存脱敏信息） |
| GET  | /automodel/jobs              | 任务列表 |
| GET  | /automodel/jobs/{id}         | 任务详情（画像/草案/审核队列/契约/统计/指标） |
| POST | /automodel/jobs/{id}/review  | 提交审核决策（accept/reject/rename/label），重建契约与指标 |
| POST | /automodel/jobs/{id}/publish | 发布本体到 Registry + 指标草稿入库 |
| POST | /automodel/jobs/{id}/write   | 写入 entities/facts（触发器→Outbox→图投影） |
| GET  | /automodel/jobs/{id}/metrics | 指标清单 + 血缘（lineage） |
| POST | /automodel/jobs/{id}/metrics/evaluate | 计算指标（可选维度） |
| POST | /automodel/jobs/{id}/ask     | AI 自助问答 |
| GET  | /automodel/jobs/{id}/suggest | 推荐问题 |
| GET  | /automodel/jobs/{id}/contract.yaml | 映射契约 YAML |

数据源的 DataFrame 只缓存在进程内（TTL 内存）；服务重启后需重新上传 / 重新连接再 rebuild。
"""
from __future__ import annotations

import math
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.modules.ontology.automodel import connectors, evaluate as ev, jobs, mapper, metrics as mx, pipeline, qa, studio
from app.modules.ontology.automodel import versions as vs

router = APIRouter()

MAX_FILES = 20
MAX_BYTES = 50 * 1024 * 1024
ALLOWED_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".parquet"}
_TTL = 6 * 3600
_SOURCES: dict[str, tuple[float, connectors.TableSet]] = {}
_FRAMES: dict[str, tuple] = {}
_LOCK = threading.Lock()


def _tenant(request: Request) -> tuple[str, str]:
    p = getattr(getattr(request, "state", None), "principal", None) or {}
    return str(p.get("tenant_id") or "default"), str(p.get("user_id") or "owner")


def _llm():
    try:
        from app.modules.llm.client import llm_client
        return llm_client
    except Exception:  # noqa: BLE001
        return None


def _keep(job_id: str, ts: connectors.TableSet) -> None:
    now = time.time()
    with _LOCK:
        for k in [k for k, (t, _) in _SOURCES.items() if now - t > _TTL]:
            _SOURCES.pop(k, None)
            _FRAMES.pop(k, None)
        _SOURCES[job_id] = (now, ts)
        _FRAMES.pop(job_id, None)


def _source(job_id: str) -> connectors.TableSet:
    with _LOCK:
        hit = _SOURCES.get(job_id)
    if not hit:
        did = vs.job_dataset(job_id)
        ts = vs.load_dataset(did) if did else None      # 内存过期 / 服务重启：从落库数据集恢复
        if ts is not None:
            _keep(job_id, ts)
            return ts
        raise HTTPException(409, "数据源已过期（服务重启或超过 6 小时），请重新上传或重新连接")
    return hit[1]


def _job(job_id: str, request: Request) -> dict:
    tenant, _ = _tenant(request)
    job = jobs.get(job_id, tenant)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


def _frames(job: dict):
    with _LOCK:
        hit = _FRAMES.get(job["id"])
    if hit:
        return hit
    if not job.get("materialized") or "entities" not in job["materialized"]:
        job["materialized"] = pipeline.materializer.materialize(_source(job["id"]), job["contract"])
    F, L = mx.frames(job["materialized"], job["draft"]["schema"])
    with _LOCK:
        _FRAMES[job["id"]] = (F, L)
    return F, L


def _summary(job: dict) -> dict:
    d = job.get("draft") or {}
    return {"id": job["id"], "source": job["source"], "status": job["status"],
            "stages": job.get("stages"), "stats": d.get("stats"),
            "review": d.get("review"), "issues": job.get("issues"),
            "materialized": (job.get("materialized") or {}).get("stats"),
            "metrics": (job.get("metrics") or {}).get("stats"),
            "business_object_discovery": job.get("business_object_discovery"),
            "standards_report": job.get("standards_report"),
            "release_gate": job.get("release_gate"),
            "schema_version": job.get("schema_version")}


def _snap(job: dict, tenant: str, actor: str, trigger: str, label: str = "") -> dict | None:
    try:
        return vs.create(job, tenant, actor, label=label, trigger=trigger,
                         dataset_id=job.get("dataset_id") or vs.job_dataset(job["id"]))
    except Exception:  # noqa: BLE001  版本快照失败不影响主流程
        return None


def _existing_schema() -> dict | None:
    try:
        from app.modules.ontology import cq_v2
        return cq_v2.get_engine().registry.current()
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 建任务
@router.post("/automodel/upload")
async def upload(request: Request, files: list[UploadFile] = File(...), source_id: str = "upload",
                 use_llm: bool = True, dataset_name: str = ""):
    tenant, actor = _tenant(request)
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"文件数需在 1～{MAX_FILES}")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", source_id):
        raise HTTPException(400, "source_id 只允许字母数字下划线和短横线")
    tmp = Path(tempfile.mkdtemp(prefix="automodel_"))
    try:
        paths = []
        for f in files:
            name = Path(f.filename or "data.csv").name
            if Path(name).suffix.lower() not in ALLOWED_EXT:
                raise HTTPException(400, f"不支持的文件类型：{name}")
            dest = tmp / name
            size = 0
            with dest.open("wb") as out:
                while chunk := await f.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise HTTPException(413, f"{name} 超过 50MB")
                    out.write(chunk)
            paths.append(dest)
        try:
            ts = connectors.from_files(paths, source_id=source_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    job = pipeline.run(ts, existing_schema=_existing_schema(), llm=_llm() if use_llm else None)
    _keep(job["id"], ts)
    ds = vs.save_dataset(ts, name=dataset_name or ",".join(p.name for p in paths), tenant=tenant, actor=actor)
    job["dataset_id"] = ds.get("id")
    jobs.save(job, tenant, actor, {"kind": "file", "files": [p.name for p in paths], "dataset_id": ds.get("id")})
    vs.link_job(job["id"], ds.get("id"))
    _snap(job, tenant, actor, "upload", "上传生成")
    return {**_summary(job), "dataset": ds}


class DbIn(BaseModel):
    url: str
    db_schema: str | None = Field(default=None, alias="schema")
    include: list[str] | None = None
    source_id: str = "db"
    use_llm: bool = True
    store_data: bool = False

    model_config = {"populate_by_name": True}


def _redact(url: str) -> dict:
    u = urlparse(url)
    return {"kind": "database", "dialect": u.scheme, "host": u.hostname, "port": u.port,
            "database": (u.path or "").lstrip("/"), "user": u.username}


@router.post("/automodel/database")
def database(payload: DbIn, request: Request):
    tenant, actor = _tenant(request)
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", payload.source_id):
        raise HTTPException(400, "source_id 只允许字母数字下划线和短横线")
    from app.config import settings
    allow_private = bool(getattr(settings, "automodel_allow_private_db", True))
    try:
        ts = connectors.from_database(payload.url, schema=payload.db_schema, include=payload.include,
                                      source_id=payload.source_id, allow_private=allow_private)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001  驱动错误不回显连接串
        raise HTTPException(502, f"数据库读取失败：{type(e).__name__}")
    job = pipeline.run(ts, existing_schema=_existing_schema(), llm=_llm() if payload.use_llm else None)
    _keep(job["id"], ts)
    ds = {}
    if payload.store_data:                          # 数据库来源默认不落原始数据，只存脱敏来源
        ds = vs.save_dataset(ts, name=f"{_redact(payload.url).get('database')}", tenant=tenant, actor=actor)
        job["dataset_id"] = ds.get("id")
    jobs.save(job, tenant, actor, _redact(payload.url))
    vs.link_job(job["id"], ds.get("id"))
    _snap(job, tenant, actor, "upload", "数据库建模")
    return {**_summary(job), "dataset": ds}


# ------------------------------------------------------------------ 查看 / 审核 / 发布 / 写库
@router.get("/automodel/jobs")
def list_jobs(request: Request, limit: int = 50):
    tenant, _ = _tenant(request)
    return {"items": jobs.listing(tenant, limit)}


@router.get("/automodel/jobs/{job_id}")
def get_job(job_id: str, request: Request, full: bool = False):
    job = _job(job_id, request)
    if not full:
        return {**_summary(job), "schema": job["draft"]["schema"], "class_map": job["draft"]["class_map"],
                "inferences": job["draft"]["inferences"]}
    return {k: v for k, v in job.items() if k != "materialized"} | {
        "materialized": (job.get("materialized") or {}).get("stats")}


class ReviewIn(BaseModel):
    decisions: list[dict] = Field(default_factory=list)
    acknowledge_warnings: bool = False


@router.post("/automodel/jobs/{job_id}/review")
def review(job_id: str, payload: ReviewIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] in ("published", "written"):
        raise HTTPException(409, "已发布的任务不能再审核，请新建任务")
    for d in payload.decisions:
        if d.get("kind") not in ("class", "relation", "attribute") or \
                d.get("action") not in ("accept", "reject", "rename", "merge"):
            raise HTTPException(400, f"非法审核决策：{d}")
        if d.get("new_name") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", d["new_name"]):
            raise HTTPException(400, f"new_name 非法：{d['new_name']}")
    pipeline.apply_review(job, payload.decisions, actor=actor)
    pipeline.rebuild(job, _source(job_id))
    if payload.acknowledge_warnings:
        pipeline.acknowledge_standards_warnings(job, actor)
    with _LOCK:
        _FRAMES.pop(job_id, None)
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "review", "审核")
    return _summary(job)


class PublishIn(BaseModel):
    version: str | None = None


@router.post("/automodel/jobs/{job_id}/publish")
def publish(job_id: str, payload: PublishIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] in ("published", "written"):
        raise HTTPException(409, "已发布")
    try:
        res = pipeline.approve_and_publish(job, actor, tenant, payload.version)
    except ValueError as e:
        raise HTTPException(409, str(e))
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "publish", f"发布 {job.get('schema_version') or ''}")
    return res


@router.post("/automodel/jobs/{job_id}/write")
def write(job_id: str, request: Request, visibility: str = "owner"):
    tenant, actor = _tenant(request)
    if visibility not in ("owner", "organization", "restricted"):
        raise HTTPException(400, "visibility 非法")
    job = _job(job_id, request)
    _frames(job)                                   # 确保物化明细在内存
    from app.db import session as db
    try:
        res = pipeline.write(job, db, visibility)
    except ValueError as e:
        raise HTTPException(409, str(e))
    jobs.save(job, tenant, actor)
    return res


@router.get("/automodel/jobs/{job_id}/contract.yaml", response_class=PlainTextResponse)
def contract_yaml(job_id: str, request: Request):
    return mapper.to_yaml(_job(job_id, request)["contract"])


# ------------------------------------------------------------------ 指标 / 问答
@router.get("/automodel/jobs/{job_id}/metrics")
def list_metrics(job_id: str, request: Request):
    job = _job(job_id, request)
    ms = job["metrics"]
    return {"stats": ms["stats"], "issues": ms["issues"], "metrics": ms["metrics"],
            "lineage": mx.lineage(ms, job["draft"]["schema"])}


class EvalIn(BaseModel):
    metric_ids: list[str] = Field(default_factory=list)
    dim: str | None = None


@router.post("/automodel/jobs/{job_id}/metrics/evaluate")
def evaluate(job_id: str, payload: EvalIn, request: Request):
    job = _job(job_id, request)
    ms, schema = job["metrics"]["metrics"], job["draft"]["schema"]
    F, L = _frames(job)
    ids = payload.metric_ids or [k for k, m in ms.items() if m["kind"] != "derived" or m.get("grain")]
    out = {}
    for mid in ids[:100]:
        if mid not in ms:
            raise HTTPException(404, f"指标不存在：{mid}")
        if payload.dim and payload.dim not in (ms[mid].get("dims_allowed") or []):
            out[mid] = {"error": "维度不适用"}
            continue
        try:
            v = mx.evaluate(mid, ms, F, L, schema, payload.dim)
        except Exception as e:  # noqa: BLE001
            out[mid] = {"error": f"{type(e).__name__}: {e}"[:200]}
            continue
        if hasattr(v, "to_dict"):
            import numpy as np
            v = v.replace([np.inf, -np.inf], np.nan).dropna()
            out[mid] = {"index": [str(i) for i in v.index], "values": [float(x) for x in v.values]}
        else:
            f = None if v is None else float(v)
            out[mid] = {"value": f if f is not None and math.isfinite(f) else None}
    return {"items": out}


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    use_llm: bool = True


@router.post("/automodel/jobs/{job_id}/ask")
def ask(job_id: str, payload: AskIn, request: Request):
    job = _job(job_id, request)
    F, L = _frames(job)
    return qa.ask(payload.question, job["draft"]["schema"], job["metrics"], F, L,
                  llm=_llm() if payload.use_llm else None, contract=job.get("contract"))


@router.get("/automodel/jobs/{job_id}/suggest")
def suggest(job_id: str, request: Request):
    job = _job(job_id, request)
    return {"items": qa.suggest(job["draft"]["schema"], job["metrics"])}


# ------------------------------------------------------------------ 本体工作台（对齐 ontology_studio_v2）
@router.get("/automodel/jobs/{job_id}/studio/overview")
def st_overview(job_id: str, request: Request):
    job = _job(job_id, request)
    _frames(job)
    return studio.overview(job)


@router.get("/automodel/jobs/{job_id}/studio/objects")
def st_objects(job_id: str, request: Request, cls: str, q: str = "", offset: int = 0, limit: int = 100):
    job = _job(job_id, request)
    _frames(job)
    return studio.objects(job, cls, q, max(0, offset), max(1, min(limit, 500)))


@router.get("/automodel/jobs/{job_id}/studio/schema-graph")
def st_schema_graph(job_id: str, request: Request, props: bool = True):
    job = _job(job_id, request)
    _frames(job)
    return studio.schema_graph(job, props)


class GraphIn(BaseModel):
    types: list[str] | None = None
    q: str = ""
    focus: str | None = None
    hops: int = 1
    max_nodes: int = 300


@router.post("/automodel/jobs/{job_id}/studio/instance-graph")
def st_instance_graph(job_id: str, payload: GraphIn, request: Request):
    job = _job(job_id, request)
    _frames(job)
    return studio.instance_graph(job, payload.types, payload.q, payload.focus, payload.hops,
                                 max(10, min(payload.max_nodes, 3000)))


@router.get("/automodel/jobs/{job_id}/studio/neighbors")
def st_neighbors(job_id: str, request: Request, entity: str):
    job = _job(job_id, request)
    _frames(job)
    return studio.neighbors(job, entity)


@router.get("/automodel/jobs/{job_id}/studio/path")
def st_path(job_id: str, request: Request, source: str, target: str, directed: bool = False):
    job = _job(job_id, request)
    _frames(job)
    return studio.shortest_path(job, source, target, directed)


@router.get("/automodel/jobs/{job_id}/studio/quality")
def st_quality(job_id: str, request: Request):
    job = _job(job_id, request)
    _frames(job)
    return studio.quality(job)


@router.get("/automodel/jobs/{job_id}/studio/governance")
def st_governance(job_id: str, request: Request):
    job = _job(job_id, request)
    _frames(job)
    principal = getattr(getattr(request, "state", None), "principal", None) or {}
    return studio.governance(job, principal)


@router.get("/automodel/jobs/{job_id}/studio/provenance")
def st_provenance(job_id: str, request: Request, entity: str, predicate: str | None = None,
                  limit: int = 200):
    job = _job(job_id, request)
    _frames(job)
    try:
        return studio.provenance(job, entity, predicate, limit)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/automodel/jobs/{job_id}/studio/provenance/source")
def st_source_provenance(job_id: str, request: Request, table: str, column: str, limit: int = 200):
    job = _job(job_id, request)
    _frames(job)
    return studio.source_provenance(job, table, column, limit)


class ActionIn(BaseModel):
    action: str                     # create / update / delete
    cls: str | None = None
    entity: str | None = None
    data: dict = Field(default_factory=dict)


@router.post("/automodel/jobs/{job_id}/studio/action")
def st_action(job_id: str, payload: ActionIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    _frames(job)
    try:
        if payload.action == "create":
            res = {"entity": studio.create_object(job, payload.cls or "", dict(payload.data))}
        elif payload.action == "update":
            studio.update_object(job, payload.entity or "", dict(payload.data))
            res = {"entity": payload.entity}
        elif payload.action == "delete":
            res = {"entity": payload.entity, "facts_removed": studio.delete_object(job, payload.entity or "")}
        else:
            raise HTTPException(400, "action 只能是 create / update / delete")
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    with _LOCK:
        _FRAMES.pop(job_id, None)                 # 物化变更后重建问答/指标用的帧
    if job.get("status") == "written":
        job["status"] = "published"               # 有未落库的编辑：需再次「写入」
    jobs.save(job, tenant, actor)
    return {**res, "actions": len(job.get("actions_log") or []), "status": job["status"]}


@router.get("/automodel/jobs/{job_id}/studio/actions")
def st_actions(job_id: str, request: Request):
    return {"items": (_job(job_id, request).get("actions_log") or [])[-50:]}


@router.get("/automodel/jobs/{job_id}/export.json")
def st_export_json(job_id: str, request: Request):
    from fastapi.responses import JSONResponse
    job = _job(job_id, request)
    _frames(job)
    return JSONResponse(studio.export_json(job), headers={
        "Content-Disposition": f'attachment; filename="ontology_{job["source"]}.json"'})


@router.get("/automodel/jobs/{job_id}/export.owl")
def st_export_owl(job_id: str, request: Request):
    from fastapi.responses import Response
    job = _job(job_id, request)
    _frames(job)
    try:
        body = studio.export_owl(job)
    except ImportError:
        raise HTTPException(501, "缺少 rdflib：pip install rdflib")
    return Response(body, media_type="application/rdf+xml", headers={
        "Content-Disposition": f'attachment; filename="ontology_{job["source"]}.owl"'})


class ConfigIn(BaseModel):
    primary_keys: dict[str, str] = Field(default_factory=dict)      # {表名: 主键列}
    manual_links: list[dict] = Field(default_factory=list)          # [{name, source, source_col, target, description}]


@router.post("/automodel/jobs/{job_id}/configure")
def st_configure(job_id: str, payload: ConfigIn, request: Request):
    """建模配置（v2：主键选择 + 手动关系）：覆盖推断主键/补关系后重跑整条流水线。"""
    tenant, actor = _tenant(request)
    old = _job(job_id, request)
    ts = _source(job_id)
    for t, pk in payload.primary_keys.items():
        if t not in ts.tables or pk not in ts.tables[t].df.columns:
            raise HTTPException(400, f"主键配置无效：{t}.{pk}")
        ts.tables[t].primary_key = [pk]
    for lk in payload.manual_links:
        s, c, tg = lk.get("source"), lk.get("source_col"), lk.get("target")
        if s not in ts.tables or tg not in ts.tables or c not in ts.tables[s].df.columns:
            raise HTTPException(400, f"手动关系无效：{lk}")
        tpk = (payload.primary_keys.get(tg) or (ts.tables[tg].primary_key or [None])[0]
               or next(iter(old["profiles"][tg]["primary_key"] or []), None))
        if not tpk:
            raise HTTPException(400, f"目标表 {tg} 没有主键，请先指定")
        ts.tables[s].foreign_keys = [fk for fk in ts.tables[s].foreign_keys if fk.column != c] + [
            connectors.ForeignKey(c, tg, tpk, declared=True)]
    job = pipeline.run(ts, existing_schema=_existing_schema(), llm=None)
    job["id"] = job_id
    names = {lk.get("source_col"): (lk.get("name") or "").strip() for lk in payload.manual_links}
    rels = job["draft"]["schema"]["relation_types"]
    for rn, r in list(rels.items()):
        nn = names.get(r.get("column"))
        if nn and nn != rn and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", nn):
            pipeline.apply_review(job, [{"kind": "relation", "ref": rn, "action": "rename", "new_name": nn}])
    for lk in payload.manual_links:
        if lk.get("description"):
            for r in rels.values():
                if r.get("column") == lk.get("source_col"):
                    r["label"] = lk["description"]
    pipeline.rebuild(job, ts)
    _keep(job_id, ts)
    job["config"] = payload.model_dump()
    job["dataset_id"] = old.get("dataset_id")
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "configure", "主键/关系配置")
    return _summary(job)


# ------------------------------------------------------------------ 本体评测
class EvalGenIn(BaseModel):
    n_per_type: int = Field(default=3, ge=1, le=10)
    seed: int = 7


@router.post("/automodel/jobs/{job_id}/eval/questions")
def eval_questions(job_id: str, payload: EvalGenIn, request: Request):
    """由本体 + 实例自动生成评测问题集（标准答案独立计算，不经问答引擎）。"""
    job = _job(job_id, request)
    F, _ = _frames(job)
    qs = ev.generate(job["draft"]["schema"], F, payload.n_per_type, payload.seed)
    job["eval_questions"] = qs
    return {"total": len(qs), "items": qs}


class EvalRunIn(BaseModel):
    questions: list[dict] | None = None
    use_llm: bool = False


@router.post("/automodel/jobs/{job_id}/eval/run")
def eval_run(job_id: str, payload: EvalRunIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    F, L = _frames(job)
    qs = payload.questions or job.get("eval_questions") or ev.generate(job["draft"]["schema"], F)
    llm = _llm() if payload.use_llm else None
    res = ev.run(qs, lambda q: qa.ask(q, job["draft"]["schema"], job["metrics"], F, L, llm=llm,
                                      contract=job.get("contract")))
    res["run_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    res["use_llm"] = payload.use_llm
    job["eval_last"] = {k: v for k, v in res.items() if k != "items"}
    jobs.save(job, tenant, actor)
    return res


@router.post("/automodel/admin/restart")
def admin_restart(request: Request):
    """本机重启 API（仅回环地址）：进程退出后由 start_hidden.py 守护在约 10 秒内拉起。"""
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "只允许本机调用")
    import os
    import threading

    threading.Timer(0.5, lambda: os._exit(3)).start()
    return {"restarting": True, "eta_seconds": 15}


# ------------------------------------------------------------------ 数据集 & 版本管理
@router.get("/automodel/datasets")
def ds_list(request: Request, limit: int = 100):
    tenant, _ = _tenant(request)
    return {"items": vs.list_datasets(tenant, limit)}


@router.delete("/automodel/datasets/{dataset_id}")
def ds_delete(dataset_id: str, request: Request):
    tenant, _ = _tenant(request)
    return {"deleted": vs.delete_dataset(dataset_id, tenant)}


class FromDsIn(BaseModel):
    use_llm: bool = False
    version_id: str | None = None        # 可选：直接套用某个历史版本的本体（复用审核结果）


@router.post("/automodel/datasets/{dataset_id}/jobs")
def ds_new_job(dataset_id: str, payload: FromDsIn, request: Request):
    """复用已落库数据集新建任务；可选套用历史版本的本体与映射。"""
    tenant, actor = _tenant(request)
    ts = vs.load_dataset(dataset_id, tenant)
    if ts is None:
        raise HTTPException(404, "数据集不存在")
    job = pipeline.run(ts, existing_schema=_existing_schema(), llm=_llm() if payload.use_llm else None)
    job["dataset_id"] = dataset_id
    if payload.version_id:
        v = vs.get(payload.version_id, tenant)
        if not v:
            raise HTTPException(404, "版本不存在")
        vs.apply(job, v)
        job["status"] = "draft"
        job["schema_version"] = None
    _keep(job["id"], ts)
    jobs.save(job, tenant, actor, {"kind": "dataset", "dataset_id": dataset_id})
    vs.link_job(job["id"], dataset_id)
    _snap(job, tenant, actor, "upload", "复用数据集" + ("·套用版本" if payload.version_id else ""))
    return _summary(job)


@router.get("/automodel/jobs/{job_id}/versions")
def v_list(job_id: str, request: Request):
    tenant, _ = _tenant(request)
    _job(job_id, request)
    return {"items": vs.listing(job_id, tenant), "dataset_id": vs.job_dataset(job_id)}


class VersionIn(BaseModel):
    label: str = ""
    note: str = ""


@router.post("/automodel/jobs/{job_id}/versions")
def v_create(job_id: str, payload: VersionIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    return vs.create(job, tenant, actor, label=payload.label, note=payload.note, trigger="manual",
                     dataset_id=job.get("dataset_id") or vs.job_dataset(job_id), dedupe=False)


@router.get("/automodel/versions/diff")
def v_diff(a: str, b: str, request: Request):
    tenant, _ = _tenant(request)
    va, vb = vs.get(a, tenant), vs.get(b, tenant)
    if not va or not vb:
        raise HTTPException(404, "版本不存在")
    return vs.diff(va, vb)


@router.get("/automodel/versions/{version_id}")
def v_get(version_id: str, request: Request):
    tenant, _ = _tenant(request)
    v = vs.get(version_id, tenant)
    if not v:
        raise HTTPException(404, "版本不存在")
    return v


@router.patch("/automodel/versions/{version_id}")
def v_patch(version_id: str, payload: VersionIn, request: Request):
    tenant, _ = _tenant(request)
    if not vs.get(version_id, tenant):
        raise HTTPException(404, "版本不存在")
    vs.update_meta(version_id, tenant, payload.label or None, payload.note or None)
    return {"ok": True}


@router.post("/automodel/jobs/{job_id}/versions/{version_id}/restore")
def v_restore(job_id: str, version_id: str, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    v = vs.get(version_id, tenant)
    if not v or v["job_id"] != job_id:
        raise HTTPException(404, "版本不存在或不属于该任务")
    vs.apply(job, v)
    if job.get("status") == "written":
        job["status"] = "published"
    with _LOCK:
        _FRAMES.pop(job_id, None)
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "restore", f"回滚到 v{v['version_no']}")
    return _summary(job)


class SchemaEditIn(BaseModel):
    kind: str                     # class / attribute / relation
    ref: str                      # 类名 / 属性键 / 关系名
    label: str | None = None
    description: str | None = None
    datatype: str | None = None


@router.post("/automodel/jobs/{job_id}/schema/edit")
def schema_edit(job_id: str, payload: SchemaEditIn, request: Request):
    """可视化编辑：直接改类/属性/关系的中文名、描述、数据类型（不改结构，不重跑流水线）。"""
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] in ("published", "written"):
        raise HTTPException(409, "已发布的本体请新建版本后再改")
    sc = job["draft"]["schema"]
    if payload.kind == "class":
        tgt = sc["entity_types"].get(payload.ref)
    elif payload.kind == "relation":
        tgt = sc.get("relation_types", {}).get(payload.ref)
    elif payload.kind == "attribute":
        tgt = (sc.get("attributes") or {}).get(payload.ref.split(".")[-1])
    else:
        tgt = None
    if tgt is None:
        raise HTTPException(404, f"找不到 {payload.kind} {payload.ref}")
    if payload.datatype and payload.kind != "attribute":
        raise HTTPException(400, "只有属性能改数据类型")
    for k in ("label", "description", "datatype"):
        v = getattr(payload, k)
        if v is not None:
            if len(v) > 500:
                raise HTTPException(400, f"{k} 过长")
            if k == "datatype" and v not in ("string", "integer", "float", "boolean", "date", "datetime"):
                raise HTTPException(400, "datatype 只能是 string/integer/float/boolean/date/datetime")
            tgt[k if k != "datatype" else "range"] = v
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "edit", f"编辑 {payload.ref}")
    return {"ok": True, "target": tgt}


@router.post("/automodel/jobs/{job_id}/metrics/republish")
def metrics_republish(job_id: str, request: Request):
    """已发布任务：按当前本体重新生成指标（修复非法 ID 等）并重新入库指标草稿，不重发本体。"""
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] not in ("published", "written"):
        raise HTTPException(409, "任务尚未发布，请先发布本体")
    from app.modules.ontology.automodel import publish as pb
    job["metrics"] = mx.generate(job["draft"]["schema"])
    with _LOCK:
        _FRAMES.pop(job_id, None)
    res = pb.publish_metrics(job["metrics"], job["draft"]["schema"], tenant, actor, job.get("schema_version"))
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "publish", "重新发布指标")
    return res


class ColumnEdit(BaseModel):
    table: str
    column: str
    label: str | None = None          # 中文名（同步到本体属性 label）
    comment: str | None = None        # 业务描述（同步到属性 description）
    role: str | None = None           # identifier/name/enum/measure/temporal/text


class MetadataIn(BaseModel):
    tables: dict[str, str] = Field(default_factory=dict)      # {表名: 表注释}
    columns: list[ColumnEdit] = Field(default_factory=list)


ROLES = {"identifier", "name", "enum", "measure", "temporal", "text", "boolean"}


@router.post("/automodel/jobs/{job_id}/metadata")
def edit_metadata(job_id: str, payload: MetadataIn, request: Request):
    """数据源元数据编辑：表注释、列中文名/描述/角色。同步到本体属性；角色变化会重算指标。"""
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] in ("published", "written"):
        raise HTTPException(409, "已发布的任务元数据只读；请在「数据集库」复用建模后修改")
    P, sc = job.get("profiles") or {}, job["draft"]["schema"]
    cmap = job["draft"].get("class_map") or {}
    role_changed = False
    for t, c in payload.tables.items():
        if t not in P:
            raise HTTPException(400, f"表不存在：{t}")
        P[t]["comment"] = c[:500]
        cls = cmap.get(t)
        if cls in sc["entity_types"] and c:
            sc["entity_types"][cls]["description"] = c[:500]
    for e in payload.columns:
        col = ((P.get(e.table) or {}).get("columns") or {}).get(e.column)
        if col is None:
            raise HTTPException(400, f"列不存在：{e.table}.{e.column}")
        if e.role is not None and e.role not in ROLES:
            raise HTTPException(400, f"角色非法：{e.role}")
        for v in (e.label, e.comment):
            if v is not None and len(v) > 500:
                raise HTTPException(400, "文本过长")
        if e.comment is not None:
            col["comment"] = e.comment
        if e.label is not None:
            col["label"] = e.label
        if e.role is not None and e.role != col.get("role"):
            col["role"] = e.role
            role_changed = True
        cls = cmap.get(e.table)
        for a in sc.get("attributes", {}).values():
            if a.get("owner") == cls and a.get("column") == e.column:
                if e.label is not None:
                    a["label"] = e.label
                if e.comment is not None:
                    a["description"] = e.comment
                if e.role is not None:
                    a["role"] = e.role
    if role_changed:
        job["metrics"] = mx.generate(sc)
        with _LOCK:
            _FRAMES.pop(job_id, None)
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "edit", "元数据编辑")
    return {"ok": True, "role_changed": role_changed,
            "metrics": (job.get("metrics") or {}).get("stats")}


# ------------------------------------------------------------------ 本体设计器（React Flow）
class DesignOp(BaseModel):
    op: str                              # add_relation / delete / rename / update
    kind: str | None = None              # class / attribute / relation
    ref: str | None = None
    new_name: str | None = None
    label: str | None = None
    description: str | None = None
    datatype: str | None = None
    source_table: str | None = None      # add_relation：外键所在表
    column: str | None = None            # add_relation：外键列
    target_class: str | None = None      # add_relation：目标类
    owner: str | None = None             # add_attribute：所属类
    role: str | None = None              # add_attribute：角色
    parent: str | None = None            # add_class：父类（is_a）


class DesignIn(BaseModel):
    ops: list[DesignOp] = Field(default_factory=list)


_IDN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_TYPES = ("string", "integer", "float", "boolean", "date", "datetime")


@router.post("/automodel/jobs/{job_id}/design/ops")
def design_ops(job_id: str, payload: DesignIn, request: Request):
    """设计图上的增删改：结构变化（增关系/删/改名）走审核管线并重建契约与物化；纯标注（中文名/描述/类型）原地更新。"""
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    if job["status"] in ("published", "written"):
        raise HTTPException(409, "已发布的本体只读；请在「8 版本」回滚或在数据集库复用建模后修改")
    sc, cmap = job["draft"]["schema"], job["draft"]["class_map"]
    structural, decisions, done = False, [], []
    # 结构性操作需要源数据重建契约与物化：先取数据源，失败则一个改动都不做（避免内存中半改状态）
    ts = _source(job_id) if any(o.op in ("delete", "rename", "add_relation", "add_class", "add_attribute", "rebind")
                                for o in payload.ops) else None
    import copy
    backup = copy.deepcopy(job["draft"])
    for o in payload.ops:
        if o.op == "update":
            sec = {"class": "entity_types", "relation": "relation_types", "attribute": "attributes"}.get(o.kind or "")
            tgt = (sc.get(sec) or {}).get(o.ref or "") if sec else None
            if tgt is None:
                raise HTTPException(404, f"找不到 {o.kind} {o.ref}")
            if o.datatype is not None:
                if o.kind != "attribute" or o.datatype not in _TYPES:
                    raise HTTPException(400, "数据类型非法")
                tgt["range"] = o.datatype
            for k in ("label", "description"):
                v = getattr(o, k)
                if v is not None:
                    tgt[k] = v[:500]
            done.append(f"更新 {o.ref}")
        elif o.op in ("delete", "rename"):
            if o.kind not in ("class", "attribute", "relation") or not o.ref:
                raise HTTPException(400, "kind/ref 必填")
            if o.op == "rename" and not (o.new_name and _IDN.fullmatch(o.new_name)):
                raise HTTPException(400, f"新名称非法：{o.new_name}（字母/下划线开头的英文标识符）")
            decisions.append({"kind": o.kind, "ref": o.ref, "action": "reject" if o.op == "delete" else "rename",
                              "new_name": o.new_name, "label": o.label})
            structural = True
            done.append(("删除 " if o.op == "delete" else "改名 ") + o.ref)
        elif o.op == "rebind":
            # 改属性绑定的源列：column 为空字符串 → 改成虚拟属性；其他 → 绑定到该列（须为所属类源表中未占用的列）
            at = sc["attributes"].get(o.ref or "")
            if at is None:
                raise HTTPException(404, f"找不到属性 {o.ref}")
            cls = at["owner"]
            if at.get("identifier") or (at.get("column") and at.get("column") in (sc["entity_types"][cls].get("key") or [])):
                raise HTTPException(400, "主键属性不能改绑")
            new_col = (o.column or "").strip()
            old = at.get("column")
            if new_col:
                tab = next((t for t, c in cmap.items() if c == cls), None)
                cols = ((job.get("profiles") or {}).get(tab) or {}).get("columns") or {}
                if not tab or new_col not in cols:
                    raise HTTPException(400, f"{cls} 的源表中没有列 {new_col}")
                if any(k != o.ref and a.get("owner") == cls and a.get("column") == new_col for k, a in sc["attributes"].items()):
                    raise HTTPException(400, f"列 {new_col} 已被其他属性占用")
                if any(r.get("table") == tab and r.get("column") == new_col for r in sc["relation_types"].values()):
                    raise HTTPException(400, f"列 {new_col} 是关系的外键列，不能绑定为属性")
                if new_col == old:
                    continue
                at["column"] = new_col
                at.pop("virtual", None)
                cp = cols[new_col]
                at.pop("enum_values", None)
                if cp.get("role") == "enum" and cp.get("samples"):
                    at["enum_values"] = cp["samples"]
                if o.datatype is None:            # 未指定类型则按新列画像推断
                    at["range"] = {"integer": "integer", "float": "float", "date": "date", "datetime": "datetime",
                                   "boolean": "boolean"}.get(cp.get("dtype"), "string")
                if o.role is None and cp.get("role") in ROLES:
                    at["role"] = cp["role"]
            else:
                if not old:
                    continue
                at.pop("column", None)
                at.pop("enum_values", None)          # 旧列的枚举值不再适用
                at["virtual"] = True
            if o.role is not None:
                if o.role not in ROLES:
                    raise HTTPException(400, f"角色非法：{o.role}")
                at["role"] = o.role
            structural = True
            done.append(f"改绑 {o.ref}：{old or '虚拟'} → {new_col or '虚拟'}")
        elif o.op == "add_class":
            name = o.new_name or ""
            if not _IDN.fullmatch(name) or name in sc["entity_types"]:
                raise HTTPException(400, f"类名非法或已存在：{name}（英文标识符，建议 PascalCase）")
            if o.parent and o.parent not in sc["entity_types"]:
                raise HTTPException(400, f"父类不存在：{o.parent}")
            sc["entity_types"][name] = {"label": (o.label or name)[:200], "description": (o.description or "")[:500],
                                        "key": [], "origin": "designer", **({"is_a": o.parent} if o.parent else {})}
            job["draft"]["inferences"].append({"kind": "class", "ref": name, "confidence": 1.0, "reason": "设计器新建"})
            structural = True                    # 无源表：不进映射契约，实例可在「4 实例与关系」用 Action 创建
            done.append(f"新建类 {name}")
        elif o.op == "add_attribute":
            name, cls = o.new_name or "", o.owner or ""
            if cls not in sc["entity_types"]:
                raise HTTPException(400, f"所属类不存在：{cls}")
            if not _IDN.fullmatch(name) or name in sc["attributes"]:
                raise HTTPException(400, f"属性名非法或已存在：{name}")
            dt = o.datatype or "string"
            if dt not in _TYPES:
                raise HTTPException(400, "数据类型非法")
            role = o.role or ("measure" if dt in ("integer", "float") else "temporal" if dt in ("date", "datetime") else "text")
            if role not in ROLES:
                raise HTTPException(400, f"角色非法：{role}")
            col = None
            if o.column:                         # 绑定源表现有列 → 进入映射契约，重建后有值
                tab = next((t for t, c in cmap.items() if c == cls), None)
                cols = ((job.get("profiles") or {}).get(tab) or {}).get("columns") or {}
                if not tab or o.column not in cols:
                    raise HTTPException(400, f"{cls} 的源表中没有列 {o.column}")
                if any(a.get("owner") == cls and a.get("column") == o.column for a in sc["attributes"].values()):
                    raise HTTPException(400, f"列 {o.column} 已映射为属性")
                col = o.column
            sc["attributes"][name] = {"range": dt, "owner": cls, "role": role, "label": (o.label or name)[:200],
                                      **({"column": col} if col else {"virtual": True}),
                                      **({"description": o.description[:500]} if o.description else {}), "origin": "designer"}
            job["draft"]["inferences"].append({"kind": "attribute", "ref": name, "confidence": 1.0, "reason": "设计器新建"})
            structural = True
            done.append(f"新建属性 {cls}.{name}" + (f"（列 {col}）" if col else "（虚拟）"))
        elif o.op == "add_relation":
            t, c, tc = o.source_table, o.column, o.target_class
            if t not in ts.tables or c not in ts.tables[t].df.columns:
                raise HTTPException(400, f"外键列不存在：{t}.{c}")
            if t not in cmap or tc not in sc["entity_types"]:
                raise HTTPException(400, "源表未映射为类，或目标类不存在")
            ttab = next((k for k, v in cmap.items() if v == tc), None)
            tpk = (ts.tables[ttab].primary_key or (job["profiles"][ttab].get("primary_key") or [None]))[0] if ttab else None
            if not tpk:
                raise HTTPException(400, f"目标类 {tc} 没有主键")
            name = o.new_name or f"{c.removesuffix('_id')}_{tc.lower()}"
            if not _IDN.fullmatch(name) or name in sc["relation_types"]:
                raise HTTPException(400, f"关系名非法或已存在：{name}")
            ts.tables[t].foreign_keys = [fk for fk in ts.tables[t].foreign_keys if fk.column != c] + [
                connectors.ForeignKey(c, ttab, tpk, declared=True)]
            sc["relation_types"][name] = {"label": o.label or name, "source": cmap[t], "target": tc,
                                          "cardinality": "many_to_one", "column": c, "table": t,
                                          "description": o.description or "设计器手动添加"}
            job["draft"]["inferences"].append({"kind": "relation", "ref": name, "confidence": 1.0,
                                               "reason": "设计器手动添加"})
            structural = True
            done.append(f"新增关系 {name}")
        else:
            raise HTTPException(400, f"未知操作：{o.op}")
    try:
        if decisions:
            pipeline.apply_review(job, decisions)
        if structural:
            pipeline.rebuild(job, ts)
            with _LOCK:
                _FRAMES.pop(job_id, None)
    except Exception:
        job["draft"] = backup                  # 失败回滚，保持与库内一致
        raise
    jobs.save(job, tenant, actor)
    _snap(job, tenant, actor, "edit", "设计器：" + "；".join(done)[:150])
    return {**_summary(job), "done": done, "issues": job.get("issues")}


class LayoutIn(BaseModel):
    positions: dict[str, dict] = Field(default_factory=dict)   # {node_id: {x, y}}


@router.post("/automodel/jobs/{job_id}/design/layout")
def design_layout(job_id: str, payload: LayoutIn, request: Request):
    tenant, actor = _tenant(request)
    job = _job(job_id, request)
    job["design_layout"] = {k: {"x": float(v.get("x", 0)), "y": float(v.get("y", 0))}
                            for k, v in list(payload.positions.items())[:2000]}
    jobs.save(job, tenant, actor)
    return {"ok": True, "saved": len(job["design_layout"])}


@router.get("/automodel/jobs/{job_id}/pipeline")
def pipeline_view(job_id: str, request: Request):
    """建模流水线 DAG：各阶段状态、耗时与产物摘要（React Flow 渲染）。"""
    job = _job(job_id, request)
    st = job.get("stages") or {}
    d = job.get("draft") or {}
    stt = d.get("stats") or {}
    mat = (job.get("materialized") or {}).get("stats") or {}
    errs = [i for i in (job.get("issues") or []) if i.get("level") == "error"]
    status = job.get("status")
    pub = status in ("published", "written")
    def node(i, label, state, detail, tab=None):
        return {"id": i, "label": label, "state": state, "detail": detail, "ms": (st.get(i) or {}).get("ms"), "tab": tab}
    P = job.get("profiles") or {}
    nodes = [
        node("source", "数据源", "done", f"{len(P)} 张表 · {job.get('source')}", "src"),
        node("profile", "数据画像", "done" if "profile" in st else "todo",
             f"{sum(len(p.get('columns') or {}) for p in P.values())} 列", "src"),
        node("draft", "本体草案", "done" if "draft" in st else "todo",
             f"类 {len(d.get('schema', {}).get('entity_types', {}))} · 关系 {len(d.get('schema', {}).get('relation_types', {}))} · 属性 {len(d.get('schema', {}).get('attributes', {}))}", "model"),
        node("review", "人工审核", "warn" if d.get("review") else "done", f"待审核 {len(d.get('review') or [])}", "model"),
        node("map", "映射契约", "done" if job.get("contract") else "todo", f"{len((job.get('contract') or {}).get('mappings') or [])} 个映射", "map"),
        node("validate", "契约校验", "error" if errs else ("done" if job.get("issues") is not None else "todo"),
             f"错误 {len(errs)} · 提示 {len(job.get('issues') or []) - len(errs)}", "map"),
        node("materialize", "物化", "done" if mat else "todo", f"实体 {mat.get('entities', '—')} · 事实 {mat.get('facts', '—')}", "inst"),
        node("metrics", "指标生成", "done" if job.get("metrics") else "todo",
             " · ".join(f"{k} {v}" for k, v in ((job.get("metrics") or {}).get("stats") or {}).items()), "metrics"),
        node("publish", "发布", "done" if pub else "todo", job.get("schema_version") or "未发布", "map"),
        node("write", "写入图谱", "done" if status == "written" else "todo",
             f"实体 {(job.get('written') or {}).get('entities_upserted', '—')} · 事实 {(job.get('written') or {}).get('facts_inserted', '—')}", "map"),
    ]
    edges = [{"source": a["id"], "target": b["id"]} for a, b in zip(nodes, nodes[1:])]
    return {"nodes": nodes, "edges": edges, "status": status, "layout": job.get("design_layout") or {}}
