"""基于「当前草稿本体」的问答：自然语言 → SPARQL → 图上的答案。

和 ``kg/qa.py`` 的分工：那一套建立在**已导入并落盘的知识图谱会话**上
（``KgSession`` + ``.runtime/kg/<id>/graph.ttl``），本模块建立在**建模画布当前草稿**上——
把 spec 按「导出」同一口径（``jsonld.build_jsonld``）编译成图，再补实例数据。

为什么不复用 ``/ontology/modelling/ask-data``（``nlquery``）：那条路要求本体先声明
「语义映射 → 业务库表」，缺映射就拒答；用户要的是「pipeline 跑完就地提问」，
答案的事实来源应该是**本草稿编译出的图**，不该被语义层配没配好卡住。

三级降级照搬 ``kg/qa.py``，保证任何情况下都有可解释的结果：
1. LLM 生成 SPARQL → 执行；
2. 执行失败 → 把报错回喂模型改一次；
3. 模型不可用 / 两次都失败 → 关键词模板兜底（离线可用）。

术语写法：草稿编译出的 IRI 形如 ``.../class/Department``，局部名带斜杠**不是合法 CURIE**，
所以本模块统一用**完整 IRI + 尖括号**，不假装有前缀。模板生成的查询也按这个口径写，
避免出现 ``?x a pdt:class/Department`` 这种必然解析失败的写法。
"""
from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

from rdflib import Graph, Literal, RDF, RDFS, URIRef
from rdflib.namespace import OWL, SKOS

from app.modules.llm.client import llm_client
from app.modules.ontology.kg import model as kg_model
from app.modules.ontology.kg import query as kg_query
from app.modules.ontology.modelling import sparql as SP

# 导出时顺带写进去的通用词表：不是业务概念，不进问答上下文，也不参与类名匹配
_NOISE_NS = (
    "http://purl.org/dc/terms/",
    "http://www.w3.org/2004/02/skos/core#",
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
)

# ---------------------------------------------------------------- 本体准绳
# 唯一来源：``web/ontology-design/personnel_ontology.ttl``（owl:versionInfo 1.0.0）。
# 下面每一条都能在那份文件里逐行对到，**不从数据反推**：数据里有什么，不构成本体
# 应该有什么。改本体就改这里，改这里必须同步 TTL —— 两边不一致时以 TTL 为准。
#
# 为什么要把 TTL 抄进代码：这套问答的准绳是本体，不是画布。画布上存在 ``Project`` /
# ``collaborates_with`` 这类本体未定义的概念，也有 ``DeptCnNameLv8`` 这种源数据多出来的
# 层级；如果照单全收，答案会「看起来查到了」但和本体口径不一致。抄成常量之后，
# 「哪些是本体认的」是显式判断，不是隐含的放任。
_TTL_CLASSES: dict[str, str] = {
    "Employee": "员工",
    "Department": "部门",
    "OrganizationalUnit": "组织单元",
    "DepartmentLevel1": "一级部门",
    "DepartmentLevel2": "二级部门",
    "DepartmentLevel3": "三级部门",
    "DepartmentLevel4": "四级部门",
    "DepartmentLevel5": "五级部门",
    "DepartmentLevel6": "六级部门",
    "DepartmentLevel7": "七级部门",
}

# 局部名 -> (domain, range, 附加公理, 中文 label)
# ``manages`` / ``isManagerOfDepartment`` 在 TTL 里**故意不写** domain/range，只给
# owl:inverseOf。它们的 domain/range 是推出来的（逆属性的 range 就是 domain），
# 这里直接写推导结果并标注来源，否则模型无从判断这两个属性能用在哪。
_TTL_OBJ_PROPS: dict[str, tuple[str, str, str, str]] = {
    "belongsToDepartment": ("Employee", "Department", "", "所属部门"),
    "hasParentDepartment": ("Department", "Department", "Transitive", "上级部门"),
    "hasDepartmentManager": ("Department", "Employee", "", "部门负责人"),
    "hasLineManager": ("Employee", "Employee", "Irreflexive", "直线主管"),
    "manages": ("Employee", "Employee", "inverseOf:hasLineManager", "管理"),
    "isManagerOfDepartment": ("Employee", "Department",
                              "inverseOf:hasDepartmentManager", "负责部门"),
}

# 局部名 -> (domain, 中文 label)
_TTL_DATA_PROPS: dict[str, tuple[str, str]] = {
    "employeeId": ("Employee", "员工ID"),
    "employeeName": ("Employee", "员工姓名"),
    "hrStatus": ("Employee", "在职状态"),
    "departmentName": ("Department", "部门名称"),
    "deptCnNameLv1": ("Employee", "一级部门名称"),
    "deptCnNameLv2": ("Employee", "二级部门名称"),
    "deptCnNameLv3": ("Employee", "三级部门名称"),
    "deptCnNameLv4": ("Employee", "四级部门名称"),
    "deptCnNameLv5": ("Employee", "五级部门名称"),
    "deptCnNameLv6": ("Employee", "六级部门名称"),
    "deptCnNameLv7": ("Employee", "七级部门名称"),
    "departmentManagerName": ("Department", "部门负责人姓名"),
    "lineManagerName": ("Employee", "直线主管姓名"),
}

# TTL ``:Employee rdfs:comment`` 里的字段映射说明，原文抄录（源字段 -> 本体属性）。
_TTL_FIELD_MAP: dict[str, str] = {
    "emplid": "employeeId",
    "emp_name": "employeeName",
    "hr_status": "hrStatus",
    "dept_cn_name": "belongsToDepartment",   # 同名字符串另可落 :departmentName
    "dept_manager_name": "hasDepartmentManager",
    "line_rep_name": "hasLineManager",
    **{"dept_cn_name_lv%d" % i: "deptCnNameLv%d" % i for i in range(1, 8)},
}

# 落库字段名 -> 画布上**已声明**的谓词局部名。
# 这是纯粹的名字差异，不是概念差异：本体定的是「字段 → 属性」的语义（上面那张表），
# 但语义层实际给出的字段叫 full_name / department，画布声明的谓词叫 emp_name /
# dept_cn_name。不先对齐这一层，``add_instances`` 按局部名找属性会全部落空并**静默丢弃**
# —— 27 个员工的姓名和部门一个都进不了图，而图看起来「有 28 个实例」，查姓名却是 0 行。
_FIELD_ALIAS: dict[str, str] = {
    "full_name": "emp_name",
    "name": "emp_name",
    "cn_name": "emp_name",
    "department": "dept_cn_name",
    "dept_name": "dept_cn_name",
}

# 画布谓词 -> 它对应 TTL 的哪个属性（给模型/模板讲口径用）。
# 值一律是**同类**的 TTL 局部名：画布上是字符串属性的，对应数据属性；是关系边的，
# 对应对象属性。混着写会让模型把数据属性当对象属性去 join（或反过来）。
# 空串 = TTL 未定义这个概念，不得当作本体术语来答。
#
# ``dept_cn_name`` 的 TTL 口径其实是「:belongsToDepartment / :departmentName」二选一：
# 落在员工身上、值是部门名 → 数据属性 :departmentName；要连到部门个体 → 对象属性
# :belongsToDepartment。画布上它是字符串，所以这里取数据属性名，对象属性那条由
# ``_PRED_CONCEPT`` 单独承担。
_TTL_OF_DRAFT: dict[str, str] = {
    "emplid": "employeeId",
    "emp_name": "employeeName",
    "hr_status": "hrStatus",
    "dept_cn_name": "departmentName",
    "dept_manager_name": "departmentManagerName",
    "line_rep_name": "lineManagerName",
    "reports_to": "hasLineManager",
    "hasDepartmentManager": "hasDepartmentManager",
    "hasParentDepartment": "hasParentDepartment",
    "collaborates_with": "",
}

# 图里没有本体命名空间时的兜底（实测草稿导出总带这个 ns，兜底只为不抛异常）。
_TTL_NS = "https://pdt.example.org/ontology/employee-organization"
_TTL_XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

# 落库谓词局部名 → 本体属性局部名：**把数据搬到本体的名字上**，而不是反过来。
# 只收 1:1 的纯改名；``dept_cn_name`` 不走这张表，它要变成「员工 → 部门个体」的边
# （见 ``_reify_departments``），改名会把字符串硬塞给 range=Department 的
# ``belongsToDepartment``，反而更不守本体。
_TTL_PRED_MIGRATE: dict[str, str] = {
    "emp_name": "employeeName",
    "emplid": "employeeId",
    "hr_status": "hrStatus",
    "dept_manager_name": "departmentManagerName",
    "line_rep_name": "lineManagerName",
    "reports_to": "hasLineManager",
    **{"dept_cn_name_lv%d" % i: "deptCnNameLv%d" % i for i in range(1, 8)},
}

_SYSTEM = """你是知识图谱问答引擎里的 SPARQL 1.1 生成器。
规则：
1. 只输出 SELECT 或 ASK 查询，禁止任何更新语句（INSERT/DELETE/LOAD/DROP/CLEAR）。
2. 术语（类 / 属性）**必须**用下面给出的完整 IRI，并用尖括号包裹，例如 <https://x/class/Employee>。
   不要发明不存在的 IRI，也不要自己编前缀缩写。
3. 类名 / 属性名可能是中文，SPARQL 里只出现 IRI；中文取值用 rdfs:label 取。
4. 对象属性可以走属性路径：层级/传递类问题用 <prop>+（传递闭包），反向用 ^<prop>。
5. 结果变量取有意义的名字（?name、?dept、?cnt），不要用 ?o1 ?o2。
6. 默认加 LIMIT 200（聚合查询除外）。
7. 锚定具体实例（人名 / 部门名）时，**禁止**用 `rdfs:label "张三"` 这种等值匹配：
   实例标签常带括号里的英文名或工号（如「张三 (Zhang San)」），等值一定落空。
   必须写 `?e rdfs:label ?l . FILTER(CONTAINS(STR(?l), "张三"))`。
8. 只能拿上面列出的**对象属性 / 数据属性**当谓词。派生函数（fn_derive_*）不是谓词，
   写进去必然 0 行。
9. 如果问题要的指标在本体里**根本没有对应属性**（例如司龄、绩效分数、薪酬），
   不要用别的属性凑一个数出来，输出 `{"sparql": "", "intent": "本体缺少XX属性", "missing": true}`。
10. 只输出 JSON：{"sparql": "<查询>", "intent": "<一句话说明你查了什么>"}
"""


# ---------------------------------------------------------------- 术语与工具
def _local(term: Any) -> str:
    """IRI / CURIE → 局部名（``.../class/Department`` → ``Department``）。"""
    t = str(term or "").strip()
    if not t:
        return ""
    return re.split(r"[#/:]", t.rstrip("/"))[-1]


def _iri(term: Any) -> str:
    """模型/模板给的术语 → SPARQL 里的写法：全 IRI 加尖括号，CURIE 原样。"""
    t = str(term or "").strip()
    if not t:
        return ""
    if t.startswith("<") and t.endswith(">"):
        return t
    if "://" in t or t.startswith("urn:"):
        return f"<{t}>"
    return t


def _is_noise(term: Any) -> bool:
    t = str(term or "")
    return any(t.startswith(ns) for ns in _NOISE_NS)


# 画布快照自带的系统元数据谓词：不是业务概念，不该出现在给模型的 prompt 里。
# 它们每条数据都带着（specHash / irHash / stageStatus 挂在节点上），所以
# `_prune_undeclared` 按「有数据就留」的规则不会摘掉它们 —— 那是刻意的，
# 摘了会丢打包信息。但也**不能**把它们列进「本体未定义的谓词」：
# 模型看到 specHash 会以为是个可查的属性，而它跟「人员-组织」毫无关系。
_META_PREDS = {
    "canvasNode", "contentLanguage", "factEntityType", "factPredicate",
    "generatedFrom", "irHash", "note", "specHash", "stageStatus",
    "createdAt", "updatedAt", "edgeKind", "sourceRef", "targetRef",
}


def _is_func(term: Any) -> bool:
    """派生函数节点（``.../function/fn_derive_*``）不是谓词。

    画布上带 161 个 ``func`` 节点，导出时它们也带 ``rdfs:domain``，于是被
    ``_app_props`` 当成对象属性交给模型；模型据此写出
    ``?emp <.../function/fn_derive_line_rep_name_is_manager> ?manager`` —— 这个 IRI
    在数据里**一条三元组都不会有**，查询语法全对、结果恒为 0 行。这是「模型答不准」
    里最隐蔽的一类：不是模型不懂 SPARQL，是我们给了它一套假谓词。
    """
    return "/function/" in str(term or "") or str(term or "").startswith("urn:pdt:fn:")


def _ttl_known(local: str) -> bool:
    """这个局部名是不是本体认的术语（类 / 对象属性 / 数据属性三者之一）。"""
    return (local in _TTL_CLASSES or local in _TTL_OBJ_PROPS
            or local in _TTL_DATA_PROPS)


def _draft_pred(field: str) -> str:
    """落库字段名 → 画布上已声明的谓词局部名（空串 = 本体未定义，必须丢弃）。

    两层里的第一层，只解决**名字差异**：本体定的是「源字段 → 属性」的语义
    （``_TTL_FIELD_MAP``），而语义层实际给的字段叫 ``full_name`` / ``department``，
    画布声明的谓词叫 ``emp_name`` / ``dept_cn_name``。

    这一层不做会怎样：``add_instances`` 拿局部名去图上找属性，``full_name`` 找不到
    → ``continue`` 静默丢弃 → 27 个员工的姓名和部门一条都进不了图，而
    ``instances_given=28`` 看起来完全正常，查姓名却是 0 行。

    这一层**只做别名，不做猜测**：``level`` / ``due_date`` / ``approval_status``
    在 TTL 里没有对应属性，就返回空串让上层丢弃——改个近似的名字硬塞进去，就是
    按数据反推本体，正是要禁止的做法。
    """
    k = _local(str(field or ""))
    if not k:
        return ""
    return _FIELD_ALIAS.get(k, k)


# 落库谓词 → 本体概念（``web/ontology-design/personnel_ontology.ttl`` 为准绳）。
#
# 为什么要这张表：TTL 里「直线主管」的对象属性是 ``:hasLineManager``
# （``rdfs:label "直线主管"@zh``，``owl:IrreflexiveProperty``），映射说明写着
# ``line_rep_name → :hasLineManager / :lineManagerName``。语义层历史关系类型却落成了
# ``reports_to``（``graph.RELATION_TYPES`` 里 label「汇报给」）——**同一业务概念的
# 两个落库名**。按 TTL 修（而不是按数据改 TTL）的做法是：**语义按 TTL 解释、
# 取值用落库谓词**。不这么对齐，「张三的直线主管是谁」在图上明明有
# ``张三 --reports_to--> 李四`` 这条边，却一个候选属性都命中不了。
_PRED_CONCEPT: dict[str, tuple[str, tuple[str, ...]]] = {
    # reports_to 是落库名、hasLineManager 是本体名；装载时数据已搬到本体名上，
    # 这里两个都留着：老数据/老快照里仍可能出现 reports_to，缺一个就命中不了。
    "hasLineManager": ("hasLineManager", ("直线主管", "直属上级", "汇报给",
                                          "汇报对象", "主管")),
    "reports_to": ("hasLineManager", ("直线主管", "直属上级", "汇报给", "汇报对象", "主管")),
    "belongs_to_department": ("belongsToDepartment", ("所属部门", "所在部门", "归属部门")),
    "belongsToDepartment": ("belongsToDepartment", ("所属部门", "所在部门", "归属部门")),
    "manages": ("manages", ("管理", "下属", "带的人")),
    "hasDepartmentManager": ("hasDepartmentManager", ("部门负责人", "部门主管")),
    "hasParentDepartment": ("hasParentDepartment", ("上级部门", "父部门", "上级组织")),
}

# 属性局部名 -> **口语问法**（只给模板的 `_prop_chain` 用，不进 `_aka`）。
#
# 为什么不并进 `_aka`：`_aka` 是「本体口径别名」，会原样写进给模型的名词解释里
# （「reports_to 旁边写着直线主管」）。把「哪个部门」这种半句话塞进去，
# 解释就变成了语法碎片，模型会照着它造句。两件事分开：
# - `_aka`          = 这个属性在本体里叫什么（给模型看）；
# - `_PROP_SURFACE` = 用户会怎么问它（给模板匹配看）。
#
# 为什么需要：TTL 的 label 是「所属部门」，而人会说「张三**属于哪个部门**」。
# 三个字一个都对不上，问句明明在本体能力范围内，却只能回「未识别到要问的属性」——
# 把「我没认出来」说成「本体没定义」，是替本体认错。
_PROP_SURFACE: dict[str, tuple[str, ...]] = {
    "belongsToDepartment": ("哪个部门", "什么部门", "属于哪个部门", "在哪个部门", "归属"),
    "hasLineManager": ("向谁汇报", "汇报给谁", "上司", "上级主管"),
    "manages": ("下属", "手下", "带的人", "管的人"),
    "hasDepartmentManager": ("谁是负责人", "负责人是谁", "谁负责"),
}


