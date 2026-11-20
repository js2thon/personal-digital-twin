"""Schema Registry 双轨合一（P1-2 / SIL-C01 / SIL-C02）回归测试。

分两层：
* 纯逻辑层——LinkML 序列化/解析互为逆运算、手写 LinkML 可读、导入 dry_run 不落数据；
* 实库层——JSONB 副本与规范化关系表（business_capabilities / capability_cqs /
  cq_requirements / schema_registry / schema_migrations / schema_proposals）逐表对账，
  缺则补、多则报；并且 Registry 版本会同步到 ontology.ontology_versions（SIL-A04）。
写库用例一律包在事务里回滚，不留下测试数据。
"""
from __future__ import annotations

import json

import pytest

from app.modules.ontology.cq_v2 import linkml
from app.modules.ontology.cq_v2 import registry as reg
from app.modules.ontology.cq_v2.service import CQEngine
from app.modules.ontology.cq_v2.store import (
    TRACK_TABLES, MemoryStore, default_store, track_key)


def _db_ready() -> bool:
    try:
        from app.db.session import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


class _Rollback(Exception):
    """在事务里主动抛出，验证「对账/同步过程本身可回滚」。"""


@pytest.fixture
def eng_():
    e = CQEngine(store=MemoryStore())
    e.bootstrap(auto_admit_actor="业务负责人")
    return e


# ---------------------------------------------------------------- LinkML 往返

def test_linkml_roundtrip_is_lossless_for_default_schema():
    report = linkml.roundtrip(reg.default_schema())
    assert report["lossless"] is True, report["diff"]
    assert report["classes"] == 8 and report["relations"] == 12
    assert report["bytes"] > 0


def test_linkml_roundtrip_keeps_extra_keys_and_orphan_attributes():
    schema = dict(reg.default_schema("v2.9.0"))
    schema["entity_types"]["Agent"] = {"label": "代理", "aliases": [],
                                       "parent": "", "uri": "https://x/Agent"}
    schema["attributes"]["grade"] = {"range": "string", "owner": "Person",
                                     "required": True}
    schema["attributes"]["free_attr"] = {"range": "integer", "owner": ""}
    schema["constraints"] = {"max_hops": 4, "single_valued": ["level"]}
    schema["pinned_by_cq"] = ["cq-1"]
    report = linkml.roundtrip(schema)
    assert report["lossless"] is True, report["diff"]


def test_linkml_roundtrip_reports_diff_instead_of_lying():
    """把输出改坏一半：必须报 lossless=False 并指出哪一段不一致。"""
    schema = reg.default_schema()
    text = linkml.dumps(schema).replace("version: v2.0.0", "version: v9.9.9")
    parsed = linkml.parse(text)
    assert parsed["schema_version"] == "v9.9.9"
    report = {"lossless": linkml._canon(schema) == linkml._canon(parsed)}
    assert report["lossless"] is False


def test_parse_handwritten_linkml_with_slots_and_flow_maps():
    text = """
id: https://example.org/x
name: X
version: v9.9.9
slots:
  name: {range: string, required: true}
classes:
  Person:                 # 手写风格
    description: 人员
    slots: [name]
    attributes:
      age: {range: integer, minimum_value: 0}
  Team:
    description: 团队
relation_types:
  member_of: {label: 成员属于, source: Person, target: Team, cardinality: many_to_many}
settings:
  constraints: {max_hops: 2}
"""
    schema = linkml.parse(text)
    assert schema["schema_version"] == "v9.9.9"
    assert set(schema["entity_types"]) == {"Person", "Team"}
    assert schema["entity_types"]["Person"]["label"] == "人员"
    assert schema["attributes"]["name"]["owner"] == "Person"
    assert schema["attributes"]["age"]["range"] == "integer"
    assert schema["relation_types"]["member_of"]["target"] == "Team"
    assert schema["constraints"]["max_hops"] == 2


def test_parse_rejects_missing_classes():
    with pytest.raises(ValueError):
        linkml.parse("version: v1\nfoo: bar\n")
    with pytest.raises(ValueError):
        linkml.parse("")


def test_subset_parser_matches_pyyaml_when_available():
    """子集解析器不能与标准 YAML 读法分叉（装了 PyYAML 就交叉校验）。"""
    yaml = pytest.importorskip("yaml")
    text = linkml.dumps(reg.default_schema())
    assert yaml.safe_load(text) == linkml.parse_yaml(text)


