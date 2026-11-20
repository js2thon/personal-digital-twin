"""基于本体语义的问答：自然语言 → SPARQL → 结果 → 中文回答。

为什么不是「直接让模型回答」：模型看不到库里的数据，只能编。
这里把本体结构（类 / 属性 / domain / range）当上下文，让模型写查询、
由 rdflib 在图上报答案——**答案的事实来源是数据，不是模型**。

三级降级，保证任何情况下都有可解释的结果：
1. LLM 生成 SPARQL → 执行；
2. 执行失败 → 把报错回喂模型改一次；
3. 模型不可用 / 两次都失败 → 关键词模板匹配（离线也能用）。
"""
from __future__ import annotations

import re

from app.modules.llm.client import llm_client
from app.modules.ontology.kg import query as kg_query
from app.modules.ontology.kg import model as kg_model
from app.modules.ontology.kg.store import KgSession

_SYSTEM = """你是知识图谱问答引擎里的 SPARQL 1.1 生成器。
规则：
1. 只输出 SELECT 或 ASK 查询，禁止任何更新语句（INSERT/DELETE/LOAD/DROP/CLEAR）。
2. 必须使用下面给出的类和属性（CURIE 优先），不要发明不存在的 IRI。
3. 中文取值优先用 rdfs:label 取，日期/数字直接比较；数量用 COUNT(DISTINCT ?x)。
4. 结果变量取有意义的名字（?name、?dept、?cnt），不要用 ?o1 ?o2。
5. 默认加 LIMIT 200（聚合查询除外）。
6. 只输出 JSON：{"sparql": "<查询>", "intent": "<一句话说明你查了什么>"}
"""


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:sparql)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    return t


def _prefixed_examples(model: dict, limit: int = 2) -> str:
    """给模型看一两条「同本体、能跑」的查询当风格样例，比纯口述规则稳。"""
    has_emp = any((c.get("curie") or "").startswith("emp:") for c in model.get("classes") or [])
    if not has_emp:
        return ""
    picks = [e for e in kg_query.EXAMPLES if e["id"] in ("e02", "e05")]
    out = []
    for ex in picks[:limit]:
        out.append(f"# {ex['question']}\n{ex['query']}")
    return "\n\n".join(out)


def _llm_sparql(model: dict, question: str, retry_note: str = "") -> tuple[str, str]:
    brief = kg_model.schema_brief(model)
    samples = _prefixed_examples(model)
    user = (
        f"本体结构：\n{brief}\n\n"
        + (f"可参考的查询写法：\n{samples}\n\n" if samples else "")
        + (f"上一次生成的查询执行报错，请修正：{retry_note}\n\n" if retry_note else "")
        + f"问题：{question}\n请输出 JSON。"
    )
    data = llm_client.chat_json(_SYSTEM, user, timeout=60)
    if isinstance(data, dict):
        sparql = _strip_fence(str(data.get("sparql") or ""))
        intent = str(data.get("intent") or "")
        if sparql:
            return sparql, intent
    return "", ""


def _answer_text(vars_: list[str], rows: list[list[dict]]) -> str:
    if not rows:
        return "没有查到符合条件的数据（0 行）。可能是本体里没有这类数据，或问题里的条件太严。"
    if len(rows) == 1:
        if len(vars_) == 1:
            return f"答案：{rows[0][0]['v']}"
        pairs = "，".join(f"{v}={row['v']}" for v, row in zip(vars_, rows[0]))
        return f"查到 1 条记录：{pairs}"
    if len(vars_) == 1:
        values = [r[0]["v"] for r in rows[:10]]
        return f"共 {len(rows)} 条：{'、'.join(values)}" + ("…" if len(rows) > 10 else "")
    head = "；".join(" / ".join(c["v"] for c in r) for r in rows[:3])
    return f"共 {len(rows)} 条结果。前 3 条：{head}" + ("…" if len(rows) > 3 else "")


def _ask_classes(model: dict) -> list[dict]:
    return model.get("classes") or []


def _match_class(model: dict, question: str) -> dict | None:
    q_low = question.lower()
    best, best_len = None, 0
    for c in _ask_classes(model):
        names = [c.get("label") or "", (c.get("curie") or "").split(":")[-1]]
        for name in names:
            if name and len(name) >= 2 and name.lower() in q_low and len(name) > best_len:
                best, best_len = c, len(name)
    return best


def _template(model: dict, question: str) -> tuple[str, str]:
    q = question.lower()
    cls = _match_class(model, question)
    is_count = bool(re.search(r"多少|几个|几位|数量|人数|统计|几[个条名]|how many|count", q))
    is_props = bool(re.search(r"属性|字段|property|字段名|有哪些列", q))
    is_sub = bool(re.search(r"子类|父类|继承|层级|subclass|hierarch", q))
    is_rel = bool(re.search(r"关系|对象属性|relation|关联", q))

    if is_props and cls:
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:  <http://www.w3.org/2002/07/owl#>

