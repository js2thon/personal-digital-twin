"""Focused regression tests for business-object-centered ontology modeling."""
from __future__ import annotations

import pandas as pd

from app.modules.ontology.automodel import connectors, pipeline
from app.modules.ontology.semantic.schema_def import (
    AttributeDef,
    EntityTypeDef,
    InterfaceDef,
    ObjectIdentityRule,
    ObjectSourceBinding,
    SemanticSchema,
    validate_business_model,
)


def test_semantic_contract_supports_multi_source_business_object_and_interface():
    schema = SemanticSchema(
        entity_types=[
            EntityTypeDef(
                name="Employee",
                object_kind="entity",
                title_property="full_name",
                attributes=[
                    AttributeDef(name="employee_no", identifier=True),
                    AttributeDef(name="full_name", title=True, required=True),
                ],
                identity_rules=[
                    ObjectIdentityRule(source_system="hr", key_properties=["employee_no"])
                ],
                backing_sources=[
                    ObjectSourceBinding(
                        source_id="hr", table="employees", primary_key=["emp_id"],
                        property_mappings={"emp_no": "employee_no", "name": "full_name"},
                    ),
                    ObjectSourceBinding(
                        source_id="iam", table="accounts", primary_key=["account_id"],
                        property_mappings={"login": "employee_no"}, mode="federated",
                    ),
                ],
                interfaces=["Identifiable"],
            )
        ],
        interfaces=[InterfaceDef(name="Identifiable", properties=["employee_no"])],
    )
    assert validate_business_model(schema) == []
    assert len(schema.entity_types[0].backing_sources) == 2


def test_semantic_contract_rejects_invalid_identity_mapping_and_inverse_links():
    schema = {
        "entity_types": {
            "Employee": {
                "name": "Employee",
                "object_kind": "entity",
                "attributes": [{"name": "employee_no"}],
                "identity_rules": [{"key_properties": ["missing_key"]}],
                "backing_sources": [{
                    "source_id": "hr", "mode": "materialized", "primary_key": [],
                    "property_mappings": {"emp_name": "missing_property"},
                }],
                "interfaces": ["UnknownInterface"],
            }
        },
        "relation_types": {
            "reports_to": {
                "source_types": ["Employee"], "target_types": ["MissingObject"],
                "inverse_of": "managed_by",
            },
            "managed_by": {
                "source_types": ["Employee"], "target_types": ["Employee"],
                "inverse_of": "wrong_inverse",
            },
        },
        "interfaces": {},
    }
    codes = {item["code"] for item in validate_business_model(schema)}
    assert {
        "UNKNOWN_IDENTITY_PROPERTY", "SOURCE_BINDING_WITHOUT_KEY",
        "UNKNOWN_BOUND_PROPERTY", "UNKNOWN_INTERFACE",
        "UNKNOWN_RELATION_ENDPOINT", "NON_RECIPROCAL_INVERSE",
    } <= codes


def test_automodel_generates_business_object_identity_title_and_source_binding():
    ts = connectors.TableSet("hr", "file")
    ts.tables["employees"] = connectors.Table(
        "employees",
        pd.DataFrame({
            "emp_no": [1001, 1002, 1003],
            "full_name": ["Zhang San", "Li Si", "Wang Wu"],
            "salary": [12000.0, 15000.0, 18000.0],
            "hire_date": ["2022-01-01", "2023-02-01", "2024-03-01"],
        }),
        row_count=3,
    )
    job = pipeline.run(ts, stop_after="validate")
    person = job["draft"]["schema"]["entity_types"]["Person"]
    assert person["object_kind"] == "entity"
    assert person["title_property"] == "full_name"
    assert person["identity_rules"][0]["key_properties"] == ["emp_no"]
    binding = person["backing_sources"][0]
    assert binding["source_id"] == "hr"
    assert binding["table"] == "employees"
    assert binding["property_mappings"]["full_name"] == "full_name"
    properties = {item["name"]: item for item in person["attributes"]}
    assert properties["salary"]["sensitivity"] == "confidential"
    assert properties["hire_date"]["temporal_role"] == "event_time"
    assert job["business_model_issues"] == []



def test_modeling_standards_report_distinguishes_blocking_errors_and_review_warnings():
    from app.modules.ontology.semantic.standards import check_ontology_modeling_standards

    report = check_ontology_modeling_standards({
        "entity_types": {
            "employee": {
                "name": "employee", "object_kind": "invalid_kind",
                "attributes": [
                    {"name": "EmployeeNo", "range": "enum", "enum_values": [],
                     "description": "", "sensitivity": "internal"},
                ],
                "identity_rules": [{"key_properties": ["EmployeeNo"]}],
                "title_property": "unknown_title",
                "backing_sources": [{
                    "source_id": "hr", "table": "employees", "primary_key": ["id"],
                    "property_mappings": {"name": "unknown_property"},
                }],
            }
        },
        "relation_types": {
            "works_for": {
                "source_types": ["*"], "target_types": ["MissingDepartment"],
                "cardinality": "invalid",
            }
        },
        "interfaces": {},
    })
    codes = {item["code"] for item in report["findings"]}
    assert report["status"] == "fail"
    assert report["summary"]["errors"] > 0
    assert report["summary"]["warnings"] > 0
    assert {
        "OBJECT_NAME_NOT_PASCAL_CASE", "OBJECT_KIND_INVALID",
        "PROPERTY_NAME_NOT_SNAKE_CASE", "ENUM_VALUES_MISSING",
        "OBJECT_TITLE_UNKNOWN", "SOURCE_TARGET_PROPERTY_UNKNOWN",
        "RELATION_CARDINALITY_INVALID", "RELATION_ENDPOINT_UNTYPED",
        "RELATION_ENDPOINT_UNKNOWN",
    } <= codes


