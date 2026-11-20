"""P0 增强回归：影响分析（P0-2）+ 状态机（P0-1）。

纯函数层默认全跑（零库可测）；API 自检端点用 TestClient（不写库）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ontology.semantic import impact, state_machine
from app.modules.ontology.semantic import schema_def as SD

# 模拟本机地址，绕过「非本机需要 API key」闸门（与 test_ontology_semantic.py 同款）
client = TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ---------------------------------------------------------------- P0-2 影响分析
def test_impact_selfcheck_pass():
    r = impact.self_check()
    assert r["pass"] is True
    assert r["total"] == 2
    assert "赵六" in r["names"] and "王五" in r["names"]


def test_impact_downstream_chain_depth():
    nodes = [{"id": x, "canonical_name": x} for x in ("a", "b", "c", "d")]
    edges = [
        {"source_id": "b", "target_id": "a", "relation_type": "reports_to"},
        {"source_id": "c", "target_id": "b", "relation_type": "reports_to"},
        {"source_id": "d", "target_id": "c", "relation_type": "reports_to"},
    ]
    r = impact.analyze_impact("a", nodes, edges, max_depth=2)
    assert r["total"] == 2                      # b(1跳) + c(2跳)，d 超出深度
    assert r["by_depth"] == {"1": 1, "2": 1}
    assert r["path_query_accuracy"]["available"] is False


def test_impact_direction_downstream_only():
    nodes = [{"id": x, "canonical_name": x} for x in ("a", "b")]
    edges = [{"source_id": "a", "target_id": "b", "relation_type": "is_project_manager_of"}]
    # downstream: a→b 命中
    r1 = impact.analyze_impact("a", nodes, edges, max_depth=2)
    assert r1["total"] == 1 and r1["affected"][0]["id"] == "b"
    # 从 b 出发不应反向命中 a（downstream 只正向）
    r2 = impact.analyze_impact("b", nodes, edges, max_depth=2)
    assert r2["total"] == 0


def test_impact_relation_filter():
    nodes = [{"id": x, "canonical_name": x} for x in ("a", "b", "c")]
    edges = [
        {"source_id": "b", "target_id": "a", "relation_type": "reports_to"},
        {"source_id": "c", "target_id": "a", "relation_type": "collaborates_with"},
    ]
    r = impact.analyze_impact("a", nodes, edges, max_depth=2,
                              relation_types=["reports_to"])
    assert r["total"] == 1 and r["affected"][0]["id"] == "b"


def test_impact_self_loop_and_cap():
    nodes = [{"id": "a", "canonical_name": "a"}]
    edges = [{"source_id": "a", "target_id": "a", "relation_type": "reports_to"}]
    r = impact.analyze_impact("a", nodes, edges, max_depth=3)
    assert r["total"] == 0  # 自环不计


# ---------------------------------------------------------------- P0-1 状态机
def test_state_machine_selfcheck_pass():
    r = state_machine.self_check()
    assert r["pass"] is True
    assert r["legal_pass"] is True
    assert r["illegal_jump_blocked"] is True
    assert r["invalid_value_blocked"] is True


def test_state_machine_legal_and_illegal_transition():
    s = SD.default_semantic_schema()
    schema = SD.to_schema_dict(s)
    ok = state_machine.validate_transition(schema, "Project", "approval_status",
                                           "approved", old_value="pending", has_old=True)
    assert ok["applicable"] is True and ok["blocking"] is False
    bad = state_machine.validate_transition(schema, "Project", "approval_status",
                                            "archived", old_value="pending", has_old=True)
    assert bad["blocking"] is True
    assert any(v["constraint"] == "state.illegal_transition" for v in bad["violations"])


def test_state_machine_inherited_from_parent():
    schema = {"entity_types": {
        "Base": {"name": "Base", "parent": "", "attributes": [
            {"name": "approval_status", "state_machine": {
                "states": ["a", "b"], "transitions": {"a": ["b"]}, "initial": "a"}}]},
        "Child": {"name": "Child", "parent": "Base", "attributes": []},
    }}
    sm = state_machine.find_state_machine(schema, "Child", "approval_status")
    assert sm is not None and sm["initial"] == "a"


def test_state_machine_new_instance_only_checks_value():
    s = SD.default_semantic_schema()
    schema = SD.to_schema_dict(s)
    r = state_machine.validate_instance_transitions(
        schema, "Project", {"approval_status": "approved"}, previous=None)
    assert r["blocking"] is False and len(r["checked"]) == 1


# ---------------------------------------------------------------- API 自检
def test_selftest_endpoint():
    r = client.get("/api/v1/ontology/semantic/selftest")
    assert r.status_code == 200
    d = r.json()
    assert d["impact"]["pass"] is True
    assert d["state_machine"]["pass"] is True


def test_state_machine_preview_endpoint():
    r = client.post("/api/v1/ontology/semantic/state-machine/preview", json={
        "type_name": "Project", "attribute": "approval_status",
        "new_value": "archived", "old_value": "pending", "has_old": True,
    })
    assert r.status_code == 200
    assert r.json()["blocking"] is True
