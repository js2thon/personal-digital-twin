"""CQ V2.0 回归测试（MemoryStore / 假 DB，不连库、不改动 V1.2 数据）。

守住的边界：
- 三类指标分开：质量低不因答不出而扣分，答不出只记知识缺口；
- 硬门槛优先于总分；机器不能写 human_approved；
- 标准答案不由 LLM 自生成自验证；
- Query Plan 不许携带任意 SQL/Cypher，且必须经过权限与复杂度校验；
- conflict / restricted 是独立状态，源/权限缺口独立于数据缺失；
- 决策层只给建议，人工确认前不得授权执行。
"""
from __future__ import annotations

import copy

import pytest

from app.modules.ontology import cq_v2
from app.modules.ontology.cq_v2 import (capability as cap, decision as dec,
                                        engine as eng, mapping as mp, models as m,
                                        planner as pl, quality as q, registry as reg)
from app.modules.ontology.cq_v2.service import CQEngine
from app.modules.ontology.cq_v2.store import MemoryStore, ddl_statements


@pytest.fixture
def eng_():
    e = CQEngine(store=MemoryStore())
    e.bootstrap(auto_admit_actor="业务负责人")
    return e


def _cq_by_code(e, code):
    return next(c for c in e.cqs() if c["code"] == code)


# ---------------------------------------------------------------- P0 建模
def test_bootstrap_builds_four_layers(eng_):
    b = eng_.bootstrap()
    assert b["skipped"] is True                     # 不覆盖已有数据
    assert len(eng_.capabilities()) == 5
    assert len(eng_.cqs()) == 16
    assert len(eng_.store.all("capability_cqs")) == 16
    assert len(eng_.requirements()) >= 60
    assert len(eng_.instances()) == 3
    layers = eng_.four_layers()
    assert [x["name"] for x in layers["layers"]] == [
        "Business Capability", "Competency Questions",
        "Ontology Requirements", "Knowledge & Evidence"]


def test_seed_quality_scores_and_admission(eng_):
    admitted = [c for c in eng_.cqs() if q.admitted(c)]
    assert [c["code"] for c in admitted] == [
        "CQ-001", "CQ-002", "CQ-005", "CQ-006", "CQ-008", "CQ-014"]
    assert _cq_by_code(eng_, "CQ-010")["quality_score"] == 51.25
    assert _cq_by_code(eng_, "CQ-010")["hard_gates"]["passed"] is False
    assert _cq_by_code(eng_, "CQ-001")["quality_score"] == 95


def test_score_formula_and_grade():
    assert q.score_dimensions({k: 4 for k in m.QUALITY_DIMENSIONS}) == (100.0, "优秀")
    assert q.score_dimensions(dict(zip(m.QUALITY_DIMENSIONS,
                                      (4, 4, 3, 4, 4, 4)))) == (95.0, "优秀")
    assert q.score_dimensions({k: 0 for k in m.QUALITY_DIMENSIONS}) == (0.0, "不合格")


def test_hard_gates_override_total_score():
    dims = dict(zip(m.QUALITY_DIMENSIONS, (4, 2, 4, 4, 4, 4)))  # 95 分但明确性不足
    gates = q.hard_gates(dims, 95)
    assert gates["passed"] is False
    assert "clarity_ge_3" in gates["failed"]
    assert gates["target_not_zero"] and gates["verifiability_ge_2"]


def test_four_gates_reject_vague_question():
    cq = m.new_cq(question="公司经营情况怎么样？", intent="lookup",
                  subject_type="OrganizationUnit", answer_shape="list")
    out = q.four_gates(cq)
    assert out["passed"] is False
    assert out["stages"][0]["stage"] == "semantic"
    assert out["suggestions"]


def test_gold_answer_source_whitelist():
    cq = m.new_cq(question_template="{person} 的直属上级是谁？")
    inst = m.new_instance(cq_id=cq["id"], instance_code="T1",
                          params={"person": "张三"},
                          expected_answer={"value": "某某乙"},
                          acceptance_criteria={"match": "exact"},
                          source_type="llm_draft_unverified", status="active")
    ok, reason = q.validate_instance(inst, cq)
    assert not ok and "LLM 草稿" in reason
    inst["source_type"] = "human_annotation"
    inst["annotated_by"] = "业务负责人"
    assert q.validate_instance(inst, cq)[0] is True


def test_parameterized_template_rendering():
    tpl = "{person} 在 {as_of} 的直属上级是谁？"
    assert m.unresolved_params(tpl) == ["as_of", "person"]
    assert "张三" in m.render_template(tpl, {"person": "张三", "as_of": "2025-12"})
    inst = m.new_instance(params={"person": "张三"},
                          expected_answer={"value": "x"},
                          acceptance_criteria={"match": "exact"},
                          source_type="human_annotation", status="active",
                          annotated_by="owner")
    ok, reason = q.validate_instance(inst, m.new_cq(question_template=tpl))
    assert not ok and "未绑定" in reason