def test_modeling_standards_checks_interface_per_implementing_object_and_inheritance_cycles():
    from app.modules.ontology.semantic.standards import check_ontology_modeling_standards

    report = check_ontology_modeling_standards({
        "entity_types": {
            "Employee": {"name": "Employee", "parent": "Manager",
                         "interfaces": ["Identifiable"],
                         "attributes": [{"name": "employee_no", "description": "工号"}]},
            "Manager": {"name": "Manager", "parent": "Employee",
                        "attributes": [{"name": "manager_no", "description": "管理者编号"}]},
        },
        "relation_types": {},
        "interfaces": {"Identifiable": {"name": "Identifiable",
                                        "properties": ["employee_no", "global_id"]}},
    })
    codes = {item["code"] for item in report["findings"]}
    assert "OBJECT_INHERITANCE_CYCLE" in codes
    assert "INTERFACE_PROPERTY_NOT_IMPLEMENTED" in codes


def test_automodel_pipeline_exposes_standards_report():
    ts = connectors.TableSet("hr", "file")
    ts.tables["employees"] = connectors.Table(
        "employees",
        pd.DataFrame({
            "emp_no": [1001, 1002, 1003],
            "full_name": ["Zhang San", "Li Si", "Wang Wu"],
            "salary": [12000.0, 15000.0, 18000.0],
            "hire_date": ["2022-01-01", "2023-02-01", "2024-03-01"],
        }),
        row_count=3,
    )
    job = pipeline.run(ts, stop_after="validate")
    assert "standards_report" in job
    assert job["standards_report"]["summary"]["total"] >= 0
    assert job["stages"]["modeling_standards"]["ok"] is True


def test_business_object_discovery_is_an_independent_pipeline_stage():
    ts = connectors.TableSet("hr", "file")
    ts.tables["employees"] = connectors.Table(
        "employees",
        pd.DataFrame({"emp_no": [1, 2], "full_name": ["A", "B"]}),
        row_count=2,
    )
    job = pipeline.run(ts, stop_after="business_object_discovery")
    assert "profile" in job["stages"]
    assert "business_object_discovery" in job["stages"]
    assert "draft" not in job["stages"]
    assert job["business_object_discovery"]["candidate_count"] == 1


def test_business_object_discovery_proposes_cross_table_merge_with_evidence():
    from app.modules.ontology.automodel import business_object_discovery, profiler

    ts = connectors.TableSet("hr", "file")
    ts.tables["employees"] = connectors.Table(
        "employees", pd.DataFrame({"emp_no": [1, 2], "full_name": ["A", "B"]}), row_count=2)
    ts.tables["staff"] = connectors.Table(
        "staff", pd.DataFrame({"emp_no": [3, 4], "full_name": ["C", "D"]}), row_count=2)
    report = business_object_discovery.discover(profiler.profile(ts))
    assert report["candidate_count"] == 2
    assert report["merge_candidate_count"] == 1
    assert report["merge_candidates"][0]["decision"] == "review_merge"
    assert report["policy"]["auto_merge"] is False


def test_review_attribute_rename_updates_all_business_object_references():
    job = {
        "id": "review-attr",
        "draft": {
            "schema": {
                "entity_types": {"Employee": {
                    "attributes": [{"name": "employee_no"}],
                    "title_property": "employee_no",
                    "identity_rules": [{"key_properties": ["employee_no"]}],
                    "backing_sources": [{"source_id": "hr", "table": "employees",
                                         "property_mappings": {"emp_no": "employee_no"}}],
                }},
                "relation_types": {},
                "attributes": {"employee_no": {"owner": "Employee", "column": "emp_no"}},
                "interfaces": {"Identifiable": {"properties": ["employee_no"]}},
            },
            "class_map": {"employees": "Employee"}, "inferences": [], "review": [],
        },
    }
    pipeline.apply_review(job, [{"kind": "attribute", "ref": "employee_no",
                                 "action": "rename", "new_name": "staff_no"}])
    schema = job["draft"]["schema"]
    assert "staff_no" in schema["attributes"]
    assert schema["entity_types"]["Employee"]["attributes"][0]["name"] == "staff_no"
    assert schema["entity_types"]["Employee"]["title_property"] == "staff_no"
    assert schema["entity_types"]["Employee"]["identity_rules"][0]["key_properties"] == ["staff_no"]
    assert schema["entity_types"]["Employee"]["backing_sources"][0]["property_mappings"]["emp_no"] == "staff_no"
    assert schema["interfaces"]["Identifiable"]["properties"] == ["staff_no"]


