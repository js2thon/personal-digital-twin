"""本体 → JSON-LD（OWL / SKOS 词表）导出。

设计口径（与项目"单一真源"约定一致）：
- 结构、属性归属、函数算法与可执行性，唯一来自 ``compiler.compile_spec(spec)`` 的 IR，
  不在这里另判一遍对象/属性（否则两处口径迟早打架）；
- IR 不保留的信息才回读原草稿节点：``rel:*`` 关系类型、``process`` 流程节点、对象是否真的填了名称；
- 不编造：未命名对象、无法解析为表达式的派生函数、未绑定 source/target 的关系类型，
  全部照实标注（``pdt:executable=false`` / ``pdt:note``），绝不补默认值冒充。

输出形态：JSON-LD 1.1 ``@graph``，命名空间按用途分段，避免同名冲突：
  {base}class/{Class}        业务对象 → owl:Class
  {base}property/{attr}      属性   → owl:DatatypeProperty（rdfs:domain / rdfs:range）
  {base}function/{fn}        函数   → owl:DatatypeProperty + pdt:functionType / pdt:aggregation
  {base}relation/{rel}       关系类型 → owl:ObjectProperty（未绑定 domain/range）
  {base}pipeline             建模流水线 → pdt:Pipeline
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..runtime.compiler import compile_spec

DEFAULT_BASE_IRI = "https://pdt.example.org/ontology/employee-organization/"
DEFAULT_TITLE = "Personal Digital Twin — Employee & Organization Ontology"

# 建模流水线五段（与画布 MD_STAGES 同序：采样→处理→对象→属性→函数）
STAGES: tuple[tuple[str, str], ...] = (
    ("sample", "数据采样"),
    ("process", "数据处理"),
    ("object", "业务对象"),
    ("attr", "属性"),
    ("func", "函数"),
)

# 画布 range → XSD 数据类型
_XSD_BY_RANGE = {
    "string": "xsd:string", "text": "xsd:string", "str": "xsd:string",
    "integer": "xsd:integer", "int": "xsd:integer", "bigint": "xsd:integer",
    "decimal": "xsd:decimal", "number": "xsd:decimal", "float": "xsd:decimal",
    "double": "xsd:decimal", "numeric": "xsd:decimal",
    "boolean": "xsd:boolean", "bool": "xsd:boolean",
    "date": "xsd:date",
    "datetime": "xsd:dateTime", "timestamp": "xsd:dateTime",
}

# 计数类算法 → xsd:integer；求和/均值类 → xsd:decimal
_COUNT_ALGO_HINT = ("count", "distinct", "hhllp", "hll", "size", "len")

# 画布两套节点命名并存：5 段流水线用 func/sample/process，旧 7 类契约用 function/datasource/profiler
_FUNC_KINDS = ("func", "function", "action")
_SAMPLE_KINDS = ("sample", "datasource")
_PROCESS_KINDS = ("process", "profiler")
_REL_KINDS = ("type", "relation")


def _context(base: str) -> dict[str, Any]:
    """JSON-LD 上下文：只声明本文件用到的词，前缀可溯源到标准词表。"""
    return {
        "@version": 1.1,
        "pdt": base,
        "owl": "http://www.w3.org/2002/07/owl#",
        "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        "xsd": "http://www.w3.org/2001/XMLSchema#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "dcterms": "http://purl.org/dc/terms/",
        "label": {"@id": "rdfs:label", "@container": "@language"},
        "prefLabel": "skos:prefLabel",
        "altLabel": {"@id": "skos:altLabel", "@container": "@set"},
        "definition": "skos:definition",
        "comment": {"@id": "rdfs:comment", "@container": "@language"},
        "domain": {"@id": "rdfs:domain", "@type": "@id"},
        "range": {"@id": "rdfs:range", "@type": "@id"},
        "subClassOf": {"@id": "rdfs:subClassOf", "@type": "@id"},
        # 等价类 / 逆属性：必须在这里声明。JSON-LD 对未声明的键静默丢弃，
        # 漏了这两行，画布上写了「equivalent=Department」「inverse_of=hasLineManager」
        # 也不会进图——导出文件里没有这条公理，查的时候 0 行，且没有任何报错。
        "equivalentClass": {"@id": "owl:equivalentClass", "@type": "@id"},
        "inverseOf": {"@id": "owl:inverseOf", "@type": "@id"},
        "oneOf": {"@id": "owl:oneOf", "@container": "@list"},
        "hasKey": {"@id": "owl:hasKey", "@type": "@id", "@container": "@set"},
        # —— 本体语义（智能问数用，与 runtime/compiler.py 的 _SEM_* 字段同名）——
        "factEntityType": "pdt:factEntityType",
        "factPredicate": "pdt:factPredicate",
        "factValue": "pdt:factValue",
        "valueMeaning": "pdt:valueMeaning",
        # —— 属性结构注释 ——
        "sourceField": "pdt:sourceField",
        "isPrimaryKey": {"@id": "pdt:isPrimaryKey", "@type": "xsd:boolean"},
        "required": {"@id": "pdt:required", "@type": "xsd:boolean"},
        "enumValues": {"@id": "pdt:enumValues", "@container": "@set"},
        "valueType": {"@id": "pdt:valueType", "@type": "@id"},
        "valuePattern": "pdt:valuePattern",
        "rangeRaw": "pdt:rangeRaw",
        # —— 函数（指标 / 派生）——
        "functionType": "pdt:functionType",
        "aggregation": "pdt:aggregation",
        "aggregationLabel": "pdt:aggregationLabel",
        "measureOf": {"@id": "pdt:measureOf", "@type": "@id", "@container": "@set"},
        "derivedFrom": {"@id": "pdt:derivedFrom", "@type": "@id", "@container": "@set"},
        "ownerAttribute": {"@id": "pdt:ownerAttribute", "@type": "@id"},
        "outputName": "pdt:outputName",
        "ruleText": "pdt:ruleText",
        "expression": "pdt:expression",
        "executable": {"@id": "pdt:executable", "@type": "xsd:boolean"},
        "official": {"@id": "pdt:official", "@type": "xsd:boolean"},
        # —— 流水线 / 溯源 ——
        "stages": {"@id": "pdt:stages", "@container": "@list"},
        "stageStatus": "pdt:stageStatus",
        # 数据血缘边：**必须在 @context 里声明**。JSON-LD 对未声明的键是静默丢弃的，
        # 漏了这一行，pipe_node["feedsObjects"] 写得再对也不会进图/进导出文件——
        # 「导出的本体里没有血缘」而且没有任何报错，只有查的时候 0 行。
        "feedsObjects": {"@id": "pdt:feedsObjects", "@type": "@id", "@container": "@set"},
        "specHash": "pdt:specHash",
        "irHash": "pdt:irHash",
        "generatedFrom": "pdt:generatedFrom",
        "contentLanguage": "pdt:contentLanguage",
        "canvasNode": "pdt:canvasNode",
        "note": "pdt:note",
    }


def _slug(text: Any) -> str:
    """IRI 局部名：仅保留 ASCII 标识符字符；空串返回空（调用方负责兜底）。"""
    s = str(text or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "-", s)
    return s.strip("-.")


def _xsd_of(range_raw: Any) -> str:
    return _XSD_BY_RANGE.get(str(range_raw or "").strip().lower(), "xsd:string")


def _split_names(text: Any) -> list[str]:
    return [x.strip() for x in re.split(r"[,，;；\s]+", str(text or "")) if x.strip()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _lang(zh: str = "", en: str = "") -> dict[str, str] | None:
    """语言标记标签（与参考 OWL 风格一致：`label: {"zh": …, "en": …}`）。

    只写实际有值的语言：中文标签来自画布 label，英文标签来自 name / 标识符。
    中文名不冒充英文标签，英文标识符不冒充中文标签——不编造翻译。
    """
    out: dict[str, str] = {}
    zh, en = str(zh or "").strip(), str(en or "").strip()
    if zh:
        out["zh"] = zh
    if en and en != zh and not _has_cjk(en):
        out["en"] = en
    return out or None


def _emit_enum(graph: list[dict[str, Any]], base: str, attr_name: str,
               attr_label: str, values: list[str]) -> str:
    """把属性枚举值落成「枚举类 + owl:oneOf + NamedIndividual」，返回枚举类 IRI。

    与参考本体一致：取值是图上的一等个体（可被 SPARQL 检索、可单独引用），
    而不是塞进属性注释的字符串。每个取值一个个体、一个单值标签，绝不合并。
    """
    enum_iri = base + "enum/" + _stable_local(attr_name, "attr")
    ind_refs: list[dict[str, str]] = []
    used: set[str] = set()
    for v in values:
        v_local = _stable_local(v)
        while v_local in used:                      # 哈希撞车极罕见，撞了就加后缀，不丢值
            v_local += "_" + hashlib.sha1((v + str(len(used))).encode("utf-8")).hexdigest()[:4]
        used.add(v_local)
        vi = enum_iri + "/" + v_local
        ind_refs.append({"@id": vi})
        graph.append({
            "@id": vi,
            "@type": ["owl:NamedIndividual", {"@id": enum_iri}],
            # 取值本身即标签；语言按书写系统判定——中文值标 zh，ASCII 值标 en，
            # 不把英文值塞进 zh（与参考格式 {"zh":"男","en":"Male"} 的分语种做法一致）
            "label": _lang(v) if _has_cjk(v) else _lang("", v),
            "prefLabel": v,
        })
    graph.append({
        "@id": enum_iri, "@type": "owl:Class",
        "label": _lang(f"{attr_label}枚举", f"{attr_name}Enum"),
        "comment": {"zh": f"属性「{attr_name}」声明的枚举取值（来自画布 enum 声明）"},
        "oneOf": ind_refs,
    })
    return enum_iri


def _norm_parent(ts: str) -> str:
    """从类型系统文本里取父类型名：剥掉 # / // 行内注释与占位符，不把注释当父类。"""
    text = re.sub(r"(#|//).*$", "", str(ts or "")).strip()
    raw = ""
    # 与 _norm_equivalent 同理：只取标识符，避免把「Department（部门）」的中文括注一起当成类名
    m = re.search(r"parent\s*[=:]\s*([A-Za-z_][A-Za-z0-9_.\-]*)", text, re.I)
    if m:
        raw = m.group(1)
    elif text and not re.search(r"[=:]", text):
        m2 = re.search(r"([A-Za-z_][A-Za-z0-9_.\-]*)", text)
        raw = m2.group(1) if m2 else ""
    raw = raw.strip().strip("'\"").strip()
    if raw.lower() in ("", "-", "/", "none", "null", "n/a", "na", "root", "无", "空"):
        return ""
    return raw


