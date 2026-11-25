"""图 ↔ 本体同步的规则测试（假 DB，不连库）。

这一层最容易出错的地方不是 SQL，而是**规则漏了一条方向**：
上一版只做「事实→边」，于是撤销事实边还在、删掉投影边会被对账复活、
人工建的边本体里根本查不到。下面每个用例对应一条规则。
"""
import json

import pytest

from app.modules.ontology import sync


class FakeDB:
    """记录调用、按脚本返回行。够验证「调了什么」而不需要真库。"""

    def __init__(self, one=None, all_=None):
        self._one = list(one or [])
        self._all = list(all_ or [])
        self.calls = []

    def fetch_one(self, sql, binds=None):
        self.calls.append(("fetch_one", sql, binds))
        return self._one.pop(0) if self._one else None

    def fetch_all(self, sql, binds=None):
        self.calls.append(("fetch_all", sql, binds))
        return self._all.pop(0) if self._all else []

    def execute(self, sql, binds=None):
        self.calls.append(("execute", sql, binds))

    def insert_returning_id(self, table, values):
        self.calls.append(("insert", table, values))
        return "11111111-1111-1111-1111-111111111111"

    def sql_of(self, kind):
        return [c[1] for c in self.calls if c[0] == kind]


@pytest.fixture
def fake(monkeypatch):
    def _make(one=None, all_=None):
        f = FakeDB(one, all_)
        monkeypatch.setattr(sync, "db", f)
        return f
    return _make


# ---------------------------------------------------------------- R2 事实撤销
def test_remove_fact_edge_targets_that_fact_only(fake):
    f = fake(one=[{"n": 1}])
    out = sync.remove_fact_edge("f-1", "fact_withdrawn")
    assert out == {"removed": 1, "fact_id": "f-1", "reason": "fact_withdrawn"}
    sql = f.sql_of("fetch_one")[0]
    assert "properties->>'fact_id' = :fid" in sql
    assert "status = 'active'" in sql
    # 软停用要留痕：谁停的、为什么
    mark = json.loads([b for c in f.calls if c[0] == "fetch_one" for b in [c[2]["mark"]]][0])
    assert mark["removed_by"] == sync.REMOVED_BY
    assert mark["removed_reason"] == "fact_withdrawn"


def test_sync_fact_withdrawn_fact_removes_edge_not_creates(fake):
    f = fake(one=[{"id": "f-1", "status": "withdrawn"}, {"n": 1}])
    out = sync.sync_fact("f-1")
    assert out["removed"] == 1
    assert not f.sql_of("insert")            # 绝不能再建边


def test_sync_fact_missing_fact_removes_edge(fake):
    f = fake(one=[None, {"n": 2}])
    out = sync.sync_fact("gone")
    assert out["removed"] == 2 and out["reason"] == "fact_deleted"


# ---------------------------------------------------------------- R1 事实建边
def test_upsert_fact_edge_reuses_existing_and_backfills_fact_id(fake, monkeypatch):
    fact = {"id": "f-2", "claim_id": None, "subject_id": "a",
            "predicate": "reports_to", "object_value": {"value": "某某乙"},
            "status": "active"}
    monkeypatch.setattr(sync, "_active_entities", lambda: [
        {"id": "a", "canonical_name": "某某", "entity_type": "Person", "aliases": []},
        {"id": "b", "canonical_name": "某某乙", "entity_type": "Person", "aliases": []},
    ])
    f = fake(one=[fact, {"id": "rel-1"}])
    out = sync.upsert_fact_edge("f-2")
    assert out["created"] is False and out["reason"] == "already_exists"
    upd = f.sql_of("execute")[0]
    # 已存在的边要补 fact_id，否则撤销时对不上账
    assert "fact_id" in upd and "COALESCE(properties->>'fact_id', '')" in upd


def test_upsert_fact_edge_skips_type_violation(fake, monkeypatch):
    fact = {"id": "f-3", "claim_id": None, "subject_id": "p",
            "predicate": "has_role", "object_value": {"value": "AI填单项目"},
            "status": "active"}
    monkeypatch.setattr(sync, "_active_entities", lambda: [
        {"id": "p", "canonical_name": "某某", "entity_type": "Person", "aliases": []},
        {"id": "pr", "canonical_name": "AI填单项目", "entity_type": "Project", "aliases": []},
    ])
    f = fake(one=[fact])
    out = sync.upsert_fact_edge("f-3")
    assert out["created"] is False and out["reason"] == "not_projectable"
    assert not f.sql_of("insert")


def test_upsert_fact_edge_prefers_entity_id_over_name(fake, monkeypatch):
    # 名字改过：宾语里的 name 是旧名，但 entity_id 仍指向同一实体
    fact = {"id": "f-4", "claim_id": None, "subject_id": "a",
            "predicate": "reports_to",
            "object_value": {"entity_id": "b", "name": "旧名字（已改）"},
            "status": "active"}
    monkeypatch.setattr(sync, "_active_entities", lambda: [
        {"id": "a", "canonical_name": "某某", "entity_type": "Person", "aliases": []},
        {"id": "b", "canonical_name": "某某乙", "entity_type": "Person", "aliases": []},
    ])
    # fetch_one 顺序：事实、查重无命中、INSERT RETURNING 新边 ID
    f = fake(one=[fact, None, {"id": "rel-new"}])
    out = sync.upsert_fact_edge("f-4")
    assert out["created"] is True
    assert out["target"] == "某某乙"