def test_review_merge_consolidates_classes_and_keeps_source_bindings():
    job = {
        "id": "review-merge",
        "draft": {
            "schema": {
                "entity_types": {
                    "Person": {"object_kind": "entity", "attributes": [{"name": "name"}],
                               "backing_sources": [{"source_id": "hr", "table": "employees"}]},
                    "Staff": {"object_kind": "entity", "attributes": [{"name": "staff_code"}],
                              "backing_sources": [{"source_id": "iam", "table": "accounts"}]},
                },
                "relation_types": {"works_for": {"source": "Staff", "target": "Person"}},
                "attributes": {"name": {"owner": "Person"}, "staff_code": {"owner": "Staff"}},
                "interfaces": {},
            },
            "class_map": {"employees": "Person", "accounts": "Staff"},
            "inferences": [], "review": [],
        },
    }
    pipeline.apply_review(job, [{"kind": "class", "ref": "Staff", "action": "merge", "target": "Person"}])
    schema = job["draft"]["schema"]
    assert "Staff" not in schema["entity_types"]
    assert job["draft"]["class_map"] == {"employees": "Person", "accounts": "Person"}
    assert len(schema["entity_types"]["Person"]["backing_sources"]) == 2
    assert schema["relation_types"]["works_for"]["source"] == "Person"
    assert schema["attributes"]["staff_code"]["owner"] == "Person"


def test_publish_schema_merge_preserves_interfaces_and_source_bindings():
    from app.modules.ontology.automodel.publish import merge_schema

    current = {
        "entity_types": {"Employee": {
            "object_kind": "entity", "identity_rules": [{"source_system": "hr", "key_properties": ["employee_no"]}],
            "backing_sources": [{"source_id": "hr", "table": "employees"}],
            "interfaces": ["Identifiable"], "attributes": [{"name": "employee_no", "range": "string"}],
        }},
        "relation_types": {}, "attributes": {}, "interfaces": {},
    }
    draft = {
        "entity_types": {"Employee": {
            "object_kind": "entity", "identity_rules": [{"source_system": "iam", "key_properties": ["login"]}],
            "backing_sources": [{"source_id": "iam", "table": "accounts"}],
            "interfaces": ["Auditable"], "attributes": [{"name": "login", "range": "string"}],
        }},
        "relation_types": {}, "attributes": {},
        "interfaces": {"Auditable": {"name": "Auditable", "properties": ["login"]}},
    }
    result = merge_schema(current, draft, "v2")
    employee = result["schema"]["entity_types"]["Employee"]
    assert len(employee["backing_sources"]) == 2
    assert {x["source_system"] for x in employee["identity_rules"]} == {"hr", "iam"}
    assert set(employee["interfaces"]) == {"Identifiable", "Auditable"}
    assert "Auditable" in result["schema"]["interfaces"]
    assert result["conflicts"] == []


def test_mapping_contract_uses_source_specific_bindings_for_same_business_object():
    from app.modules.ontology.automodel import mapper, profiler

    schema = {
        "schema_version": "v2",
        "entity_types": {"Employee": {
            "title_property": "full_name",
            "backing_sources": [
                {"source_id": "hr", "table": "employees", "primary_key": ["emp_id"],
                 "property_mappings": {"emp_name": "full_name"}},
                {"source_id": "iam", "table": "accounts", "primary_key": ["account_id"],
                 "property_mappings": {"login": "login_name"}},
            ],
        }},
        "relation_types": {},
        "attributes": {
            "full_name": {"owner": "Employee", "column": "emp_name"},
            "login_name": {"owner": "Employee", "column": "login"},
        },
    }
    hr = connectors.TableSet("hr", "file")
    hr.tables["employees"] = connectors.Table(
        "employees", pd.DataFrame({"emp_id": [1], "emp_name": ["Alice"]}), row_count=1)
    hr_contract = mapper.build_contract(
        "hr", {"schema": schema, "class_map": {"employees": "Employee"}}, profiler.profile(hr))
    assert len(hr_contract["mappings"]) == 1
    assert hr_contract["mappings"][0]["primary_key"] == ["emp_id"]
    assert hr_contract["mappings"][0]["attributes"] == {"full_name": "emp_name"}
    assert hr_contract["mappings"][0]["name"] == "emp_name"

    iam = connectors.TableSet("iam", "file")
    iam.tables["accounts"] = connectors.Table(
        "accounts", pd.DataFrame({"account_id": [2], "login": ["alice"]}), row_count=1)
    iam_contract = mapper.build_contract(
        "iam", {"schema": schema, "class_map": {"accounts": "Employee"}}, profiler.profile(iam))
    assert len(iam_contract["mappings"]) == 1
    assert iam_contract["mappings"][0]["primary_key"] == ["account_id"]
    assert iam_contract["mappings"][0]["attributes"] == {"login_name": "login"}