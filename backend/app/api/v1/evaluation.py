"""质量评估 API（SIL-Spec v1.5 / P0-4 / GV-02 / SIL-A10）。

把「五组指标 + 固定测试集 + 版本对照 + 门禁」变成可调用的服务，供发布流程
直接读 GET /api/v1/evaluation/gate 的 release_blocking 字段。

只读业务数据；POST /evaluation/run 与 /evaluation/baseline 仅写评估自身报告目录。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.modules.evaluation import dataset as ds
from app.modules.evaluation import service as ev
from app.modules.evaluation import ontology_qa as oq_eval

router = APIRouter()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, {
            "code": "EVALUATION_ERROR",
            "message": f"{type(exc).__name__}: {str(exc)[:200]}",
            "hint": "检查 /api/v1/evaluation/metrics 与实库连通性",
        }) from None


class RunIn(BaseModel):
    groups: list[str] | None = Field(
        default=None, description="要跑的指标组；为空表示五组全跑")
    persist: bool = Field(default=True, description="是否落盘报告")
    regression_tolerance: float = Field(
        default=0.10, ge=0.0, le=1.0,
        description="相对回归容差，默认 10%；不设绝对性能阈值")
    bootstrap_baseline: bool = Field(
        default=True, description="无基线时把本次运行固化为首个基线")


class BaselineIn(BaseModel):
    confirm: bool = Field(default=False, description="显式确认覆盖已有基线")


@router.get("/evaluation/dataset")
def evaluation_dataset():
    """固定基准集元信息 + 用例摘要 + 已知缺口。"""
    return _call(ds.dataset_summary)


@router.get("/evaluation/metrics")
def evaluation_metrics():
    """五组指标目录与口径定义。"""
    return _call(ev.metric_catalog)


@router.post("/evaluation/run")
def evaluation_run(body: RunIn):
    """跑一次完整评估，返回报告（含门禁结论与版本对照）。"""
    return _call(ev.run_suite, groups=body.groups, persist=body.persist,
                 regression_tolerance=body.regression_tolerance,
                 bootstrap_baseline=body.bootstrap_baseline)


@router.get("/evaluation/report")
def evaluation_report():
    """最近一次报告；服务重启后自动从磁盘恢复。"""
    r = ev.latest_report()
    if not r:
        raise HTTPException(404, "尚无评估报告，请先调用 POST /api/v1/evaluation/run")
    return r


@router.get("/evaluation/report/markdown")
def evaluation_report_markdown():
    r = ev.latest_report()
    if not r:
        raise HTTPException(404, "尚无评估报告，请先调用 POST /api/v1/evaluation/run")
    return PlainTextResponse(ev.to_markdown(r), media_type="text/markdown; charset=utf-8")


@router.get("/evaluation/gate")
def evaluation_gate():
    """发布门禁入口：只读结构判据结论，不重新跑评估。"""
    r = ev.latest_report()
    if not r:
        raise HTTPException(404, "尚无评估报告，请先调用 POST /api/v1/evaluation/run")
    gate = dict(r.get("gate") or {})
    gate["run_id"] = r.get("run_id")
    gate["started_at"] = r.get("started_at")
    gate["versions"] = r.get("versions")
    return gate


@router.post("/evaluation/baseline")
def evaluation_baseline(body: BaselineIn):
    """显式把最近一次报告固化为回归基线（覆盖需 confirm=true）。"""
    current = ev.load_baseline()
    if current and not body.confirm:
        raise HTTPException(400, "已有基线；覆盖请传 confirm=true")
    return _call(ev.save_baseline)


@router.get("/evaluation/history")
def evaluation_history():
    return {"runs": _call(ev.report_history),
            "baseline": _call(ev.load_baseline)}


class OntologySuiteIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="1", max_length=64)
    split: str = Field(default="gold")
    questions: list[dict] = Field(min_length=1, max_length=500)


class OntologyRunIn(BaseModel):
    spec: dict
    data: dict | None = None
    snapshot: dict = Field(default_factory=dict)
    use_llm: bool = True


def _tenant_actor(request: Request) -> tuple[str, str]:
    principal = getattr(request.state, "principal", {}) or {}
    return (str(principal.get("tenant_id") or "default"),
            str(principal.get("user_id") or "anonymous"))


@router.post("/evaluation/ontology/suites")
def ontology_eval_suite_create(body: OntologySuiteIn, request: Request):
    tenant, actor = _tenant_actor(request)
    return _call(oq_eval.create_suite, tenant, actor, body.model_dump())


@router.get("/evaluation/ontology/suites")
def ontology_eval_suites(request: Request):
    tenant, _ = _tenant_actor(request)
    return {"items": _call(oq_eval.list_suites, tenant)}


@router.post("/evaluation/ontology/suites/{suite_id}/run")
def ontology_eval_run(suite_id: str, body: OntologyRunIn, request: Request):
    tenant, actor = _tenant_actor(request)
    return _call(oq_eval.run_suite, tenant, actor, suite_id, body.spec,
                  body.data, body.snapshot, body.use_llm)


@router.get("/evaluation/ontology/runs")
def ontology_eval_runs(request: Request, limit: int = 30):
    tenant, _ = _tenant_actor(request)
    return {"items": _call(oq_eval.list_runs, tenant, limit)}


@router.get("/evaluation/ontology/runs/{run_id}")
def ontology_eval_run_detail(run_id: str, request: Request):
    tenant, _ = _tenant_actor(request)
    return _call(oq_eval.get_run, tenant, run_id)