def _class_local(iri: str) -> str:
    return str(iri or "").rsplit("/", 1)[-1]


def _norm_equivalent(ts: str) -> str:
    """从类型系统文本里取「等价类」名（``equivalent=Department`` / ``owl:equivalentClass:X``）。

    ``_norm_parent`` 只认 ``parent=``，等价类靠它取不到；等价类不是继承，不能塞进
    ``rdfs:subClassOf``（那会把「组织单元」说成「部门」的下位，方向反了且丢失对称语义）。
    """
    text = re.sub(r"(#|//).*$", "", str(ts or "")).strip()
    # 只认标识符字符集。画布上常写成
    # ``equivalent=Department（owl:equivalentClass，共享同一批个体）``，
    # 用 \S+ 会把中文括注一起吞进来，结果永远匹配不到类表 → 公理静默丢失。
    m = re.search(r"(?:owl:)?equivalent(?:Class)?\s*[=:]\s*([A-Za-z_][A-Za-z0-9_.\-]*)",
                  text, re.I)
    raw = m.group(1) if m else ""
    raw = raw.strip().strip("'\"").strip()
    if raw.lower() in ("", "-", "/", "none", "null", "n/a", "na", "无", "空"):
        return ""
    return raw


# 关系节点上可声明的 OWL 属性特征：画布用自然语言词写，导出成 OWL 内置类型。
# 缺了这些类型，「传递闭包」「自己不是自己的主管」在图上就只是一句注释——
# 校验器读不到，SPARQL 也推不出，等于模型里根本没有这条约束。
_OWL_REL_CHAR: dict[str, str] = {
    "transitive": "owl:TransitiveProperty",
    "irreflexive": "owl:IrreflexiveProperty",
    "reflexive": "owl:ReflexiveProperty",
    "symmetric": "owl:SymmetricProperty",
    "asymmetric": "owl:AsymmetricProperty",
    "functional": "owl:FunctionalProperty",
    "inversefunctional": "owl:InverseFunctionalProperty",
    "inverse_functional": "owl:InverseFunctionalProperty",
}


def _owl_rel_chars(text: Any) -> list[str]:
    """把画布上的特征词（``transitive, irreflexive``）翻成 OWL 类型 IRI 列表。"""
    out: list[str] = []
    for w in re.split(r"[,，;；\s]+", str(text or "")):
        key = w.strip().lower().lstrip("owl:").rstrip(".")
        key = key.replace("property", "")
        iri = _OWL_REL_CHAR.get(key)
        if iri and iri not in out:
            out.append(iri)
    return out


def _is_false_flag(v: Any) -> bool:
    """画布布尔字段可能是 Python bool / "false" / "否" / 0，统一判假。"""
    if isinstance(v, bool):
        return not v
    if v is None:
        return False
    return str(v).strip().lower() in ("false", "0", "no", "off", "否", "不", "none")


def _general_owner(cands: list[str], class_by_name: dict[str, str],
                   parent_of_cls: dict[str, str]) -> str:
    """同一属性被父子两个类都声明时，``rdfs:domain`` 只落在**最通用**的那个类上。

    画布上「部门名 / 层级 / 负责人 / 人数」这类共用属性常被复制到每个子类里
    （七级部门各写一遍）。若按「先出现者」定 domain，domain 就挂到了
    ``DepartmentLevel1`` 上：查「部门有哪些属性」用 ``?p rdfs:domain :Department``
    精确匹配只能拿到 3 个，另外 6 个明明在数据里、却查不出来——数字对得上、答案少一半，
    最难被发现。domain 声明在父类上也更符合 OWL 语义（子类继承，不必重复声明）。
    """
    iris = {class_by_name[c]: c for c in cands if c in class_by_name}
    for c in cands:
        p = parent_of_cls.get(class_by_name.get(c, ""))
        guard: set[str] = set()
        while p and p not in guard:
            guard.add(p)
            if p in iris:           # 找到了声明同一属性的祖先类
                return iris[p]
            p = parent_of_cls.get(p)
    return cands[0]


def _stable_local(text: Any, prefix: str = "v") -> str:
    """IRI 局部名：能安全转 ASCII 就用可读 slug，纯中文/符号则退回短哈希。

    用哈希而不是序号兜底，是为了让 IRI 只由「取值本身」决定——枚举值增删或调序后
    不会整体漂移，导出的 JSON-LD 可以稳定 diff（序号兜底会让 v1/v2/v3 换位）。
    """
    s = _slug(text)
    if s:
        return s
    return prefix + hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:8]


def _enum_values(raw: Any) -> list[str]:
    """枚举取值：接受 JSON 数组或逗号分隔字符串，按出现顺序去重、去包裹引号。

    不能直接 for-in 字符串（"在职,离职" 会拆成单个字符），也不能 str(list)
    （整份清单会变成一个元素），两种写法都要吃掉。
    """
    if isinstance(raw, (list, tuple, set)):
        vals = [str(x) for x in raw]
    else:
        vals = re.split(r"[,，;；、|]+", str(raw or ""))
    out: list[str] = []
    seen: set[str] = set()
    for v in vals:
        v = str(v).strip().strip("'\"").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _add_note(node: dict[str, Any], text: str) -> None:
    """给节点追加说明：已有说明不覆盖，按「；」接在后面（既有归属说明又有引用说明时不互相吃掉）。"""
    prev = str(node.get("note") or "").strip()
    node["note"] = (prev + "；" if prev else "") + str(text or "").strip()


