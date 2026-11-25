"""本体文件 → 设计模型。

只做「读三元组」，不做 OWL 推理：类 / 属性 / 约束都按显式声明读取。
推理才能得到的信息（例如 ABox 里实际用到但没写 domain 的谓词）单独放在
`observed` / `inferred` 字段里，让界面能区分「声明过」和「观察到」，
而不是假装本体已经推过一遍。
"""
from __future__ import annotations

import re
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

_FORMATS = {
    ".ttl": "turtle", ".turtle": "turtle", ".n3": "n3",
    ".nt": "nt", ".ntriples": "nt", ".trig": "trig", ".nq": "nquads",
    ".rdf": "xml", ".owl": "xml", ".xml": "xml",
    ".jsonld": "json-ld", ".json": "json-ld", ".json-ld": "json-ld",
}
_TRY_ORDER = ("turtle", "xml", "json-ld", "nt", "n3")

# rdflib 给「没有名字的命名空间」自动生成的前缀，形如 ns1 / ns2 —— 留着没法看
_ANON_PREFIX = re.compile(r"ns\d+")

_LABEL_PREDS = (RDFS.label, SKOS.prefLabel, URIRef("http://purl.org/dc/terms/title"))
_COMMENT_PREDS = (RDFS.comment, SKOS.definition, URIRef("http://purl.org/dc/terms/description"))

_OWL_THING = OWL.Thing


