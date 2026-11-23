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
    monkeypatch.setattr(jobs, "ensure_schema", lambda: False)
    tenant_a = {"id": "same-job", "source": "a", "status": "ready", "stages": {}}
    tenant_b = {"id": "same-job", "source": "b", "status": "ready", "stages": {}}
    jobs.save(tenant_a, tenant="tenant-a")
    jobs.save(tenant_b, tenant="tenant-b")
    assert jobs.get("same-job", tenant="tenant-a") is tenant_a
    assert jobs.get("same-job", tenant="tenant-b") is tenant_b
    assert [x["id"] for x in jobs.listing(tenant="tenant-a")] == ["same-job"]
    assert [x["id"] for x in jobs.listing(tenant="tenant-b")] == ["same-job"]


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


def test_release_gate_requires_explicit_acknowledgement_for_modeling_warnings():
    job = _valid_job()
    job["standards_report"] = {
        "status": "warning",
        "findings": [{"code": "MISSING_DESCRIPTION", "severity": "warning"}],
    }
    report = release_gate.evaluate(job)
    assert report["state"] == "REVIEW_REQUIRED"
    assert not report["ready"]
    assert "STANDARDS_REVIEW_PENDING" in {x["code"] for x in report["blockers"]}
    job["standards_review_acknowledged"] = True
    report = release_gate.evaluate(job)
    assert report["state"] == "READY"
    assert report["ready"]


def test_partial_publish_retry_recovers_existing_version_without_republishing(monkeypatch):
    from app.modules.ontology.automodel import publish

    job = _valid_job()
    job["status"] = "publish_partial_failure"
    job["schema_version"] = "v2.0.1+hr.123456"
    monkeypatch.setattr(release_gate, "assert_publishable", lambda *a, **k: {"ready": True})
    monkeypatch.setattr(publish, "recover_partial_publish", lambda *a, **k: {
        "published": True, "schema_version": "v2.0.1+hr.123456", "recovered": True,
    })
    monkeypatch.setattr(publish, "publish_schema",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not republish")))
    result = pipeline.approve_and_publish(job, "owner", tenant="tenant-a")
    assert result["recovered"] is True
    assert job["status"] == "published"
    assert job["publish_steps"]["completed"] is True



def test_chinese_business_object_name_does_not_block_ontology_publish_gate():
    from app.modules.ontology.semantic.standards import check_ontology_modeling_standards

    schema = {
        "entity_types": {
            "数据管理部人员": {
                "label": "数据管理部人员",
                "object_kind": "entity",
                "attributes": [{"name": "employee_no", "description": "工号",
                                "identifier": True, "range": "string"}],
                "identity_rules": [{"key_properties": ["employee_no"]}],
            }
        },
        "relation_types": {},
        "interfaces": {},
    }
    report = check_ontology_modeling_standards(schema)
    codes = {item["code"] for item in report["findings"]}
    assert "OBJECT_NAME_NOT_PASCAL_CASE" not in codes
    assert "OBJECT_NAME_LOCALIZED" in codes
    assert report["summary"]["errors"] == 0
    assert report["status"] in {"passed", "needs_review"}

    job = _valid_job()
    job["draft"]["schema"] = schema
    job["standards_report"] = report
    job["issues"] = [
        {"level": "info", "code": item["code"], "msg": item["message"]}
        for item in report["findings"] if item["severity"] == "info"
    ]
    gate = release_gate.evaluate(job)
    assert gate["ready"] is True
