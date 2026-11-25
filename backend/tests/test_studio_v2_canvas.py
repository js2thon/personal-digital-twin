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
    assert onto.update_property("Employee", "name", clear_source_mapping=True)
    graph = schema_graph_payload(onto)
    name_prop = next(n for n in graph["nodes"] if n["id"] == "PROP:Employee:name")
    assert name_prop["source"]["mapped"] is False
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



def test_schema_edit_endpoint_adds_source_bound_property():
    sess = _session()
    try:
        result = studio_v2.schema_edit(
            sess.session_id,
            studio_v2.SchemaEditRequest(ops=[
                studio_v2.SchemaEditOp(
                    op="add_property", type_name="Employee",
                    property_name="displayName", source_column="employee_name",
                )
            ]),
        )
        assert result["ok"] is True
        graph = schema_graph_payload(sess.ontology)
        prop = next(n for n in graph["nodes"] if n["id"] == "PROP:Employee:displayName")
        assert prop["source"]["source_column"] == "employee_name"
        assert prop["source"]["mapped"] is True
    finally:
        default_store.delete(sess.session_id)


def test_dataset_compare_reports_diff_without_mutating_session(monkeypatch):
    from types import SimpleNamespace
    from app.modules.ontology.automodel import versions

    sess = _session()
    sess.meta["source_info"] = {"kind": "dataset", "dataset_id": "old-dataset"}
    incoming = SimpleNamespace(tables={
        "Employee": SimpleNamespace(
            name="Employee",
            df=pd.DataFrame([{
                "employee_id": "E1", "department_id": "D1",
                "employee_name": "Ada", "employee_level": "L5",
            }]),
        ),
        "Department": SimpleNamespace(
            name="Department",
            df=pd.DataFrame([{"department_id": "D1", "department_name": "Engineering"}]),
        ),
    })
    monkeypatch.setattr(versions, "load_dataset", lambda dataset_id, tenant: incoming)
    before = list(sess.ontology.object_types["Employee"]["properties"])
    try:
        diff = studio_v2.compare_source_dataset(sess.session_id, "new-dataset")
        assert diff["requires_review"] is True
        assert diff["mutated"] is False
        assert diff["dataset_changed"] is True
        employee = next(d for d in diff["table_diffs"] if d["table"] == "Employee")
        assert "employee_level" in employee["added_columns"]
        assert sess.ontology.object_types["Employee"]["properties"] == before
    finally:
        default_store.delete(sess.session_id)



def test_relation_rebuild_uses_configured_non_primary_target_property():
    onto = AutoOntologyBuilder().from_dataframes({
        "Employee": pd.DataFrame([{"employee_id": "E1", "dept_code": "D-ENG"}]),
        "Department": pd.DataFrame([
            {"department_id": "D1", "dept_code": "D-ENG"},
        ]),
    })
    onto.add_link_type(
        "belongsToDepartment", "Employee", "Department",
        source_property="dept_code", target_property="dept_code",
    )
    onto.rebuild_links()
    edges = list(onto.links.edges(data=True, keys=True))
    assert len(edges) == 1
    assert edges[0][0] == "Employee:E1"
    assert edges[0][1] == "Department:D1"
    assert edges[0][3]["target_pk"] == "D1"
