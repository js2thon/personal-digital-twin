"""AI 本体设计：一段业务需求（或一份需求文档）→ 一份能解析的 OWL 本体。

为什么让模型直接写 Turtle 而不是先写 JSON 再拼：
JSON 里塞多行 Turtle 要转义，小模型十次有三次把引号/换行弄坏；Turtle 本身
就是行式的，注释还能当元数据用（`# name:` / `# summary:` / `# assume:`），
解析失败时把 rdflib 的报错原文回喂一次，比任何 schema 约束都管用。

设计口径：
- 生成结果先过 rdflib：解析不出三元组、或者一个 owl:Class 都没有，一律判为失败；
- 失败会带着解析器报错重试一次（只重试一次，避免接口卡住）；
- 模型不可用 / 两次都失败 → 如实返回错误，不拿模板编造一份假本体；
- 校验通过的图直接交给 store 建会话（或并入现有会话），后续走既有导入链路。
"""
from __future__ import annotations

import re

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef

from app.modules.llm.client import llm_client
from app.modules.ontology.kg import model as kg_model

_SYSTEM = """你是本体工程师，精通 OWL 2 与 Turtle。
根据用户的业务需求，设计一份可直接被 rdflib 解析的本体。

硬性要求：
1. 只输出 Turtle，不要输出任何解释性段落、不要输出 JSON。
2. 用注释写元数据（必须是的行首注释，顺序固定）：
   # name: <本体名，中文，不超过 16 字>
   # summary: <一句话说明设计思路>
   # assume: <你替用户做的一个假设>   （0~5 条，可重复）
3. 命名空间固定：@prefix ex: <http://example.org/onto#> . 并声明 rdf / rdfs / owl / xsd。
4. 所有类和属性都放在 ex: 下：类用 PascalCase，属性用 camelCase。
5. 每个类都写成：
   ex:Foo a owl:Class ; rdfs:label "中文名"@zh ; rdfs:comment "一句话说明"@zh .
6. 数据属性：a owl:DatatypeProperty ; rdfs:domain ex:Foo ; rdfs:range xsd:string ;
   rdfs:label "中文名"@zh . range 只能是 xsd:string / xsd:integer / xsd:decimal /
   xsd:boolean / xsd:date / xsd:dateTime 之一。
7. 对象属性：a owl:ObjectProperty ; rdfs:domain ex:A ; rdfs:range ex:B ; rdfs:label "中文名"@zh .
8. 只在需求里确实存在上下位关系时才写 rdfs:subClassOf，不要为了好看造层级。
9. 规模按需求复杂度定：通常 4~15 个类，每个类 3~12 个属性；只建需求能推出的概念，
   不要编造需求里没有的业务对象。
10. 不要写实例数据（ABox），不要写 owl:imports，不要写 owl:Restriction。

输出形如：
# name: 员工管理本体
# summary: 员工归属部门与职位，部门有上下级
# assume: 一个员工只属于一个部门
@prefix ex: <http://example.org/onto#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

ex:Employee a owl:Class ; rdfs:label "员工"@zh ; rdfs:comment "公司雇员"@zh .
...
"""

_HDR = re.compile(r"^\s*#\s*(name|summary|assume)\s*[:：]\s*(.+?)\s*$", re.I)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*$")
_MAX_REQ_CHARS = 12000
_MAX_TURTLE_CHARS = 16000      # 多轮改写时把当前草稿塞回提示词的上限
_MAX_HIST_TURNS = 8            # 只回带最近 8 轮，够理解「再给它加个字段」指谁
_MAX_HIST_CHARS = 700          # 单条消息截断（需求文档类的长消息不用整段回喂）


