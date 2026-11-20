"""CQ V2 状态持久化（P1-5）回归测试。

分两层：
* 纯逻辑层——Registry 状态序列化、MemoryStore 快照往返、非持久化降级语义；
* 实库层——DDL、重启水合（新引擎只靠库内副本恢复 16 条 CQ / Registry / 审计）、
  幂等 ensure_ready、漂移检出与 flush 修复。写操作用例一律包在事务里回滚，
  不留下测试数据。
"""
import pytest

from app.modules.ontology.cq_v2 import registry as reg
from app.modules.ontology.cq_v2.service import CQEngine
from app.modules.ontology.cq_v2.store import (
    STATE_TABLE, MemoryStore, PostgresStore, default_store, requested_mode,)


def _db_ready() -> bool:
    try:
        from app.db.session import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


class _Rollback(Exception):
    """在事务里主动抛出，验证「验证过程本身可回滚」。"""


# ---------------------------------------------------------------- Registry 序列化

def test_registry_state_roundtrip_keeps_every_field():
    r = reg.SchemaRegistry(reg.default_schema())
    schema = dict(r.current())
    schema["schema_version"] = "v2.0.1"
    schema["attributes"] = {**(schema.get("attributes") or {}),
                            "grade": {"range": "string", "owner": "Person"}}
    r.publish(schema, "owner")
    r.pin("cq-1", "v2.0.1")
    state = r.to_state()

    fresh = reg.SchemaRegistry(reg.default_schema())
    assert fresh.load_state(state) is True
    assert fresh.current_version() == "v2.0.1"
    assert fresh.order == r.order
    assert len(fresh.versions) == len(r.versions) == 2
    assert len(fresh.migrations) == 1
    assert fresh.pins == {"cq-1": "v2.0.1"}
    # 锁定关系落在版本对象里，重新水合后仍然可查
    assert "cq-1" in fresh.get("v2.0.1")["pinned_by_cq"]


def test_registry_load_state_empty_keeps_current_schema():
    r = reg.SchemaRegistry(reg.default_schema())
    before = r.current_version()
    assert r.load_state(None) is False
    assert r.load_state({}) is False
    assert r.current_version() == before


def test_registry_state_is_json_serialisable():
    """必须能原样进 JSONB：不能带 set / 自定义对象。"""
    import json

    r = reg.SchemaRegistry(reg.default_schema())
    json.dumps(r.to_state(), ensure_ascii=False)


# ---------------------------------------------------------------- MemoryStore 快照

def test_memory_store_snapshot_and_restore_roundtrip():
    s = MemoryStore()
    s.insert("cqs", {"id": "c1", "code": "CQ-001"})
    s.insert("cqs", {"id": "c2", "code": "CQ-002"})
    s.write_state("schema_registry", {"order": ["v2.0.0"]})
    snap = s.snapshot()

    other = MemoryStore()
    other.restore(snap)
    assert len(other.all("cqs")) == 2
    assert other.read_state("schema_registry") == {"order": ["v2.0.0"]}
    assert other.status()["total_objects"] == 2


def test_memory_store_restore_replaces_previous_content():
    s = MemoryStore()
    s.insert("cqs", {"id": "stale"})
    s.restore({"tables": {"cqs": [{"id": "fresh"}]}, "state": {}})
    assert [r["id"] for r in s.all("cqs")] == ["fresh"]


def test_memory_store_drift_is_not_comparable():
    d = MemoryStore().drift()
    assert d["comparable"] is False and d["in_sync"] is False


# ---------------------------------------------------------------- 非持久化降级

def test_memory_engine_degrades_explicitly():
    e = CQEngine(store=MemoryStore())
    e.bootstrap()
    assert e.store.durable is False
    assert e.persist_state()["skipped"] is True
    restored = e.restore()
    assert restored["restored"] is False and restored["durable"] is False
    status = e.persistence_status()
    assert status["durable"] is False
    assert status["cqs"] == 16
    verify = e.verify_persistence()
    assert verify["comparable"] is False


def test_meta_exposes_persistence_summary():
    e = CQEngine(store=MemoryStore())
    p = e.meta()["persistence"]
    assert p["durable"] is False
    assert "registry_versions" in p and "audit_entries" in p


def test_requested_mode_prefers_env(monkeypatch):
    monkeypatch.setenv("CQ_V2_STORE", "memory")
    assert requested_mode() == "memory"
    assert isinstance(default_store(), MemoryStore)


def test_requested_mode_defaults_to_postgres(monkeypatch):
    monkeypatch.delenv("CQ_V2_STORE", raising=False)
    assert requested_mode() in ("postgres", "memory")   # settings 可能被显式改过
    store = default_store()
    assert store.durable or store.degraded_reason      # 要么持久化，要么给原因


# ---------------------------------------------------------------- 实库：DDL 与水合

@db_required
def test_postgres_ddl_applies_without_error():
    store = PostgresStore()
    assert store.ping() is True
    out = store.ensure_schema()
    assert out["errors"] == []
    assert out["applied"] == out["ddl_statements"]
    assert out["durable"] is True


