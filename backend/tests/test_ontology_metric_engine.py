from app.modules.ontology.runtime import metric_engine as ME


def _defs():
    return [
        {"metric_id": "employee_count", "name": "员工数", "kind": "native",
         "subject": "Employee", "aggregation": "count", "unit": "人"},
        {"metric_id": "active_count", "name": "在职人数", "kind": "native",
         "subject": "ActiveEmployee", "aggregation": "count", "unit": "人"},
        {"metric_id": "active_rate", "name": "在职率", "kind": "derived",
         "formula": "active_count / employee_count * 100",
         "dependencies": ["active_count", "employee_count"], "unit": "%"},
        {"metric_id": "weighted_score", "name": "组织指数", "kind": "composite",
         "formula": "(active_rate * 0.7) + (active_rate * 0.3)",
         "dependencies": ["active_rate"], "unit": "%"},
    ]


def test_metric_dag_order_and_preview():
    report = ME.validate(_defs())
    assert report["valid"] is True, report["issues"]
    assert report["topological_order"].index("employee_count") < report["topological_order"].index("active_rate")
    data = {"entities": {"Employee": [{"id": "1"}, {"id": "2"}],
                         "ActiveEmployee": [{"id": "1"}]}}
    result = ME.preview(_defs(), data)
    assert result["ok"] is True
    values = {x["metric_id"]: x["value"] for x in result["items"]}
    assert values["employee_count"] == 2
    assert values["active_rate"] == 50
    assert values["weighted_score"] == 50


def test_empty_native_count_is_null_not_zero():
    result = ME.preview([_defs()[0]], {"entities": {}})
    assert result["items"][0]["value"] is None
    assert result["items"][0]["ok"] is False


def test_formula_blocks_calls_and_attribute_access():
    bad = [{"metric_id": "x", "name": "恶意", "kind": "derived",
            "formula": "__import__('os').system('whoami')"}]
    report = ME.validate(bad)
    assert report["valid"] is False
    assert any(x["code"] == "FORMULA_INVALID" for x in report["issues"])


def test_dependency_cycle_is_blocked():
    defs = [
        {"metric_id": "a", "name": "A", "kind": "derived", "formula": "b + 1"},
        {"metric_id": "b", "name": "B", "kind": "composite", "formula": "a + 1"},
    ]
    report = ME.validate(defs)
    assert report["valid"] is False
    assert any(x["code"] == "DEPENDENCY_CYCLE" for x in report["issues"])


def test_unit_mismatch_for_addition_is_blocked():
    defs = [
        {"metric_id": "people", "name": "人数", "kind": "native", "subject": "Employee",
         "aggregation": "count", "unit": "人"},
        {"metric_id": "cost", "name": "成本", "kind": "native", "subject": "Order",
         "aggregation": "sum", "field": "amount", "unit": "元"},
        {"metric_id": "bad", "name": "错误复合", "kind": "composite", "formula": "people + cost"},
    ]
    report = ME.validate(defs)
    assert any(x["code"] == "UNIT_MISMATCH" for x in report["issues"])


def test_discovery_only_suggests_native_count_and_numeric_sum():
    spec = {"nodes": [
        {"id": "o", "kind": "entity", "fields": {"name": "Employee", "primary_key": "id",
          "attributes": "age:integer\nsalary:number\nname:string"}},
    ], "edges": []}
    result = ME.discover(spec)
    assert any(x["metric_id"] == "count_Employee" for x in result["items"])
    assert any(x["aggregation"] == "sum" and x["field"] == "salary" for x in result["items"])
    assert not any(x.get("field") == "name" and x["aggregation"] == "sum" for x in result["items"])
