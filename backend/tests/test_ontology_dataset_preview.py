import base64

from app.modules.ontology.mapping_pipeline import preview_rows, read_tabular_file


SCHEMA = {
    "entity_types": {
        "Employee": {
            "name": "Employee",
            "label": "员工",
            "attributes": [
                {"name": "full_name", "range": "string", "required": True},
                {"name": "age", "range": "integer", "required": False},
            ],
        }
    }
}

MAPPINGS = [
    {"source": "姓名", "entity_type": "Employee", "property": "full_name"},
    {"source": "年龄", "entity_type": "Employee", "property": "age"},
]


def test_preview_rows_coerces_types_and_emits_lineage():
    report = preview_rows(
        [{"姓名": "张三", "年龄": "32"}], MAPPINGS, SCHEMA, source_name="staff.csv"
    )
    item = report["preview"][0]
    assert report["valid"] is True
    assert item["properties"] == {"full_name": "张三", "age": 32}
    assert item["lineage"]["age"]["column"] == "年龄"
    assert item["lineage"]["age"]["source"] == "staff.csv"
    assert report["execution"] == "not_executed"


def test_preview_rows_reports_required_and_conversion_errors():
    report = preview_rows(
        [{"姓名": "", "年龄": "not-a-number"}], MAPPINGS, SCHEMA
    )
    assert report["valid"] is False
    assert report["invalid_instances"] == 1
    assert report["row_errors"]


def test_preview_rejects_invalid_mapping_without_transforming():
    report = preview_rows(
        [{"姓名": "张三"}],
        [{"source": "姓名", "entity_type": "Employee", "property": "missing"}],
        SCHEMA,
    )
    assert report["valid"] is False
    assert report["stage"] == "mapping_validation"
    assert report["preview"] == []


def test_csv_reader_supports_gb18030():
    raw = "姓名,年龄\n张三,32\n".encode("gb18030")
    dataset = read_tabular_file("员工.csv", raw)
    assert dataset["columns"][0]["name"] == "姓名"
    assert dataset["rows"][0]["姓名"] == "张三"


def test_csv_reader_rejects_duplicate_headers():
    raw = "姓名,姓名\n张三,李四\n".encode("utf-8")
    try:
        read_tabular_file("duplicate.csv", raw)
    except ValueError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("duplicate headers should be rejected")
