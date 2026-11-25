"""Canonical business-object ontology model shared by Studio V2 and Automodel.

The canonical model is a stable, JSON-serializable contract. UI-specific graph
payloads and execution-specific Automodel schemas are projections of this model.
"""
from __future__ import annotations

from typing import Any

OBJECT_KINDS = {"static_entity", "transaction", "derived"}
CARDINALITY_MAP = {
    "1:1": "one_to_one",
    "1:N": "one_to_many",
    "N:1": "many_to_one",
    "N:M": "many_to_many",
    "one_to_one": "one_to_one",
    "one_to_many": "one_to_many",
    "many_to_one": "many_to_one",
    "many_to_many": "many_to_many",
}


def _object_kind(info: dict[str, Any]) -> str:
    raw = str(info.get("object_kind") or info.get("category") or "static_entity").strip().lower()
    aliases = {
        "static": "static_entity", "entity": "static_entity",
        "event": "transaction", "transactional": "transaction",
        "aggregate": "derived", "derived_aggregate": "derived",
    }
    value = aliases.get(raw, raw)
    return value if value in OBJECT_KINDS else "static_entity"


def canonical_model_from_studio(ontology: Any, *, session_id: str = "",
                                 version: str = "draft") -> dict[str, Any]:
    """Normalize the current Studio Ontology into the canonical business model."""
    objects: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for name, info in (getattr(ontology, "object_types", {}) or {}).items():
        source = (getattr(ontology, "data_mapping", {}) or {}).get(name) or {}
        properties = []
        seen: set[str] = set()
        for prop in info.get("properties") or []:
            prop_name = str(prop)
            if prop_name in seen:
                continue
            seen.add(prop_name)
            definition = (info.get("property_definitions") or {}).get(prop_name) or {}
            binding = (info.get("property_mappings") or {}).get(prop_name) or {}
            properties.append({
                "name": prop_name,
                "label": definition.get("label") or prop_name,
                "data_type": str(definition.get("type") or "string").lower(),
                "description": definition.get("description") or "",
                "required": bool(definition.get("required", prop_name == info.get("primary_key"))),
                "identifier": prop_name == info.get("primary_key"),
                "data_binding": {
                    "source_table": binding.get("source_table") or source.get("source_table") or name,
                    "source_column": binding.get("source_column") or "",
                },
            })
        objects.append({
            "id": str(name),
            "name": str(name),
            "label": info.get("label") or str(name),
            "description": info.get("description") or "",
            "object_kind": _object_kind(info),
            "primary_key": str(info.get("primary_key") or ""),
            "source": {
                "source_kind": source.get("source_kind") or "unknown",
                "source_table": source.get("source_table") or name,
                "dataset_id": source.get("dataset_id"),
                "columns": list(source.get("columns") or []),
                "row_count": int(source.get("row_count") or 0),
            },
            "properties": properties,
            "actions": list(info.get("actions") or []),
            "metrics": list(info.get("metrics") or []),
        })

    object_names = {item["name"] for item in objects}
    for name, info in (getattr(ontology, "link_types", {}) or {}).items():
        source_type, target_type = info.get("source"), info.get("target")
        source_info = (getattr(ontology, "object_types", {}) or {}).get(source_type, {})
        target_info = (getattr(ontology, "object_types", {}) or {}).get(target_type, {})
        source_prop = str(info.get("source_property") or "")
        target_prop = str(info.get("target_property") or target_info.get("primary_key") or "")
        source_binding = (source_info.get("property_mappings") or {}).get(source_prop) or {}
        target_binding = (target_info.get("property_mappings") or {}).get(target_prop) or {}
        raw_cardinality = info.get("cardinality") or info.get("relation_type") or "N:1"
        cardinality = CARDINALITY_MAP.get(str(raw_cardinality), "many_to_one")
        relations.append({
            "id": str(name),
            "name": str(name),
            "label": info.get("label") or info.get("description") or str(name),
            "description": info.get("description") or "",
            "source_object": source_type,
            "target_object": target_type,
            "cardinality": cardinality,
            "directed": bool(info.get("directed", True)),
            "data_binding": {
                "source_table": source_binding.get("source_table") or source_type,
                "source_column": source_binding.get("source_column") or source_prop,
                "target_table": target_binding.get("source_table") or target_type,
                "target_column": target_binding.get("source_column") or target_prop,
                "junction_table": info.get("junction_table") or "",
                "junction_source_column": info.get("junction_source_column") or "",
                "junction_target_column": info.get("junction_target_column") or "",
            },
        })

    errors: list[dict[str, str]] = []
    for item in objects:
        if not item["primary_key"]:
            errors.append({"code": "OBJECT_PRIMARY_KEY_MISSING", "target": item["name"],
                           "message": f"业务对象 {item['name']} 未定义主键"})
        prop_names = {prop["name"] for prop in item["properties"]}
        if item["primary_key"] and item["primary_key"] not in prop_names:
            errors.append({"code": "OBJECT_PRIMARY_KEY_NOT_PROPERTY", "target": item["name"],
                           "message": f"主键 {item['primary_key']} 未定义为对象属性"})
    for relation in relations:
        if relation["source_object"] not in object_names or relation["target_object"] not in object_names:
            errors.append({"code": "RELATION_ENDPOINT_MISSING", "target": relation["name"],
                           "message": f"关系 {relation['name']} 的起点或终点对象不存在"})
        if relation["cardinality"] == "many_to_many":
            binding = relation["data_binding"]
            if not (binding["junction_table"] and binding["junction_source_column"]
                    and binding["junction_target_column"]):
                errors.append({"code": "JUNCTION_BINDING_MISSING", "target": relation["name"],
                               "message": f"多对多关系 {relation['name']} 缺少关联表及两端字段绑定"})

    return {
        "model_type": "canonical_business_object_model",
        "model_version": "1.0",
        "session_id": session_id,
        "draft_version": version,
        "objects": objects,
        "relationships": relations,
        "validation": {"valid": not errors, "errors": errors},
    }