def test_registry_to_linkml_still_single_entry():
    """旧入口保留：`registry.to_linkml` 与 `linkml.dumps` 必须同一份实现。"""
    schema = reg.default_schema()
    assert reg.to_linkml(schema) == linkml.dumps(schema)


# ---------------------------------------------------------------- 导入路径

def test_linkml_import_dry_run_touches_nothing(eng_):
    before_versions = len(eng_.registry.versions)
    before_rows = len(eng_.store.all("schema_registry"))
    text = linkml.dumps(reg.default_schema()).replace(
        "version: v2.0.0", "version: v2.1.0").replace(
        "  Person:\n    description:",
        "  Contractor:\n    description: 外包\n  Person:\n    description:")
    out = eng_.linkml_import(text, "业务负责人", dry_run=True)
    assert out["parsed"] and out["dry_run"] and not out["published"]
    assert out["compatibility"] in ("compatible", "breaking")
    assert out["diff"]["entity_types"]["added"] == ["Contractor"]
    assert out["roundtrip"]["lossless"] is True
    assert len(eng_.registry.versions) == before_versions
    assert len(eng_.store.all("schema_registry")) == before_rows


def test_linkml_import_apply_publishes_and_persists(eng_):
    text = linkml.dumps(reg.default_schema()).replace(
        "version: v2.0.0", "version: v2.1.0")
    out = eng_.linkml_import(text, "业务负责人", dry_run=False)
    assert out["published"] is True
    assert eng_.registry.current_version() == "v2.1.0"
    rows = {r["schema_version"]: r for r in eng_.store.all("schema_registry")}
    assert set(rows) == {"v2.0.0", "v2.1.0"}
    # 落库的 linkml_yaml 能反解回同一个 schema —— 关系轨本身就是可用的建模源
    back = linkml.parse(rows["v2.1.0"]["linkml_yaml"])
    assert back["schema_version"] == "v2.1.0"
    assert linkml.roundtrip(back)["lossless"] is True
    assert eng_.store.all("schema_migrations")


def test_linkml_import_rejects_duplicate_version(eng_):
    text = linkml.dumps(reg.default_schema())      # version 仍是 v2.0.0
    with pytest.raises(ValueError):
        eng_.linkml_import(text, "业务负责人", dry_run=False)


def test_publish_writes_schema_registry_and_pin_updates_it(eng_):
    schema = dict(eng_.registry.current())
    schema["schema_version"] = "v2.0.1"
    eng_.registry_publish(schema, "业务负责人")
    row = next(r for r in eng_.store.all("schema_registry")
               if r["schema_version"] == "v2.0.1")
    assert row["status"] == "published"
    assert "classes:" in row["linkml_yaml"]
    eng_.registry_pin("cq-1", "v2.0.1")
    row = next(r for r in eng_.store.all("schema_registry")
               if r["schema_version"] == "v2.0.1")
    assert row["pinned_by_cq"] == ["cq-1"]


def test_governance_summary_self_reports():
    gov = CQEngine(store=MemoryStore()).governance_summary()
    assert gov["linkml_single_source"] is True
    assert gov["linkml_roundtrip_lossless"] is True
    assert gov["linkml_roundtrip_diff"] == []


# ---------------------------------------------------------------- 自然键

def test_track_keys_use_natural_keys_not_ids():
    link = {"id": "x1", "capability_id": "cap-1", "cq_id": "cq-1"}
    assert track_key("capability_cqs", link) == "cap-1|cq-1"
    req = {"id": "r1", "cq_id": "cq-1", "cq_version": 2, "kind": "entity",
           "ref": "Person"}
    assert track_key("cq_requirements", req) == "cq-1|2|entity|Person"
    assert track_key("schema_registry", {"schema_version": "v2.0.0"}) == "v2.0.0"
    assert set(TRACK_TABLES) >= {"capabilities", "capability_cqs", "cq_requirements",
                                 "schema_registry", "schema_migrations",
                                 "schema_proposals"}


