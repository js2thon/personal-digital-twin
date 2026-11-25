from types import SimpleNamespace

from app.modules.ontology.canonical_model import canonical_model_from_studio


def test_canonical_model_preserves_object_properties_and_source_bindings():
    ontology = SimpleNamespace(
        object_types={
            "Hospital": {
                "primary_key": "hospital_id",
                "label": "医院",
                "properties": ["hospital_id", "name"],
                "property_definitions": {"name": {"type": "string", "description": "医院名称"}},
                "property_mappings": {"hospital_id": {"source_column": "id"},
                                      "name": {"source_column": "hospital_name"}},
            }
        },
        data_mapping={"Hospital": {"source_table": "hospital", "columns": ["id", "hospital_name"]}},
        link_types={},
    )
    model = canonical_model_from_studio(ontology, session_id="s1")
    obj = model["objects"][0]
    assert obj["object_kind"] == "static_entity"
    assert obj["primary_key"] == "hospital_id"
    assert obj["properties"][1]["data_binding"]["source_column"] == "hospital_name"
    assert model["validation"]["valid"] is True


def test_canonical_model_preserves_relationship_cardinality_and_checks_junction_binding():
    ontology = SimpleNamespace(
        object_types={
            "Department": {"primary_key": "id", "properties": ["id"], "property_mappings": {}},
            "Material": {"primary_key": "id", "properties": ["id"], "property_mappings": {}},
        },
        data_mapping={},
        link_types={
            "uses_material": {"source": "Department", "target": "Material",
                              "cardinality": "N:M", "source_property": "id",
                              "target_property": "id"}
        },
    )
    model = canonical_model_from_studio(ontology)
    assert model["relationships"][0]["cardinality"] == "many_to_many"
    assert any(e["code"] == "JUNCTION_BINDING_MISSING" for e in model["validation"]["errors"])



def test_studio_relation_editor_persists_cardinality_and_junction_binding():
    from app.modules.ontology.studio_v2.core import Ontology

    ontology = Ontology()
    ontology.add_object_type("Department", "id", ["id"])
    ontology.add_object_type("Material", "id", ["id"])
    ontology.add_link_type(
        "uses_material", "Department", "Material",
        source_property="id", target_property="id",
        cardinality="many_to_many", junction_table="material_usage",
        junction_source_column="dept_id", junction_target_column="material_id",
    )
    relation = ontology.link_types["uses_material"]
    assert relation["cardinality"] == "many_to_many"
    assert relation["junction_table"] == "material_usage"
    assert relation["junction_source_column"] == "dept_id"
    assert relation["junction_target_column"] == "material_id"

    ontology.update_link_type("uses_material", cardinality="one_to_many")
    assert ontology.link_types["uses_material"]["cardinality"] == "one_to_many"
