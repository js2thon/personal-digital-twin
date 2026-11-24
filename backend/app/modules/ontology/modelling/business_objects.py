"""Business-object-first ontology modelling helpers.

The ontology remains authoritative; this module adds business semantics to existing
entity/relation nodes without introducing a parallel schema or writing production data.
"""
from __future__ import annotations

import re
from typing import Any

ROLES = {"master", "event", "derived", "reference"}
RELATION_KINDS = {"belongs_to", "contains", "uses", "provides", "associates", "derived_from"}
ROLE_HINTS = {
    "master": ("info", "information", "master", "主数据", "基础信息", "档案"),
    "event": ("daily", "usage", "transaction", "record", "event", "明细", "使用", "运营", "流水"),
    "derived": ("summary", "aggregate", "monthly", "daily_summary", "汇总", "统计", "分析"),
    "reference": ("type", "category", "dictionary", "catalog", "字典", "分类", "目录"),
}


def _fields(node: dict[str, Any]) -> dict[str, Any]:
    return dict(node.get("fields") or {})


def _name(node: dict[str, Any]) -> str:
    f = _fields(node)
    return str(f.get("name") or f.get("label") or node.get("id") or "").strip()


def _is_object(node: dict[str, Any]) -> bool:
    if node.get("kind") != "entity":
        return False
    f = _fields(node)
    return not ("range" in f and not str(f.get("primary_key") or "").strip())


def _role_hint(name: str) -> str | None:
    value = name.lower()
    for role, words in ROLE_HINTS.items():
        if any(word in value for word in words):
            return role
    return None


def analyze_business_objects(spec: dict[str, Any]) -> dict[str, Any]:
    """Project existing workflow nodes into a business-object graph and quality report."""
    nodes = [n for n in spec.get("nodes") or [] if isinstance(n, dict)]
    objects = [n for n in nodes if _is_object(n)]
    by_name = {_name(n): n for n in objects if _name(n)}
    by_id = {str(n.get("id")): n for n in objects if n.get("id")}
    items: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []

    for node in objects:
        f = _fields(node)
        name = _name(node)
        role = str(f.get("business_role") or f.get("object_role") or "").strip().lower()
        if role not in ROLES:
            role = "unspecified"
        hint = _role_hint(name)
        pk = str(f.get("primary_key") or "").strip()
        definition = str(f.get("business_definition") or f.get("definition") or "").strip()
        item = {
            "id": str(node.get("id") or name),
            "name": name,
            "label": str(f.get("label") or name),
            "role": role,
            "role_suggestion": hint,
            "primary_key": pk,
            "identity_strategy": str(f.get("identity_strategy") or ("single" if pk else "unresolved")),
            "business_definition": definition,
            "business_grain": str(f.get("business_grain") or ""),
            "event_time": str(f.get("event_time") or ""),
            "lifecycle": str(f.get("lifecycle") or ""),
            "source_system": str(f.get("source_system") or f.get("source_object") or ""),
        }
        items.append(item)
        if not name:
            issues.append({"level": "error", "code": "BO_NAME_MISSING", "object": name, "message": "业务对象缺少名称"})
        if not pk:
            issues.append({"level": "error", "code": "BO_PRIMARY_KEY_MISSING", "object": name, "message": f"对象「{name}」未定义业务唯一标识"})
        if not definition:
            issues.append({"level": "warning", "code": "BO_DEFINITION_MISSING", "object": name, "message": f"对象「{name}」缺少业务定义"})
        if role == "unspecified":
            issues.append({"level": "warning", "code": "BO_ROLE_UNSPECIFIED", "object": name, "message": f"对象「{name}」未确认业务角色；建议值：{hint or '需业务人员判断'}"})
        if role == "event" and not item["event_time"]:
            issues.append({"level": "warning", "code": "BO_EVENT_TIME_MISSING", "object": name, "message": f"事件对象「{name}」未定义业务发生时间"})
        if role == "derived" and not item["business_grain"]:
            issues.append({"level": "warning", "code": "BO_GRAIN_MISSING", "object": name, "message": f"派生对象「{name}」未定义统计粒度"})

    links: list[dict[str, Any]] = []
    for rel in (n for n in nodes if n.get("kind") == "relation"):
        f = _fields(rel)
        name = str(f.get("name") or "").strip()
        source = str(f.get("source_entity") or "").strip()
        target = str(f.get("target_entity") or "").strip()
        kind = str(f.get("semantic_kind") or f.get("relation_kind") or "").strip().lower()
        cardinality = str(f.get("cardinality") or "").strip()
        source_node = by_id.get(source) or by_name.get(source)
        target_node = by_id.get(target) or by_name.get(target)
        link = {
            "id": str(rel.get("id") or name), "name": name, "source": source,
            "target": target, "semantic_kind": kind or "unspecified",
            "cardinality": cardinality, "inverse_name": str(f.get("inverse_name") or ""),
        }
        links.append(link)
        if not source_node or not target_node:
            issues.append({"level": "error", "code": "BO_RELATION_ENDPOINT", "object": name, "message": f"关系「{name}」必须连接两个已定义业务对象"})
        if kind and kind not in RELATION_KINDS:
            issues.append({"level": "error", "code": "BO_RELATION_KIND_INVALID", "object": name, "message": f"关系「{name}」语义类型不支持：{kind}"})
        elif not kind:
            issues.append({"level": "warning", "code": "BO_RELATION_KIND_MISSING", "object": name, "message": f"关系「{name}」未声明业务语义"})
        if not cardinality:
            issues.append({"level": "error", "code": "BO_RELATION_CARDINALITY_MISSING", "object": name, "message": f"关系「{name}」未声明基数"})
        if kind == "derived_from":
            derived = next((x for x in items if x["name"] == source), None)
            if derived and derived["role"] != "derived":
                issues.append({"level": "warning", "code": "BO_DERIVATION_ROLE_MISMATCH", "object": name, "message": f"derived_from 源对象「{source}」通常应为派生对象，请确认方向"})

    errors = sum(x["level"] == "error" for x in issues)
    warnings = sum(x["level"] == "warning" for x in issues)
    return {
        "model_type": "business_object",
        "objects": items,
        "relations": links,
        "summary": {"object_count": len(items), "relation_count": len(links), "errors": errors, "warnings": warnings},
        "issues": issues,
        "valid": errors == 0,
        "policy": {
            "ontology_authority": "published ontology",
            "mapping_authority": "MappingContract",
            "role_suggestions_are_advisory": True,
            "production_mutation": "requires human approval and a new version",
        },
    }
