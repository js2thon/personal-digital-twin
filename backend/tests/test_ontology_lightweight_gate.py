from app.modules.ontology.automodel.release_gate import evaluate


def _publishable_job():
    return {
        "draft": {"schema": {"entity_types": {"Employee": {}},
                             "relation_types": {}}, "review": []},
        "contract": {"mappings": [{"source": "employees", "target": "Employee"}]},
        "materialized": {"data_scope": {"rows": 2}, "errors": [], "dangling": []},
    }


def test_advisory_standards_warnings_do_not_block_publish():
    job = _publishable_job()
    job["issues"] = [{"level": "warning", "code": "OBJECT_DESCRIPTION_MISSING",
                      "msg": "对象缺少描述"}]
    job["standards_report"] = {
        "findings": [{"severity": "warning", "code": "OBJECT_DESCRIPTION_MISSING",
                      "message": "对象缺少描述"}]
    }

    report = evaluate(job)

    assert report["ready"] is True
    assert report["blockers"] == []
    assert any(item["code"] == "PIPELINE_WARNING" for item in report["warnings"])
    assert any(item["code"] == "OBJECT_DESCRIPTION_MISSING" for item in report["warnings"])


def test_mapping_errors_still_block_publish():
    job = _publishable_job()
    job["issues"] = [{"level": "error", "code": "MAPPING_TARGET_UNKNOWN",
                      "msg": "映射目标不存在"}]

    report = evaluate(job)

    assert report["ready"] is False
    assert any(item["code"] == "MAPPING_CONTRACT_ERRORS" for item in report["blockers"])


def test_dangling_relationships_still_block_publish():
    job = _publishable_job()
    job["materialized"]["dangling"] = [{"row": 2, "target": "missing"}]

    report = evaluate(job)

    assert report["ready"] is False
    assert any(item["code"] == "DANGLING_REFERENCES" for item in report["blockers"])
