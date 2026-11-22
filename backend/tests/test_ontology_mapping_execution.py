"""Safety-contract tests for controlled ontology dataset ingestion."""
from pathlib import Path


def test_execution_requires_approved_mapping_and_atomic_transaction():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert 'mapping_set.get("status") != "approved"' in source
    assert "with db.transaction():" in source
    assert "ON CONFLICT (tenant_id, mapping_set_id, source_key, source_row, entity_type)" in source
    assert "MAX_EXECUTION_ROWS = 500" in source


def test_execution_persists_field_level_lineage_and_version_binding():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert "ontology.dataset_field_lineage" in source
    assert "source_column" in source
    assert "mapping_version" in source
    assert "schema_version" in source
    assert "ontology_version" in source


def test_api_exposes_execution_run_and_lineage_routes():
    source = Path("app/api/v1/ontology_modelling.py").read_text(encoding="utf-8")
    assert '"/ontology/modelling/dataset/execute"' in source
    assert '"/ontology/modelling/ingestion-runs/{run_id}"' in source
    assert '"/ontology/modelling/instances/{instance_id}/lineage"' in source



def test_upsert_requires_unique_stable_source_ids():
    from app.modules.ontology.mapping_execution import _validated_source_ids

    assert _validated_source_ids(
        [{"external_id": "A-1"}, {"external_id": "A-2"}], "external_id"
    ) == ["A-1", "A-2"]


def test_upsert_rejects_missing_or_duplicate_source_ids():
    import pytest
    from app.modules.ontology.mapping_execution import _validated_source_ids

    with pytest.raises(ValueError, match="缺少稳定源记录标识"):
        _validated_source_ids([{"external_id": "A-1"}, {}], "external_id")
    with pytest.raises(ValueError, match="重复"):
        _validated_source_ids(
            [{"external_id": "A-1"}, {"external_id": "A-1"}], "external_id"
        )


def test_execution_supports_stable_source_record_upsert():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert "ontology.dataset_source_records" in source
    assert "mapping_checksum" in source
    assert 'sync_mode == "upsert"' in source
    assert 'instance_payload["id"] = source_state["instance_id"]' in source
    assert "Missing rows are never deleted implicitly" in source



def test_full_snapshot_requires_upsert_mode():
    from app.modules.ontology.mapping_execution import execute_approved_dataset
    import inspect

    params = inspect.signature(execute_approved_dataset).parameters
    assert params["full_snapshot"].default is False
    assert params["sync_mode"].default == "append"


def test_full_snapshot_uses_tombstones_not_hard_deletes():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert "tombstoned_at=now()" in source
    assert "last_seen_run_id IS DISTINCT FROM" in source
    assert "it never deletes" in source or "never deletes" in source
    assert "records_tombstoned" in source


def test_full_snapshot_marks_seen_before_idempotency_reservation():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    mark = source.index("Mark the business key as present")
    reservation = source.index("# A file-level reservation")
    assert mark < reservation
    assert "last_seen_run_id=CAST(:run AS uuid)" in source


def test_source_record_identity_is_namespaced_by_mapping_and_source():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert "source_namespace VARCHAR(64)" in source
    assert "hashlib.sha256(" in source
    assert 'f"{mapping_set_id}:{source_name}"' in source
    assert "UNIQUE (tenant_id, source_namespace, mapping_name, source_record_id, entity_type)" in source
    assert "WHERE tenant_id=:t AND source_namespace=:ns AND mapping_name=:mn AND entity_type=:et" in source
