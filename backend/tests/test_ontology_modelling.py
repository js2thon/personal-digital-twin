"""本体建模工作流回归：画布 7 类建模节点契约 + 四类完整验证 + 收尾区发布门禁。

纯函数层零库可测；API 自检端点用 TestClient（不写库）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ontology.modelling import (clean_rules as CR, node_schema as NS,
                                             sample as S, validator as V,
                                             workflow as WF, assistant as ASST)

client = TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ---------------------------------------------------------------- 契约
def test_schema_7_types_and_order():
    assert len(NS.WORKFLOW_ORDER) == 7
    assert NS.WORKFLOW_ORDER[0] == "datasource"
    assert NS.WORKFLOW_ORDER[-1] == "action"
    assert NS.MANDATORY_KINDS == ()  # 画布无强制节点；门禁按验证报告判定
    # 退出画布的 4 类节点不再出现在契约里
    for gone in ("schema_mapping", "instantiate", "validation", "publish"):
        assert gone not in NS.NODE_TYPES


def test_node_json_schema_shape():
    s = NS.node_json_schema()
    assert s["$schema"].endswith("draft-07/schema#")
    assert set(s["x-workflow-order"]) == set(NS.WORKFLOW_ORDER)
    assert "entity" in s["x-node-types"]
    assert "primary_key" in s["x-node-types"]["entity"]["fields"]
    # Schema 映射内化字段在 entity 契约里
    ef = s["x-node-types"]["entity"]["fields"]
    for f in ("source_field", "is_pk", "target_entity", "source_fields"):
        assert f in ef


def test_internalized_mapping_fields_in_sample():
    spec = S.sample_spec()
    ent = [n for n in spec["nodes"] if n["id"] == "ent_emp"][0]
    assert ent["fields"]["target_entity"] == "Employee"
    assert "emp_no=employee_no" in ent["fields"]["mappings"]
    assert not any(n["kind"] in ("schema_mapping", "instantiate", "validation", "publish")
                   for n in spec["nodes"])
    assert spec["instantiate"]["target_entity"] == "Employee"
    assert spec["publish"]["version"] == "v1.0.0"


def test_legacy_kind_mapping():
    assert NS.kind_of("sample") == "datasource"
    assert NS.kind_of("object") == "entity"
    assert NS.kind_of("func") == "function"
    assert NS.kind_of("mapping") == "entity"
    assert NS.kind_of("instantiate") == "entity"
    assert NS.kind_of("validation") == "entity"
    assert NS.kind_of("publish") == "entity"
    assert NS.kind_of("unknown") is None


# ---------------------------------------------------------------- 结构完整性
def test_structure_no_mandatory_validation_node():
    # 发布门禁按四类验证报告判定，画布不再强制 validation 节点
    spec = {"nodes": [{"id": "e1", "kind": "entity", "fields": {"name": "X", "primary_key": "id"}}], "edges": []}
    items = V.check_structure(spec)
    assert not any(i["code"] == "MANDATORY_VALIDATION" for i in items)


def test_structure_entity_without_pk_fails():
    spec = {"nodes": [
        {"id": "e1", "kind": "entity", "fields": {"name": "X"}},
        {"id": "v1", "kind": "validation", "fields": {"name": "V"}},
    ], "edges": []}
    items = V.check_structure(spec)
    assert any(i["code"] == "ENTITY_NO_PK" for i in items)


def test_structure_relation_bad_cardinality():
    spec = {"nodes": [
        {"id": "e1", "kind": "entity", "fields": {"name": "A", "primary_key": "id"}},
        {"id": "e2", "kind": "entity", "fields": {"name": "B", "primary_key": "id"}},
        {"id": "r1", "kind": "relation", "fields": {"name": "r", "source_entity": "A", "target_entity": "B", "cardinality": ""}},
        {"id": "v1", "kind": "validation", "fields": {"name": "V"}},
    ], "edges": []}
    items = V.check_structure(spec)
    assert any(i["code"] == "REL_BAD_CARDINALITY" for i in items)
    assert not any(i["code"] == "REL_ENDPOINT_TYPE" for i in items)


def test_structure_cyclic_inheritance():
    spec = {"nodes": [
        {"id": "e1", "kind": "entity", "fields": {"name": "A", "primary_key": "id", "type_system": "parent=B"}},
        {"id": "e2", "kind": "entity", "fields": {"name": "B", "primary_key": "id", "type_system": "parent=A"}},
        {"id": "v1", "kind": "validation", "fields": {"name": "V"}},
    ], "edges": []}
    items = V.check_structure(spec)
    assert any(i["code"] == "CYCLIC_INHERITANCE" for i in items)


# ---------------------------------------------------------------- 数据驱动：no-data 不编造
def test_no_data_marks_nodata_not_pass():
    spec = S.sample_spec()
    report = V.validate_workflow(spec, {})
    assert report["complete"] is False
    assert report["nodata_count"] > 0
    assert report["summary"]["coverage"]["level"] == "no-data"
    assert report["summary"]["consistency"]["level"] == "no-data"


# ---------------------------------------------------------------- 覆盖度 / 一致性 / 可执行性
def test_required_fill_below_fail():
    spec = S.sample_spec()
    data = S.sample_data()
    data["entities"]["Employee"][0]["properties"]["full_name"] = ""
    items = V.check_coverage(spec, data)
    assert any(i["code"] == "REQUIRED_FILL_BELOW" for i in items)


def test_uniqueness_conflict():
    spec = S.sample_spec()
    data = S.sample_data()
    data["entities"]["Employee"][1]["properties"]["email"] = data["entities"]["Employee"][0]["properties"]["email"]
    items = V.check_consistency(spec, data)
    assert any(i["code"] == "UNIQUENESS_CONFLICT" for i in items)


def test_referential_broken():
    spec = S.sample_spec()
    data = S.sample_data()
    data["relations"][0]["target_id"] = "MISSING"
    items = V.check_consistency(spec, data)
    assert any(i["code"] == "REFERENTIAL_BROKEN" for i in items)


def test_function_unresolved_input():
    spec = S.sample_spec()
    for n in spec["nodes"]:
        if n["id"] == "fn_risk":
            n["fields"]["inputs"] = "not_exist"
    items = V.check_executability(spec, S.sample_data())
    assert any(i["code"] == "FN_INPUT_UNRESOLVED" for i in items)


# ---------------------------------------------------------------- 闭环示例
def test_sample_complete_and_gate_allows():
    r = S.run_sample()
    assert r["report"]["complete"] is True
    assert r["report"]["verdict"] == "本体建模完整"
    assert r["publish"]["allowed"] is True and r["publish"]["blocked"] is False
    for cat in ("structure", "coverage", "consistency", "executability"):
        assert r["report"]["summary"][cat]["passed"] is True


def test_publish_gate_blocks_when_incomplete():
    spec = S.sample_spec()
    r = WF.run(spec, S.sample_data())
    # 破坏一致性：制造必填空值
    assert r["publish"]["allowed"] is True
    data = S.sample_data()
    data["entities"]["Employee"][0]["properties"]["email"] = ""
    r2 = WF.run(spec, data)
    assert r2["report"]["complete"] is False
    assert r2["publish"]["blocked"] is True and r2["publish"]["allowed"] is False


def test_topo_no_cycle_and_order():
    t = WF.topo_order(S.sample_spec())
    assert t["has_cycle"] is False
    assert t["order"].index("ds_file") < t["order"].index("ent_emp") < t["order"].index("act_notify")


def test_topo_detects_cycle():
    spec = {"nodes": [{"id": "a", "kind": "entity", "fields": {}},
                      {"id": "b", "kind": "function", "fields": {}}],
            "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]}
    assert WF.topo_order(spec)["has_cycle"] is True


# ---------------------------------------------------------------- API 自检
def test_api_selftest():
    r = client.get("/api/v1/ontology/modelling/selftest")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["validator"]["pass"] is True
    assert j["workflow"]["pass"] is True
    assert j["sample"]["complete"] is True


def test_api_sample_run():
    r = client.post("/api/v1/ontology/modelling/sample/run")
    assert r.status_code == 200, r.text
    assert r.json()["report"]["complete"] is True


def test_api_contract():
    r = client.get("/api/v1/ontology/modelling/contract")
    assert r.status_code == 200, r.text
    assert len(r.json()["order"]) == 7


def test_publish_gate_from_tail_config():
    """发布门禁按四类验证报告判定，版本来自收尾区 spec.publish。"""
    spec = S.sample_spec()
    r = WF.run(spec, S.sample_data())
    assert r["report"]["complete"] is True
    assert r["publish"]["allowed"] is True and r["publish"]["blocked"] is False
    assert r["publish"]["versions"] == ["v1.0.0"]
    assert r["publish"]["source"] == "tail"
    # 验证不通过 → 阻断
    data = S.sample_data()
    data["entities"]["Employee"][0]["properties"]["full_name"] = ""
    r2 = WF.run(spec, data)
    assert r2["report"]["complete"] is False
    assert r2["publish"]["blocked"] is True and r2["publish"]["allowed"] is False
    # 无版本号 → 不发起发布、不阻断
    spec2 = dict(spec); spec2["publish"] = {}
    r3 = WF.run(spec2, S.sample_data())
    assert r3["publish"]["versions"] == [] and r3["publish"]["blocked"] is False


# ---------------------------------------------------------------- 设计助手（LLM 对话 + 降级）
def test_assistant_selfcheck():
    assert ASST.self_check()["pass"] is True


def test_assistant_digest_pure():
    spec = {"nodes": [
        {"id": "a", "kind": "entity", "fields": {"name": "员工", "primary_key": "id"}},
        {"id": "b", "kind": "function", "fields": {"name": "年薪", "logic": "月薪*12"}},
    ], "edges": [{"from": "a", "to": "b"}]}
    d = ASST.spec_digest(spec)
    assert d["node_count"] == 2 and d["edge_count"] == 1
    assert "员工" in d["nodes"][0]
    assert d["kind_count"]["entity"] == 1
    p = ASST.build_user_prompt("有哪些对象？", d)
    assert "员工" in p and "用户问题" in p


def test_assistant_ask_with_injected_llm():
    def fake_chat(system, user, temperature=0.2, timeout=None):
        assert "只依据给定草稿" in system
        assert "用户问题" in user
        return "草稿里有 1 个实体：员工。"
    r = ASST.ask("有哪些对象？", {"nodes": [{"id": "a", "kind": "entity", "fields": {"name": "员工"}}],
                                "edges": []}, chat=fake_chat)
    assert r["available"] is True and "员工" in r["answer"]


def test_assistant_ask_degrades_when_llm_down():
    def boom(*a, **k):
        raise ConnectionError("refused")
    r = ASST.ask("测试", {"nodes": [], "edges": []}, chat=boom)
    assert r["available"] is False and r["fallback"] is True
    assert "ConnectionError" in r["reason"] and r["answer"] == ""


def test_assistant_ask_empty_question():
    r = ASST.ask("   ", {"nodes": [], "edges": []}, chat=lambda *a, **k: "x")
    assert r["available"] is False and "为空" in r["reason"]


def test_api_assist_endpoint():
    r = client.post("/api/v1/ontology/modelling/assist",
                    json={"question": "这份草稿能支撑什么？", "spec": {"nodes": [], "edges": []}})
    assert r.status_code == 200, r.text
    j = r.json()
    # 无论 LLM 是否可达，结构完整、不编造：不可达时 available=False + reason
    assert "available" in j and "answer" in j and "fallback" in j
    if not j["available"]:
        assert j["reason"] and j["answer"] == ""


def test_selftest_includes_assistant():
    j = client.get("/api/v1/ontology/modelling/selftest").json()
    assert j["assistant"]["pass"] is True


# ---------------------------------------------------------------- 清洗规则 DSL
def test_clean_rules_dedup_example_parses():
    rep = CR.validate_clean_rules(CR.CLEAN_RULE_EXAMPLES)
    assert rep["ok"] is True, rep["errors"]
    dedup = [r for r in rep["rules"] if r["op"] == "dedup"][0]
    assert dedup["fields"] == ["employee_no"]
    assert dedup["options"]["keep"] == "first"


def test_clean_rules_bad_dedup_option_fails():
    rep = CR.validate_clean_rules("dedup(employee_no, keep=middle)")
    assert rep["ok"] is False
    assert any("keep" in e["message"] for e in rep["errors"])


def test_clean_rules_legacy_is_recorded_not_machine_checked():
    rep = CR.validate_clean_rules("去前后空格,空值剔除")
    assert rep["ok"] is True and rep["structured"] == 0
    assert len(rep["legacy_lines"]) == 1


def test_clean_rules_unknown_field_detected_when_columns_given():
    rep = CR.validate_clean_rules("dedup(emp_no, keep=first)", known_fields=["employee_no"])
    assert rep["ok"] is True  # 语法本身没问题
    assert rep["unknown_fields"] and rep["unknown_fields"][0]["field"] == "emp_no"


def test_structure_blocks_bad_clean_rules():
    spec = {"nodes": [
        {"id": "p", "kind": "profiler", "fields": {"name": "清洗", "clean_rules": "dedup(x, keep=nope)"}},
        {"id": "v", "kind": "validation", "fields": {"name": "门禁"}},
    ], "edges": []}
    items = V.check_structure(spec)
    assert any(i["code"] == "CLEAN_RULE_SYNTAX" and i["level"] == "fail" for i in items)
