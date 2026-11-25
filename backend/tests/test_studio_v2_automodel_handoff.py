from types import SimpleNamespace

from app.api.v1.studio_v2 import _studio_v2_automodel_schema
from app.modules.ontology.automodel import mapper
from app.modules.ontology.automodel.profiler import ColumnProfile, TableProfile


def test_studio_v2_schema_handoff_preserves_edited_business_model():
    ontology = SimpleNamespace(
        object_types={
            "Employee": {
                "primary_key": "employee_id",
                "properties": ["employee_id", "display_name", "department_id"],
                "label": "员工",
                "description": "员工业务对象",
                "title_property": "display_name",
                "property_definitions": {"display_name": {"type": "string"}},
                "property_mappings": {
                    "employee_id": {"source_column": "employee_id"},
                    "display_name": {"source_column": "full_name"},
                    "department_id": {"source_column": "dept_id"},
                },
            },
            "Department": {
                "primary_key": "dept_id",
                "properties": ["dept_id", "dept_name"],
                "label": "部门",
                "title_property": "dept_name",
                "property_definitions": {},
                "property_mappings": {"dept_name": {"source_column": "name"}},
            },
        },
        data_mapping={
            "Employee": {"source_table": "employees", "columns": [
                "employee_id", "full_name", "dept_id"
            ], "primary_key": "employee_id"},
            "Department": {"source_table": "departments", "columns": [
                "dept_id", "name"
            ], "primary_key": "dept_id"},
        },
        link_types={
            "works_in": {
                "source": "Employee", "target": "Department",
                "source_property": "department_id", "target_property": "dept_id",
                "description": "任职部门",
            }
        },
    )
    result = _studio_v2_automodel_schema(
        SimpleNamespace(session_id="session-1", ontology=ontology), "studio_v2"
    )

    schema = result["schema"]
    assert result["class_map"] == {
        "employees": "Employee", "departments": "Department"
    }
    assert schema["entity_types"]["Employee"]["label"] == "员工"
    assert schema["entity_types"]["Employee"]["title_property"] == "display_name"
    assert schema["entity_types"]["Employee"]["backing_sources"][0][
        "property_mappings"
    ]["full_name"] == "display_name"
    assert schema["relation_types"]["works_in"]["column"] == "dept_id"
    assert schema["relation_types"]["works_in"]["target_table"] == "departments"


def test_mapping_contract_accepts_verified_explicit_studio_relation_binding():
    columns = lambda names: {
        name: ColumnProfile(
            name=name, dtype="integer", role="identifier", null_ratio=0,
            distinct=3, distinct_ratio=1, unique=True, samples=[]
        )
        for name in names
    }
    profiles = {
        "employees": TableProfile(
            name="employees", rows=3, primary_key=["employee_id"],
            pk_inferred=False, columns=columns(["employee_id", "dept_id"]),
            foreign_keys=[],
        ),
        "departments": TableProfile(
            name="departments", rows=3, primary_key=["dept_id"],
            pk_inferred=False, columns=columns(["dept_id"]),
            foreign_keys=[],
        ),
    }
    schema = {
        "schema_version": "studio-v2-test",
        "entity_types": {"Employee": {}, "Department": {}},
        "attributes": {},
        "relation_types": {
            "works_in": {
                "source": "Employee", "target": "Department",
                "cardinality": "many_to_one", "table": "employees",
                "column": "dept_id", "target_table": "departments",
                "target_column": "dept_id",
            }
        },
    }
    draft = {
        "schema": schema,
        "class_map": {"employees": "Employee", "departments": "Department"},
    }

    contract = mapper.build_contract("studio_v2", draft, profiles)
    issues = mapper.validate_contract(contract, schema, profiles)

    relation = contract["mappings"][0]["relations"][0]
    assert relation["explicit_binding"] is True
    assert relation["target_id"] == "studio_v2:departments:{value}"
    assert not [x for x in issues if x["code"] == "RELATION_FK_MISMATCH"]
