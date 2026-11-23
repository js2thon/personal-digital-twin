"""Deterministic, auditable auto-fixes for ontology naming conventions.

Only identifier-shape changes are automated. Business labels and source column names
are preserved; every change is recorded so reviewers can inspect or override it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


_PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def _token(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\\1_\\2", str(value or ""))
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return value.lower()


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _property_refs(schema: dict[str, Any], owner: dict[str, Any], old: str, new: str) -> None:
    for attr in owner.get("attributes") or []:
        if attr.get("name") == old:
            attr["name"] = new
    if owner.get("title_property") == old:
        owner["title_property"] = new
    for rule in owner.get("identity_rules") or []:
        rule["key_properties"] = [new if x == old else x for x in rule.get("key_properties", [])]
    for binding in owner.get("backing_sources") or []:
        binding["property_mappings"] = {
            col: new if prop == old else prop
            for col, prop in (binding.get("property_mappings") or {}).items()
        }
    for interface_name in owner.get("interfaces") or []:
        interface = (schema.get("interfaces") or {}).get(interface_name)
        if interface:
            interface["properties"] = [new if x == old else x for x in interface.get("properties", [])]


def _snake_name(name: str, used: set[str], prefix: str) -> str:
    candidate = _token(name)
    if not candidate or not candidate[0].isalpha():
        candidate = f"{prefix}_{candidate}" if candidate else ""
    if not candidate:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        candidate = f"{prefix}_{digest}"
    if not candidate[0].islower():
        candidate = f"{prefix}_{candidate.lower()}"
    return _unique(candidate, used)


def _pascal_name(name: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", name)
    if parts:
        candidate = "".join(p[:1].upper() + p[1:] for p in parts)
        if _PASCAL.fullmatch(candidate):
            return candidate
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8].upper()
    return f"BusinessObject{digest}"


def auto_fix_naming(schema: dict[str, Any]) -> dict[str, Any]:
    """Apply safe deterministic identifier fixes and return an auditable change set."""
    changes: list[dict[str, str]] = []
    objects = schema.get("entity_types") or {}
    for object_name, obj in objects.items():
        if not _PASCAL.fullmatch(str(obj.get("technical_name") or object_name)):
            old = str(obj.get("technical_name") or object_name)
            new = _pascal_name(old)
            obj["technical_name"] = new
            changes.append({"kind": "object_technical_name", "ref": object_name,
                            "before": old, "after": new,
                            "reason": "technical identifier must use PascalCase"})

        attrs = obj.get("attributes") or []
        used = {str(a.get("name")) for a in attrs if a.get("name") and
                _SNAKE.fullmatch(str(a.get("name")))}
        for attr in attrs:
            old = str(attr.get("name") or "")
            if not old or _SNAKE.fullmatch(old):
                continue
            new = _snake_name(old, used, "field")
            used.add(new)
            _property_refs(schema, obj, old, new)
            changes.append({"kind": "property_name", "ref": f"{object_name}.{old}",
                            "before": old, "after": new,
                            "reason": "property identifier must use snake_case"})

    relations = schema.get("relation_types") or {}
    used_relations = {str(k) for k in relations if _SNAKE.fullmatch(str(k))}
    for old in list(relations):
        if _SNAKE.fullmatch(str(old)):
            continue
        new = _snake_name(str(old), used_relations, "related")
        used_relations.add(new)
        relations[new] = relations.pop(old)
        for relation in relations.values():
            if relation.get("inverse_of") == old:
                relation["inverse_of"] = new
            characteristics = relation.get("characteristics") or {}
            if characteristics.get("inverse_of") == old:
                characteristics["inverse_of"] = new
        changes.append({"kind": "relation_name", "ref": str(old),
                        "before": str(old), "after": new,
                        "reason": "relation identifier must use snake_case"})

    return {"applied": changes, "count": len(changes)}
