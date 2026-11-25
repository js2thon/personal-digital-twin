import pandas as pd
import pytest

from app.api.v1 import studio_v2
from app.modules.ontology.studio_v2.core import AutoOntologyBuilder, schema_graph_payload
from app.modules.ontology.studio_v2.service import default_store


def _session():
    tables = {
        "Employee": pd.DataFrame([
            {"employee_id": "E1", "department_id": "D1", "employee_name": "Ada"},
        ]),
        "Department": pd.DataFrame([
            {"department_id": "D1", "department_name": "Engineering"},
        ]),
    }
    return default_store.build_from_dataframes(tables, name="canvas-test")


def test_schema_graph_exposes_source_lineage():
    onto = AutoOntologyBuilder().from_dataframes({
        "Employee": pd.DataFrame([{"employee_id": "E1", "department_id": "D1"}]),
        "Department": pd.DataFrame([{"department_id": "D1"}]),
    })
    graph = schema_graph_payload(onto)
    employee = next(n for n in graph["nodes"] if n["id"] == "CLASS:Employee")
    prop = next(n for n in graph["nodes"] if n["id"] == "PROP:Employee:department_id")
    rel = next(e for e in graph["edges"] if e["kind"] == "objectProperty")
    assert employee["source"]["source_table"] == "Employee"
    assert prop["source"]["source_column"] == "department_id"
    assert prop["source"]["mapped"] is True
    assert rel["source_binding"]["source_table"] == "Employee"


def test_property_lifecycle_keeps_source_binding():
    onto = AutoOntologyBuilder().from_dataframes({
        "Employee": pd.DataFrame([{"employee_id": "E1", "name": "Ada", "display_name": "Ada L"}]),
    })
    assert onto.add_property("Employee", "displayName", source_column="display_name")
    assert onto.objects["Employee"]["E1"]["displayName"] == "Ada L"
    assert onto.update_property("Employee", "displayName", new_name="displayLabel")
    assert "displayLabel" in onto.object_types["Employee"]["properties"]
    assert onto.remove_property("Employee", "displayLabel")
    with pytest.raises(ValueError):
        onto.remove_property("Employee", "employee_id")


def test_canvas_layout_is_saved_and_restored_in_session():
    sess = _session()
    try:
        node_id = "CLASS:Employee"
        saved = studio_v2.save_canvas_layout(
            sess.session_id,
            studio_v2.CanvasLayoutRequest(positions={node_id: {"x": 120, "y": 240}}),
        )
        assert saved["saved"] == 1
        assert studio_v2.get_canvas_layout(sess.session_id)["positions"][node_id] == {"x": 120.0, "y": 240.0}
    finally:
        default_store.delete(sess.session_id)
