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
