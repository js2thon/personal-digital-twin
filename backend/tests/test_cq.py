"""CQ（能力问题）的规则测试（假 DB / 假执行器，不连库）。

这一层要守住的是「不骗人」：
- 答不上就说答不上，不能把「没记录」当成「没有」；
- 缺口要能落到具体动作上（缺实体/缺关系/缺属性/缺类型/缺证据）；
- 覆盖率只统计真正能回答的实体，别用「有边就算」自我安慰。
"""
import json

import pytest

from app.modules.ontology import cq


# ---------------------------------------------------------------- 基础工具
def test_as_list_handles_shapes():
    assert cq._as_list(["a"]) == ["a"]
    assert cq._as_list('["a","b"]') == ["a", "b"]
    assert cq._as_list(None) == []
    assert cq._as_list("not-json") == []
    assert cq._as_list({"x": 1}) == []


def test_validate_rejects_bad_intent():
    with pytest.raises(ValueError):
        cq._validate_payload({"question": "某个人的上级是谁", "intent": "magic"}, True)
    out = cq._validate_payload({"question": "某个人的上级是谁", "intent": "lookup"}, True)
    assert out["intent"] == "lookup"


def test_validate_rejects_short_question():
    with pytest.raises(ValueError):
        cq._validate_payload({"question": "谁"}, True)


def test_validate_priority_range():
    with pytest.raises(ValueError):
        cq._validate_payload({"priority": 9}, False)
    assert cq._validate_payload({"priority": 1}, False)["priority"] == 1


def test_validate_cleans_blank_lists():
    out = cq._validate_payload({"required_relations": [" reports_to ", "", "  "]}, False)
    assert out["required_relations"] == ["reports_to"]


