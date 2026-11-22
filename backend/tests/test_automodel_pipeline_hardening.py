"""Regression tests for tenant isolation, release gate, and review audit."""
import pytest

from app.modules.ontology.automodel import jobs, pipeline, release_gate


def _valid_job():
    return {
        "id": "job-1",
        "draft": {
            "schema_version": "v1",
            "schema": {
                "entity_types": {"Person": {}},
                "relation_types": {},
                "attributes": {},
            },
            "review": [],
            "inferences": [],
            "class_map": {},
        },
        "contract": {"mappings": [{"table": "people", "class": "Person", "id": "people:{id}"}]},
        "issues": [],
        "materialized": {"data_scope": "discovery_sample", "entities": [], "facts": [],
                         "errors": [], "dangling": []},
        "metrics": {"metrics": {}},
    }


def test_in_memory_job_cache_is_tenant_scoped(monkeypatch):
    jobs._MEM.clear()
    jobs._MEM_TENANTS.clear()
    monkeypatch.setattr(jobs, "ensure_schema", lambda: False)
    job = {"id": "same-job", "source": "a", "status": "ready", "stages": {}}
    jobs.save(job, tenant="tenant-a")
    assert jobs.get("same-job", tenant="tenant-a") is job
    assert jobs.get("same-job", tenant="tenant-b") is None
    with pytest.raises(PermissionError, match="不属于当前租户"):
        jobs.save({"id": "same-job", "source": "spoof", "status": "ready", "stages": {}}, tenant="tenant-b")
    assert [x["id"] for x in jobs.listing(tenant="tenant-a")] == ["same-job"]
    assert jobs.listing(tenant="tenant-b") == []


def test_release_gate_blocks_contract_review_and_data_integrity():
    job = _valid_job()
    job["issues"] = [{"level": "error", "code": "BAD_MAPPING"}]
    job["draft"]["review"] = [{"kind": "attribute", "ref": "name"}]
    job["materialized"]["dangling"] = [{"relation": "works_at"}]
    report = release_gate.evaluate(job)
    assert not report["ready"]
    assert {x["code"] for x in report["blockers"]} >= {
        "MAPPING_CONTRACT_ERRORS", "HUMAN_REVIEW_PENDING", "DANGLING_REFERENCES"
    }
    with pytest.raises(ValueError, match="发布门禁未通过"):
        release_gate.assert_publishable(job)


def test_release_gate_can_require_cq_acceptance():
    job = _valid_job()
    assert release_gate.evaluate(job)["ready"]
    assert not release_gate.evaluate(job, require_cq=True)["ready"]
    job["cq_results"] = {"accuracy": 0.99, "failed": 0}
    assert release_gate.evaluate(job, require_cq=True)["ready"]
    job["cq_results"] = {"accuracy": 0.80, "failed": 2}
    assert not release_gate.evaluate(job, require_cq=True)["ready"]
    job["cq_results"] = {"accuracy": 0.90, "items": [{"pass": True}, {"pass": False}]}
    assert not release_gate.evaluate(job, require_cq=True)["ready"]


def test_review_rejects_unknown_actions_and_records_audit():
    job = _valid_job()
    job["draft"]["schema"]["attributes"]["name"] = {"owner": "Person"}
    with pytest.raises(ValueError, match="不支持的审核动作"):
        pipeline.apply_review(job, [{"kind": "attribute", "ref": "name", "action": "ignore"}])
    pipeline.apply_review(job, [{"kind": "attribute", "ref": "name", "action": "accept",
                                 "comment": "业务确认"}], actor="reviewer-1")
    assert job["change_set"][-1]["actor"] == "reviewer-1"
    assert job["change_set"][-1]["target_ref"] == "name"
    assert job["change_set"][-1]["comment"] == "业务确认"
