"""统一语义 API（SIL-Spec v1.5 / 蓝图 9.3）。

薄路由：只做请求校验、上下文注入、错误映射与幂等，业务逻辑全在
app.modules.semantic_runtime.service。

端点分两组：
  /api/v1/semantic/*            7 个统一语义入口（understand/search/query/reason/
                                entity/resolve/graph/traverse/evidence）
  /api/v1/semantic/ontology/*   3 个带契约的本体端点（schema/proposals/versions）
  另补 GET /api/v1/ontology/versions（蓝图 9.3 要求的原路径，此前项目缺失）

既有 /api/v1/ontology/schema 与 /ontology/proposals 由 legacy router 提供，
保持原样不重复注册，避免路径遮蔽与前端行为变化。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.modules.semantic_runtime import service as sem
from app.modules.semantic_runtime.context import from_request

router = APIRouter()

# 幂等：同一 X-Request-Id 在 24h 内重放返回首次结果（AC-06）。
# 单 worker 进程内缓存；不上限会撑爆内存，故设 500 条 + 惰性清理。
_IDEMPOTENT: dict[str, tuple[float, Any]] = {}
_IDEM_LOCK = threading.Lock()
_IDEM_TTL = 86400.0
_IDEM_MAX = 500


def _call(fn, *args, **kwargs):
    """统一错误映射，照 cq_v2.py 的范式。"""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except Exception as exc:  # noqa: BLE001  不让底层异常裸奔成非结构化 500
        raise HTTPException(503, {
            "code": "SEMANTIC_RUNTIME_ERROR",
            "message": f"{type(exc).__name__}: {str(exc)[:200]}",
            "hint": "检查 /api/v1/semantic/meta 的通道状态与降级原因",
        }) from None


def _cached(request: Request, produce):
    rid = (request.headers.get("x-request-id") or "").strip()
    if request.method != "POST" or not rid:
        return produce()
    key = f"{request.url.path}|{rid}"
    now = time.time()
    with _IDEM_LOCK:
        hit = _IDEMPOTENT.get(key)
        if hit and now - hit[0] < _IDEM_TTL:
            result = dict(hit[1])
            result["idempotent_replay"] = True
            return result
        if len(_IDEMPOTENT) >= _IDEM_MAX:
            for k, (ts, _) in list(_IDEMPOTENT.items()):
                if now - ts >= _IDEM_TTL:
                    _IDEMPOTENT.pop(k, None)
    result = produce()
    with _IDEM_LOCK:
        _IDEMPOTENT[key] = (now, result)
    return result


def _json(ctx, request: Request, fn, payload: dict | None = None):
    def produce():
        result = _call(fn, ctx, payload) if payload is not None else _call(fn, ctx)
        return sem.public(result)
    return _cached(request, produce)


# ---------------------------------------------------------------- 请求模型

class UnderstandIn(BaseModel):
    text: str | None = None
    query: str | None = None


class SearchIn(BaseModel):
    query: str
    tenant_id: str | None = None
    filters: dict = Field(default_factory=dict)
    retrieval: dict = Field(default_factory=dict)
    top_k: int = 20
    scope: list[str] | None = None


class QueryIn(BaseModel):
    predicate: str | None = None
    status: str | None = None
    subject_id: str | None = None
    limit: int = 50


class ReasonIn(BaseModel):
    risk_level: str = "medium"
    action: str | None = None
    facts: list[dict] = Field(default_factory=list)
    allow_no_evidence: bool = False


class EntityResolveIn(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    limit: int = 10


class GraphTraverseIn(BaseModel):
    mode: str = "subgraph"
    center: str | None = None
    depth: int = 2
    types: Any = None
    limit: int = 150
    source: str | None = None
    target: str | None = None
    max_depth: int = 4


class EvidenceIn(BaseModel):
    evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    fact_ids: list[str] = Field(default_factory=list)
    limit: int = 50


class ProposalsIn(BaseModel):
    proposal: Any
    rationale: str = ""
    commit: bool = False


# ---------------------------------------------------------------- 7 个统一语义入口

@router.post("/semantic/understand")
def understand(body: UnderstandIn, request: Request):
    """RP-1/RP-2：自然语言理解、意图识别、本体与实体解析。"""
    return _json(from_request(request), request, sem.understand,
                 body.model_dump(exclude_none=True))


@router.post("/semantic/search")
def search(body: SearchIn, request: Request):
    """RP-3~RP-5：四通道混合检索 + RRF 融合 + 证据绑定 + 上下文装配。"""
    return _json(from_request(request), request, sem.search,
                 body.model_dump(exclude_none=True))


@router.post("/semantic/query")
def structured_query(body: QueryIn, request: Request):
    """结构化业务查询：谓词/状态白名单，全部用户输入走绑定参数。"""
    return _json(from_request(request), request, sem.query, body.model_dump())


@router.post("/semantic/reason")
def reason(body: ReasonIn, request: Request):
    """AC-05 决策输出：事实/规则/模型置信度分别记录。"""
    return _json(from_request(request), request, sem.reason, body.model_dump())


@router.post("/semantic/entity/resolve")
def entity_resolve(body: EntityResolveIn, request: Request):
    """实体对齐解析，三级置信度策略。"""
    return _json(from_request(request), request, sem.entity_resolve, body.model_dump())


@router.post("/semantic/graph/traverse")
def graph_traverse(body: GraphTraverseIn, request: Request):
    """图谱遍历：subgraph k 跳 / 最短路径，逐跳权限过滤。"""
    return _json(from_request(request), request, sem.graph_traverse, body.model_dump())


@router.post("/semantic/evidence")
def evidence(body: EvidenceIn, request: Request):
    """证据查询：九项元数据 + 权限继承。"""
    return _json(from_request(request), request, sem.evidence, body.model_dump())


# ---------------------------------------------------------------- 3 个带契约的本体端点

@router.get("/semantic/ontology/schema")
def semantic_ontology_schema(request: Request, version: str | None = None):
    ctx = from_request(request)
    return _cached(request, lambda: sem.public(_call(sem.ontology_schema, ctx, version)))


@router.post("/semantic/ontology/proposals")
def semantic_ontology_proposals(body: ProposalsIn, request: Request):
    """默认 dry run（只做校验与影响分析）；commit=true 才写候选表。

    commit=true 属本体修改，要求 owner 级主体（SIL-A07）；dry run 放开。
    """
    if body.commit:
        from app.api.v1.cq_v2 import require_owner

        require_owner(request)
    return _json(from_request(request), request, sem.ontology_proposals, body.model_dump())


@router.get("/semantic/ontology/versions")
def semantic_ontology_versions(request: Request):
    ctx = from_request(request)
    return _cached(request, lambda: sem.public(_call(sem.ontology_versions, ctx)))


@router.get("/ontology/versions")
def ontology_versions(request: Request):
    """蓝图 9.3 原路径：此前项目缺失，此处补齐；语义版见 /semantic/ontology/versions。"""
    ctx = from_request(request)
    return _cached(request, lambda: sem.public(_call(sem.ontology_versions, ctx)))


# ---------------------------------------------------------------- 自检

@router.get("/semantic/meta")
def meta(request: Request):
    """通道能力、契约端点、上下文策略与向量状态自检（不要求 startup 完成）。"""
    ctx = from_request(request)
    result = dict(_call(sem.meta))
    result["context"] = ctx.as_dict()
    result["query_id"] = ctx.request_id
    return sem.public(result)