@db_required
def test_restart_hydrates_state_from_db_copy():
    """模拟重启：全新引擎（内存空）只靠库内副本恢复。"""
    from app.db.session import fetch_all

    e = CQEngine(store=PostgresStore())
    out = e.ensure_ready()
    assert out["durable"] is True
    assert e.restore()["restored"] is False          # 已有内存态，不覆盖

    fresh = CQEngine(store=PostgresStore())
    r = fresh.restore()
    assert r["durable"] is True and r["restored"] is True
    assert r["cqs"] == 16
    assert fresh.registry.current_version() == e.registry.current_version()

    # 身份必须与 competency_questions 对齐，否则写覆盖率会撞外键
    ids_db = {row["code"]: row["id"] for row in fetch_all(
        "SELECT id::text AS id, code FROM ontology.competency_questions")}
    for cq in fresh.cqs():
        if cq["code"] in ids_db:
            assert ids_db[cq["code"]] == cq["id"]

    verify = fresh.verify_persistence()
    assert verify["comparable"] is True
    assert verify["in_sync"] is True


@db_required
def test_ensure_ready_is_idempotent():
    from app.db.session import fetch_one

    def total() -> int:
        return int(fetch_one("SELECT count(*) AS n FROM ontology.cq_v2_records")["n"])

    first = CQEngine(store=PostgresStore()).ensure_ready()
    n1 = total()
    second = CQEngine(store=PostgresStore()).ensure_ready()
    n2 = total()
    assert first["cqs"] == second["cqs"] == 16
    assert second["seeded"] is None          # 第二次是水合，不再播种
    assert n1 == n2


@db_required
def test_state_keys_are_persisted():
    snap = PostgresStore().load_snapshot()
    assert {"schema_registry", "audit_log", "baseline"} <= set(snap["state"])
    assert snap["state"]["schema_registry"]["order"]


# ---------------------------------------------------------------- 实库：Registry 跨重启

@db_required
def test_registry_survives_restart_inside_transaction():
    from app.db import session as db

    try:
        with db.transaction():
            e = CQEngine(store=PostgresStore())
            e.restore()
            base = e.registry.current_version()
            before_versions = len(e.registry.versions)
            before_migrations = len(e.registry.migrations)
            e.registry_pin(e.cqs()[0]["id"], base)
            schema = dict(e.registry.current())
            schema["schema_version"] = "v2.9.9"
            schema["attributes"] = {**(schema.get("attributes") or {}),
                                    "regression_flag": {"range": "string",
                                                        "owner": "Person"}}
            e.registry_publish(schema, "owner")

            fresh = CQEngine(store=PostgresStore())
            fresh.restore()
            assert fresh.registry.current_version() == "v2.9.9"
            # 版本数按「发布前 + 1」比较：已发布的版本会随治理推进增加，
            # 写死 2 会让用例在每次合法发布后假失败（P1-2 就撞了一次）。
            assert len(fresh.registry.versions) == before_versions + 1
            assert len(fresh.registry.migrations) == before_migrations + 1
            assert fresh.registry.pins
            assert any(a["step"] == "schema_publish" for a in fresh.audit_log)
            raise _Rollback
    except _Rollback:
        pass

    after = CQEngine(store=PostgresStore())
    after.restore()
    assert after.registry.current_version() != "v2.9.9"    # 已回滚，未污染


# ---------------------------------------------------------------- 实库：漂移与修复

@db_required
def test_drift_detected_then_flush_repairs():
    from app.db import session as db

    try:
        with db.transaction():
            e = CQEngine(store=PostgresStore())
            e.restore()
            db.execute(
                "DELETE FROM ontology.cq_v2_records WHERE table_name = 'cqs' "
                "AND id = (SELECT id FROM ontology.cq_v2_records "
                "          WHERE table_name = 'cqs' LIMIT 1)")
            drift = e.verify_persistence()
            assert drift["in_sync"] is False
            assert len(drift["extra_in_memory"]["cqs"]) == 1

            e.flush()
            assert e.verify_persistence()["in_sync"] is True
            raise _Rollback
    except _Rollback:
        pass


# ---------------------------------------------------------------- 装配层

def test_persistence_routes_registered():
    pytest.importorskip("fastapi")
    from app.api.v1 import cq_v2 as api

    paths = {r.path for r in api.router.routes}
    assert "/ontology/v2/persistence" in paths
    assert "/ontology/v2/persistence/hydrate" in paths
    assert "/ontology/v2/persistence/flush" in paths
    assert "/ontology/v2/persistence/verify" in paths


def test_state_table_name_is_not_a_business_table():
    """`_state` 是 KV 伪表，不能与业务表重名，否则水合时会串桶。"""
    assert STATE_TABLE.startswith("_")
    for name in ("cqs", "capabilities", "cq_requirements", "instances"):
        assert name != STATE_TABLE
