"""FastAPI 主入口：/api/v1 统一前缀，静态托管 Web 控制台。"""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html

from app.api.v1 import (  # noqa: F401
    cognition,
    cq_v2,
    documents,
    evaluation,
    kg,
    knowledge,
    lineage,
    messages,
    ontology,
    ontology_modelling,
    ontology_semantic,
    pipeline,
    platform,
    projection,
    retrieval,
    security_audit as audit_api,
    semantic,
    sources,
    structured,
    twin,
    workflow,
)
from app.auth import actor_from_request
from app.config import settings

# SHACL strict 模式抛出的违规异常：注册在这里而不是领域模块里，
# 避免 app.modules.ontology 反向依赖 Web 框架。
try:
    from app.modules.ontology.shacl import ShaclViolation as _ShaclViolation
except Exception:  # noqa: BLE001  校验器不可用时不影响启动
    class _ShaclViolation(ValueError):  # type: ignore[no-redef]
        report: dict = {}

# M7：关掉自动暴露的文档路由，改为下方纳入身份闸门的显式路由，
# 避免 /docs 与 /openapi.json 在闸门外泄漏全部接口结构。
app = FastAPI(title="Personal Digital Twin Platform", version="0.1.0",
              docs_url=None, redoc_url=None, openapi_url=None)

_SEMANTIC_EXPOSE = [
    "X-Semantic-Tenant-Id", "X-Semantic-User-Id", "X-Semantic-Request-Id",
    "X-Semantic-Trace-Id", "X-Semantic-Permission-Context",
    "X-Semantic-Schema-Version", "X-Semantic-Context-Source",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type", "X-Twin-API-Key",
        # SIL AC-01：六项语义上下文 + 严格模式开关，供跨域联调
        "X-Tenant-Id", "X-User-Id", "X-Request-Id", "X-Trace-Id",
        "X-Permission-Context", "X-Schema-Version", "X-Semantic-Strict",
    ],
    # 六项上下文随响应回带，前端/联调需要能读到这些响应头
    expose_headers=_SEMANTIC_EXPOSE,
)


def _context_headers(response, ctx) -> None:
    """AC-01：把服务端注入的六项上下文随响应回带（纯 ASCII，避免头编码问题）。"""
    import json as _json

    response.headers["X-Semantic-Tenant-Id"] = str(ctx.tenant_id)[:80]
    response.headers["X-Semantic-User-Id"] = str(ctx.user_id)[:120]
    response.headers["X-Semantic-Request-Id"] = str(ctx.request_id)[:120]
    response.headers["X-Semantic-Trace-Id"] = str(ctx.trace_id)[:120]
    response.headers["X-Semantic-Permission-Context"] = _json.dumps(
        ctx.permission_context, ensure_ascii=True, separators=(",", ":"), default=str)[:500]
    response.headers["X-Semantic-Schema-Version"] = str(ctx.schema_version)[:80]
    response.headers["X-Semantic-Context-Source"] = (
        "derived" if ctx.derived else "explicit")


