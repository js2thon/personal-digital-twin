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
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value or ""))
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return value.lower()


def _unique(base: str, used: set[str]) -> str:
    candidate = base
    index = 2
    while candidate in used:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def _property_refs(schema: dict[str, Any], owner_name: str, owner: dict[str, Any], old: str, new: str) -> None:
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
    global_attrs = schema.get("attributes") or {}
    for key, attr in list(global_attrs.items()):
        if key == old and attr.get("owner") == owner_name:
            global_attrs[new] = global_attrs.pop(key)
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
        # Localized business keys are valid and must remain untouched. Only repair
        # an explicitly supplied technical identifier; style-only property and
        # relation naming is reported as informational and never rewritten.
        explicit = str(obj.get("technical_name") or "").strip()
        if explicit and not _PASCAL.fullmatch(explicit):
            new = _pascal_name(explicit)
            obj["technical_name"] = new
            changes.append({"kind": "object_technical_name", "ref": object_name,
                            "before": explicit, "after": new,
                            "reason": "explicit technical identifier must use PascalCase"})

    return {"applied": changes, "count": len(changes)}