def _aka(p: dict) -> tuple[str, ...]:
    """属性的「本体口径别名」：TTL 里的 label，以及同一概念的其他叫法。

    既是给模板的匹配词，也是给模型的名词解释——模型看到 ``reports_to`` 旁边写着
    「直线主管」，就不会拿它去答「谁向谁汇报」以外的问题。
    """
    hit = _PRED_CONCEPT.get(_local(p.get("curie")))
    return hit[1] if hit else ()


def _surface(p: dict) -> tuple[str, ...]:
    """属性的**口语问法**（只用于模板匹配，不写进给模型的解释）。"""
    return _PROP_SURFACE.get(_local(p.get("curie")), ())


def _concept(p: dict) -> str:
    """该属性在 TTL 里对应的概念名（无对齐时返回空串）。"""
    hit = _PRED_CONCEPT.get(_local(p.get("curie")))
    return hit[0] if hit else ""


def _canvas_classes(model: dict) -> list[dict]:
    """画布上的全部业务类（排除通用词表）。只用于**如实报告画布规模**，不参与作答。"""
    return [c for c in (model.get("classes") or []) if not _is_noise(c.get("uri"))]


def _app_classes(model: dict) -> list[dict]:
    """参与匹配与作答的类：**只收本体定义的那 10 个**。

    为什么不照单全收：画布上还有 Company / Person / Manager / Project / Risk / Team …
    共 12 个类，它们是早期演示对象和 pipeline 顺带带出来的，``personnel_ontology.ttl``
    里没有。收进来会有两个坏处——
    ① 问「有几个项目」会匹配到 Project 并答「1 个」：数字是真的，但它不属这份本体
       定义的范围，用户拿到的是一个口径不明的数字；
    ② 类名匹配按最长子串抢先后，「人」（Person 的别名）会抢走本该属于「员工」的问句。

    剔除后这类问题走 ``_template`` 的越界说明分支，回「本体未定义该概念」——
    比给一个来源不明的数字可靠。
    """
    return [c for c in _canvas_classes(model)
            if _local(c.get("curie")) in _TTL_CLASSES]


def _extra_classes(model: dict) -> list[dict]:
    """画布上有、但本体未定义的类：不参与作答，只在需要时如实声明。"""
    return [c for c in _canvas_classes(model)
            if _local(c.get("curie")) not in _TTL_CLASSES]


def _ttl_name_of(p: dict) -> str:
    """画布谓词 → 它对应的 TTL 属性名（对不上返回空串 = 本体未定义）。

    注意这里只做**命名对齐**：``reports_to`` 是 ``hasLineManager`` 的落库名，
    ``emp_name`` 对应 ``employeeName``。属性是对象属性还是数据属性，仍以图里
    实际声明为准（``model["object_props"]`` / ``data_props``），不按 TTL 名判断——
    ``dept_cn_name`` 的 TTL 名是对象属性 ``belongsToDepartment``，但画布上它是字符串
    属性，按 TTL 名当对象属性会让模型写出「员工 → 部门个体 → 部门名」的错误链。
    """
    local = _local(p.get("curie"))
    if local in _TTL_OF_DRAFT:
        return _TTL_OF_DRAFT[local]
    return local if _ttl_known(local) else ""


def _app_props(model: dict) -> list[dict]:
    """业务属性：排除通用词表与派生函数，且**必须有 domain 声明**——没有 domain 的
    属性（导出时顺带写的 pdt:canvasNode 之类）回答不出「谁有这个属性」。

    两个例外必须收进来，否则「数据里有、问不出来」会变成永久缺陷：

    - **图里真有用量的孤儿对象属性**：``reports_to`` / ``collaborates_with`` 是语义层
      历史关系类型，导出时只写了 ``owl:ObjectProperty`` + ``uses``，没有
      ``rdfs:domain/range``（note 原话：「当前画布未绑定 source/target 对象，故不声明」）。
      一律按 domain 过滤就等于把「张三 --reports_to--> 李四」这条真实边判成不存在。
      收进来后由「图上验一验」（锚点个体真有这条边才认）兜住，不会凭空作答。
    - **派生函数**（``/function/``）永远不收：它们不是谓词，图里没有三元组。
    """
    out = []
    all_p = (model.get("object_props") or []) + (model.get("data_props") or [])
    # 按局部名索引，供 _eff_axioms 找逆属性（manages → hasLineManager）
    by_local: dict[str, dict] = {}
    for q in all_p:
        by_local.setdefault(_local(q.get("curie")), q)

    for p in all_p:
        if _is_noise(p.get("uri")) or _is_func(p.get("uri")):
            continue
        if not _ttl_name_of(p):
            # TTL 未定义的属性（authored / believes / knows / owns / works_at /
            # collaborates_with …）。它们**留在图里**，只是不进「可查询面」：
            # 让模型看见它们，等于默许用本体外的谓词作答，答案和本体口径就分叉了。
            continue
        # manages / isManagerOfDepartment 的 domain/range 由 inverseOf 推导补上，
        # 否则这两条会掉进下面「没有 domain 就过滤」的分支，明明本体定义了却问不出来。
        p = _eff_axioms(p, by_local)
        if not (p.get("domain") or []):
            # 只有「声明过 + 图里确实在用」的孤儿属性才放行；数据属性不放行，
            # 它们没有 domain 时多半是导出噪声（canvasNode / irHash 那一批）。
            used = bool(p.get("declared")) and bool(p.get("uses"))
            in_obj = str(p.get("uri")) in {str(x.get("uri")) for x in
                                           (model.get("object_props") or [])}
            if not (used and in_obj):
                continue
        out.append(p)
    return out


def _cls_label(c: dict) -> str:
    return str(c.get("label") or _local(c.get("curie")) or "?")


def _ancestors(model: dict) -> dict[str, set[str]]:
    """类 IRI → 自身 + 全部祖类 IRI（传递闭包）。

    ``build_model`` 的 ``parent_uris`` 只给**直接**父类，画布上的多级继承要迭代收敛，
    否则「部门 > 一级部门 > 二级部门」这种三层结构会漏掉最上面一层。
    """
    up: dict[str, set[str]] = {}
    for c in _app_classes(model):
        uri = str(c.get("uri") or "")
        if not uri:
            continue
        up.setdefault(uri, set()).add(uri)
        for p in c.get("parent_uris") or []:
            up[uri].add(str(p))
    for _ in range(len(up) + 1):
        changed = False
        for uri, seen in up.items():
            add: set[str] = set()
            for p in list(seen):
                add |= up.get(p, {p})
            if not add <= seen:
                seen |= add
                changed = True
        if not changed:
            break
    return up


def _under(uri: str, ancestor: str, up: dict[str, set[str]]) -> bool:
    """``uri`` 是不是 ``ancestor`` 本身或其子孙（子类）。"""
    return ancestor in up.get(uri, {uri})


def _props_of(props: list[dict], cls: dict, up: dict[str, set[str]]) -> list[dict]:
    """「这个类有哪些属性」——**两个方向都要收**，只收一边都会漏。

    - 向下（声明在子类上）：真实草稿里父类的属性常常只写在某个子类上（部门名 / 层级 /
      负责人 / 人数都只声明在 ``DepartmentLevel1``），只按 ``rdfs:domain == Department``
      精确匹配会全漏：用户问「部门有哪些属性」只答出 5 个，实际有 9 个。
    - 向上（声明在父类上）：反过来问「一级部门里谁负责」时，负责人属性是声明在
      ``Department`` 上的，只看子孙方向会一个都匹配不到，于是一个「取某个关系的值」
      的问题被当成「列实例」，答出一串部门名——数据没错，但完全没答到点上。
    """
    me = str(cls.get("uri") or "")
    out = []
    for p in props:
        doms = [str(d) for d in (p.get("domain") or [])]
        if any(_under(d, me, up) or _under(me, d, up) for d in doms):
            out.append(p)
    return out


def _dedup_props(ps: list[dict]) -> list[dict]:
    """按 IRI 去重：同一个属性只算一次。

    草稿里同一属性常被反复声明（父类一次、子类一次，自动补齐时又生成一次），
    Python 侧就会拿到多条同 curie 的记录。把这些条数当成「共 N 个属性」报出去，
    就会出现「提示说共 4 个、结果只有 3 行」——差的那一个不是漏数据，是同一条数了两遍。
    """
    seen, out = set(), []
    for p in ps:
        k = str(p.get("curie") or "")
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _owner_label(p: dict, cls: dict) -> str:
    """属性声明在子类上时标出归属类名，免得用户以为这些属性都长在父类上。"""
    me = str(cls.get("uri") or "")
    doms = [str(d) for d in (p.get("domain") or [])]
    if not doms or me in doms:
        return ""
    return _local(doms[0])


def _lit(text: str) -> str:
    """Python 字符串 → SPARQL 字符串字面量（含转义）。"""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _prop_label(p: dict) -> str:
    return str(p.get("label") or _local(p.get("curie")) or "?")


class _DraftSession:
    """和 ``KgSession`` 同形状的最小实现——问答引擎只用到 ``graph`` / ``model()`` / ``lock``。

    不落盘：草稿每次提问现编译，图只活在这一个请求里，问答不会把临时数据写到磁盘。
    """

    def __init__(self, graph: Graph):
        self.graph = graph
        self._model: dict | None = None
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def model(self) -> dict:
        with self._lock:
            if self._model is None:
                self._model = kg_model.build_model(self.graph)
            return self._model


# ---------------------------------------------------------------- 本体公理注入
def _onto_base(graph: Graph) -> str:
    """图的本体命名空间（``.../employee-organization``，不带 ``/class`` 尾巴）。"""
    ns = SP._ontology_ns(graph).rstrip("/")
    if ns.endswith("/class"):
        ns = ns[: -len("/class")]
    return ns or _TTL_NS


def _add_if_new(graph: Graph, triple: tuple, tally: list) -> None:
    """只在图里没有时才加，顺手累计「补了多少条」。"""
    if triple not in graph:
        graph.add(triple)
        tally[0] += 1


def _ensure_ttl_label(graph: Graph, uri, zh: str, tally: list,
                     retagged: list) -> None:
    """保证该 IRI 上有 TTL 声明的中文 ``rdfs:label``，并纠正「英文名被标成 @zh」。

    为什么不能只做「没有 label 才补」：TTL 第 30 行写的是
    ``:Department rdfs:label "部门"@zh , "Department"@en``，而画布导出把英文名
    塞进了 ``@zh`` 槽位——``Department rdfs:label "Department"@zh``。于是
    「有 label」成立，TTL 的「部门」永远补不进来。后果不是显示难看，是**匹配不到**：
    ``_match_classes`` 拿 label / 局部名 / 中文别名去问句里找，「部门有多少个」
    三个字在 ``Department`` 这串 ASCII 里一个都找不到，直接掉到兜底类清单。
    而 ``Employee`` 能匹配纯属侥幸——画布恰好另外带了 ``skos:altLabel "员工"``。

    「英文名标成 @zh」不只是缺个标签，它本身是个错误断言：那句话在说
    「Department 这个词是中文」。所以把纯 ASCII 的 ``@zh`` 改标成 ``@en``
    （英文问句仍然命中，只是语言标对了），再按 TTL 补真正的中文名。
    """
    if not zh:
        return
    # 纠正语言标记：@zh 槽位里放着不含 CJK 的串，一律改标 @en
    for lit in list(graph.objects(uri, RDFS.label)):
        if not isinstance(lit, Literal):
            continue
        if str(lit.language or "") != "zh":
            continue
        text = str(lit).strip()
        if not text or re.search(r"[\u4e00-\u9fff]", text):
            continue
        graph.remove((uri, RDFS.label, lit))
        tally[0] += 1
        _add_if_new(graph, (uri, RDFS.label, Literal(text, lang="en")), tally)
        retagged.append("%s:%s->@en" % (_local(uri), text))
    if zh not in {str(x).strip() for x in graph.objects(uri, RDFS.label)
                  if isinstance(x, Literal)}:
        _add_if_new(graph, (uri, RDFS.label, Literal(zh, lang="zh")), tally)


def _ttl_overlay(graph: Graph) -> dict:
    """把 ``personnel_ontology.ttl`` 的 TBox 公理补进图（不动任何数据三元组）。

    为什么必须补：画布导出只留下了**名词**（类名、属性名、两条 domain/range），
    公理部分全丢。实测这份草稿导出后，图里 ``owl:TransitiveProperty`` /
    ``owl:IrreflexiveProperty`` / ``owl:inverseOf`` **各 0 条**，
    ``hasLineManager`` / ``belongsToDepartment`` / ``manages`` /
    ``isManagerOfDepartment`` 四个对象属性**连声明都没有**。于是本体白纸黑字写着的
    三件事，在问答链路里一点依据都没有：

    - 「上级的上级也算上级」（``hasParentDepartment`` Transitive）；
    - 「自己不是自己的主管」（``hasLineManager`` Irreflexive）；
    - 「某人负责某部门 ⟺ 该部门负责人是某人」（``isManagerOfDepartment`` inverseOf）。

    处置为什么是「补图」而不是「改问答」：本体是唯一准绳，画布只是它的一次渲染，
    渲染丢了公理不等于公理不存在。补进去之后，所有下游（模型提示、模板、
    SPARQL 属性路径）看到的是同一套公理，不用各自记硬编码。

    为什么这不是「按数据反推本体」：补的每一条都能在 TTL 里逐行对到，数据一条没动。

    ``manages`` / ``isManagerOfDepartment`` 的 domain/range **故意不补**——TTL 里就是
    没写（只给了 inverseOf）。补了等于替本体做决定；它们的 domain/range 由
    ``_eff_axioms`` 在读取时按逆属性推导，并标注「推出来的」。
    """
    ns = _onto_base(graph)
    tally = [0]
    retagged: list = []
    info: dict[str, Any] = {"ttl_ns": ns, "ttl_axioms_added": 0,
                            "ttl_classes_created": [], "ttl_props_created": []}

    # ---- 1) 10 个类：同名类优先复用，避免造出两套 Employee ----
    existing_cls: dict[str, Any] = {}
    for c in graph.subjects(RDF.type, OWL.Class):
        existing_cls.setdefault(_local(c), c)
    cls_uri: dict[str, Any] = {}
    for name, label in _TTL_CLASSES.items():
        uri = existing_cls.get(name)
        if uri is None:
            uri = URIRef("%s/class/%s" % (ns, name))
            info["ttl_classes_created"].append(name)
        cls_uri[name] = uri
        _add_if_new(graph, (uri, RDF.type, OWL.Class), tally)
        _ensure_ttl_label(graph, uri, label, tally, retagged)

    # DepartmentLevel1..7 rdfs:subClassOf :Department（TTL 七条，逐条补）
    for i in range(1, 8):
        _add_if_new(graph, (cls_uri["DepartmentLevel%d" % i],
                            RDFS.subClassOf, cls_uri["Department"]), tally)
    # OrganizationalUnit owl:equivalentClass :Department
    _add_if_new(graph, (cls_uri["OrganizationalUnit"],
                        OWL.equivalentClass, cls_uri["Department"]), tally)

    # ---- 2) 6 个对象属性 ----
    existing_obj: dict[str, Any] = {}
    for p in graph.subjects(RDF.type, OWL.ObjectProperty):
        existing_obj.setdefault(_local(p), p)
    existing_dt: dict[str, Any] = {}
    for p in graph.subjects(RDF.type, OWL.DatatypeProperty):
        existing_dt.setdefault(_local(p), p)
    prop_uri: dict[str, Any] = {}

    for name, (dom, rng, ax, label) in _TTL_OBJ_PROPS.items():
        uri = existing_obj.get(name)
        if uri is None:
            uri = URIRef("%s/property/%s" % (ns, name))
            info["ttl_props_created"].append(name)
        prop_uri[name] = uri
        _add_if_new(graph, (uri, RDF.type, OWL.ObjectProperty), tally)
        _ensure_ttl_label(graph, uri, label, tally, retagged)
        if ax.startswith("inverseOf:"):
            inv_name = ax.split(":", 1)[1]
            inv_uri = prop_uri.get(inv_name) or existing_obj.get(inv_name)
            if inv_uri is not None:
                _add_if_new(graph, (uri, OWL.inverseOf, inv_uri), tally)
        else:
            # domain/range 是 TTL 直写的，照抄；manages 那两条走上面的 inverseOf 分支
            _add_if_new(graph, (uri, RDFS.domain, cls_uri[dom]), tally)
            _add_if_new(graph, (uri, RDFS.range, cls_uri[rng]), tally)
        if ax == "Transitive":
            _add_if_new(graph, (uri, RDF.type, OWL.TransitiveProperty), tally)
        if ax == "Irreflexive":
            _add_if_new(graph, (uri, RDF.type, OWL.IrreflexiveProperty), tally)

    # ---- 3) 13 个数据属性 ----
    # 只补声明与 domain/range；有同名落库谓词的直接复用（不造第二个 IRI），
    # 真正把数据搬过来是下一步 ``_align_predicates`` 的事。
    for name, (dom, label) in _TTL_DATA_PROPS.items():
        uri = existing_dt.get(name)
        if uri is None:
            uri = URIRef("%s/property/%s" % (ns, name))
            info["ttl_props_created"].append(name)
        prop_uri[name] = uri
        _add_if_new(graph, (uri, RDF.type, OWL.DatatypeProperty), tally)
        _add_if_new(graph, (uri, RDFS.domain, cls_uri[dom]), tally)
        _add_if_new(graph, (uri, RDFS.range, URIRef(_TTL_XSD_STRING)), tally)
        if name == "employeeId":
            # TTL: :employeeId a owl:DatatypeProperty , owl:FunctionalProperty
            _add_if_new(graph, (uri, RDF.type, OWL.FunctionalProperty), tally)
        _ensure_ttl_label(graph, uri, label, tally, retagged)

    if retagged:
        info["ttl_labels_retagged"] = retagged
    info["ttl_axioms_added"] = tally[0]
    info["_prop_uri"] = prop_uri
    return info


