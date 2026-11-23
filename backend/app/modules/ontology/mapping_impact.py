"""Dry-run impact analysis for immutable ontology data bindings."""
from __future__ import annotations


def _property_maps(definition: dict) -> dict[tuple, dict]:
    binding = definition.get("ontology_binding") or {}
    result = {}
    for obj in binding.get("object_bindings") or []:
        entity = obj.get("object_type")
        for item in obj.get("property_maps") or []:
            key = (entity, item.get("target_property"))
            result.setdefault(key, set()).add((
                item.get("source_table"), item.get("source_column")))
    return result


def compare_mappings(old: dict, new: dict) -> dict:
    """Compare stable binding semantics; never infer or backfill missing versions."""
    before, after = _property_maps(old), _property_maps(new)
    changed = sorted(
        (entity, prop) for entity, prop in before.keys() | after.keys()
        if before.get((entity, prop)) != after.get((entity, prop))
    )
    old_links = set(map(repr, (old.get("ontology_binding") or {}).get("link_maps") or []))
    new_links = set(map(repr, (new.get("ontology_binding") or {}).get("link_maps") or []))
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
    }


def analyze(*, tenant_id: str, old_mapping: dict, new_mapping: dict) -> dict:
    """Return a read-only estimate from persisted field lineage and ingestion runs."""
    from app.db import session as db

    old_id = old_mapping["id"]
    old_definition = old_mapping.get("definition") or {}
    new_definition = new_mapping.get("definition") or {}
    changes = compare_mappings(old_definition, new_definition)
    changed = changes["changed_properties"]

    ME = __import__("app.modules.ontology.mapping_execution", fromlist=["ensure_schema"])
    ME.ensure_schema()
    params = {"tenant_id": tenant_id, "mapping_id": old_id}
    where = []
    for i, item in enumerate(changed):
        params[f"entity_{i}"] = item["entity_type"]
        params[f"property_{i}"] = item["property"]
        where.append(f"(entity_type=:entity_{i} AND property_name=:property_{i})")
    impacted = db.fetch_one(
        "SELECT count(*) AS lineage_rows, count(DISTINCT instance_id) AS instances "
        "FROM ontology.dataset_field_lineage WHERE tenant_id=:tenant_id "
        "AND mapping_set_id=CAST(:mapping_id AS uuid) "
        + ("AND (" + " OR ".join(where) + ")" if where else "AND false"),
        params,
    ) or {}
    batches = db.fetch_all(
        "SELECT id::text AS batch_id, source_sha256, status, rows_received "
        "FROM ontology.dataset_ingestion_runs "
        "WHERE tenant_id=:tenant_id AND mapping_set_id=CAST(:mapping_id AS uuid) "
        "ORDER BY started_at",
        {"tenant_id": tenant_id, "mapping_id": old_id},
    )
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
            "lineage_rows_for_changed_properties": int(impacted.get("lineage_rows") or 0),
            "distinct_instances_for_changed_properties": int(impacted.get("instances") or 0),
            "batches": batches,
            "scope_note": "仅统计旧映射版本已持久化的字段血缘；无血缘的历史实例标记为未评估，不推测影响数量。",
        },
        "legacy_unversioned": False,
        "next_step": "人工审核影响清单后，另行批准重投影；本接口不会修改实例或 Fact。",
    }
