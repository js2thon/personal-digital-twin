"""本体驱动知识图谱 API：导入 → 灌数 → 只读 SPARQL → 语义问答。

端点一览（前缀 /api/v1）：
- GET    /kg/status                 运行时自检（rdflib / 落盘目录 / LLM 可用性 / 会话数）
- GET    /kg/sessions               会话列表（按创建时间倒序）
- POST   /kg/import                 导入本体文件（multipart，可多文件）
- POST   /kg/import/text            粘贴文本导入（JSON）
- POST   /kg/design/draft           AI 设计助手：需求文本 → 本体（Turtle）+ 设计摘要
- POST   /kg/design/doc             AI 设计助手：需求文档 → 抽正文 → 同上
- POST   /kg/design/apply           把确认过的 Turtle 落成会话（或并入现有会话）
- GET    /kg/{sid}                  会话详情：ontology + prefixes + stats + 类/属性/个体/边
- POST   /kg/{sid}/data             追加实例数据（multipart，可多文件）
- POST   /kg/{sid}/data/text        追加实例数据（粘贴，JSON）
- POST   /kg/{sid}/sparql           执行只读 SPARQL（SELECT / ASK / CONSTRUCT / DESCRIBE）
- GET    /kg/{sid}/examples         内置示例集（按本体标记 runnable）
- POST   /kg/{sid}/examples/{eid}   跑单条内置示例
- POST   /kg/{sid}/ask              自然语言问答（LLM 生成 SPARQL + 模板兜底）
- GET    /kg/{sid}/export.ttl       导出当前合并图（Turtle，便于外部核对）
- DELETE /kg/{sid}                  删除会话

约定：
- 数据只在「导入的内容」里，服务端不臆造任何实体或属性；
- SPARQL 一律只读，更新类关键字在 query.run 里被拦截；
- 问答的事实来源是图数据，LLM 只负责写查询，engine 字段如实标注是谁写的。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from rdflib import Graph

from app.modules.ontology.kg import design as kg_design
from app.modules.ontology.kg import model as kg_model
from app.modules.ontology.kg import qa as kg_qa
from app.modules.ontology.kg import query as kg_query
from app.modules.ontology.kg import store as kg_store

router = APIRouter()

_MAX_BYTES = 32 * 1024 * 1024      # 单文件 32MB，本体文件足够；超了让人先裁
_MAX_FILES = 20


# ---------------------------------------------------------------- 入参模型
class TextImportIn(BaseModel):
    name: str | None = None
    content: str
    filename: str | None = None       # 仅用于猜格式，可省


class SparqlIn(BaseModel):
    query: str
    limit: int = 500


class AskIn(BaseModel):
    question: str
    use_llm: bool = True


class DesignIn(BaseModel):
    requirements: str = ""
    sid: str | None = None            # 传了就按「补充设计」读现有本体当上下文
    name: str | None = None
    turtle: str | None = None         # 对话式：上一版草稿，本次在它上面改
    history: list[dict] | None = None  # 对话式：最近几轮 {role, content}


class DesignApplyIn(BaseModel):
    turtle: str
    name: str | None = None
    sid: str | None = None            # 传了就并入现有会话，否则新建


# ---------------------------------------------------------------- 内部工具
def _principal(request: Request) -> dict:
    p = getattr(getattr(request, "state", None), "principal", None)
    return p if isinstance(p, dict) else {}


async def _read_limited(file: UploadFile, limit: int = _MAX_BYTES) -> bytes:
    data = await file.read()
    if len(data) > limit:
        raise HTTPException(413, f"{file.filename or '文件'} 超过 {limit // 1024 // 1024}MB 上限")
    return data


def _source(kind: str, name: str, fmt: str, triples: int) -> dict:
    return {"kind": kind, "name": name, "format": fmt, "triples": triples,
            "at": round(time.time(), 3)}


def _merge(files: list[tuple[str, bytes]], kind: str) -> tuple[Graph, list[dict], list[str]]:
    """把多个文件解析进同一张图。返回 (图, 成功来源, 失败原因)。

    注意：新建的 Graph() 不会继承源文件里的 @prefix 绑定，只加三元组的话
    类/属性的 CURIE 会退化成整条 URI（http://example.org/onto#Employee），
    落盘重读后还会变成 ns1:/onto: 这种名字。这里把每个文件的命名空间一起
    搬过来，界面与 SPARQL 里的前缀才和原文一致。
    """
    graph = Graph()
    sources: list[dict] = []
    failed: list[str] = []
    for fname, data in files:
        if not data:
            failed.append(f"{fname}：文件为空")
            continue
        try:
            one, fmt = kg_model.parse_rdf(fname, data)
        except Exception as exc:  # noqa: BLE001  解析器会抛各种 excepthook
            failed.append(f"{fname}：{str(exc).splitlines()[0][:200]}")
            continue
        n = len(one)
        if n == 0:
            failed.append(f"{fname}：解析成功但没有任何三元组")
            continue
        for tri in one:
            graph.add(tri)
        for prefix, ns in one.namespaces():
            try:
                graph.bind(prefix, ns, override=False)
            except Exception:  # noqa: BLE001  非法前缀名忽略即可，不影响三元组
                pass
        sources.append(_source(kind, fname, fmt, n))
    return graph, sources, failed


def _need(sid: str):
    sess = kg_store.get(sid)
    if sess is None:
        raise HTTPException(404, "会话不存在或已被清理（重新导入即可）")
    return sess


def _default_name(sources: list[dict], fallback: str = "未命名本体") -> str:
    for s in sources:
        stem = str(s.get("name") or "").rsplit("/", 1)[-1]
        for ext in (".ttl", ".owl", ".rdf", ".jsonld", ".json", ".nt", ".n3"):
            if stem.lower().endswith(ext):
                stem = stem[: -len(ext)]
                break
        if stem:
            return stem
    return fallback


# ---------------------------------------------------------------- 自检 / 列表
@router.get("/kg/status")
def status(request: Request):
    import rdflib

    base = kg_store._STORE  # noqa: SLF001  内部路径在这里只用于显示，不改
    try:
        base.mkdir(parents=True, exist_ok=True)
        writable = True
    except Exception:  # noqa: BLE001
        writable = False
    return {
        "ok": True,
        "rdflib": getattr(rdflib, "__version__", "unknown"),
        "formats": ["turtle", "xml", "json-ld", "nt", "n3"],
        "store_dir": str(base),
        "store_writable": writable,
        "sessions": len(kg_store.listing()),
        "llm_configured": bool(getattr(kg_qa, "llm_client", None)),
        "examples": {"employee": sum(1 for e in kg_query.EXAMPLES if e["suite"] == kg_query.EMP),
                     "generic": sum(1 for e in kg_query.EXAMPLES if e["suite"] == kg_query.GEN)},
        "principal": _principal(request).get("user_id", "anonymous"),
    }


@router.get("/kg/sessions")
def sessions():
    items = kg_store.listing()
    items.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------- 导入
@router.post("/kg/import")
async def import_ontology(files: list[UploadFile] = File(...),
                          name: str | None = Form(None)):
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"一次最多 {_MAX_FILES} 个文件")
    payload = [(f.filename or f"upload-{i}", await _read_limited(f)) for i, f in enumerate(files)]
    graph, sources, failed = _merge(payload, "ontology")
    if not sources:
        raise HTTPException(400, {"message": "所有文件都没能解析成本体", "failed": failed})
    sess = kg_store.create(name or _default_name(sources), graph, sources[0])
    if len(sources) > 1:
        for s in sources[1:]:
            sess.sources.append(s)
        kg_store._persist(sess)  # noqa: SLF001  合并进来的其余来源补记一次
    return {"ok": True, "session": sess.public(), "failed": failed,
            "model": sess.model()}


@router.post("/kg/import/text")
def import_ontology_text(payload: TextImportIn):
    fname = payload.filename or "pasted.ttl"
    graph, sources, failed = _merge([(fname, payload.content.encode("utf-8"))], "ontology")
    if not sources:
        raise HTTPException(400, {"message": "粘贴的内容没能解析成本体",
                                  "failed": failed or ["内容为空"]})
    sess = kg_store.create(payload.name or _default_name(sources), graph, sources[0])
    return {"ok": True, "session": sess.public(), "failed": failed, "model": sess.model()}


# ---------------------------------------------------------------- AI 设计助手
def _existing_model(sid: str | None) -> dict | None:
    """补充设计时拿现有会话当上下文；会话不在了就当全新设计，不报错。"""
    if not sid:
        return None
    sess = kg_store.get(sid)
    return sess.model() if sess is not None else None


def _design_payload(out: dict) -> dict:
    """draft() 的返回 → 前端可渲染的摘要（graph 对象不出接口）。"""
    if not out.get("ok"):
        return {"ok": False,
                "error": out.get("error") or "模型没有产出可用的本体",
                "engine": out.get("engine") or "llm",
                "attempts": out.get("attempts") or [],
                "turtle": out.get("turtle") or ""}
    s = kg_design.summarize(out["graph"], out.get("header") or {})
    return {"ok": True, "engine": out.get("engine") or "llm",
            "attempts": out.get("attempts") or [],
            "turtle": out["turtle"],
            "name": s["name"], "summary": s["summary"],
            "assumptions": s["assumptions"], "classes": s["classes"],
            "relations": s["relations"], "data_props": s["data_props"],
            "stats": s["stats"],
            # 对话式：这句是助手在聊天里说的话；delta 是这轮的结构变化
            "reply": out.get("reply") or s["summary"] or "",
            "delta": out.get("delta")}


def _hist(raw: str | None) -> list[dict] | None:
    """表单里带上来的对话上文（JSON 字符串）；坏了就当没有，不影响本轮生成。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, list) else None


