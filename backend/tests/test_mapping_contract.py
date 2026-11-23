from app.modules.ontology.mapping_contract import execution_mappings, to_binding, validate_binding


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


def test_execution_rows_are_derived_from_canonical_binding():
    contract = {
        "source": "hr",
        "mappings": [
            {"source": "emp_name", "entity_type": "Employee", "property": "employeeName"},
            {"source": "emp_email", "entity_type": "Employee", "property": "email"},
        ],
    }

    assert execution_mappings(contract) == [
        {"entity_type": "Employee", "property": "employeeName", "source": "emp_name"},
        {"entity_type": "Employee", "property": "email", "source": "emp_email"},
    ]


def test_execution_prefers_stored_binding_projection():
    contract = {
        "mappings": [{"source": "old_name", "entity_type": "Employee", "property": "name"}],
        "ontology_binding": {
            "binding_version": 1,
            "object_bindings": [{
                "object_type": "Employee",
                "property_maps": [{
                    "source_column": "preferred_name",
                    "target_property": "displayName",
                }],
            }],
        },
    }

    assert execution_mappings(contract) == [
        {"entity_type": "Employee", "property": "displayName", "source": "preferred_name"}
    ]


def test_automodel_builder_emits_binding_without_changing_legacy_contract():
    from types import SimpleNamespace
    from app.modules.ontology.automodel.mapper import build_contract

    schema = {
        "schema_version": "schema-7",
        "entity_types": {"Person": {"title_property": "fullName"}},
        "relation_types": {},
        "attributes": {
            "fullName": {"owner": "Person", "column": "full_name"},
        },
    }
    draft = {"schema": schema, "class_map": {"people": "Person"}}
    profiles = {
        "people": SimpleNamespace(
            primary_key=["person_id"],
            columns={"person_id": object(), "full_name": object()},
            foreign_keys=[],
        ),
    }

    contract = build_contract("hr", draft, profiles)

    assert contract["mappings"][0]["attributes"] == {"fullName": "full_name"}
    assert contract["ontology_binding"]["schema_version"] == "schema-7"
    obj = contract["ontology_binding"]["object_bindings"][0]
    assert obj["object_type"] == "Person"
    assert obj["backing_sources"][0]["primary_key"] == ["person_id"]
    assert obj["property_maps"] == [{
        "source_column": "full_name",
        "target_property": "fullName",
        "source_table": "people",
        "status": "draft",
    }]


def test_existing_binding_inherits_registry_ontology_version_when_missing():
    binding = {
        "binding_version": 1,
        "object_bindings": [],
        "link_maps": [],
    }
    result = to_binding({"ontology_binding": binding}, ontology_version="ontology-9")
    assert result["ontology_version"] == "ontology-9"
