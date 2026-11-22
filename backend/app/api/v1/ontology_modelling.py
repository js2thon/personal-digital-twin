"""本体建模工作流 API（画布 5 段建模设计节点 + 收尾区验证门禁）。

只读/纯计算端点为主，不写业务库：
- GET  /ontology/modelling/node-schema      节点 JSON Schema 契约
- GET  /ontology/modelling/contract         精简契约摘要（前端渲染面板）
- GET  /ontology/modelling/sample           示例工作流（节点 + 模拟数据）
- POST /ontology/modelling/validate         对给定草稿跑四类完整验证（可带数据）
- POST /ontology/modelling/run              拓扑推演 + 完整验证 + 发布门禁（按报告判定）
- POST /ontology/modelling/sample/run       一键跑通示例（自证闭环）
- POST /ontology/modelling/assist           设计助手：草稿上下文 + 问题 → 大模型回答
- POST /ontology/modelling/export/jsonld    导出 JSON-LD（OWL/SKOS）本体文件（纯计算）
- POST /ontology/modelling/import/rdf       导入 RDF/OWL/TTL → 画布 spec + 实例载荷（纯计算）
- POST /ontology/modelling/import/seed      把实例载荷灌进 entities/facts/relations（写库）
- POST /ontology/modelling/sparql           在内存图上跑只读 SPARQL
- POST /ontology/modelling/sparql/examples  按给定本体生成一组可直接跑的示例
- POST /ontology/modelling/ask-ontology     基于当前草稿本体问答（pipeline 跑完即可问）
- POST /ontology/modelling/ask-ontology/suggest  按当前草稿现场生成推荐问题
- GET  /ontology/modelling/assist/status    设计助手 LLM 服务可用性探测
- GET  /ontology/modelling/selftest         引擎自检

字段→属性映射内化到属性/类型节点；实例化/验证/发布在收尾区，发布门禁按四类验证报告判定。
数据来自请求体（调用方负责提供真实或模拟数据），服务端不臆造数据；
无数据时数据类检查如实标 no-data，符合项目「缺口照实披露」约定。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.modules.ontology.modelling import (node_schema as NS, sample as S,
                                             validator as V, workflow as WF,
                                             assistant as ASST)

router = APIRouter()


class SpecIn(BaseModel):
    spec: dict
    data: dict | None = None


class RunIn(BaseModel):
    spec: dict
    data: dict | None = None
    dry_run_only: bool = False


class AssistIn(BaseModel):
    question: str
    spec: dict | None = None
    history: list[dict] | None = None   # 预留：多轮对话上下文（当前未喂给模型）


class AskDataIn(BaseModel):
    question: str
    spec: dict | None = None          # 当前本体建模草稿——答案必须由它编译推导，不可省略
    with_evidence: bool = True


class ExportIn(BaseModel):
    spec: dict | None = None
    base_iri: str | None = None       # 默认占位域名，对接外部系统时可换成正式 IRI
    title: str | None = None


class ImportIn(BaseModel):
    """RDF/OWL 导入：text 与 content 二选一（前者便于 curl，后者便于前端读文件）。"""
    content: str | None = None
    text: str | None = None
    format: str | None = None          # ttl / jsonld / rdf / owl / nt；留空自动探测
    source_name: str | None = None
    with_instances: bool = True


class SeedIn(BaseModel):
    instances: dict | None = None      # 直接给实例载荷（导入结果里的 instances）
    content: str | None = None         # 或者给原文，服务端现抽
    text: str | None = None
    format: str | None = None
    dry_run: bool = False
    visibility: str = "organization"   # 必须显式；'owner' 时仅 created_by 本人可读


class SparqlIn(BaseModel):
    query: str
    spec: dict | None = None
    content: str | None = None
    text: str | None = None
    format: str | None = None
    instances: dict | None = None
    max_rows: int = 200


class SparqlExamplesIn(BaseModel):
    spec: dict | None = None
    content: str | None = None
    text: str | None = None
    format: str | None = None


class QualityReportIn(BaseModel):
    """RDF/OWL 本体静态质量报告；content/text 与 spec 二选一。"""
    spec: dict | None = None
    content: str | None = None
    text: str | None = None
    format: str | None = None

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



def _mapping_schema(schema: dict | None) -> dict:
    if schema:
        return schema
    from app.modules.ontology.semantic import schema_def as SD
    return SD.to_schema_dict(SD.default_semantic_schema())


@router.post("/ontology/modelling/mapping/suggest")
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


@router.post("/ontology/modelling/mapping/validate")
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


@router.post("/ontology/modelling/dataset/inspect")
def dataset_inspect(payload: DatasetInspectIn, request: Request):
    """解析 CSV/XLSX，返回字段画像和本体映射候选；只读。"""
    from app.modules.ontology import mapping_pipeline as MP
    dataset = _read_dataset_payload(payload.file_name, payload.content_base64, payload.sheet_name)
    schema = _mapping_schema(payload.ontology_schema)
    report = MP.suggest_mappings(dataset["columns"], schema)
    return {"dataset": {k: v for k, v in dataset.items() if k != "rows"},
            "mapping_suggestions": report, "principal": _principal(request).get("user_id", "anonymous")}


@router.post("/ontology/modelling/mapping/preview-file")
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


def _principal(request: Request) -> dict:
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, dict) else {}


@router.get("/ontology/modelling/node-schema")
def node_schema(request: Request):
    return NS.node_json_schema()


@router.get("/ontology/modelling/contract")
def contract(request: Request):
    return NS.contract_summary()


@router.get("/ontology/modelling/sample")
def get_sample(request: Request):
    return {"spec": S.sample_spec(), "data": S.sample_data()}


@router.post("/ontology/modelling/quality-report")
def quality_report(payload: QualityReportIn, request: Request):
    """对 RDF/OWL/TTL 或当前画布草稿执行本体结构质量检查（纯计算、只读）。"""
    from app.modules.ontology import rdf_quality
    from app.modules.ontology.modelling import sparql as SP

    raw = payload.content if payload.content is not None else (payload.text or "")
    try:
        if raw.strip():
            graph, used_format = SP.graph_from_text(raw, payload.format)
            source = "rdf-text"
        elif payload.spec:
            graph = SP.graph_from_spec(payload.spec)
            used_format = "jsonld"
            source = "canvas-spec"
        else:
            raise HTTPException(400, "需要 content/text（本体原文）或 spec（画布草稿）之一")
        report = rdf_quality.analyze(graph)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(400, f"本体解析失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"本体质量分析失败：{type(exc).__name__}: {exc}") from exc

    return {
        "principal": _principal(request).get("user_id", "anonymous"),
        "source": source,
        "format": used_format,
        "report": report,
    }


@router.post("/ontology/modelling/validate")
def validate(payload: SpecIn, request: Request):
    spec = payload.spec or {}
    if not isinstance(spec.get("nodes"), list) or not isinstance(spec.get("edges"), list):
        raise HTTPException(400, "spec.nodes 与 spec.edges 必须是数组")
    report = V.validate_workflow(spec, payload.data or {})
    return {"principal": _principal(request).get("user_id", "anonymous"), "report": report}


@router.post("/ontology/modelling/run")
def run_workflow(payload: RunIn, request: Request):
    spec = payload.spec or {}
    if not isinstance(spec.get("nodes"), list) or not isinstance(spec.get("edges"), list):
        raise HTTPException(400, "spec.nodes 与 spec.edges 必须是数组")
    if payload.dry_run_only:
        return WF.dry_run(spec)
    return WF.run(spec, payload.data or {})


@router.post("/ontology/modelling/sample/run")
def run_sample(request: Request):
    return S.run_sample()


@router.post("/ontology/modelling/assist")
def assist(payload: AssistIn, request: Request):
    """设计助手对话：草稿摘要作为上下文，调用私有化 LLM。

    服务不可达时 assistant.ask 返回 available=False + 原因，HTTP 仍 200，
    由前端明确提示并回退到本地静态分析——绝不编造模型回答。
    """
    result = ASST.ask(payload.question, payload.spec)
    result["principal"] = _principal(request).get("user_id", "anonymous")
    return result


@router.post("/ontology/modelling/ask-data")
def ask_data(payload: AskDataIn, request: Request):
    """智能问数：把当前本体 spec 编译成 IR，由 IR.semantics 推导出查询，再查真实库。

    答案带 derivation=ontology-derived + provenance（指回本体节点），可核对推导链。
    本体缺语义映射 → ok=false 拒答，绝不查原始库猜数字。
    principal 取自 AC-01 全局中间件注入的 request.state.principal（不绕过）。
    """
    from app.modules.ontology import nlquery
    p = _principal(request)
    result = nlquery.answer(payload.question, spec=payload.spec, principal=p,
                            with_evidence=bool(payload.with_evidence))
    result["principal"] = p.get("user_id", "anonymous")
    return result


@router.get("/ontology/modelling/ask-data/capabilities")
def ask_data_capabilities(request: Request):
    """问数能力自述：本体需要声明哪些字段、问句支持哪些意图（供前端提示与核对）。"""
    from app.modules.ontology import nlquery
    return nlquery.capabilities()


class AskOntologyIn(BaseModel):
    question: str
    spec: dict | None = None          # 当前画布草稿：答案由它现场编译出的图产生
    data: dict | None = None          # 可带实例载荷（种子/真实数据），对齐「导入实例」两种形状
    use_llm: bool = True              # false = 只用关键词模板（离线/排障时用）


@router.post("/ontology/modelling/ask-ontology")
def ask_ontology(payload: AskOntologyIn, request: Request):
    """基于**当前草稿本体**问答：spec → JSON-LD → 内存图 → SPARQL → 答案 + 证据。

    与 ``ask-data`` 的分工（两者不重叠，别混用）：
    - ``ask-data`` 走 nlquery，要求本体先声明「语义映射 → 业务库表」，缺映射就拒答，
      面向「线上业务库」；
    - 本端点不碰业务库，只查草稿编译出的这张图（本体 + 请求体里的实例数据），
      所以 pipeline 一搭完就能问，不需要先落盘成 KgSession。

    ``engine`` 如实标注答案来源：``llm`` = 模型写了能跑通的查询，``template`` =
    模型不可用/两次失败后走关键词模板。**两种情况事实都来自图数据**，SPARQL 一并返回可核对。
    """
    from app.modules.ontology.modelling import ontology_qa as OQ
    out = OQ.answer(payload.spec or {}, payload.question,
                    data=payload.data, use_llm=bool(payload.use_llm))
    out["principal"] = _principal(request).get("user_id", "anonymous")
    return out


class SuggestIn(BaseModel):
    spec: dict | None = None
    data: dict | None = None          # 带上实例载荷，推荐里才会出现「有多少条」这类计数题


@router.post("/ontology/modelling/ask-ontology/suggest")
def ask_ontology_suggest(payload: SuggestIn, request: Request):
    """推荐问题：全部由当前草稿的类/属性/实例数现场推出，不写死话术。

    没有实例的类只推结构类问题（属性/关系）；没有自反对象属性就不推「上级的上级」，
    保证点进去的每个问题在图上都真能查出东西。带 ``data`` 时会多出「有多少条」这类计数题。
    """
    from app.modules.ontology.modelling import ontology_qa as OQ
    return OQ.suggest(payload.spec or {}, data=payload.data)


@router.post("/ontology/modelling/runtime/run")
def runtime_run(payload: SpecIn, request: Request):
    """直接运行已建模本体：编译 → 执行，产出指标/派生/行动/约束违规/不可执行项。

    数据由请求体提供（真实或模拟），服务端不臆造；无数据时指标 value=None 并给原因。
    """
    from app.modules.ontology.runtime.compiler import compile_spec
    from app.modules.ontology.runtime.engine import run as run_ir
    ir = compile_spec(payload.spec or {})
    result = run_ir(ir, payload.data or {})
    return {
        "principal": _principal(request).get("user_id", "anonymous"),
        "hash": ir.get("hash"),
        "health": ir.get("health"),
        "result": result,
    }


@router.post("/ontology/modelling/ontology-view")
def ontology_view(payload: SpecIn, request: Request):
    """基于当前草稿编译出的本体画像 + 体检结论（compiler 唯一口径，零 I/O）。

    供设计助手徽标、MCP 能力自述、前端「能算什么/哪不可执行」共用同一份结果。
    """
    from app.modules.ontology.runtime.compiler import compile_spec
    ir = compile_spec(payload.spec or {})
    return {
        "principal": _principal(request).get("user_id", "anonymous"),
        "hash": ir.get("hash"),
        "stats": ir.get("stats"),
        "health": ir.get("health"),
        "objects": ir.get("objects"),
        "unassigned_attrs": ir.get("unassigned_attrs"),
        "diagnostics": ir.get("diagnostics"),
    }


@router.post("/ontology/modelling/export/jsonld")
def export_jsonld(payload: ExportIn, request: Request):
    """把建模流水线的产物导出为 JSON-LD 本体文档（OWL Class/Property + SKOS 标签）。

    结构唯一来自 compiler.compile_spec 的 IR；IR 不保留的信息（rel:* 关系类型、
    process 节点、对象是否真填名称）回读草稿。不可执行项如实标注，不编造表达式。
    纯计算不写盘，由调用方决定落盘位置（前端「导出 JSON-LD」直接触发下载）。
    """
    from app.modules.ontology.modelling import jsonld as JL
    doc = JL.build_jsonld(payload.spec or {}, base_iri=payload.base_iri, title=payload.title)
    graph = doc.get("@graph") or []
    by_type: dict[str, int] = {}
    for n in graph:
        t = n.get("@type")
        # @type 可能是字符串，也可能是数组（如枚举个体 ["owl:NamedIndividual", 枚举类]）；
        # 逐个计，别把整个数组 str 成一个 key（会漏掉 owl:NamedIndividual 的计数）
        for one in (t if isinstance(t, list) else [t]):
            key = str(one.get("@id") if isinstance(one, dict) else one or "").strip()
            if key:
                by_type[key] = by_type.get(key, 0) + 1
    return {
        "principal": _principal(request).get("user_id", "anonymous"),
        "jsonld": doc,
        "counts": by_type,
        "nodes": len(graph),
    }


@router.post("/ontology/modelling/import/rdf")
def import_rdf(payload: ImportIn, request: Request):
    """导入 RDF/OWL/TTL → 建模画布 5 段 spec + 实例载荷（**纯计算，不写盘不写库**）。

    前端拿到 spec 后**整份替换**当前草稿（用户要求「把现在本体设计页面内容全部删除」），
    再由画布的统一布局函数排版。实例载荷要不要落库由 ``/import/seed`` 决定，
    两步分开是为了让用户先看清「导入解析出什么」再决定写不写。
    """
    from app.modules.ontology.modelling import rdf_import as RI
    raw = payload.content if payload.content is not None else (payload.text or "")
    if not (raw or "").strip():
        raise HTTPException(400, "content 为空：请提供本体文件文本（TTL/RDF-XML/JSON-LD）")
    try:
        out = RI.import_text(raw, payload.format, source_name=payload.source_name or "",
                             with_instances=bool(payload.with_instances))
    except ValueError as exc:
        raise HTTPException(400, f"解析失败：{exc}") from exc
    out["principal"] = _principal(request).get("user_id", "anonymous")
    return out


@router.post("/ontology/modelling/import/seed")
def import_seed(payload: SeedIn, request: Request):
    """把实例载荷灌进 ``ontology.entities`` / ``knowledge.facts`` / ``ontology.relations``。

    权限口径见 ``modelling/seed.py``：显式写 ``created_by`` 与 ``visibility``，
    否则 ``policy.read_clause`` 会把行全过滤掉、问数静默返回 0。
    ``dry_run=true`` 只报预计新增量，不写库。
    """
    from app.modules.ontology.modelling import rdf_import as RI, seed as SD
    inst = payload.instances
    if inst is None:
        raw = payload.content if payload.content is not None else (payload.text or "")
        if not (raw or "").strip():
            raise HTTPException(400, "要么给 instances，要么给 content/text 让服务端现抽")
        try:
            inst = RI.import_text(raw, payload.format)["instances"]
        except ValueError as exc:
            raise HTTPException(400, f"解析失败：{exc}") from exc
    result = SD.seed_instances(inst, principal=_principal(request),
                               visibility=payload.visibility,
                               dry_run=bool(payload.dry_run))
    result["principal"] = _principal(request).get("user_id", "anonymous")
    return result


@router.post("/ontology/modelling/sparql")
def sparql_query(payload: SparqlIn, request: Request):
    """在内存图上跑一条**只读** SPARQL（SELECT/ASK/CONSTRUCT/DESCRIBE）。

    图由 ``content/text``（本体原文）或 ``spec``（画布草稿经 JSON-LD 回环）装载，
    可选 ``instances`` 把数据一起补进去。``SERVICE``/``LOAD``/更新类关键字一律拒绝：
    只在本进程内存执行，不出网、不改库。
    """
    from app.modules.ontology.modelling import sparql as SP
    graph, info = _graph_of(payload, SP)
    out = SP.run(graph, payload.query, max_rows=max(1, min(int(payload.max_rows), 1000)))
    out.update(info)
    out["triples"] = len(graph)
    out["principal"] = _principal(request).get("user_id", "anonymous")
    return out


@router.post("/ontology/modelling/sparql/examples")
def sparql_examples(payload: SparqlExamplesIn, request: Request):
    """按给定本体现场生成一组可直接跑的示例查询（不是写死的模板）。"""
    from app.modules.ontology.modelling import sparql as SP
    try:
        graph, info = _graph_of(payload, SP)
    except HTTPException:
        # 没有本体也给出骨架版示例，前端不至于空白
        return {"examples": SP.examples(None), "profile": None,
                "principal": _principal(request).get("user_id", "anonymous")}
    return {"examples": SP.examples(graph), "profile": SP.profile(graph),
            **info, "principal": _principal(request).get("user_id", "anonymous")}


def _graph_of(payload, SP) -> tuple:
    """按 content/text → spec 的优先级装图，返回 (graph, 说明信息)。"""
    raw = getattr(payload, "content", None)
    if raw is None:
        raw = getattr(payload, "text", None)
    fmt = getattr(payload, "format", None)
    if (raw or "").strip():
        try:
            graph, used = SP.graph_from_text(raw, fmt)
        except ValueError as exc:
            raise HTTPException(400, f"解析失败：{exc}") from exc
        info = {"source": "rdf-text", "format": used}
    elif getattr(payload, "spec", None):
        try:
            graph = SP.graph_from_spec(payload.spec)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"草稿转图失败：{type(exc).__name__}: {exc}") from exc
        info = {"source": "canvas-spec", "format": "json-ld"}
    else:
        raise HTTPException(400, "需要 content/text（本体原文）或 spec（画布草稿）之一")
    inst = getattr(payload, "instances", None)
    if inst:
        info["added_triples"] = SP.add_instances(graph, inst)
    return graph, info


@router.get("/ontology/modelling/assist/status")
def assist_status(request: Request):
    """探测 LLM 服务可用性（短超时，不阻塞）；如实返回，不臆测。"""
    return ASST.service_status(timeout=5.0)


@router.get("/ontology/modelling/selftest")
def selftest(request: Request):
    from app.modules.ontology.runtime import compiler as COMP, expr as EX, metrics as MT, engine as ENG
    from app.modules.ontology import nlquery as NLQ
    from app.modules.ontology.modelling import jsonld as JL, rdf_import as RI, sparql as SP
    from app.modules.ontology.modelling import ontology_qa as OQ
    from app.modules.ontology import rdf_quality
    return {
        "rdf_quality": rdf_quality.selftest(),
        "validator": V.self_check(),
        "workflow": WF.self_check(),
        "assistant": ASST.self_check(),
        "nlquery": NLQ.self_check(),
        "jsonld": JL.self_check(),
        "rdf_import": RI.self_check(),
        "sparql": SP.self_check(),
        "ontology_qa": OQ.self_check(),
        "runtime": {
            "expr": EX.self_check(),
            "metrics": MT.self_check(),
            "compiler": COMP.self_check(),
            "engine": ENG.self_check(),
        },
        "node_types": len(NS.WORKFLOW_ORDER),
        "sample": {"complete": S.run_sample()["report"]["complete"]},
    }
