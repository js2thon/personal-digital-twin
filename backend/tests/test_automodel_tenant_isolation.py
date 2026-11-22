"""Regression tests for tenant isolation in the Automodel job cache."""
from app.modules.ontology.automodel import jobs


def _job(job_id: str, source: str, status: str = "ready_to_publish") -> dict:
    return {"id": job_id, "source": source, "source_kind": "file", "status": status, "stages": {}}


def test_memory_cache_lookup_is_tenant_scoped(monkeypatch):
    monkeypatch.setattr(jobs, "ensure_schema", lambda: False)
    jobs._MEM.clear()
    jobs.save(_job("same-id", "tenant-a-source"), tenant="tenant-a")
    jobs.save(_job("same-id", "tenant-b-source"), tenant="tenant-b")

    assert jobs.get("same-id", "tenant-a")["source"] == "tenant-a-source"
    assert jobs.get("same-id", "tenant-b")["source"] == "tenant-b-source"
    assert jobs.get("same-id", "tenant-c") is None


def test_memory_fallback_listing_is_tenant_scoped(monkeypatch):
    monkeypatch.setattr(jobs, "ensure_schema", lambda: False)
    jobs._MEM.clear()
    jobs.save(_job("a-1", "a-source"), tenant="tenant-a")
    jobs.save(_job("b-1", "b-source"), tenant="tenant-b")
    jobs.save(_job("a-2", "a-source-2"), tenant="tenant-a")

    assert {row["id"] for row in jobs.listing("tenant-a")} == {"a-1", "a-2"}
    assert {row["id"] for row in jobs.listing("tenant-b")} == {"b-1"}
    assert jobs.listing("tenant-c") == []


def test_persistent_upsert_cannot_reassign_job_to_another_tenant(monkeypatch):
    class FakeDB:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, params=None):
            self.sql = sql
            return None

    db = FakeDB()
    monkeypatch.setattr(jobs, "_db", lambda: db)
    monkeypatch.setattr(jobs, "ensure_schema", lambda: True)
    jobs._MEM.clear()

    jobs.save(_job("persisted-id", "source"), tenant="tenant-a")

    assert "WHERE ontology.automodel_jobs.tenant_id=EXCLUDED.tenant_id" in db.sql
    assert "ON CONFLICT (id) DO UPDATE" in db.sql
