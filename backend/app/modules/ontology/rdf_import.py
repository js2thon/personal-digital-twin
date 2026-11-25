"""RDF / OWL 本体文件导入：本体 → 建模画布 5 段 spec + 实例数据载荷。

设计口径（与项目「单一真源、缺口照实披露」约定一致）：

- 只做**确定性映射**，不做推断性补全：
    ``owl:Class``                     → 业务对象节点（object）
    ``owl:DatatypeProperty``+domain   → 该对象下的属性节点（attr）
    ``owl:ObjectProperty``+range 为枚举类 → 每个枚举个体一个属性节点
                                        （带 value_meaning / fact_predicate / fact_value，
                                          正是问数 meanings 契约要的三件套）
    ``owl:ObjectProperty``+range 为实体类 → 引用属性节点（画布已无关系段，关系语义
                                        记在 desc / target_entity，不伪造关系节点）
    ``owl:oneOf``                     → 取值域（写进属性的 enum 与枚举值语义）
    ``owl:hasKey``                    → 对象主键 primary_key
- 语义映射字段（``fact_entity_type`` / ``fact_predicate`` / ``value_meaning`` /
  ``fact_value``）直接落进节点字段，问数即可**从本体推导**，不需要人工再补一遍；
- 不编造：TTL 里没声明的（主键、函数、约束）不硬造。两处**推断**逐条进 ``warnings``：
  ① 无 ``owl:hasKey`` 时按 ``*ID / *Id / *Code`` 命名约定猜主键；
  ② 每个有主键的类生成一个「实例计数」指标函数（类声明推导，非 TTL 显式函数）。

实例载荷（``instances``）只做「从图里取出来」，不写库；落库由
``/ontology/modelling/import/seed`` 负责（那里才处理 AC-01 权限三件套）。
"""

from __future__ import annotations

import re
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS, XSD

# 文件扩展名 / 用户传的 format → rdflib parser 名
FORMAT_ALIASES: dict[str, str] = {
    "ttl": "turtle", "turtle": "turtle",
    "n3": "n3",
    "nt": "nt", "ntriples": "nt",
    "jsonld": "json-ld", "json-ld": "json-ld", "json": "json-ld",
    "rdf": "xml", "xml": "xml", "owl": "xml", "rdfxml": "xml",
}
# 未指定 format 时的尝试顺序：turtle 优先（N3 是它的超集，反过来会误判）
_AUTO_ORDER = ("turtle", "json-ld", "xml", "nt")

# TTL 里不属于「业务属性」的元谓词，实例抽取时跳过
_META_PREDICATES = {RDF.type, RDFS.label, RDFS.comment, SKOS.prefLabel,
                    SKOS.altLabel, RDFS.seeAlso, OWL.sameAs}

# 「值是 IRI 但不算个体间关系」的谓词局部名：元数据 / 模式级三元组，
# 收成关系会变成 A 的 scheme → B 的 scheme 这类噪声边
_REL_SKIP_LOCAL = {"type", "label", "comment", "seeAlso", "sameAs", "isDefinedBy",
                   "subClassOf", "domain", "range", "equivalentClass", "versionInfo",
                   "priorVersion", "backwardCompatibleWith"}

# XSD 局部名 → 画布标准类型（string/integer/number/boolean/date/datetime）
_XSD_RANGE: dict[str, str] = {
    "string": "string", "normalizedString": "string", "token": "string",
    "anyURI": "string", "language": "string", "NMTOKEN": "string",
    "integer": "integer", "int": "integer", "long": "integer", "short": "integer",
    "byte": "integer", "nonNegativeInteger": "integer", "positiveInteger": "integer",
    "unsignedInt": "integer", "unsignedLong": "integer", "gYear": "integer",
    "decimal": "number", "double": "number", "float": "number",
    "boolean": "boolean",
    "date": "date",
    "dateTime": "datetime", "dateTimeStamp": "datetime",
}

# 主键命名约定（无 owl:hasKey 时的兜底，命中会进 warnings）
_PK_SUFFIX_RE = re.compile(r"(?:ID|Id|_id|Code|code|No|no)$")

# 找不到 name 类属性时，用于挑「显示名」的属性名特征
_NAMEISH_RE = re.compile(r"(?:fullName|name|title)$", re.I)


# ---------------------------------------------------------------- 基础工具
def _s(v: Any) -> str:
    return str(v or "").strip()


def _iri(v: Any) -> str:
    return str(v) if isinstance(v, URIRef) else ""


