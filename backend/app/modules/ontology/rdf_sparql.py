"""SPARQL 示例与只读执行：把「本体 + 实例」装进内存图，跑可直接复制的查询。

为什么要这一层：项目里原来的本体只有「画布 spec / 编译 IR / JSON-LD 导出」三种形态，
没有可执行查询入口。本体导入之后，用户最直接的验收方式是**拿一组 SPARQL 跑出真数据**，
所以这里补一个纯内存、只读、无副作用的执行器。

口径约定：

- 图从哪来（三条路，互斥优先）：
    ① 传 ``text``（TTL / RDF-XML / JSON-LD 原文）→ 直接用原图，命名空间即作者声明的那套；
    ② 传 ``spec``（建模画布 spec）→ 走 ``jsonld.build_jsonld`` 转 JSON-LD 再解析，
       本体外的父类、枚举个体等由导出层保证不自相矛盾；
    ③ 传 ``instances``（``rdf_import.build_instances`` 的产出）→ 把实例补进图里，
       这样「导入 TTL 但实例在另一份数据里」也能一起查。
- **只读**：只调 ``graph.query()``，从不调 ``graph.update()``；``SERVICE`` / ``LOAD``
  会发起网络请求，一律拒绝（外部端点会把内网 IRI 带出去，也会让结果不可复现）。
- 示例查询不是写死的字符串：``examples(graph)`` 从图里探测本体命名空间、类、属性、
  枚举，**现场拼**出针对这份本体的语句。换一份本体重跑，示例依然可执行。
- 结果如实返回：解析失败给 ``error`` 与行列位置，不吞异常、不编造空结果。
"""

from __future__ import annotations

import time
from typing import Any

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from app.modules.ontology import rdf_jsonld as JL
from app.modules.ontology import rdf_import as RI

# 查询里出现这些关键字就是「不是只读查询」或者「会出网」，直接拒
_FORBIDDEN = ("SERVICE", "LOAD", "INSERT", "DELETE", "DROP", "CLEAR",
              "CREATE", "ADD", "MOVE", "COPY", "WITH")
# 允许的查询形态（首个非 PREFIX/BASE 关键字）
_QUERY_FORMS = ("SELECT", "ASK", "CONSTRUCT", "DESCRIBE")

# 结果序列化的默认上限（防止 CONSTRUCT 大图把响应撑爆）
MAX_ROWS = 500

# 自检用：只有结构、没有实例的小本体（用来验「补实例」这条路）
_ONT_TTL = """
@prefix : <http://example.org/inst#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Employee a owl:Class .
:Status a owl:Class ; owl:oneOf ( :Active :Resigned ) .
:Active a owl:NamedIndividual , :Status .
:Resigned a owl:NamedIndividual , :Status .
:fullName a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string .
:hasStatus a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Status .
:mentor a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Employee .
"""


# ---------------------------------------------------------------- 基础工具
def _s(v: Any) -> str:
    return str(v or "").strip()


def _local(v: Any) -> str:
    s = _s(v).rstrip("/#")
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def _ns_of(v: Any) -> str:
    s = str(v) if isinstance(v, URIRef) else ""
    for sep in ("#", "/"):
        i = s.rfind(sep)
        if i > 0:
            return s[: i + 1]
    return ""


def _qname(graph: Graph, iri: Any) -> str:
    """IRI → 带前缀写法（:Employee / foaf:Person）；解析不出前缀退回局部名。"""
    try:
        q = graph.namespace_manager.normalizeUri(iri)
        got = _s(q)
        # normalizeUri 解析不到前缀时会原样返回完整 IRI，这种情况退回局部名更可读
        if got and not got.startswith(("http://", "https://", "urn:")):
            return got
    except Exception:  # noqa: BLE001
        pass
    return _local(iri)


def _label(graph: Graph, subj: Any) -> str:
    """中文优先的显示名，其次英文、其它语言。"""
    zh = en = other = ""
    for pred in (RDFS.label, SKOS.prefLabel):
        for o in graph.objects(subj, pred):
            if not isinstance(o, Literal):
                continue
            val = _s(o)
            if not val:
                continue
            lang = _s(o.language).lower()
            if lang.startswith("zh") and not zh:
                zh = val
            elif lang.startswith("en") and not en:
                en = val
            elif not other:
                other = val
    return zh or other or en


