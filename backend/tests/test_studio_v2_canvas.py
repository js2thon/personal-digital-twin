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
    assert onto.add_property("Employee", "departmentCode", source_column="dept_code")
    assert onto.add_property("Department", "departmentCode", source_column="dept_code")
    onto.add_link_type(
        "belongsToDepartment", "Employee", "Department",
        source_property="departmentCode", target_property="departmentCode",
    )
    onto.rebuild_links()
    graph = schema_graph_payload(onto)
    relation = next(e for e in graph["edges"] if e.get("label") == "belongsToDepartment")
    assert relation["source_binding"]["source_column"] == "dept_code"
    assert relation["source_binding"]["mapped"] is True
    edges = list(onto.links.edges(data=True, keys=True))
    assert len(edges) == 1
    assert edges[0][0] == "Employee:E1"
    assert edges[0][1] == "Department:D1"
    assert edges[0][3]["target_pk"] == "D1"
    onto.update_property("Employee", "departmentCode", clear_source_mapping=True)
    onto.rebuild_links()
    assert onto.links.number_of_edges() == 0



def test_relation_editor_can_clear_source_field_binding():
    onto = AutoOntologyBuilder().from_dataframes({
        "Employee": pd.DataFrame([{"employee_id": "E1", "department_id": "D1"}]),
        "Department": pd.DataFrame([{"department_id": "D1"}]),
    })
    relation_name = next(iter(onto.link_types))
    assert onto.link_types[relation_name]["source_property"] == "department_id"
    assert onto.update_link_type(
        relation_name, source_property="", target_property="",
        clear_source_property=True, clear_target_property=True,
    )
    assert onto.link_types[relation_name]["source_property"] == ""
    assert onto.link_types[relation_name]["target_property"] == ""


def test_studio_session_json_snapshot_round_trip_preserves_schema_and_lineage():
    from app.modules.ontology.studio_v2.service import PersistentStudioV2Store

    sess = _session()
    try:
        sess.meta["source_info"] = {"kind": "dataset", "dataset_id": "dataset-1"}
        sess.meta["canvas_layout"] = {"CLASS:Employee": {"x": 90.0, "y": 180.0}}
        payload = PersistentStudioV2Store._dump(sess)
        restored = PersistentStudioV2Store._restore(payload)
        assert restored.session_id == sess.session_id
        assert restored.meta["source_info"]["dataset_id"] == "dataset-1"
        assert restored.meta["canvas_layout"]["CLASS:Employee"]["x"] == 90.0
        assert restored.ontology.object_types == sess.ontology.object_types
        assert restored.ontology.objects == sess.ontology.objects
        assert restored.ontology.data_mapping == sess.ontology.data_mapping
        assert restored.ontology.links.number_of_nodes() == sess.ontology.links.number_of_nodes()
        assert restored.ontology.links.number_of_edges() == sess.ontology.links.number_of_edges()
    finally:
        default_store.delete(sess.session_id)


def test_studio_v2_database_store_restart_and_version_restore():
    from app.modules.ontology.studio_v2.service import PersistentStudioV2Store

    sess = _session()
    restarted_store = PersistentStudioV2Store()
    try:
        sess.meta["canvas_layout"] = {"CLASS:Employee": {"x": 30.0, "y": 60.0}}
        sess.touch()
        version = default_store.create_version(sess.session_id, "baseline", "before edit")
        sess.meta["canvas_layout"] = {"CLASS:Employee": {"x": 300.0, "y": 600.0}}
        sess.touch()

        restored_session = restarted_store.require(sess.session_id)
        assert restored_session.meta["canvas_layout"]["CLASS:Employee"]["x"] == 300.0
        versions = restarted_store.list_versions(sess.session_id)
        assert versions[0]["id"] == version["id"]

        restored_store.restore_version(sess.session_id, version["id"])
        assert restarted_store.require(sess.session_id).meta["canvas_layout"]["CLASS:Employee"]["x"] == 30.0
    finally:
        default_store.delete(sess.session_id)