def _align_predicates(graph: Graph, prop_uri: dict) -> dict:
    """把落库谓词的数据搬到本体属性上（``emp_name`` → ``employeeName`` 等）。

    为什么搬而不是留两份：同一事实两个谓词，会让「有几个属性」「统计一下」这类问题
    口径翻倍，也给了模型「本体外的名字也能用」的错误信号。搬完之后图里一个概念
    只有一个名字，这个名字取自 TTL。

    只搬 ``_TTL_PRED_MIGRATE`` 里 1:1 改名的那批；``dept_cn_name`` 走
    ``_reify_departments`` 变成边。
    """
    ns = _onto_base(graph)
    moved: dict[str, int] = {}
    # 旧谓词的候选来源有两个，缺一不可：
    #   ① graph.predicates()：真有数据的谓词（emp_name / reports_to 这类）；
    #   ② 声明三元组的主语：emplid / hr_status / dept_manager_name / line_rep_name
    #      在当前数据里**一条三元组都没有**，它们只作为 `?p a owl:DatatypeProperty`
    #      的主语躺着。只查①会漏掉它们 → 声明留在图上 → model 里同时出现
    #      `emplid` 和 `employeeId` 两套名字，prompt 里属性表凭空多出 4 行，
    #      模型还可能挑中那条 0 数据的 `emplid` 去查，查出空结果。
    cand: set = set(graph.predicates())
    for t in (OWL.DatatypeProperty, OWL.ObjectProperty, OWL.AnnotationProperty):
        cand |= set(graph.subjects(RDF.type, t))
    for old_local, new_local in _TTL_PRED_MIGRATE.items():
        target = prop_uri.get(new_local) or URIRef("%s/property/%s" % (ns, new_local))
        # 落库谓词可能在 /property/ 也可能在 /relation/ 下，按局部名找全
        olds = [p for p in cand
                if _local(p) == old_local and str(p) != str(target)]
        for old in olds:
            for s, o in list(graph.subject_objects(old)):
                graph.remove((s, old, o))
                graph.add((s, target, o))
                moved[old_local] = moved.get(old_local, 0) + 1
            for t in list(graph.triples((old, None, None))):
                graph.remove(t)
    return {"ttl_pred_migrated": moved} if moved else {}


def _reify_departments(graph: Graph, prop_uri: dict) -> dict:
    """把员工身上的部门名变成「员工 --belongsToDepartment--> 部门个体」。

    依据是 TTL 自己的字段映射：``dept_cn_name → :belongsToDepartment / :departmentName``。
    斜杠是「二选一」，而 ``:departmentName rdfs:domain :Department`` —— 所以字符串形式
    的主语必须是**部门**。员工身上只该有指向部门个体的边。

    不这么做的话：``Department`` 实例数是 0、``belongsToDepartment`` 一条三元组都没有，
    「部门负责人是谁」「这个部门有几个人」这类**本体明确建模过**的问题全部无解，
    而数据里明明有部门名。

    部门个体由部门名派生（同一名字只建一个），IRI 用名字的摘要，稳定且不含中文，
    避免每次装载换一套 IRI。字段里没有层级信息，因此**不臆造** ``DepartmentLevel*``
    （那需要 lv1..lv7 字段，源数据没给）。
    """
    ns = _onto_base(graph)
    dept_name_p = prop_uri.get("departmentName")
    belongs_p = prop_uri.get("belongsToDepartment")
    member_local = [p for p in set(graph.predicates()) if _local(p) == "dept_cn_name"]
    if not (dept_name_p and belongs_p and member_local):
        return {}

    dept_cls = next((c for c in graph.subjects(RDF.type, OWL.Class)
                     if _local(c) == "Department"), None)
    org_cls = next((c for c in graph.subjects(RDF.type, OWL.Class)
                    if _local(c) == "OrganizationalUnit"), None)
    if dept_cls is None:
        return {}

    by_name: dict[str, Any] = {}
    edges = 0
    for pred in member_local:
        for s, o in list(graph.subject_objects(pred)):
            name = str(o).strip()
            if not name:
                continue
            dept = by_name.get(name)
            if dept is None:
                dept = URIRef("%s/dept/%s" % (ns, hashlib.md5(name.encode("utf-8"))
                                              .hexdigest()[:10]))
                by_name[name] = dept
                graph.add((dept, RDF.type, dept_cls))
                if org_cls is not None:
                    # equivalentClass 的直接后果：两个类的实例集合相同
                    graph.add((dept, RDF.type, org_cls))
                graph.add((dept, RDFS.label, Literal(name, lang="zh")))
                graph.add((dept, dept_name_p, Literal(name)))
            if (s, belongs_p, dept) not in graph:
                graph.add((s, belongs_p, dept))
                edges += 1
            graph.remove((s, pred, o))
        # 字符串形式已被边取代，谓词声明一并撤掉，免得「属性清单」里还挂着它
        for t in list(graph.triples((pred, None, None))):
            graph.remove(t)
    if not by_name:
        return {}
    return {"ttl_abox_departments": len(by_name),
            "ttl_abox_belongs_edges": edges}


def _eff_axioms(p: dict, by_local: dict[str, dict]) -> dict:
    """补齐 ``owl:inverseOf`` 推导出的 domain/range（返回副本）。

    TTL 里 ``manages`` / ``isManagerOfDepartment`` 只写 ``owl:inverseOf``，**故意不写**
    domain/range。公理后果是确定的：``Q = inverseOf(P)`` 时 ``domain(Q) = range(P)``、
    ``range(Q) = domain(P)``。不推的话这两个属性在图里 ``domain=[] range=[]``，
    模型看到的就是「不知道能用在哪」，于是「谁管着谁」「谁是部门负责人」只能靠猜。

    推导结果带 ``axiom_source`` 标记，``_brief`` 会如实写「由 owl:inverseOf 推导」，
    不让模型以为本体文件里写了这一条。
    """
    if p.get("domain") or not p.get("inverse_of"):
        return p
    inv = by_local.get(_local(p.get("inverse_of")))
    src = "inverseOf:" + _local(p.get("inverse_of"))
    if inv:
        q = dict(p)
        q["domain"] = list(inv.get("range") or [])
        q["domain_uris"] = list(inv.get("range_uris") or [])
        q["range"] = list(inv.get("domain") or [])
        q["range_uris"] = list(inv.get("domain_uris") or [])
        q["axiom_source"] = src
        if q["domain"]:
            return q
    hit = _TTL_OBJ_PROPS.get(_ttl_name_of(p) or _local(p.get("curie")))
    if not hit:
        return p
    ns = _TTL_NS
    q = dict(p)
    q["domain"] = ["%s/class/%s" % (ns, hit[0])]
    q["domain_uris"] = list(q["domain"])
    q["range"] = ["%s/class/%s" % (ns, hit[1])]
    q["range_uris"] = list(q["range"])
    q["axiom_source"] = src
    return q


def _prune_undeclared(graph: Graph, prop_uri: dict) -> dict:
    """摘掉「本体没定义、图里也没有数据」的属性声明。

    为什么非做不可：模型写 SPARQL 时并不只看 prompt 里的属性表，它经常直接
    ``?p rdfs:domain <Employee>`` 全图扫。画布导出的声明噪声——``dept_id_lv1..13``、
    ``dept_en_name_lv1..13``、161 个 ``fn_derive_*``——有 320 条躺在图上，
    一扫就出来。于是「员工有哪些属性和关系」被答成 ``dept_id_lv1`` /
    ``dept_en_name_lv1`` / ``dept_id_lv2`` …：属性表已经按本体过滤过，
    全图扫这条路却还通着，等于白过滤。

    只摘**纯声明**（该谓词在图里 0 条数据三元组）。有数据的越界谓词
    （``collaborates_with`` 这类）一律不动——删掉数据比留下一个"未定义谓词"更糟，
    那属于静默篡改事实；它们的名字由 ``_brief`` 显式点名，模型就知道不能用。
    """
    allowed = {_local(u) for u in prop_uri.values()}
    allowed |= set(_TTL_OBJ_PROPS) | set(_TTL_DATA_PROPS)
    pruned, kept = 0, set()
    seen: set = set()
    for t in (OWL.DatatypeProperty, OWL.ObjectProperty):
        for s in list(graph.subjects(RDF.type, t)):
            if s in seen:
                continue
            seen.add(s)
            if _local(s) in allowed:
                continue
            if list(graph.subject_objects(s)):
                kept.add(_local(s))
                continue
            for tr in list(graph.triples((s, None, None))):
                graph.remove(tr)
            pruned += 1
    out: dict[str, Any] = {"ttl_undeclared_pruned": pruned}
    if kept:
        out["ttl_undeclared_preds_kept"] = sorted(kept)
    return out


def _drop_self_loops(graph: Graph) -> dict:
    """``owl:IrreflexiveProperty`` 的自环一律剔掉，并如实报数。

    TTL 原话：「irreflexive：自己不是自己的主管——HR 导出常把链条顶端的 reports_to
    填成本人，这类自环不得落图」。当前样本恰好没有自环，所以这条**不能靠数据碰巧干净**
    来兜底：只要有一个人把自己填成上级，图里就多一条本体禁止的边，而问题
    「X 的上级是谁」会答出 X 自己——看起来有答案，其实违反公理。

    过滤必须在 ``add_instances`` 之后：事实和关系两条写入路径都可能产生自环，
    只有落到图上才看得全。
    """
    irr = {p for p in graph.subjects(RDF.type, OWL.IrreflexiveProperty)}
    dropped: dict[str, int] = {}
    for p in irr:
        for s, o in list(graph.subject_objects(p)):
            if s == o:
                graph.remove((s, p, o))
                dropped[_local(p)] = dropped.get(_local(p), 0) + 1
    return {"ttl_irreflexive_dropped": dropped} if dropped else {}


# ---------------------------------------------------------------- 实例载荷归一
def _safe_local(name: str) -> str:
    """实例 id → 合法 IRI 局部名（中文 id 百分号编码后仍可用，但可读性差，这里保守替换）。"""
    s = re.sub(r"[^0-9A-Za-z_.\-]", "_", str(name or "")).strip("_")
    return s or "n"


def _norm_instances(data: dict | None) -> tuple[dict, str]:
    """把调用方给的实例载荷归一成 ``sparql.add_instances`` 认的形状。

    前端/外部会给两种形状，**两种都得认**（只认一种的后果是静默 0 行，
    和「本来就没数据」完全不可区分）：

    - 画布验证用的 ``{mode, entities:{类名:[{id, properties}]}, relations:[...]}``
      （``mode=none`` 表示没有数据，如实返回空）；
    - 导入链用的 ``{entities:[{iri, ref, entity_type, canonical_name}], facts:[...]}``。
    """
    if not isinstance(data, dict):
        return {}, "none"
    ents = data.get("entities")
    if isinstance(ents, list):
        # 已经是规范形状，原样交出去（facts/relations 一并保留）
        return {"entities": ents, "facts": data.get("facts") or [],
                "relations": data.get("relations") or []}, str(data.get("mode") or "payload")

    if not isinstance(ents, dict):
        return {}, str(data.get("mode") or "none")
    mode = str(data.get("mode") or "instances")
    if mode == "none":
        return {}, mode

    base = ""
    for p, ns in (kg_model.build_model(Graph()).get("prefixes") or {}).items():
        if p == "pdt":
            base = ns
    base = base or "urn:pdt:draft:"
    base = base.rstrip("/") + "/inst/"

    out_ents: list[dict] = []
    facts: list[dict] = []
    seen: dict[str, str] = {}
    skipped: list[str] = []
    aliased: dict[str, str] = {}
    for cls_name, rows in ents.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
            rid = str(row.get("id") or props.get("id") or props.get("code") or "").strip()
            if not rid:
                continue
            iri = rid if "://" in rid else base + _safe_local(rid)
            if iri in seen:
                continue
            seen[iri] = rid
            # 中文人读名优先取这几个常见键，都没有就退回 id（不臆造）
            name = ""
            for key in ("name", "full_name", "title", "label", "cn_name"):
                if str(props.get(key) or "").strip():
                    name = str(props[key]).strip()
                    break
            out_ents.append({"iri": iri, "ref": rid, "entity_type": str(cls_name),
                             "canonical_name": name})
            for k, v in props.items():
                if v is None or isinstance(v, (list, dict)):
                    continue
                pred = _draft_pred(k)
                if not pred:
                    # 本体没定义这个字段（level / due_date / approval_status …）。
                    # 丢弃而不是硬塞：图里多一个本体不认的谓词，问「有哪些属性」时
                    # 会把它算进去，答案就和本体不一致了。丢弃要计数，不能静默。
                    skipped.append(str(k))
                    continue
                if pred != _local(str(k)):
                    aliased[str(k)] = pred
                facts.append({"subject_iri": iri, "predicate": pred, "value": v})

    ref_map = {e["ref"]: e["iri"] for e in out_ents}
    relations = []
    for rel in data.get("relations") or []:
        if not isinstance(rel, dict):
            continue
        s = str(rel.get("source_id") or rel.get("source_iri") or "")
        t = str(rel.get("target_id") or rel.get("target_iri") or "")
        pred = str(rel.get("relation_type") or rel.get("predicate") or "")
        if not (s and t and pred):
            continue
        relations.append({"source_iri": ref_map.get(s, s), "target_iri": ref_map.get(t, t),
                          "relation_type": pred})
    return ({"entities": out_ents, "facts": facts, "relations": relations,
             "_skipped_fields": sorted(set(skipped)), "_aliased_fields": aliased}, mode)


def build_graph(spec: dict, data: dict | None = None) -> tuple[Graph, dict]:
    """草稿 spec + 可选实例数据 → 内存图。返回 ``(graph, 装载说明)``。

    装载顺序是**有讲究的**，换一步就会出错：

    1. ``graph_from_spec``：画布名词（类 / 属性 / 两条 domain-range）；
    2. ``_ttl_overlay``：按 TTL 补公理（传递 / 不自反 / 逆属性 / equivalentClass）。
       必须在写数据**之前**——``hasLineManager`` 这些属性原来图里根本没有，
       先补出来，数据才有地方可落；
    3. ``add_instances``：写实体、事实、关系；
    4. ``_align_predicates``：把落库谓词的数据搬到本体属性名上（``emp_name`` →
       ``employeeName``）——必须在写数据**之后**，否则没东西可搬；
    5. ``_reify_departments``：员工身上的部门名 → 部门个体 + ``belongsToDepartment`` 边；
    6. ``_prune_undeclared``：摘掉本体未定义且无数据的属性声明（画布噪声）；
    7. ``_drop_self_loops``：剔掉 ``owl:IrreflexiveProperty`` 的自环——必须最后，
       因为事实、关系、以及上面几步都可能造出自环。
    """
    graph = SP.graph_from_spec(spec or {})
    ov = _ttl_overlay(graph)
    payload, mode = _norm_instances(data)
    added = 0
    if payload.get("entities") or payload.get("facts") or payload.get("relations"):
        added = SP.add_instances(graph, payload)
    info: dict[str, Any] = {"data_mode": mode, "added_triples": added,
                            "instances_given": len(payload.get("entities") or [])}
    prop_uri = ov.pop("_prop_uri", {})
    info.update(ov)
    info.update(_align_predicates(graph, prop_uri))
    info.update(_reify_departments(graph, prop_uri))
    info.update(_prune_undeclared(graph, prop_uri))
    info.update(_drop_self_loops(graph))
    info["graph_triples"] = len(graph)
    # 装载说明必须如实包含「丢了什么」，否则「查不到」和「没装载」无法区分：
    # - ttl_skipped_fields：本体未定义而主动丢弃的源字段（level / due_date …）
    # - predicate_not_declared：字段名对齐了，但画布上没声明这个谓词，仍写不进图
    if payload.get("_skipped_fields"):
        info["ttl_skipped_fields"] = payload["_skipped_fields"]
    if payload.get("_aliased_fields"):
        info["field_aliased"] = payload["_aliased_fields"]
    declared = {_local(s) for s in graph.subjects(RDF.type, OWL.DatatypeProperty)}
    declared |= {_local(s) for s in graph.subjects(RDF.type, OWL.ObjectProperty)}
    lost = sorted({_local(f.get("predicate")) for f in (payload.get("facts") or [])
                   if _local(f.get("predicate")) not in declared})
    if lost:
        info["predicate_not_declared"] = lost
    return graph, info


