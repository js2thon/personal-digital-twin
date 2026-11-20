"""SPARQL 执行 + 示例查询集。

两条原则：
1. **只读**：只允许 SELECT / ASK / CONSTRUCT / DESCRIBE，SERVICE / LOAD / 更新类
   关键字一律拒绝——图在内存里，一次 LOAD 就能把内网地址当数据源读出去。
2. **可直接跑**：示例查询按某个本体写好，但执行前会补齐图里已有、查询里没声明的
   PREFIX，所以同一份 .rq 粘到页面、Jena、GraphDB 都能跑，不用先改前缀。
"""
from __future__ import annotations

import re
import time

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDFS

MAX_ROWS = 500
_CONSTRUCT_PREVIEW = 20000

_FORBIDDEN = re.compile(
    r"\b(SERVICE|LOAD|INSERT|DELETE|DROP|CLEAR|CREATE|ADD|MOVE|COPY|WITH|USING)\b", re.I)
_ALLOWED = ("SELECT", "ASK", "CONSTRUCT", "DESCRIBE")
# rdflib 落盘重读后会自己绑上 ns1/ns2 这种占位前缀，展示时不能露出去
_ANON_PREFIX = re.compile(r"ns\d+")


def _strip_noise(query: str) -> str:
    q = re.sub(r"#[^\n]*", " ", query)
    q = re.sub(r"\"\"\"[\s\S]*?\"\"\"", '""', q)
    q = re.sub(r'"(?:[^"\\]|\\.)*"', '""', q)
    return q


def _kind(query: str) -> str:
    body = re.sub(r"(?im)^\s*PREFIX\s+[^\n]*$", " ", _strip_noise(query)).strip()
    body = re.sub(r"(?im)^\s*BASE\s+[^\n]*$", " ", body).strip()
    m = re.match(r"(?i)(SELECT|ASK|CONSTRUCT|DESCRIBE)", body)
    return m.group(1).upper() if m else ""


def declared_prefixes(query: str) -> set[str]:
    return {m.group(1).lower() for m in re.finditer(r"(?im)^\s*PREFIX\s+([\w\-]*):", _strip_noise(query))}


