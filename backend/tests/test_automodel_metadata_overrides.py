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


def test_tabular_reader_adds_automodel_normalized_header_aliases():
    from app.modules.ontology.mapping_pipeline import read_tabular_file
    parsed = read_tabular_file("employees.csv", "员工编号,Employee Name\nE001,Zhang San\n".encode("utf-8"))
    row = parsed["rows"][0]
    assert row["员工编号"] == "E001"
    assert row["employee_name"] == "Zhang San"


def test_tabular_reader_rejects_colliding_normalized_headers():
    from app.modules.ontology.mapping_pipeline import read_tabular_file
    import pytest
    with pytest.raises(ValueError, match="规范化后重名"):
        read_tabular_file("employees.csv", "Employee ID,employee_id\n1,2\n".encode("utf-8"))


def test_dataset_pack_roundtrip_keeps_manual_metadata():
    from app.modules.ontology.automodel.versions import _pack, _unpack
    source = TableSet(source_id="hr", kind="file", tables={"employees": Table(
        name="employees", df=pd.DataFrame({"salary": [100]}), comment="员工主数据",
        column_comments={"salary": "员工薪酬"}, column_labels={"salary": "薪资"},
        column_roles={"salary": "enum"})})
    restored = _unpack(_pack(source)).tables["employees"]
    assert restored.comment == "员工主数据"
    assert restored.column_comments["salary"] == "员工薪酬"
    assert restored.column_labels["salary"] == "薪资"
    assert restored.column_roles["salary"] == "enum"
