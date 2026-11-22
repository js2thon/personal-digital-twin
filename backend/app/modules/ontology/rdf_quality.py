"""RDF/OWL 本体静态质量分析。

面向导入前/发布前的 TBox 质量门禁。纯函数，不访问数据库，不执行推理。
与 quality.py（已落库实体关系质量）和 shacl.py（事实写入约束）分层：
本模块专门检查 RDF 本体结构、语义引用和文档化程度。
"""
from __future__ import annotations

from collections import defaultdict
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD, SKOS

_LABELS = (RDFS.label, SKOS.prefLabel)
_BUILTIN_CLASS = {OWL.Thing, OWL.Nothing}
_XSD_DATATYPES = {
    str(getattr(XSD, name)) for name in (
        "string", "normalizedString", "token", "boolean", "decimal", "integer",
        "nonNegativeInteger", "positiveInteger", "long", "int", "short", "byte",
        "unsignedLong", "unsignedInt", "unsignedShort", "unsignedByte",
        "double", "float", "date", "time", "dateTime", "dateTimeStamp",
        "gYear", "gMonth", "gDay", "anyURI", "duration"
    )
}


def _local(uri) -> str:
    value = str(uri)
    return value.rsplit("#", 1)[-1].rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _has_label(graph: Graph, node) -> bool:
    return any(str(value).strip() for pred in _LABELS for value in graph.objects(node, pred))


def _issue(code: str, severity: str, message: str, focus: str = "", path: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message,
            "focus": focus, "path": path}


def _cycle_nodes(edges: dict[str, set[str]]) -> list[list[str]]:
    """返回继承图中的环路，限制为 URI 节点并防止重复报告。"""
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for parent in sorted(edges.get(node, set())):
            if state.get(parent, 0) == 0:
                visit(parent)
            elif state.get(parent) == 1:
                idx = stack.index(parent)
                cycle = stack[idx:] + [parent]
                # 环路旋转归一，避免 A-B-A 与 B-A-B 重复
                body = cycle[:-1]
                if body:
                    rotations = [tuple(body[i:] + body[:i]) for i in range(len(body))]
                    key = min(rotations)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(edges):
        if state.get(node, 0) == 0:
            visit(node)
    return cycles