def build_jsonld(
    spec: dict[str, Any] | None,
    base_iri: str | None = None,
    title: str | None = None,
    generated_from: str = "canvas://pdt.ontology.modelling.v1",
    created_at: str | None = None,
) -> dict[str, Any]:
    """把建模画布的 spec 转成 JSON-LD 文档（不写盘，纯函数）。

    返回的 dict 本身就是完整 JSON-LD，调用方直接 json.dumps 即成 .jsonld 文件。
    """
    base = (str(base_iri or DEFAULT_BASE_IRI).strip().rstrip("/")) + "/"
    spec = spec or {}
    ir = compile_spec(spec)
    raw = {n.get("id"): n for n in (spec.get("nodes") or []) if isinstance(n, dict) and n.get("id")}

    def rf(nid: Any) -> dict[str, Any]:
        return ((raw.get(nid) or {}).get("fields") or {})

    def rk(nid: Any) -> str:
        return str((raw.get(nid) or {}).get("kind") or "")

    def cls_iri(name: str) -> str:
        return base + "class/" + _slug(name)

    def prop_iri(name: str) -> str:
        return base + "property/" + _slug(name)

    def fn_iri(name: str) -> str:
        return base + "function/" + _slug(name)

    def rel_iri(name: str) -> str:
        return base + "relation/" + _slug(str(name).split(":", 1)[-1])

    graph: list[dict[str, Any]] = []
    notes: list[str] = []
    head_iri = base.rstrip("/")

    # ---------- 1. 本体头 ----------
    head: dict[str, Any] = {
        "@id": head_iri,
        "@type": "owl:Ontology",
        "label": {"zh": str(title or DEFAULT_TITLE)},
        "comment": {"zh": "由 Personal Digital Twin 本体建模画布按流水线（采样→处理→对象→属性→函数）"
                          "导出的 OWL 本体；结构读编译后的 IR，关系类型/进程/画布节点 id 回读原草稿。"},
        "dcterms:title": str(title or DEFAULT_TITLE),
        "dcterms:created": {"@value": str(created_at or _now_iso()), "@type": "xsd:dateTime"},
        "dcterms:creator": "Personal Digital Twin 本体建模画布",
        "owl:versionInfo": f"spec-{str(ir.get('hash') or '')[:8]}",
        "contentLanguage": "zh-Hans",
        "specHash": str(ir.get("hash") or ""),
        "irHash": str(ir.get("hash") or ""),
        "generatedFrom": generated_from,
    }
    graph.append(head)

    # ---------- 2. 业务对象 → owl:Class ----------
    class_by_name: dict[str, str] = {}
    class_pk: dict[str, str] = {}
    skipped_objects: list[str] = []
    pending_subclass: list[tuple[str, str]] = []   # (子类 IRI, 声明的父类型名)，循环后统一解析
    pending_equiv: list[tuple[str, str]] = []      # (类 IRI, 等价类名)，循环后统一解析
    for o in ir.get("objects") or []:
        name = str(o.get("name") or "").strip()
        if not name or name.startswith("rel:"):
            continue  # 关系类型单独走第 5 段
        fields = rf(o.get("id"))
        if o.get("id") in raw and not str(fields.get("name") or "").strip():
            skipped_objects.append(str(o.get("id")))
            continue  # 画布上存在但没填名称：不生成空壳类
        class_by_name[name] = cls_iri(name)
        zh_label = str(fields.get("label") or "").strip()
        node: dict[str, Any] = {"@id": cls_iri(name), "@type": "owl:Class",
                                "label": _lang(zh_label or name, name)}
        alt: list[str] = []
        if zh_label and zh_label != name:
            alt.append(zh_label)
        alt.extend(_split_names(fields.get("aliases")))
        if alt:
            node["altLabel"] = list(dict.fromkeys(alt))
        node["prefLabel"] = zh_label or name
        desc = str(fields.get("desc") or o.get("desc") or "").strip()
        if desc:
            node["definition"] = desc
            node["comment"] = {"zh": desc}
        fet = str(fields.get("fact_entity_type") or "").strip()
        if fet:
            node["factEntityType"] = fet
        pk = str(o.get("primary_key") or fields.get("primary_key") or "").strip()
        if pk:
            class_pk[cls_iri(name)] = pk
        # 类型系统 / 父类型：只有指向本本体里确实存在的类时才声明 subClassOf（不引用空壳）
        parent_ref = _norm_parent(str(fields.get("type_system") or ""))
        if not parent_ref:
            parent_ref = _norm_parent(str(fields.get("parent") or ""))
        if parent_ref and parent_ref != name:
            pending_subclass.append((cls_iri(name), parent_ref))
        equiv_ref = _norm_equivalent(str(fields.get("type_system") or ""))
        if not equiv_ref:
            equiv_ref = _norm_equivalent(str(fields.get("equivalent") or ""))
        if equiv_ref and equiv_ref != name:
            pending_equiv.append((cls_iri(name), equiv_ref))
        node["canvasNode"] = str(o.get("id") or "")
        graph.append(node)

    sub_resolved = 0
    sub_missing: list[str] = []
    parent_of_cls: dict[str, str] = {}      # 子类 IRI → 父类 IRI（属性 domain 归属判定用）
    for ciri, pref in pending_subclass:
        tgt = class_by_name.get(pref) or class_by_name.get(pref.split(":")[-1])
        if tgt and tgt != ciri:
            parent_of_cls[ciri] = tgt
            for g in graph:
                if g.get("@id") == ciri:
                    g["subClassOf"] = {"@id": tgt}
                    break
            sub_resolved += 1
        else:
            sub_missing.append(f"{_class_local(ciri)}→{pref}")
    if sub_missing:
        notes.append(f"{len(sub_missing)} 个对象声明的父类型在本体里没有对应类"
                     f"（{'、'.join(sub_missing)}），故未声明 rdfs:subClassOf（不引用不存在的类）")

    # 等价类：owl:equivalentClass 是**对称**公理，两边的实例集合相同。
    # 只声明在一侧即可（OWL 语义上等价），这里按声明侧如实落，不额外补反向边——
    # 反向边是推理结果，写进 TBox 会让人误以为本体里真的写了两次。
    equiv_resolved = 0
    equiv_missing: list[str] = []
    for ciri, pref in pending_equiv:
        tgt = class_by_name.get(pref) or class_by_name.get(pref.split(":")[-1])
        if tgt and tgt != ciri:
            for g in graph:
                if g.get("@id") == ciri:
                    g["equivalentClass"] = {"@id": tgt}
                    break
            equiv_resolved += 1
        else:
            equiv_missing.append(f"{_class_local(ciri)}≡{pref}")
    if equiv_missing:
        notes.append(f"{len(equiv_missing)} 个对象声明的等价类在本体里没有对应类"
                     f"（{'、'.join(equiv_missing)}），故未声明 owl:equivalentClass（不引用不存在的类）")

    # ---------- 3. 属性 → owl:DatatypeProperty ----------
    attr_names: set[str] = set()   # 合并后真正落图的属性名（hasKey 判定用）
    attr_owner: dict[str, str] = {}
    sem_by_attr: dict[str, dict[str, str]] = {}
    sem = ir.get("semantics") or {}
    for m in (sem.get("meanings") or []):
        nm = str(m.get("attribute") or "").strip()
        if nm:
            sem_by_attr[nm] = {
                "valueMeaning": str(m.get("meaning") or ""),
                "factPredicate": str(m.get("predicate") or ""),
                "factValue": str(m.get("fact_value") or ""),
            }
    for m in (sem.get("lookups") or []):
        nm = str(m.get("attribute") or "").strip()
        if nm and nm not in sem_by_attr:
            sem_by_attr[nm] = {
                "valueMeaning": str(m.get("meaning") or ""),
                "factPredicate": str(m.get("predicate") or ""),
                "factValue": "",
            }

    # 属性来源两处：独立属性节点（语义真源：标签/描述/指标/事实映射）+ 对象 attributes 声明。
    # 只导出前者会让「只写了对象属性清单」的本体缺属性（参考格式里每个属性都要有 domain/range），
    # 故按名字合并：属性节点优先，声明只补它没有的（归属对象、枚举取值）。
    # 关系谓词名（对应对象属性）先收集出来：对象 attributes 里把同一个关系又写成 `xxx:string`，
    # 导出会多出一个**同名** owl:DatatypeProperty。OWL 里同名谓词是两个不同 IRI 的属性，
    # 数据一旦落到字符串那个上，「上级的上级」「主管的经理」这类多跳查询永远 0 行——
    # 图里看不出缺数据，只是关系边断了。故一律以关系节点为准，attributes 里的同名声明跳过。
    rel_pred_names: set[str] = set()
    for o in ir.get("objects") or []:
        _rnm = str(o.get("name") or "").strip()
        if _rnm.startswith("rel:"):
            rel_pred_names.add(_rnm.split(":", 1)[-1])
    for _nid in raw:
        if rk(_nid) == "relation":
            _rnm = str(rf(_nid).get("name") or "").strip()
            if _rnm:
                rel_pred_names.add(_rnm.split(":", 1)[-1])
    rel_shadowed: list[tuple[str, str]] = []

    merged_attrs: list[dict[str, Any]] = []
    merged_by_name: dict[str, dict[str, Any]] = {}
    for a in ir.get("attrs") or []:
        nm = str(a.get("name") or "").strip()
        if not nm:
            continue
        entry = dict(a)
        merged_attrs.append(entry)
        merged_by_name[nm] = entry
    declared_only = 0
    declared_by: dict[str, list[str]] = {}   # 属性名 → 声明过它的全部类（父子类常各写一遍）
    for o in ir.get("objects") or []:
        oname = str(o.get("name") or "").strip()
        if not oname or oname.startswith("rel:") or oname not in class_by_name:
            continue
        for d in o.get("declared_attrs") or []:
            nm = str(d.get("name") or "").strip()
            if not nm:
                continue
            if nm in rel_pred_names and nm not in merged_by_name:
                # 关系已由 relation 节点导出成 owl:ObjectProperty，这里不再造同名数据属性
                rel_shadowed.append((oname, nm))
                continue
            bucket = declared_by.setdefault(nm, [])
            if oname not in bucket:
                bucket.append(oname)
            cur = merged_by_name.get(nm)
            if cur is not None:
                if not str(cur.get("object") or "").strip():
                    cur["object"] = oname
                if not _enum_values(cur.get("enum")) and _enum_values(d.get("enum")):
                    cur["enum"] = _enum_values(d.get("enum"))
                continue
            entry = {
                "id": "", "name": nm, "label": "", "desc": "", "metric_fn": "",
                "range": str(d.get("range") or "string"), "required": d.get("required") is True,
                "enum": _enum_values(d.get("enum")), "pattern": str(d.get("pattern") or ""),
                "source_field": "", "is_pk": False, "object": oname, "declared": True,
            }
            merged_attrs.append(entry)
            merged_by_name[nm] = entry
            declared_only += 1
    if rel_shadowed:
        _pairs = "、".join(f"{o}.{n}" for o, n in rel_shadowed[:6])
        notes.append(f"{len(rel_shadowed)} 处 attributes 把关系写成了字符串属性（{_pairs}），"
                     f"已按关系节点导出为 owl:ObjectProperty 并跳过多余的同名数据属性——"
                     f"同名双谓词会让多跳关系查询查不到数据")
    if declared_only:
        notes.append(f"{declared_only} 个属性只在对象的 attributes 声明里定义（画布上没有独立属性节点），"
                     f"已按声明生成 owl:DatatypeProperty；其中文标签/描述/事实映射为空，"
                     f"如需补齐请在画布上补属性节点")

    # 导入的本体把枚举类**展开成「一个取值一个属性节点」**（问数要 fact_predicate + fact_value
    # 这套三件套）。导出 OWL 时必须还原成「一个枚举类 + owl:oneOf 个体 + 一个 ObjectProperty」，
    # 否则每个取值都长成一个 DatatypeProperty（`hasEmploymentStatus_Act` 这种），枚举语义就丢了。
    enum_groups: dict[tuple[str, str], dict[str, Any]] = {}
    enum_member_ids: set[str] = set()
    enum_exported = 0
    for a in merged_attrs:
        f_raw = rf(a.get("id"))
        ecls = str(f_raw.get("enum_class") or "").strip()
        ev = str(f_raw.get("enum_value") or "").strip()
        if not (ecls and ev):
            continue
        owner0 = str(a.get("object") or "").strip()
        pred0 = str(f_raw.get("fact_predicate") or a.get("source_field")
                    or a.get("name") or "").strip()
        if not pred0:
            continue
        grp = enum_groups.setdefault((owner0, pred0), {
            "enum_class": ecls, "predicate": pred0, "owner": owner0,
            "predicate_label": str(f_raw.get("enum_predicate_label") or "").strip(),
            "range": str(a.get("range") or "string"), "members": [], "canvas": [],
        })
        nid0 = str(a.get("id") or "").strip()
        if nid0:
            enum_member_ids.add(nid0)
            if nid0 not in grp["canvas"]:
                grp["canvas"].append(nid0)
        if all(m[0] != ev for m in grp["members"]):
            grp["members"].append((ev, str(a.get("label") or "").strip() or ev))

    # 属性特征（owl:FunctionalProperty 等）画布写在对象/属性的 type_system 文本里，
    # 例如 Employee 的 "owl:hasKey=employeeId; owl:functionalProperty=employeeId"。
    # 不落成 OWL 类型，「一个员工只有一个工号」在图上只是句注释：既查不出来，
    # 唯一性校验也没有 TBox 依据——数据里真出现两个工号时不会有人发现。
    functional_props: set[str] = set()
    for o in ir.get("objects") or []:
        _ts = str(rf(o.get("id")).get("type_system") or "")
        for m in re.finditer(r"(?:owl:)?functional(?:Property)?\s*[=:]\s*([A-Za-z0-9_,\s]+)", _ts, re.I):
            for one in _split_names(m.group(1)):
                functional_props.add(one)

    for a in merged_attrs:
        name = str(a.get("name") or "").strip()
        if not name:
            continue
        if str(a.get("id") or "").strip() in enum_member_ids:
            continue  # 同一枚举类的取值统一由下面的「枚举类」段导出，不各写一个数据属性
        attr_names.add(name)
        enum = _enum_values(a.get("enum"))
        # 引用型属性：导入本体时对象属性的 rdfs:range 落在 fields.target_entity 上，
        # IR 不透传这个字段，故按节点 id 回读原草稿。
        ref_target = str(rf(a.get("id")).get("target_entity") or "").strip()
        ref_hit = bool(ref_target) and ref_target in class_by_name
        # 枚举属性在 OWL 里是指向个体的对象属性（range 是枚举类）；DatatypeProperty 的
        # range 只能是数据类型，指到类上不是合法 OWL（参考件 hasGender/hasEmploymentStatus
        # 也是 owl:ObjectProperty + range=枚举类）。故按有无枚举值分型。
        node = {"@id": prop_iri(name),
                "@type": "owl:ObjectProperty" if (enum or ref_hit) else "owl:DatatypeProperty",
                "label": _lang(str(a.get("label") or "").strip() or name, name)}
        if name in functional_props:
            # 函数型属性 = 单值约束（一主一值）。落成 OWL 内置类型，而不是只留一句 note。
            node["@type"] = [node["@type"], "owl:FunctionalProperty"]
        owner = str(a.get("object") or "").strip()
        # 父子类都声明了同一属性：domain 挂到最通用的那个类（见 _general_owner 说明）
        _alts = declared_by.get(name) or []
        if owner and len(_alts) > 1:
            owner = _general_owner([owner] + [o for o in _alts if o != owner],
                                   class_by_name, parent_of_cls)
        if owner:
            attr_owner[name] = owner
        if owner and owner in class_by_name:
            node["domain"] = {"@id": class_by_name[owner]}
        else:
            node["note"] = ("归属对象未解析：该属性没有 object→attr 边、也没有写进任何对象的 attributes 声明，"
                            "故不声明 rdfs:domain（不猜测归属）")
        range_raw = str(a.get("range") or "").strip()
        xsd = _xsd_of(range_raw)
        node["range"] = {"@id": xsd}
        if range_raw and xsd == "xsd:string" and range_raw.lower() not in ("string", "text", "str"):
            node["rangeRaw"] = range_raw  # 画布自定类型名照实保留
        label = str(a.get("label") or "").strip()
        if label and label != name:
            node["altLabel"] = [label]
        node["prefLabel"] = label or name
        desc = str(a.get("desc") or "").strip()
        if desc:
            node["definition"] = desc
            node["comment"] = {"zh": desc}
        src = str(a.get("source_field") or "").strip()
        if src and src != name:
            node["sourceField"] = src
        if a.get("required") is True:
            node["required"] = True
        if a.get("is_pk") is True:
            node["isPrimaryKey"] = True
        enum = _enum_values(a.get("enum"))
        if enum:
            # 枚举按参考本体风格建「类 + owl:oneOf + 个体」，而不是把取值塞成字符串注释：
            # 这样「在职」这类语义在图上是一个个体（可被 SPARQL 检索），与物理码分开存放。
            node["enumValues"] = enum
            enum_iri = _emit_enum(graph, base, name, label or name, enum)
            node["range"] = {"@id": enum_iri}   # 值域改为枚举类
            node["valueType"] = xsd             # 物理存储仍按原类型（如 xsd:string 存码值）
            node.pop("rangeRaw", None)
        elif ref_hit:
            # 取值是另一个类的实例：OWL 里这是 owl:ObjectProperty + range=类。
            # 写成 DatatypeProperty + range=string 会把「员工属于某部门」压成一段字符串，
            # 关系语义（分组统计、多跳）整条断掉。
            node["range"] = {"@id": class_by_name[ref_target]}
            node.pop("rangeRaw", None)
            _add_note(node, f"引用型属性：取值是 {ref_target} 的实例（rdfs:range 来自导入本体的对象属性声明）")
        elif ref_target:
            _add_note(node, f"引用目标「{ref_target}」不在本次导出的类里（只被属性引用、未声明为类），"
                            f"故保留为 DatatypeProperty，不声明 rdfs:range")
        pat = str(a.get("pattern") or "").strip()
        if pat:
            node["valuePattern"] = pat
        s = sem_by_attr.get(name) or {}
        for k in ("valueMeaning", "factPredicate", "factValue"):
            if s.get(k):
                node[k] = s[k]
        if str(a.get("id") or "").strip():
            node["canvasNode"] = str(a.get("id"))   # 只有画布上真有属性节点时才写溯源 id
        graph.append(node)

    # ---------- 3b. 枚举类还原：owl:Class + owl:oneOf + 个体，属性收成一个 ObjectProperty ----------
    for grp in enum_groups.values():
        ecls = str(grp["enum_class"])
        cname = ecls
        if cname in class_by_name:
            # 枚举类与业务类重名：不覆盖已有业务类，加后缀并在说明里讲清（不静默改名）
            cname = ecls + "Enum"
        enum_iri = cls_iri(cname)
        ind_refs: list[dict[str, str]] = []
        used: set[str] = set()
        for code, vlabel in grp["members"]:
            v_local = _stable_local(code, "v")
            while v_local in used:      # 撞车极罕见，撞了加后缀，绝不丢取值
                v_local += "_" + hashlib.sha1((code + str(len(used))).encode("utf-8")).hexdigest()[:4]
            used.add(v_local)
            vi = base + "enum/" + _slug(cname) + "/" + v_local
            ind_refs.append({"@id": vi})
            graph.append({
                "@id": vi,
                "@type": ["owl:NamedIndividual", {"@id": enum_iri}],
                "label": _lang(vlabel) if _has_cjk(vlabel) else _lang("", vlabel),
                "prefLabel": vlabel,
                "factValue": code,      # 物理码（本体里的个体局部名）与中文标签分开存放
            })
        zh_cls = f"{grp['predicate_label']}取值" if grp["predicate_label"] else ""
        graph.append({
            "@id": enum_iri, "@type": "owl:Class",
            "label": _lang(zh_cls, cname),
            "prefLabel": zh_cls or cname,
            "comment": {"zh": f"枚举类 {ecls}：由导入的本体按 owl:oneOf 重建"
                              f"（原本体每个取值在画布上各占一个属性节点，此处收成一个类）"},
            "oneOf": ind_refs,
        })
        pnode: dict[str, Any] = {
            "@id": prop_iri(grp["predicate"]), "@type": "owl:ObjectProperty",
            "label": _lang(grp["predicate_label"] or grp["predicate"], grp["predicate"]),
            "prefLabel": grp["predicate_label"] or grp["predicate"],
            "range": {"@id": enum_iri},
            "valueType": {"@id": _xsd_of(grp["range"])},
            "factPredicate": grp["predicate"],
        }
        owner = str(grp["owner"]).strip()
        if owner and owner in class_by_name:
            pnode["domain"] = {"@id": class_by_name[owner]}
        else:
            _add_note(pnode, "归属对象未解析：该属性没有 object→attr 边、也没有写进任何对象的 "
                             "attributes 声明，故不声明 rdfs:domain（不猜测归属）")
        if grp["canvas"]:
            pnode["canvasNode"] = list(grp["canvas"])
        graph.append(pnode)
        enum_exported += 1

    # owl:hasKey 只在主键属性确实存在时才声明（否则不声明，也不臆造一个 id 属性）
    pk_missing: list[str] = []
    for ciri, pk in class_pk.items():
        if pk in attr_names:
            for g in graph:
                if g.get("@id") == ciri:
                    g["hasKey"] = [{"@id": prop_iri(pk)}]
                    break
        else:
            pk_missing.append(f"{ciri.split('/')[-1]}→{pk}")
    if pk_missing:
        notes.append(f"{len(pk_missing)} 个类声明了主键但该主键未出现在属性节点中"
                     f"（{'、'.join(pk_missing)}），故未生成 owl:hasKey；"
                     f"如需主键约束，请先在画布补对应属性节点")

    # ---------- 4. 函数（指标 / 派生）→ owl:DatatypeProperty ----------
    # IR 的函数不带画布节点 id（id 恒为空），按 name/label 回查草稿里的 func 节点，保证溯源可点回画布
    fn_node_by_key: dict[str, str] = {}
    for nid in raw:
        if rk(nid) not in _FUNC_KINDS:
            continue
        f0 = rf(nid)
        for key in (f0.get("name"), f0.get("label")):
            k = str(key or "").strip()
            if k and k not in fn_node_by_key:
                fn_node_by_key[k] = nid
    fn_total = 0
    derive_unexec = 0
    for f in ir.get("functions") or []:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        fn_total += 1
        ftype = str(f.get("type") or "").strip() or "derive"
        node = {
            "@id": fn_iri(name),
            "@type": "owl:DatatypeProperty",
            "label": _lang(str(f.get("label") or "").strip() or name, name),
            "prefLabel": str(f.get("label") or name),
            "functionType": ftype,
            "outputName": str(f.get("output") or ""),
        }
        owner_attr = str(f.get("owner_attr") or "").strip()
        if owner_attr in attr_names:
            node["ownerAttribute"] = {"@id": prop_iri(owner_attr)}
        owner_obj = attr_owner.get(owner_attr, "")
        if owner_obj in class_by_name:
            node["domain"] = {"@id": class_by_name[owner_obj]}  # 函数挂在其所有者属性所在的业务对象上
        inputs = [x for x in (f.get("inputs") or []) if str(x).strip() in attr_names]
        if inputs:
            node["measureOf"] = [{"@id": prop_iri(x)} for x in inputs]
            if ftype != "metric":
                node["derivedFrom"] = [{"@id": prop_iri(x)} for x in inputs]
        algo = str(f.get("algo") or "").strip()
        obj_notes: list[str] = []
        if algo:
            node["aggregation"] = algo
            node["aggregationLabel"] = str(f.get("algo_label") or "")
            is_count = any(h in algo.lower() for h in _COUNT_ALGO_HINT)
            node["range"] = {"@id": "xsd:integer" if is_count else "xsd:decimal"}
        else:
            # 无算法 → 看派生结果是否已建模为属性：有就沿用该属性的 range（本体内部推导，非猜测）
            out_name = str(f.get("output") or f.get("target") or "").strip()
            tgt = merged_by_name.get(out_name)
            if tgt is not None:
                node["range"] = {"@id": _xsd_of(tgt.get("range"))}
            else:
                obj_notes.append(f"派生结果「{out_name or '未声明'}」未建模为属性节点，"
                                 f"输出类型不可知，故不声明 rdfs:range")
        logic = str(f.get("logic") or "").strip()
        if not logic and ftype == "action":
            cond = str(f.get("condition") or "").strip()
            eff = str(f.get("effect") or "").strip()
            logic = "；".join(x for x in (f"条件：{cond}" if cond else "",
                                          f"效果：{eff}" if eff else "") if x)
        if logic:
            node["ruleText"] = logic
        executable = bool(f.get("executable"))
        node["executable"] = executable
        if f.get("official") is not None:
            node["official"] = bool(f.get("official"))
        if executable and ftype != "metric" and logic:
            node["expression"] = logic  # 只有编译器判定可执行时才当表达式写
        if not executable:
            derive_unexec += 1 if ftype != "metric" else 0
            reason = str(f.get("reason") or "").strip()
            obj_notes.append("当前不可执行" + (f"：{reason}" if reason else "") +
                             "；规则文本见 pdt:ruleText，未生成 pdt:expression（不把中文描述当表达式）")
        if obj_notes:
            node["note"] = "；".join(obj_notes)
        node["canvasNode"] = fn_node_by_key.get(name) or str(f.get("id") or "")
        graph.append(node)

    # ---------- 5. 关系类型 → owl:ObjectProperty ----------
    rel_names: list[str] = [str(o.get("name") or "") for o in (ir.get("objects") or [])]
    rel_names += [str((raw[nid].get("fields") or {}).get("name") or "")
                  for nid in raw if rk(nid) in _REL_KINDS]
    rel_seen: list[str] = []
    for nm in rel_names:
        nm = nm.strip()
        if nm.startswith("rel:") and nm not in rel_seen:
            rel_seen.append(nm)
    for nm in rel_seen:
        graph.append({
            "@id": rel_iri(nm),
            "@type": "owl:ObjectProperty",
            "label": _lang(nm.split(":", 1)[-1]),
            "prefLabel": nm.split(":", 1)[-1],
            "note": "关系类型来自真实 schema；当前画布未绑定 source/target 对象，"
                    "故不声明 rdfs:domain/range（不推测两端类型）",
        })
    if rel_seen:
        notes.append(f"{len(rel_seen)} 个关系类型只声明为 owl:ObjectProperty，未绑定两端对象，"
                     f"因此没有 rdfs:domain/rdfs:range")

    # 旧 7 类契约的 relation 节点带 source_entity/target_entity，两端能对上业务对象时才声明 domain/range
    bound_rels = 0
    for nid in raw:
        if rk(nid) != "relation":
            continue
        f0 = rf(nid)
        nm = str(f0.get("name") or "").strip()
        if not nm or nm.startswith("rel:"):
            continue
        src = str(f0.get("source_entity") or "").strip()
        tgt = str(f0.get("target_entity") or "").strip()
        # label 必须带语言标记。写成裸字符串会落成**无语言**的 rdfs:label：
        # TTL 里 :hasLineManager 是 rdfs:label "直线主管"@zh，画布上也有中文 label，
        # 但导出时语言位丢了——问「张三的直线主管是谁」拿中文标签去匹配属性就永远
        # 命中不到，只能按英文局部名匹配，中文问句因此答不出来。
        node = {"@id": rel_iri(nm), "@type": "owl:ObjectProperty",
                "label": _lang(str(f0.get("label") or "").strip(), nm)}
        # —— 属性特征（owl:TransitiveProperty / owl:IrreflexiveProperty …）——
        # 传递性如果只写在描述里，「上级的上级」在 TBox 上就没有依据；不自反性如果只写在
        # 描述里，数据里出现「自己是自己的主管」也不会被判违规。故一律落成 OWL 内置类型。
        chars = _owl_rel_chars(f0.get("owl_characteristics")
                               or f0.get("characteristics")
                               or f0.get("property_types"))
        if chars:
            node["@type"] = ["owl:ObjectProperty"] + chars
        # —— 逆属性（owl:inverseOf）——
        # 逆属性不是「再加一条同义关系」，而是**方向由它定义**：manages 的域/值域
        # 是 hasLineManager 的两端对调，本体里根本不必再写一遍 domain/range。
        # 以前不导出这条，逆属性在图上成了孤立谓词，两端类型只能靠猜。
        inv = str(f0.get("inverse_of") or "").strip()
        if inv:
            node["inverseOf"] = {"@id": rel_iri(inv.split(":", 1)[-1])}
        # 本体只声明了 owl:inverseOf、没写 domain/range 的逆属性，这里也**不许臆造**：
        # 画布上填的两端类型是给人看的，「本体文件里没有」就不能变成图上的公理——
        # 否则问「manages 的值域是什么」答出一个本体文档里查不到的类型。
        skip_dr = _is_false_flag(f0.get("declare_domain_range")) and bool(inv)
        if skip_dr:
            src, tgt = "", ""
        if src in class_by_name:
            node["domain"] = {"@id": class_by_name[src]}
        if tgt in class_by_name:
            node["range"] = {"@id": class_by_name[tgt]}
        card = str(f0.get("cardinality") or "").strip()
        obj_notes2 = [f"基数 {card}（画布声明）"] if card else []
        if skip_dr:
            obj_notes2.append("本体只声明了 owl:inverseOf，未手写 rdfs:domain/rdfs:range"
                              "（两端类型由逆属性蕴含得出，见下方补全说明）")
        if src and src not in class_by_name:
            obj_notes2.append(f"源对象「{src}」未在业务对象中建模，未声明 rdfs:domain")
        if tgt and tgt not in class_by_name:
            obj_notes2.append(f"目标对象「{tgt}」未在业务对象中建模，未声明 rdfs:range")
        if obj_notes2:
            node["note"] = "；".join(obj_notes2)
        node["canvasNode"] = nid
        graph.append(node)
        bound_rels += 1

    # —— 逆属性的 domain/range：由 owl:inverseOf **蕴含**，不是画布猜的 ——
    # 本体只写 `:manages owl:inverseOf :hasLineManager` 时，「manages 的域/值域是
    # hasLineManager 的两端对调」在 OWL 语义下是**必然结论**，不需要也不应该再手写一遍。
    # 但若图上真的什么都没有，manages 就成了无归属的孤立谓词：问答侧按「有 domain 的属性」
    # 枚举业务属性时整条被过滤掉，「某主管管了哪些人」直接答不出来。
    # 故这里做一次收尾补全：只补**能从逆属性推出来的**，推导不来的一律留空。
    _by_iri = {g.get("@id"): g for g in graph}
    entailed_rels = 0
    for g in graph:
        inv_ref = g.get("inverseOf")
        inv = inv_ref.get("@id") if isinstance(inv_ref, dict) else ""
        if not inv:
            continue
        src_node = _by_iri.get(inv) or {}
        added = []
        if "domain" not in g and src_node.get("range"):
            g["domain"] = src_node["range"]
            added.append("domain=逆属性的 range")
        if "range" not in g and src_node.get("domain"):
            g["range"] = src_node["domain"]
            added.append("range=逆属性的 domain")
        if added:
            entailed_rels += 1
            prev = str(g.get("note") or "")
            g["note"] = "；".join(x for x in (
                prev,
                f"两端类型由 owl:inverseOf 蕴含（{'、'.join(added)}），非画布声明",
            ) if x)
    if entailed_rels:
        notes.append(f"{entailed_rels} 个逆属性的 rdfs:domain/rdfs:range 由 owl:inverseOf 蕴含补全"
                     f"（两端对调，等价于本体文件的语义，未新增公理）")

    # ---------- 6. 流水线 ----------
    stage_count = {
        "sample": sum(1 for nid in raw if rk(nid) in _SAMPLE_KINDS),
        "process": sum(1 for nid in raw if rk(nid) in _PROCESS_KINDS),
        "object": len(class_by_name),
        "attr": len(attr_names),
        "func": fn_total,
    }
    proc_detail = ""
    proc_links: list[str] = []
    for nid in raw:
        if rk(nid) not in _PROCESS_KINDS:
            continue
        pf = rf(nid)
        filled = [k for k in ("name", "rule", "input", "output", "profile_rules", "clean_rules")
                  if str(pf.get(k) or "").strip()]
        proc_detail = ("处理节点已填：" + "、".join(filled)) if filled else \
            "处理节点在本草稿中未填规则/输入/输出，按空如实记录"
        # 数据血缘：处理结果喂给哪些业务对象（画布 link_objects，值可能是对象 id 也可能是对象名）。
        # 只导出能对上已生成 owl:Class 的，对不上的如实留在 note 里，不凭空造类。
        links = pf.get("link_objects") or []
        if isinstance(links, str):
            links = links.replace("，", ",").split(",")
        for one in links:
            key = str(one or "").strip()
            if not key:
                continue
            nm = ""
            if rk(key) in ("entity", "object"):
                nm = str(rf(key).get("name") or "").strip()
            nm = nm or key
            if nm and nm not in proc_links:
                proc_links.append(nm)
        break
    link_note = ""
    if proc_detail:
        link_note = ("处理节点关联业务对象：" + "、".join(proc_links)) if proc_links else \
            "处理节点未关联业务对象（数据血缘为空，可在处理节点详情里勾选）"
    pipe_node = {
        "@id": base + "pipeline",
        "@type": "pdt:Pipeline",
        "label": "建模流水线",
        "stages": [{"@value": k, "@language": "en"} for k, _ in STAGES],
        "stageStatus": "；".join(f"{label}={stage_count.get(k, 0)}" for k, label in STAGES),
        "note": "；".join(x for x in (proc_detail, link_note) if x) or "本草稿未包含处理节点",
    }
    fed = [{"@id": class_by_name[n]} for n in proc_links if n in class_by_name]
    if fed:
        # 数据血缘落到图上：pdt:feedsObject 指回业务对象，外部系统能顺这条边看清「数据从哪来」
        pipe_node["feedsObjects"] = fed
    graph.append(pipe_node)
    # 元类不是业务类：若声明成 owl:Class，SPARQL 侧会把它当一个业务对象，
    # 「各类实例数」里凭空多一行 Pipeline=0，示例也会挑错主角类。用 pdt 自有元类词汇声明。
    graph.append({"@id": base + "Pipeline", "@type": "pdt:PipelineMetaClass",
                  "label": "Pipeline",
                  "altLabel": ["建模流水线元类（非业务对象）"],
                  "comment": "建模流水线的元类（pdt 词表自声明，不是 owl:Class，不计入业务对象）"})

    # ---------- 7. 如实披露 ----------
    if skipped_objects:
        notes.append(f"{len(skipped_objects)} 个对象节点未填名称（{', '.join(skipped_objects)}），"
                     f"未生成 owl:Class，避免空壳类污染本体")
    if derive_unexec:
        notes.append(f"{derive_unexec} 个派生函数的规则是中文描述而非可解析表达式，"
                     f"已标注 pdt:executable=false，仅保留 pdt:ruleText")
    notes.insert(0, f"业务对象 {len(class_by_name)} 个已生成为 owl:Class；"
                    f"另有 1 个 pdt:Pipeline 元类声明（建模流水线的类型，不计入业务对象）")
    if enum_exported:
        notes.append(f"{enum_exported} 个枚举类按 owl:oneOf 重建为 owl:Class（本体里每个取值原本各占"
                     f"画布一个属性节点，导出时收成一个 owl:ObjectProperty + N 个个体）")
    notes.append(f"本文件由建模画布 spec 自动导出，IRI 基址为 {head_iri}（占位域名），"
                 f"对接外部系统前请替换为正式 IRI；文件不含实例数据，仅本体结构")
    for g in graph:
        if g.get("@id") == head_iri:
            g["note"] = notes
            break

    return {"@context": _context(base), "@graph": graph}