@router.post("/kg/design/draft")
def design_draft(payload: DesignIn):
    """需求文本 → 本体（Turtle）+ 设计摘要。只生成不落库，等前端确认后 apply。

    带 `turtle` 时按「在上一版草稿上改」处理（对话式多轮），不传就是全新设计。
    """
    req = (payload.requirements or "").strip()
    if len(req) < 6:
        raise HTTPException(400, "需求描述太短，至少写一句话说明要管理什么业务对象")
    out = kg_design.draft(req, existing_model=_existing_model(payload.sid),
                          base_turtle=payload.turtle, history=payload.history)
    res = _design_payload(out)
    if payload.name and res.get("ok"):
        res["name"] = payload.name
    return res


@router.post("/kg/design/doc")
async def design_from_doc(file: UploadFile = File(...),
                          name: str | None = Form(None),
                          sid: str | None = Form(None),
                          turtle: str | None = Form(None),
                          history: str | None = Form(None)):
    """需求文档 → 抽出正文 → 同一套生成流程。解析引擎与告警如实回给前端。

    `turtle` / `history` 让「对话里传文档」也能接着上一版草稿改。
    """
    from app.modules.document_parser.adapters import parse_bytes

    data = await _read_limited(file, 16 * 1024 * 1024)
    fname = file.filename or "requirements"
    if not data:
        raise HTTPException(400, f"{fname} 是空文件")
    try:
        parsed = parse_bytes(data, fname, fast=True)   # 只要正文，不走版式模型
    except Exception as exc:  # noqa: BLE001  解析器会抛各种底层异常
        raise HTTPException(400, f"{fname} 解析失败：{str(exc).splitlines()[0][:200]}") from exc

    text = (parsed.get("text") or "").strip()
    doc = {"filename": fname, "engine": parsed.get("engine") or "",
           "warning": parsed.get("warning") or "", "chars": len(text)}
    if len(text) < 20:
        return {"ok": False, "doc": doc,
                "error": f"没从 {fname} 里读出足够正文（{len(text)} 字），"
                         f"扫描件请先做 OCR 或直接把需求贴到输入框",
                "attempts": [], "engine": parsed.get("engine") or "parser"}

    out = kg_design.draft(text, existing_model=_existing_model(sid),
                          base_turtle=turtle, history=_hist(history))
    res = _design_payload(out)
    res["doc"] = doc
    res["requirements"] = text[:4000]
    if name and res.get("ok"):
        res["name"] = name
    return res


