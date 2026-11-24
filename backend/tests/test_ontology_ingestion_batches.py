"""Unit tests for deterministic bounded ingestion helpers."""
import pytest

from app.modules.ontology.ingestion_batches import _chunks, _digest


def test_chunks_preserve_order_and_boundaries():
    rows = [{"id": str(i)} for i in range(7)]
    batches = list(_chunks(rows, 3))
    assert [(i, len(batch)) for i, batch in batches] == [(0, 3), (1, 3), (2, 1)]
    assert [row["id"] for _, batch in batches for row in batch] == [str(i) for i in range(7)]


def test_batch_digest_is_deterministic_and_content_sensitive():
    a = _digest({"index": 0, "rows": [{"id": "1"}]})
    assert a == _digest({"index": 0, "rows": [{"id": "1"}]})
    assert a != _digest({"index": 1, "rows": [{"id": "1"}]})
    assert a != _digest({"index": 0, "rows": [{"id": "2"}]})


def test_chunks_reject_non_object_rows():
    with pytest.raises(ValueError, match="对象/字典"):
        list(_chunks([{"id": 1}, "bad"], 1))



def test_database_source_namespace_isolated_by_profile_schema_and_table():
    from app.modules.ontology.ingestion_batches import _database_source_namespace

    crm = _database_source_namespace("crm", "public", "customers")
    hr = _database_source_namespace("hr", "public", "customers")
    audit = _database_source_namespace("crm", "audit", "customers")
    assert crm == "db:crm:public.customers"
    assert len({crm, hr, audit}) == 3


def test_snapshot_checkpoints_and_reconciliation_are_namespace_scoped(monkeypatch):
    from app.modules.ontology import ingestion_batches as IB

    namespace = "db:crm:public.customers"
    statements = []
    mapping_set = {"status": "approved", "name": "customer-map", "checksum": "sum-1"}
    monkeypatch.setattr(IB, "ensure_schema", lambda: None)
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: mapping_set)

    def fetch_one(sql, params=None):
        statements.append(("fetch", sql, params or {}))
        if "clock_timestamp" in sql:
            from datetime import datetime, timezone
            return {"started_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        if "WITH stale AS" in sql:
            return {"n": 0}
        if "FROM ontology.dataset_ingestion_snapshots" in sql:
            return None
        if "SELECT batch_sha256" in sql:
            return None
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(IB.db, "fetch_one", fetch_one)
    monkeypatch.setattr(IB.db, "execute",
                        lambda sql, params=None: statements.append(("execute", sql, params or {})))
    executed = []
    def execute_batch(**kwargs):
        executed.append(kwargs)
        return {"run_id": "run-1", "status": "succeeded"}
    monkeypatch.setattr(IB, "execute_approved_dataset", execute_batch)

    result = IB.execute_approved_dataset_stream(
        tenant_id="tenant-a", mapping_set_id="mapping-1",
        rows=[{"id": "C-1"}], source_name="crm customers",
        snapshot_id="snapshot-1", actor="tester",
        sync_mode="upsert", source_id_column="id", batch_size=10,
        reconcile_missing=True, source_namespace=namespace,
    )
    assert result["source_namespace"] == namespace
    assert executed[0]["source_namespace"] == namespace
    relevant = [entry for entry in statements if "source_namespace" in entry[1]]
    assert relevant
    assert all(entry[2].get("ns") == namespace for entry in relevant)


def test_empty_snapshot_cannot_reconcile_all_source_records(monkeypatch):
    import pytest
    from app.modules.ontology import ingestion_batches as IB
    monkeypatch.setattr(IB, "ensure_schema", lambda: None)
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: {
        "status": "approved", "name": "customer-map", "checksum": "sum-1"
    })
    def fetch_one(sql, params=None):
        if "clock_timestamp" in sql:
            from datetime import datetime, timezone
            return {"started_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        return None
    monkeypatch.setattr(IB.db, "fetch_one", fetch_one)
    with pytest.raises(ValueError, match="空快照"):
        IB.execute_approved_dataset_stream(
            tenant_id="tenant-a", mapping_set_id="mapping-1",
            rows=[], source_name="crm customers", snapshot_id="empty-snapshot",
            actor="tester", sync_mode="upsert", source_id_column="id",
            reconcile_missing=True, source_namespace="db:crm:public.customers",
        )


def test_database_snapshot_passes_namespaced_source_to_batch_pipeline(monkeypatch):
    from app.modules.ontology import database_connector
    from app.modules.ontology import ingestion_batches as IB
    mapping_set = {
        "status": "approved", "checksum": "sum-1",
        "definition": {"mappings": [{"source": "name", "entity_type": "customer", "property": "name"}]},
    }
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: mapping_set)
    monkeypatch.setattr(database_connector, "stream_table_rows",
                        lambda **kwargs: iter([{"id": "C-1", "name": "Alice"}]))
    captured = {}
    def execute_stream(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}
    monkeypatch.setattr(IB, "execute_approved_dataset_stream", execute_stream)
    result = IB.execute_approved_database_snapshot(
        tenant_id="tenant-a", mapping_set_id="mapping-1",
        profile_name="crm", schema_name="public", table_name="customers",
        primary_key="id", snapshot_id="snapshot-1", actor="tester",
    )
    assert result["status"] == "succeeded"
    assert captured["source_namespace"] == "db:crm:public.customers"
    assert captured["source_name"] == "db:crm:public.customers"
    assert captured["snapshot_id"] == "snapshot-1"



