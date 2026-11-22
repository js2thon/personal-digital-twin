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