@router.post("/kg/design/apply")
def design_apply(payload: DesignApplyIn):
    """把确认过的 Turtle 变成设计图：新建会话，或并入现有会话。"""
    turtle = (payload.turtle or "").strip()
    if not turtle:
        raise HTTPException(400, "没有可应用的本体内容")
    graph, sources, failed = _merge([("design.ttl", turtle.encode("utf-8"))], "design")
    if not sources:
        raise HTTPException(400, {"message": "生成的 Turtle 无法解析",
                                  "failed": failed or ["内容为空"]})

    name = (payload.name or "").strip() or _default_name(sources, "AI 设计本体")
    sess = kg_store.get(payload.sid) if payload.sid else None
    if sess is None:
        sess = kg_store.create(name, graph, sources[0])
        merged = False
    else:
        kg_store.add_data(sess, graph, sources[0])
        merged = True
    return {"ok": True, "merged": merged, "failed": failed,
            "session": sess.public(), "model": sess.model()}


# ---------------------------------------------------------------- 详情
@router.get("/kg/{sid}")
def detail(sid: str):
    sess = _need(sid)
    m = sess.model()
    return {"ok": True, "session": sess.public(),
            "ontology": m["ontology"], "prefixes": m["prefixes"], "stats": m["stats"],
            "classes": m["classes"], "object_props": m["object_props"],
            "data_props": m["data_props"], "annotation_props": m["annotation_props"],
            "individuals": m["individuals"], "individuals_total": m["individuals_total"],
            "edges": m["edges"], "truncated": m["truncated"]}