SELECT ?prop ?label ?range
WHERE {{
  ?prop rdfs:domain {cls['curie']} .
  OPTIONAL {{ ?prop rdfs:label ?l . FILTER(lang(?l) = "zh") }}
  OPTIONAL {{ ?prop rdfs:range ?r }}
  BIND(COALESCE(?l, REPLACE(STR(?prop), "^.*[#/]", "")) AS ?label)
  BIND(COALESCE(REPLACE(STR(?r), "^.*[#/]", ""), "-") AS ?range)
}} ORDER BY ?prop""",
                f"用模板列出 {cls['label']} 的所有属性（声明了 domain 的属性）")

    if is_sub:
        return ("""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?sub ?subLabel ?super ?superLabel
WHERE {
  ?sub rdfs:subClassOf ?super . FILTER(isIRI(?super))
  OPTIONAL { ?sub rdfs:label ?sl . FILTER(lang(?sl) = "zh") }
  OPTIONAL { ?super rdfs:label ?pl . FILTER(lang(?pl) = "zh") }
  BIND(COALESCE(?sl, REPLACE(STR(?sub), "^.*[#/]", "")) AS ?subLabel)
  BIND(COALESCE(?pl, REPLACE(STR(?super), "^.*[#/]", "")) AS ?superLabel)
} LIMIT 200""", "用模板列出类之间的 subClassOf 关系")

    if cls and is_count:
        return (f"""SELECT (COUNT(DISTINCT ?x) AS ?count)
WHERE {{ ?x a {cls['curie']} }}""",
                f"用模板统计 {cls['label']} 的实例数")

    if cls and (is_rel or is_count):
        return (f"""SELECT ?x ?rel ?y WHERE {{
  ?x a {cls['curie']} ; ?rel ?y .
  FILTER(isIRI(?y))
  FILTER(?rel != rdf:type)
}} LIMIT 50""", f"用模板查看 {cls['label']} 的关联关系")

    if cls:
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?x ?label WHERE {{
  ?x a {cls['curie']} .
  OPTIONAL {{ ?x rdfs:label ?label }}
}} LIMIT 100""", f"用模板列出 {cls['label']} 的实例")

    # 完全没匹配到类：回到结构盘点，至少给出可用的下一步
    return ("""PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?class ?label (COUNT(DISTINCT ?i) AS ?instances)
WHERE {
  { ?class a owl:Class } UNION { ?class a rdfs:Class }
  OPTIONAL { ?class rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }
  OPTIONAL { ?i a ?class }
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
} GROUP BY ?class ?label ORDER BY DESC(?instances) LIMIT 50""",
            "没识别出问的是哪个类，先列出本体里有哪些类（换个更具体的说法再问）")


def ask(sess: KgSession, question: str, use_llm: bool = True) -> dict:
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题为空"}
    model = sess.model()
    attempts: list[dict] = []
    sparql, intent, engine, last_error = "", "", "", ""

    if use_llm:
        for round_no in (1, 2):
            try:
                spa, it = _llm_sparql(model, question, "" if round_no == 1 else last_error)
            except Exception as exc:  # noqa: BLE001  网关不可用 / 超时都走模板兜底
                last_error = f"LLM 调用失败：{type(exc).__name__}: {str(exc)[:160]}"
                attempts.append({"engine": f"llm#{round_no}", "error": last_error})
                break
            if not spa:
                last_error = "模型没有返回可用的 SPARQL"
                attempts.append({"engine": f"llm#{round_no}", "error": last_error})
                continue
            with sess.lock:
                res = kg_query.run(sess.graph, spa, prefixes=model.get("prefixes") or {})
            attempts.append({"engine": f"llm#{round_no}", "ok": res.get("ok"),
                             "ms": res.get("ms"), "error": res.get("error", "")})
            if res.get("ok"):
                sparql, intent, engine, last_error = spa, it, "llm", ""
                break
            last_error = res.get("error", "")

    if not sparql:
        spa, it = _template(model, question)
        with sess.lock:
            res = kg_query.run(sess.graph, spa, prefixes=model.get("prefixes") or {})
        attempts.append({"engine": "template", "ok": res.get("ok"), "ms": res.get("ms"),
                         "error": res.get("error", "")})
        if res.get("ok"):
            sparql, intent, engine = spa, it, "template"
        else:
            return {"ok": False, "error": res.get("error") or "查询执行失败",
                    "sparql": spa, "attempts": attempts, "engine": "template"}

    answer = _answer_text(res.get("vars") or [], res.get("rows") or [])
    return {
        "ok": True, "engine": engine, "intent": intent, "answer": answer,
        "sparql": sparql, "result": res, "attempts": attempts,
        "note": ("模型生成的查询已执行，答案来自图数据。" if engine == "llm"
                 else "模型不可用，已用关键词模板兜底（结果同样来自图数据）。"),
    }