def test_admit_requires_human_signature(eng_):
    cq = _cq_by_code(eng_, "CQ-001")
    with pytest.raises(ValueError):
        q.admit(cq, {"machine_passed": True, "hard_gates": {"passed": True}}, "llm")
    out = eng_.admit_cq(cq["id"], "业务负责人")
    assert out["review_status"] == "human_approved"


def test_capability_many_to_many(eng_):
    cq = _cq_by_code(eng_, "CQ-001")
    other = next(c for c in eng_.capabilities() if c["code"] == "CAP-REL")
    eng_.link_cq(other["id"], cq["id"], 0.5)
    linked = [l for l in eng_.store.all("capability_cqs") if l["cq_id"] == cq["id"]]
    assert len(linked) == 2
    rows = cap.capability_coverage(eng_.capabilities(),
                                   eng_.store.all("capability_cqs"),
                                   {cq["id"]: "answered"})
    assert all(r["cq_count"] >= 0 for r in rows)


def test_cq_version_and_three_layer_states(eng_):
    cq = _cq_by_code(eng_, "CQ-001")
    patch = m.transition(cq, "ontology", "published")
    assert patch["layer_states"]["ontology"] == "published"
    assert patch["version"] == cq["version"] + 1
    advanced = {**cq, "layer_states": {**cq["layer_states"],
                                      "business": "approved"}}
    with pytest.raises(ValueError):
        m.transition(advanced, "business", "proposed")  # 不允许倒退


# ---------------------------------------------------------------- P0/P1 Schema 治理
def test_registry_diff_migration_rollback_and_pin(eng_):
    s = copy.deepcopy(eng_.registry.current())
    s["schema_version"] = "v2.1.0"
    s["relation_types"]["applies_to"] = {
        "label": "约束", "source": "Policy", "target": "OrganizationUnit",
        "cardinality": "one_to_many"}
    pub = eng_.registry_publish(s)
    assert pub["compatibility"] == "compatible"
    assert "applies_to" in pub["diff"]["relation_types"]["added"]
    assert pub["migration_plan"]
    s2 = copy.deepcopy(s)
    s2["schema_version"] = "v2.2.0"
    del s2["relation_types"]["reports_to"]
    assert eng_.registry_publish(s2)["compatibility"] == "breaking"
    eng_.registry_pin(_cq_by_code(eng_, "CQ-001")["id"], "v2.0.0")
    assert eng_.registry.pins
    assert eng_.registry_rollback("v2.0.0")["rolled_back_to"] == "v2.0.0"


def test_linkml_single_source_export(eng_):
    yaml = eng_.registry_linkml()
    assert "classes:" in yaml and "Person:" in yaml
    assert "relation_types:" in yaml and "reports_to:" in yaml


def test_mapping_six_operations():
    r = reg.SchemaRegistry(reg.default_schema())
    # 注入一个以业务叫法命名的重复类型，触发 merge 判定
    r.current()["entity_types"]["事业部"] = {"label": "事业部", "aliases": []}
    found = {
        "部门": mp.map_concept("部门", r)["operation"],
        "项目经理": mp.map_concept("项目经理", r)["operation"],
        "事业部": mp.map_concept("事业部", r)["operation"],
        "对象": mp.map_concept("对象", r)["operation"],
        "量子态": mp.map_concept("量子态", r)["operation"],
        "Org": mp.map_concept("Org", r)["operation"],
    }
    assert found == {"部门": "reuse", "项目经理": "extend", "事业部": "merge",
                     "对象": "split", "量子态": "create", "Org": "deprecate"}


def test_create_proposal_requires_cq_basis_and_examples(eng_):
    p = eng_.mapping_propose("量子态", examples=["例1", "例2", "例3"])
    assert p["operation"] == "create"
    assert p["necessity"]["allowed"] is False        # 缺 CQ 依据
    with pytest.raises(ValueError):
        eng_.proposal_approve(p["id"], "审核人")
    p2 = eng_.mapping_propose("量子态", cq_id=_cq_by_code(eng_, "CQ-001")["id"],
                              examples=["例1", "例2", "例3"])
    assert eng_.proposal_approve(p2["id"], "审核人")["status"] == "approved"


# ---------------------------------------------------------------- P1 Query Plan
def test_plan_ast_is_structured_and_versioned(eng_):
    cq = _cq_by_code(eng_, "CQ-001")
    plan = eng_.build_plan(cq["id"])
    assert plan["validated"] is True
    assert plan["checksum"] and plan["plan_version"].startswith("QP-")
    assert all(s["op"] in m.PLAN_OPS for s in plan["steps"])
    assert plan["permissions"]["tenant_scoped"] is True
    assert "retrieval" in plan["executor_chain"]      # 检索是正式执行器
    assert "fact" in plan["executor_chain"] and "graph" in plan["executor_chain"]


