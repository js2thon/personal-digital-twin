"""Dry-run impact analysis for immutable ontology data bindings."""
from __future__ import annotations

import json


def _property_maps(definition: dict) -> dict[tuple, dict]:
    binding = definition.get("ontology_binding") or {}
    result = {}
    for obj in binding.get("object_bindings") or []:
        entity = obj.get("object_type")
        for item in obj.get("property_maps") or []:
            key = (entity, item.get("target_property"))
            result.setdefault(key, set()).add(json.dumps({
                "source_table": item.get("source_table"),
                "source_column": item.get("source_column"),
                "transform": item.get("transform"),
                "status": item.get("status"),
            }, sort_keys=True, ensure_ascii=False))
    return result


def _schema_diff(old: dict, new: dict) -> dict:
    result = {}
    for section in ("entity_types", "attributes", "relation_types"):
        before, after = old.get(section) or {}, new.get(section) or {}
        keys = before.keys() | after.keys()
        result[section] = [
            {"name": key, "change": "added" if key not in before else
             "removed" if key not in after else "changed"}
            for key in sorted(keys)
            if before.get(key) != after.get(key)
        ]
    return result


def compare_mappings(old: dict, new: dict) -> dict:
    """Compare mapping and ontology schema semantics without mutating either version."""
    before, after = _property_maps(old), _property_maps(new)
    changed = sorted(
        (entity, prop) for entity, prop in before.keys() | after.keys()
        if before.get((entity, prop)) != after.get((entity, prop))
    )
    old_links = {json.dumps(x, sort_keys=True, ensure_ascii=False)
                 for x in (old.get("ontology_binding") or {}).get("link_maps") or []}
    new_links = {json.dumps(x, sort_keys=True, ensure_ascii=False)
                 for x in (new.get("ontology_binding") or {}).get("link_maps") or []}
    return {
        "changed_properties": [
            {"entity_type": entity, "property": prop,
             "change": "added" if (entity, prop) not in before else
                       "removed" if (entity, prop) not in after else "remapped"}
            for entity, prop in changed
        ],
        "changed_links": len(old_links ^ new_links),
        "ontology_version_changed": (
            (old.get("ontology_binding") or {}).get("ontology_version")
            != (new.get("ontology_binding") or {}).get("ontology_version")
        ),
        "schema_version_changed": (
            (old.get("ontology_binding") or {}).get("schema_version")
            != (new.get("ontology_binding") or {}).get("schema_version")
        ),
        "ontology_schema_changes": _schema_diff(
            old.get("ontology_schema") or {}, new.get("ontology_schema") or {}),

    }


def analyze(*, tenant_id: str, old_mapping: dict, new_mapping: dict) -> dict:
    """Return a read-only estimate from persisted field lineage and ingestion runs."""
    from app.db import session as db

    old_id = old_mapping["id"]
    old_definition = dict(old_mapping.get("definition") or {})
    new_definition = dict(new_mapping.get("definition") or {})
    for definition, mapping in ((old_definition, old_mapping), (new_definition, new_mapping)):
        binding = dict(definition.get("ontology_binding") or {})
        binding.setdefault("ontology_version", mapping.get("ontology_version"))
        binding.setdefault("schema_version", mapping.get("schema_version"))
        definition["ontology_binding"] = binding
    changes = compare_mappings(old_definition, new_definition)
    changed = changes["changed_properties"]

    tables = db.fetch_one(
        "SELECT to_regclass('ontology.dataset_field_lineage') AS lineage_table, "
        "to_regclass('ontology.dataset_ingestion_runs') AS runs_table") or {}
    params = {"tenant_id": tenant_id, "mapping_id": old_id}
    entities = sorted({item["entity_type"] for item in changed if item["entity_type"]})
    where = []
    for i, entity in enumerate(entities):
        params[f"entity_{i}"] = entity
        where.append(f"entity_type=:entity_{i}")
    impacted = db.fetch_one(
        "SELECT count(*) AS lineage_rows, count(DISTINCT instance_id) AS instances, "
        "count(*) FILTER (WHERE ontology_version IS NULL) AS unversioned_rows "
        "FROM ontology.dataset_field_lineage WHERE tenant_id=:tenant_id "
        "AND mapping_set_id=CAST(:mapping_id AS uuid) "
        + ("AND (" + " OR ".join(where) + ")" if where else "AND false"),
        params,
    ) or {} if tables.get("lineage_table") else {}
    batches = db.fetch_all(
        "SELECT id::text AS batch_id, source_sha256, status, rows_received "
        "FROM ontology.dataset_ingestion_runs "
        "WHERE tenant_id=:tenant_id AND mapping_set_id=CAST(:mapping_id AS uuid) "
        "ORDER BY started_at",
        {"tenant_id": tenant_id, "mapping_id": old_id},
    ) if tables.get("runs_table") else []
    return {
        "dry_run": True,
        "execution_performed": False,
        "old_mapping": {k: old_mapping.get(k) for k in
                        ("id", "name", "version", "ontology_version", "schema_version", "status")},
        "new_mapping": {k: new_mapping.get(k) for k in
                        ("id", "name", "version", "ontology_version", "schema_version", "status")},
        "changes": changes,
        "impact_estimate": {
            "changed_property_count": len(changed),
            "changed_link_count": changes["changed_links"],
            "lineage_rows_for_affected_entities": int(impacted.get("lineage_rows") or 0),
            "unversioned_lineage_rows": int(impacted.get("unversioned_rows") or 0),
            "distinct_instances_for_changed_properties": int(impacted.get("instances") or 0),
            "batches": batches,
            "scope_note": "仅统计旧映射版本已持久化的字段血缘；无血缘的历史实例标记为未评估，不推测影响数量。",
            "lineage_available": bool(tables.get("lineage_table")),
            "batch_history_available": bool(tables.get("runs_table")),
        },
        "legacy_unversioned": not bool(old_mapping.get("ontology_version")),
        "next_step": "人工审核影响清单后，另行批准重投影；本接口不会修改实例或 Fact。",
    }