@app.middleware("http")
async def semantic_context_gate(request: Request, call_next):
    """AC-01 全局强制上下文闸门（P2 收口，替代原 identity_gate）。

    对所有 /api/v1 请求统一完成：身份判定 → 六项语义上下文注入 → 归一化
    权限主体 → 绑定安全审计上下文；响应回带六项上下文头。严格模式由端点级
    提升为全局生效。端点层 from_request() 复用中间件结果，不再各自解析头。
    """
    path = request.url.path
    api = request.method != "OPTIONS" and path.startswith("/api/v1/")
    token = None
    if api:
        try:
            request.state.actor = actor_from_request(request)
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
        try:
            from app import policy as pol
            from app import security_audit
            from app.modules.semantic_runtime.context import from_request

            ctx = from_request(request)
            principal = pol.principal_from(ctx)
            request.state.principal = principal
            token = security_audit.bind(
                tenant_id=principal.get("tenant_id"),
                user_id=principal.get("user_id"),
                actor=principal.get("actor"),
                request_id=ctx.request_id, trace_id=ctx.trace_id,
                method=request.method, path=path,
            )
        except HTTPException as e:
            # 严格模式缺上下文：先落审计再 400，不能静默拒绝
            try:
                from app import security_audit

                security_audit.record(
                    "context_strict_reject", rule="missing_context", severity="block",
                    detail={"detail": e.detail},
                    ctx={"method": request.method, "path": path, "status_code": e.status_code},
                )
                security_audit.flush()
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
        except Exception as exc:  # noqa: BLE001  fail-closed：上下文构造异常不裸放行
            try:
                from app import security_audit

                security_audit.record(
                    "context_strict_reject", rule="context_error", severity="block",
                    detail={"error": f"{type(exc).__name__}: {exc}"[:200]},
                    ctx={"method": request.method, "path": path, "status_code": 400},
                )
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse(status_code=400, content={
                "detail": "语义上下文构造失败，fail-closed",
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    try:
        response = await call_next(request)
    finally:
        if token is not None:
            try:
                from app import security_audit

                security_audit.unbind(token)
            except Exception:  # noqa: BLE001
                pass
    ctx = getattr(request.state, "semantic_context", None)
    if api and ctx is not None:
        _context_headers(response, ctx)
    # 页面（HTML）一律 no-store：避免浏览器沿用旧版控制台，导致新旧布局混用
    # （控制台是单文件前端，no-store 保证改版后刷新必然拿到新版本，不会出现「看着像没更新」）
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["cache-control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers.setdefault("pragma", "no-cache")
        response.headers.setdefault("expires", "0")
    return response


@app.on_event("startup")
def bootstrap_schema():
    """启动时幂等补齐运行时表与列，杜绝「DDL / 本地库 / 运行时建表」三处漂移。"""
    # V1.3 Sprint 1 验收铁律第 1 条：生产环境禁止 MemoryStore。
    # 故意不包 try/except —— 该检查失败必须让进程 fail-fast，而不是
    # 悄悄以进程内存储启动。非生产环境直接返回 checked=false，行为不变。
    from app.modules.ontology.cq_v2 import store as cq_store

    cq_store.assert_production_store()
    try:
        from app.modules.messaging import service as messaging

        messaging.ensure_schema()
    except Exception:  # noqa: BLE001  数据库暂不可用时服务仍可启动
        pass
    try:
        from app.modules.retrieval import service as retrieval
        from app.modules.retrieval import vector_store

        retrieval.ensure_vector_column()
        # 向量本体 P0：6 个本体/向量元数据列 + 2 索引 + ontology.vector_index 表
        vector_store.ensure_columns()
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.modules.ontology import service as ontology

        ontology.ensure_schema()
    except Exception:  # noqa: BLE001  本体可信度列缺失时不影响启动
        pass
    try:
        # SIL-DC-05 / DC-08：事实字段契约列（只加列，回填需显式调用 backfill）
        from app.modules.knowledge import contract as fact_contract

        fact_contract.ensure_schema()
    except Exception:  # noqa: BLE001  契约列缺失时不影响启动
        pass
    try:
        from app.db import session as db

        # L15：低置信度 Claim 的复核优先级标记（幂等补列，不改事实表）
        db.execute(
            "ALTER TABLE knowledge.claims ADD COLUMN IF NOT EXISTS "
            "review_priority VARCHAR(20) NOT NULL DEFAULT 'normal'"
        )
        # 存量候选按同一阈值回填（幂等，只动未标记的 candidate）
        db.execute(
            "UPDATE knowledge.claims SET review_priority = 'low_confidence' "
            "WHERE status = 'candidate' AND review_priority = 'normal' "
            "AND confidence < CAST(:t AS numeric)",
            {"t": float(settings.claim_review_threshold)},
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.modules.twin import pipeline

        pipeline.ensure_schema()
    except Exception:  # noqa: BLE001  链路详情表缺失时不影响启动
        pass
    try:
        from app.modules.distillation import review as run_review

        run_review.ensure_schema()
    except Exception:  # noqa: BLE001  复核留痕表缺失时不影响启动
        pass
    try:
        from app.modules.structured import contracts as structured_contracts

        structured_contracts.ensure_schema()
    except Exception:  # noqa: BLE001  结构化接入表缺失时不影响启动
        pass
    try:
        _ensure_indexes()
    except Exception:  # noqa: BLE001  索引失败不影响启动
        pass
    try:
        # SIL-EC-01～EC-05：Outbox 账本结构升级 + 投影分发线程。
        # 必须早于 fact_events.start()：后者建的触发器会往账本里写事件。
        from app.modules.ontology import outbox

        outbox.start()
    except Exception:  # noqa: BLE001  账本不可用时不影响 API，实时通道仍在
        pass
    try:
        # 事实变更 → 本体投影边的实时同步：建触发器 + 常驻 LISTEN 线程
        from app.modules.ontology import fact_events

        fact_events.start()
    except Exception:  # noqa: BLE001  监听起不来不影响 API 可用，API 即时钩子仍在
        pass
    try:
        # P1-5：CQ V2 状态落库 + 重启自动水合，消除「每次重启手工 bootstrap」
        from app.modules.ontology import cq_v2

        cq_v2.get_engine().ensure_ready()
    except Exception:  # noqa: BLE001  持久化不可用时仍可按需手工 bootstrap
        pass
    try:
        # P1-4：核心表落 tenant_id（facts 已由契约列补齐，这里补其余三张）
        from app.modules import policy as tenant_policy

        tenant_policy.ensure_tenant_columns()
    except Exception:  # noqa: BLE001  补列失败不影响启动，读路径按 fail-closed 处理
        pass
    try:
        # P2 收口：安全审计持久化（建表 → 从库水合 → 起批量刷盘线程）
        from app import security_audit

        security_audit.start()
    except Exception:  # noqa: BLE001  审计表不可用时 API 仍可用，只留内存读模型
        pass
    try:
        # Ontology 语义层增强：幂等建表 + 播种语义 Schema 快照 + 核心回归集。
        # 全新增量，失败不影响既有 API（各读端点亦有惰性 _ensure 兜底）。
        from app.modules.ontology import semantic

        semantic.ensure_ready()
    except Exception:  # noqa: BLE001  语义层表不可用时既有链路零影响
        pass


def _ensure_indexes() -> None:
    """高频过滤列补索引（PG 外键不自动建索引）+ 可选 pg_trgm 加速 ILIKE 模糊检索。

    全部幂等：CREATE INDEX IF NOT EXISTS；pg_trgm 扩展不可用时跳过（词法检索仍可用）。
    每组独立容错：一项失败不影响其余索引创建。
    """
    from app.db import session as db

    basic = [
        ("idx_runs_document_id", "ON ingest.pipeline_runs (document_id)"),
        ("idx_documents_external_id", "ON source.documents (external_id)"),
        ("idx_chunks_document_id", "ON knowledge.semantic_chunks (document_id)"),
        ("idx_claims_status", "ON knowledge.claims (status)"),
        ("idx_facts_subject_predicate", "ON knowledge.facts (subject_id, predicate, status)"),
        ("idx_evidence_spans_doc_hash",
         "ON knowledge.evidence_spans (document_id, md5(content))"),
    ]
    for name, ddl in basic:
        try:
            db.execute(f"CREATE INDEX IF NOT EXISTS {name} {ddl}")
        except Exception:  # noqa: BLE001
            pass
    try:  # pg_trgm 便携版 PG 可能未内置，失败则词法检索继续走全表扫描
        db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        trgm = [
            ("idx_entities_name_trgm",
             "ON ontology.entities USING gin (canonical_name gin_trgm_ops)"),
            ("idx_chunks_content_trgm",
             "ON knowledge.semantic_chunks USING gin (content gin_trgm_ops)"),
            ("idx_memory_content_trgm",
             "ON memory.memory_items USING gin ((content::text) gin_trgm_ops)"),
        ]
        for name, ddl in trgm:
            try:
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} {ddl}")
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


API = "/api/v1"
app.include_router(sources.router, prefix=API, tags=["ingestion"])
app.include_router(documents.router, prefix=API, tags=["ingestion"])
app.include_router(knowledge.router, prefix=API, tags=["knowledge"])
app.include_router(cognition.router, prefix=API, tags=["cognition"])
app.include_router(ontology.router, prefix=API, tags=["ontology"])
app.include_router(twin.router, prefix=API, tags=["twin"])
app.include_router(workflow.router, prefix=API, tags=["workflow"])
app.include_router(lineage.router, prefix=API, tags=["lineage"])
app.include_router(messages.router, prefix=API, tags=["messages"])
app.include_router(pipeline.router, prefix=API, tags=["pipeline"])
app.include_router(structured.router, prefix=API, tags=["structured"])
app.include_router(cq_v2.router, prefix=API, tags=["cq-v2"])
# 统一语义运行时（SIL B2）：蓝图 9.3 的 10 个契约端点 + 自检 + /ontology/versions 原路径
app.include_router(semantic.router, prefix=API, tags=["semantic"])
app.include_router(evaluation.router, prefix=API, tags=["evaluation"])
# SIL-EC-01～EC-05：Outbox 账本 + 投影对账/重建（6 条端点）
app.include_router(projection.router, prefix=API, tags=["projection"])
# P2 收口：安全审计持久化查询 / 对账 / 强制刷盘（5 条端点，owner 闸门）
app.include_router(audit_api.router, prefix=API, tags=["security-audit"])
# V1.3 换栈 Phase 0：只读 IdentityContext + CQ 存储健康检查
app.include_router(platform.router, prefix=API, tags=["platform"])
# Ontology 语义层增强：Schema/约束/推理/规则/版本冻结/CQ execute/回归集/评估
app.include_router(ontology_semantic.router, prefix=API, tags=["ontology-semantic"])
# 向量本体 P0：向量召回自检（强制词法 0 命中，验证向量独立召回）
app.include_router(retrieval.router, prefix=API, tags=["retrieval"])
# 本体建模工作流：画布 7 类建模节点 + 收尾区四类完整验证门禁（纯计算，不写业务库）
app.include_router(ontology_modelling.router, prefix=API, tags=["ontology-modelling"])
# 本体驱动知识图谱：导入本体/数据 → 自动生成设计模型 → 只读 SPARQL → 语义问答
app.include_router(kg.router, prefix=API, tags=["kg"])


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.exception_handler(_ShaclViolation)
def _shacl_violation_handler(request: Request, exc: _ShaclViolation):
    """SHACL strict 模式拦截 → 400（携带逐条违规），不是 500。"""
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "error": "shacl_violation",
                 "violations": (exc.report or {}).get("violations", [])})


@app.get("/openapi.json", include_in_schema=False)
def protected_openapi(request: Request):
    """接口描述同样属于身份边界内：非本人一律 401。"""
    actor_from_request(request)
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def protected_docs(request: Request):
    """Swagger UI 纳入身份闸门，与 OpenAPI JSON 使用同一判定。"""
    actor_from_request(request)
    return get_swagger_ui_html(openapi_url="/openapi.json",
                               title="Personal Digital Twin API")


# Web 控制台（六个核心工作区 + 消息中心）
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
if WEB_DIR.exists():
    # 控制台入口不缓存：index.html 是单文件前端，改版后浏览器若吃旧缓存会「看着像没更新」。
    # 这两个路由必须注册在 StaticFiles mount 之前，才能优先命中并带上 no-store 头。
    _CONSOLE_NO_STORE = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/console/", include_in_schema=False)
    @app.get("/console/index.html", include_in_schema=False)
    def console_index():
        return FileResponse(WEB_DIR / "index.html", headers=_CONSOLE_NO_STORE)

    app.mount("/console", StaticFiles(directory=str(WEB_DIR), html=True), name="console")

    @app.get("/")
    def index():
        # 根路径即控制台：日常访问 http://127.0.0.1:8101/ 即可，不必再带 /console/
        return FileResponse(WEB_DIR / "index.html", headers=_CONSOLE_NO_STORE)