def test_plan_rejects_arbitrary_sql_and_missing_tenant(eng_):
    plan = eng_.build_plan(_cq_by_code(eng_, "CQ-001")["id"])
    bad = {**plan, "raw_sql": "DROP TABLE knowledge.facts"}
    assert eng_.validate_plan(bad, {"tenant_id": "t1", "scopes": []})["validated"] is False
    assert eng_.validate_plan(plan, {})["validated"] is False
    assert eng_.validate_plan({**plan, "validated": True, "validation_notes": []},
                              {"tenant_id": "t1", "scopes": []})["validated"] is True


def test_plan_complexity_and_hops_bounds(eng_):
    plan = eng_.build_plan(_cq_by_code(eng_, "CQ-001")["id"])
    assert max([s.get("max_hops", 0) for s in plan["steps"]]) <= m.MAX_HOPS
    assert len(plan["steps"]) <= m.MAX_PLAN_STEPS
    assert any(s["op"] == "validate" for s in plan["steps"])


# ---------------------------------------------------------------- P1 执行与覆盖
def test_five_dimensions_and_five_states(eng_):
    r = eng_.coverage_run()
    assert r["total"] == 6                            # 分母只含质量准入通过的 CQ
    assert set(r["items"][0]["dimensions"]) == set(m.COVERAGE_DIMENSIONS)
    states = {k: v for k, v in r["states"].items() if v}
    assert "conflict" in states and "restricted" in states
    assert "answered" in states or "partial" in states


def test_answerability_does_not_change_quality(eng_):
    cq = _cq_by_code(eng_, "CQ-011")                  # 当前答不了但有业务价值
    before = cq["quality_score"]
    result = eng_.run(cq["id"])
    assert result["state"] in m.ANSWER_STATES
    assert eng_.get_cq(cq["id"])["quality_score"] == before


def test_conflict_and_restricted_are_independent_states(eng_):
    r = eng_.run(_cq_by_code(eng_, "CQ-008")["id"])   # has_role 事实有冲突
    assert r["state"] == "conflict"
    assert r["validation"]["has_conflict"] is True
    r2 = eng_.run(_cq_by_code(eng_, "CQ-005")["id"])  # 项目负责人事实受限
    assert r2["state"] == "restricted"
    assert r2["validation"]["has_restricted"] is True
    r3 = eng_.run(_cq_by_code(eng_, "CQ-001")["id"])
    assert r3["state"] != "restricted"                # 不因全库某条受限数据误判


def test_six_gap_kinds_and_priority_formula(eng_):
    r = eng_.coverage_run()
    assert "source_access" in {k for k, v in r["gap_kinds"].items() if v}
    for g in eng_.gaps():
        assert g["kind"] in m.GAP_KINDS
        assert g["gap_priority"] == round(
            g["business_impact"] * g["cq_weight"] * g["severity"], 2)
    assert r["gap_kinds"]["source_access"] < 20       # 只报本 CQ 声明的数据源


def test_weighted_coverage_and_layered_gates():
    items = [
        {"state": "answered", "cq_weight": 3.0, "risk_level": "high", "priority": 1,
         "dimensions": {"evidence": 1.0}},
        {"state": "partial", "cq_weight": 1.0, "risk_level": "normal", "priority": 3,
         "dimensions": {"evidence": 0.8}},
    ]
    w = eng.weighted_coverage(items)
    assert w["coverage"] == 0.75                      # 3 / (3+1)
    gates = eng.layered_gates(items)
    assert gates["core_cq_coverage"] == 1.0
    # 核心能力达标也不能替普通 CQ 放行：分层门禁如实拦截
    assert gates["passed"] is False
    good = [dict(items[0]), dict(items[1], state="answered")]
    assert eng.layered_gates(good)["passed"] is True


def test_execution_audit_has_full_provenance(eng_):
    eng_.run(_cq_by_code(eng_, "CQ-001")["id"])
    rec = eng_.store.all("cq_execution_runs")[-1]
    assert rec["plan_version"] and rec["schema_version"]
    assert rec["data_snapshot_id"] and rec["tenant_id"] and rec["actor"]
    assert "evidence_refs" in rec


def test_three_metrics_are_separated_columns(eng_):
    eng_.coverage_run()
    cid = _cq_by_code(eng_, "CQ-001")["id"]
    eng_.store.insert("cq_evaluation_runs", {
        "id": m.uid(), "cq_id": cid, "accuracy": 0.92, "evidence_rate": 0.95,
        "run_at": m.now()})
    row = next(x for x in eng_.metrics() if x["id"] == cid)
    assert row["quality_score"] == 95                 # 定义质量
    assert row["answerability_state"] in m.ANSWER_STATES   # 当前能否回答
    assert row["answer_accuracy"] == 0.92            # 答得对不对
    assert row["admitted"] is True


