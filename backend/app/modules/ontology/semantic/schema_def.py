"""Schema-as-Code 定义层（V1.3 语义层方案 · 能力 1）。

定位：**在现有 CQ V2 Schema Registry 之上做增强，不推倒重来**。
- Pydantic 2 定义实体/关系/属性/约束/特性的强类型契约；
- 从 `cq_v2.registry.default_schema()` 派生增强层（继承、对称/互逆/传递、
  必填/枚举/值域、业务规则），Registry 仍是 LinkML 建模源；
- 导出标准 JSON Schema（既可给人审阅，也可直接喂给写入侧校验）；
- 纯 dict 投影 `to_schema_dict()` 供约束/推理/规则三个纯函数引擎离线消费，
  不依赖 Web/DB 框架，方便零库单测。

设计边界（照实披露）：新增的继承链、特性、属性必填/枚举只作用于
`OntologyService.write_instance / write_relation` 这条新写入口，
不回写 `ontology.entities` / `knowledge.facts` 等既有权威数据。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 强类型契约

RANGES = ("string", "integer", "number", "boolean", "date", "datetime", "enum")
RULE_TYPES = ("integrity", "derivation", "authorization")
CARDINALITIES = ("one_to_one", "one_to_many", "many_to_one", "many_to_many")
OBJECT_KINDS = ("entity", "event", "transaction", "reference", "association")
SENSITIVITY_LEVELS = ("public", "internal", "confidential", "restricted")
SOURCE_MODES = ("materialized", "federated", "action_created")


class ObjectIdentityRule(BaseModel):
    """跨数据源识别同一业务对象的身份规则。"""
    source_system: str = ""
    key_properties: list[str] = Field(default_factory=list)
    normalization: str = "trim"
    priority: int = 100
    unique: bool = True


class ObjectSourceBinding(BaseModel):
    """对象到真实数据资产的绑定；一个对象允许多个数据源共同供数。"""
    source_id: str
    dataset: str = ""
    table: str = ""
    primary_key: list[str] = Field(default_factory=list)
    property_mappings: dict[str, str] = Field(default_factory=dict)
    mode: str = "materialized"
    filter: dict[str, Any] = Field(default_factory=dict)


class InterfaceDef(BaseModel):
    """可复用业务接口：多个对象类型共享属性形状和能力契约。"""
    name: str
    label: str = ""
    description: str = ""
    properties: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class StateMachineDef(BaseModel):
    """属性状态机（P0-1）：合法状态集 + 允许的状态转换 + 初始态。

    只描述「状态属性」的合法流转；写入时若同时给出旧值（服务端从库取，
    不接受客户端自报），即可做 old→new 转换校验（fail-closed）。
    """
    states: list[str] = Field(default_factory=list)
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    initial: str = ""
    description: str = ""

    def as_dict(self) -> dict:
        return {"states": list(self.states),
                "transitions": {k: list(v) for k, v in self.transitions.items()},
                "initial": self.initial, "description": self.description}


class AttributeDef(BaseModel):
    """属性定义：类型、业务语义、敏感级别、单位、时间角色和来源映射。"""
    name: str
    range: str = "string"
    required: bool = False
    semantic_type: str = ""
    unit: str = ""
    sensitivity: str = "internal"
    temporal_role: str = ""
    identifier: bool = False
    title: bool = False
    source_mappings: list[dict[str, str]] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    state_machine: StateMachineDef | None = None
    description: str = ""


class EntityTypeDef(BaseModel):
    """业务对象类型：业务身份、展示属性、多源绑定、接口与生命周期。"""
    name: str
    label: str = ""
    description: str = ""
    object_kind: str = "entity"
    parent: str = ""
    aliases: list[str] = Field(default_factory=list)
    title_property: str = ""
    identity_rules: list[ObjectIdentityRule] = Field(default_factory=list)
    backing_sources: list[ObjectSourceBinding] = Field(default_factory=list)
    interfaces: list[str] = Field(default_factory=list)
    lifecycle_property: str = ""
    attributes: list[AttributeDef] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class Characteristics(BaseModel):
    """关系语义特性：轻量推理的三个可配置开关（务实范围，非 OWL 推理机）。"""
    symmetric: bool = False
    transitive: bool = False
    inverse_of: str = ""
    max_transitive_depth: int = 3


class RelationTypeDef(BaseModel):
    """业务关系：端点、基数、反向关系、有效时间和关系自身属性。"""
    name: str
    label: str = ""
    description: str = ""
    source_types: list[str] = Field(default_factory=lambda: ["*"])
    target_types: list[str] = Field(default_factory=lambda: ["*"])
    cardinality: str = "many_to_many"
    inverse_of: str = ""
    relationship_kind: str = "business"
    temporal: bool = False
    valid_from_property: str = ""
    valid_to_property: str = ""
    backing_sources: list[ObjectSourceBinding] = Field(default_factory=list)
    properties: list[AttributeDef] = Field(default_factory=list)
    characteristics: Characteristics = Field(default_factory=Characteristics)
    constraints: dict[str, Any] = Field(default_factory=dict)


class BusinessRuleDef(BaseModel):
    """业务规则：condition 匹配 → action；结果只作证据，不改写权威 Fact。"""
    name: str
    rule_type: str = "integrity"
    condition: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    enabled: bool = True


class SemanticSchema(BaseModel):
    """完整语义 Schema 快照（可绑定具体 ontology_version 冻结）。"""
    schema_version: str = "sem-1.0.0"
    ontology_version: str = "v1.0.0"
    entity_types: list[EntityTypeDef] = Field(default_factory=list)
    relation_types: list[RelationTypeDef] = Field(default_factory=list)
    interfaces: list[InterfaceDef] = Field(default_factory=list)
    business_rules: list[BusinessRuleDef] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source: str = "cq_v2.registry+semantic_enrichment"


# ---------------------------------------------------------------- 增强层定义
# 只叠加在 Registry 8 类 12 关系之上，新增 4 个子类 + 1 个互逆关系，
# 全部语义自洽，用于证明「继承 / 对称 / 互逆 / 传递」确实可跑通。
_INHERITANCE = {
    "Employee": ("Person", "员工"),
    "Manager": ("Employee", "管理者"),
    "Department": ("OrganizationUnit", "部门"),
    "Team": ("OrganizationUnit", "团队"),
}

_ATTRS: dict[str, list[AttributeDef]] = {
    "Person": [AttributeDef(name="level", range="string", description="职级"),
               AttributeDef(name="job_title", range="string", description="岗位")],
    "Employee": [AttributeDef(name="full_name", range="string", required=True,
                              description="姓名（必填，取自 facts.full_name）"),
                 AttributeDef(name="department", range="string",
                              description="部门"),
                 AttributeDef(name="employee_no", range="string",
                              pattern=r"^E[0-9]{5}$",
                              description="工号（选填，facts 无工号源时可空）")],
    "Project": [AttributeDef(name="due_date", range="date", required=True,
                             description="截止日期（必填）"),
                AttributeDef(name="approval_status", range="enum",
                             enum_values=["pending", "approved", "rejected", "archived"],
                             state_machine=StateMachineDef(
                                 states=["pending", "approved", "rejected", "archived"],
                                 transitions={
                                     "pending": ["approved", "rejected"],
                                     "approved": ["archived"],
                                     "rejected": ["pending"],   # 驳回后可重新提交
                                     "archived": [],            # 终态
                                 },
                                 initial="pending",
                                 description="审批状态机：待审批→通过/驳回；通过→归档；驳回→重新提交；归档为终态"),
                             description="审批状态（枚举 + 状态机约束）")],
    "Risk": [AttributeDef(name="risk_level", range="enum", required=True,
                          enum_values=["low", "medium", "high"],
                          description="风险等级（必填枚举）")],
}

# 关系特性覆盖：仅对语义确凿的关系开启，避免「什么都推理」的过度设计。
_CHARACTERISTICS: dict[str, Characteristics] = {
    "collaborates_with": Characteristics(symmetric=True, max_transitive_depth=1),
    "reports_to": Characteristics(transitive=True, max_transitive_depth=3),
    "member_of": Characteristics(transitive=True, max_transitive_depth=3),
    "references": Characteristics(transitive=True, max_transitive_depth=2),
}

# 互逆对：新增 managed_by 作为 is_project_manager_of 的逆关系（不写回 Registry）。
_INVERSE_EXTRA = RelationTypeDef(
    name="managed_by", label="被管理于",
    source_types=["Project"], target_types=["Person"],
    cardinality="many_to_one",
    characteristics=Characteristics(inverse_of="is_project_manager_of"),
)

_DEFAULT_RULES = [
    BusinessRuleDef(
        name="R-INTEGRITY-01", priority=10,
        condition={"all": [
            {"field": "operation", "op": "eq", "value": "write_relation"},
            {"field": "relation_type", "op": "eq", "value": "is_project_manager_of"},
        ]},
        action={"rule_type": "integrity", "assert": "source_type_is_Employee_or_descendant",
                "message": "项目负责人必须为员工（含子类）"},
    ),
    BusinessRuleDef(
        name="R-DERIVATION-01", priority=50,
        condition={"all": [
            {"field": "operation", "op": "eq", "value": "write_relation"},
            {"field": "relation_type", "op": "eq", "value": "reports_to"},
        ]},
        action={"rule_type": "derivation", "emit": "org_closure_candidate",
                "note": "传递闭包只产出候选证据，不改写权威关系"},
    ),
    BusinessRuleDef(
        name="R-AUTHZ-01", priority=5,
        condition={"field": "visibility", "op": "eq", "value": "restricted"},
        action={"rule_type": "authorization", "effect": "deny",
                "message": "restricted 可见性不得作为决策证据"},
    ),
    BusinessRuleDef(
        name="R-CQ-TRACE-01", priority=1, rule_type="derivation",
        condition={"all": [
            {"field": "operation", "op": "eq", "value": "cq_execute"},
        ]},
        action={"rule_type": "derivation", "emit": "cq_result_as_decision_evidence",
                "note": "每次 CQ 执行把结果登记为决策确定性证据，写入 Decision Trace"},
    ),
]


def from_registry(reg: dict | None = None) -> SemanticSchema:
    """以 CQ V2 Registry 为建模源，叠加语义增强层，产出强类型 SemanticSchema。"""
    if reg is None:
        from app.modules.ontology.cq_v2 import registry as _reg
        reg = _reg.default_schema()

    ent: dict[str, EntityTypeDef] = {}
    for name, meta in (reg.get("entity_types") or {}).items():
        ent[name] = EntityTypeDef(
            name=name, label=(meta or {}).get("label", ""),
            parent=(meta or {}).get("parent", "") or "",
            aliases=list((meta or {}).get("aliases") or []),
            attributes=list(_ATTRS.get(name, [])),
        )
    for name, (parent, label) in _INHERITANCE.items():
        ent[name] = EntityTypeDef(name=name, label=label, parent=parent,
                                  attributes=list(_ATTRS.get(name, [])))

    rel: dict[str, RelationTypeDef] = {}
    for name, meta in (reg.get("relation_types") or {}).items():
        src = (meta or {}).get("source") or "*"
        dst = (meta or {}).get("target") or "*"
        rel[name] = RelationTypeDef(
            name=name, label=(meta or {}).get("label", ""),
            source_types=[src] if isinstance(src, str) else list(src),
            target_types=[dst] if isinstance(dst, str) else list(dst),
            cardinality=(meta or {}).get("cardinality", "many_to_many"),
            characteristics=_CHARACTERISTICS.get(name, Characteristics()),
        )
    # 给项目负责人关系补互逆定义
    if "is_project_manager_of" in rel:
        rel["is_project_manager_of"].characteristics = Characteristics(
            inverse_of="managed_by")
    rel[_INVERSE_EXTRA.name] = _INVERSE_EXTRA

    published = str(reg.get("schema_version") or "v2.0.0")
    return SemanticSchema(
        schema_version=f"sem-from-{published}",
        ontology_version=published,
        entity_types=list(ent.values()),
        relation_types=list(rel.values()),
        interfaces=[],
        business_rules=list(_DEFAULT_RULES),
        constraints={"max_transitive_depth": 3,
                     "max_hops": (reg.get("constraints") or {}).get("max_hops", 3),
                     "write_gate": "application_enforced"},
    )


def default_semantic_schema() -> SemanticSchema:
    """默认增强 Schema（供启动播种与离线单测）。"""
    return from_registry(None)


# ---------------------------------------------------------------- 纯 dict 投影
def to_schema_dict(sem: SemanticSchema | dict) -> dict:
    """投影为纯标准库 dict，供约束/推理/规则引擎消费（零 Pydantic 依赖）。"""
    if isinstance(sem, dict):
        return sem
    d = sem.model_dump(mode="python")
    return {
        "schema_version": d["schema_version"],
        "ontology_version": d["ontology_version"],
        "source": d.get("source", ""),
        "constraints": d.get("constraints", {}),
        "entity_types": {e["name"]: e for e in d["entity_types"]},
        "relation_types": {r["name"]: r for r in d["relation_types"]},
        "interfaces": {i["name"]: i for i in d.get("interfaces", [])},
        "business_rules": d["business_rules"],
    }


def to_json_schema(sem: SemanticSchema | None = None) -> dict:
    """导出「Schema 定义本身」的 JSON Schema（契约即代码，可外部校验）。"""
    return (sem or default_semantic_schema()).model_json_schema()


def instance_json_schema(schema: dict, type_name: str) -> dict:
    """为某实体类型生成「实例属性」的 JSON Schema：必填 / 类型 / 枚举 / 正则 / 值域。

    写入侧 `write_instance` 可直接据此校验，也可供前端「属性约束提示」渲染。
    """
    types = schema.get("entity_types") or {}
    node = types.get(type_name) or {}
    props: dict[str, dict] = {}
    required: list[str] = []
    _RANGE_MAP = {"string": "string", "integer": "integer", "number": "number",
                  "boolean": "boolean", "enum": "string",
                  "date": "string", "datetime": "string"}
    for attr in node.get("attributes") or []:
        spec: dict[str, Any] = {"type": _RANGE_MAP.get(attr.get("range"), "string")}
        if attr.get("range") in ("date", "datetime"):
            spec["format"] = attr["range"]
        if attr.get("enum_values"):
            spec["enum"] = list(attr["enum_values"])
        if attr.get("pattern"):
            spec["pattern"] = attr["pattern"]
        if attr.get("minimum") is not None:
            spec["minimum"] = attr["minimum"]
        if attr.get("maximum") is not None:
            spec["maximum"] = attr["maximum"]
        if attr.get("description"):
            spec["description"] = attr["description"]
        props[attr["name"]] = spec
        if attr.get("required"):
            required.append(attr["name"])
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"Instance::{type_name}",
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": True,
    }



def validate_business_model(schema: SemanticSchema | dict) -> list[dict[str, str]]:
    """Validate Palantir-style business-object modeling invariants without I/O."""
    data = to_schema_dict(schema)

    def named_map(value):
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {str(item.get("name")): item for item in value if isinstance(item, dict) and item.get("name")}
        return {}

    entities = named_map(data.get("entity_types"))
    relations = named_map(data.get("relation_types"))
    interfaces = named_map(data.get("interfaces"))
    issues: list[dict[str, str]] = []

    def add(code: str, ref: str, message: str) -> None:
        issues.append({"code": code, "ref": ref, "message": message})

    for name, obj in entities.items():
        kind = obj.get("object_kind", "entity")
        if kind not in OBJECT_KINDS:
            add("INVALID_OBJECT_KIND", name, f"对象类型 {name} 的 object_kind 不合法：{kind}")
        attrs = {a.get("name") for a in obj.get("attributes") or []}
        title = obj.get("title_property")
        if title and title not in attrs:
            add("UNKNOWN_TITLE_PROPERTY", name, f"展示属性 {title} 未在对象 {name} 中定义")
        action_created = any((b.get("mode") == "action_created") for b in obj.get("backing_sources") or [])
        if not action_created and not obj.get("identity_rules") and not obj.get("backing_sources"):
            add("OBJECT_WITHOUT_IDENTITY_OR_SOURCE", name,
                f"业务对象 {name} 既没有身份规则，也没有数据源绑定")
        for rule in obj.get("identity_rules") or []:
            if not rule.get("key_properties"):
                add("EMPTY_IDENTITY_RULE", name, "对象身份规则必须指定至少一个业务键属性")
            unknown = set(rule.get("key_properties") or []) - attrs
            if unknown:
                add("UNKNOWN_IDENTITY_PROPERTY", name,
                    f"身份规则引用了未定义属性：{', '.join(sorted(unknown))}")
        for binding in obj.get("backing_sources") or []:
            if binding.get("mode", "materialized") not in SOURCE_MODES:
                add("INVALID_SOURCE_MODE", name, f"数据源绑定模式不合法：{binding.get('mode')}")
            if binding.get("mode", "materialized") != "action_created" and not binding.get("primary_key"):
                add("SOURCE_BINDING_WITHOUT_KEY", name,
                    f"对象数据源 {binding.get('source_id')} 未定义稳定主键")
            unknown_props = set((binding.get("property_mappings") or {}).values()) - attrs
            if unknown_props:
                add("UNKNOWN_BOUND_PROPERTY", name,
                    f"数据源映射了未定义属性：{', '.join(sorted(unknown_props))}")
        for interface in obj.get("interfaces") or []:
            if interface not in interfaces:
                add("UNKNOWN_INTERFACE", name, f"对象引用了未定义接口：{interface}")
        for attr in obj.get("attributes") or []:
            if attr.get("sensitivity", "internal") not in SENSITIVITY_LEVELS:
                add("INVALID_PROPERTY_SENSITIVITY", f"{name}.{attr.get('name')}",
                    f"属性敏感级别不合法：{attr.get('sensitivity')}")

    for name, rel in relations.items():
        sources = rel.get("source_types") or ([rel["source"]] if rel.get("source") else ["*"])
        targets = rel.get("target_types") or ([rel["target"]] if rel.get("target") else ["*"])
        for endpoint in sources + targets:
            if endpoint != "*" and endpoint not in entities:
                add("UNKNOWN_RELATION_ENDPOINT", name, f"关系 {name} 引用了不存在的对象类型：{endpoint}")
        inverse = rel.get("inverse_of") or (rel.get("characteristics") or {}).get("inverse_of", "")
        if inverse:
            inverse_def = relations.get(inverse)
            if not inverse_def:
                add("UNKNOWN_INVERSE_RELATION", name, f"反向关系 {inverse} 不存在")
            elif name not in (inverse_def.get("inverse_of"), (inverse_def.get("characteristics") or {}).get("inverse_of"), ""):
                add("NON_RECIPROCAL_INVERSE", name, f"关系 {name} 与 {inverse} 的反向定义不一致")
    for name, interface in interfaces.items():
        if not name:
            add("EMPTY_INTERFACE_NAME", name, "接口名称不能为空")
        for prop in interface.get("properties") or []:
            if not any(prop in {a.get("name") for a in obj.get("attributes") or []}
                       for obj in entities.values()):
                add("UNIMPLEMENTED_INTERFACE_PROPERTY", name,
                    f"接口属性 {prop} 未被任何业务对象实现")
    return issues