# ---------------------------------------------------------------- LLM 路径
def _brief(model: dict, max_classes: int = 60, max_props: int = 120) -> str:
    """给模型的「可查询面」清单：完整 IRI，不假装有前缀。

    只列**真能查出三元组的业务属性**：导出顺带写的 dcterms/skos/pdt 术语列进去只会
    让模型以为它们是业务概念，生成一堆查不到数据的查询。

    **派生函数必须挡在外面**。画布上有 161 个 ``func`` 节点（``fn_derive_*``），它们
    也带 ``rdfs:domain``，早前被当成对象属性列出来，模型照着写
    ``?emp <.../function/fn_derive_line_rep_name_is_manager> ?manager`` —— 语法全对、
    恒 0 行。所以这里把函数单独归一段，并明说「不能当谓词」；顺带把函数挤占的
    名额还给真正的对象属性（原先前 120 条全被函数吃掉）。
    """
    onto = model.get("ontology") or {}
    lines = [f"本体：{onto.get('label') or ''} {onto.get('uri') or ''}".strip()]
    lines.append("通用前缀（可用）：rdf: rdfs: owl: xsd:")

    lines.append(f"类（IRI | 名称 | 实例数）：")
    for c in _app_classes(model)[:max_classes]:
        lines.append(f"- <{c.get('uri')}> | {_cls_label(c)} | {c.get('instances', 0)}")

    all_props = _app_props(model)
    # 按属性**真身**分段，不按 declared：`declared` 只表示「图里声明过」，
    # 数据属性同样 satisfies 它。此前用它分段，13 个 owl:DatatypeProperty 全挤进
    # 「对象属性」段，标题还写着「只有这些 IRI 可能出现在数据三元组里」，
    # 而紧跟其后的「数据属性 共 0 个」暴露了矛盾 —— 模型会以为本体没有数据属性。
    # 判据只认 model 给出的 `object_props` 集合（那是按 rdf:type 分出来的）。
    obj_uris = {str(p.get("uri")) for p in (model.get("object_props") or [])}
    obj = [p for p in all_props if str(p.get("uri")) in obj_uris]
    lines.append("对象属性（IRI | 名称 | domain → range | 标记）："
                 "domain/range 都是类，值只能写 IRI。")
    for p in obj[:max_props]:
        dom = "/".join(_local(d) for d in (p.get("domain") or [])) or "?"
        rng = "/".join(_local(r) for r in (p.get("range") or [])) or "?"
        marks = []
        if p.get("transitive"):
            marks.append("传递")
        # 「不自反」是本体实打实声明过的（owl:IrreflexiveProperty），必须告诉模型：
        # 否则它看到 hasLineManager 的 domain/range 都是 Employee，就会自作主张去查
        # 「有没有人是自己的主管」，而这类自环在落图时已被剔掉，答案只能是空——
        # 空得对不对，得由本体公理来解释，不能靠模型猜。
        if p.get("irreflexive"):
            marks.append("不自反")
        if p.get("inverse_of"):
            marks.append(f"反向={_local(p['inverse_of'])}")
        if p.get("functional"):
            marks.append("单值")
        # 本体口径别名：落库谓词叫 reports_to，TTL 里这个概念叫「直线主管」。
        # 不告诉模型，它就不认识用户嘴里的词；告诉模板，模板才匹配得上。
        aka = _aka(p)
        if aka:
            marks.append("本体口径=" + "、".join(aka[:2]))
        if p.get("axiom_source"):
            # domain/range 不是我标的，是 owl:inverseOf 推出来的。必须说清来源，
            # 否则模型会以为本体文件里写了这条，答错时也没法判断该信谁。
            marks.append("%s 推导" % p["axiom_source"])
        if not (p.get("domain") or []):
            marks.append("画布未绑 domain/range，按实际三元组论域理解")
        lines.append(f"- <{p.get('uri')}> | {_prop_label(p)} | {dom} → {rng} | "
                     f"{'、'.join(marks) or '-'}")

    data_props = [p for p in all_props if str(p.get("uri")) not in obj_uris]
    lines.append(f"数据属性（IRI | 名称 | domain → range，共 {len(data_props)} 个，列前 60）："
                 "range 是字面量，值直接写字面量，别当 IRI 去 JOIN。")
    for p in data_props[:60]:
        dom = "/".join(_local(d) for d in (p.get("domain") or [])) or "?"
        rng = "/".join(_local(r) for r in (p.get("range") or [])) or "?"
        lines.append(f"- <{p.get('uri')}> | {_prop_label(p)} | {dom} → {rng}")

    # 本体未定义、但图里还有数据的谓词：必须点名。
    # 这些是画布早期演示带出来的关系（collaborates_with 之类），数据是真的，
    # 但《人员基本信息本体》没有定义它们。不说清楚，模型会当成合法属性去查，
    # 答出来的东西口径不明；说了，它才知道边界在哪、该怎么回答「查不到」。
    ttl_names = set(_TTL_OBJ_PROPS) | set(_TTL_DATA_PROPS)
    stray = [p for p in (model.get("object_props") or []) + (model.get("data_props") or [])
             if _local(p.get("curie")) not in ttl_names
             and _local(p.get("curie")) not in _META_PREDS
             and not _is_func(p.get("uri")) and not _is_noise(p.get("uri"))]
    if stray:
        names = "、".join(_local(p.get("curie")) for p in stray[:10])
        lines.append(f"\n本体未定义的谓词（{len(stray)} 个：{names}）：图上仍有数据，"
                     f"但《人员基本信息本体》没有定义它们，**不要写进 SPARQL**；"
                     f"用户若问起，回「该概念不在本体范围内」，不要拿这些边凑答案。")

    # 派生函数：列个规模 + 几个样例就够了。写清「不能当谓词」，模型才不会再去试。
    funcs = [p for p in (model.get("object_props") or [])
             + (model.get("data_props") or []) if _is_func(p.get("uri"))]
    if funcs:
        sample = "、".join(_local(p.get("curie")) for p in funcs[:4])
        lines.append(f"\n派生函数（{len(funcs)} 个，如 {sample}）：这些是**计算规则**，"
                     f"图里没有任何以它们为谓词的三元组，**不要写进 SPARQL**；"
                     f"要算指标只能靠已有的数据属性。")

    # 实例长什么样：这一节专治「模型的查询语法全对、就是 0 行」。
    # 语义层落库的人名是「张三 (Zhang San)」，模型往往写成
    # `?e rdfs:label "张三"` 精确等值 —— 一个都匹配不上。
    inds = model.get("individuals") or []
    if inds:
        samples = [str(x.get("label") or "") for x in inds[:3] if x.get("label")]
        lines.append("\n实例标签样例：" + "；".join(samples))
        lines.append("锚定某个具体实例时，**不要用 rdfs:label 等值匹配**（标签常带括号英文名/"
                     "工号），改用：?e rdfs:label ?l . FILTER(CONTAINS(STR(?l), \"中文名\"))")
    return "\n".join(lines)


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:sparql|json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    return t


def _llm_sparql(model: dict, question: str,
                retry_note: str = "") -> tuple[str, str, bool]:
    """→ ``(SPARQL, 意图, 是否「本体缺属性」)``。

    第三个返回值专门用来区分「没生成出来」和「本体里压根没有这个属性」：
    前者该降级到模板，后者降级只会让模型拿别的属性凑个数（问「平均司龄」
    答成「27 个人」就是这么来的）。后者要如实拒答。
    """
    user = (
        f"{_brief(model)}\n\n"
        + (f"上一次生成的查询执行报错，请修正：{retry_note}\n\n" if retry_note else "")
        + f"问题：{question}\n请输出 JSON。"
    )
    data = llm_client.chat_json(_SYSTEM, user, timeout=60)
    if isinstance(data, dict):
        sparql = _strip_fence(str(data.get("sparql") or ""))
        intent = str(data.get("intent") or "")
        if sparql:
            return sparql, intent, False
        if data.get("missing") and intent:
            return "", intent, True
    return "", "", False


# ---------------------------------------------------------------- 模板兜底
_RE_HIER = re.compile(r"上级的上级|主管的主管|经理的经理|领导的领导|所有上级|全部上级|祖先|"
                      r"上一层|再往上|往上一层|上面一层|顶层|根节点|双亲的|父级的父级|"
                      r"汇报链|汇报线|层级链|上一级链|间接|传递闭包|transitive|ancestor", re.I)
_RE_COUNT = re.compile(r"多少|几个|几位|几条|数量|人数|总数|统计|算一下|count|how many", re.I)
_RE_PROPS = re.compile(r"属性|字段|有哪些列|哪些列|字段名|property|字段列表", re.I)
_RE_SUB = re.compile(r"子类|父类|继承|类的层级|类型层级|subclass|类型树|类清单|有哪些类", re.I)
_RE_REL = re.compile(r"关系|关联|对象属性|relation|联系|指向|挂在|归属", re.I)
_RE_LINEAGE = re.compile(r"血缘|数据从哪|从哪来|喂给|处理结果|流水线|pipeline|lineage", re.I)
_RE_NEG = re.compile(r"没有|无|不含|排除|not\b", re.I)
# 「谁负责 / 负责人是谁」问的是**某个关系的取值**，不是实例清单。
# 不单独成一支的话会被 7) 的单类列表接走，答出一串部门 IRI，问题其实没答。
_RE_OWNER = re.compile(r"谁负责|谁在管|谁主管|负责人|责任人是|主管是|经理是|归谁|owner|managed?\s*by", re.I)
_RE_MANAGER_PROP = re.compile(r"负责|主管|经理|manager|owner|head", re.I)
# 「谁…」标记**主语待定**；再配上「汇报 / 下属」这类逆方向词，才是主宾倒置问法。
# 光有「汇报」不够：「李四向谁汇报」也含「汇报」，但那句里点名的人是**主语**，
# 问的是他的主管（正向）。两句要的答案互为反向，只看词不看语序必然有一句答错。
_RE_ASK_WHO = re.compile(r"谁|哪些人|哪些员工|哪位", re.I)
_RE_REVERSE_WORD = re.compile(r"汇报|下属|手下|带的人|管的人", re.I)
# 逆方向词 -> 该词指向的 TTL 概念（正向属性的局部名）。
# 之所以要一张表而不是一个正则：「汇报」在正反两个方向都出现，方向由语序定，
# 概念由这个词定——两件事分开，才不会「因为句子里有汇报就把方向搞反」。
_REVERSE_WORD_CONCEPT: tuple[tuple[str, str], ...] = (
    ("汇报", "hasLineManager"),
    ("下属", "hasLineManager"),
    ("手下", "hasLineManager"),
    ("带的人", "hasLineManager"),
    ("管的人", "hasLineManager"),
)

# ---------------------------------------------------------------- 超纲拦截
# 本体只声明了「有哪些类、哪些属性、实例之间有哪些边」，没有任何派生指标，
# 也没有声明排序 / 聚合取极值的能力。问句一旦落到这两类上，**必须拒答**。
#
# 不拦会怎样（use_llm=False 实测，两条都不是报错，是静默误答）：
#   「员工的平均司龄是多少」——「多少」命中 _RE_COUNT → 分支 3) 计数 → 答「27」。
#     27 是员工人数。数字真实、口径全错，而且答案里没有任何迹象表明答的不是
#     这个问题。用户看到一个数就会当真，比说「不知道」危险得多。
#   「哪个部门人最多」——「部门」命中类、「最多」没人认 → 一路掉到分支 7) 列举
#     → 14 个部门。14 是部门总数、还没排序，「最多」那半个问题一毫米没答。
#
# 两类超纲**必须分开说**，因为缺的东西不是一回事（这也是「严格按本体」的要求：
# 不能把「我没实现」写成「本体不支持」，也不能反过来）：
#   · 派生指标（平均 / 合计 / 占比 / 同比）：缺的是**本体里的字段**。
#     TTL 那 13 个数据属性全是名字与状态类字符串，没有任何日期、入职时间或司龄，
#     派生口径也没声明过。→ 本体缺口，要补本体。
#   · 排序 / 取极值（最多 / 最少 / 排名 / 排序）：数据是有的——`:belongsToDepartment`
#     能数出每个部门各有多少人；缺的是**引擎的实现**，模板只有「计数」「列举」
#     两种形态，没有 GROUP BY + ORDER BY 取第一的分支。→ 实现缺口，要补代码。
_RE_DERIVED = re.compile(r"平均|均值|中位数|合计|总计|总和|求和|占比|比例|百分比|"
                         r"同比|环比|增长|增幅", re.I)
# 「取极值」与「排序」是两件事，措辞不能混：
# 「哪个部门人最多」要的是分组 + 排序 + 取第一；「按姓名排序列出员工」只要顺序，
# 数据全在、也不涉及聚合。用同一句话回这两问，就会把后者说成「要取极值」——
# 拒答里的措辞错误和答案里的措辞错误一样算错话。
_RE_EXTREME = re.compile(r"最多|最少|最大|最小|top\s*\d*", re.I)
_RE_SORT = re.compile(r"排名|排序|排行", re.I)
# 拒答答案的固定开头。探针按这句话判「安全拒答」而不是「答出了东西」，
# 所以它必须是一个常量，不能在各处手写不同措辞。
REFUSAL_HEAD = "按本体口径，这个问题答不出来"

_DERIVED_ANSWER = (
    REFUSAL_HEAD + "：「{hit}」要的是一个**派生指标**，而 `personnel_ontology.ttl` 的 "
    "13 个数据属性是 employeeId / employeeName / hrStatus / departmentName / "
    "deptCnNameLv1~7 / departmentManagerName / lineManagerName —— "
    "**没有任何日期、入职时间或司龄字段**，也没有声明过任何派生口径。"
    "本体里没有能算出它的数，所以我不给数：拿员工人数之类的现成数字凑上去，"
    "用户会当成真答案，比说不知道糟得多。要答这类问题，得先在本体里补上对应属性"
    "（或在 pipeline 里加一个派生字段）。"
)
_EXTREME_ANSWER = (
    REFUSAL_HEAD + "：「{hit}」要的是**分组排序取极值**。本体这边数据是够的——"
    "`:belongsToDepartment` 能把员工和部门连起来，理论上数得出每个部门各有多少人；"
    "缺的是**引擎的实现**：模板只有「计数」与「列举」两种形态，没有 "
    "GROUP BY + ORDER BY 取第一的分支，所以不会替你猜一个「最多」是谁。"
    "这一条是**实现缺口，不是本体缺口**——别把它记在本体账上。"
    "现在能回答的是「部门一共有多少个」这类总数问题。"
)
_SORT_ANSWER = (
    REFUSAL_HEAD + "：这句要的是**按指定字段排好序的结果**，而排序这一层现在是缺的。"
    "数据本身一条不缺——员工、部门、以及它们之间的边都在图里；"
    "缺的是**排序能力**：本体没有声明任何排序口径，模板也只实现了「计数」与「列举」，"
    "没有 ORDER BY 分支。这一条是**实现缺口，不是本体缺口**。"
    "要清单可以问「列出所有员工」——但返回顺序是图里的遍历顺序，不保证按你指定的字段排。"
)


def _unsupported(question: str) -> tuple[str, str] | None:
    """问句要的是本体给不出的东西 → ``(答案, 意图)``；否则 ``None``。

    判定用词表而不是靠模型：模型对这类问句很配合，会给一个能跑通的查询
    （数一数、列一列），结果看着像答案。词表短、可枚举、可复核，
    宁可漏掉几个变体，也不放一个凑数的数出去。
    """
    q = str(question or "")
    m = _RE_DERIVED.search(q)
    if m:
        return (_DERIVED_ANSWER.format(hit=m.group(0)),
                f"超纲拦截(本体缺字段)：问句含「{m.group(0)}」，要的是派生指标；"
                f"TTL 13 个数据属性里没有对应字段、也没有派生口径声明，"
                f"按本体口径拒答（未降级到计数/列举凑数）")
    m = _RE_EXTREME.search(q)
    if m:
        return (_EXTREME_ANSWER.format(hit=m.group(0)),
                f"超纲拦截(引擎未实现)：问句含「{m.group(0)}」，要的是分组排序取极值；"
                f"本体只有实例间的边、没声明排序口径，模板也没有该分支，"
                f"按实现缺口如实说明（未降级到列举凑数）")
    m = _RE_SORT.search(q)
    if m:
        return (_SORT_ANSWER,
                f"超纲拦截(引擎未实现)：问句含「{m.group(0)}」，要的是排序；"
                f"本体未声明排序口径、模板无 ORDER BY 分支，"
                f"按实现缺口如实说明（未降级到列举凑数）")
    return None


