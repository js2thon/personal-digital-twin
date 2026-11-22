"""Regression tests for traceable, fail-closed ontology QA evidence."""
from app.modules.ontology.automodel import qa


def _schema():
    return {
        "entity_types": {"Employee": {"label": "员工"}, "Department": {"label": "部门"}},
        "attributes": {
            "hire_date": {"owner": "Employee", "label": "入职日期", "role": "temporal", "range": "date"},
            "dept_name": {"owner": "Department", "label": "部门名称", "role": "name", "range": "string"},
        },
    }


def _metrics():
    return {"metrics": {}}


def test_time_filter_rejects_non_temporal_attribute():
    plan = {"op": "list", "cls": "Employee", "time_filter": {
        "attr": "dept_name", "op": ">=", "value": "2024-01-01",
    }}
    errors = qa.validate_plan(plan, _schema(), _metrics())
    assert any("不是时间属性" in error for error in errors)


def test_time_filter_rejects_wrong_owner():
    plan = {"op": "list", "cls": "Department", "time_filter": {
        "attr": "hire_date", "op": ">=", "value": "2024-01-01",
    }}
    errors = qa.validate_plan(plan, _schema(), _metrics())
    assert any("不属于对象类型" in error for error in errors)


def test_between_requires_ordered_date_values():
    plan = {"op": "list", "cls": "Employee", "time_filter": {
        "attr": "hire_date", "op": "between", "value": ["2025-01-01", "2024-01-01"],
    }}
    errors = qa.validate_plan(plan, _schema(), _metrics())
    assert any("开始日期必须早于结束日期" in error for error in errors)


def test_source_evidence_exposes_mapping_and_discovery_scope():
    contract = {
        "source": "hr_upload",
        "mappings": [{
            "class": "Employee", "table": "employees", "primary_key": ["employee_no"],
            "attributes": {"full_name": "姓名", "hire_date": "入职日期"},
            "relations": [{"type": "works_in", "column": "dept_id", "target_table": "departments"}],
        }],
    }
    result = {"rows": [{"_id": "hr_upload:employees:1001"}]}
    evidence = qa._source_evidence(contract, _schema(), {"op": "list", "cls": "Employee"}, result)
    assert evidence["data_scope"] == "discovery_sample"
    assert evidence["traceability_level"] == "source_table_and_column"
    assert evidence["source_mappings"][0]["table"] == "employees"
    assert evidence["source_mappings"][0]["property_columns"]["hire_date"] == "入职日期"
    assert evidence["matched_instances"] == [{"instance_id": "hr_upload:employees:1001"}]
    assert "不代表源库全量统计" in evidence["scope_note"]
