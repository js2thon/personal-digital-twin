"""Regression tests for tenant isolation in the Automodel job cache."""
import pytest

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
        def __init__(self, existing=None):
            self.existing = existing
            self.sql = ""

        def fetch_one(self, sql, params=None):
            return self.existing

        def execute(self, sql, params=None):
            self.sql = sql
            return None

    db = FakeDB()
    monkeypatch.setattr(jobs, "_db", lambda: db)
    monkeypatch.setattr(jobs, "ensure_schema", lambda: True)
    jobs._MEM.clear()

    jobs.save(_job("persisted-id", "source"), tenant="tenant-a")

    assert "WHERE ontology.automodel_jobs.tenant_id=EXCLUDED.tenant_id" in db.sql
    assert ("tenant-a", "persisted-id") in jobs._MEM


def test_persistent_id_collision_does_not_poison_other_tenant_cache(monkeypatch):
    class FakeDB:
        def fetch_one(self, sql, params=None):
            return {"tenant_id": "tenant-a"}

        def execute(self, sql, params=None):
            raise AssertionError("upsert must not run after tenant collision")

    monkeypatch.setattr(jobs, "_db", lambda: FakeDB())
    monkeypatch.setattr(jobs, "ensure_schema", lambda: True)
    jobs._MEM.clear()

    with pytest.raises(PermissionError, match="another tenant"):
        jobs.save(_job("shared-id", "attacker-source"), tenant="tenant-b")

    assert ("tenant-b", "shared-id") not in jobs._MEM