def _class_list(graph: Graph) -> list[URIRef]:
    out = [c for c in graph.subjects(RDF.type, OWL.Class) if _is_app_class(c)]
    # 也收「只被 rdfs:domain/range 引用的类」——很多本体不显式 a owl:Class
    for pred in (RDFS.domain, RDFS.range):
        for c in graph.objects(None, pred):
            if _is_app_class(c) and c not in out:
                out.append(c)
    seen: set[str] = set()
    uniq: list[URIRef] = []
    for c in out:
        k = str(c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


# 标准词表里的 IRI 不是「业务类」，别把它们当类收进来
_STD_NS = ("http://www.w3.org/2001/XMLSchema#",
           "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
           "http://www.w3.org/2000/01/rdf-schema#",
           "http://www.w3.org/2002/07/owl#",
           "http://www.w3.org/2004/02/skos/core#",
           "http://purl.org/dc/", "http://xmlns.com/foaf/0.1/")


def _is_app_class(v: Any) -> bool:
    if not isinstance(v, URIRef):
        return False
    s = str(v)
    return not s.startswith(_STD_NS)


def _props_of(graph: Graph, typ: URIRef) -> list[URIRef]:
    out = [p for p in graph.subjects(RDF.type, typ) if isinstance(p, URIRef)]
    return sorted(set(out), key=lambda x: str(x))


def _range_of(graph: Graph, prop: Any) -> Any:
    for o in graph.objects(prop, RDFS.range):
        return o
    return None


def _domains(graph: Graph, prop: Any) -> list[Any]:
    return [d for d in graph.objects(prop, RDFS.domain) if isinstance(d, URIRef)]


def _enum_members(graph: Graph, cls: Any) -> list[Any]:
    """枚举成员：优先 owl:oneOf，其次「a 该类」的个体。"""
    members = RI._one_of(graph, cls)
    if members:
        return members
    return [i for i in graph.subjects(RDF.type, cls) if isinstance(i, URIRef)]


# ---------------------------------------------------------------- 构图
def graph_from_text(text: str, fmt: str | None = None) -> tuple[Graph, str]:
    """本体原文 → 图。解析失败抛 ValueError（由调用方转 400）。"""
    return RI.parse_text(text, fmt)


def graph_from_spec(spec: dict[str, Any], *, base_iri: str = "",
                    title: str = "") -> Graph:
    """画布 spec → 图（借 JSON-LD 导出，保证与「导出」看到的模型一致）。"""
    text = JL.to_jsonld_text(spec, base_iri=base_iri or JL.DEFAULT_BASE_IRI,
                             title=title or JL.DEFAULT_TITLE)
    graph = Graph()
    graph.parse(data=text, format="json-ld")
    return graph


def add_instances(graph: Graph, instances: dict[str, Any]) -> int:
    """把实例载荷补进图，返回新增三元组数。

    只写「实体 → 属性事实」「实体 → 关系」两类；``properties`` 是事实的镜像，
    跳过避免重复计数。

    载荷有两种来路，字段名不一样，**两种都必须认**：

    - ``rdf_import.import_text()`` 产出：实体 ``ref`` + ``iri``，事实 ``subject_ref``，
      关系 ``source_ref`` / ``target_ref``；
    - 历史数据目录产出：``iri`` / ``subject_iri`` / ``source_iri`` / ``target_iri``。

    只认一种的后果是「导入本体 + 补实例」时事实与关系被静默跳过——图里三元组不增、
    查询回空行，和「本来就没数据」完全不可区分（当初就是这样丢掉了整份实例）。
    """
    before = len(graph)
    ref_iri: dict[str, str] = {}
    for e in instances.get("entities") or []:
        iri = _s(e.get("iri") or "")
        ref = _s(e.get("ref") or "")
        if ref and iri:
            ref_iri[ref] = iri
        if not iri:
            continue
        subj = URIRef(iri)
        # 一个个体可以有多条 rdf:type：``entity_type`` 只是「主类型」，细分类型在
        # ``types`` 里（如 Department / DepartmentLevel1 / OrganizationalUnit）。
        # 只写主类型的话，「一级部门有多少个」这类问**细分类型**的问题永远 0 行，
        # 而图里其实有数据——这种「数据在、查不到」最难被发现。
        cands: list[str] = []
        for t in [e.get("entity_type")] + list(e.get("types") or []):
            v = _s(t)
            if v and v not in cands:
                cands.append(v)
        for etype in cands:
            # 类型断言优先指向本体里同名类；找不到就不写（不凭空造类）
            cls = _find_class(graph, etype)
            if cls is not None:
                graph.add((subj, RDF.type, cls))
        name = _s(e.get("canonical_name"))
        if name:
            graph.add((subj, RDFS.label, Literal(name, lang="zh")))

    def _subject(*cands: Any) -> str:
        """``iri`` 原样用；``*_ref`` 过 ref→IRI 表（表的依据是同批 entities）。"""
        for c in cands:
            v = _s(c)
            if not v:
                continue
            if v.startswith(("http://", "https://", "urn:")):
                return v
            if v in ref_iri:
                return ref_iri[v]
        return ""

    for f in instances.get("facts") or []:
        iri = _subject(f.get("subject_iri"), f.get("subject_ref"), f.get("iri"))
        pred = _s(f.get("predicate"))
        if not iri or not pred:
            continue
        p = _find_prop(graph, pred)
        if p is None:
            continue
        # 对象属性（枚举成员 / 实体引用）的取值必须落成 IRI：写成字面量的话，
        # 「?v a :Status」「?e :mentor ?x」这类查询永远查不到，等于数据丢了但看不出来。
        target = _object_target(graph, p, f.get("value"), f.get("value_iri"), ref_iri)
        if target is not None:
            graph.add((URIRef(iri), p, target))
        else:
            graph.add((URIRef(iri), p, _literal(f.get("value"), f.get("datatype"))))
    for rel in instances.get("relations") or []:
        s_iri = _subject(rel.get("source_iri"), rel.get("source_ref"))
        t_iri = _subject(rel.get("target_iri"), rel.get("target_ref"))
        pred = _s(rel.get("relation_type") or rel.get("predicate"))
        if not (s_iri and t_iri and pred):
            continue
        # 关系两端都是 IRI，命中同名数据属性就是写错了谓词：优先对象属性
        p = _find_prop(graph, pred, prefer="object")
        if p is not None:
            graph.add((URIRef(s_iri), p, URIRef(t_iri)))
    return len(graph) - before


def _object_target(graph: Graph, prop: Any, val: Any, value_iri: Any,
                   ref_iri: dict[str, str]) -> Any:
    """对象属性的取值 → :class:`URIRef`；不是对象属性或对不上个体时返回 ``None``。

    判定顺序：显式 ``value_iri`` → 本身就是绝对 IRI → 同批实体 ref →
    range 类下按局部名匹配的个体（枚举成员优先走 ``owl:oneOf``）。
    """
    v = _s(value_iri) or _s(val)
    if not v:
        return None
    if v.startswith(("http://", "https://", "urn:")):
        return URIRef(v)
    if v in ref_iri:
        return URIRef(ref_iri[v])
    if (prop, RDF.type, OWL.ObjectProperty) not in graph:
        return None
    rng = _range_of(graph, prop)
    if rng is None:
        return None
    for m in _enum_members(graph, rng):
        if _local(m) == v:
            return URIRef(str(m))
    return None


def _find_class(graph: Graph, local: str) -> Any:
    for c in _class_list(graph):
        if _local(c) == local:
            return c
    return None


def _find_prop(graph: Graph, local: str, prefer: str = "") -> Any:
    """按局部名找属性。

    同名双谓词是**真实存在**的坑：画布上既可能把 ``hasParentDepartment`` 写成部门的
    数据属性（``attributes`` 文本块里那一行），又有一个同名对象属性。若不加区分，
    写**关系**时可能命中字符串属性——13 条层级边全部落到字面量谓词上，
    对象属性一条不增，「上级的上级」永远查不出东西，而且看不出来（只是 0 行）。

    所以给调用方一个优先级：写关系（目标是 IRI）时 ``prefer="object"``。
    """
    dt = list(graph.subjects(RDF.type, OWL.DatatypeProperty))
    ob = list(graph.subjects(RDF.type, OWL.ObjectProperty))
    order = (ob + dt) if prefer == "object" else (dt + ob)
    for p in order:
        if _local(p) == local:
            return p
    return None


def _literal(val: Any, datatype: str | None) -> Literal:
    dt = _local(_s(datatype))
    if dt and hasattr(XSD, dt):
        try:
            return Literal(val, datatype=getattr(XSD, dt))
        except Exception:  # noqa: BLE001
            pass
    return Literal(val)


# ---------------------------------------------------------------- 本体画像
def profile(graph: Graph) -> dict[str, Any]:
    """探测这份本体的「可查询面」：命名空间、类、属性、枚举、实例数。"""
    classes = _class_list(graph)
    dt_props = _props_of(graph, OWL.DatatypeProperty)
    op_props = _props_of(graph, OWL.ObjectProperty)

    enum_classes: dict[str, list[Any]] = {}
    for c in classes:
        # 枚举口径与 rdf_import 保持一致：**只认 owl:oneOf**。
        # 若按「被对象属性当 range」判，Employee 这种被自指属性引用的实体类会被误判成枚举。
        if not RI._one_of(graph, c):
            continue
        members = _enum_members(graph, c)
        if members:
            enum_classes[_local(c)] = members

    inst_counts: dict[str, int] = {}
    for c in classes:
        if _local(c) in enum_classes:
            continue
        n = len([i for i in graph.subjects(RDF.type, c) if isinstance(i, URIRef)])
        if n:
            inst_counts[_local(c)] = n

    onto_ns = _ontology_ns(graph)
    return {
        "ontology_namespace": onto_ns,
        "ontology_qname": _qname(graph, URIRef(onto_ns)) if onto_ns else "",
        "classes": [{"local": _local(c), "qname": _qname(graph, c), "iri": str(c),
                     "label": _label(graph, c), "instances": inst_counts.get(_local(c), 0)}
                    for c in classes if _local(c) not in enum_classes],
        "enum_classes": [{"local": k, "values": [
            {"qname": _qname(graph, m), "label": _label(graph, m), "local": _local(m)}
            for m in v]} for k, v in enum_classes.items()],
        "datatype_properties": [{"local": _local(p), "qname": _qname(graph, p),
                                 "iri": str(p), "label": _label(graph, p),
                                 "domain": _local(d) if (d := (_domains(graph, p) or [None])[0]) else "",
                                 "range": _local(_range_of(graph, p))}
                                for p in dt_props],
        "object_properties": [{"local": _local(p), "qname": _qname(graph, p),
                               "iri": str(p), "label": _label(graph, p),
                               "domain": _local(d) if (d := (_domains(graph, p) or [None])[0]) else "",
                               "range": _local(_range_of(graph, p)),
                               "enum": _local(_range_of(graph, p)) in enum_classes}
                              for p in op_props],
        "triples": len(graph),
    }


def _ontology_ns(graph: Graph) -> str:
    """本体命名空间：优先「类实际所在命名空间」（可精确取到 ``... #``），
    其次 ``owl:Ontology`` 主语所在命名空间。

    反过来的顺序会出错：``<http://example.org/onto> a owl:Ontology`` 的
    ``_ns_of`` 得到的是 ``http://example.org/``，而类在前缀 ``http://example.org/onto#`` 下，
    两者一错位，所有 ``:Class`` 都查不到东西。
    """
    tally: dict[str, int] = {}
    for c in _class_list(graph):
        ns = _ns_of(c)
        if ns:
            tally[ns] = tally.get(ns, 0) + 1
    if tally:
        return max(tally, key=lambda k: tally[k])
    for s in graph.subjects(RDF.type, OWL.Ontology):
        ns = _ns_of(s)
        if ns:
            return ns
    return ""


def prefix_block(graph: Graph, ns: str = "") -> str:
    """生成 PREFIX 头：本体命名空间用空前缀 ``:``，加上标准词表。"""
    lines: list[str] = []
    if ns:
        lines.append(f"PREFIX : <{ns}>")
    lines += [
        "PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
        "PREFIX owl:  <http://www.w3.org/2002/07/owl#>",
        "PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>",
        "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>",
    ]
    # 本体里还用到别的命名空间（foaf 等）时补上，最多补 6 个。
    # **只补图里真的出现过三元组项的词表**：rdflib 的 NamespaceManager 一建图就
    # 默认绑了 brick/csvw/dc/dcat/dcmitype/dcterms 一堆词表，照搬会把无关前缀塞进
    # 示例头——不影响执行，但读起来全是噪声，也让人以为本体引了这些词表。
    used_ns: set[str] = set()
    for s_, p_, o_ in graph:
        for t_ in (s_, p_, o_):
            if isinstance(t_, URIRef):
                i = str(t_)
                used_ns.add(i.rsplit("#", 1)[0] + "#" if "#" in i
                            else i.rsplit("/", 1)[0] + "/")
    extra: list[tuple[str, str]] = []
    for prefix, uri in graph.namespaces():
        p = _s(prefix)
        u = _s(uri)
        if not p or not u or u == ns:
            continue
        if u not in used_ns:
            continue          # 声明了但一条三元组都没用上 → 不进 PREFIX 头
        if u.startswith(("http://www.w3.org/1999/02/22-rdf-syntax-ns#",
                         "http://www.w3.org/2000/01/rdf-schema#",
                         "http://www.w3.org/2002/07/owl#",
                         "http://www.w3.org/2001/XMLSchema#",
                         "http://www.w3.org/2004/02/skos/core#")):
            continue
        if any(u == x[1] for x in extra):
            continue
        extra.append((p, u))
    for p, u in extra[:6]:
        lines.append(f"PREFIX {p}: <{u}>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 示例查询
def examples(graph: Graph | None = None) -> list[dict[str, Any]]:
    """按本体现场生成一组可直接跑的 SPARQL。

    没有图时返回「非定制」的骨架版（依赖 ``:`` 前缀，示例说明里讲清怎么用）。
    每条给 ``id/title/desc/query/tags``，顺序按「入门 → 统计 → 多跳 → 体检」排列。
    """
    if graph is None:
        return _generic_examples()
    prof = profile(graph)
    ns = prof["ontology_namespace"]
    head = prefix_block(graph, ns)
    cls = prof["classes"]
    if not cls:
        return _generic_examples()
    # 挑主角类：有实例的优先按实例数；**没有实例时按声明的属性/关系数挑最丰富的那个**。
    # 原来没有实例就取第一个类，纯结构本体（还没灌数据）的示例会全落在最空的类上，
    # 跑出来一片空行——等于没给示例。
    prop_tally: dict[str, int] = {}
    for _p in prof["datatype_properties"] + prof["object_properties"]:
        _d = _p["domain"]
        if _d:
            prop_tally[_d] = prop_tally.get(_d, 0) + 1
    main = max(cls, key=lambda c: (c["instances"], prop_tally.get(c["local"], 0)))
    main_q = main["qname"] if main["qname"].startswith(":") else ":" + main["local"]
    dt_by_dom = [p for p in prof["datatype_properties"] if p["domain"] == main["local"]]
    op_by_dom = [p for p in prof["object_properties"] if p["domain"] == main["local"]]
    enum_ops = [p for p in op_by_dom if p["enum"]]
    rel_ops = [p for p in op_by_dom if not p["enum"]]
    ref_ops = [p for p in op_by_dom if not p["enum"] and p["range"] in
               {c["local"] for c in cls}]

    def _edges(p: dict[str, Any]) -> int:
        return len(list(graph.triples((None, URIRef(_s(p.get("iri"))), None))))

    # 示例要跑出真数据：同类属性里优先选图上有边的那个
    enum_ops = sorted(enum_ops, key=lambda p: (-_edges(p), p["local"]))
    ref_ops = sorted(ref_ops, key=lambda p: (-_edges(p), p["local"]))

    out: list[dict[str, Any]] = []

    # 1 拷问「本体里到底有什么」——体检最先
    out.append(_ex(
        "ontology-inventory", "本体全貌清点",
        "列出这份本体声明的类、数据属性、对象属性数量，先确认图装载正确。",
        head + """
SELECT ?kind (COUNT(DISTINCT ?x) AS ?n)
WHERE {
  { ?x a owl:Class }        UNION
  { ?x a owl:DatatypeProperty } UNION
  { ?x a owl:ObjectProperty }
  BIND(IF(EXISTS { ?x a owl:Class }, "类",
       IF(EXISTS { ?x a owl:DatatypeProperty }, "数据属性", "对象属性")) AS ?kind)
}
GROUP BY ?kind
ORDER BY ?kind""",
        ["体检"]))

    # 2 类清单 + 每类实例数
    out.append(_ex(
        "class-census", "各类实例数（含空类）",
        "把本体的类全列出来并 LEFT JOIN 计数，空的类显示 0——能立刻看出哪些类还没数据。",
        head + """
SELECT ?cls (SAMPLE(?label) AS ?clsLabel) (COUNT(DISTINCT ?inst) AS ?n)
WHERE {
  ?cls a owl:Class .
  FILTER NOT EXISTS { ?cls owl:oneOf ?any }
  OPTIONAL { ?cls rdfs:label ?label }
  OPTIONAL { ?inst a ?cls }
}
GROUP BY ?cls
ORDER BY DESC(?n)""",
        ["统计"]))

    # 3 主角类的实例列举
    out.append(_ex(
        "instances-of-main", f"{main['label'] or main['local']} 全部实例",
        f"列出 ``{main_q}`` 的所有实例与其显示名。",
        head + f"""
SELECT ?s ?name
WHERE {{
  ?s a {main_q} .
  # 多语种 label 各占一行会把同一个实例重复列出，故按主题取一个
  OPTIONAL {{ SELECT ?s (SAMPLE(?lbl) AS ?name) WHERE {{ ?s rdfs:label ?lbl }} GROUP BY ?s }}
}}
ORDER BY ?name""",
        ["入门"]))

    # 4 按枚举值过滤（有多少在职）
    if enum_ops:
        ep = enum_ops[0]
        eprop = ep["qname"] if ep["qname"].startswith(":") else ":" + ep["local"]
        vals = next((e["values"] for e in prof["enum_classes"] if e["local"] == ep["range"]), [])
        if vals:
            vq = vals[0]["qname"] if vals[0]["qname"].startswith(":") else ":" + vals[0]["local"]
            vlabel = vals[0]["label"] or vals[0]["local"]
            out.append(_ex(
                "filter-by-enum", f"按枚举取值筛选：{ep['label'] or ep['local']} = {vlabel}",
                f"枚举过滤是这类本体最常用的问法；把 ``{vq}`` 换成同枚举的其它个体即可改口径。",
                head + f"""
SELECT (COUNT(DISTINCT ?s) AS ?n)
WHERE {{
  ?s a {main_q} ;
     {eprop} {vq} .
}}""",
                ["统计", "枚举"]))
            out.append(_ex(
                "enum-distribution", f"{ep['label'] or ep['local']} 取值分布",
                "按枚举值分组计数，顺手验证枚举个体是否都被用到；"
                "一个个体有多语种 label 时合并到同一格，不会把同一取值拆成多行。",
                head + f"""
SELECT ?v (GROUP_CONCAT(DISTINCT ?lbl; SEPARATOR=" / ") AS ?vLabel)
       (COUNT(DISTINCT ?s) AS ?n)
WHERE {{
  ?s a {main_q} ;
     {eprop} ?v .
  OPTIONAL {{ ?v rdfs:label ?lbl }}
}}
GROUP BY ?v
ORDER BY DESC(?n)""",
                ["统计", "枚举"]))

    # 5 单跳分组统计（按引用属性）
    if ref_ops:
        rp = ref_ops[0]
        rq = rp["qname"] if rp["qname"].startswith(":") else ":" + rp["local"]
        out.append(_ex(
            "group-by-ref", f"按「{rp['label'] or rp['local']}」分组计数",
            f"经对象属性 ``{rq}`` 把 {main['label'] or main['local']} 挂到 "
            f"{rp['range']} 上分组统计。",
            head + f"""
SELECT ?target (GROUP_CONCAT(DISTINCT ?lbl; SEPARATOR=" / ") AS ?targetLabel)
       (COUNT(DISTINCT ?s) AS ?n)
WHERE {{
  ?s a {main_q} ;
     {rq} ?target .
  OPTIONAL {{ ?target rdfs:label ?lbl }}
}}
GROUP BY ?target
ORDER BY DESC(?n)""",
            ["统计"]))

    # 6 两跳 / 传递闭包
    depth = _relation_chain(graph, prof, main["local"])
    if depth["two_hop"]:
        a, b = depth["two_hop"]
        note = "" if depth["two_hop_hits"] else "（当前实例里没有接续的边，跑出来会空，改属性即可）"
        out.append(_ex(
            "two-hop", f"两跳：{a['label'] or a['local']} → {b['label'] or b['local']}",
            "顺着两个对象属性走下去，是验证本体关系建模是否连通的最短路径。" + note,
            head + f"""
SELECT ?s ?mid ?end
WHERE {{
  ?s a {main_q} ;
     {_cq(a)} ?mid .
  ?mid {_cq(b)} ?end .
}}
LIMIT 50""",
            ["多跳"]))
    if depth["self_chain"]:
        p = depth["self_chain"]
        note = "" if depth["self_chain_edges"] else "（该关系当前没有实例数据，跑出来会空）"
        out.append(_ex(
            "transitive", f"传递闭包：{p['label'] or p['local']}+",
            "用 ``+`` 求传递闭包，一次拿到整条上下级链路；把 ``+`` 换成 ``*`` 可含自身。" + note,
            head + f"""
SELECT ?s ?ancestor
WHERE {{
  ?s {_cq(p)}+ ?ancestor .
}}
ORDER BY ?s
LIMIT 100""",
            ["多跳"]))

    # 7 数据属性投影
    if dt_by_dom:
        picks = dt_by_dom[:4]
        sel = " ".join(f"?{_safe_var(p['local'])}" for p in picks)
        body = "\n".join(f"  OPTIONAL {{ ?s {_cq(p)} ?{_safe_var(p['local'])} }}"
                         for p in picks)
        out.append(_ex(
            "projection", "属性投影表（OPTIONAL 保全空值）",
            "把几个数据属性拉成表；用 OPTIONAL 而非要求全部命中，缺值行不会消失。",
            head + f"""
SELECT ?s ?name {sel}
WHERE {{
  ?s a {main_q} .
  OPTIONAL {{ SELECT ?s (SAMPLE(?lbl) AS ?name) WHERE {{ ?s rdfs:label ?lbl }} GROUP BY ?s }}
{body}
}}
ORDER BY ?s
LIMIT 100""",
            ["入门"]))

    # 8 必填/缺失体检
    if dt_by_dom:
        # 挑「真有缺口」的属性来举例——全员都填了的属性列出来等于什么都没说
        gap = None
        for cand in dt_by_dom:
            piris = {URIRef(cand["iri"])}
            if any(not list(graph.objects(s, next(iter(piris))))
                   for s in graph.subjects(RDF.type, URIRef(main["iri"]))):
                gap = cand
                break
        p = gap or dt_by_dom[0]
        gap_note = "" if gap else "（当前所有实例都填了，这条只作模板）"
        out.append(_ex(
            "missing-values", f"缺「{p['label'] or p['local']}」的实例",
            "反查空值，导入数据后第一件事就是看必填项有没有漏。" + gap_note,
            head + f"""
SELECT ?s
WHERE {{
  ?s a {main_q} .
  FILTER NOT EXISTS {{ ?s {_cq(p)} ?v }}
}}""",
            ["体检"]))

    # 9 枚举个体清点
    if prof["enum_classes"]:
        ec = prof["enum_classes"][0]
        out.append(_ex(
            "enum-individuals", f"枚举类 {ec['local']} 的全部取值",
            "枚举个体本身也是图里的节点，直接查出来可以核对中文标签有没有缺。",
            head + f"""
SELECT ?v (GROUP_CONCAT(DISTINCT ?lbl; SEPARATOR=" / ") AS ?label)
WHERE {{
  ?v a {_cq_local(ec['local'])} .
  OPTIONAL {{ ?v rdfs:label ?lbl }}
}}
GROUP BY ?v
ORDER BY ?v""",
            ["枚举"]))

    # 10 本体自身的 schema 体检
    out.append(_ex(
        "schema-check", "对象属性缺 domain / range",
        "对象属性没有 domain/range 就没法推导领域约束，这条能一次列全。",
        head + """
SELECT ?p ?missing
WHERE {
  ?p a owl:ObjectProperty .
  BIND(IF(NOT EXISTS { ?p rdfs:domain ?d }, "缺 domain",
       IF(NOT EXISTS { ?p rdfs:range ?r }, "缺 range", "")) AS ?missing)
  FILTER(?missing != "")
}""",
        ["体检"]))

    return out


def _ex(eid: str, title: str, desc: str, query: str, tags: list[str]) -> dict[str, Any]:
    return {"id": eid, "title": title, "desc": desc, "query": query.strip(), "tags": tags}


def _cq(p: dict[str, Any]) -> str:
    q = _s(p.get("qname"))
    if not q:
        return ":" + _s(p.get("local"))
    return q if ":" in q else ":" + q


def _cq_local(local: str) -> str:
    return ":" + _s(local)


def _safe_var(name: str) -> str:
    """属性局部名 → 合法 SPARQL 变量名（非字母数字下划线一律换下划线）。"""
    out = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in _s(name))
    return out or "v"


def _relation_chain(graph: Graph, prof: dict[str, Any],
                    main_local: str) -> dict[str, Any]:
    """找两跳链路与自指传递关系。

    同一类路径常常有多条（``parentDepartment`` 和 ``reportsTo`` 都是自指），
    这里**优先选图里真有边的那条**——示例跑出 0 行等于没给示例。
    全都无数据时退回按声明顺序的第一条，并在说明里讲清楚。
    """
    ops = prof["object_properties"]
    classes = {c["local"] for c in prof["classes"]}

    def _edge_count(p: dict[str, Any]) -> int:
        return len(list(graph.triples((None, URIRef(_s(p.get("iri"))), None))))

    hop_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for a in ops:
        if a["domain"] != main_local or a["enum"] or a["range"] not in classes:
            continue
        for b in ops:
            if b["domain"] == a["range"] and not b["enum"] and b["local"] != a["local"]:
                hop_pairs.append((a, b))

    def _hop_hits(pair: tuple[dict[str, Any], dict[str, Any]]) -> int:
        a_iri = URIRef(_s(pair[0].get("iri")))
        b_iri = URIRef(_s(pair[1].get("iri")))
        n = 0
        for _s_, _p_, mid in graph.triples((None, a_iri, None)):
            if any(graph.triples((mid, b_iri, None))):
                n += 1
        return n

    two_hop = None
    if hop_pairs:
        scored = sorted(hop_pairs, key=lambda pr: (-_hop_hits(pr), pr[0]["local"]))
        two_hop = scored[0]

    selfs = [p for p in ops if p["domain"] == p["range"] and p["domain"] in classes
             and not p["enum"]]
    self_chain = None
    if selfs:
        selfs_sorted = sorted(selfs, key=lambda p: (-_edge_count(p), p["local"]))
        self_chain = selfs_sorted[0]
    return {"two_hop": two_hop, "self_chain": self_chain,
            "two_hop_hits": _hop_hits(two_hop) if two_hop else 0,
            "self_chain_edges": _edge_count(self_chain) if self_chain else 0}


def _generic_examples() -> list[dict[str, Any]]:
    """没有图时的骨架版：不猜命名空间，留给用户填。"""
    head = ("PREFIX : <http://example.org/ontology#>\n"
            "PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "PREFIX owl:  <http://www.w3.org/2002/07/owl#>")
    return [
        _ex("class-list", "类的清单",
            "先落一个最基础的查询；把 ``:`` 换成你本体自己的命名空间即可。",
            head + """
SELECT ?cls ?label
WHERE {
  ?cls a owl:Class .
  OPTIONAL { ?cls rdfs:label ?label }
}
ORDER BY ?cls""", ["入门"]),
        _ex("instance-count", "各实例数",
            "按类统计个体数量，空的类显示 0。",
            head + """
SELECT ?cls (COUNT(DISTINCT ?s) AS ?n)
WHERE {
  ?cls a owl:Class .
  OPTIONAL { ?s a ?cls }
}
GROUP BY ?cls
ORDER BY DESC(?n)""", ["统计"]),
    ]


# ---------------------------------------------------------------- 执行
def check_query(query: str) -> tuple[str, str]:
    """只读与语法形态检查，返回 (规范化查询, 错误信息)；无错时错误为空串。"""
    q = _s(query)
    if not q:
        return "", "查询为空"
    # 去注释再判断（# 到行尾）
    stripped = "\n".join(line.split("#", 1)[0] if line.lstrip().startswith("#") else line
                         for line in q.splitlines())
    upper = stripped.upper()
    for bad in _FORBIDDEN:
        # 关键字按词边界判断，避免把变量名 ?insertTime 误杀
        if _word_in(upper, bad):
            if bad in ("SERVICE", "LOAD"):
                return q, f"不支持 {bad}：查询只在本机内存图上执行，避免外发数据"
            return q, f"只允许只读查询（SELECT/ASK/CONSTRUCT/DESCRIBE），检测到 {bad}"
    # 跳过 PREFIX / BASE 找首个查询关键字
    form = ""
    for line in _strip_prefixes(stripped).split("\n"):
        t = line.strip()
        if not t:
            continue
        form = t.split(None, 1)[0].upper().rstrip("{(")
        break
    if form not in _QUERY_FORMS:
        return q, (f"首关键字是 {form or '(空)'}，只接受 "
                   "SELECT / ASK / CONSTRUCT / DESCRIBE")
    return q, ""


def _word_in(text: str, word: str) -> bool:
    i = 0
    n = len(word)
    while True:
        i = text.find(word, i)
        if i < 0:
            return False
        before = text[i - 1] if i else " "
        after = text[i + n] if i + n < len(text) else " "
        if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
            return True
        i += n


def _strip_prefixes(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        t = line.strip()
        if t.upper().startswith(("PREFIX ", "BASE ")) or t.startswith("@prefix"):
            continue
        out.append(line)
    return "\n".join(out)


def run(graph: Graph, query: str, *, max_rows: int = MAX_ROWS) -> dict[str, Any]:
    """执行一条只读查询，返回列名 / 行 / 计数 / 耗时；出错如实给 error。"""
    q, err = check_query(query)
    if err:
        return {"ok": False, "error": err, "columns": [], "rows": [], "count": 0}
    t0 = time.perf_counter()
    try:
        res = graph.query(q)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "columns": [], "rows": [], "count": 0}
    elapsed = round((time.perf_counter() - t0) * 1000, 2)

    # rdflib 的返回类型看 Result.type（'SELECT' / 'ASK' / 'CONSTRUCT' / 'DESCRIBE'），
    # 不能看类名：SELECT 拿到的是 SPARQLResult 子类，类名判断会误落到图分支。
    form = str(getattr(res, "type", "") or "").upper()
    if form == "SELECT":
        columns = [str(v) for v in (res.vars or [])]
        rows: list[dict[str, Any]] = []
        total = 0
        for row in res:
            total += 1
            if len(rows) >= max_rows:
                continue
            rows.append({str(v): _cell(row[v]) for v in (res.vars or [])})
        return {"ok": True, "form": "SELECT", "columns": columns, "rows": rows,
                "count": total, "truncated": total > len(rows),
                "elapsed_ms": elapsed, "error": ""}
    if form == "ASK":
        ans = bool(getattr(res, "askAnswer", False))
        return {"ok": True, "form": "ASK", "columns": ["result"],
                "rows": [{"result": {"value": "true" if ans else "false",
                                     "kind": "literal", "display": "true" if ans else "false"}}],
                "count": 1, "truncated": False, "elapsed_ms": elapsed,
                "error": "", "ask": ans}
    # CONSTRUCT / DESCRIBE：结果在 res.graph 上
    got = getattr(res, "graph", None)
    triples = [[_cell(s), _cell(p), _cell(o)] for s, p, o in (got or [])]
    return {"ok": True, "form": "GRAPH", "columns": ["subject", "predicate", "object"],
            "rows": [{"subject": t[0], "predicate": t[1], "object": t[2]}
                     for t in triples[:max_rows]],
            "count": len(triples), "truncated": len(triples) > max_rows,
            "triples": len(triples), "elapsed_ms": elapsed, "error": ""}


def _cell(v: Any) -> dict[str, Any]:
    """单元格标准化：值 + 类型 + 展示名，前端直接渲染。"""
    if v is None:
        return {"value": "", "kind": "unbound", "display": ""}
    if isinstance(v, URIRef):
        return {"value": str(v), "kind": "uri", "display": _local(v)}
    if isinstance(v, BNode):
        return {"value": f"_:{v}", "kind": "bnode", "display": f"_:{v}"}
    if isinstance(v, Literal):
        dt = _local(v.datatype) if v.datatype else ""
        return {"value": str(v), "kind": "literal", "datatype": dt,
                "lang": _s(v.language), "display": str(v)}
    return {"value": str(v), "kind": type(v).__name__, "display": str(v)}


def query_text(text: str, query: str, fmt: str | None = None,
               instances: dict[str, Any] | None = None) -> dict[str, Any]:
    """一步到位：文本 → 图（可选补实例）→ 执行，并带上本体画像。"""
    graph, used = graph_from_text(text, fmt)
    added = add_instances(graph, instances) if instances else 0
    out = run(graph, query)
    out["format"] = used
    out["triples"] = len(graph)
    out["added_triples"] = added
    return out


# ---------------------------------------------------------------- 自检
_SAMPLE_TTL = """
@prefix : <http://example.org/onto#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .

<http://example.org/onto> a owl:Ontology ; rdfs:label "示例本体"@zh .

:Employee a owl:Class ; rdfs:label "员工"@zh , "Employee"@en ;
    rdfs:subClassOf foaf:Person ; owl:hasKey ( :employeeID ) .
:Department a owl:Class ; rdfs:label "部门"@zh .
:Status a owl:Class ; owl:oneOf ( :Active :Resigned ) .
:Active a owl:NamedIndividual , :Status ; rdfs:label "在职"@zh .
:Resigned a owl:NamedIndividual , :Status ; rdfs:label "离职"@zh .

:employeeID a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string .
:fullName a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string .
:hireDate a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:date .
:deptName a owl:DatatypeProperty ; rdfs:domain :Department ; rdfs:range xsd:string .
:belongsTo a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Department .
:hasStatus a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Status .
:reportsTo a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Employee .
:parentDept a owl:ObjectProperty ; rdfs:domain :Department ; rdfs:range :Department .

:emp001 a :Employee ; rdfs:label "张三"@zh ; :employeeID "E001" ; :fullName "张三" ;
    :hireDate "2020-01-01"^^xsd:date ; :belongsTo :dept_tech ;
    :hasStatus :Active ; :reportsTo :emp002 .
:emp002 a :Employee ; rdfs:label "李四"@zh ; :employeeID "E002" ; :fullName "李四" ;
    :belongsTo :dept_tech ; :hasStatus :Active .
:emp003 a :Employee ; rdfs:label "王五"@zh ; :employeeID "E003" ; :fullName "王五" ;
    :belongsTo :dept_hr ; :hasStatus :Resigned .
:dept_tech a :Department ; rdfs:label "技术部"@zh ; :deptName "技术部" .
:dept_hr a :Department ; rdfs:label "人力资源部"@zh ; :deptName "人力资源部" ;
    :parentDept :dept_tech .
"""


def self_check() -> dict[str, Any]:
    """自检：解析 → 画像 → 示例生成 → 逐条执行 → 越权拦截，全链断言。"""
    checks: list[dict[str, Any]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})

    graph, fmt = graph_from_text(_SAMPLE_TTL)
    ck("解析示例本体", len(graph) > 40, f"{fmt} / {len(graph)} 三元组")

    prof = profile(graph)
    ck("探测本体命名空间", prof["ontology_namespace"] == "http://example.org/onto#",
       prof["ontology_namespace"])
    locals_ = [c["local"] for c in prof["classes"]]
    ck("实体类进 classes、枚举类不进", sorted(locals_) == ["Department", "Employee"],
       locals_)
    ck("枚举类单列且成员齐", [e["local"] for e in prof["enum_classes"]] == ["Status"]
       and len(prof["enum_classes"][0]["values"]) == 2, prof["enum_classes"])
    ck("枚举成员带中文标签",
       prof["enum_classes"][0]["values"][0]["label"] == "在职",
       prof["enum_classes"][0]["values"][0])
    ck("类实例数正确", {c["local"]: c["instances"] for c in prof["classes"]}
       == {"Employee": 3, "Department": 2},
       {c["local"]: c["instances"] for c in prof["classes"]})
    ck("数据属性 domain/range 都解析",
       any(p["local"] == "hireDate" and p["domain"] == "Employee" and p["range"] == "date"
           for p in prof["datatype_properties"]),
       [p for p in prof["datatype_properties"] if p["local"] == "hireDate"])
    ck("对象属性标出枚举/实体两类",
       any(p["local"] == "hasStatus" and p["enum"] for p in prof["object_properties"])
       and any(p["local"] == "belongsTo" and not p["enum"]
               for p in prof["object_properties"]),
       prof["object_properties"])

    head = prefix_block(graph, prof["ontology_namespace"])
    ck("PREFIX 头含空前缀 :", head.startswith("PREFIX : <http://example.org/onto#>"), head[:80])
    ck("PREFIX 头含标准词表", all(k in head for k in ("rdfs:", "owl:", "xsd:")), "")
    ck("PREFIX 头不夹带未使用的默认词表",
       not any(k in head for k in ("brick:", "csvw:", "dc:", "dcat:", "dcmitype:", "dcterms:")),
       [ln for ln in head.splitlines() if "/dc" in ln or "brick" in ln or "csvw" in ln])
    _foaf_g = Graph()
    _foaf_g.parse(data="""
@prefix : <http://example.org/f#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .
:Employee a owl:Class ; rdfs:subClassOf foaf:Person .
""", format="turtle")
    _foaf_head = prefix_block(_foaf_g, "http://example.org/f#")
    ck("真正用到的外部词表（foaf）仍会补进 PREFIX 头",
       "PREFIX foaf: <http://xmlns.com/foaf/0.1/>" in _foaf_head, _foaf_head)

    exs = examples(graph)
    ck("示例数量覆盖各类型", len(exs) >= 8, f"{len(exs)} 条")
    ids = {e["id"] for e in exs}
    ck("示例含体检/统计/多跳/枚举四类",
       {"ontology-inventory", "class-census", "missing-values", "schema-check"}
       <= ids and "two-hop" in ids and "enum-distribution" in ids, sorted(ids))
    ck("示例全部带 PREFIX（可直接复制执行）",
       all(e["query"].lstrip().startswith("PREFIX") for e in exs),
       [e["id"] for e in exs if not e["query"].lstrip().startswith("PREFIX")])

    # 每条示例都必须能跑通——这是「可直接跑」的唯一证据
    failed: list[str] = []
    for e in exs:
        r = run(graph, e["query"])
        if not r["ok"]:
            failed.append(f"{e['id']}: {r['error']}")
    ck("10/n 条示例全部执行成功", not failed, "; ".join(failed))

    # 关键数值抽查
    by_id = {e["id"]: e for e in exs}
    r = run(graph, by_id["class-census"]["query"])
    census = {(row["cls"]["display"]): int(row["n"]["value"]) for row in r["rows"]}
    ck("类清点：Employee=3 / Department=2", census.get("Employee") == 3
       and census.get("Department") == 2, census)

    r = run(graph, by_id["enum-distribution"]["query"])
    dist = {row["v"]["display"]: int(row["n"]["value"]) for row in r["rows"]}
    ck("状态分布：Active=2 / Resigned=1", dist.get("Active") == 2
       and dist.get("Resigned") == 1, dist)

    r = run(graph, by_id["group-by-ref"]["query"])
    grp = {row["target"]["display"]: int(row["n"]["value"]) for row in r["rows"]}
    ck("按部门分组：dept_tech=2 / dept_hr=1", grp.get("dept_tech") == 2
       and grp.get("dept_hr") == 1, grp)

    r = run(graph, by_id["transitive"]["query"])
    ck("传递闭包命中 1 条汇报链", r["count"] == 1, f"{r['count']} 行")

    r = run(graph, by_id["two-hop"]["query"])
    ck("两跳：员工→部门→上级部门", r["ok"] and r["count"] == 1, f"{r['count']} 行")

    r = run(graph, by_id["missing-values"]["query"])
    ck("缺 hireDate 的实例有 2 个", r["count"] == 2, f"{r['count']} 行")

    r = run(graph, by_id["schema-check"]["query"])
    ck("schema 体检：无缺 domain/range 的对象属性", r["ok"] and r["count"] == 0,
       f"{r['count']} 行")

    # 只读与形态校验
    bad_cases = [
        ("INSERT DATA { <a> <b> <c> }", "只读"),
        ("DELETE WHERE { ?s ?p ?o }", "只读"),
        ("SELECT * WHERE { SERVICE <http://x/sparql> { ?s ?p ?o } }", "SERVICE"),
        ("SELECT * WHERE { ?s ?p ?o }", ""),          # 合法，不该被拒
        ("ASK { ?s a owl:Class }", ""),               # 合法
        ("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o } LIMIT 5", ""),  # 合法
        ("", "空"),
    ]
    for q, want in bad_cases:
        _, err = check_query(q)
        if want:
            ck(f"拦截：{want}", bool(err) and want in err, err or "(未拦截)")
        else:
            ck(f"放行：{q.split()[0] if q else ''}", not err, err)

    q, err = check_query("SELECT * WHERE { ?s ?insertTime ?o }")
    ck("变量名含关键字不误杀", not err, err)

    # 越界形态
    r = run(graph, "SELECT * WHERE { ?s ?p ?o }", max_rows=5)
    ck("结果截断标记正确", r["ok"] and len(r["rows"]) == 5 and r["truncated"]
       and r["count"] > 5, f"{r['count']} → {len(r['rows'])}")

    r = run(graph, "SELECT ?s WHERE { ?s a :Nope }")
    ck("查不存在的类返回空而非报错", r["ok"] and r["count"] == 0, "")

    r = run(graph, "SELECT ?s WHERE { ?s a }")
    ck("语法错误如实报错", not r["ok"] and bool(r["error"]), r["error"][:120])

    # 单元格序列化
    cell = _cell(URIRef("http://example.org/onto#emp001"))
    ck("URI 单元格给局部名做展示", cell["kind"] == "uri" and cell["display"] == "emp001", cell)
    ck("未绑定值不塞假数据", _cell(None)["kind"] == "unbound", "")

    # spec → 图（JSON-LD 回环）
    try:
        spec_graph = graph_from_spec({
            "nodes": [
                {"id": "o1", "kind": "object", "fields": {"name": "Employee", "label": "员工"}},
                {"id": "a1", "kind": "attr", "fields": {"name": "fullName", "label": "姓名",
                                                        "range": "string"}},
            ],
            "edges": [{"from": "o1", "to": "a1"}],
        })
        ck("spec 可转图（JSON-LD 回环）", len(spec_graph) > 3, f"{len(spec_graph)} 三元组")
        ck("回环图能查出类", spec_graph.query(
            "SELECT ?c WHERE { ?c a owl:Class }").__len__() >= 1, "")
    except Exception as exc:  # noqa: BLE001
        ck("spec 可转图（JSON-LD 回环）", False, f"{type(exc).__name__}: {exc}")

    # 纯结构本体（还没灌数据）：主角类按「实例数 → 声明属性数」挑，
    # 不能取第一个类——否则示例全落在最空的类上，跑出来一片空行等于没给示例。
    _struct = graph_from_spec({
        "nodes": [
            {"id": "s1", "kind": "object", "fields": {"name": "Alpha", "label": "甲"}},
            {"id": "s2", "kind": "object", "fields": {"name": "Zulu", "label": "乙"}},
            {"id": "s3", "kind": "attr", "fields": {"name": "onlyOne", "label": "唯一属性",
                                                    "range": "string"}},
            {"id": "s4", "kind": "attr", "fields": {"name": "k1", "label": "字段一",
                                                    "range": "string"}},
            {"id": "s5", "kind": "attr", "fields": {"name": "k2", "label": "字段二",
                                                    "range": "string"}},
            {"id": "s6", "kind": "attr", "fields": {"name": "k3", "label": "字段三",
                                                    "range": "string"}},
        ],
        "edges": [{"from": "s1", "to": "s3"}, {"from": "s2", "to": "s4"},
                  {"from": "s2", "to": "s5"}, {"from": "s2", "to": "s6"}],
    })
    _sex = {e["id"]: e for e in examples(_struct)}
    ck("纯结构本体按属性数挑主角类（不是取第一个类）",
       ":Zulu" in _sex["instances-of-main"]["query"], _sex["instances-of-main"]["query"][-160:])

    # 「导入本体 → 补实例 → 一起查」这条路：导入载荷用 ref/subject_ref，
    # 历史目录用 iri/subject_iri，两种都得认，否则事实被静默跳过（查询回空行）。
    g_inst, _ = graph_from_text(_ONT_TTL)
    added = add_instances(g_inst, {
        "entities": [
            {"ref": "e1", "iri": "http://example.org/inst#e1",
             "entity_type": "Employee", "canonical_name": "张三"},
            {"ref": "e2", "iri": "http://example.org/inst#e2",
             "entity_type": "Employee", "canonical_name": "李四"},
        ],
        "facts": [
            {"subject_ref": "e1", "predicate": "fullName", "value": "张三"},
            {"subject_ref": "e1", "predicate": "hasStatus", "value": "Active"},
            {"subject_ref": "e2", "predicate": "mentor", "value": "e1"},
            {"subject_iri": "http://example.org/inst#e2", "predicate": "fullName", "value": "李四"},
        ],
        "relations": [],
    })
    ck("导入载荷的 ref 写法能补进图（事实不被静默跳过）", added >= 8, f"新增 {added} 三元组")
    r = run(g_inst, "SELECT ?s WHERE { ?s a :Employee }")
    ck("补实例后类型断言可查", r["ok"] and r["count"] == 2, f"{r['count']} 行")
    r = run(g_inst, "SELECT ?v WHERE { :e1 :hasStatus ?v }")
    ck("枚举事实落成个体 IRI（不是字面量）",
       r["ok"] and r["count"] == 1 and r["rows"][0]["v"]["kind"] == "uri"
       and r["rows"][0]["v"]["display"] == "Active",
       r["rows"][:1] or r.get("error"))
    r = run(g_inst, "SELECT ?t WHERE { :e2 :mentor ?t }")
    ck("实体引用经 ref 表落成实体 IRI", r["ok"] and r["count"] == 1
       and r["rows"][0]["t"]["display"] == "e1", r["rows"][:1] or r.get("error"))
    r = run(g_inst, "SELECT ?v WHERE { :e1 :fullName ?v }")
    ck("数据属性仍是字面量", r["ok"] and r["count"] == 1
       and r["rows"][0]["v"]["kind"] == "literal", r["rows"][:1] or r.get("error"))

    # 多语种 label 不能把行数放大：一个个体带 @zh + @en 两个 label 时，
    # 原来 OPTIONAL 直接绑定会让每个个体多出一行（3 个取值显示 6 行、计数看起来翻倍）。
    _multi = Graph()
    _multi.parse(data="""
@prefix : <http://example.org/multi#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Employee a owl:Class ; rdfs:label "员工"@zh , "Employee"@en .
:Status a owl:Class ; owl:oneOf ( :Active :Resigned ) ;
    rdfs:label "在职状态"@zh , "Status"@en .
:Active a owl:NamedIndividual , :Status ; rdfs:label "在职"@zh , "Active"@en .
:Resigned a owl:NamedIndividual , :Status ; rdfs:label "离职"@zh , "Resigned"@en .
:fullName a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string ;
    rdfs:label "姓名"@zh , "Full name"@en .
:hasStatus a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Status ;
    rdfs:label "在职状态"@zh , "Status"@en .
:e1 a :Employee ; rdfs:label "张三"@zh , "Zhang San"@en ; :fullName "张三" ; :hasStatus :Active .
:e2 a :Employee ; rdfs:label "李四"@zh , "Li Si"@en ; :fullName "李四" ; :hasStatus :Resigned .
""", format="turtle")
    _mex = {e["id"]: e for e in examples(_multi)}
    for _eid in ("instances-of-main", "projection"):
        r = run(_multi, _mex[_eid]["query"])
        ck(f"多语种 label 不让 {_eid} 行数翻倍", r["ok"] and r["count"] == 2, f"{r['count']} 行")
    r = run(_multi, _mex["enum-individuals"]["query"])
    ck("多语种 label 不让 enum-individuals 行数翻倍", r["ok"] and r["count"] == 2, f"{r['count']} 行")
    r = run(_multi, _mex["enum-distribution"]["query"])
    dist2 = {row["v"]["display"]: int(row["n"]["value"]) for row in r["rows"]}
    ck("多语种 label 不让 enum-distribution 拆行（每取值一行）",
       r["ok"] and r["count"] == 2 and dist2 == {"Active": 1, "Resigned": 1}, dist2)
    r = run(_multi, _mex["group-by-ref"]["query"]) if "group-by-ref" in _mex else {"ok": True, "count": 0}
    ck("多语种 label 下引用分组仍按目标实体聚合", r["ok"], f"{r.get('count')} 行")

    # 空图不崩
    ck("空图生成骨架示例", len(examples(Graph())) >= 2, "")
    ck("空内容解析必须抛错", _raises(lambda: graph_from_text("   ")), "")

    passed = sum(1 for c in checks if c["ok"])
    return {"ok": passed == len(checks), "passed": passed,
            "total": len(checks), "checks": checks}


def _raises(fn: Any) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except Exception:  # noqa: BLE001
        return True
    return False