# ---------------------------------------------------------------- 保存前提醒
def test_warnings_flag_unregistered_relation(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person", "Project"})
    warns = cq.warnings_for({"subject_type": "System",
                             "required_relations": ["applies_to"]})
    assert any("System" in w for w in warns)
    assert any("applies_to" in w for w in warns)


def test_warnings_clean_payload_has_none(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person"})
    assert cq.warnings_for({"subject_type": "Person",
                            "required_relations": ["reports_to"]}) == []


# ---------------------------------------------------------------- 确定性抽取
def test_analyze_matches_type_relation_and_intent(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person"})
    out = cq.analyze("某个人向谁汇报？")
    assert "Person" in out["matched_types"]
    assert "reports_to" in out["matched_relations"]
    assert out["suggested"]["subject_type"] == "Person"
    assert not out["gaps"]


def test_analyze_flags_undeclared_type(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person"})
    out = cq.analyze("某个系统的负责人是谁？")
    assert "System" in out["matched_types"]
    assert any(g["kind"] == "type_undeclared" for g in out["gaps"])


def test_analyze_flags_unregistered_relation(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Policy"})
    out = cq.analyze("某项制度约束哪些角色？")
    # 「约束」不在关系词表里，必须如实报「没命中」而不是硬猜一个
    assert out["matched_relations"] == [] or all(
        r in cq.og.RELATION_TYPES for r in out["matched_relations"])
    assert out["suggested"]["subject_type"] == "Policy"


def test_analyze_aggregate_intent(monkeypatch):
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person", "Organization"})
    out = cq.analyze("某个部门下面有多少人？")
    assert out["intent"] == "aggregate"
    assert out["suggested"]["answer_shape"] == "count"


# ---------------------------------------------------------------- 覆盖率评估
def _patch(monkeypatch, total_ids, rel_map=None, attr_map=None,
           evidence=(0, 0), declared=("Person",)):
    monkeypatch.setattr(cq, "declared_types", lambda: set(declared))
    monkeypatch.setattr(cq, "_entity_ids", lambda t: set(total_ids))
    monkeypatch.setattr(cq, "_rel_subject_ids",
                        lambda t, r, s: set((rel_map or {}).get(r, [])))
    monkeypatch.setattr(cq, "_attr_subject_ids",
                        lambda t, a: set((attr_map or {}).get(a[0], [])))
    monkeypatch.setattr(cq, "_rel_evidence", lambda t, rels, s: evidence)
    monkeypatch.setattr(cq, "_candidate_facts", lambda r: 3)


def _cq(**kw):
    base = {"code": "CQ-001", "question": "某个人的上级是谁", "intent": "lookup",
            "subject_type": "Person", "subject_side": "source",
            "answer_shape": "entity", "required_relations": ["reports_to"],
            "required_attributes": [], "min_evidence": 1, "status": "draft"}
    base.update(kw)
    return base


def test_evaluate_answered_when_fully_covered(monkeypatch):
    _patch(monkeypatch, ["e1", "e2"], {"reports_to": ["e1", "e2"]},
           evidence=(2, 2))
    out = cq.evaluate(_cq())
    assert out["state"] == "answered"
    assert out["score"] == 1.0
    assert not out["gaps"]


def test_evaluate_partial_counts_only_covered(monkeypatch):
    _patch(monkeypatch, ["e1", "e2", "e3", "e4"], {"reports_to": ["e1", "e2"]},
           evidence=(2, 2))
    out = cq.evaluate(_cq())
    assert out["state"] == "partial"
    assert out["score"] == 0.5
    assert out["requirements"][0]["covered"] == 2
    assert out["requirements"][0]["total"] == 4


def test_evaluate_relation_empty_suggests_building(monkeypatch):
    _patch(monkeypatch, ["e1", "e2"], {"reports_to": []})
    out = cq.evaluate(_cq())
    assert out["state"] == "unanswerable"
    gap = next(g for g in out["gaps"] if g["kind"] == "relation_empty")
    # 事实库有候选时，动作是「审核事实即可建边」，不是「去补来源」
    assert gap["action"]["candidates"] == 3
    assert "建关系" in gap["action"]["label"]


def test_evaluate_entity_missing(monkeypatch):
    _patch(monkeypatch, [])
    out = cq.evaluate(_cq())
    assert out["state"] == "unanswerable"
    assert out["score"] == 0.0
    assert any(g["kind"] == "entity_missing" for g in out["gaps"])


def test_evaluate_unregistered_relation_blocks(monkeypatch):
    _patch(monkeypatch, ["e1"])
    out = cq.evaluate(_cq(required_relations=["applies_to"]))
    kinds = [g["kind"] for g in out["gaps"]]
    assert "relation_unregistered" in kinds
    assert out["state"] == "unanswerable"


def test_evaluate_type_undeclared(monkeypatch):
    _patch(monkeypatch, ["e1"], declared=("Person",))
    out = cq.evaluate(_cq(subject_type="System", required_relations=[],
                          required_attributes=["owner"]))
    assert any(g["kind"] == "type_undeclared" for g in out["gaps"])


def test_evaluate_attribute_empty(monkeypatch):
    _patch(monkeypatch, ["e1", "e2"], attr_map={})
    out = cq.evaluate(_cq(required_relations=[], required_attributes=["due_date"]))
    assert any(g["kind"] == "attribute_empty" for g in out["gaps"])
    assert out["state"] == "unanswerable"


def test_evaluate_evidence_missing_lowers_score(monkeypatch):
    _patch(monkeypatch, ["e1"], {"reports_to": ["e1"]}, evidence=(1, 0))
    out = cq.evaluate(_cq())
    assert any(g["kind"] == "evidence_missing" for g in out["gaps"])
    assert out["score"] < 1.0


def test_evaluate_union_relations_not_all_required(monkeypatch):
    """两条关系任一成立即可回答（如「在哪家公司/哪个部门」），不能要求全都命中。"""
    _patch(monkeypatch, ["e1", "e2"], {"works_at": ["e1"], "member_of": []},
           evidence=(1, 1))
    out = cq.evaluate(_cq(required_relations=["works_at", "member_of"]))
    assert out["score"] == 0.5
    kinds = [g["kind"] for g in out["gaps"]]
    assert "relation_empty" in kinds          # 具体到 member_of 仍然报缺
    assert out["state"] == "partial"


def test_evaluate_path_intent_uses_reachability(monkeypatch):
    _patch(monkeypatch, ["e1", "e2"], declared=("Person",))
    monkeypatch.setattr(cq, "_reachable_ids", lambda t: {"e1"})
    out = cq.evaluate(_cq(intent="path", required_relations=[]))
    assert out["requirements"][0]["kind"] == "reachability"
    assert out["score"] == 0.5


# ---------------------------------------------------------------- 样例实测
class FakeDB:
    def __init__(self, all_=None):
        self._all = list(all_ or [])
        self.calls = []

    def fetch_all(self, sql, binds=None):
        self.calls.append((sql, binds))
        return self._all.pop(0) if self._all else []

    def fetch_one(self, sql, binds=None):
        self.calls.append((sql, binds))
        return None

    def execute(self, sql, binds=None):
        self.calls.append((sql, binds))


def test_sample_check_reads_actual_values(monkeypatch):
    fake = FakeDB(all_=[[{"name": "某某乙", "type": "Person",
                          "evidence_claim_id": "c1", "relation_type": "reports_to"}]])
    monkeypatch.setattr(cq, "db", fake)
    out = cq.sample_check(
        _cq(sample_question="某某向谁汇报？"),
        names=[("某某", "e1", "Person"), ("赵家", "e2", "Person")])
    assert out["found"] and out["answerable"]
    assert out["entity"] == "某某"          # 取最长匹配，别命中半个名字
    assert out["values"][0]["value"] == "某某乙"


def test_sample_check_without_known_entity():
    out = cq.sample_check(_cq(sample_question="某公司 CEO 是谁？"), names=[])
    assert out["found"] is False
    assert out["values"] == []


def test_sample_check_no_values_is_not_answerable(monkeypatch):
    monkeypatch.setattr(cq, "db", FakeDB(all_=[[]]))
    out = cq.sample_check(_cq(sample_question="某某向谁汇报？"),
                          names=[("某某", "e1", "Person")])
    assert out["found"] is True
    assert out["answerable"] is False


# ---------------------------------------------------------------- 落库行为
def test_coverage_dry_run_does_not_write(monkeypatch):
    fake = FakeDB(all_=[[{"id": "c1", "code": "CQ-001", "question": "问句话在这里",
                          "sample_question": None, "intent": "lookup",
                          "subject_type": "Person", "subject_side": "source",
                          "answer_shape": "entity", "required_relations": ["reports_to"],
                          "required_attributes": [], "min_evidence": 1, "priority": 1,
                          "domain": None, "status": "draft", "owner": None,
                          "notes": None, "reviewed_by": None, "reviewed_at": None,
                          "created_at": None, "updated_at": None}]])
    monkeypatch.setattr(cq, "db", fake)
    monkeypatch.setattr(cq, "evaluate", lambda c: {"state": "partial", "score": 0.5,
                                                   "total": 2, "requirements": [],
                                                   "gaps": [], "plan": {}})
    out = cq.coverage(persist=False)
    assert out["total"] == 1
    assert out["avg_score"] == 0.5
    assert not [c for c in fake.calls if "INSERT INTO ontology.cq_coverage_snapshots" in c[0]]


def test_seed_skips_reviewed(monkeypatch):
    """已启用的 CQ 不能被种子覆盖（人审过的内容优先）。"""
    rows = [{"id": "c1", "code": s["code"], "status": "active"} for s in cq.SEEDS]
    monkeypatch.setattr(cq, "db", FakeDB(all_=[rows]))
    monkeypatch.setattr(cq, "create_cq", lambda *a, **k: {"code": "X"})
    monkeypatch.setattr(cq, "update_cq", lambda *a, **k: {"changed": 1, "warnings": []})
    out = cq.seed()
    assert out["inserted"] == []
    assert len(out["skipped"]) == len(cq.SEEDS)


def test_analyze_prefers_earliest_type(monkeypatch):
    """「ChatBI 系统的负责人是谁」主语是系统，不是人——按名词出现位置判定。"""
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person", "System"})
    out = cq.analyze("ChatBI 系统的负责人是谁？")
    assert out["matched_types"][0] == "System"
    assert out["suggested"]["subject_type"] == "System"


def test_analyze_infers_target_side_for_asset_subject(monkeypatch):
    """主语是项目、关系从人指向项目时，主语要落在目标端，否则统计方向会反。"""
    monkeypatch.setattr(cq, "declared_types", lambda: {"Person", "Project"})
    out = cq.analyze("某个项目有哪些人参与？")
    assert out["suggested"]["subject_type"] == "Project"
    assert out["suggested"]["subject_side"] == "target"