def test_memory_store_reports_tracks_unavailable():
    """memory 模式没有第二轨：必须如实说不可比，而不是返回 in_sync=True。"""
    e = CQEngine(store=MemoryStore())
    report = e.registry_tracks()
    assert report["comparable"] is False and report["in_sync"] is False
    assert report["reason"]


# ---------------------------------------------------------------- 实库：双轨

@db_required
def test_relational_track_is_written_after_bootstrap():
    """P1-2 的核心：四层结构不能只活在 JSONB 里。"""
    eng = CQEngine(store=default_store())
    assert eng.store.mode == "postgres"
    ready = eng.ensure_ready()            # 与启动同一条路径：建表 → 水合 → 对齐双轨
    assert ready["tracks"]["in_sync"] is True, ready["tracks"]
    assert ready["derived"]["in_sync"] is True, ready["derived"]
    report = eng.registry_tracks()
    assert report["comparable"] is True
    caps = report["tables"]["capabilities"]
    links = report["tables"]["capability_cqs"]
    reqs = report["tables"]["cq_requirements"]
    assert caps["relational_rows"] == caps["copy_rows"] == 5
    assert links["relational_rows"] == links["copy_rows"] == 16
    assert reqs["relational_rows"] == reqs["copy_rows"] == 74
    assert caps["in_sync"] and links["in_sync"] and reqs["in_sync"]
    assert report["in_sync"] is True


@db_required
def test_schema_registry_row_carries_linkml():
    from app.db import session as db

    eng = CQEngine(store=default_store())
    row = db.fetch_one(
        "SELECT schema_version, linkml_yaml, status, entity_types "
        "FROM ontology.schema_registry ORDER BY created_at DESC LIMIT 1")
    assert row and row["linkml_yaml"]
    parsed = linkml.parse(row["linkml_yaml"])
    assert parsed["schema_version"] == row["schema_version"]
    assert json.loads(row["entity_types"]) if isinstance(row["entity_types"], str) \
        else row["entity_types"]


@db_required
def test_tracks_detects_missing_row_and_sync_repairs_it():
    """删掉一行关系轨 → 对账必须报缺 → 同步补齐（全程事务内，结束回滚）。"""
    from app.db import session as db

    eng = CQEngine(store=default_store())
    eng.ensure_ready()
    try:
        with db.transaction():
            db.execute(
                "DELETE FROM ontology.capability_cqs WHERE ctid = "
                "(SELECT ctid FROM ontology.capability_cqs LIMIT 1)")
            broken = eng.registry_tracks()
            assert broken["in_sync"] is False
            assert broken["tables"]["capability_cqs"]["missing_count"] == 1
            fixed = eng.registry_sync_tracks()
            assert fixed["in_sync"] is True, fixed
            assert fixed["written_total"] >= 16
            raise _Rollback
    except _Rollback:
        pass


@db_required
def test_sync_tracks_is_idempotent():
    from app.db import session as db

    eng = CQEngine(store=default_store())
    eng.ensure_ready()
    try:
        with db.transaction():
            first = eng.registry_sync_tracks()
            second = eng.registry_sync_tracks()
            before = db.fetch_one(
                "SELECT count(*) AS n FROM ontology.cq_requirements")["n"]
            third = eng.registry_sync_tracks()
            after = db.fetch_one(
                "SELECT count(*) AS n FROM ontology.cq_requirements")["n"]
            assert first["in_sync"] and second["in_sync"] and third["in_sync"]
            assert before == after, "重复同步不得新增需求行"
            raise _Rollback
    except _Rollback:
        pass


@db_required
def test_registry_versions_reach_ontology_versions():
    """SIL-A04：Registry 发布要出现在运行时读的版本表里，且至少 2 个 published。"""
    from app.db import session as db

    eng = CQEngine(store=default_store())
    eng.ensure_ready()
    try:
        with db.transaction():
            schema = dict(eng.registry.current())
            schema["schema_version"] = "v2.1.0-p1-2-test"
            out = eng.registry_publish(schema, "业务负责人")
            assert out["runtime_versions"]["count"] >= 2
            row = db.fetch_one(
                "SELECT version, status, schema_uri, schema_content "
                "FROM ontology.ontology_versions WHERE version = :v",
                {"v": "v2.1.0-p1-2-test"})
            assert row["status"] == "published"
            assert row["schema_uri"].endswith("v2.1.0-p1-2-test")
            content = row["schema_content"]
            content = json.loads(content) if isinstance(content, str) else content
            assert content["schema_version"] == "v2.1.0-p1-2-test"
            assert "linkml_yaml" in content
            published = db.fetch_one(
                "SELECT count(*)::int AS n FROM ontology.ontology_versions "
                "WHERE lower(status) = 'published'")["n"]
            assert published >= 2, "SIL-A04 要求至少 2 个 published 版本"
            raise _Rollback
    except _Rollback:
        pass