# ---------------------------------------------------------------- R3 人工边 → 事实
def test_ensure_backing_fact_uses_entity_id_not_name(fake):
    rel = {"id": "rel-9", "source_entity_id": "a", "target_entity_id": "b",
           "relation_type": "reports_to", "properties": {"origin": "manual"},
           "source_name": "某某", "target_name": "某某乙"}
    f = fake(one=[rel])
    out = sync.ensure_backing_fact("rel-9")
    assert out["created"] is True
    table, values = [(c[1], c[2]) for c in f.calls if c[0] == "insert"][0]
    assert table == "knowledge.facts"
    obj = json.loads(values["object_value"])
    assert obj["entity_id"] == "b" and obj["name"] == "某某乙"
    assert json.loads(values["source_ref"])["origin"] == sync.MANUAL_ORIGIN
    # 边侧要回记 fact_id，删边时才能反过来撤事实
    upd_binds = [c[2] for c in f.calls if c[0] == "execute"][0]
    assert json.loads(upd_binds["p"])["fact_id"] == "11111111-1111-1111-1111-111111111111"


def test_ensure_backing_fact_idempotent(fake):
    rel = {"id": "rel-9", "source_entity_id": "a", "target_entity_id": "b",
           "relation_type": "reports_to", "properties": {"fact_id": "f-9"},
           "source_name": "赵根", "target_name": "某某乙"}
    f = fake(one=[rel])
    out = sync.ensure_backing_fact("rel-9")
    assert out == {"created": False, "reason": "already_backed", "fact_id": "f-9"}
    assert not f.sql_of("insert")


# ---------------------------------------------------------------- R4 删边 → 撤事实
def test_on_relation_removed_withdraws_backing_fact(fake, monkeypatch):
    seen = {}
    monkeypatch.setattr(sync, "withdraw_backing_fact",
                        lambda fid, actor="owner", reason="": seen.update(
                            {"fid": fid, "reason": reason}) or {"withdrawn": True})
    out = sync.on_relation_removed({"id": "rel-1", "properties": {"fact_id": "f-1"}})
    assert out["withdrawn"] is True
    assert seen == {"fid": "f-1", "reason": "relation_removed"}


def test_on_relation_removed_without_fact_is_noop(fake, monkeypatch):
    f = fake(one=[None])
    out = sync.on_relation_removed({"id": "rel-2", "properties": {}})
    assert out == {"withdrawn": False, "reason": "no_backing_fact"}


# ---------------------------------------------------------------- 实体变更
def test_sync_entity_removes_edges_when_entity_inactive(fake):
    f = fake(one=[{"id": "e1", "canonical_name": "AI填单项目", "entity_type": "Project",
                   "status": "merged"}, {"n": 3}])
    out = sync.sync_entity("e1")
    assert out["edges_removed"] == 3
    soft = f.sql_of("fetch_one")[-1]          # 第二条才是软停用边的 UPDATE
    assert "source_entity_id = CAST(:e AS uuid)" in soft and "target_entity_id" in soft


def test_sync_entity_reprojects_its_facts(fake, monkeypatch):
    monkeypatch.setattr(sync, "sync_fact", lambda fid: {"created": True})
    f = fake(one=[{"id": "e1", "canonical_name": "某某乙", "entity_type": "Person",
                   "status": "active"}],
             all_=[[{"id": "f1"}, {"id": "f2"}]])
    out = sync.sync_entity("e1")
    assert out["facts"] == 2 and out["edges_created"] == 2


# ---------------------------------------------------------------- 对账视图
def test_orphans_reports_three_buckets(fake):
    fake(all_=[
        [{"id": "r1", "relation_type": "reports_to", "source_name": "某某",
          "target_name": "某某乙", "fact_status": "withdrawn"}],
        [{"id": "r2", "relation_type": "works_at", "source_name": "某某",
          "target_name": "公司", "source_status": "active", "target_status": "merged"}],
        [{"id": "r3", "relation_type": "participated_in", "source_name": "某某",
          "target_name": "AI填单项目"}],
    ])
    out = sync.orphans()
    assert out["counts"] == {"stale_projection": 1, "dangling_entity": 1,
                             "unbacked_manual": 1}
    assert out["stale_projection"][0]["relation_id"] == "r1"


def test_sync_all_dry_run_never_writes(fake, monkeypatch):
    from app.modules.ontology import workbench

    monkeypatch.setattr(workbench, "reconcile_relations", lambda dry_run=True: {
        "to_add": 2, "to_remove": 1, "plan": {"existing": 5},
        "existing": {"manual": 1, "legacy": 2}})
    f = fake(all_=[[], [], []])
    out = sync.sync_all(dry_run=True)
    assert out["dry_run"] is True and out["reconcile"]["to_add"] == 2
    assert not f.sql_of("execute")           # 预览阶段一条都不写
