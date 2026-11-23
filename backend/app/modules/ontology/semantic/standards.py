"""Ontology modeling standards checks.

The checker is deterministic and evidence-based. It separates blocking errors from
review warnings so legacy schemas can be adopted incrementally without silently
publishing structurally invalid business models.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .schema_def import to_schema_dict

_OBJECT_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_MEMBER_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
_KINDS = {"entity", "event", "transaction", "reference", "association"}
_SENSITIVITY = {"public", "internal", "confidential", "restricted"}


def _named(value: Any) -> dict[str, dict]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if isinstance(v, dict)}
    if isinstance(value, list):
        return {str(v["name"]): v for v in value
                if isinstance(v, dict) and v.get("name")}
    return {}


def check_ontology_modeling_standards(schema: Any) -> dict[str, Any]:
    """Return deterministic standard checks with error/warning/info severities."""
    data = to_schema_dict(schema)
    objects = _named(data.get("entity_types"))
    relations = _named(data.get("relation_types"))
    interfaces = _named(data.get("interfaces"))
    findings: list[dict[str, str]] = []

    def add(severity: str, code: str, ref: str, message: str, recommendation: str) -> None:
        findings.append({
            "severity": severity, "code": code, "ref": ref,
            "message": message, "recommendation": recommendation,
        })

    if not objects:
        add("error", "MODEL_NO_OBJECT_TYPES", "entity_types",
            "本体没有定义任何业务对象类型", "至少定义一个有明确业务含义的对象类型")

    # Object naming, identity, display, lifecycle and property semantics.
    for name, obj in objects.items():
        if not _OBJECT_NAME.fullmatch(name):
            add("error", "OBJECT_NAME_NOT_PASCAL_CASE", name,
                f"对象类型名称 {name} 不符合 PascalCase 规范", "使用 Employee、PurchaseOrder 等 PascalCase 名称")
        if not str(obj.get("label") or "").strip():
            add("warning", "OBJECT_LABEL_MISSING", name,
                f"对象 {name} 缺少中文业务名称", "补充面向业务人员的 label")
        if not str(obj.get("description") or "").strip():
            add("warning", "OBJECT_DESCRIPTION_MISSING", name,
                f"对象 {name} 缺少业务定义", "说明对象代表什么、边界是什么，以及不包含什么")
        kind = obj.get("object_kind", "entity")
        if kind not in _KINDS:
            add("error", "OBJECT_KIND_INVALID", name,
                f"对象 {name} 的类别 {kind} 不受支持", "使用 entity/event/transaction/reference/association")
        attrs = obj.get("attributes") or []
        attr_names = [str(a.get("name") or "") for a in attrs]
        duplicates = sorted(k for k, n in Counter(attr_names).items() if k and n > 1)
        for prop in duplicates:
            add("error", "PROPERTY_DUPLICATED", f"{name}.{prop}",
                f"对象 {name} 存在重复属性 {prop}", "合并重复定义并保留唯一属性语义")
        attr_map = {a.get("name"): a for a in attrs if a.get("name")}
        if not attrs:
            add("warning", "OBJECT_WITHOUT_PROPERTIES", name,
                f"对象 {name} 尚未定义属性", "识别业务标识、核心状态、业务时间和展示属性")
        title = obj.get("title_property")
        if not title:
            add("warning", "OBJECT_TITLE_MISSING", name,
                f"对象 {name} 没有展示属性", "指定适合对象列表和详情页展示的 title_property")
        elif title not in attr_map:
            add("error", "OBJECT_TITLE_UNKNOWN", name,
                f"对象 {name} 的展示属性 {title} 未定义", "将 title_property 指向对象已定义的属性")
        identifiers = [a["name"] for a in attrs if a.get("identifier")]
        identity_rules = obj.get("identity_rules") or []
        if not identity_rules and not any(b.get("mode") == "action_created"
                                          for b in obj.get("backing_sources") or []):
            add("error", "OBJECT_IDENTITY_MISSING", name,
                f"对象 {name} 缺少业务身份规则", "定义稳定业务键；无法预先分配身份的对象需声明 action_created")
        if not identifiers and identity_rules:
            add("warning", "IDENTIFIER_PROPERTY_NOT_MARKED", name,
                f"对象 {name} 有身份规则，但没有标记 identifier 属性", "将参与身份解析的属性标记 identifier=true")
        for attr in attrs:
            prop = str(attr.get("name") or "")
            ref = f"{name}.{prop}"
            if not _MEMBER_NAME.fullmatch(prop):
                add("error", "PROPERTY_NAME_NOT_SNAKE_CASE", ref,
                    f"属性名称 {prop} 不符合 snake_case 规范", "使用 employee_no、created_at 等 snake_case 名称")
            if not str(attr.get("description") or "").strip():
                add("warning", "PROPERTY_DESCRIPTION_MISSING", ref,
                    f"属性 {ref} 缺少业务定义", "说明业务含义、口径、单位及取值边界")
            if attr.get("range", "string") not in {"string", "integer", "number", "boolean", "date", "datetime", "enum"}:
                add("error", "PROPERTY_RANGE_INVALID", ref,
                    f"属性 {ref} 的数据类型不受支持", "使用受支持的标准值类型")
            if attr.get("range") == "enum" and not attr.get("enum_values"):
                add("error", "ENUM_VALUES_MISSING", ref,
                    f"枚举属性 {ref} 没有枚举值", "定义受控枚举值，避免自由文本造成语义分裂")
            if attr.get("range") in {"date", "datetime"} and not attr.get("temporal_role"):
                add("warning", "TEMPORAL_ROLE_MISSING", ref,
                    f"时间属性 {ref} 未说明时间角色", "标注 event_time、valid_from、valid_to 或 created_at 等语义")
            if attr.get("sensitivity", "internal") not in _SENSITIVITY:
                add("error", "PROPERTY_SENSITIVITY_INVALID", ref,
                    f"属性 {ref} 的敏感等级无效", "使用 public/internal/confidential/restricted")
            if attr.get("identifier") and attr.get("sensitivity") == "restricted":
                add("warning", "SENSITIVE_IDENTIFIER", ref,
                    f"身份属性 {ref} 被标记为 restricted", "确认是否需要脱敏、哈希或受控身份解析")

        # Source mapping must target declared properties and have stable source keys.
        for index, binding in enumerate(obj.get("backing_sources") or []):
            bref = f"{name}.backing_sources[{index}]"
            if not binding.get("source_id") or not (binding.get("table") or binding.get("dataset")):
                add("error", "SOURCE_BINDING_INCOMPLETE", bref,
                    f"对象 {name} 的数据源绑定缺少 source_id 或数据集/表", "补充来源系统标识及数据集或表名")
            if binding.get("mode", "materialized") != "action_created" and not binding.get("primary_key"):
                add("error", "SOURCE_PRIMARY_KEY_MISSING", bref,
                    f"对象 {name} 的数据源绑定缺少稳定源主键", "声明源表主键或稳定复合键")
            for source_field, target_prop in (binding.get("property_mappings") or {}).items():
                if target_prop not in attr_map:
                    add("error", "SOURCE_TARGET_PROPERTY_UNKNOWN", bref,
                        f"源字段 {source_field} 映射到未定义属性 {target_prop}", "先定义目标业务属性，再建立字段映射")
            if not (binding.get("property_mappings") or {}):
                add("warning", "SOURCE_MAPPING_EMPTY", bref,
                    f"对象 {name} 的数据源绑定没有字段映射", "至少映射业务标识及核心业务属性")

        parent = obj.get("parent")
        if parent and parent not in objects:
            add("error", "OBJECT_PARENT_UNKNOWN", name,
                f"对象 {name} 的父类型 {parent} 不存在", "定义父类型或修正 parent 引用")

    # Inheritance cycle detection.
    for name in objects:
        seen: set[str] = set()
        current = name
        while current and current in objects:
            if current in seen:
                add("error", "OBJECT_INHERITANCE_CYCLE", name,
                    f"对象 {name} 的继承链存在循环", "调整 parent 关系，确保继承是有向无环结构")
                break
            seen.add(current)
            current = str(objects[current].get("parent") or "")

    # Relationship semantics, typed endpoints, inverse and temporal validity.
    for name, rel in relations.items():
        if not _MEMBER_NAME.fullmatch(name):
            add("error", "RELATION_NAME_NOT_SNAKE_CASE", name,
                f"关系名称 {name} 不符合 snake_case 规范", "使用 reports_to、belongs_to 等动词或业务短语")
        if not str(rel.get("label") or "").strip():
            add("warning", "RELATION_LABEL_MISSING", name,
                f"关系 {name} 缺少中文业务名称", "补充自然语言关系名称")
        if not str(rel.get("description") or "").strip():
            add("warning", "RELATION_DESCRIPTION_MISSING", name,
                f"关系 {name} 缺少业务定义", "说明关系含义、方向、业务约束和边界")
        cardinality = rel.get("cardinality", "many_to_many")
        if cardinality not in _CARDINALITIES:
            add("error", "RELATION_CARDINALITY_INVALID", name,
                f"关系 {name} 的基数 {cardinality} 不合法", "明确 one_to_one/one_to_many/many_to_one/many_to_many")
        sources = rel.get("source_types") or ([rel["source"]] if rel.get("source") else ["*"])
        targets = rel.get("target_types") or ([rel["target"]] if rel.get("target") else ["*"])
        if "*" in sources or "*" in targets:
            add("warning", "RELATION_ENDPOINT_UNTYPED", name,
                f"关系 {name} 使用通配端点，缺少明确业务对象类型", "将关系两端约束到明确的对象类型")
        for endpoint in list(sources) + list(targets):
            if endpoint != "*" and endpoint not in objects:
                add("error", "RELATION_ENDPOINT_UNKNOWN", name,
                    f"关系 {name} 引用了不存在的对象类型 {endpoint}", "创建对象类型或修正关系端点")
        inverse = rel.get("inverse_of") or (rel.get("characteristics") or {}).get("inverse_of", "")
        if inverse and inverse not in relations:
            add("error", "RELATION_INVERSE_UNKNOWN", name,
                f"关系 {name} 的反向关系 {inverse} 不存在", "定义反向关系或移除错误引用")
        if rel.get("temporal"):
            valid_from, valid_to = rel.get("valid_from_property"), rel.get("valid_to_property")
            if not valid_from or not valid_to:
                add("warning", "TEMPORAL_RELATION_BOUNDS_MISSING", name,
                    f"时态关系 {name} 未同时定义有效起止属性", "定义 valid_from_property 和 valid_to_property")
        if cardinality == "many_to_many" and not rel.get("backing_sources"):
            add("warning", "MANY_TO_MANY_WITHOUT_ASSOCIATION_SOURCE", name,
                f"多对多关系 {name} 没有关系承载数据源", "评估是否需要关联对象/关联表承载关系属性和生命周期")

    # Each interface's property contract must be implemented by every object
    # that explicitly claims the interface (not merely by any object in schema).
    for obj_name, obj in objects.items():
        attrs = {a.get("name") for a in obj.get("attributes") or []}
        for interface_name in obj.get("interfaces") or []:
            interface = interfaces.get(interface_name)
            if not interface:
                add("error", "INTERFACE_UNKNOWN", obj_name,
                    f"对象 {obj_name} 引用了不存在的接口 {interface_name}", "先定义接口或修正对象的 interfaces")
                continue
            missing = sorted(set(interface.get("properties") or []) - attrs)
            if missing:
                add("error", "INTERFACE_PROPERTY_NOT_IMPLEMENTED", obj_name,
                    f"对象 {obj_name} 未实现接口 {interface_name} 的属性：{', '.join(missing)}",
                    "补齐接口属性，或调整对象的接口声明")

    counts = Counter(item["severity"] for item in findings)
    return {
        "status": "fail" if counts["error"] else "needs_review" if counts["warning"] else "passed",
        "summary": {"total": len(findings), "errors": counts["error"],
                    "warnings": counts["warning"], "info": counts["info"]},
        "findings": findings,
        "policy": {"errors_block_publish": True, "warnings_require_review": True,
                   "info_blocks_publish": False},
    }