def _class_aka(graph: Graph | None, cls: dict) -> list[str]:
    """类的**中文别名**（``skos:altLabel``）。

    真实草稿是从语义层导入的，类名是 ``Employee`` / ``Department``，而用户只会说
    「员工」「部门」。导出时别名是写进图里的（``Employee skos:altLabel "员工"``），
    但 ``model()`` 只透出 ``label``，不读别名——问「员工有多少人」时一个类都匹配不上，
    最后答「本体里有哪些类」。这里按图补齐，只收**含中文**的别名：
    纯 ASCII 的别名（``DepartmentLevel1 skos:altLabel "Department"``）与局部名重复，
    收进来只会在英文问句上误撞。
    """
    if graph is None:
        return []
    out = []
    for a in graph.objects(URIRef(str(cls.get("uri"))), SKOS.altLabel):
        s = str(a).strip()
        if len(s) < 2:
            continue
        if not re.search(r"[\u4e00-\u9fff]", s):
            continue
        out.append(s)
    return out


_SURNAME = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平"
    "黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝"
    "董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊"
    "胡凌霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石"
    "崔吉钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富"
    "巫乌焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武"
    "符刘景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池"
    "乔阴鬱胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通"
    "边扈燕冀郏浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾"
    "终暨居衡步都耿满弘匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾"
    "敖融冷訾辛阚那简饶空曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖"
    "益桓公"
)

# 「叫李雷」「李雷的」这两种把具体的人点出来的说法
_RE_NAMED_CALL = re.compile(r"叫\s*([\u4e00-\u9fa5]{2,3})")
_RE_POSSESSIVE = re.compile(r"([\u4e00-\u9fa5]{2,3})的")

# 这些不是人名，是问句里的泛称/术语，出现「X的」时不能当成人名去图上找
_NAME_STOP = {
    "我们", "你们", "他们", "她们", "它们", "这个", "那个", "哪个", "什么",
    "所有", "每个", "这些", "那些", "上面", "下面", "当前", "本体", "图上",
    "草稿", "结果", "数据", "处理", "加工", "血缘", "属性", "关系", "字段",
    "全部", "其他", "其中", "以上", "以下", "下面", "里面", "之间", "上面",
    "同事", "领导", "下属", "上级", "下级", "员工们", "部门里", "公司里",
}


def _unknown_subject(graph: Graph | None, model: dict, question: str) -> str | None:
    """问句点名了一个具体的人，但图里没有这个实例 → 返回这个名字；否则 ``None``。

    为什么必须单独判：``_template`` 在「类识别成功、个体锚定失败」时会继续走
    「按类查属性」那条分支。于是「叫李雷的员工的主管是谁」被答成「所有 27 个员工的
    ``dept_manager_id``」——27 行看着像答案，其实一个人都没答到。这比答 0 行更有害：
    用户会拿一堆别人家的上级去凑这个问题的答案。所以先把「点了个图里没有的人」认出来，
    如实说没有，不让它退化成全类扫表。

    只认**像人名**的候选（2~3 个汉字且首字是常见姓氏），并且要同时不在类名、属性名、
    别名、已有实例标签里。宁可漏判（漏了就退回原来的行为，不会更糟），
    不能误判——把「处理的」当成人名去拒答，等于把本来答得出的问题搞坏。
    """
    if graph is None:
        return None
    q = str(question or "")
    if not q:
        return None

    classes = _app_classes(model)
    props = _app_props(model)

    # 图里真实存在的实例标签（整串 + 核心名）：命中说明这个人真的有
    known: set[str] = set()
    owner = {str(c.get("uri")) for c in classes if c.get("uri")}
    for s, t in graph.subject_objects(RDF.type):
        if str(t) not in owner:
            continue
        for lbl in graph.objects(s, RDFS.label):
            if isinstance(lbl, Literal):
                name = str(lbl).strip()
                if name:
                    known.add(name)
                    known.add(_label_core(name))

    # 概念词：类名 / 类别名 / 属性名 / 属性别名，都不是人名
    concept: set[str] = set()
    for c in classes:
        for k in ("label", "curie", "id", "name"):
            if c.get(k):
                concept.add(str(c[k]))
        concept.update(_class_aka(graph, c))
    for p in props:
        concept.add(_prop_label(p))
        concept.add(_local(p.get("curie")))
        concept.update(_aka(p))
    concept = {x for x in concept if len(x) >= 2}

    for pat in (_RE_NAMED_CALL, _RE_POSSESSIVE):
        for m in pat.finditer(q):
            # 「叫李雷的员工」里，``{2,3}`` 会把「的」一起吃掉，得到「李雷的」。
            # 人名不以上面这个结构助词结尾，剥掉才是真名字。
            cand = m.group(1).rstrip("的")
            if len(cand) < 2 or cand[0] not in _SURNAME:
                continue
            if cand in _NAME_STOP or cand in concept:
                continue
            if any(cand in k or (len(k) >= 2 and k in cand) for k in known):
                continue
            if any(cand in c for c in concept):
                continue
            return cand
    return None


def _match_classes(classes: list[dict], question: str,
                   graph: Graph | None = None) -> list[dict]:
    """按「名称 / 局部名 / 中文别名」在问题里做最长子串匹配，按出现先后返回。

    中文类名普遍两个字（员工 / 部门），所以命中长度相同时**看谁先出现**——
    问「员工的部门」时先问的是员工，主语就是员工。
    """
    q = question.lower()
    hits: list[tuple[int, int, dict]] = []
    for c in classes:
        if _local(c.get("curie")).lower() in ("pipeline",):
            continue
        best = 0
        for name in [_cls_label(c), _local(c.get("curie"))] + _class_aka(graph, c):
            n = str(name or "").strip()
            if len(n) < 2:
                continue
            pos = q.find(n.lower())
            if pos >= 0 and len(n) > best:
                best = len(n)
                hits.append((pos, len(n), c))
    # 去重（同一个类可能被名称和局部名各命中一次），保留最先出现的
    out, seen = [], set()
    for pos, _ln, c in sorted(hits, key=lambda x: (x[0], -x[1])):
        if c.get("uri") in seen:
            continue
        seen.add(c.get("uri"))
        out.append(c)
    return out


# ---------------------------------------------------------------- 实例锚定
def _label_core(name: str) -> str:
    """人读标签 → 问句里真会出现的那个「核心名」。

    语义层落库的人名是 ``张三 (Zhang San)``（中文名 + 括号里的英文名），
    经过 ``/ontology/semantic/instances`` 直接喂进图后，``rdfs:label`` 就是这一整串。
    用户嘴上说的是「张三」，问句里永远不含括号和英文名——拿整串做子串匹配
    一命中不了，锚点就是 ``None``，于是「张三的直线主管是谁」被答成
    「本体里有哪些类」。这不是模型答错，是根本没找到人。

    剥括号只做**展示名**这一件事，不改图里的标签：真正的三元组里仍是全名，
    取回来的答案也仍是全名（用户看到「李四 (Li Si)」才对得上数据）。
    """
    s = str(name or "").strip()
    for sep in ("（", "(", "[", "【"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.strip()


def _individual_anchor(graph: Graph, classes: list[dict],
                       question: str) -> tuple[str, str] | None:
    """问句里点名的那个实例 → ``(IRI, 显示名)``；没点名返回 ``None``。

    为什么不能只靠类名取主语：真实问句常常只报人名。``_match_classes`` 在
    「张三的直线主管是谁」上返回空，``cls`` 就成了 ``None``，下面所有「按类查」
    的分支（1/2/2.5/3/4/6/7）全部跳过，最后只能答「本体里有哪 10 个类」——
    数据一个没错，但用户问的是一个人，一个字都没答到。

    这里改成拿**图里已有实例的标签**去问句里做最长匹配：命中谁，主语就是谁。
    只认实例（``rdf:type`` 落在本体类上的主语），不碰类 / 属性 / 枚举值，
    所以「员工的直线主管是谁」这种带类名的问句不会被误判成某个员工。

    标签同时按**整串**和**核心名**（``_label_core``，剥掉括号里的英文名）各试一次：
    真实语义层数据只有后者能命中，而干净草稿只有前者能命中，两种都得管。
    返回的显示名一律用**整串**，因为那是图里真实存的值。
    """
    q = str(question or "")
    if not q or graph is None:
        return None
    owner = {str(c.get("uri")) for c in classes if c.get("uri")}
    if not owner:
        return None
    best: tuple[int, str, str] | None = None
    for s, t in graph.subject_objects(RDF.type):
        if str(t) not in owner:
            continue
        for lbl in graph.objects(s, RDFS.label):
            if not isinstance(lbl, Literal):
                continue
            # 只认中文 / 无语言标记的标签。@en 那批是英文名（Employee、hasLineManager），
            # 中文问句里根本不会出现，留着只会平白多出几个能误撞的短串。
            if str(lbl.language or "") not in ("zh", ""):
                continue
            name = str(lbl).strip()
            if len(name) < 2 or name.lower().startswith("http"):
                continue
            core = _label_core(name)
            hit = 0
            if name in q:
                hit = len(name)
            elif len(core) >= 2 and core in q:
                # 核心名命中的优先级压到整串之下：同一句话里若既有全名又有别人，
                # 全名那个更具体，不能因为核心名短就抢先。
                hit = len(core)
            if hit and (best is None or hit > best[0]):
                best = (hit, str(s), name)
    return (best[1], best[2]) if best else None


def _name_core(p: dict) -> str:
    """属性局部名 → 比对用的核心词：去掉 has / is / be 前缀，统一小写。

    ``hasLineManager`` 与 ``lineManagerName`` 是同一个业务概念的两种写法，
    名字上的唯一共同点就是这个核心词（``linemanager``）。
    """
    n = _local(p.get("curie")).lower()
    for pre in ("has", "is", "be"):
        if n.startswith(pre) and len(n) > len(pre) + 3:
            return n[len(pre):]
    return n


def _siblings(props: list[dict], obj_uris: set[str], p: dict) -> list[dict]:
    """同一个概念的「另一套表示」：对象属性 ↔ 同核心词的数据属性。

    ``hasLineManager``（指到 Employee）与 ``lineManagerName``（名字的字符串副本）
    就是这样一对。真实数据里两者不一定同时有边——张三只填了 ``lineManagerName``
    也照样有 ``hasLineManager`` 那条关系，但换个人可能只有其中一个。所以查的时候
    要能互相兜底，不能只认一种。
    """
    core = _name_core(p)
    if len(core) < 4:
        return []
    p_is_obj = str(p.get("uri")) in obj_uris
    out = []
    for x in props:
        if str(x.get("uri")) == str(p.get("uri")):
            continue
        # 对方必须是「另一种类型」，否则只是同名重复声明，兜底没有意义
        if (str(x.get("uri")) in obj_uris) == p_is_obj:
            continue
        c = _name_core(x)
        if len(c) >= 4 and (core in c or c in core):
            out.append(x)
    return out


def _prop_chain(props: list[dict], obj_uris: set[str], question: str) -> list[dict]:
    """问句里提到的属性 → 候选属性链（最贴合问句的在前，同概念的另一套表示在后）。

    命中长度优先：问「直线主管」时 ``直线主管``（hasLineManager，4 字）比
    ``管理``（manages，2 字）更具体，必须排在前面——否则「张三的直线主管是谁」
    会按 ``管理`` 去查他的下属，答出来的是另一批人。
    长度相同时对象属性优先：它能追到人，字符串属性只是名字的副本。

    匹配词除标签 / 局部名外，还算上**本体口径别名**（``_aka``）：落库谓词叫
    ``reports_to``、TTL 里这个概念叫「直线主管」，不认别名就永远对不上。
    """
    q = str(question or "")
    scored: list[tuple[int, int, dict]] = []
    for p in props:
        hit = 0
        for name in ((_prop_label(p), _local(p.get("curie")))
                     + _aka(p) + _surface(p)):
            n = str(name or "").strip()
            if len(n) >= 2 and n.lower() in q:
                hit = max(hit, len(n))
        if hit:
            scored.append((hit, 0 if str(p.get("uri")) in obj_uris else 1, p))
    if not scored and _RE_MANAGER_PROP.search(q):
        # 省略说法：「张三的主管是谁」里没有「直线主管」这个全称，
        # 但「主管」二字明确指向本体的主管类属性。这时按同一套词面规则
        # 把主管/负责/经理一类的属性都收进候选，靠下面的「图上验一验」挑出
        # 真正在这个人身上有边的那个——不是挑字符串最像的那个。
        scored = [(4, 0 if str(p.get("uri")) in obj_uris else 1, p) for p in props
                  if any(_RE_MANAGER_PROP.search(x) for x in
                         (_prop_label(p), _local(p.get("curie"))) + _aka(p))]
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    chain: list[dict] = []
    seen: set[str] = set()
    for _ln, _k, p in scored:
        for x in [p] + _siblings(props, obj_uris, p):
            u = str(x.get("uri"))
            if u in seen:
                continue
            seen.add(u)
            chain.append(x)
    return chain[:3]


def _direction_prop(props: list[dict], obj_uris: set[str], question: str,
                    anchor_name: str) -> tuple[dict, bool] | None:
    """问句点名的人在这条关系里是**主语**还是**宾语**？返回 ``(属性, 是否倒着查)``。

    为什么必须单独判方向：「李四向谁汇报」和「谁向李四汇报」共享全部词面
    （李四、汇报、谁），只差语序，而答案一个是他主管、一个是他下属——互为反向。
    字面匹配分不开这两句，分不开就会把宾语当主语，答出来是另一个人，
    而且只有一条结果，看着完全正常。

    判据（全部来自语序，不来自词感）：
    - 「谁」出现在点名实例**之前** → 实例是宾语，倒着查；
    - 「下属 / 手下 / 带的人 / 管的人」出现在实例**之后** → 同样是倒着查
      （「王五手下」= 以王五为宾语的那些 ``:hasLineManager`` 边）；
    - 其余（「谁」在实例之后、且带「汇报」）→ 正向，查他自己的主管。

    倒着查只在**本体声明过逆属性**时才授权：TTL 有
    ``:manages owl:inverseOf :hasLineManager``，所以「有人向 Y 汇报」⟺
    「那个人 :hasLineManager Y」成立。没写 inverseOf 的属性反向不成立
    （``:belongsToDepartment`` 倒过来问「谁属于某部门」语义不通），一律不放行。

    返回的永远是**正向**那条属性（``:hasLineManager``），不是逆属性（``:manages``）：
    前者是 TTL 直写 domain/range、也是装载时真正落边的那条；后者只在图里落了逆边
    时才有值，拿它查会在老数据上答空。
    """
    q = str(question or "")
    if not _RE_REVERSE_WORD.search(q):
        return None
    name = str(anchor_name or "").strip()
    a_pos = -1
    for cand in (name, _label_core(name)):
        if cand and cand in q:
            a_pos = q.find(cand)
            break
    if a_pos < 0:
        return None
    want = next((concept for word, concept in _REVERSE_WORD_CONCEPT if word in q), None)
    if not want:
        return None
    fwd = next((p for p in props if _local(p.get("curie")) == want), None)
    who = _RE_ASK_WHO.search(q)
    noun_after = any(w in q[a_pos:] for w in ("下属", "手下", "带的人", "管的人"))
    if (who is not None and who.start() < a_pos) or noun_after:
        declared = any(str(p.get("uri")) in obj_uris
                       and p.get("inverse_of")
                       and _local(p["inverse_of"]) == want
                       for p in props)
        if not declared:
            return None
        fwd = fwd or next((p for p in props if p.get("inverse_of")
                           and _local(p["inverse_of"]) == want), None)
        return (fwd, True) if fwd else None
    if who is not None:
        # 「李四向谁汇报」：正向问，不需要逆公理授权
        return (fwd, False) if fwd else None
    return None


def _connector(props: list[dict], a: dict, b: dict,
               up: dict[str, set[str]] | None = None) -> tuple[dict, str] | None:
    """找一条把 a 类连到 b 类的对象属性；返回 (属性, "fwd"|"rev")。

    两端都按「祖先闭包」比对：属性声明在子类（``DepartmentLevel1``）上，
    问的却是父类（``Department``）时也要能连上。
    """
    up = up or {}
    a_uri, b_uri = str(a.get("uri") or ""), str(b.get("uri") or "")
    for p in props:
        if not p.get("declared"):
            continue
        dom = [str(x) for x in (p.get("domain") or [])]
        rng = [str(x) for x in (p.get("range") or [])]
        a_in_dom = any(_under(d, a_uri, up) for d in dom)
        b_in_rng = any(_under(r, b_uri, up) for r in rng)
        a_in_rng = any(_under(r, a_uri, up) for r in rng)
        b_in_dom = any(_under(d, b_uri, up) for d in dom)
        if a_in_dom and b_in_rng:
            return p, "fwd"
        if a_in_rng and b_in_dom:
            return p, "rev"
    return None



def _ns(model: dict) -> str:
    """本体命名空间（以 / 结尾）——画布编译出的 @id 都在它下面。"""
    return str((model.get("ontology") or {}).get("uri") or "").rstrip("/") + "/"


_PROCESS_KINDS = ("process", "profiler", "datasource", "sample")


def _node_label(n: dict) -> str:
    f = n.get("fields") or {}
    return str(f.get("name") or n.get("title") or n.get("id") or "").strip()


def _lineage_overview(spec: dict) -> dict:
    """从**草稿**直接读出数据血缘台账（不经图）。

    为什么要在装图之先看一眼：导出的 ``pdt:Pipeline`` 节点是**单例**，无论画布上
    有没有处理节点都会写出来。没有处理节点时 ``stageStatus`` 会是一串 0，
    直接查图会答成「数据采样=0；数据处理=0」——把「根本没搭血缘」说成
    「血缘为零」，读起来像个结论，实际是空。所以要在这里分清三态：
    没有处理节点 / 有处理节点但没登记 / 已登记。
    """
    nodes = (spec or {}).get("nodes") or []
    obj_kinds = ("object", "entity")
    objs = {str(n.get("id")): _node_label(n) for n in nodes
            if str(n.get("kind") or "") in obj_kinds}
    by_name = {_node_label(n): str(n.get("id")) for n in nodes
               if str(n.get("kind") or "") in obj_kinds}
    procs = []
    for n in nodes:
        if str(n.get("kind") or "") not in ("process", "profiler"):
            continue
        f = n.get("fields") or {}
        raw = f.get("link_objects") or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,，;；]", raw) if x.strip()]
        named: list[str] = []
        for v in raw if isinstance(raw, list) else []:
            v = str(v).strip()
            if not v:
                continue
            # link_objects 里存的是对象 id（也可能有人手填对象名），两种都要认
            label = objs.get(v) or _local(by_name.get(v) or v) or v
            if label not in named:
                named.append(label)
        procs.append({"name": _node_label(n) or str(n.get("id")),
                      "objects": named, "mode": str(f.get("link_mode") or "")})
    return {"procs": procs, "objects": [v for v in objs.values() if v]}