def to_jsonld_text(spec: dict[str, Any] | None, **kw: Any) -> str:
    """直接给出可落盘的 .jsonld 文本（UTF-8、缩进 2、不转义中文）。"""
    return json.dumps(build_jsonld(spec, **kw), ensure_ascii=False, indent=2)


def _types(node: dict) -> set[str]:
    """把节点的 ``@type`` 统一读成集合：JSON-LD 里单个类型是字符串，多个类型是数组。

    属性一旦带上 ``owl:FunctionalProperty``、关系一旦带上 ``owl:TransitiveProperty``，
    ``@type`` 就必然是列表。判定处必须两种形状都认，否则这些节点会被静默漏掉。
    """
    t = node.get("@type")
    if isinstance(t, (list, tuple, set)):
        return {str(x) for x in t if x}
    return {str(t)} if t else set()


def _is_a(node: dict, *iris: str) -> bool:
    return any(i in _types(node) for i in iris)


def self_check() -> dict[str, Any]:
    """自检：用示例工作流跑一遍，断言 JSON-LD 结构硬约束与幂等性。"""
    checks: list[dict[str, Any]] = []

    def ck(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    from .sample import sample_spec

    spec = sample_spec()
    doc = build_jsonld(spec, created_at="2026-01-01T00:00:00+08:00")
    graph = doc.get("@graph") or []
    ctx = doc.get("@context") or {}

    ck("context_version_1_1", ctx.get("@version") == 1.1)
    ids = [g.get("@id") for g in graph]
    ck("all_nodes_have_id", all(bool(i) for i in ids), f"{len(ids)} nodes")
    ck("ids_unique", len(ids) == len(set(ids)), f"{len(ids)}/{len(set(ids))}")

    classes = [g for g in graph if _is_a(g, "owl:Class")]
    dprops = [g for g in graph if _is_a(g, "owl:DatatypeProperty")]
    oprops = [g for g in graph if _is_a(g, "owl:ObjectProperty")]
    head = [g for g in graph if _is_a(g, "owl:Ontology")]
    ck("has_ontology_head", len(head) == 1)
    ck("has_classes", len(classes) > 0, f"{len(classes)}")
    ck("has_datatype_properties", len(dprops) > 0, f"{len(dprops)}")
    ck("pipeline_present", any(_is_a(g, "pdt:Pipeline") for g in graph))
    ck("object_properties_optional", True, f"oprops={len(oprops)}")

    ck("datatype_props_range_or_explained",
       all(("range" in g) or bool(str(g.get("note") or "").strip()) for g in dprops),
       f"无 range 且无说明={sum(1 for g in dprops if 'range' not in g and not str(g.get('note') or '').strip())}")
    ck("head_has_notes", bool((head[0].get("note") if head else None)),
       f"{len(head[0].get('note') or []) if head else 0} notes")
    ck("generated_from_recorded", bool(head and head[0].get("generatedFrom")))
    fn_nodes = [g for g in dprops if g.get("functionType")]
    ck("function_canvas_node_resolved",
       all(str(g.get("canvasNode") or "").strip() for g in fn_nodes),
       f"{sum(1 for g in fn_nodes if str(g.get('canvasNode') or '').strip())}/{len(fn_nodes)}")

    doc2 = build_jsonld(sample_spec(), created_at="2026-01-01T00:00:00+08:00")
    ck("idempotent_same_input", json.dumps(doc, sort_keys=True) == json.dumps(doc2, sort_keys=True))

    # 枚举（参考格式：一个枚举类 + N 个 owl:NamedIndividual，一值一个个体，标签单值）
    enum_inds = [g for g in graph
                 if isinstance(g.get("@type"), list) and "owl:NamedIndividual" in g["@type"]]
    enum_classes = [g for g in graph if g.get("oneOf")]
    want = sum(len(g.get("enumValues") or []) for g in graph)
    ck("has_enum_class", len(enum_classes) >= 1, f"{len(enum_classes)} 个枚举类")
    ck("enum_individual_per_value", len(enum_inds) == want, f"{len(enum_inds)}/{want}")
    ck("enum_individual_single_label",
       all(isinstance(g.get("label"), dict) and len(g["label"]) == 1
           and isinstance(g.get("prefLabel"), str) and g["prefLabel"] for g in enum_inds),
       f"{sum(1 for g in enum_inds if not isinstance(g.get('prefLabel'), str))} 个个体标签不是单值")
    ck("enum_oneOf_matches_property_values",
       all(len(c["oneOf"]) == len(next((p.get("enumValues") or [] for p in graph
                                        if (p.get("range") or {}).get("@id") == c["@id"]), []))
           for c in enum_classes))
    # 枚举属性必须是指向个体的 owl:ObjectProperty；DatatypeProperty 的 range 不能指到类上
    _class_iris = {c["@id"] for c in classes}
    ck("enum_property_is_object_property",
       all(_is_a(g, "owl:ObjectProperty") for g in graph if g.get("enumValues"))
       and not any((g.get("range") or {}).get("@id") in _class_iris for g in dprops),
       f"枚举属性 {sum(1 for g in graph if g.get('enumValues'))} 个")
    # 语言标签要按书写系统判定：ASCII 枚举值不能标成 zh（参考格式是分语种的 label map）
    _cjk_inds = [g for g in (build_jsonld({"nodes": [
        {"id": "o9", "kind": "entity", "fields": {"name": "Employee", "label": "员工"}},
        {"id": "a9", "kind": "entity", "fields": {"name": "emp_status", "label": "在职状态",
                                                  "range": "enum", "enum": ["在职", "离职"]}},
    ], "edges": [{"from": "o9", "to": "a9"}]}, created_at="2026-01-01T00:00:00+08:00")
        .get("@graph") or [])
        if isinstance(g.get("@type"), list) and "owl:NamedIndividual" in g["@type"]]
    ck("enum_label_language_by_script",
       len(_cjk_inds) == 2
       and all(g["label"] == {"zh": g["prefLabel"]} for g in _cjk_inds)
       and all(g["label"] == {"en": g["prefLabel"]} for g in enum_inds
               if not _has_cjk(g.get("prefLabel") or "")),
       f"中文 {len(_cjk_inds)} / ASCII {len(enum_inds)} 个个体")

    # 对象 attributes 声明里的属性也要落图（否则本体缺属性，参考格式要求每个属性都有 domain/range）
    ck("declared_attr_exported_as_property",
       any(str(g.get("@id", "")).endswith("/property/approval_status") and "domain" in g
           and _is_a(g, "owl:DatatypeProperty", "owl:ObjectProperty") for g in graph))
    # 类型系统的行内注释不能被当成父类名
    ck("parent_comment_not_treated_as_class",
       _norm_parent("parent=  # 根类型") == "" and _norm_parent("parent=Person") == "Person"
       and _norm_parent("root") == "" and _norm_parent("Employee") == "Employee")

    # 枚举 IRI 只由取值决定：调序后同一个值的 IRI 不变（可稳定 diff）
    def _enum_ind_ids(spec_obj: dict[str, Any]) -> set[str]:
        d = build_jsonld(spec_obj, created_at="2026-01-01T00:00:00+08:00")
        return {g.get("@id") for g in d.get("@graph") or []
                if isinstance(g.get("@type"), list) and "owl:NamedIndividual" in g["@type"]}

    spec_a = {"nodes": [
        {"id": "o1", "kind": "entity", "fields": {"name": "Employee", "label": "员工"}},
        {"id": "a1", "kind": "entity", "fields": {"name": "status", "label": "在职状态",
                                                  "range": "enum", "enum": ["在职", "离职", "退休"]}},
    ], "edges": [{"from": "o1", "to": "a1"}]}
    spec_b = json.loads(json.dumps(spec_a, ensure_ascii=False))
    spec_b["nodes"][1]["fields"]["enum"] = ["退休", "在职", "离职"]
    ids_a, ids_b = _enum_ind_ids(spec_a), _enum_ind_ids(spec_b)
    ck("enum_iri_stable_across_order", ids_a == ids_b and len(ids_a) == 3, f"{len(ids_a)}/{len(ids_b)}")
    # 中文枚举值不能被折叠成一个个体（曾因 str(list) 塌缩：3 个取值只剩 v1）
    ck("cjk_enum_values_not_collapsed", len(ids_a) == 3, f"{len(ids_a)} 个个体")

    # 导入本体 → 导出：对象属性的 range 落在 fields.target_entity 上，IR 不透传，
    # 必须回读草稿还原成 owl:ObjectProperty + range=类。压成 DatatypeProperty + string
    # 会把「员工属于某部门」降级成一段字符串，分组统计与多跳查询整条断掉。
    spec_ref = {"nodes": [
        {"id": "o1", "kind": "object", "fields": {"name": "Employee", "label": "员工"}},
        {"id": "o2", "kind": "object", "fields": {"name": "Department", "label": "部门"}},
        {"id": "a1", "kind": "attr", "fields": {"name": "belongsToDept", "label": "所属部门",
                                                "range": "string",
                                                "target_entity": "Department"}},
        {"id": "a2", "kind": "attr", "fields": {"name": "hasStatus_Active", "label": "在职",
                                                "range": "string",
                                                "fact_predicate": "hasStatus",
                                                "fact_value": "Active", "value_meaning": "在职",
                                                "enum_class": "EmpStatus", "enum_value": "Active"}},
        {"id": "a3", "kind": "attr", "fields": {"name": "hasStatus_Resigned", "label": "离职",
                                                "range": "string",
                                                "fact_predicate": "hasStatus",
                                                "fact_value": "Resigned", "value_meaning": "离职",
                                                "enum_class": "EmpStatus", "enum_value": "Resigned"}},
    ], "edges": [{"from": "o1", "to": "a1"}, {"from": "o1", "to": "a2"}, {"from": "o1", "to": "a3"}]}
    g_ref = build_jsonld(spec_ref, created_at="2026-01-01T00:00:00+08:00").get("@graph") or []
    ref_prop = next((g for g in g_ref if str(g.get("@id", "")).endswith("/belongsToDept")), {})
    ck("引用型属性导出为 owl:ObjectProperty 且 range 指向类",
       _is_a(ref_prop, "owl:ObjectProperty")
       and str((ref_prop.get("range") or {}).get("@id", "")).endswith("/class/Department"),
       f"{ref_prop.get('@type')} / {(ref_prop.get('range') or {}).get('@id')}")
    enum_cls2 = [g for g in g_ref if g.get("oneOf")]
    enum_ind2 = [g for g in g_ref
                 if isinstance(g.get("@type"), list) and "owl:NamedIndividual" in g["@type"]]
    ck("枚举取值收回成一个 owl:oneOf 类 + N 个个体",
       len(enum_cls2) == 1 and len(enum_cls2[0]["oneOf"]) == 2 and len(enum_ind2) == 2,
       f"{len(enum_cls2)} 类 / {len(enum_ind2)} 个体")
    enum_prop2 = next((g for g in g_ref if str(g.get("@id", "")).endswith("/hasStatus")), {})
    ck("枚举由一个 ObjectProperty 指向（不再一值一个数据属性）",
       _is_a(enum_prop2, "owl:ObjectProperty")
       and str((enum_prop2.get("range") or {}).get("@id", "")).endswith("/class/EmpStatus")
       and not any(str(g.get("@id", "")).endswith("/hasStatus_Active") for g in g_ref),
       f"{enum_prop2.get('@type')} / {(enum_prop2.get('range') or {}).get('@id')}")
    ck("枚举属性记住画布节点（导出后可回指）",
       sorted(enum_prop2.get("canvasNode") or []) == ["a2", "a3"],
       enum_prop2.get("canvasNode"))
    ck("流水线元类不冒充 owl:Class",
       any(_is_a(g, "pdt:PipelineMetaClass") for g in graph)
       and not any(str(g.get("@id", "")).endswith("/Pipeline") and _is_a(g, "owl:Class")
                   for g in graph),
       [g.get("@id") for g in graph if str(g.get("@id", "")).endswith("/Pipeline")])

    bad = build_jsonld({"nodes": [], "edges": []})
    ck("empty_spec_no_crash", bool((bad.get("@graph") or [])), f"{len(bad.get('@graph') or [])} nodes")

    passed = sum(1 for c in checks if c["ok"])
    return {"ok": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}
