import pandas as pd

from app.modules.ontology.automodel.connectors import Table, TableSet
from app.modules.ontology.automodel import profiler


def test_profile_rebuild_keeps_column_metadata_and_manual_role():
    source = TableSet(source_id="hr", kind="file", tables={"employees": Table(
        name="employees", df=pd.DataFrame({"employee_id": ["E1", "E2"], "salary": [100, 200]}),
        primary_key=["employee_id"], column_comments={"salary": "员工薪酬"},
        column_labels={"salary": "薪资"}, column_roles={"salary": "enum"}, comment="员工主数据")})
    profile = profiler.profile(source)["employees"]
    assert profile.comment == "员工主数据"
    assert profile.columns["salary"].comment == "员工薪酬"
    assert profile.columns["salary"].label == "薪资"
    assert profile.columns["salary"].role == "enum"
    assert profile.columns["employee_id"].role == "identifier"


def test_profile_serialization_exposes_column_label():
    source = TableSet(source_id="hr", kind="file", tables={"employees": Table(
        name="employees", df=pd.DataFrame({"name": ["A", "B"]}), column_labels={"name": "员工姓名"})})
    result = profiler.profile(source)["employees"].to_dict()
    assert result["columns"]["name"]["label"] == "员工姓名"