def _template(model: dict, question: str,
              graph: Graph | None = None) -> tuple[str, str]:
    """关键词模板兜底 → ``(SPARQL, 意图说明)``。

    ``graph`` 只为「按实例名锚定」这一支服务：要把「张三」认成图里某个
    ``:Employee`` 个体，必须看实例的 ``rdfs:label``，光有 model 看不到。
    不传也能用，只是那一支不生效（self_check 里的自检就不需要）。
    """
    classes = _app_classes(model)
    props = _app_props(model)
    obj_uris = {str(p.get("uri")) for p in (model.get("object_props") or [])}
    hits = _match_classes(classes, question, graph)
    cls = hits[0] if hits else None
    second = hits[1] if len(hits) > 1 else None
    q = question.lower()
    want_count = bool(_RE_COUNT.search(q))
    want_props = bool(_RE_PROPS.search(q))

    # 0) 数据血缘：这是 pipeline 跑完最容易问、也最该问得出的问题
    if _RE_LINEAGE.search(q):
        ns = _ns(model)
        # 只答「喂给了哪些业务对象」。stageStatus 那种「数据采样=N；数据处理=N」是
        # 节点计数，不是血缘——把它拼进答案会让人以为已经追到了数据去向。
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?objLabel WHERE {{
  ?p a <{ns}Pipeline> .
  ?p <{ns}feedsObjects> ?obj .
  OPTIONAL {{ ?obj rdfs:label ?ol . FILTER(lang(?ol) IN ("zh", "")) }}
  BIND(COALESCE(?ol, REPLACE(STR(?obj), "^.*[#/]", "")) AS ?objLabel)
}} ORDER BY ?objLabel LIMIT 200""",
                "沿 pdt:Pipeline.feedsObjects 查：处理结果喂给了哪些业务对象")

    # 0.5) 问句点名了**某个具体实例**（「张三的直线主管是谁」）：
    #      先把主语锚定到这个人，再取他某个属性的值。
    #
    #      没有这一支会怎样：整句话里一个类名都没有，_match_classes 返回空，
    #      1/2/2.5/3/4/6/7 全部因为 cls 为空而跳过，直接掉到 8) 的类清单兜底——
    #      用户问的是一个人，回答的是「本体里有哪些类」。数据全对，但一毫米没答到。
    #
    #      锚定了还必须**在图上验一验**：候选属性在锚点个体上一条边都没有时，
    #      就不认这个问法。宁可答「未登记」/「没识别出要问哪一项」，
    #      也不能因为「直线主管」里含「管理」就把他管的人列出来——那是自信的错答，
    #      比「不知道」更糟。
    #
    #      「不认这个问法」的落法改过一版，值得记下来：原来 filter 完变成空链就
    #      `return` 都不 return，继续往下走，于是掉进 3) 的「列举该类的全部实例」。
    #      线上实测「张三的员工ID是什么」→ 27 个员工的名字。**问一个人的 ID，
    #      回答全员名单**——27 行看着像答案，一个人都没答到。根因是线上语义层这块
    #      没有 employeeId 数据（只有 full_name / department / level 三个字段），
    #      而「属性没数据」被当成了「这个问法不成立」。
    #      现在分成三态：有数据 → 照常答；属性认出来了但 0 条边 → 如实答「未登记」；
    #      连属性都没认出来 → 如实说「没识别出要问哪一项」，都不许降级成按类扫表。
    if graph is not None and not _RE_HIER.search(q):
        anchor = _individual_anchor(graph, classes, question)
        if anchor:
            a_iri, a_name = anchor
            a_node = URIRef(a_iri)
            # 方向必须先定：它和下面的正向字面链是**互斥**的。
            # 「李四向谁汇报」和「谁向李四汇报」共享全部词面，只差语序，
            # 答案一条是他主管、一条是他下属。字面打分分不开这两句，
            # 分不开就会把宾语当主语——答出另一个人，还只有一条结果，看着完全正常。
            dir_hit = _direction_prop(props, obj_uris, q, a_name)
            rev = dir_hit[0] if (dir_hit and dir_hit[1]) else None
            if rev is not None:
                rev_iri = str(rev.get("uri"))
                rev_local = _local(rev.get("curie"))
                body = (f"  ?v <{rev_iri}> ?x .\n"
                        f"  FILTER(?v != ?x)\n"
                        f'  OPTIONAL {{ ?v rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }}\n'
                        f'  BIND(COALESCE(?l, REPLACE(STR(?v), "^.*[#/]", "")) AS ?value)')
                head = (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT {"(COUNT(DISTINCT ?value) AS ?cnt)" if want_count else "?value"} WHERE {{
  VALUES (?x ?xName) {{ (<{a_iri}> {_lit(a_name)}) }}
""" + body + ("\n}" if want_count else "\n} ORDER BY ?value LIMIT 200"))
                return (head,
                        f"主宾倒置问法：「谁 / 下属」把实例「{a_name}」放在了**宾语**位置。"
                        f"按 TTL 的 `:manages owl:inverseOf :{rev_local}` 这条逆公理，"
                        f"「有人向 {a_name} 汇报」等价于「那个人 :{rev_local} {a_name}」，"
                        f"所以直接拿**正向**边 `<{rev_local}>` 倒着查（以「{a_name}」为宾语）——"
                        f"不依赖图里是否落了 `:manages` 的逆边，也不需要推理机；"
                        f"自环按 owl:IrreflexiveProperty 排除")
            # 正向点名问法（「李四向谁汇报」）：方向已由语序定下，直接用该属性，
            # 不再走字面打分——「汇报」在别名表里是「汇报给 / 汇报对象」，字面撞不上。
            chain = ([dir_hit[0]] if (dir_hit and not dir_hit[1])
                     else _prop_chain(props, obj_uris, q))
            # 只认「最强命中」那一组：同核心词的对象属性/数据属性互为兜底
            # （hasLineManager ↔ lineManagerName 是同一概念的两种写法），
            # 但**不往更弱的字面命中降级**——「直线主管」里含「管理」，
            # manages 只是字面撞车，拿它去查会答成他管的人。
            if chain:
                core0 = _name_core(chain[0])
                group = []
                for x in chain:
                    c = _name_core(x)
                    if c == core0 or c in core0 or core0 in c:
                        group.append(x)
            else:
                group = []
            with_data = [x for x in group
                         if next(graph.objects(a_node, URIRef(str(x.get("uri")))), None)
                         is not None]
            use = with_data or group
            if use:
                blocks, picks = [], []
                for i, p in enumerate(use):
                    p_iri = str(p.get("uri"))
                    # 自环要排除，两种形态都得管：
                    # - 对象属性：?v 是 IRI，`FILTER(?v != ?x)` 就是「自己不是自己的主管」
                    #   （本体把 hasLineManager 声明成 owl:IrreflexiveProperty）；
                    # - 数据属性：?v 是字面量，跟 ?x 永远不相等，得拿名字比，
                    #   否则「李四的直线主管」会答成「李四」——他自己的名字就填在
                    #   lineManagerName 里。落图时只剔 IRI 自环，这条字面量自环还在。
                    blocks.append(f"""  OPTIONAL {{ ?x <{p_iri}> ?v{i} .
    FILTER(?v{i} != ?x)
    FILTER(STR(?v{i}) != STR(?xName))
    OPTIONAL {{ ?v{i} rdfs:label ?l{i} . FILTER(lang(?l{i}) IN ("zh", "")) }} }}""")
                    # 对象属性取对方的 rdfs:label（不能把 IRI 甩给用户），
                    # 数据属性直接取字面量；两套表示谁有值用谁。
                    picks.append(f'IF(isIRI(?v{i}), COALESCE(?l{i}, '
                                 f'REPLACE(STR(?v{i}), "^.*[#/]", "")), ?v{i})')
                # 只 SELECT ?value：单变量单行会被 _answer_text 渲染成「答案：李四」。
                # 把 ?x / ?xLabel 也带上就是三个变量，答案变成
                # 「查到 1 条记录：x=张三，xLabel=张三，value=李四」——
                # 主语本来就在问句里，再复述一遍只是噪音。
                head = (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?value WHERE {{
  VALUES (?x ?xName) {{ (<{a_iri}> {_lit(a_name)}) }}
""" + "\n".join(blocks) + "\n  BIND(COALESCE(" + ", ".join(picks) +
                        ', "（未登记）") AS ?value)\n} LIMIT 20')
                if want_count and with_data:
                    # 「他管了多少人」：计数就别把「（未登记）」算进去
                    head = (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT (COUNT(DISTINCT ?value) AS ?cnt) WHERE {{
  VALUES (?x ?xName) {{ (<{a_iri}> {_lit(a_name)}) }}
""" + "\n".join(blocks) + "\n  BIND(COALESCE(" + ", ".join(picks) +
                            ") AS ?value)\n}""")
                names = "、".join(f"<{_local(p.get('curie'))}>" for p in use)
                # 「0 条边」有两种完全不同的原因，说错就是错话：
                # ① 整份数据里没有任何主语带这个属性 → 源数据根本没这一列；
                # ② 只有这一个人没有 → 是这一行没填（链条顶端的人本来就没有主管）。
                # 原来一律写成「源数据没有这一列」，问「李四的直线主管」时
                # 就把「他是最高层」错说成「没采这一列」。这里按图实测分开口径。
                if with_data:
                    tail = ""
                else:
                    elsewhere = any(True for _ in graph.subject_objects(
                        URIRef(str(use[0].get("uri")))))
                    tail = ("；**该属性在「%s」身上 0 条边**，答「（未登记）」——"
                            % a_name) + (
                        "别的实例上有值，所以是**这一行没填**（例如链条顶端本就无上级），"
                        "不是本体没定义它" if elsewhere else
                        "整份数据里没有任何主语带这个属性，是**源数据缺这一列**，"
                        "不是本体没定义它")
                return (head,
                        f"用模板把主语锚定到实例「{a_name}」"
                        f"（在问句里按实例标签最长匹配命中），再取它的 {names}："
                        f"对象属性取对方的 rdfs:label，只有同名字符串属性时取字面量；"
                        f"自环按 owl:IrreflexiveProperty 排除" + tail)
            # 锚定成功、连属性都没认出来：**不能**退回按类计数/列举。
            # 「李四管几个部门」曾经会掉到计数分支答「9」——那是全部部门数，
            # 不是他管的部门数，数字真实、语义全错，比不答危险得多。
            return ("""SELECT ("（未识别到要问的属性）" AS ?value) WHERE {}""",
                    f"问句点名了实例「{a_name}」（按实例标签最长匹配命中），"
                    f"但没识别出要问他的哪一项属性/关系；为避免拿全类统计冒充个人数据，"
                    f"这里不作答，请把属性说具体些（例如「直线主管」「员工ID」「所属部门」）")

    # 1) 「属性 / 字段」优先于计数：「员工有多少个属性」问的是清单不是数量
    if want_props and cls:
        up = _ancestors(model)
        mine = _dedup_props(_props_of(props, cls, up))
        if mine:
            # range 也从 Python 侧给，不在 SPARQL 里 GROUP_CONCAT。
            # 踩过的坑：manages / isManagerOfDepartment 的 rdfs:range 是
            # owl:inverseOf 推出来的，图里根本没有这条三元组，于是
            # `OPTIONAL { ?prop rdfs:range ?r }` 不绑定 ?r、下游
            # `GROUP_CONCAT(DISTINCT ?rname)` 直接抛 "Variable rname is not bound"，
            # 整个问句 ok=False。由 Python 传 range（`_app_props` 已含推导值），
            # SPARQL 只负责取 label —— 口径统一由本体侧决定。
            vals = " ".join(
                f"({_iri(p['curie'])} {_lit('/'.join(_local(r) for r in (p.get('range') or [])) or '?')} "
                f"{_lit(_owner_label(p, cls))})" for p in mine)
            # 一个属性一行：label 可能有多条（中英文/重复标注），不聚合就会
            # 「提示说共 4 个属性、结果却回 28 行」——数字对不上，读者只会觉得答错了。
            # 所以按属性 GROUP BY：label 用 SAMPLE 取一条，行数恒等于属性个数。
            return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?prop (SAMPLE(?lbl) AS ?label) ?rng ?owner WHERE {{
  VALUES (?prop ?rng ?owner) {{ {vals} }}
  OPTIONAL {{ ?prop rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }}
  BIND(COALESCE(?l, REPLACE(STR(?prop), "^.*[#/]", "")) AS ?lbl)
}} GROUP BY ?prop ?rng ?owner
  ORDER BY ?prop LIMIT 200""",
                f"用模板列出「{_cls_label(cls)}」的全部属性（含子类声明的，共 {len(mine)} 个；"
                f"owner 非空表示该属性声明在子类上；range 由本体侧给出（含 inverseOf 推导））")

    # 2) 层级传递：两端同域的对象属性 + 属性路径（按数据实际链算，不依赖 OWL 推理器）
    #
    #    注意用词：这里挑的是「domain 与 range 有交集」的属性，**不等于自反（reflexive）**。
    #    自反要求每个个体都指向自己，本体的 hasLineManager 恰恰相反——它是
    #    owl:IrreflexiveProperty（自己不是自己的主管）。所以下面一律按「两端同域」陈述，
    #    本体真声明了什么（传递 / 不自反）才照实写上。
    if _RE_HIER.search(q):
        up = _ancestors(model)
        pool = _props_of(props, cls, up) if cls else props
        self_props = [p for p in pool if p.get("declared")
                      and (set(p.get("domain") or []) & set(p.get("range") or []))]
        if not self_props and cls:
            # 问的类上没有两端同域的属性：退回全本体的这类关系（层级关系常挂在祖先类上）
            self_props = [p for p in props if p.get("declared")
                          and (set(p.get("domain") or []) & set(p.get("range") or []))]
        pick = next((p for p in self_props
                     if _prop_label(p).lower() in q or _local(p.get("curie")).lower() in q), None)
        if not pick and _RE_MANAGER_PROP.search(q):
            # 「主管的主管是谁」里没有「直线主管」这个全称，按标签精确匹配会落空，
            # 然后顺手挑了第一个两端同域的属性——hasParentDepartment（部门层级），
            # 答成部门链。主管这类简称必须单独认一次，标签里就有「主管」二字。
            pick = next((p for p in self_props if _RE_MANAGER_PROP.search(_prop_label(p))
                         or _RE_MANAGER_PROP.search(_local(p.get("curie")))), None)
        pick = pick or next((p for p in self_props if p.get("transitive")), None) \
            or (self_props[0] if self_props else None)

        if pick:
            return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?s ?o WHERE {{
  ?s {_iri(pick['curie'])}+ ?o .
  # 自环不算「上级」。源数据里链条顶端常把自己填成自己的主管（如 reports_to 填本人），
  # 落图若留下这条边，这里就会答「X 的主管是 X」，还平白把闭包撑大一圈。
  # 宁可在图上少一条，也不能在答案里多一条错的。
  # 这条过滤不是临时补丁：本体把 hasLineManager 声明成 owl:IrreflexiveProperty，
  # 「自己不是自己的主管」是公理，FILTER 只是把公理落到了查询上。
  FILTER(?s != ?o)
}} LIMIT 200""",
                    f"用属性路径 <{_local(pick['curie'])}>+ 求传递闭包"
                    f"（一层、两层、多层都在里面，所以「主管的主管」答得出来）")


    # 2.5) 「谁负责 / 负责人是谁」：取某个属性的值，而不是列实例
    if _RE_OWNER.search(q) and cls:
        up = _ancestors(model)
        pool = _props_of(props, cls, up)
        cands = [p for p in pool
                 if _RE_MANAGER_PROP.search(_prop_label(p))
                 or _RE_MANAGER_PROP.search(_local(p.get("curie")))]
        # 对象属性（指向 Employee）优先于同名的字符串属性：后者只是名字的副本，
        # 指向不了人，也就追不到「这个人的上级是谁」。
        cands.sort(key=lambda p: 0 if p.get("declared") else 1)
        if cands:
            p = cands[0]
            return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?xLabel ?owner WHERE {{
  ?x rdf:type/rdfs:subClassOf* {_iri(cls['curie'])} .
  OPTIONAL {{ ?x {_iri(p['curie'])} ?v .
    OPTIONAL {{ ?v rdfs:label ?vl . FILTER(lang(?vl) IN ("zh", "")) }} }}
  OPTIONAL {{ ?x rdfs:label ?xl . FILTER(lang(?xl) IN ("zh", "")) }}
  BIND(COALESCE(?xl, REPLACE(STR(?x), "^.*[#/]", "")) AS ?xLabel)
  BIND(IF(!BOUND(?v), "—",
          COALESCE(?vl, IF(isLiteral(?v), STR(?v), REPLACE(STR(?v), "^.*[#/]", "")))) AS ?owner)
}} ORDER BY ?xLabel LIMIT 200""",
                    f"用模板取「{_cls_label(cls)}」的 <{_local(p['curie'])}>："
                    f"负责人是谁（对象属性取对方的 rdfs:label，字符串属性取字面量）")

    # 3) 计数：用 rdf:type/rdfs:subClassOf* 兼容子类实例——
    #    部门实例都挂在 DepartmentLevel1..7 上，只写 `a Department` 会得到 0，
    #    那不是「没数据」，是把子类实例当外人。
    if want_count and cls:
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT (COUNT(DISTINCT ?x) AS ?cnt)
WHERE {{ ?x rdf:type/rdfs:subClassOf* {_iri(cls['curie'])} }}""",
                f"用模板统计「{_cls_label(cls)}」的实例数（含子类实例）")

    # 4) 两个类之间的关系：找一条能连上的对象属性（方向由问句主语决定）
    if second:
        found = _connector(props, cls, second, _ancestors(model))
        if found:
            p, direction = found
            s_cls, o_cls = (cls, second) if direction == "fwd" else (second, cls)
            s_expr = (f"?s rdf:type/rdfs:subClassOf* {_iri(s_cls['curie'])} . "
                      f"?s {_iri(p['curie'])} ?o . "
                      f"?o rdf:type/rdfs:subClassOf* {_iri(o_cls['curie'])} .")
            # DISTINCT 必须有：个体常同时带「父类 + 子类」两个 rdf:type（部门个体既是
            # Department 又是 DepartmentLevelN），属性路径 rdf:type/rdfs:subClassOf* 会为
            # 每条边匹配出两条解 —— 27 个「员工→部门」会答成 54 行，数字翻倍但看着像有数据。
            return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?s ?o WHERE {{
  {s_expr}
}} LIMIT 200""",

                f"用模板沿 <{_local(p['curie'])}> 把「{_cls_label(s_cls)}」连到「{_cls_label(o_cls)}」"
                f"（{'正向' if direction == 'fwd' else '反向，谓词方向与问句主语相反'}）")

    # 5) 子类 / 类清单
    #    问句几乎总是把两件事一起问（「本体里有哪些类？类之间怎么继承的？」）。只答
    #    subClassOf 的话，7 条继承关系里只出现过 3 个类名——顶层类（Employee /
    #    Department / OrganizationalUnit）一个都不在结果里，「有哪些类」等于没答。
    #    故一张表答两件事：一行一个类，?super 列给父类，?cnt 给本类实例数。
    if _RE_SUB.search(q):
        if classes:
            vals = " ".join(_iri(c["curie"]) for c in classes)
            return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?class ?label ?super (COUNT(DISTINCT ?i) AS ?cnt) WHERE {{
  VALUES ?class {{ {vals} }}
  OPTIONAL {{ ?class rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }}
  OPTIONAL {{ ?i a ?class }}
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
  # 父类必须走子查询取，不能写成 OPTIONAL {{ ?class rdfs:subClassOf ?sp ... }}：
  # 那样没有父类的顶层类（Employee / Department / OrganizationalUnit /
  # 枚举类）会被 rdflib 直接丢掉整行——结果「7 个子类在、4 个顶层类全消失」，
  # 而且不报错、看着像本体里真的只有 7 个类。
  OPTIONAL {{ {{ SELECT ?class (GROUP_CONCAT(DISTINCT ?sn; separator=", ") AS ?sup) WHERE {{
        ?class rdfs:subClassOf ?sp . FILTER(isIRI(?sp))
        BIND(REPLACE(STR(?sp), "^.*[#/]", "") AS ?sn)
      }} GROUP BY ?class }} }}
  BIND(COALESCE(?sup, "") AS ?super)
}} GROUP BY ?class ?label ?super ORDER BY ?label LIMIT 200""",
                    f"用模板先列本体里的 {len(classes)} 个类（含本类实例数），"
                    f"?super 列给出父类——「有哪些类」与「怎么继承」一张表答完")
        return ("""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?sub ?subLabel ?super ?superLabel WHERE {
  ?sub rdfs:subClassOf ?super . FILTER(isIRI(?super))
  OPTIONAL { ?sub rdfs:label ?sl . FILTER(lang(?sl) IN ("zh", "")) }
  OPTIONAL { ?super rdfs:label ?pl . FILTER(lang(?pl) IN ("zh", "")) }
  BIND(COALESCE(?sl, REPLACE(STR(?sub), "^.*[#/]", "")) AS ?subLabel)
  BIND(COALESCE(?pl, REPLACE(STR(?super), "^.*[#/]", "")) AS ?superLabel)
} LIMIT 200""", "用模板列出类之间的 subClassOf（继承）关系")

    # 6) 单类的关联关系
    #    DISTINCT 同理：部门个体同时带 Department / DepartmentLevelN 两个类型，
    #    路径 rdf:type/rdfs:subClassOf* 会把同一条出边数两遍。
    if cls and _RE_REL.search(q):
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?s ?sLabel ?p ?o ?oLabel WHERE {{
  ?s rdf:type/rdfs:subClassOf* {_iri(cls['curie'])} .
  ?s ?p ?o . FILTER(isIRI(?o))
  FILTER(?p != <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>)
  OPTIONAL {{ ?s rdfs:label ?sl . FILTER(lang(?sl) IN ("zh", "")) }}
  OPTIONAL {{ ?o rdfs:label ?ol . FILTER(lang(?ol) IN ("zh", "")) }}
  BIND(COALESCE(?sl, REPLACE(STR(?s), "^.*[#/]", "")) AS ?sLabel)
  BIND(COALESCE(?ol, REPLACE(STR(?o), "^.*[#/]", "")) AS ?oLabel)
}} LIMIT 200""", f"用模板看「{_cls_label(cls)}」实例指向了哪些对象（含子类实例）")

    # 7) 单类实例列表（只取 ?x：单元里已经带 rdfs:label，再拼一列 label 会重复显示两遍）
    if cls:
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT DISTINCT ?x WHERE {{
  ?x rdf:type/rdfs:subClassOf* {_iri(cls['curie'])} .
}} LIMIT 100""", f"用模板列出「{_cls_label(cls)}」的实例（含子类实例）")


    # 8) 什么都没匹配到：退回结构盘点，至少告诉用户能问什么
    #
    #    这里**不能**用 `?class a owl:Class` 全图扫。画布上还躺着 12 个本体没定义的类
    #    （Project / Person / Team / Risk …），全图扫会把它们当作「本体里的类」端给
    #    用户：「调程序/问问题」的兜底反而成了越界概念最大的出口 —— 12 个类全在这儿
    #    露脸，而 `_app_classes` 白名单在别处又把它们挡掉了，前后自相矛盾。
    #    改用与第 5 支同口径的 VALUES 白名单：只列本体定义的类。
    if classes:
        vals = " ".join(_iri(c["curie"]) for c in classes)
        return (f"""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
SELECT ?class ?label (COUNT(DISTINCT ?i) AS ?cnt) WHERE {{
  VALUES ?class {{ {vals} }}
  OPTIONAL {{ ?class rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }}
  OPTIONAL {{ ?i a ?class }}
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
}} GROUP BY ?class ?label ORDER BY DESC(?cnt) LIMIT 50""",
                f"没识别出问的是哪个类，先列出《人员基本信息本体》定义的 {len(classes)} 个类、"
                f"各有多少实例（换个更具体的说法再问）")
    return ("""PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
SELECT ?class ?label (COUNT(DISTINCT ?i) AS ?cnt) WHERE {
  { ?class a owl:Class } UNION { ?class a rdfs:Class }
  OPTIONAL { ?class rdfs:label ?l . FILTER(lang(?l) IN ("zh", "")) }
  OPTIONAL { ?i a ?class }
  BIND(COALESCE(?l, REPLACE(STR(?class), "^.*[#/]", "")) AS ?label)
} GROUP BY ?class ?label ORDER BY DESC(?cnt) LIMIT 50""",
            "没识别出问的是哪个类，先列出图上所有类、各有多少实例（换个更具体的说法再问）")