# ---------------------------------------------------------------- P2 决策与演进
def test_decision_layer_only_suggests_until_human_approval(eng_):
    cq = eng_.create_cq({
        "code": "CQ-D01", "question": "某采购申请是否应该通过？", "intent": "decision",
        "subject_type": "Project", "answer_shape": "decision", "risk_level": "high",
        "business_domain": "采购管理", "business_purpose": "采购审批分流",
        "capability_ids": [], "scope_constraints": {}, "temporal_constraints": {},
        "acceptance_criteria": {}, "evidence_requirements": {}})
    d = eng_.decision_run(cq["id"], {"amount": 30000,
                                     "evidence_summary": {"valid": 2, "total": 2}},
                          [{"code": "R1", "when": {"amount": {"op": "<=",
                                                               "value": 50000}},
                            "then": "approve", "weight": 0.6}])
    assert d["approval_status"] == "pending"
    assert d["suggestion"] == "approve" and d["rationale"]
    assert "high_risk_decision" in d["risk_flags"]
    with pytest.raises(ValueError):
        eng_.decision_approve(d["id"], "审批人", authorized_execution=True)
    assert eng_.decision_approve(d["id"], "审批人")["approval_status"] == "approved"


def test_decision_conflict_blocks_approval():
    d = dec.evaluate(m.new_cq(intent="decision"), {"has_conflict": True}, [])
    d["risk_flags"].append("fact_conflict")
    with pytest.raises(ValueError):
        dec.approve(d, "审批人")


def test_portfolio_quality_checks(eng_):
    p = eng_.portfolio()
    assert p["admitted_total"] == 6
    assert "intent_entropy" in p and "difficulty_distribution" in p
    assert p["coverage_matrix"]["note"].startswith("规划矩阵")
    assert isinstance(p["findings"], list)


def test_evolution_loop_seven_steps_with_audit(eng_):
    out = eng_.evolution_cycle()
    assert out["status"] == "completed"
    assert len(out["steps"]) == 7
    assert len(out["audit"]) == 7
    assert out["baseline_after"] and out["gap_summary"]
    assert eng_.store.all("evolution_runs")


# ---------------------------------------------------------------- 不破坏既有系统
def test_legacy_cq_module_unchanged():
    from app.modules.ontology import cq as legacy
    assert len(legacy.SEEDS) == 16
    assert len(legacy.INTENTS) == 6
    assert legacy.ANSWER_THRESHOLD == 0.6


def test_ddl_is_idempotent_and_covers_all_layers():
    stmts = ddl_statements()
    assert len(stmts) >= 20
    assert all("IF NOT EXISTS" in s or s.startswith("ALTER TABLE")
               or s.startswith("DO") or s.startswith("CREATE OR REPLACE")
               or s.startswith("COMMENT") for s in stmts)


def test_api_routes_importable():
    pytest.importorskip("fastapi")
    from app.api.v1 import cq_v2 as api
    paths = {r.path for r in api.router.routes}
    assert "/ontology/v2/meta" in paths
    assert "/ontology/v2/coverage" in paths
    assert "/ontology/v2/evolution/cycle" in paths


def test_app_main_registers_v2_routes():
    """装配层回归：必须验证 app.main 真的 include_router 了 cq_v2。

    只检查子 router 的裸 path（上一个测试）无法发现 main.py 漏注册，更无法发现
    「运行进程加载旧代码 → 静态页 200 / V2 全组 404」这类运维态漂移。同时覆盖
    /api/v1 前缀、identity_gate 身份闸门、以及 /console 静态挂载未遮蔽 API。
    不用 TestClient 的 context manager，避免触发 startup 连库。
    """
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from app.main import app

    spec_paths = app.openapi()["paths"]
    v2 = [p for p in spec_paths if "/api/v1/ontology/v2/" in p]
    assert len(v2) >= 30
    for path in ("/api/v1/ontology/v2/meta",
                 "/api/v1/ontology/v2/coverage",
                 "/api/v1/ontology/v2/evolution/cycle"):
        assert path in spec_paths

    # 回环 client 满足 auth.actor_from_request，否则非回环会被身份闸门 401
    client = TestClient(app, client=("127.0.0.1", 12345))
    meta = client.get("/api/v1/ontology/v2/meta")
    assert meta.status_code == 200
    assert meta.json()["version"] == "V2.0"
    # V1.2 仍可用，且两组路由互不遮蔽
    assert client.get("/api/v1/ontology/cq/meta").status_code == 200