def analyze(graph: Graph) -> dict:
    """分析一个已解析 RDF Graph，返回可供 UI 展示的质量报告。"""
    if not isinstance(graph, Graph):
        raise TypeError("graph 必须是 rdflib.Graph")

    issues: list[dict] = []
    triples = len(graph)
    classes = {
        s for kind in (OWL.Class, RDFS.Class)
        for s in graph.subjects(RDF.type, kind)
        if isinstance(s, URIRef) and s not in _BUILTIN_CLASS
    }
    object_props = {
        s for s in graph.subjects(RDF.type, OWL.ObjectProperty) if isinstance(s, URIRef)
    }
    datatype_props = {
        s for s in graph.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(s, URIRef)
    }
    annotation_props = {
        s for s in graph.subjects(RDF.type, OWL.AnnotationProperty) if isinstance(s, URIRef)
    }
    properties = object_props | datatype_props | annotation_props

    if triples == 0:
        issues.append(_issue("EMPTY_GRAPH", "error", "本体图为空，未解析到任何三元组"))
    if not classes:
        issues.append(_issue("NO_CLASSES", "error", "未发现显式声明的 OWL/RDFS 类"))

    ontology_nodes = set(graph.subjects(RDF.type, OWL.Ontology))
    if not ontology_nodes:
        issues.append(_issue("ONTOLOGY_HEADER_MISSING", "warning",
                             "缺少 owl:Ontology 元数据声明（版本、标题和命名空间治理将较困难）"))

    # 类/属性文档化质量
    for node in sorted(classes | properties, key=str):
        if not _has_label(graph, node):
            kind = "类" if node in classes else "属性"
            issues.append(_issue("LABEL_MISSING", "warning",
                                 f"{kind} {_local(node)} 缺少 rdfs:label / skos:prefLabel",
                                 str(node), "rdfs:label"))

    # 属性 domain/range 引用与完整性
    for prop in sorted(object_props, key=str):
        domains = list(graph.objects(prop, RDFS.domain))
        ranges = list(graph.objects(prop, RDFS.range))
        if not domains:
            issues.append(_issue("OBJECT_PROPERTY_DOMAIN_MISSING", "warning",
                                 f"对象属性 {_local(prop)} 未声明 domain", str(prop), "rdfs:domain"))
        if not ranges:
            issues.append(_issue("OBJECT_PROPERTY_RANGE_MISSING", "warning",
                                 f"对象属性 {_local(prop)} 未声明 range", str(prop), "rdfs:range"))
        for node in domains + ranges:
            if isinstance(node, URIRef) and node not in classes and node not in _BUILTIN_CLASS:
                issues.append(_issue("PROPERTY_CLASS_REFERENCE_UNKNOWN", "error",
                                     f"属性 {_local(prop)} 引用了未声明的类 {_local(node)}",
                                     str(prop), "rdfs:domain/range"))

    for prop in sorted(datatype_props, key=str):
        domains = list(graph.objects(prop, RDFS.domain))
        ranges = list(graph.objects(prop, RDFS.range))
        if not domains:
            issues.append(_issue("DATATYPE_PROPERTY_DOMAIN_MISSING", "warning",
                                 f"数据属性 {_local(prop)} 未声明 domain", str(prop), "rdfs:domain"))
        if not ranges:
            issues.append(_issue("DATATYPE_PROPERTY_RANGE_MISSING", "warning",
                                 f"数据属性 {_local(prop)} 未声明 range", str(prop), "rdfs:range"))
        for rng in ranges:
            if isinstance(rng, URIRef) and str(rng).startswith(str(XSD)) and str(rng) not in _XSD_DATATYPES:
                issues.append(_issue("DATATYPE_RANGE_UNKNOWN", "warning",
                                     f"数据属性 {_local(prop)} 使用未识别的 XSD 值域 {_local(rng)}",
                                     str(prop), "rdfs:range"))
            elif isinstance(rng, BNode):
                # 匿名 datatype expression 合法，但当前静态检查器不展开其表达式
                issues.append(_issue("DATATYPE_RANGE_COMPLEX", "info",
                                     f"数据属性 {_local(prop)} 使用匿名值域表达式，需进一步展开检查",
                                     str(prop), "rdfs:range"))

    # 子类关系：未知父类与继承环
    inheritance: dict[str, set[str]] = defaultdict(set)
    for child, parent in graph.subject_objects(RDFS.subClassOf):
        if not isinstance(child, URIRef):
            continue
        if isinstance(parent, URIRef):
            inheritance[str(child)].add(str(parent))
            if parent not in classes and parent not in _BUILTIN_CLASS:
                issues.append(_issue("PARENT_CLASS_UNKNOWN", "warning",
                                     f"子类 {_local(child)} 的父类 {_local(parent)} 未显式声明",
                                     str(child), "rdfs:subClassOf"))
    cycles = _cycle_nodes(inheritance)
    for cycle in cycles:
        issues.append(_issue("SUBCLASS_CYCLE", "error",
                             "检测到循环继承：" + " -> ".join(_local(x) for x in cycle),
                             cycle[0], "rdfs:subClassOf"))

    # 同一局部名跨命名空间冲突，常导致 CURIE/映射歧义
    by_local: dict[str, list[str]] = defaultdict(list)
    for node in classes | properties:
        by_local[_local(node)].append(str(node))
    for name, uris in sorted(by_local.items()):
        unique = sorted(set(uris))
        if len(unique) > 1:
            issues.append(_issue("LOCAL_NAME_COLLISION", "warning",
                                 f"局部名 {name} 对应多个 URI，映射时必须使用完整 IRI",
                                 name, "URI"))

    # 类结构完整性：没有任何属性 domain 指向该类
    for cls in sorted(classes, key=str):
        declared = [p for p in properties
                    if (p, RDFS.domain, cls) in graph]
        if not declared:
            issues.append(_issue("CLASS_WITHOUT_PROPERTIES", "info",
                                 f"类 {_local(cls)} 未关联任何已声明属性",
                                 str(cls), "rdfs:domain"))

    errors = sum(1 for x in issues if x["severity"] == "error")
    warnings = sum(1 for x in issues if x["severity"] == "warning")
    infos = sum(1 for x in issues if x["severity"] == "info")
    score = max(0, 100 - errors * 20 - warnings * 5 - infos)
    verdict = "block" if errors else ("review" if warnings else "pass")
    return {
        "summary": {
            "triples": triples, "classes": len(classes),
            "object_properties": len(object_props),
            "datatype_properties": len(datatype_props),
            "annotation_properties": len(annotation_props),
            "errors": errors, "warnings": warnings, "infos": infos,
            "score": score, "verdict": verdict,
        },
        "issues": issues,
        "checks": {
            "ontology_header": bool(ontology_nodes),
            "has_classes": bool(classes),
            "inheritance_cycles": len(cycles),
            "documented_terms": sum(1 for n in classes | properties if _has_label(graph, n)),
            "terms_total": len(classes | properties),
        },
        "limitations": [
            "本报告是确定性静态检查，不执行 OWL 推理。",
            "复杂匿名类表达式、属性链、跨文件 owl:imports 未展开。",
            "数据覆盖率需在业务数据映射/采样执行后单独计算。",
        ],
    }


def selftest() -> dict:
    """内置最小反例/正例，保证核心规则可触发。"""
    from rdflib import Namespace
    EX = Namespace("https://example.org/test#")
    good = Graph()
    good.add((EX.Ontology, RDF.type, OWL.Ontology))
    good.add((EX.Person, RDF.type, OWL.Class))
    good.add((EX.Person, RDFS.label, __import__("rdflib").Literal("人员")))
    good.add((EX.name, RDF.type, OWL.DatatypeProperty))
    good.add((EX.name, RDFS.domain, EX.Person))
    good.add((EX.name, RDFS.range, XSD.string))
    good.add((EX.name, RDFS.label, __import__("rdflib").Literal("姓名")))
    bad = Graph()
    bad.add((EX.A, RDF.type, OWL.Class))
    bad.add((EX.B, RDF.type, OWL.Class))
    bad.add((EX.A, RDFS.subClassOf, EX.B))
    bad.add((EX.B, RDFS.subClassOf, EX.A))
    bad.add((EX.knows, RDF.type, OWL.ObjectProperty))
    good_report, bad_report = analyze(good), analyze(bad)
    good_ok = good_report["summary"]["errors"] == 0
    bad_codes = {x["code"] for x in bad_report["issues"]}
    bad_ok = {"SUBCLASS_CYCLE", "OBJECT_PROPERTY_DOMAIN_MISSING",
              "OBJECT_PROPERTY_RANGE_MISSING"} <= bad_codes
    return {"passed": good_ok and bad_ok,
            "checks": [{"name": "合格本体无结构错误", "passed": good_ok},
                       {"name": "循环继承和缺失 domain/range 可检出", "passed": bad_ok}],
            "good": good_report["summary"], "bad": bad_report["summary"]}