def test_completed_snapshot_replay_rejects_truncated_batch_tail(monkeypatch):
    from app.modules.ontology import ingestion_batches as IB

    rows = [{"id": "C-1"}]
    source_sha = "source-sha"
    batch_sha = IB._digest({"base": source_sha, "index": 0, "rows": rows})
    statements = []
    monkeypatch.setattr(IB, "ensure_schema", lambda: None)
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: {
        "status": "approved", "name": "customer-map", "checksum": "sum-1"
    })

    def fetch_one(sql, params=None):
        if "clock_timestamp" in sql:
            from datetime import datetime, timezone
            return {"started_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        if "FROM ontology.dataset_ingestion_snapshots" in sql:
            return {"source_sha256": source_sha, "batch_count": 2, "status": "succeeded"}
        if "SELECT batch_sha256" in sql:
            return {"batch_sha256": batch_sha, "status": "succeeded", "run_id": "run-1"}
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(IB.db, "fetch_one", fetch_one)
    monkeypatch.setattr(IB.db, "execute",
                        lambda sql, params=None: statements.append((sql, params or {})))
    monkeypatch.setattr(IB, "execute_approved_dataset",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("completed batch must be skipped")))

    with pytest.raises(ValueError, match="批次数量发生变化"):
        IB.execute_approved_dataset_stream(
            tenant_id="tenant-a", mapping_set_id="mapping-1", rows=rows,
            source_name="crm customers", snapshot_id="snapshot-1", source_sha256=source_sha,
            actor="tester", sync_mode="upsert", source_id_column="id", batch_size=1,
            reconcile_missing=True, source_namespace="db:crm:public.customers",
        )

    assert not any("WITH stale AS" in sql for sql, _ in statements)


def test_completed_snapshot_replay_rejects_added_batch_before_ingesting_it(monkeypatch):
    from app.modules.ontology import ingestion_batches as IB

    rows = [{"id": "C-1"}, {"id": "C-2"}]
    source_sha = "source-sha"
    first_batch_sha = IB._digest({"base": source_sha, "index": 0, "rows": [rows[0]]})
    checkpoint_reads = []
    batch_executes = []
    monkeypatch.setattr(IB, "ensure_schema", lambda: None)
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: {
        "status": "approved", "name": "customer-map", "checksum": "sum-1"
    })

    def fetch_one(sql, params=None):
        if "FROM ontology.dataset_ingestion_snapshots" in sql:
            return {"source_sha256": source_sha, "batch_count": 1, "status": "succeeded"}
        if "SELECT batch_sha256" in sql:
            checkpoint_reads.append(params["i"])
            return {"batch_sha256": first_batch_sha, "status": "succeeded", "run_id": "run-1"}
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(IB.db, "fetch_one", fetch_one)
    monkeypatch.setattr(IB.db, "execute", lambda *a, **k: None)
    monkeypatch.setattr(IB, "execute_approved_dataset",
                        lambda **kwargs: batch_executes.append(kwargs))

    with pytest.raises(ValueError, match="批次数量发生变化"):
        IB.execute_approved_dataset_stream(
            tenant_id="tenant-a", mapping_set_id="mapping-1", rows=rows,
            source_name="crm customers", snapshot_id="snapshot-1", source_sha256=source_sha,
            actor="tester", sync_mode="upsert", source_id_column="id", batch_size=1,
            source_namespace="db:crm:public.customers",
        )

    assert checkpoint_reads == [0]
    assert batch_executes == []


def test_completed_snapshot_replay_rejects_changed_source_fingerprint(monkeypatch):
    from app.modules.ontology import ingestion_batches as IB

    monkeypatch.setattr(IB, "ensure_schema", lambda: None)
    monkeypatch.setattr(IB.mapping_registry, "get", lambda *a, **k: {
        "status": "approved", "name": "customer-map", "checksum": "sum-1"
    })
    monkeypatch.setattr(IB.db, "fetch_one", lambda *a, **k: {
        "source_sha256": "old-source-sha", "batch_count": 1, "status": "succeeded"
    })
    with pytest.raises(ValueError, match="源快照指纹发生变化"):
        IB.execute_approved_dataset_stream(
            tenant_id="tenant-a", mapping_set_id="mapping-1", rows=[{"id": "C-1"}],
            source_name="crm customers", snapshot_id="snapshot-1", source_sha256="new-source-sha",
            actor="tester", sync_mode="upsert", source_id_column="id",
            source_namespace="db:crm:public.customers",
        )
