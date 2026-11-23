from app.modules.ontology.mapping_impact import compare_mappings


def _definition(column="employee_name", version="ov-1"):
    return {
        "ontology_binding": {
            "ontology_version": version,
            "schema_version": "schema-1",
            "object_bindings": [{
                "object_type": "Employee",
                "property_maps": [{
                    "source_table": "employee",
                    "source_column": column,
                    "target_property": "name",
                }],
            }],
            "link_maps": [],
        }
    }


def test_compare_mapping_versions_detects_property_remap():
    result = compare_mappings(_definition(), _definition("display_name", "ov-2"))
    assert result["changed_properties"] == [{
        "entity_type": "Employee", "property": "name", "change": "remapped"
    }]
    assert result["ontology_version_changed"] is True
    assert result["schema_version_changed"] is False


def test_compare_mapping_versions_detects_added_and_removed_properties():
    old = {"ontology_binding": {"object_bindings": [{
        "object_type": "Employee", "property_maps": []
    }]}}
    new = {"ontology_binding": {"object_bindings": [{
        "object_type": "Employee", "property_maps": [{
            "source_table": "employee", "source_column": "email",
            "target_property": "email",
        }]
    }]}}
    result = compare_mappings(old, new)
    assert result["changed_properties"] == [{
        "entity_type": "Employee", "property": "email", "change": "added"
    }]


def test_compare_mapping_versions_detects_link_change():
    old = {"ontology_binding": {"object_bindings": [], "link_maps": []}}
    new = {"ontology_binding": {"object_bindings": [], "link_maps": [{
        "target_link_type": "reportsTo", "mapping_type": "foreign_key",
        "source": {"table": "employee", "column": "manager_id"},
        "target": {"object_type": "Employee", "table": "employee"},
    }]}}
    assert compare_mappings(old, new)["changed_links"] == 1