def _local(v: Any) -> str:
    """IRI 局部名：取最后一段（# 或 / 之后）。非 IRI 原样返回。"""
    s = _s(_iri(v) or v)
    s = s.rstrip("/#")
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s


def _ns_of(v: Any) -> str:
    s = _iri(v)
    for sep in ("#", "/"):
        i = s.rfind(sep)
        if i > 0:
            return s[: i + 1]
    return ""


def _qname(graph: Graph, iri: Any) -> str:
    """外部类的可读写法（foaf:Person）；解析不出前缀就退回局部名。"""
    try:
        q = graph.namespace_manager.normalizeUri(iri)
        return _s(q) or _local(iri)
    except Exception:  # noqa: BLE001
        return _local(iri)


def _texts(graph: Graph, subj: Any, pred: Any) -> tuple[str, str, str]:
    """取某谓词的字面量，按语言分桶：(中文, 英文, 其它)。"""
    zh = en = other = ""
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
    return zh, en, other


def _label(graph: Graph, subj: Any) -> tuple[str, str]:
    """显示名：rdfs:label 优先，其次 skos:prefLabel。返回 (中文, 英文)。"""
    for pred in (RDFS.label, SKOS.prefLabel):
        zh, en, other = _texts(graph, subj, pred)
        if zh or en or other:
            return zh or other, en
    return "", ""


def _comment(graph: Graph, subj: Any) -> str:
    zh, en, other = _texts(graph, subj, RDFS.comment)
    if zh or other or en:
        return zh or other or en
    zh, en, other = _texts(graph, subj, SKOS.definition)
    return zh or other or en


def _one_of(graph: Graph, cls: Any) -> list[Any]:
    """owl:oneOf 枚举成员；没有则返回空表。"""
    for head in graph.objects(cls, OWL.oneOf):
        try:
            return [m for m in Collection(graph, head)]
        except Exception:  # noqa: BLE001
            return []
    return []


def _has_key(graph: Graph, cls: Any) -> str:
    """owl:hasKey 声明的键（取第一个），没有返回空串。"""
    for head in graph.objects(cls, OWL.hasKey):
        try:
            items = [m for m in Collection(graph, head)]
        except Exception:  # noqa: BLE001
            items = []
        if items:
            return _local(items[0])
    return ""


# ---------------------------------------------------------------- 解析
def parse_text(text: str, fmt: str | None = None) -> tuple[Graph, str]:
    """把本体文本解析成 rdflib 图，返回 (图, 实际生效的格式)。解析不出就抛 ValueError。"""
    raw = _s(text)
    if not raw:
        raise ValueError("本体文件内容为空")
    want = _s(fmt).lower().lstrip(".")
    order = (FORMAT_ALIASES[want],) if want in FORMAT_ALIASES else _AUTO_ORDER
    errors: list[str] = []
    for f in order:
        graph = Graph()
        try:
            graph.parse(data=raw, format=f)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{f}: {type(exc).__name__}: {exc}")
            continue
        if len(graph):
            return graph, f
        errors.append(f"{f}: 解析成功但 0 三元组")
    raise ValueError("无法解析本体内容（已尝试 " + "、".join(order) + "）；" + "；".join(errors[:3]))


# ---------------------------------------------------------------- 本体 → 画布 spec
def _class_list(graph: Graph) -> list[Any]:
    found: set[Any] = set()
    for t in (OWL.Class, RDFS.Class):
        for c in graph.subjects(RDF.type, t):
            if isinstance(c, URIRef) and c != OWL.Thing:
                found.add(c)
    return sorted(found, key=_local)


_META_CLASS_NS = (str(OWL), str(RDFS), str(RDF))


def _observed_classes(graph: Graph) -> list[Any]:
    """实例断言里实际用到的类（``?s rdf:type ?c``），排除 OWL/RDFS/RDF 元词表。

    为什么需要：本体与实例常常分两个文件交付（TBox 一个、ABox 一个）。
    只认「**本文件里** ``a owl:Class`` 的类时，单独导入实例文件会抽出 0 个实体——
    和「本来就没数据」完全不可区分（问「有多少条」静默回 0，看不出是漏抽）。
    这里只把实例实际用到的类收进来，不改图、不补声明，保证 ABox 单独导入也能抽取。
    """
    out: set[Any] = set()
    for c in graph.objects(None, RDF.type):
        if not isinstance(c, URIRef):
            continue
        s = str(c)
        if any(s.startswith(ns) for ns in _META_CLASS_NS):
            continue
        out.add(c)
    return sorted(out, key=_local)