def _local(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _strip_fence(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if not _FENCE.match(ln)]
    return "\n".join(lines).strip()


def _header(text: str) -> tuple[dict, str]:
    """取出 `# name:` / `# summary:` / `# assume:` 元数据，并裁掉 Turtle 正文前的杂字。"""
    meta = {"name": "", "summary": "", "assumptions": []}
    body_lines: list[str] = []
    for ln in text.splitlines():
        m = _HDR.match(ln)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "assume":
                if len(meta["assumptions"]) < 6:
                    meta["assumptions"].append(val)
            elif not meta[key]:
                meta[key] = val
            continue
        body_lines.append(ln)
    body = "\n".join(body_lines).strip()
    # 模型偶尔会先写一段「下面是本体：」，从第一个 @prefix / PREFIX 起截断
    idx = min([i for i in (body.find("@prefix"), body.find("@base"), body.find("PREFIX"))
               if i >= 0] or [-1])
    if idx > 0:
        body = body[idx:]
    return meta, body


def _counts(graph: Graph) -> dict:
    classes = {s for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    return {"classes": len(classes), "triples": len(graph)}


def _parse(turtle: str) -> tuple[Graph | None, str]:
    """只认「能解析 + 至少一个类」的结果，其它一律返回报错原文。"""
    if not turtle.strip():
        return None, "模型没有输出任何 Turtle 内容"
    try:
        graph, _fmt = kg_model.parse_rdf("design.ttl", turtle.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001  rdflib 解析器异常类型很杂
        return None, str(exc).splitlines()[0][:300]
    c = _counts(graph)
    if c["triples"] == 0:
        return None, "解析成功但没有任何三元组"
    if c["classes"] == 0:
        return None, "图里没有任何 owl:Class（至少要有 1 个类）"
    return graph, ""


_ONTO_URI = "http://example.org/onto"


def _stamp(graph: Graph, header: dict) -> Graph:
    """补一条 owl:Ontology 声明。

    模型只写了类与属性，页面标题 / 副标题取自 owl:Ontology 的 label / comment；
    缺了这条，设计图顶部就只能显示会话名、副标题空着。
    """
    name = (header.get("name") or "").strip()
    summary = (header.get("summary") or "").strip()
    if not name and not summary:
        return graph
    onto = URIRef(_ONTO_URI)
    graph.add((onto, RDF.type, OWL.Ontology))
    if name:
        graph.add((onto, RDFS.label, Literal(name, lang="zh")))
    if summary:
        graph.add((onto, RDFS.comment, Literal(summary, lang="zh")))
    return graph


def _brief_existing(model: dict | None) -> str:
    if not model:
        return ""
    brief = kg_model.schema_brief(model, max_classes=30, max_props=40)
    return ("\n当前已有本体（本次是「补充设计」，不要重复建已存在的类与属性，"
            "新概念要能和它们连起来）：\n" + brief + "\n")


def _base_text(base_turtle: str | None) -> str:
    """对话式修改：把上一版草稿整段回喂，要求输出「完整」的修改后本体。"""
    t = (base_turtle or "").strip()
    if not t:
        return ""
    return ("\n当前本体草稿（用户已经看过这一版，本次是在它上面改）：\n"
            "- 只动用户这次明确说到的地方，其余类、属性、关系、中文标签原样保留；\n"
            "- 输出**完整**的修改后本体（不是差异片段）；\n"
            "- `# summary:` 也要跟着更新成这一版的整体说明。\n"
            "```turtle\n" + t[:_MAX_TURTLE_CHARS] + "\n```\n")


def _history_text(history: list[dict] | None) -> str:
    if not history:
        return ""
    rows: list[str] = []
    for item in list(history)[-_MAX_HIST_TURNS:]:
        if not isinstance(item, dict):
            continue
        txt = str(item.get("content") or "").strip()
        if not txt:
            continue
        role = "用户" if str(item.get("role") or "").lower() in ("user", "me") else "助手"
        if len(txt) > _MAX_HIST_CHARS:
            txt = txt[:_MAX_HIST_CHARS] + "…"
        rows.append(f"{role}：{txt}")
    if not rows:
        return ""
    return "\n前面几轮对话（从旧到新，用来判断这次说的是哪个对象）：\n" + "\n".join(rows) + "\n"


def _signature(model: dict | None) -> dict:
    """把模型压成「类 → 属性标签集合」，用来算两版之间的结构差异。"""
    classes: dict[str, dict] = {}
    for c in (model or {}).get("classes") or []:
        key = str(c.get("curie") or c.get("label") or "")
        if not key:
            continue
        props = {}
        for p in c.get("data_props") or []:
            pk = str(p.get("curie") or p.get("label") or "")
            if pk:
                props[pk] = str(p.get("label") or p.get("curie") or pk)
        classes[key] = {"label": str(c.get("label") or key), "attrs": props}
    return {"classes": classes,
            "obj": len((model or {}).get("object_props") or []),
            "data": len((model or {}).get("data_props") or [])}


def _clip(items: list[str], n: int = 5) -> str:
    items = [x for x in items if x]
    if not items:
        return ""
    head = "、".join(items[:n])
    return head + (f" 等 {len(items)} 个" if len(items) > n else "")


def compare(old_model: dict | None, new_model: dict | None) -> dict | None:
    """两版本体的结构差异（都从图上算，不看模型自述）。旧版缺失时返回 None。"""
    if not old_model or not new_model:
        return None
    a, b = _signature(old_model), _signature(new_model)
    added = [b["classes"][k]["label"] for k in b["classes"] if k not in a["classes"]]
    removed = [a["classes"][k]["label"] for k in a["classes"] if k not in b["classes"]]
    attrs_add: list[dict] = []
    attrs_del: list[dict] = []
    for k, cls in b["classes"].items():
        if k not in a["classes"]:
            continue
        old_p, new_p = a["classes"][k]["attrs"], cls["attrs"]
        add = [new_p[p] for p in new_p if p not in old_p]
        dele = [old_p[p] for p in old_p if p not in new_p]
        if add:
            attrs_add.append({"cls": cls["label"], "props": add})
        if dele:
            attrs_del.append({"cls": cls["label"], "props": dele})
    return {"added_classes": added, "removed_classes": removed,
            "added_attrs": attrs_add, "removed_attrs": attrs_del,
            "relations": [a["obj"], b["obj"]],
            "data_props": [a["data"], b["data"]]}


def _reply(delta: dict | None, header: dict, new_model: dict | None) -> str:
    """对话里那句人话。首轮说整体，后续说「这轮改了什么」。"""
    st = (new_model or {}).get("stats") or {}
    head = (f"{st.get('classes', 0)} 个类 / {st.get('object_props', 0)} 条关系 / "
            f"{st.get('data_props', 0)} 个数据属性")
    summary = (header.get("summary") or "").strip()
    if delta is None:
        return ((summary + " ") if summary else "已生成一版本体草稿。") + f"共 {head}。"
    bits: list[str] = []
    if delta["added_classes"]:
        bits.append("新增类 " + _clip(delta["added_classes"]))
    if delta["removed_classes"]:
        bits.append("删掉类 " + _clip(delta["removed_classes"]))
    for a in delta["added_attrs"]:
        bits.append(f"「{a['cls']}」加了 {_clip(a['props'])}")
    for d in delta["removed_attrs"]:
        bits.append(f"「{d['cls']}」去掉 {_clip(d['props'])}")
    old_r, new_r = delta["relations"]
    if old_r != new_r:
        bits.append(f"关系 {old_r} → {new_r} 条")
    old_d, new_d = delta["data_props"]
    if old_d != new_d and not any("加了" in b for b in bits):
        bits.append(f"数据属性 {old_d} → {new_d} 个")
    if not bits:
        return f"这一版结构没变（类 / 属性 / 关系数量与上一版一致），现在仍是 {head}。"
    return "改好了：" + "；".join(bits) + f"。现在共 {head}。"


def draft(requirements: str, existing_model: dict | None = None,
          base_turtle: str | None = None,
          history: list[dict] | None = None) -> dict:
    """调模型写本体。返回 {ok, turtle, header, attempts, engine, reply, delta, model}。

    `base_turtle` 非空 = 对话式改写：在上一版草稿上改，并回报结构差异。
    为空但 `existing_model` 非空 = 往一个已导入的本体里补设计（不重复建已有的类）。
    """
    req = (requirements or "").strip()
    if not req:
        return {"ok": False, "error": "需求为空", "engine": "llm", "attempts": []}
    req = req[:_MAX_REQ_CHARS]
    base = _base_text(base_turtle)
    ctx = base if base else _brief_existing(existing_model)
    user = ctx + _history_text(history) + "业务需求：\n" + req + "\n\n请输出 Turtle。"

    attempts: list[dict] = []
    last_err = ""
    turtle = ""
    for round_no in (1, 2):
        try:
            raw = llm_client.chat(
                _SYSTEM,
                user + (f"\n\n上一次的输出无法解析，请修正后重新只输出合法 Turtle。"
                        f"解析器报错：{last_err}" if last_err else ""),
                temperature=0.2, timeout=120,
            )
        except Exception as exc:  # noqa: BLE001  网关不可用 / 超时
            last_err = f"LLM 调用失败：{type(exc).__name__}: {str(exc)[:200]}"
            attempts.append({"engine": f"llm#{round_no}", "error": last_err})
            continue

        meta, body = _header(_strip_fence(raw))
        graph, err = _parse(body)
        attempts.append({"engine": f"llm#{round_no}", "ok": graph is not None,
                         "chars": len(body), "error": err,
                         "classes": _counts(graph)["classes"] if graph is not None else 0})
        if graph is not None:
            # 落库用的 Turtle 走一次 serialize：补上 owl:Ontology 声明，并且把前缀
            # 按 rdflib 的绑定原样写出来，避免复制进新图后 CURIE 退化成整条 URI
            graph = _stamp(graph, meta)
            turtle = graph.serialize(format="turtle")
            new_model = kg_model.build_model(graph)
            old_model = _model_of(base_turtle) if base else None
            delta = compare(old_model, new_model)
            return {"ok": True, "turtle": turtle, "header": meta, "attempts": attempts,
                    "engine": "llm", "graph": graph, "model": new_model,
                    "delta": delta, "reply": _reply(delta, meta, new_model)}
        last_err = err
        turtle = body

    return {"ok": False, "error": last_err or "模型没有产出可用的本体",
            "engine": "unavailable" if not turtle else "llm",
            "attempts": attempts, "turtle": turtle}


def _model_of(turtle: str | None) -> dict | None:
    """把上一版草稿解析成模型；解析不了就当没有基准（差分会跳过）。"""
    if not (turtle or "").strip():
        return None
    try:
        graph, _fmt = kg_model.parse_rdf("base.ttl", turtle.encode("utf-8"))
        return kg_model.build_model(graph)
    except Exception:  # noqa: BLE001  基准坏了不该拖垮本轮生成
        return None



def summarize(graph: Graph, header: dict) -> dict:
    """给前端展示的设计摘要：类 + 每类属性数 + 关系数（都从图上算，不靠模型自述）。"""
    m = kg_model.build_model(graph)
    classes = [{"curie": c.get("curie") or "", "label": c.get("label") or "",
                "attrs": len(c.get("data_props") or []),
                "desc": c.get("comment") or ""}
               for c in (m.get("classes") or [])]
    return {
        "name": header.get("name") or (m.get("ontology") or {}).get("label") or "AI 设计本体",
        "summary": header.get("summary") or (m.get("ontology") or {}).get("comment") or "",
        "assumptions": header.get("assumptions") or [],
        "classes": classes,
        "relations": len(m.get("object_props") or []),
        "data_props": len(m.get("data_props") or []),
        "stats": m.get("stats") or {},
        "model": m,
    }