def _pfx(graph: Graph, prefixes: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """一份统一的前缀映射，按命名空间长度倒序（CURIE 取最长匹配）。

    图里的 `graph.namespaces()` 不能直接用：本体落过一次盘再读回来，rdflib 会把
    原来的短前缀记成 `ns1`/`ns2`。`model._Names` 已经把这类占位名收敛成可读短名，
    这里让调用方把它传进来盖住图里的旧名；剩下模型没覆盖到的命名空间再补在后面，
    保证任何三元组都能拿到 CURIE。
    """
    pairs: dict[str, str] = {}
    for prefix, ns in graph.namespaces():
        pairs.setdefault(str(prefix), str(ns))
    named = {str(k): str(v) for k, v in (prefixes or {}).items()}
    if named:
        renamed = set(named.values())
        # 同一命名空间已有可读名，图里那批 ns1/ns2 就该让位
        pairs = {p: ns for p, ns in pairs.items()
                 if not (_ANON_PREFIX.fullmatch(p) and ns in renamed)}
        pairs.update(named)
    return sorted(pairs.items(), key=lambda kv: len(kv[1]), reverse=True)


def with_prefixes(graph: Graph, query: str, prefixes: dict[str, str] | None = None) -> str:
    """补齐图里有、查询里没写的 PREFIX；查询自己写的优先。"""
    have = declared_prefixes(query)
    lines = set()
    for prefix, ns in _pfx(graph, prefixes):
        p = str(prefix)
        if p.lower() in have:
            continue
        lines.add(f"PREFIX {p}: <{ns}>" if p else f"PREFIX : <{ns}>")
    if not lines:
        return query
    return "\n".join(sorted(lines)) + "\n" + query


def _cell(graph: Graph, term, pairs: list[tuple[str, str]]) -> dict:
    if isinstance(term, URIRef):
        label = next((str(o) for o in graph.objects(term, RDFS.label)
                      if isinstance(o, Literal)), "")
        return {"v": _curie(term, pairs), "k": "u", "label": label}
    if isinstance(term, Literal):
        return {"v": str(term), "k": "l", "lang": (term.language or ""),
                "dt": (str(term.datatype).rsplit("#", 1)[-1] if term.datatype else "")}
    return {"v": "_:" + str(term)[:10], "k": "b"}


def _curie(uri: URIRef, pairs: list[tuple[str, str]]) -> str:
    uri_s = str(uri)
    for prefix, ns_s in pairs:
        if not uri_s.startswith(ns_s):
            continue
        local = uri_s[len(ns_s):]
        if local and re.fullmatch(r"[A-Za-z_][\w.\-]*", local):
            return f"{prefix}:{local}" if prefix else f":{local}"
    return uri_s


def run(graph: Graph, query: str, row_limit: int = MAX_ROWS,
        prefixes: dict[str, str] | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "查询为空"}
    kind = _kind(query)
    if kind not in _ALLOWED:
        return {"ok": False, "error": "只支持只读查询（SELECT / ASK / CONSTRUCT / DESCRIBE）"}
    body = re.sub(r"(?im)^\s*PREFIX\s+[^\n]*$", " ", _strip_noise(query))
    m = _FORBIDDEN.search(body)
    if m:
        return {"ok": False,
                "error": f"查询里包含被禁止的关键字：{m.group(1).upper()}（本工作台只做只读查询）"}

    q = with_prefixes(graph, query, prefixes)
    pairs = _pfx(graph, prefixes)
    started = time.perf_counter()
    try:
        result = graph.query(q)
    except Exception as exc:  # noqa: BLE001  rdflib 的解析异常类型不统一
        return {"ok": False, "error": str(exc).splitlines()[0][:400], "query_used": q,
                "ms": round((time.perf_counter() - started) * 1000)}
    ms = round((time.perf_counter() - started) * 1000)

    if kind == "ASK":
        return {"ok": True, "kind": "ASK", "boolean": bool(result.answer),
                "vars": [], "rows": [], "query_used": q, "ms": ms}

    if kind in ("CONSTRUCT", "DESCRIBE"):
        out = Graph()
        for tri in result:
            out.add(tri)
        text = out.serialize(format="turtle")
        return {"ok": True, "kind": kind, "vars": [], "rows": [],
                "text": text[:_CONSTRUCT_PREVIEW],
                "text_truncated": len(text) > _CONSTRUCT_PREVIEW,
                "triples": len(out), "query_used": q, "ms": ms}

    vars_ = [str(v) for v in (result.vars or [])]
    rows, total = [], 0
    try:
        for row in result:
            total += 1
            if len(rows) >= row_limit:
                continue
            rows.append([_cell(graph, row[i], pairs) for i in range(len(vars_))])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"结果读取失败：{str(exc)[:200]}", "query_used": q, "ms": ms}
    return {"ok": True, "kind": "SELECT", "vars": vars_, "rows": rows,
            "rows_total": total, "truncated": total > len(rows),
            "query_used": q, "ms": ms}


# ---------------------------------------------------------------------------
# 示例查询集
#   员工本体套件：与 employee-ontology.ttl 严格对齐，导入该文件即可跑。
#   通用套件：只用 rdf/rdfs/owl 标准词汇，任何导入的本体都能跑。
# ---------------------------------------------------------------------------

EMP = "employee"
GEN = "generic"
EMP_NS = "http://example.org/ontology/employee#"