def _props_of_type(graph: Graph, kind: Any) -> list[Any]:
    return sorted({p for p in graph.subjects(RDF.type, kind) if isinstance(p, URIRef)}, key=_local)


def _domains(graph: Graph, prop: Any) -> list[Any]:
    return [d for d in graph.objects(prop, RDFS.domain) if isinstance(d, URIRef)]


def _range_of(graph: Graph, prop: Any) -> Any:
    for r in graph.objects(prop, RDFS.range):
        return r
    return None


def _xsd_range(range_iri: Any) -> str:
    if not isinstance(range_iri, URIRef):
        return "string"
    if _ns_of(range_iri) == str(XSD) or "XMLSchema" in _iri(range_iri):
        return _XSD_RANGE.get(_local(range_iri), "string")
    return "string"


def _enum_labels(graph: Graph, members: list[Any]) -> list[tuple[str, str, str]]:
    """枚举成员 → [(局部名, 中文名, 英文名)]。"""
    out: list[tuple[str, str, str]] = []
    for m in members:
        local = _local(m)
        if not local:
            continue
        zh, en = _label(graph, m)
        out.append((local, zh, en))
    return out


def _pk_of(dt_props: list[Any]) -> tuple[str, bool]:
    """命名约定猜主键，返回 (主键局部名, 是否按约定推断)。"""
    for p in dt_props:
        local = _local(p)
        if _PK_SUFFIX_RE.search(local):
            return local, True
    return "", False


