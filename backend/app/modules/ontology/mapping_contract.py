"""Small compatibility adapter for the repository's mapping contract shapes.

The legacy execution payload stays unchanged. This module exposes one stable
Ontology Data Binding view for registry, UI and future execution migration.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

BINDING_VERSION = 1


def to_binding(contract: dict | None, *, ontology_version: str | None = None) -> dict:
    """Project legacy Automodel or dataset mappings into one binding structure."""
    data = deepcopy(contract or {})
    if data.get("binding_version") == BINDING_VERSION:
        if ontology_version and not data.get("ontology_version"):
            data["ontology_version"] = ontology_version
        return data

    objects: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    def object_binding(object_type: str) -> dict:
        return objects.setdefault(object_type, {
            "object_type": object_type, "backing_sources": [], "property_maps": []
        })

    for item in data.get("mappings") or []:
        if not isinstance(item, dict):
            continue
        table = item.get("table") or item.get("source_table")
        object_type = item.get("class") or item.get("entity_type") or item.get("target_object_type")
        if not object_type:
            continue
        obj = object_binding(str(object_type))
        source = {
            "source_id": data.get("source") or item.get("source_id"),
            "table": str(table) if table else None,
            "primary_key": list(item.get("primary_key") or []),
            "title_property": item.get("title_property"),
        }
        if source not in obj["backing_sources"]:
            obj["backing_sources"].append(source)

        attrs = item.get("attributes") or {}
        if item.get("source") and item.get("property"):
            pairs = [{"source_column": item.get("source"),
                      "target_property": item.get("property")}]
        elif isinstance(attrs, dict):
            pairs = ({"source_column": column, "target_property": prop}
                     for prop, column in attrs.items())
        else:
            pairs = ({"source_column": row.get("source") or row.get("source_column"),
                      "target_property": row.get("property") or row.get("target_property")}
                     for row in attrs if isinstance(row, dict))
        for pair in pairs:
            if pair.get("source_column") and pair.get("target_property"):
                obj["property_maps"].append({
                    **pair, "source_table": str(table) if table else None,
                    "status": "approved" if data.get("status") == "approved" else "draft",
                })

        for relation in item.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            links.append({
                "target_link_type": relation.get("type"),
                "mapping_type": "foreign_key",
                "source": {"table": str(table) if table else None, "column": relation.get("column")},
                "target": {
                    "object_type": relation.get("target"),
                    "table": relation.get("target_table"),
                    "id_template": relation.get("target_id"),
                },
            })

    for link in data.get("links") or []:
        if not isinstance(link, dict):
            continue
        links.append({
            "target_link_type": link.get("type"),
            "mapping_type": "join_table",
            "join_table": link.get("table"),
            "source": {"table": link.get("source_table"), "column": link.get("source_column")},
            "target": {"table": link.get("target_table"), "column": link.get("target_column")},
        })

    return {
        "binding_version": BINDING_VERSION,
        "ontology_version": ontology_version or data.get("ontology_version"),
        "schema_version": data.get("schema_version"),
        "source_id": data.get("source"),
        "object_bindings": list(objects.values()),
        "link_maps": links,
    }


def validate_binding(binding: dict) -> list[dict]:
    """Return structural issues without changing or executing the contract."""
    issues = []
    if binding.get("binding_version") != BINDING_VERSION:
        issues.append({"code": "UNSUPPORTED_BINDING_VERSION"})
    seen = set()
    for obj in binding.get("object_bindings") or []:
        object_type = obj.get("object_type")
        if not object_type:
            issues.append({"code": "OBJECT_TYPE_REQUIRED"})
        for mapping in obj.get("property_maps") or []:
            source_column = mapping.get("source_column")
            source = (mapping.get("source_table") or binding.get("source_id"), source_column)
            target = (object_type, mapping.get("target_property"))
            if not source_column or not target[1]:
                issues.append({"code": "PROPERTY_MAPPING_INCOMPLETE", "object_type": object_type})
            if source_column and source in seen:
                issues.append({"code": "DUPLICATE_SOURCE_COLUMN", "source": source})
            if source_column:
                seen.add(source)
    for link in binding.get("link_maps") or []:
        if not link.get("target_link_type") or not link.get("source") or not link.get("target"):
            issues.append({"code": "LINK_MAPPING_INCOMPLETE"})
        if link.get("mapping_type") not in {"foreign_key", "join_table"}:
            issues.append({"code": "UNSUPPORTED_LINK_MAPPING_TYPE"})
    return issues

def execution_mappings(contract: dict | None) -> list[dict]:
    """Return the legacy deterministic projection rows from the canonical binding."""
    data = contract or {}
    binding = data.get("ontology_binding") or to_binding(data)
    rows = []
    for obj in binding.get("object_bindings") or []:
        for item in obj.get("property_maps") or []:
            rows.append({
                "entity_type": obj.get("object_type"),
                "property": item.get("target_property"),
                "source": item.get("source_column"),
            })
    return rows

