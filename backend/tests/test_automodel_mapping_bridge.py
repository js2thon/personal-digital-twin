from app.api.v1.automodel import _automodel_mapping_snapshot


def test_automodel_mapping_snapshot_freezes_schema_and_canonical_binding():
    job = {
        "source": "upload-1",
        "schema_version": "v2.1.0",
        "draft": {
            "schema": {
                "schema_version": "auto-upload-1-job",
                "entity_types": {
                    "Person": {
                        "label": "人员",
                        "attributes": [{"name": "full_name", "range": "string"}],
                    }
                },
                "relation_types": {},
                "attributes": {
                    "full_name": {"owner": "Person", "range": "string"}
                },
            }
        },
        "contract": {
            "contract_version": "1",
            "source": "upload-1",
            "schema_version": "auto-upload-1-job",
            "mappings": [{
                "table": "people",
                "class": "Person",
                "primary_key": ["person_id"],
                "attributes": {"full_name": "name"},
                "relations": [],
            }],
            "links": [],
        },
    }

    definition, schema_version, ontology_version = _automodel_mapping_snapshot(
        job, "job-123"
    )

    assert schema_version == "auto-upload-1-job"
    assert ontology_version == "v2.1.0"
    assert definition["origin"] == {
        "kind": "automodel_job", "job_id": "job-123"
    }
    binding = definition["ontology_binding"]
    assert binding["binding_version"] == 1
    assert binding["object_bindings"][0]["object_type"] == "Person"
    assert binding["object_bindings"][0]["property_maps"][0] == {
        "source_column": "name",
        "target_property": "full_name",
        "source_table": "people",
        "status": "draft",
    }