@router.get("/kg/{sid}/export.ttl")
def export_ttl(sid: str):
    from fastapi.responses import PlainTextResponse

    sess = _need(sid)
    with sess.lock:
        text = sess.graph.serialize(format="turtle")
    return PlainTextResponse(text, media_type="text/turtle",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{sess.name or sid}.ttl"'})


# ---------------------------------------------------------------- 灌数据
@router.post("/kg/{sid}/data")
async def add_data(sid: str, files: list[UploadFile] = File(...)):
    sess = _need(sid)
    if len(files) > _MAX_FILES:
        raise HTTPException(400, f"一次最多 {_MAX_FILES} 个文件")
    payload = [(f.filename or f"data-{i}", await _read_limited(f)) for i, f in enumerate(files)]
    graph, sources, failed = _merge(payload, "data")
    if not sources:
        raise HTTPException(400, {"message": "所有文件都没能解析成数据", "failed": failed})
    before = sess.model()["stats"]
    kg_store.add_data(sess, graph, sources[0])
    if len(sources) > 1:
        sess.sources.extend(sources[1:])
        kg_store._persist(sess)  # noqa: SLF001
    m = sess.model()
    return {"ok": True, "failed": failed, "session": sess.public(), "model": m,
            "delta": {k: m["stats"].get(k, 0) - before.get(k, 0)
                      for k in ("triples", "individuals", "edges", "classes")}}


@router.post("/kg/{sid}/data/text")
def add_data_text(sid: str, payload: TextImportIn):
    sess = _need(sid)
    fname = payload.filename or "pasted-data.ttl"
    graph, sources, failed = _merge([(fname, payload.content.encode("utf-8"))], "data")
    if not sources:
        raise HTTPException(400, {"message": "粘贴的内容没能解析成数据",
                                  "failed": failed or ["内容为空"]})
    before = sess.model()["stats"]
    kg_store.add_data(sess, graph, sources[0])
    m = sess.model()
    return {"ok": True, "failed": failed, "session": sess.public(), "model": m,
            "delta": {k: m["stats"].get(k, 0) - before.get(k, 0)
                      for k in ("triples", "individuals", "edges", "classes")}}


# ---------------------------------------------------------------- 查询
def _pfx_of(sess) -> dict:
    """结果展示与 PREFIX 补齐共用同一份前缀表（模型收敛过的那套）。

    直接读 graph.namespaces() 会在「落盘后重读」的会话里露出 ns1:xxx 这种占位名，
    这里统一改用 model() 里已经修好的映射。
    """
    return (sess.model() or {}).get("prefixes") or {}


@router.post("/kg/{sid}/sparql")
def sparql(sid: str, payload: SparqlIn):
    sess = _need(sid)
    limit = max(1, min(int(payload.limit or 500), 2000))
    with sess.lock:
        res = kg_query.run(sess.graph, payload.query, row_limit=limit, prefixes=_pfx_of(sess))
    return res


@router.get("/kg/{sid}/examples")
def examples(sid: str):
    sess = _need(sid)
    return {"items": kg_query.examples_for(sess.model())}


@router.post("/kg/{sid}/examples/{eid}")
def run_example(sid: str, eid: str):
    sess = _need(sid)
    ex = next((e for e in kg_query.examples_for(sess.model()) if e["id"] == eid), None)
    if ex is None:
        raise HTTPException(404, f"没有编号为 {eid} 的示例")
    with sess.lock:
        res = kg_query.run(sess.graph, ex["query"], prefixes=_pfx_of(sess))
    return {"example": ex, "result": res}


# ---------------------------------------------------------------- 问答
@router.post("/kg/{sid}/ask")
def ask(sid: str, payload: AskIn):
    q = (payload.question or "").strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    sess = _need(sid)
    with sess.lock:
        out = kg_qa.ask(sess, q, use_llm=payload.use_llm)
    out["question"] = q
    return out


@router.delete("/kg/{sid}")
def drop(sid: str):
    ok = kg_store.remove(sid)
    if not ok:
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "id": sid}