EXAMPLES: list[dict] = [
    # ---------------- 员工本体 ----------------
    {"id": "e01", "suite": EMP, "category": "实体刻画",
     "title": "员工档案（单行宽表）",
     "question": "张三的工号、邮箱、手机、出生日期、入职日期、性别、在职状态是什么？",
     "query": """PREFIX emp:  <http://example.org/ontology/employee#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?empId ?name ?gender ?birth ?hire ?email ?phone ?status ?dept ?position ?level
WHERE {
  ?e a emp:Employee ; emp:fullName ?name .
  FILTER (?name = "张三")
  OPTIONAL { ?e emp:employeeID ?empId }
  OPTIONAL { ?e emp:birthDate  ?birth }
  OPTIONAL { ?e emp:hireDate   ?hire }
  OPTIONAL { ?e emp:email      ?email }
  OPTIONAL { ?e emp:phone      ?phone }
  OPTIONAL { ?e emp:hasGender ?g . ?g rdfs:label ?gender . FILTER(lang(?gender) = "zh") }
  OPTIONAL { ?e emp:hasEmploymentStatus ?s . ?s rdfs:label ?status . FILTER(lang(?status) = "zh") }
  OPTIONAL { ?e emp:belongsToDepartment ?d . ?d emp:departmentName ?dept }
  OPTIONAL { ?e emp:holdsPosition ?p . ?p emp:positionName ?position . ?p emp:positionLevel ?level }
}"""},
    {"id": "e02", "suite": EMP, "category": "归属定位",
     "title": "全员一览：人 → 部门 → 职位 → 公司",
     "question": "谁在哪个部门、做什么职位、职级是什么、属于哪家公司？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?name ?dept ?deptCode ?position ?level ?company
WHERE {
  ?e emp:fullName ?name ;
     emp:belongsToDepartment ?d ;
     emp:holdsPosition ?p ;
     emp:employedBy ?c .
  ?d emp:departmentName ?dept ; emp:departmentCode ?deptCode .
  ?p emp:positionName ?position ; emp:positionLevel ?level .
  ?c emp:companyName ?company .
} ORDER BY ?name"""},
    {"id": "e03", "suite": EMP, "category": "多跳",
     "title": "三跳：员工 → 上级 → 职位 → 职级",
     "question": "李四的上级是谁？这位上级担任什么职位、什么职级？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?staff ?staffId ?boss ?bossId ?bossPosition ?bossLevel
WHERE {
  ?sub emp:fullName ?staff ; emp:employeeID ?staffId ; emp:reportsTo ?sup .
  ?sup emp:fullName ?boss ; emp:employeeID ?bossId ; emp:holdsPosition ?pos .
  ?pos emp:positionName ?bossPosition ; emp:positionLevel ?bossLevel .
}"""},
    {"id": "e04", "suite": EMP, "category": "反向查询",
     "title": "直接下属名单",
     "question": "张三手下有哪些人？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?bossName ?subName ?subId ?subDept ?subPos
WHERE {
  emp:emp001 emp:fullName ?bossName .
  ?sub emp:reportsTo emp:emp001 ; emp:fullName ?subName ; emp:employeeID ?subId .
  OPTIONAL { ?sub emp:belongsToDepartment ?d . ?d emp:departmentName ?subDept }
  OPTIONAL { ?sub emp:holdsPosition ?p . ?p emp:positionName ?subPos }
}"""},
    {"id": "e05", "suite": EMP, "category": "传递闭包",
     "title": "整条汇报链（含间接下属）",
     "question": "张三能管到的所有人是谁（含间接下级）？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT DISTINCT ?subName ?subId ?subDept
WHERE {
  ?sub emp:reportsTo+ emp:emp001 ; emp:fullName ?subName ; emp:employeeID ?subId .
  OPTIONAL { ?sub emp:belongsToDepartment ?d . ?d emp:departmentName ?subDept }
} ORDER BY ?subId"""},
    {"id": "e06", "suite": EMP, "category": "集合查询",
     "title": "部门在编名单",
     "question": "技术部有谁？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?dept ?deptCode ?name ?empId ?position ?level
WHERE {
  ?d emp:departmentName "技术部" ; emp:departmentCode ?deptCode .
  BIND("技术部" AS ?dept)
  ?e emp:belongsToDepartment ?d ; emp:fullName ?name ; emp:employeeID ?empId .
  OPTIONAL { ?e emp:holdsPosition ?p . ?p emp:positionName ?position ; emp:positionLevel ?level }
} ORDER BY ?empId"""},
    {"id": "e07", "suite": EMP, "category": "缺失检测",
     "title": "没有记录上级的人",
     "question": "哪些员工没有上级记录？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?emp ?name ?dept
WHERE {
  ?e a emp:Employee ; emp:fullName ?name .
  BIND(REPLACE(STR(?e), "^.*#", "") AS ?emp)
  OPTIONAL { ?e emp:belongsToDepartment ?d . ?d emp:departmentName ?dept }
  FILTER NOT EXISTS { ?e emp:reportsTo ?anyBoss }
} ORDER BY ?emp"""},
    {"id": "e08", "suite": EMP, "category": "聚合",
     "title": "部门人数 / 平均司龄 / 最早入职",
     "question": "每个部门多少人、平均司龄几年、最早入职是哪天？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?dept ?deptCode
       (COUNT(DISTINCT ?e) AS ?headcount)
       (ROUND(AVG(?tenure) * 10) / 10 AS ?avgTenureYears)
       (MIN(?hire) AS ?earliestHire)
WHERE {
  ?d a emp:Department ; emp:departmentName ?dept .
  OPTIONAL { ?d emp:departmentCode ?deptCode }
  OPTIONAL {
    ?e emp:belongsToDepartment ?d ; emp:hireDate ?hire .
    BIND((YEAR(NOW()) - YEAR(?hire))
         - IF(MONTH(NOW()) < MONTH(?hire)
              || (MONTH(NOW()) = MONTH(?hire) && DAY(NOW()) < DAY(?hire)), 1, 0) AS ?tenure)
  }
} GROUP BY ?dept ?deptCode ORDER BY DESC(?headcount)"""},
    {"id": "e09", "suite": EMP, "category": "聚合",
     "title": "性别分布",
     "question": "员工性别比例是多少？",
     "query": """PREFIX emp:  <http://example.org/ontology/employee#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?gender (COUNT(?e) AS ?cnt)
WHERE {
  ?e a emp:Employee ; emp:hasGender ?g .
  ?g rdfs:label ?gender . FILTER(lang(?gender) = "zh")
} GROUP BY ?gender ORDER BY DESC(?cnt)"""},
    {"id": "e10", "suite": EMP, "category": "聚合",
     "title": "在职率",
     "question": "在职人数占比多少？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT (COUNT(?e) AS ?total) (SUM(?isActive) AS ?activeCnt)
       (ROUND(10000.0 * SUM(?isActive) / COUNT(?e)) / 100.0 AS ?activePct)
WHERE {
  ?e a emp:Employee ; emp:hasEmploymentStatus ?s .
  BIND(IF(?s = emp:Active, 1, 0) AS ?isActive)
}"""},
    {"id": "e11", "suite": EMP, "category": "一致性",
     "title": "跨部门汇报",
     "question": "有没有下属和上级不在同一个部门的异常？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?subId ?subName ?subDept ?bossId ?bossName ?bossDept
WHERE {
  ?sub emp:reportsTo ?sup ; emp:fullName ?subName ; emp:belongsToDepartment ?sd .
  ?sup emp:fullName ?bossName ; emp:belongsToDepartment ?pd .
  ?sd emp:departmentName ?subDept .
  ?pd emp:departmentName ?bossDept .
  BIND(REPLACE(STR(?sub), "^.*[#/]", "") AS ?subId)
  BIND(REPLACE(STR(?sup), "^.*[#/]", "") AS ?bossId)
  FILTER (?sd != ?pd)
}"""},
    {"id": "e12", "suite": EMP, "category": "一致性",
     "title": "汇报链成环（含自环）",
     "question": "汇报关系有没有绕成环？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT DISTINCT ?emp ?name
WHERE {
  ?e emp:reportsTo+ ?e .
  ?e emp:fullName ?name .
  BIND(REPLACE(STR(?e), "^.*#", "") AS ?emp)
}"""},
    {"id": "e13", "suite": EMP, "category": "一致性",
     "title": "工号重复",
     "question": "有没有两个员工用同一个工号？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?empId (COUNT(DISTINCT ?e) AS ?cnt)
       (GROUP_CONCAT(DISTINCT ?name; separator=" / ") AS ?names)
WHERE {
  ?e a emp:Employee ; emp:employeeID ?empId .
  OPTIONAL { ?e emp:fullName ?name }
} GROUP BY ?empId HAVING (COUNT(DISTINCT ?e) > 1)"""},
    {"id": "e14", "suite": EMP, "category": "一致性",
     "title": "必填归属缺失",
     "question": "哪些员工缺部门、职位或公司？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?emp ?name ?missing
WHERE {
  ?e a emp:Employee .
  BIND(REPLACE(STR(?e), "^.*#", "") AS ?emp)
  OPTIONAL { ?e emp:fullName ?name }
  { FILTER NOT EXISTS { ?e emp:belongsToDepartment ?x } BIND("缺 belongsToDepartment" AS ?missing) }
  UNION
  { FILTER NOT EXISTS { ?e emp:holdsPosition ?x } BIND("缺 holdsPosition" AS ?missing) }
  UNION
  { FILTER NOT EXISTS { ?e emp:employedBy ?x } BIND("缺 employedBy" AS ?missing) }
} ORDER BY ?emp ?missing"""},
    {"id": "e15", "suite": EMP, "category": "约束校验",
     "title": "封闭词表越界",
     "question": "性别 / 在职状态的取值有没有跑到 oneOf 列表外面？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?emp ?name ?prop ?value
WHERE {
  ?e a emp:Employee .
  BIND(REPLACE(STR(?e), "^.*[#/]", "") AS ?emp)
  OPTIONAL { ?e emp:fullName ?name }
  {
    ?e emp:hasGender ?v .
    FILTER (?v NOT IN (emp:Male, emp:Female, emp:Other))
    BIND("hasGender" AS ?prop)
    BIND(REPLACE(STR(?v), "^.*[#/]", "") AS ?value)
  }
  UNION
  {
    ?e emp:hasEmploymentStatus ?v .
    FILTER (?v NOT IN (emp:Active, emp:Resigned, emp:OnLeave, emp:Retired))
    BIND("hasEmploymentStatus" AS ?prop)
    BIND(REPLACE(STR(?v), "^.*[#/]", "") AS ?value)
  }
}"""},
    {"id": "e16", "suite": EMP, "category": "结构缺口",
     "title": "空缺职位",
     "question": "有哪些职位没有任何员工担任？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?posId ?posName ?level
WHERE {
  ?pos a emp:Position ; emp:positionName ?posName .
  OPTIONAL { ?pos emp:positionLevel ?level }
  BIND(REPLACE(STR(?pos), "^.*[#/]", "") AS ?posId)
  FILTER NOT EXISTS { ?anyone emp:holdsPosition ?pos }
}"""},
    {"id": "e17", "suite": EMP, "category": "结构缺口",
     "title": "职位归属部门（只能靠员工反推）",
     "question": "每个职位属于哪个部门、有多少人？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?posName ?level ?deptName (COUNT(DISTINCT ?e) AS ?headcount)
WHERE {
  ?pos a emp:Position ; emp:positionName ?posName .
  OPTIONAL { ?pos emp:positionLevel ?level }
  OPTIONAL { ?e emp:holdsPosition ?pos ; emp:belongsToDepartment ?d . ?d emp:departmentName ?deptName }
} GROUP BY ?posName ?level ?deptName ORDER BY ?posName"""},
    {"id": "e18", "suite": EMP, "category": "类层级",
     "title": "foaf:Person 继承链",
     "question": "哪些类是 foaf:Person 的子类？",
     "query": """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX foaf: <http://xmlns.com/foaf/0.1/>

SELECT ?cls ?label
WHERE {
  ?cls rdfs:subClassOf* foaf:Person .
  OPTIONAL { ?cls rdfs:label ?l . FILTER(lang(?l) = "zh") }
  BIND(COALESCE(?l, REPLACE(STR(?cls), "^.*[#/]", "")) AS ?label)
} ORDER BY ?cls"""},
    {"id": "e19", "suite": EMP, "category": "传递闭包",
     "title": "部门层级树",
     "question": "部门的上下级关系是怎样的？",
     "query": """PREFIX emp: <http://example.org/ontology/employee#>

SELECT ?dept ?ancestor
WHERE {
  ?d emp:departmentName ?dept ; emp:parentDepartment+ ?a .
  ?a emp:departmentName ?ancestor .
} ORDER BY ?dept ?ancestor"""},

    # ---------------- 通用（任何本体都能跑） ----------------
    {"id": "g01", "suite": GEN, "category": "结构盘点",
     "title": "类清单（含中文名与实例数）",
     "question": "这份本体里都有哪些类？",
     "query": """PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?class ?label (COUNT(DISTINCT ?i) AS ?instances)
WHERE {
  { ?class a owl:Class } UNION { ?class a rdfs:Class }
  OPTIONAL { ?class rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }
  OPTIONAL { ?i a ?class }
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
} GROUP BY ?class ?label ORDER BY DESC(?instances) LIMIT 200"""},
    {"id": "g02", "suite": GEN, "category": "结构盘点",
     "title": "每个类的实例数",
     "question": "各类数据分别有多少条？",
     "query": """SELECT ?class (COUNT(DISTINCT ?i) AS ?cnt)
WHERE { ?i a ?class . FILTER(isIRI(?class)) }
GROUP BY ?class ORDER BY DESC(?cnt)"""},
    {"id": "g03", "suite": GEN, "category": "类层级",
     "title": "子类关系（subClassOf 边）",
     "question": "类之间的继承关系是怎样的？",
     "query": """PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?sub ?subLabel ?super ?superLabel
WHERE {
  ?sub rdfs:subClassOf ?super . FILTER(isIRI(?super))
  OPTIONAL { ?sub rdfs:label ?sl . FILTER(lang(?sl) = "zh") }
  OPTIONAL { ?super rdfs:label ?pl . FILTER(lang(?pl) = "zh") }
  BIND(COALESCE(?sl, REPLACE(STR(?sub), "^.*[#/]", "")) AS ?subLabel)
  BIND(COALESCE(?pl, REPLACE(STR(?super), "^.*[#/]", "")) AS ?superLabel)
} LIMIT 300"""},
    {"id": "g04", "suite": GEN, "category": "结构盘点",
     "title": "对象属性：domain → range",
     "question": "有哪些对象属性，定义域和值域是什么？",
     "query": """PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?p ?label ?domain ?range
WHERE {
  ?p a owl:ObjectProperty .
  OPTIONAL { ?p rdfs:label ?l . FILTER(lang(?l) = "zh") }
  OPTIONAL { ?p rdfs:domain ?d }
  OPTIONAL { ?p rdfs:range ?r }
  BIND(COALESCE(?l, REPLACE(STR(?p), "^.*[#/]", "")) AS ?label)
  BIND(COALESCE(REPLACE(STR(?d), "^.*[#/]", ""), "-") AS ?domain)
  BIND(COALESCE(REPLACE(STR(?r), "^.*[#/]", ""), "-") AS ?range)
} ORDER BY ?p LIMIT 300"""},
    {"id": "g05", "suite": GEN, "category": "结构盘点",
     "title": "数据属性清单",
     "question": "有哪些字段（数据属性），值类型是什么？",
     "query": """PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?p ?label ?range
WHERE {
  ?p a owl:DatatypeProperty .
  OPTIONAL { ?p rdfs:label ?l . FILTER(lang(?l) = "zh") }
  OPTIONAL { ?p rdfs:range ?r }
  BIND(COALESCE(?l, REPLACE(STR(?p), "^.*[#/]", "")) AS ?label)
  BIND(COALESCE(REPLACE(STR(?r), "^.*#", ""), "-") AS ?range)
} ORDER BY ?p LIMIT 300"""},
    {"id": "g06", "suite": GEN, "category": "质量体检",
     "title": "空类：没有实例也没有子类",
     "question": "哪些类是空的（既没数据也没被继承）？",
     "query": """PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?class ?label
WHERE {
  { ?class a owl:Class } UNION { ?class a rdfs:Class }
  FILTER NOT EXISTS { ?i a ?class }
  FILTER NOT EXISTS { ?child rdfs:subClassOf ?class }
  OPTIONAL { ?class rdfs:label ?l . FILTER(lang(?l) = "zh") }
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
} ORDER BY ?class LIMIT 200"""},
    {"id": "g07", "suite": GEN, "category": "质量体检",
     "title": "谓词使用频次 TOP 30",
     "question": "数据里哪些属性用得最多？",
     "query": """SELECT ?p (COUNT(*) AS ?n)
WHERE { ?s ?p ?o . FILTER(isIRI(?p)) }
GROUP BY ?p ORDER BY DESC(?n) LIMIT 30"""},
    {"id": "g08", "suite": GEN, "category": "质量体检",
     "title": "没有 rdf:type 的个体",
     "question": "哪些资源没有声明类型？",
     "query": """SELECT DISTINCT ?s
WHERE {
  ?s ?p ?o .
  FILTER(isIRI(?s))
  FILTER NOT EXISTS { ?s a ?t }
} LIMIT 50"""},
    {"id": "g09", "suite": GEN, "category": "类层级",
     "title": "跨两层的类继承（a ⊑ mid ⊑ b）",
     "question": "哪些类的继承链隔了两层？",
     "query": """PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

SELECT ?a ?mid ?b
WHERE {
  ?a rdfs:subClassOf ?mid . ?mid rdfs:subClassOf ?b .
  FILTER(isIRI(?a) && isIRI(?mid) && isIRI(?b))
  FILTER(?a != ?b)
} LIMIT 100"""},
    {"id": "g10", "suite": GEN, "category": "规模统计",
     "title": "三元组总数",
     "question": "这份图里有几条三元组？",
     "query": """SELECT (COUNT(*) AS ?triples) WHERE { ?s ?p ?o }"""},
]


def examples_for(model: dict) -> list[dict]:
    """按导入的本体挑套件：默认都给，但把明显跑不出结果的员工套件标出来。

    判定看命名空间而不是前缀名——同一个本体，有人写 `emp:`、有人写 `:`，
    前缀叫什么由文件自己决定，URI 才是恒定的。
    """
    has_emp = (EMP_NS in (model.get("prefixes") or {}).values()
               or any((c.get("uri") or "").startswith(EMP_NS)
                      for c in model.get("classes") or []))
    out = []
    for ex in EXAMPLES:
        item = dict(ex)
        item["runnable"] = True if ex["suite"] == GEN else has_emp
        out.append(item)
    return out