def build_spec(graph: Graph, *, source_name: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """本体图 → 画布 5 段 spec。返回 (spec, meta)。

    spec = ``{"version": "1.0", "nodes": [...], "edges": [...]}``，节点坐标留 0，
    由前端 ``modLayoutPipeline()`` 统一排版（布局口径只有前端一处，避免两边打架）。
    """
    warnings: list[str] = []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    classes = _class_list(graph)
    enum_members = {c: _one_of(graph, c) for c in classes}
    enum_classes = {c: m for c, m in enum_members.items() if m}
    obj_classes = [c for c in classes if c not in enum_classes]
    if not obj_classes:
        warnings.append("本体里没有可建模的业务类（owl:Class 只有枚举类或为空），画布将只有枚举语义")

    dt_props = _props_of_type(graph, OWL.DatatypeProperty)
    op_props = _props_of_type(graph, OWL.ObjectProperty)

    # 属性 → 归属类（多 domain 只落第一个，其余进 warnings，不重复建节点）
    def props_for(cls: Any, pool: list[Any]) -> list[Any]:
        picked = []
        for p in pool:
            doms = _domains(graph, p)
            if cls in doms:
                picked.append(p)
                if len(doms) > 1:
                    warnings.append(
                        f"属性「{_local(p)}」声明了 {len(doms)} 个 domain，只落在第一个类"
                        f"「{_local(doms[0])}」下（其余：{'、'.join(_local(d) for d in doms[1:])}）")
        return picked

    orphan_dt = [p for p in dt_props if not _domains(graph, p)]
    orphan_op = [p for p in op_props if not _domains(graph, p)]
    if orphan_dt or orphan_op:
        names = "、".join(_local(p) for p in (orphan_dt + orphan_op))
        warnings.append(f"{len(orphan_dt) + len(orphan_op)} 个属性未声明 rdfs:domain（{names}），"
                        f"无法判定归属对象，未进画布")

    obj_node_id: dict[Any, str] = {}
    pk_attr_id: dict[Any, str] = {}
    pk_name: dict[Any, str] = {}
    n_attr = 0
    n_enum_attr = 0
    n_ref_attr = 0

    for cls in obj_classes:
        local = _local(cls)
        zh, en = _label(graph, cls)
        desc = _comment(graph, cls)
        node_id = f"n_obj_{local}"
        obj_node_id[cls] = node_id

        # 父类：本体内父类进 type_system；外部词表写 qname（导 JSON-LD 时按「本体里没有该类」如实提示）
        parents = [d for d in graph.objects(cls, RDFS.subClassOf)
                   if isinstance(d, URIRef) and d not in (OWL.Thing, cls)]
        inner = [d for d in parents if d in obj_classes]
        outer = [d for d in parents if d not in obj_classes]
        type_system = _local(inner[0]) if inner else (_qname(graph, outer[0]) if outer else "")
        if outer and not inner:
            warnings.append(f"类「{local}」继承的是外部类 {'、'.join(_qname(graph, d) for d in outer)}，"
                            f"画布只建模本体内的类，父类型按原文写进「类型系统」不生成父节点")

        dt_here = props_for(cls, dt_props)
        op_here = props_for(cls, op_props)

        pk_declared = _has_key(graph, cls)
        pk, pk_guessed = (pk_declared, False) if pk_declared else _pk_of(dt_here)
        pk_name[cls] = pk
        if pk_guessed:
            warnings.append(f"类「{local}」没有 owl:hasKey 声明，主键按命名约定推断为 {pk}"
                            f"（可在画布对象详情里改）")
        elif not pk:
            warnings.append(f"类「{local}」既无 owl:hasKey、属性名也不含 ID/Code 等键特征，"
                            f"主键留空（后端默认按 id 处理，会出一条 INFO 提示）")

        nodes.append({
            "id": node_id, "kind": "object", "x": 0, "y": 0,
            "fields": {
                "name": local,
                "label": zh or local,
                "desc": desc,
                "source_object": f"rdf:{_ns_of(cls)}{local}" if _ns_of(cls) else "",
                "aliases": en if en and en != (zh or local) else "",
                "primary_key": pk,
                "attributes": "",
                "type_system": type_system,
                # 问数实体类型：本体的类局部名就是事实库 entity_type 的口径
                "fact_entity_type": local,
            },
        })

        # ---- 数据属性
        for p in dt_here:
            plocal = _local(p)
            pzh, pen = _label(graph, p)
            rng = _range_of(graph, p)
            enum_vals = _enum_labels(graph, enum_members.get(rng, [])) if rng in enum_classes else []
            attr_id = f"n_attr_{local}_{plocal}"
            nodes.append({
                "id": attr_id, "kind": "attr", "x": 0, "y": 0,
                "fields": {
                    "name": plocal,
                    "label": pzh or pen or plocal,
                    "desc": _comment(graph, p),
                    "range": _xsd_range(rng),
                    "required": False,
                    "enum": "、".join(v[1] or v[2] or v[0] for v in enum_vals),
                    "pattern": "",
                    "source_field": plocal,
                    "is_pk": bool(pk and plocal == pk),
                    "value_meaning": pzh or pen or plocal,
                    "fact_predicate": plocal,
                },
            })
            edges.append({"from": node_id, "to": attr_id})
            n_attr += 1
            if pk and plocal == pk:
                pk_attr_id[cls] = attr_id
            if enum_vals:
                warnings.append(f"属性「{plocal}」的取值范围是枚举类「{_local(rng)}」，"
                                f"取值（{'、'.join(v[1] or v[0] for v in enum_vals)}）已写进属性的枚举列")

        # ---- 对象属性
        for p in op_here:
            plocal = _local(p)
            pzh, pen = _label(graph, p)
            rng = _range_of(graph, p)
            members = enum_members.get(rng, [])
            if members:
                # 枚举值域：一个取值一个属性，直接长出问数要的 value_meaning/fact_value 三件套
                for ilocal, izh, ien in _enum_labels(graph, members):
                    attr_id = f"n_attr_{local}_{plocal}_{ilocal}"
                    nodes.append({
                        "id": attr_id, "kind": "attr", "x": 0, "y": 0,
                        "fields": {
                            "name": f"{plocal}_{ilocal}",
                            "label": izh or ien or ilocal,
                            "desc": f"{pzh or plocal} = {izh or ien or ilocal}"
                                    f"（本体枚举 {_local(rng)} 的一个取值）",
                            "range": "string",
                            "required": False,
                            "enum": "",
                            "pattern": "",
                            "source_field": plocal,
                            "is_pk": False,
                            # 过滤类语义三件套：语义 → 谓词 = 取值
                            "value_meaning": izh or ien or ilocal,
                            "fact_predicate": plocal,
                            "fact_value": ilocal,
                            # 枚举原始口径：画布把一个枚举类展开成「一值一属性」，
                            # 导出 JSON-LD 时靠这三个字段还原成 owl:oneOf 枚举类
                            # （否则一个取值就长成一个 DatatypeProperty，枚举语义丢失）
                            "enum_class": _local(rng),
                            "enum_value": ilocal,
                            "enum_predicate_label": pzh or pen or plocal,
                        },
                    })
                    edges.append({"from": node_id, "to": attr_id})
                    n_enum_attr += 1
                continue
            # 实体引用：画布已退休关系段，落成引用属性（目标类记在 target_entity / desc）
            attr_id = f"n_attr_{local}_{plocal}"
            target = _local(rng) if isinstance(rng, URIRef) else ""
            nodes.append({
                "id": attr_id, "kind": "attr", "x": 0, "y": 0,
                "fields": {
                    "name": plocal,
                    "label": pzh or pen or plocal,
                    "desc": (f"本体对象属性 → {target or '未声明 range'}"
                             if target else f"本体对象属性（未声明 rdfs:range）：{pzh or plocal}"),
                    "range": "string",
                    "required": False,
                    "enum": "",
                    "pattern": "",
                    "source_field": plocal,
                    "is_pk": False,
                    "target_entity": target,
                    "value_meaning": pzh or pen or plocal,
                    "fact_predicate": plocal,
                },
            })
            edges.append({"from": node_id, "to": attr_id})
            n_ref_attr += 1
            if not target:
                warnings.append(f"对象属性「{plocal}」未声明 rdfs:range，引用目标留空（不猜）")

    # ---- 每个有主键的类给一个实例计数指标（类声明推导，明确标注来源）
    n_func = 0
    for cls in obj_classes:
        local = _local(cls)
        pk = pk_name.get(cls, "")
        attr_id = pk_attr_id.get(cls, "")
        if not pk or not attr_id:
            continue
        zh, _en = _label(graph, cls)
        fn_id = f"n_fn_count_{local}"
        nodes.append({
            "id": fn_id, "kind": "func", "x": 0, "y": 0,
            "fields": {
                "name": f"count_{local}",
                "label": f"{zh or local}数量",
                "fn_type": "metric",
                "kind": "aggregate",
                "agg": "RowCount",
                "owner_attr": attr_id,
                "inputs": pk,
                "output": f"{local.lower()}_count",
                "logic": f"count(distinct {pk})",
                "desc": f"本体类 {local} 的实例计数：count(distinct {pk})（口径 RowCount / 实例数）；"
                        f"按类声明推导，TTL 未显式声明函数",
                "stateful": False,
                "derived_by_importer": True,
            },
        })
        edges.append({"from": attr_id, "to": fn_id})
        n_func += 1

    spec = {"version": "1.0", "nodes": nodes, "edges": edges}
    meta = {
        "source": source_name,
        "warnings": warnings,
        "counts": {
            "classes": len(classes),
            "object_classes": len(obj_classes),
            "enum_classes": len(enum_classes),
            "enum_values": sum(len(v) for v in enum_members.values()),
            "datatype_properties": len(dt_props),
            "object_properties": len(op_props),
            "attr_nodes": n_attr + n_enum_attr + n_ref_attr,
            "datatype_attrs": n_attr,
            "enum_value_attrs": n_enum_attr,
            "reference_attrs": n_ref_attr,
            "function_nodes": n_func,
            "nodes": len(nodes),
            "edges": len(edges),
        },
        "primary_keys": {_local(c): pk_name.get(c, "") for c in obj_classes},
        "semantic_entity_types": {_local(c): _local(c) for c in obj_classes},
    }
    return spec, meta


# ---------------------------------------------------------------- 实例数据
def _display_name(graph: Graph, ind: Any, dt_props: list[Any]) -> str:
    """实例显示名：rdfs:label → skos:prefLabel → 类里 name 类属性的值 → 局部名。"""
    zh, en = _label(graph, ind)
    if zh or en:
        return zh or en
    for p in dt_props:
        if not _NAMEISH_RE.search(_local(p)):
            continue
        for o in graph.objects(ind, p):
            val = _s(o)
            if val:
                return val
    return _local(ind)


def build_instances(graph: Graph) -> dict[str, Any]:
    """从图里取出实例：实体 / 事实 / 关系三类载荷（只读图，不碰库）。"""
    declared = set(_class_list(graph))
    # 实例文件单独导入时本文档没有 a owl:Class 的声明，靠实例用到的类兜住（见 _observed_classes）
    observed = [c for c in _observed_classes(graph) if c not in declared]
    classes = sorted(declared | set(observed), key=_local)
    enum_members = {c: _one_of(graph, c) for c in classes}
    enum_classes = {c for c, m in enum_members.items() if m}
    obj_classes = [c for c in classes if c not in enum_classes]
    dt_props = _props_of_type(graph, OWL.DatatypeProperty)
    op_props = _props_of_type(graph, OWL.ObjectProperty)
    op_set = set(op_props)
    dt_set = set(dt_props)
    enum_of_prop = {p: _range_of(graph, p) for p in op_props}

    entities: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    skipped: list[str] = []
    by_iri: dict[str, dict[str, Any]] = {}

    for cls in obj_classes:
        local = _local(cls)
        cls_dt = [p for p in dt_props if cls in _domains(graph, p)]
        for ind in set(graph.subjects(RDF.type, cls)):
            if not isinstance(ind, URIRef):
                continue
            ref = _local(ind)
            if not ref:
                continue
            iri_s = _iri(ind)
            # 多类型个体（部门同时是 Department / DepartmentLevel7 / OrganizationalUnit）会在每个类下
            # 各被走一遍：不按 IRI 去重就会同一个人出三条记录，落库变成重复行。
            # 保留全部类型到 types，entity_type 仍取先遇到的（类名序），契约不变。
            dup = by_iri.get(iri_s)
            if dup is not None:
                if local and local not in dup["types"]:
                    dup["types"].append(local)
                continue
            zh, en = _label(graph, ind)
            ent = {
                "iri": iri_s,
                "ref": ref,
                "entity_type": local,
                "types": [local] if local else [],
                "canonical_name": _display_name(graph, ind, cls_dt),
                "aliases": [a for a in (en,) if a and a != (zh or ref)],
                "properties": {},
            }
            by_iri[iri_s] = ent
            entities.append(ent)
            for p, o in graph.predicate_objects(ind):
                if p in _META_PREDICATES:
                    continue
                plocal = _local(p)
                if not plocal or plocal in ("type",):
                    continue
                if p in op_set:
                    rng = enum_of_prop.get(p)
                    if rng in enum_classes:
                        # 枚举取值：既是一条事实（问数用），也是取值语义
                        izh, ien = _label(graph, o)
                        facts.append({
                            "subject_ref": ref, "entity_type": local,
                            "predicate": plocal, "value": _local(o),
                            "value_label": izh or ien or _local(o),
                            "datatype": "string", "source": "object-property",
                        })
                    else:
                        relations.append({
                            "source_ref": ref, "relation_type": plocal,
                            "target_ref": _local(o),
                            "target_type": _local(rng) if isinstance(rng, URIRef) else "",
                        })
                    continue
                if p in (RDFS.seeAlso, OWL.sameAs):
                    continue
                if p not in dt_set and _ns_of(p) != "":
                    # 本体未声明的谓词：照实收下并记一次，不静默丢
                    if plocal not in skipped:
                        skipped.append(plocal)
                if isinstance(o, URIRef):
                    # 值是 IRI 就说明它指向另一个个体：谓词即使没被声明成 owl:ObjectProperty
                    # （TBox / ABox 分两个文件导入时是常态），也**必须**落成关系。
                    # 当字面量丢掉的话，「员工属于哪个部门」「主管是谁」会整批凭空消失，
                    # 而用户看到的只是「0 行」，和「本来就没数据」分不清。
                    if plocal not in _REL_SKIP_LOCAL:
                        rtypes = [t for t in graph.objects(o, RDF.type)
                                  if isinstance(t, URIRef)
                                  and not any(str(t).startswith(ns) for ns in _META_CLASS_NS)]
                        relations.append({
                            "source_ref": ref, "relation_type": plocal,
                            "target_ref": _local(o),
                            "target_type": _local(rtypes[0]) if rtypes else "",
                        })
                    continue
                if not isinstance(o, Literal):
                    continue
                val = _s(o)
                if not val:
                    continue
                facts.append({
                    "subject_ref": ref, "entity_type": local,
                    "predicate": plocal, "value": val,
                    "value_label": val,
                    "datatype": _local(o.datatype) if o.datatype else "string",
                    "source": "datatype-property",
                })

    # 事实回填到实体 properties（便于前端一次性展示；落库仍按 facts 逐条）
    by_ref = {e["ref"]: e for e in entities}
    for f in facts:
        e = by_ref.get(f["subject_ref"])
        if e is not None:
            e["properties"][f["predicate"]] = f["value"]

    return {
        "entities": entities,
        "facts": facts,
        "relations": relations,
        "counts": {"entities": len(entities), "facts": len(facts), "relations": len(relations)},
        "undeclared_predicates": skipped,
        # 实例用到、但本文档没声明的类（TBox/ABox 分文件时的常态）：照实收下并如实上报
        "undeclared_classes": [_local(c) for c in observed],
    }


# ---------------------------------------------------------------- 对外入口
def import_text(text: str, fmt: str | None = None, *,
                source_name: str = "", with_instances: bool = True) -> dict[str, Any]:
    """本体文本 → 导入结果（spec + 实例 + 统计 + 警告）。纯计算，不落盘、不写库。"""
    graph, used = parse_text(text, fmt)
    spec, meta = build_spec(graph, source_name=source_name)
    inst = build_instances(graph) if with_instances else {
        "entities": [], "facts": [], "relations": [],
        "counts": {"entities": 0, "facts": 0, "relations": 0},
        "undeclared_predicates": [], "undeclared_classes": [],
    }
    stats = {
        "format": used,
        "triples": len(graph),
        "namespaces": _safely_namespaces(graph),
        **meta["counts"],
        "instances": inst["counts"]["entities"],
        "facts": inst["counts"]["facts"],
        "relations": inst["counts"]["relations"],
    }
    warnings = list(meta["warnings"])
    if inst.get("undeclared_classes"):
        warnings.append("实例用到的类在本体里没有声明："
                        + "、".join(inst["undeclared_classes"][:12])
                        + ("…" if len(inst["undeclared_classes"]) > 12 else "")
                        + "（按实例实际用法收下，未编造类声明）")
    if inst.get("undeclared_predicates"):
        warnings.append("实例里出现本体未声明的谓词："
                        + "、".join(inst["undeclared_predicates"]) + "（照实收下，未编造声明）")
    return {
        "format": used,
        "source": source_name,
        "spec": spec,
        "instances": inst,
        "stats": stats,
        "warnings": warnings,
        "meta": meta,
    }


def _safely_namespaces(graph: Graph) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for prefix, ns in graph.namespaces():
            p = _s(prefix)
            if p and p not in out:
                out[p] = str(ns)
    except Exception:  # noqa: BLE001
        return {}
    return out


# ---------------------------------------------------------------- 自检
def self_check() -> dict[str, Any]:
    """纯函数自检：解析、类/属性/枚举映射、语义三件套、实例抽取、格式识别。"""
    ttl = """
@prefix : <http://example.org/ontology/employee#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix foaf: <http://xmlns.com/foaf/0.1/> .

<http://example.org/ontology/employee> a owl:Ontology ;
    rdfs:label "员工本体"@zh , "Employee"@en ; owl:versionInfo "1.0" .

:Employee a owl:Class ; rdfs:subClassOf foaf:Person ;
    rdfs:label "员工"@zh , "Employee"@en .
:Department a owl:Class ; rdfs:label "部门"@zh , "Department"@en .
:EmploymentStatus a owl:Class ;
    owl:oneOf ( :Active :Resigned ) ;
    rdfs:label "在职状态"@zh .
:Active a owl:NamedIndividual , :EmploymentStatus ; rdfs:label "在职"@zh .
:Resigned a owl:NamedIndividual , :EmploymentStatus ; rdfs:label "离职"@zh .

:employeeID a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string ;
    rdfs:label "工号"@zh .
:fullName a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:string ;
    rdfs:label "姓名"@zh .
:hireDate a owl:DatatypeProperty ; rdfs:domain :Employee ; rdfs:range xsd:date ;
    rdfs:label "入职日期"@zh .
:departmentName a owl:DatatypeProperty ; rdfs:domain :Department ; rdfs:range xsd:string ;
    rdfs:label "部门名称"@zh .
:hasEmploymentStatus a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :EmploymentStatus ;
    rdfs:label "在职状态"@zh .
:belongsToDepartment a owl:ObjectProperty ; rdfs:domain :Employee ; rdfs:range :Department ;
    rdfs:label "所属部门"@zh .
:emp001 a :Employee ; :employeeID "E001" ; :fullName "张三" ;
    :hireDate "2018-07-01"^^xsd:date ; :hasEmploymentStatus :Active ;
    :belongsToDepartment :dept_tech .
:emp002 a :Employee ; :employeeID "E002" ; :fullName "李四" ;
    :hasEmploymentStatus :Resigned .
:dept_tech a :Department ; :departmentName "技术部" .
"""
    checks: list[dict[str, Any]] = []

    def ck(name: str, cond: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(cond), "detail": detail})

    graph, used = parse_text(ttl, "ttl")
    ck("turtle 解析", used == "turtle" and len(graph) > 40, f"{len(graph)} 三元组")

    r = import_text(ttl, "ttl", source_name="self_check.ttl")
    spec, stats, inst = r["spec"], r["stats"], r["instances"]
    kinds = [n["kind"] for n in spec["nodes"]]
    ck("类 → 业务对象节点", kinds.count("object") == 2, f"{kinds.count('object')} 个对象")
    # 4 个数据属性（其中 departmentName 归 Department）+ 2 个枚举值属性 + 1 个引用属性
    ck("数据属性 → 属性节点", stats["datatype_attrs"] == 4, f"{stats['datatype_attrs']} 个")
    ck("枚举取值 → 每个值一个属性", stats["enum_value_attrs"] == 2, f"{stats['enum_value_attrs']} 个")
    ck("实体引用 → 引用属性", stats["reference_attrs"] == 1, f"{stats['reference_attrs']} 个")

    by_name = {(_s(n["fields"].get("name"))): n for n in spec["nodes"] if n["kind"] == "attr"}
    ck("属性中文名进 label（不进 name）",
       by_name["employeeID"]["fields"]["label"] == "工号"
       and by_name["employeeID"]["fields"]["name"] == "employeeID")
    ck("xsd:date → 画布 date", by_name["hireDate"]["fields"]["range"] == "date")

    active = by_name.get("hasEmploymentStatus_Active", {}).get("fields", {})
    ck("枚举值语义三件套",
       active.get("value_meaning") == "在职"
       and active.get("fact_predicate") == "hasEmploymentStatus"
       and active.get("fact_value") == "Active",
       f"{active.get('value_meaning')}/{active.get('fact_predicate')}/{active.get('fact_value')}")
    ck("引用属性记住目标类",
       by_name["belongsToDepartment"]["fields"].get("target_entity") == "Department")

    emp = next(n for n in spec["nodes"] if n["kind"] == "object" and n["fields"]["name"] == "Employee")
    ck("对象写 fact_entity_type", emp["fields"]["fact_entity_type"] == "Employee")
    ck("外部父类写进类型系统", emp["fields"]["type_system"].endswith("Person"),
       emp["fields"]["type_system"])
    ck("主键按命名约定推断（并披露）",
       emp["fields"]["primary_key"] == "employeeID"
       and any("命名约定" in w for w in r["warnings"]))

    fn = [n for n in spec["nodes"] if n["kind"] == "func"]
    ck("按类生成实例计数指标", any(_s(n["fields"].get("name")) == "count_Employee" for n in fn),
       f"{len(fn)} 个函数")

    ck("实例抽取：实体数", inst["counts"]["entities"] == 3, f"{inst['counts']['entities']}")
    ck("实例抽取：事实数", inst["counts"]["facts"] == 8, f"{inst['counts']['facts']}")
    ck("实例抽取：关系数", inst["counts"]["relations"] == 1, f"{inst['counts']['relations']}")
    emp1 = next(e for e in inst["entities"] if e["ref"] == "emp001")
    ck("实例显示名取姓名属性", emp1["canonical_name"] == "张三", emp1["canonical_name"])
    ck("枚举事实带中文取值标签",
       any(f["predicate"] == "hasEmploymentStatus" and f["value_label"] == "在职"
           for f in inst["facts"]))

    # 编译口径对齐：导入产物必须能被 compiler 编出 meanings（问数才走得通）
    from ..runtime.compiler import compile_spec
    ir = compile_spec(spec)
    sem = ir.get("semantics") or {}
    ck("compiler 编出 meanings（在职）",
       any(m.get("meaning") == "在职" and m.get("predicate") == "hasEmploymentStatus"
           and m.get("fact_value") == "Active" for m in sem.get("meanings", [])),
       f"{len(sem.get('meanings', []))} 条 meanings")
    ck("compiler 编出 entity_types",
       (sem.get("entity_types") or {}).get("Employee") == "Employee")
    ck("导入草稿无 unassigned 属性", not ir.get("unassigned_attrs"),
       f"{len(ir.get('unassigned_attrs') or [])} 个未归属")

    # 格式识别：空内容 / 乱码要报错而不是返回空图
    bad = False
    try:
        parse_text("   ", "ttl")
    except ValueError:
        bad = True
    ck("空内容报错", bad)
    bad2 = False
    try:
        parse_text("this is not rdf at all ,,,", "turtle")
    except ValueError:
        bad2 = True
    ck("不可解析内容报错", bad2)

    passed = sum(1 for c in checks if c["ok"])
    return {"ok": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}