def suffix_of(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def parse_rdf(filename: str, data: bytes) -> tuple[Graph, str]:
    """按扩展名猜格式解析；猜错就按常见格式依次重试。

    返回 (graph, 实际使用的格式)。失败抛 ValueError，消息里带上试过的格式，
    避免只给一句「解析失败」让人猜。
    """
    if not data:
        raise ValueError("文件是空的")
    ext = "." + suffix_of(filename)
    guess = _FORMATS.get(ext)
    order = ([guess] if guess else []) + [f for f in _TRY_ORDER if f != guess]
    errors: list[str] = []
    for fmt in order:
        g = Graph()
        try:
            g.parse(data=data, format=fmt)
        except Exception as exc:  # noqa: BLE001  解析器异常类型五花八门，统一收集
            errors.append(f"{fmt}: {str(exc).splitlines()[0][:120]}")
            continue
        if len(g):
            return g, fmt
        # 解析成功但零三元组：多半是格式碰巧「能读」其实读错了（如把 xml 当 turtle），
        # 继续试下一个；全部试完仍为空则按空本体返回。
    if errors and all(not e.startswith("turtle") for e in errors):
        raise ValueError("无法解析该文件，尝试过的格式：" + "；".join(errors))
    if errors:
        raise ValueError("无法解析该文件，尝试过的格式：" + "；".join(errors))
    return Graph(), guess or "turtle"


def _uri_heads(uris) -> set[str]:
    """把一批 URI 切成「可能的命名空间」，用于判断某个前缀到底用没用上。

    只按最后一个 # 和最后一个 / 切两刀，够覆盖 99% 的词表命名习惯，
    比拿每个 URI 去和三十来个前缀做 startswith 便宜得多。
    """
    heads: set[str] = set()
    for u in uris:
        i = u.rfind("#")
        if i > 0:
            heads.add(u[: i + 1])
        j = u.rfind("/")
        if j > 0:
            heads.add(u[: j + 1])
    return heads


def _synth_prefix(ns: str, taken: set[str]) -> str:
    """给匿名前缀（空串 / ns1 这类）的命名空间起个能看的名字。

    rdflib 的 NamespaceManager 是「一个命名空间只留一个前缀」，
    所以本体里同时写了 `@prefix emp:` 和 `@prefix :` 时，先绑的那个会被顶掉；
    落盘再读回来时还会被改写成 `ns1`。界面上就成了 `:Employee` / `ns1:Employee`，
    多个命名空间混在一起时根本分不清谁是谁。
    这里从 URI 末段反推一个短名（.../employee# → employee），并避开重名。
    """
    core = ns.rstrip("#/").rsplit("/", 1)[-1]
    core = re.sub(r"[^A-Za-z0-9_\-]", "", core).strip("-_").lower()
    if not core or not re.match(r"[a-z]", core):
        return ""
    name = core[:12]
    base, i = name, 2
    while name in taken:
        name = f"{base[:10]}{i}"
        i += 1
    return name


class _Names:
    """命名空间 → CURIE。取「最长匹配」而不是第一个匹配，
    否则 http://example.org/ 会把 http://example.org/ontology/ 抢走。

    rdflib 解析时会自动绑上 brick / csvw / dcat 等三十来个通用词表前缀。
    这些跟用户的图谱毫无关系，全写进提示词只会稀释上下文、全塞给前端会
    糊满一屏，所以这里只保留「图里真的出现过」的命名空间。
    """

    def __init__(self, graph: Graph):
        pairs: dict[str, str] = {}
        for prefix, ns in graph.namespaces():
            pairs[str(prefix)] = str(ns)
        for prefix, ns in (("owl", str(OWL)), ("rdf", str(RDF)), ("rdfs", str(RDFS)),
                           ("xsd", str(XSD)), ("skos", str(SKOS))):
            pairs.setdefault(prefix, ns)

        uris = set()
        for s, p, o in graph:
            for term in (s, p, o):
                if isinstance(term, URIRef):
                    uris.add(str(term))
        heads = _uri_heads(uris)

        used: set[str] = set()
        probe = sorted(uris)[:3000]
        for ns in set(pairs.values()):
            if ns in heads or (ns and any(u.startswith(ns) for u in probe)):
                used.add(ns)

        # 一个命名空间只留一个前缀。有名字的优先；匿名前缀（空串，或 rdflib
        # 落盘重读后自动生成的 ns1/ns2）单独排队，另起短名。
        by_ns: dict[str, list[str]] = {}
        for prefix, ns in pairs.items():
            if ns in used:
                by_ns.setdefault(ns, []).append(prefix)

        taken = {"owl", "rdf", "rdfs", "xsd", "skos"}
        final: dict[str, str] = {}
        pending: list[tuple[str, list[str]]] = []
        for ns, prefixes in by_ns.items():
            named = sorted(p for p in prefixes if p and not _ANON_PREFIX.fullmatch(p))
            if named:
                final[named[0]] = ns
                taken.add(named[0])
            else:
                pending.append((ns, prefixes))

        for ns, prefixes in pending:
            name = _synth_prefix(ns, taken) or prefixes[0]
            taken.add(name)
            final[name] = ns

        self.pairs = final
        self._sorted = sorted(final.items(), key=lambda kv: len(kv[1]), reverse=True)

    def prefix_block(self, skip: set[str] | None = None) -> str:
        skip = skip or set()
        out = []
        for prefix, ns in sorted(self.pairs.items()):
            if prefix in skip:
                continue
            out.append(f"PREFIX {prefix}: <{ns}>" if prefix else f"PREFIX : <{ns}>")
        return "\n".join(out)

    def curie(self, term) -> str:
        if isinstance(term, BNode):
            return "_:" + str(term)[:12]
        if isinstance(term, Literal):
            return str(term)
        uri = str(term)
        for prefix, ns in self._sorted:
            if uri.startswith(ns):
                local = uri[len(ns):]
                if local and re.fullmatch(r"[A-Za-z_][\w.\-]*", local):
                    return f"{prefix}:{local}" if prefix else f":{local}"
        return uri


def _first_literal(graph: Graph, node, preds) -> str:
    for pred in preds:
        values = [v for v in graph.objects(node, pred) if isinstance(v, Literal)]
        if not values:
            continue
        # 中文优先，其次英文，最后无语言标签
        for lang in ("zh", "zh-cn", "zh-hans"):
            for v in values:
                if (v.language or "").lower() == lang:
                    return str(v)
        for v in values:
            if (v.language or "").lower().startswith("en"):
                return str(v)
        return str(values[0])
    return ""


def _label(graph: Graph, node) -> str:
    return _first_literal(graph, node, _LABEL_PREDS)


def _comment(graph: Graph, node) -> str:
    return _first_literal(graph, node, _COMMENT_PREDS)


def _is_true(graph: Graph, s, p) -> bool:
    return any(str(o).lower() in ("true", "1") for o in graph.objects(s, p))


def _rdf_list(graph: Graph, head) -> list:
    out, seen = [], set()
    while head and head != RDF.nil and head not in seen and len(out) < 200:
        seen.add(head)
        firsts = list(graph.objects(head, RDF.first))
        if not firsts:
            break
        out.append(firsts[0])
        rests = list(graph.objects(head, RDF.rest))
        head = rests[0] if rests else None
    return out


def _depths(classes: list[dict]) -> None:
    """按 subClassOf 求层级深度（最长路径）。成环时停止加深，防止无限循环。"""
    by_uri = {c["uri"]: c for c in classes}
    depth: dict[str, int] = {}

    def walk(uri: str, stack: frozenset) -> int:
        if uri in depth:
            return depth[uri]
        if uri in stack:
            return 0
        node = by_uri.get(uri)
        if not node:
            return 0
        best = 0
        for parent in node["parent_uris"]:
            if parent in by_uri:
                best = max(best, walk(parent, stack | {uri}) + 1)
        depth[uri] = best
        return best

    for c in classes:
        walk(c["uri"], frozenset())
    for c in classes:
        c["depth"] = depth.get(c["uri"], 0)


def build_model(graph: Graph) -> dict:
    names = _Names(graph)
    q = names.curie

    # ---------- 类 ----------
    class_set: set = set()
    for cls_type in (OWL.Class, RDFS.Class):
        class_set |= {s for s in graph.subjects(RDF.type, cls_type) if isinstance(s, URIRef)}
    # 只声明了 subClassOf、没写 rdf:type owl:Class 的文件很常见，补收进来
    for cand in set(graph.subjects(RDFS.subClassOf, None)) | set(graph.objects(None, RDFS.subClassOf)):
        if isinstance(cand, URIRef) and (cand, RDF.type, OWL.Class) in graph:
            class_set.add(cand)

    # ---------- 属性 ----------
    def props_of(kind) -> list:
        return sorted({s for s in graph.subjects(RDF.type, kind) if isinstance(s, URIRef)}, key=str)

    obj_uris = set(props_of(OWL.ObjectProperty))
    data_uris = set(props_of(OWL.DatatypeProperty))
    ann_uris = set(props_of(OWL.AnnotationProperty))

    builtin = {RDF.type, RDFS.label, RDFS.comment, RDFS.subClassOf, RDFS.subPropertyOf,
               RDFS.domain, RDFS.range, OWL.onProperty, RDF.first, RDF.rest,
               OWL.sameAs, OWL.versionInfo, RDFS.seeAlso, OWL.imports,
               OWL.inverseOf, OWL.oneOf, OWL.unionOf, OWL.intersectionOf,
               OWL.hasValue, OWL.onClass, OWL.someValuesFrom, OWL.allValuesFrom,
               OWL.minCardinality, OWL.maxCardinality, OWL.cardinality,
               OWL.qualifiedCardinality, OWL.minQualifiedCardinality,
               OWL.maxQualifiedCardinality, OWL.datatypeComplementOf,
               OWL.disjointWith, OWL.equivalentClass, RDFS.subClassOf,
               OWL.propertyChainAxiom, OWL.onDataRange, SKOS.prefLabel}

    # 数据里实际用到、但既没声明类型也没声明的谓词
    used_preds: dict = {}
    for s, p, o in graph:
        if p in builtin or not isinstance(p, URIRef):
            continue
        used_preds[p] = used_preds.get(p, 0) + 1
    for p in used_preds:
        if p in obj_uris or p in data_uris or p in ann_uris:
            continue
        # 只使用 URI 宾语的谓词视作对象属性候选；有 domain/range 声明更可信
        has_domain = any(True for _ in graph.objects(p, RDFS.domain))
        has_range = any(True for _ in graph.objects(p, RDFS.range))
        uri_objects = any(isinstance(o, URIRef) for o in graph.objects(None, p))
        if has_domain and has_range and uri_objects:
            obj_uris.add(p)
        elif not uri_objects:
            data_uris.add(p)

    def prop_entry(p) -> dict:
        domains = [d for d in graph.objects(p, RDFS.domain) if isinstance(d, URIRef)]
        ranges = [r for r in graph.objects(p, RDFS.range) if isinstance(r, URIRef)]
        one_of = []
        for r in ranges:
            if (r, RDF.type, OWL.Class) in graph:
                for head in graph.objects(r, OWL.oneOf):
                    one_of = [q(v) for v in _rdf_list(graph, head)]
        return {
            "uri": str(p), "curie": q(p),
            "label": _label(graph, p) or q(p).split(":")[-1],
            "comment": _comment(graph, p),
            "domain": [q(d) for d in domains], "domain_uris": [str(d) for d in domains],
            "range": [q(r) for r in ranges], "range_uris": [str(r) for r in ranges],
            "range_label": (q(ranges[0]) if ranges else ""),
            "one_of": one_of,
            "functional": (p, RDF.type, OWL.FunctionalProperty) in graph,
            "inverse_of": next((q(o) for o in graph.objects(p, OWL.inverseOf)), ""),
            "transitive": (p, RDF.type, OWL.TransitiveProperty) in graph,
            # 不自反（irreflexive）：本体用它声明「自己不是自己的主管」。这个标记必须透出，
            # 否则问答侧只能凭「有没有自环边」猜，而自环本来就在落图时被剔掉了——
            # 「为什么没有 X 是 X 的主管」就答不出本体依据。
            "irreflexive": (p, RDF.type, OWL.IrreflexiveProperty) in graph,
            "symmetric": (p, RDF.type, OWL.SymmetricProperty) in graph,
            "sub_property_of": next((q(o) for o in graph.objects(p, RDFS.subPropertyOf)), ""),
            "declared": (p, RDF.type, OWL.ObjectProperty) in graph
                        or (p, RDF.type, OWL.DatatypeProperty) in graph,
            "uses": used_preds.get(p, 0),
        }

    obj_props = [prop_entry(p) for p in sorted(obj_uris, key=str)]
    data_props = [prop_entry(p) for p in sorted(data_uris, key=str)]
    ann_props = [prop_entry(p) for p in sorted(ann_uris - obj_uris - data_uris, key=str)]

    # ---------- 类条目 ----------
    classes: list[dict] = []
    for cls in sorted(class_set, key=str):
        parents = [p for p in graph.objects(cls, RDFS.subClassOf) if isinstance(p, URIRef)]
        children = [c for c in graph.subjects(RDFS.subClassOf, cls) if isinstance(c, URIRef)]
        one_of = []
        for head in graph.objects(cls, OWL.oneOf):
            one_of = [q(v) for v in _rdf_list(graph, head)]
        restrictions = []
        for sup in graph.objects(cls, RDFS.subClassOf):
            if (sup, RDF.type, OWL.Restriction) not in graph:
                continue
            on = next((p for p in graph.objects(sup, OWL.onProperty)), None)
            if on is None:
                continue
            item = {"property": q(on), "on": q(on)}
            for key, label in ((OWL.someValuesFrom, "some"), (OWL.allValuesFrom, "only"),
                               (OWL.hasValue, "value")):
                vals = [q(v) for v in graph.objects(sup, key)]
                if vals:
                    item[label] = vals[0]
            for key, label in ((OWL.minCardinality, "min"), (OWL.maxCardinality, "max"),
                               (OWL.cardinality, "exactly"),
                               (OWL.minQualifiedCardinality, "min"),
                               (OWL.maxQualifiedCardinality, "max"),
                               (OWL.qualifiedCardinality, "exactly")):
                for v in graph.objects(sup, key):
                    item[label] = str(v)
            if len(item) > 2:
                restrictions.append(item)

        members = [s for s in graph.subjects(RDF.type, cls) if isinstance(s, URIRef)]
        dprops = [p for p in data_props if str(cls) in p["domain_uris"]]
        oprops_out = [p for p in obj_props if str(cls) in p["domain_uris"]]
        oprops_in = [p for p in obj_props if str(cls) in p["range_uris"]]
        classes.append({
            "uri": str(cls), "curie": q(cls),
            "label": _label(graph, cls) or q(cls).split(":")[-1],
            "comment": _comment(graph, cls),
            "parent_uris": [str(p) for p in parents],
            "parents": [q(p) for p in parents],
            "children": [q(c) for c in children],
            "instances": len(members),
            "one_of": one_of,
            "restrictions": restrictions,
            "data_props": [{"curie": p["curie"], "label": p["label"], "range": p["range_label"],
                            "uses": p["uses"]} for p in dprops],
            "obj_out": [{"curie": p["curie"], "label": p["label"],
                         "range": p["range"][0] if p["range"] else ""} for p in oprops_out],
            "obj_in": [{"curie": p["curie"], "label": p["label"],
                        "domain": p["domain"][0] if p["domain"] else ""} for p in oprops_in],
        })

    _depths(classes)
    class_by_uri = {c["uri"]: c for c in classes}

    # ABox 观察到的谓词：补进类面板（真实文件常常不写 domain）
    observed: dict[str, dict[str, int]] = {}
    for cls in class_set:
        for inst in graph.subjects(RDF.type, cls):
            if not isinstance(inst, URIRef):
                continue
            for p in graph.predicates(inst, None):
                if p in builtin or not isinstance(p, URIRef):
                    continue
                bucket = observed.setdefault(str(cls), {})
                bucket[str(p)] = bucket.get(str(p), 0) + 1
    for uri, bucket in observed.items():
        entry = class_by_uri.get(uri)
        if not entry:
            continue
        declared = {d["curie"] for d in entry["data_props"]} | {o["curie"] for o in entry["obj_out"]}
        extra = []
        for p, n in sorted(bucket.items(), key=lambda kv: -kv[1]):
            curie = q(URIRef(p))
            if curie in declared:
                continue
            extra.append({"curie": curie, "label": _label(graph, URIRef(p)) or curie.split(":")[-1],
                          "uses": n})
        entry["observed_props"] = extra[:12]

    # ---------- 个体 ----------
    individuals, individuals_total = [], 0
    seen_inst: set = set()
    for cls in sorted(class_set, key=str):
        for inst in graph.subjects(RDF.type, cls):
            if not isinstance(inst, URIRef) or inst in seen_inst:
                continue
            if inst in class_set or inst in obj_uris or inst in data_uris:
                continue
            seen_inst.add(inst)
            individuals_total += 1
            if len(individuals) >= 200:
                continue
            types = [q(t) for t in graph.objects(inst, RDF.type) if isinstance(t, URIRef)
                     and t in class_set]
            attrs, n_triples = [], 0
            for p, o in graph.predicate_objects(inst):
                n_triples += 1
                if isinstance(o, Literal) and len(attrs) < 8:
                    attrs.append({"p": q(p), "v": str(o)[:80]})
                elif isinstance(o, URIRef) and len(attrs) < 8:
                    label = _label(graph, o)
                    attrs.append({"p": q(p), "v": label or q(o), "link": q(o)})
            individuals.append({
                "uri": str(inst), "curie": q(inst),
                "label": _label(graph, inst) or q(inst).split(":")[-1],
                "classes": types, "attrs": attrs, "triples": n_triples,
            })

    # ---------- 图边 ----------
    edges = []
    for p in obj_props:
        for d in p["domain"]:
            for r in p["range"]:
                edges.append({"source": d, "target": r, "label": p["label"],
                              "curie": p["curie"], "kind": "object"})
    for c in classes:
        for parent in c["parents"]:
            edges.append({"source": c["curie"], "target": parent, "label": "subClassOf",
                          "curie": "rdfs:subClassOf", "kind": "subclass"})

    onto = next((s for s in graph.subjects(RDF.type, OWL.Ontology) if isinstance(s, URIRef)), None)
    meta = {
        "uri": str(onto) if onto else "",
        "label": (_label(graph, onto) if onto else "") or "",
        "comment": (_comment(graph, onto) if onto else "") or "",
        "version": (next((str(v) for v in graph.objects(onto, OWL.versionInfo)), "") if onto else ""),
    }

    prefixes = {p: ns for p, ns in names.pairs.items()}
    return {
        "ontology": meta,
        "prefixes": prefixes,
        "stats": {
            "triples": len(graph),
            "classes": len(classes),
            "object_props": len(obj_props),
            "data_props": len(data_props),
            "annotation_props": len(ann_props),
            "individuals": individuals_total,
            "edges": len(edges),
        },
        "classes": classes,
        "object_props": obj_props,
        "data_props": data_props,
        "annotation_props": ann_props,
        "individuals": individuals,
        "individuals_total": individuals_total,
        "edges": edges,
        "truncated": {
            "classes": len(classes) > 400,
            "individuals": individuals_total > len(individuals),
        },
    }


def schema_brief(model: dict, max_classes: int = 60, max_props: int = 140) -> str:
    """给 LLM 的本体摘要：CURIE + 中文名 + 关键属性。刻意用纯文本，
    比塞整份 JSON 省 token，也比只给类名少猜。"""
    lines = []
    meta = model.get("ontology") or {}
    if meta.get("label") or meta.get("uri"):
        lines.append(f"本体：{meta.get('label') or ''} {meta.get('uri') or ''}".strip())
    lines.append("前缀：" + ", ".join(f"{p}:={ns}" for p, ns in list(model["prefixes"].items())[:20]))
    lines.append("\n类（curie | 名称 | 实例数 | 数据属性 / 对象属性）：")
    for c in model["classes"][:max_classes]:
        dps = ", ".join(f"{d['curie']}({d['label']})" for d in c["data_props"][:10])
        ops = ", ".join(f"{o['curie']}→{o['range']}" for o in c["obj_out"][:10])
        lines.append(f"- {c['curie']} | {c['label']} | {c['instances']} | {dps or '-'} | {ops or '-'}")
    if len(model["classes"]) > max_classes:
        lines.append(f"（其余 {len(model['classes']) - max_classes} 个类省略）")
    obj = model["object_props"][:max_props]
    if obj:
        lines.append("\n对象属性（curie | 名称 | domain → range）：")
        for p in obj:
            lines.append(f"- {p['curie']} | {p['label']} | {'/'.join(p['domain']) or '?'} → "
                         f"{'/'.join(p['range']) or '?'}")
    return "\n".join(lines)
