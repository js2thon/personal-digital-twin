from app.modules.ontology.object_view import (
    demo_catalog, list_objects, get_object, object_graph_payload, from_schema,
)


def test_demo_catalog_has_alarm_and_hospital():
    cat = demo_catalog()
    ids = {x["id"] for x in cat["items"]}
    assert "Alarm" in ids and "Hospital" in ids
    assert cat["source"] == "demo"


def test_get_object_alarm_actions():
    obj = get_object("Alarm", force_demo=True)
    assert obj is not None
    assert "escalate" in obj["actions"]
    assert obj["quality"]["property_count"] >= 5


def test_object_graph_columns():
    g = object_graph_payload("Alarm", force_demo=True)
    assert g["object"]["id"] == "Alarm"
    assert len(g["columns"]["properties"]) >= 3
    assert len(g["columns"]["functions"]) >= 1
    assert len(g["columns"]["actions"]) >= 1
    assert any(e["kind"] == "owns" for e in g["edges"])


def test_from_schema_aggregation():
    schema = {
        "entity_types": {
            "Patient": {"label": "患者", "key": ["patient_id"], "domain": "医疗"}
        },
        "attributes": {
            "patient_id": {"owner": "Patient", "label": "患者ID", "range": "String", "role": "identity"},
            "age": {"owner": "Patient", "label": "年龄", "range": "Integer"},
        },
        "relation_types": {
            "HAS_VISIT": {"source": "Patient", "target": "Visit", "label": "就诊", "cardinality": "1:N"},
        },
    }
    cat = from_schema(schema)
    assert cat and cat["source"] == "schema"
    patient = next(x for x in cat["items"] if x["id"] == "Patient")
    assert "patient_id" in patient["properties"]
    assert "HAS_VISIT" in patient["relations"]


def test_list_objects_fallback_demo():
    cat = list_objects(schema=None, force_demo=False)
    assert cat["source"] == "demo"
    assert len(cat["items"]) >= 2