# ---------------------------------------------------------------- 答案文本
def _clean_rows(rows: list[list[dict]]) -> list[list[dict]]:
    """把「未绑定变量」的单元格规整成空值。

    SPARQL 里 OPTIONAL 没命中时 rdflib 给的是 ``None``，共用的 ``query._cell`` 会把它
    当匿名节点拼成 ``"_:None"``——直接渲染就是给用户看 ``obj=_:None``。这是显示层的事，
    在这里就地归一，不动共用代码（KG 问答那边还有自己的呈现逻辑）。
    """
    out = []
    for r in rows or []:
        cells = []
        for c in r or []:
            v = str((c or {}).get("v") or "") if isinstance(c, dict) else ""
            cells.append({"v": "", "k": "x"} if v in ("", "_:None") else c)
        out.append(cells)
    return out


def _cell_text(cell: dict) -> str:
    """单元格 → 人读文本。

    IRI 一律优先用 ``rdfs:label``（``query.run`` 已经把 label 抓出来了），
    没有 label 才退回局部名——直接甩一串
    ``https://pdt.example.org/ontology/employee-organization/class/Department``
    给用户看等于没回答。
    """
    if not isinstance(cell, dict):
        return str(cell)
    v = str(cell.get("v") or "")
    if not v:
        return "—"
    if cell.get("k") == "u":
        return str(cell.get("label") or "").strip() or _local(v)
    return v


def _is_empty_result(rows: list[list[dict]]) -> bool:
    """答案是否「什么都没查出来」——零行，或聚合回来的全是一行 0。

    ``COUNT`` 在没有匹配时返回的是一行 ``0``，不是零行；只看 ``len(rows)`` 会把
    「图里没数据」当成「查到了一个确定的 0」。
    """
    if not rows:
        return True
    for r in rows:
        for c in r or []:
            v = str((c or {}).get("v") or "").strip()
            if v not in ("", "0", "0.0", "0.00"):
                return False
    return True


def _answer_text(vars_: list[str], rows: list[list[dict]]) -> str:
    if not rows:
        return ("没有查到符合条件的数据（0 行）。可能这份草稿还没有实例数据，"
                "或问题里的条件在图上一行都对不上——换个问法/换个数据源再试。")
    if len(vars_) == 1:
        vals = [_cell_text(r[0]) for r in rows if r]
        if len(rows) == 1:
            return f"答案：{vals[0]}"
        return f"共 {len(rows)} 条：{'、'.join(vals[:12])}" + ("…" if len(vals) > 12 else "")
    if len(rows) == 1:
        pairs = "，".join(f"{v}={_cell_text(c)}" for v, c in zip(vars_, rows[0]))
        return f"查到 1 条记录：{pairs}"
    head = "；".join(" / ".join(_cell_text(c) for c in r) for r in rows[:3])
    return f"共 {len(rows)} 条结果。前 3 条：{head}" + ("…" if len(rows) > 3 else "")


def _stats(model: dict, graph: Graph) -> dict:
    st = model.get("stats") or {}
    return {
        "triples": len(graph),
        "classes": st.get("classes", 0),
        "object_props": st.get("object_props", 0),
        "data_props": st.get("data_props", 0),
        "individuals": st.get("individuals", 0),
    }


