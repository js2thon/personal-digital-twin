from app.modules.ontology.mapping_contract import to_binding, validate_binding


def test_automodel_contract_projects_object_properties_and_links():
    contract = {
        "source": "hr",
        "schema_version": "schema-2",
        "mappings": [{
            "table": "employee",
            "class": "Employee",
            "primary_key": ["id"],
            "attributes": {"employeeName": "emp_name"},
            "relations": [{
                "type": "belongsToDepartment",
                "column": "dept_id",
                "target": "Department",
                "target_table": "department",
                "target_id": "hr:department:{value}",
            }],
        }],
        "links": [{
            "table": "employee_department",
            "type": "worksWith",
            "source_table": "employee",
            "source_column": "employee_id",
            "target_table": "department",
            "target_column": "department_id",
        }],
    }

    binding = to_binding(contract, ontology_version="ontology-3")

    assert binding["binding_version"] == 1
    assert binding["object_bindings"][0]["backing_sources"][0]["primary_key"] == ["id"]
    assert binding["object_bindings"][0]["property_maps"][0]["target_property"] == "employeeName"
    assert [x["mapping_type"] for x in binding["link_maps"]] == ["foreign_key", "join_table"]
    assert binding["ontology_version"] == "ontology-3"
    assert validate_binding(binding) == []


def test_legacy_property_mapping_contract_projects_to_binding():
    contract = {
        "source": "hr",
        "mappings": [
            {"source": "emp_name", "entity_type": "Employee", "property": "employeeName"},
            {"source": "emp_email", "entity_type": "Employee", "property": "email"},
        ],
    }

    binding = to_binding(contract)

    assert len(binding["object_bindings"]) == 1
    assert {x["target_property"] for x in binding["object_bindings"][0]["property_maps"]} == {
        "employeeName", "email"
    }


def test_binding_validation_reports_duplicate_source_columns():
    binding = {
        "binding_version": 1,
        "object_bindings": [{
            "object_type": "Employee",
            "property_maps": [
                {"source_table": "employee", "source_column": "name", "target_property": "name"},
                {"source_table": "employee", "source_column": "name", "target_property": "displayName"},
            ],
        }],
        "link_maps": [],
    }

    assert "DUPLICATE_SOURCE_COLUMN" in {
        issue["code"] for issue in validate_binding(binding)
    }
