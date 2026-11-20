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
    """属性定义：类型 / 必填 / 枚举 / 值域 / 正则 / 状态机（可选）。"""
    name: str
    range: str = "string"
    required: bool = False
    enum_values: list[str] = Field(default_factory=list)
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    state_machine: StateMachineDef | None = None
    description: str = ""


class EntityTypeDef(BaseModel):
    """实体类型：`parent` 支持继承（子类实例可被父类查询命中）。"""
    name: str
    label: str = ""
    parent: str = ""
    aliases: list[str] = Field(default_factory=list)
    attributes: list[AttributeDef] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class Characteristics(BaseModel):
    """关系语义特性：轻量推理的三个可配置开关（务实范围，非 OWL 推理机）。"""
    symmetric: bool = False
    transitive: bool = False
    inverse_of: str = ""
    max_transitive_depth: int = 3


class RelationTypeDef(BaseModel):
    """关系类型：source/target 强类型约束 + 特性 + 基数。"""
    name: str
    label: str = ""
    source_types: list[str] = Field(default_factory=lambda: ["*"])
    target_types: list[str] = Field(default_factory=lambda: ["*"])
    cardinality: str = "many_to_many"
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