# ---------------------------------------------------------------- 对外入口
def answer(spec: dict, question: str, data: dict | None = None,
           use_llm: bool = True) -> dict:
    """在「当前草稿本体 + 可选实例数据」上回答问题。

    返回 ``{ok, engine, intent, answer, sparql, result, attempts, stats, note}``，
    ``engine`` 如实标注答案是怎么来的：``llm`` = 模型写了能跑通的查询，
    ``template`` = 模型不可用/两次都失败，用关键词模板兜底。**两种情况下事实都来自图数据**。
    """
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题为空"}
    if not (spec or {}).get("nodes"):
        return {"ok": False, "error": "当前草稿还没有节点：先在画布上把 pipeline 搭起来再问"}

    try:
        graph, load = build_graph(spec, data)
    except Exception as exc:  # noqa: BLE001  草稿结构不合法 / JSON-LD 编译失败
        return {"ok": False, "error": f"草稿转图失败：{type(exc).__name__}: {exc}"}

    sess = _DraftSession(graph)
    model = sess.model()
    stats = _stats(model, graph)
    if not stats["classes"]:
        return {"ok": False, "error": "这份草稿没有编译出任何类：至少需要一个业务对象节点",
                "stats": stats, **load}

    attempts: list[dict] = []
    sparql, intent, engine, last_error = "", "", "", ""

    # 超纲拦截必须放在**最前面**，连模型都不问：
    # 这类问句（平均司龄 / 哪个部门人最多）模型很配合，会给一条语法完全正确、
    # 也能跑出数的查询——只不过数的是另一件事。放在 LLM 之后拦是拦不住的
    # （查询已经成功、行数大于 0，就轮不到兜底分支了）。
    # 同时这也是给「engine」的诚实标注：这条答案既不是模型写的、也不是模板凑的，
    # 是照着本体能力边界拒答的，所以 engine 记 ``guard``。
    refuse = _unsupported(question)
    if refuse:
        return {
            "ok": True, "engine": "guard", "intent": refuse[1],
            "answer": refuse[0],
            "sparql": "", "result": {"vars": [], "rows": [], "kind": "SELECT"},
            "attempts": attempts, "stats": stats,
            "note": ("问句要的量超出本体的表达力（派生指标 / 排序取极值），"
                     "按本体口径拒答，未生成查询、未拿计数或列举凑数。"),
            **load,
        }

    # 血缘类问题先看台账：导出的 pdt:Pipeline 是单例，画布上没有处理节点时它也照样
    # 存在（stageStatus 全 0）。不先分辨，就会把「没搭血缘」答成「血缘为零」。
    if _RE_LINEAGE.search(question.lower()):
        ov = _lineage_overview(spec)
        if not ov["procs"]:
            return {
                "ok": True, "engine": "template", "intent":
                    "问数据血缘：当前草稿没有数据处理节点（sample/process），导出里 pdt:feedsObjects 为空",
                "answer": ("当前草稿没有数据处理节点，所以没有数据血缘可查。"
                           "画布上补一个「数据处理」节点、再把它的结果勾到业务对象上，"
                           "这条问题才会有答案（导出后会落成 pdt:Pipeline.feedsObjects）。"),
                "sparql": "", "result": {"vars": [], "rows": [], "kind": "SELECT"},
                "attempts": [], "stats": stats,
                "note": "答案来自草稿台账（不是图查询）：草稿里没有 process 节点，图上的 Pipeline 节点是导出的空壳。",
                **load,
            }
        if not any(p["objects"] for p in ov["procs"]):
            names = "、".join(f"「{p['name']}」" for p in ov["procs"])
            return {
                "ok": True, "engine": "template", "intent":
                    f"问数据血缘：有 {len(ov['procs'])} 个处理节点，但都没登记 link_objects",
                "answer": (f"有 {len(ov['procs'])} 个数据处理节点（{names}），但都还没关联业务对象，"
                           "数据血缘是断的——导出后 pdt:Pipeline.feedsObjects 不会有边。"
                           "在节点详情的「数据血缘」里勾选，或直接画一条「处理 → 对象」的连线即可。"),
                "sparql": "", "result": {"vars": [], "rows": [], "kind": "SELECT"},
                "attempts": [], "stats": stats,
                "note": "答案来自草稿台账（不是图查询）：处理节点存在但 link_objects 为空。",
                **load,
            }

    # 模型那条「语法通、0 行」的结果先留着：它可能是真答案（本体确实没有这种人），
    # 也可能只是查错了。留着才能在模板复核也落空时把模型的原答案交出去，
    # 而不是拿一句通用的“没识别出”盖掉一个本来正确的空结论。
    llm_spa, llm_it, llm_res = "", "", None
    cross_checked = False

    if use_llm:
        for round_no in (1, 2):
            try:
                spa, it, missing = _llm_sparql(model, question,
                                               "" if round_no == 1 else last_error)
            except Exception as exc:  # noqa: BLE001  网关不可用 / 超时都走模板兜底
                last_error = f"LLM 调用失败：{type(exc).__name__}: {str(exc)[:160]}"
                attempts.append({"engine": f"llm#{round_no}", "error": last_error})
                break
            if missing:
                # 本体里没有这个属性。**不能降到模板**：模板只会拿别的属性凑一个数
                # （问「员工的平均司龄」答成「27 个人」就是这么来的），凑出来的数
                # 比拒答有害得多——用户会把假数当真。
                return {
                    "ok": True, "engine": "llm", "intent": it,
                    "answer": (f"当前本体里没有能回答这个问题的属性（{it}）。"
                               f"图上只有类、属性与已加载的实例，没有这个指标的计算口径，"
                               f"所以我不给数——给一个凑出来的数比说不知道更糟。"
                               f"要答这类问题，得先在这样本里补上对应属性（或在 pipeline 里加一个派生字段）。"),
                    "sparql": "", "result": {"vars": [], "rows": [], "kind": "SELECT"},
                    "attempts": attempts, "stats": stats,
                    "note": "模型判定本体缺少该指标所需属性，按本体口径如实拒答（未降级到模板凑数）。",
                    **load,
                }
            if not spa:
                last_error = "模型没有返回可用的 SPARQL"
                attempts.append({"engine": f"llm#{round_no}", "error": last_error})
                continue
            res = kg_query.run(graph, spa, prefixes=model.get("prefixes") or {})
            n_rows = len(res.get("rows") or [])
            attempts.append({"engine": f"llm#{round_no}", "ok": res.get("ok"),
                             "rows": n_rows, "ms": res.get("ms"),
                             "error": res.get("error", "")})
            if res.get("ok"):
                # ASK 的答案是布尔值，没有行也算查到了
                if n_rows or res.get("kind") == "ASK":
                    sparql, intent, engine, last_error = spa, it, "llm", ""
                    break
                # ── 这里以前是直接 break 的：`ok` 只说明查询**语法可执行**，
                # 不说明查到了东西。于是「语法全对、恒 0 行」的查询被当成成功，
                # 页面上就出现「没有查到符合条件的数据（0 行）」——而数据明明在。
                # 现在把 0 行当「未证实」：记下来，带着诊断提示让模型改一次，
                # 改不出来再交给模板按本体口径复核。
                if not llm_spa:
                    llm_spa, llm_it, llm_res = spa, it, res
                last_error = (
                    "上一次生成的查询语法没问题，但返回 0 行。最常见的原因是："
                    "① 用 rdfs:label 等值匹配实例——实例标签常带括号英文名"
                    "（「张三 (Zhang San)」），必须改用 FILTER(CONTAINS(STR(?l), \"张三\"))；"
                    "② 把派生函数 fn_derive_* 当成谓词（图里没有这类三元组）；"
                    "③ 类名/属性名与清单不一致。请按这三条逐条核对后重写。")
                continue
            last_error = res.get("error", "")

    if not sparql:
        # 问句点名了一个图里没有的人：不能交给模板——模板会退化成「按类扫全表」，
        # 把 27 个别人的上级当成这个人的答案交出去（见 _unknown_subject 的说明）。
        # 模型那边已经 0 行，这里再确认一次「图里确实没有这个人」，如实回答。
        unknown = _unknown_subject(graph, model, question)
        if unknown:
            n_ind = int(stats.get("individuals") or 0)
            hint = (f"图里现有 {n_ind} 个实例，可以先问「列出所有员工」看看都有谁。"
                    if n_ind else "当前草稿还没有加载实例数据。")
            return {
                "ok": True, "engine": "template",
                "intent": f"问句点名的「{unknown}」在图里找不到对应实例",
                "answer": (f"当前这份草稿的图里没有叫「{unknown}」的实例，"
                           f"所以「{unknown}的…」这个问题没法落到具体主语上。"
                           f"{hint}"),
                "sparql": "", "result": {"vars": [], "rows": [], "kind": "SELECT"},
                "attempts": attempts, "stats": stats,
                "note": ("模型查了 0 行；模板复核时发现问句点名的个体不在图上，"
                         "按「图里没有这个人」如实回答，没有退化成全类扫表。"),
                **load,
            }
        # 把图一起传下去：模板里「按实例名锚定」那支要看实例的 rdfs:label
        spa, it = _template(model, question, graph)
        res = kg_query.run(graph, spa, prefixes=model.get("prefixes") or {})
        t_rows = len(res.get("rows") or [])
        attempts.append({"engine": "template", "ok": res.get("ok"), "rows": t_rows,
                         "ms": res.get("ms"), "error": res.get("error", "")})
        # 模板的「没识别出问的是哪个类」只是兜底清单，不是对问题的回答。
        # 拿它去盖掉模型给出的「真 0 行」结论，等于把「查了、确实没有」
        # 降级成「没听懂」——两种错法都不该有。
        generic = it.startswith("没识别出")
        if res.get("ok") and (t_rows or res.get("kind") == "ASK") and not generic:
            sparql, intent, engine = spa, it, "template"
        elif llm_res is not None:
            sparql, intent, engine, res = llm_spa, llm_it, "llm", llm_res
            cross_checked = True
        elif not res.get("ok"):
            return {"ok": False, "error": res.get("error") or "查询执行失败",
                    "sparql": spa, "attempts": attempts, "engine": "template",
                    "stats": stats, **load}
        else:
            sparql, intent, engine = spa, it, "template"

    if res.get("kind") == "ASK":
        answer_text = "是的。" if res.get("boolean") else "不是。"
    else:
        res["rows"] = _clean_rows(res.get("rows") or [])
        answer_text = _answer_text(res.get("vars") or [], res.get("rows") or [])
        # 「答案：0」和「没装数据」在结果上都是查不出东西，但对用户是两件事：
        # 前者是个结论（这个类确实没有实例），后者只是没加载实例、图里只有 TBox。
        # 不区分的话，问「员工有多少条」在纯本体上会得到一个看起来很确定的 0。
        # 注意 COUNT 查的是**一行 0**（不是零行），所以两种「空」都要认。
        if _is_empty_result(res.get("rows") or []) and not load.get("instances_given") \
                and not stats.get("individuals"):
            answer_text = ("当前只有本体结构（类 / 属性 / 关系），还没有加载实例数据，"
                           "所以统计类问题只能得到 0——这不代表「没有数据」。"
                           "在问答面板把数据源换成「模拟数据」或「真实数据」再问一次。"
                           + (f"（本次查的是：{answer_text}）" if answer_text else ""))

    if cross_checked:
        note = ("模型生成的查询语法可执行但返回 0 行；已按本体口径用关键词模板复核过，"
                "同样是 0 行，所以这个「空」是有依据的结论，不是没查到。")
    elif engine == "llm":
        note = "模型生成的查询已在草稿编译出的图上执行，答案来自图数据。"
    else:
        note = "模型不可用或生成的查询跑不通，已用关键词模板兜底（结果同样来自图数据）。"

    return {
        "ok": True, "engine": engine, "intent": intent, "answer": answer_text,
        "sparql": sparql, "result": res, "attempts": attempts,
        "stats": stats, "note": note,
        **load,
    }


def _is_enum(c: dict) -> bool:
    """枚举类：导出时落在 ``.../enum/<名>`` 下，label 习惯写成「XX枚举」。

    枚举不是业务实体——它有实例（A/I/T），但「有几个实例」是没有意义的问法，
    该问的是「有哪些取值」。
    """
    return "/enum/" in str(c.get("uri") or "") or _cls_label(c).endswith("枚举")


def suggest(spec: dict, limit: int = 8, data: dict | None = None) -> dict:
    """按**当前草稿**现场生成推荐问题——不是写死的话术，全部由编译出的类/属性和实例数推出来。

    每个问题都保证「图上有东西可查」：没实例的类只推结构类问题（属性/关系），
    有实例的类才推「列出/有多少」；没有自反对象属性就不推「上级的上级」。
    ``data`` 与问答同口径：带上实例载荷，推荐问题才会出现「有多少条」这类计数题。
    """
    try:
        graph, load = build_graph(spec, data)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"草稿转图失败：{type(exc).__name__}: {exc}",
                "questions": []}
    model = kg_model.build_model(graph)
    all_classes = _app_classes(model)
    classes = [c for c in all_classes if not _is_enum(c)]
    enums = [c for c in all_classes if _is_enum(c)]
    props = _app_props(model)
    obj_props = [p for p in props if p.get("declared")]
    stats = _stats(model, graph)
    if not all_classes:
        return {"ok": False, "error": "这份草稿还没有编译出任何类", "questions": [],
                "stats": stats, **load}

    out: list[dict] = []

    def add(q: str, why: str, kind: str) -> None:
        if q and all(x["question"] != q for x in out):
            out.append({"question": q, "why": why, "kind": kind})

    # 1) 结构盘点（永远可问）
    #    数字分两个口径说清楚：`stats['classes']` 是画布规模（用户在页面上看到的那个数），
    #    `len(all_classes)` 是《人员基本信息本体》实际定义、也真正参与作答的类数。
    #    只报前者会让人以为画布上的 22 个类都能问；只报后者会和页面上的数字对不上。
    add("本体里有哪些类？类之间怎么继承的？",
        f"当前本体有 {stats['classes']} 个类，其中 "
        f"{sum(1 for c in all_classes if c.get('depth'))} 个有父类"
        f"；按《人员基本信息本体》口径收录 {len(all_classes)} 个",
        "structure")

    # 2) 有实例的业务类：先推计数，再推列举（这两个能立刻验证「图里到底有没有数」）。
    #    排序偏向「有关系的类」：叶子子类实例多（七级部门一抓一大把），但问它「有多少条」
    #    远不如问「员工」「部门」有意义——骨架类才是用户想核对的对象。
    with_inst = [c for c in classes if int(c.get("instances") or 0) > 0]
    with_inst.sort(key=lambda c: (-(len(c.get("obj_out") or []) + len(c.get("obj_in") or [])),
                                  -int(c.get("instances") or 0)))
    for c in with_inst[:2]:
        add(f"「{_cls_label(c)}」一共有多少条？",
            f"图里有 {c.get('instances')} 个实例，可直接计数核对", "count")
        add(f"列出所有「{_cls_label(c)}」",
            f"图里有 {c.get('instances')} 个「{_cls_label(c)}」实例", "list")

    # 3) 关系类问题：优先「两个业务类怎么连」——这是本体比宽表强的地方
    seen_pair = set()
    for p in obj_props:
        dom, rng = p.get("domain") or [], p.get("range") or []
        if not dom or not rng:
            continue
        d = next((c for c in classes if c.get("uri") in dom), None)
        r = next((c for c in classes if c.get("uri") in rng), None)
        if not d or not r or d.get("uri") == r.get("uri"):
            continue
        key = frozenset((d.get("uri"), r.get("uri")))   # 员工→部门 与 部门→员工 是同一问
        if key in seen_pair:
            continue
        seen_pair.add(key)
        add(f"「{_cls_label(d)}」和「{_cls_label(r)}」是怎么关联的？",
            f"对象属性「{_prop_label(p)}」把两者连起来（{_cls_label(d)} → {_cls_label(r)}）",
            "relation")
        if len([x for x in out if x["kind"] == "relation"]) >= 2:
            break

    # 4) 层级传递：只在真有「两端同域」的对象属性时推（否则问了也查不出东西）
    #
    #    这里不能把「domain∩range 相同」叫「自反关系」：自反（owl:ReflexiveProperty）
    #    说的是每个个体都指向自己，而我们的 hasLineManager 在 TTL 里是
    #    owl:IrreflexiveProperty（自己不是自己的主管），意思正好相反。说过头了，
    #    用户拿这句话去核对本体文件，会发现问答在替本体编公理。
    #    所以：问题按「能不能求传递闭包」来提，理由只用本体真有的那几条公理。
    self_props = [p for p in obj_props
                  if (set(p.get("domain") or []) & set(p.get("range") or []))]
    if self_props:
        # 优先挑本体显式声明了传递性的那条——「再往上一层是谁」这问只有传递才有意义；
        # 都没声明传递时，退而求其次挑声明了不自反的（至少能问「有没有人是自己的X」）。
        p = (next((x for x in self_props if x.get("transitive")), None)
             or next((x for x in self_props if x.get("irreflexive")), None)
             or self_props[0])
        dom_cls = next((c for c in classes if c.get("uri") in (p.get("domain") or [])), None)
        who = _cls_label(dom_cls) if dom_cls else "这类对象"
        if p.get("transitive"):
            add(f"「{who}」的 {_prop_label(p)} 再往上一层是谁？（要看传递闭包）",
                f"「{_prop_label(p)}」两端同域（{who} → {who}），本体已声明 "
                f"owl:TransitiveProperty，可直接沿属性路径求传递闭包",
                "hierarchy")
        else:
            add(f"「{who}」里有没有人的 {_prop_label(p)} 是自己？",
                f"「{_prop_label(p)}」两端同域（{who} → {who}），"
                + ("本体声明 owl:IrreflexiveProperty，答案应当没有"
                   if p.get("irreflexive")
                   else "按属性路径 + 求实际传递闭包"),
                "hierarchy")

    # 5) 属性清单（数量按「含子类声明」算，和模板问答给出的条数保持一致）
    up = _ancestors(model)
    for c in classes[:2]:
        n = len(_dedup_props(_props_of(props, c, up)))
        if n:
            add(f"「{_cls_label(c)}」有哪些属性和关系？",
                f"该类的 domain 下声明了 {n} 个属性/关系（含子类声明的）", "props")

    # 6) 枚举取值（枚举单独问「有哪些取值」，不要问「有几个」）
    if enums:
        add(f"「{_cls_label(enums[0])}」有哪些取值？",
            "枚举类落在 .../enum/ 下，取值就是它的个体（A/I/T 这种）", "enum")

    # 7) 数据血缘：pipeline 跑完最该问、也最能体现「与设计一致」的一问
    add("这套流水线的数据处理结果喂给了哪些业务对象？",
        "数据血缘落在 pdt:Pipeline.feedsObjects 上，可直接沿边查", "lineage")

    return {"ok": True, "questions": out[:max(1, limit)], "stats": stats, **load}


def self_check() -> dict:
    """自检：一份最小草稿能不能走完「装图 → 建模型 → 模板问答 → 推荐问题」。

    最小草稿按**真实画布契约**写：实体把属性放在 ``fields.attributes`` 文本块里
    （``名:类型:必填[:枚举值]`` 逐行），关系用 ``source_entity`` / ``target_entity``
    定 domain/range。用错形状（比如把属性写成独立 attr 节点）编译不出类，
    自检就会假失败，掩盖真正的问题。
    """
    spec = {
        "version": "1.0",
        "nodes": [
            {"id": "o1", "kind": "entity", "fields": {
                "name": "Employee", "label": "员工", "primary_key": "employeeId",
                "attributes": "employeeId:string:1\nemployeeName:string:1\nhrStatus:enum:1:A|I|T"}},
            {"id": "o2", "kind": "entity", "fields": {
                "name": "Department", "label": "部门", "primary_key": "departmentName",
                "attributes": "departmentName:string:1\ndeptLevel:integer:1"}},
            {"id": "r1", "kind": "relation", "fields": {
                "name": "belongsToDepartment", "source_entity": "Employee",
                "target_entity": "Department", "cardinality": "1:N"}},
        ],
        "edges": [
            {"from": "o1", "to": "o2"},
            {"from": "r1", "to": "o1"},
        ],
    }
    try:
        graph, load = build_graph(spec, None)
        model = kg_model.build_model(graph)
        n_classes = len(_app_classes(model))
        # 模板路径（不调 LLM，自检要能在离线环境跑）
        spa, _it = _template(model, "员工一共有多少条？")
        res = kg_query.run(graph, spa, prefixes=model.get("prefixes") or {})
        sug = suggest(spec, limit=5)
        ok = (n_classes >= 2 and bool(res.get("ok")) and bool(sug.get("questions")))
        return {"pass": ok, "classes": n_classes, "triples": len(graph),
                "template_ok": bool(res.get("ok")), "template_error": res.get("error", ""),
                "suggestions": len(sug.get("questions") or []),
                "note": "草稿装图 → 类模型 → 模板问答 → 推荐问题，四步都通" if ok else
                        "四步里有不通的（看 template_error / suggestions）"}
    except Exception as exc:  # noqa: BLE001
        return {"pass": False, "error": f"{type(exc).__name__}: {exc}"}
