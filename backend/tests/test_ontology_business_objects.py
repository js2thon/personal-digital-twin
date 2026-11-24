from app.modules.ontology.modelling.business_objects import analyze_business_objects


def _spec():
    return {
        "nodes": [
            {"id": "hospital", "kind": "entity", "fields": {
                "name": "Hospital", "label": "医院", "primary_key": "hospital_id",
                "business_role": "master", "business_definition": "医疗机构主数据",
            }},
            {"id": "dept", "kind": "entity", "fields": {
                "name": "Department", "primary_key": "department_id",
                "business_role": "master", "business_definition": "医院内部组织单元",
            }},
            {"id": "ops", "kind": "entity", "fields": {
                "name": "DepartmentDailyOps", "primary_key": "ops_id",
                "business_role": "event", "event_time": "business_date",
            }},
            {"id": "month", "kind": "entity", "fields": {
                "name": "DepartmentMonthlySummary", "primary_key": "summary_id",
                "business_role": "derived", "business_grain": "department_id + month",
            }},
            {"id": "r1", "kind": "relation", "fields": {
                "name": "containsDepartment", "source_entity": "Hospital",
                "target_entity": "Department", "cardinality": "1:N",
                "semantic_kind": "contains",
            }},
            {"id": "r2", "kind": "relation", "fields": {
                "name": "departmentHasDailyOps", "source_entity": "Department",
                "target_entity": "DepartmentDailyOps", "cardinality": "1:N",
                "semantic_kind": "belongs_to",
            }},
        ],
        "edges": [],
    }


def test_business_object_projection_preserves_roles_and_links():
    result = analyze_business_objects(_spec())
    assert result["valid"] is True
    assert result["summary"] == {"object_count": 4, "relation_count": 2, "errors": 0, "warnings": 0}
    roles = {x["name"]: x["role"] for x in result["objects"]}
    assert roles["Hospital"] == "master"
    assert roles["DepartmentDailyOps"] == "event"
    assert roles["DepartmentMonthlySummary"] == "derived"
    assert result["relations"][0]["semantic_kind"] == "contains"


def test_missing_identity_and_relation_semantics_are_reported():
    spec = _spec()
    spec["nodes"][0]["fields"].pop("primary_key")
    spec["nodes"][4]["fields"].pop("semantic_kind")
    result = analyze_business_objects(spec)
    codes = {x["code"] for x in result["issues"]}
    assert "BO_PRIMARY_KEY_MISSING" in codes
    assert "BO_RELATION_KIND_MISSING" in codes
    assert result["valid"] is False


def test_role_name_hint_is_advisory_not_auto_classification():
    spec = _spec()
    spec["nodes"][0]["fields"].pop("business_role")
    result = analyze_business_objects(spec)
    hospital = next(x for x in result["objects"] if x["name"] == "Hospital")
    assert hospital["role"] == "unspecified"
    assert hospital["role_suggestion"] == "master"
