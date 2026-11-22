"""Regression tests for the deterministic ontology mapping pipeline."""
from app.modules.ontology.mapping_pipeline import suggest_mappings, validate_mappings


SCHEMA = {
    "entity_types": {
        "Employee": {
            "name": "Employee",
            "label": "员工",
            "attributes": [
                {"name": "full_name", "description": "姓名", "range": "string", "required": True},
                {"name": "employee_no", "description": "工号", "range": "string"},
                {"name": "age", "description": "年龄", "range": "integer"},
            ],
        }
    }
}


def test_suggest_exact_alias_match_with_evidence():
    report = suggest_mappings(
        [{"name": "full_name", "samples": ["张三", "李四"]}],
        SCHEMA,
    )
    item = report["mappings"][0]
    assert item["candidates"][0]["property"] == "full_name"
    assert item["candidates"][0]["score"] >= 0.85
    assert item["candidates"][0]["evidence"]
    assert report["execution"] == "not_executed"


def test_suggest_unmapped_column():
    report = suggest_mappings([{"name": "unrelated_field"}], SCHEMA)
    assert report["mappings"][0]["status"] == "unmapped"


def test_validate_approved_mapping():
    report = validate_mappings(
        [{"source": "姓名", "entity_type": "Employee", "property": "full_name"}],
        SCHEMA,
    )
    assert report["valid"] is True
    assert report["approved_mapping_count"] == 1
    assert report["execution"] == "not_executed"


def test_validate_rejects_unknown_and_duplicate_targets():
    report = validate_mappings(
        [
            {"source": "name_a", "entity_type": "Employee", "property": "full_name"},
            {"source": "name_b", "entity_type": "Employee", "property": "full_name"},
            {"source": "bad", "entity_type": "Employee", "property": "missing"},
        ],
        SCHEMA,
    )
    codes = {issue["code"] for issue in report["issues"]}
    assert report["valid"] is False
    assert "duplicate_target" in codes
    assert "unknown_target" in codes


def test_semantic_schema_dict_shape_is_supported():
    from app.modules.ontology.semantic import schema_def as SD
    schema = SD.to_schema_dict(SD.default_semantic_schema())
    report = suggest_mappings([{"name": "full_name"}], schema)
    assert report["mapping_count"] == 1
