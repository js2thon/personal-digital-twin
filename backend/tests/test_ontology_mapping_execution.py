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
    source = Path("app/api/v1/ontology_mapping.py").read_text(encoding="utf-8")
    assert '"/ontology/mapping/dataset/execute"' in source
    assert '"/ontology/mapping/ingestion-runs/{run_id}"' in source
    assert '"/ontology/mapping/instances/{instance_id}/lineage"' in source



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



def test_upsert_source_state_is_scoped_by_source_namespace(monkeypatch):
    from contextlib import nullcontext
    from app.modules.ontology import mapping_execution as ME

    mapping_set = {
        "status": "approved", "name": "employee-map", "version": 1,
        "checksum": "mapping-checksum", "ontology_version": "o1", "schema_version": "s1",
        "definition": {
            "mappings": [{"source": "full_name", "entity_type": "employee", "property": "name"}],
            "ontology_schema": {
                "entity_types": {
                    "employee": {"name": "employee", "attributes": [{"name": "name", "range": "string"}]}
                }
            },
        },
    }
    monkeypatch.setattr(ME, "ensure_schema", lambda: None)
    monkeypatch.setattr(ME.mapping_registry, "get", lambda *a, **k: mapping_set)
    monkeypatch.setattr(ME, "_create_run", lambda **k: {"id": "run-1"})
    monkeypatch.setattr(ME.db, "transaction", lambda: nullcontext())
    monkeypatch.setattr(ME.db, "execute", lambda *a, **k: None)

    lookups = []
    def fetch_one(sql, params=None):
        lookups.append((sql, params or {}))
        if "INSERT INTO ontology.dataset_source_records" in sql:
            return {"id": "source-state-1"}
        if "INSERT INTO ontology.dataset_ingestion_records" in sql:
            return {"id": "ingestion-record-1", "instance_id": None}
        if "FROM ontology.dataset_source_records" in sql:
            return None
        raise AssertionError(f"unexpected query: {sql}")

    class FakeService:
        def __init__(self, **kwargs):
            self.ov = "o1"

        def write_instance(self, payload):
            return {"id": "instance-1"}

    monkeypatch.setattr(ME.db, "fetch_one", fetch_one)
    monkeypatch.setattr(ME, "OntologyService", FakeService)
    result = ME.execute_approved_dataset(
        tenant_id="tenant-a", mapping_set_id="mapping-1",
        rows=[{"id": "C-1", "full_name": "张三"}],
        source_name="crm customers", source_sha256="batch-sha", actor="tester",
        sync_mode="upsert", source_id_column="id",
        source_namespace="db:crm:public.customers",
    )
    source_queries = [(sql, params) for sql, params in lookups
                      if "dataset_source_records" in sql]
    assert len(source_queries) == 2
    assert all("source_namespace" in sql for sql, _ in source_queries)
    assert all(params.get("ns") == "db:crm:public.customers" for _, params in source_queries)
    insert_sql = next(sql for sql, _ in source_queries if "INSERT INTO" in sql)
    assert "ON CONFLICT (tenant_id, mapping_name, source_namespace, source_record_id, entity_type)" in insert_sql
    assert result["source_namespace"] == "db:crm:public.customers"


def test_source_namespace_rejects_empty_or_oversized_values():
    import pytest
    from app.modules.ontology.mapping_execution import _normalize_source_namespace

    with pytest.raises(ValueError, match="不能为空"):
        _normalize_source_namespace("   ")
    with pytest.raises(ValueError, match="500"):
        _normalize_source_namespace("x" * 501)


def test_execution_contract_keeps_source_table_and_projects_relation_links():
    from app.modules.ontology.mapping_contract import execution_mappings, to_binding

    contract = {
        "source": "hr",
        "schema_version": "s1",
        "mappings": [{
            "table": "employees", "class": "Employee",
            "primary_key": ["employee_id"],
            "attributes": {"name": "employee_name"},
            "relations": [{
                "type": "works_for", "column": "department_id",
                "target": "Department", "target_table": "departments",
                "target_id": "hr:departments:{value}", "by": "external_id",
            }],
        }],
        "links": [{
            "table": "employee_projects", "primary_key": ["id"],
            "type": "assigned_to", "source_table": "employees",
            "source_column": "employee_id", "target_table": "projects",
            "target_column": "project_id",
        }],
    }
    binding = to_binding(contract, ontology_version="o1")
    rows = execution_mappings({"ontology_binding": binding})

    assert rows == [{
        "entity_type": "Employee", "property": "name",
        "source": "employee_name", "source_table": "employees",
    }]
    assert any(item["mapping_type"] == "join_table" for item in binding["link_maps"])


def test_relation_execution_requires_explicit_source_table_for_multi_table_contract():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert 'raise ValueError("映射契约包含多张源表，请明确指定 source_table")' in source
    assert "service._write_relation_in_transaction" in source
    assert "ontology.instance_relations" in source
    assert "relations_inserted" in source


def test_relation_api_accepts_explicit_source_table():
    source = Path("app/api/v1/ontology_mapping.py").read_text(encoding="utf-8")
    assert "source_table: str | None = None" in source
    assert "source_table=payload.source_table" in source


def test_relation_ingestion_is_table_scoped_and_keeps_source_snapshot_state():
    source = Path("app/modules/ontology/mapping_execution.py").read_text(encoding="utf-8")
    assert 'f"{namespace}:{selected_table}"' in source
    assert 'f"{namespace}:{table_name}"' in source
    assert "source_active=TRUE" in source
    assert "ADD COLUMN IF NOT EXISTS source_active BOOLEAN NOT NULL DEFAULT TRUE" in source
    assert 'f"{source_sha256}:{mapping_set_id}:{selected_table or \'default\'}"' in source


def test_streaming_execution_passes_source_table_to_each_batch():
    source = Path("app/modules/ontology/ingestion_batches.py").read_text(encoding="utf-8")
    assert "source_table: str | None = None" in source
    assert "source_namespace=namespace, source_table=source_table" in source
    assert 'snapshot_key = f"{snapshot_id}:{source_table}" if source_table else snapshot_id' in source


def test_database_snapshot_uses_table_specific_contract_columns():
    source = Path("app/modules/ontology/ingestion_batches.py").read_text(encoding="utf-8")
    assert 'if item.get("table") == table_name' in source
    assert 'source_table=table_name' in source
    assert 'source_namespace = f"db:{profile_name}:{schema_name or \'default\'}"' in source


def test_automodel_junction_mapping_preserves_primary_key():
    source = Path("app/modules/ontology/automodel/mapper.py").read_text(encoding="utf-8")
    assert '"primary_key": list(p.primary_key or [])' in source