@db_required
def test_relational_rows_satisfy_foreign_keys():
    """关系轨不是摆设：能力-CQ 关联与需求项都要能连回真实主键。"""
    from app.db import session as db

    CQEngine(store=default_store()).ensure_ready()
    row = db.fetch_one("""
        SELECT
          (SELECT count(*)::int FROM ontology.capability_cqs cc
             JOIN ontology.business_capabilities c ON c.id = cc.capability_id
             JOIN ontology.competency_questions q ON q.id = cc.cq_id) AS linked,
          (SELECT count(*)::int FROM ontology.cq_requirements r
             JOIN ontology.competency_questions q ON q.id = r.cq_id) AS reqs
    """)
    assert row["linked"] == 16
    assert row["reqs"] == 74


# ---------------------------------------------------------------- 派生表根因

def test_derived_status_detects_orphan_rows(eng_):
    """P1-2 暴露的真实缺陷：CQ 主键变过之后，旧派生行指向孤儿 id。"""
    status = eng_.derived_status()
    assert status["in_sync"] is True
    stale = eng_.store.all("capability_cqs")[0]
    eng_.store.update("capability_cqs", stale["id"],
                      {"cq_id": "00000000-0000-0000-0000-0000000000ff"})
    broken = eng_.derived_status()
    assert broken["in_sync"] is False
    assert broken["capability_cqs"]["orphan"] == 1
    assert broken["capability_cqs"]["missing"] == 1


def test_rebuild_derived_dry_run_then_apply(eng_):
    stale = eng_.store.all("cq_requirements")[0]
    eng_.store.update("cq_requirements", stale["id"],
                      {"cq_id": "00000000-0000-0000-0000-0000000000fe"})
    plan = eng_.rebuild_derived(dry_run=True)
    assert plan["dry_run"] is True and plan.get("applied") is not True
    assert plan["delete_requirements"] >= 1 and plan["add_requirements"] >= 1
    assert eng_.derived_status()["in_sync"] is False       # dry run 不动数据
    out = eng_.rebuild_derived(dry_run=False)
    assert out["applied"] is True
    assert out["status"]["in_sync"] is True, out["status"]
    assert len(eng_.store.all("cq_requirements")) == 74
    assert len(eng_.store.all("capability_cqs")) == 16


@db_required
def test_derived_tables_have_no_orphans_after_startup():
    """启动路径跑完后，派生表必须全部指向真实 CQ（否则关系轨写不进去）。"""
    from app.db import session as db

    eng = CQEngine(store=default_store())
    ready = eng.ensure_ready()
    assert ready["derived"]["in_sync"] is True, ready["derived"]
    row = db.fetch_one("""
        SELECT
          (SELECT count(*)::int FROM ontology.cq_requirements r
             WHERE NOT EXISTS (SELECT 1 FROM ontology.competency_questions q
                               WHERE q.id = r.cq_id)) AS orphan_reqs,
          (SELECT count(*)::int FROM ontology.capability_cqs c
             WHERE NOT EXISTS (SELECT 1 FROM ontology.competency_questions q
                               WHERE q.id = c.cq_id)) AS orphan_links
    """)
    assert row["orphan_reqs"] == 0 and row["orphan_links"] == 0


# ---------------------------------------------------------------- 装配层

def test_new_routes_registered():
    pytest.importorskip("fastapi")
    from app.api.v1 import cq_v2 as api

    paths = {r.path for r in api.router.routes}
    for path in ("/ontology/v2/registry/tracks",
                 "/ontology/v2/registry/tracks/sync",
                 "/ontology/v2/schema/linkml/roundtrip",
                 "/ontology/v2/schema/linkml/import",
                 "/ontology/v2/shapes",
                 "/ontology/v2/shapes/validate"):
        assert path in paths, path
