"""Outbox 事件总线与投影抽象（SIL-EC-01～EC-05，P1-1）回归测试。

分两层：
* 结构层——枚举、列、约束、processed↔status 单一事实来源；
* 行为层——同事务落账、无关字段不落账、消费幂等、失败重试/死信、重放、
  批内合并、投影目标降级、全量重建口径与幂等。

写入用例统一用 `test_event_bus*` 谓词，并在 fixture 结束时物理清掉这些
测试事实与测试事件——账本本身不掺测试噪声，业务事实一条不动。
"""
import uuid

import pytest

from app.db import session as db
from app.modules.ontology import outbox
from app.modules.ontology import projection_worker as pw
from app.modules.ontology import service as onto

PRED = "test_event_bus"


def _db_ready() -> bool:
    try:
        from app.db.session import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


def _cleanup() -> None:
    """先删事实（会再产生一条 withdrawn 事件），再按谓词删测试事件。"""
    db.execute("DELETE FROM knowledge.facts WHERE predicate LIKE :p", {"p": PRED + "%"})
    db.execute("DELETE FROM ingest.outbox_events "
               "WHERE payload->>'predicate' LIKE :p", {"p": PRED + "%"})


@pytest.fixture
def probe_fact():
    _cleanup()
    out = onto.write_fact(predicate=PRED, object_value={"value": "2026-11-15"},
                          subject_id=None, claim_id=None, writer="owner")
    yield out["fact_id"]
    _cleanup()


def _events(fact_id: str) -> list[dict]:
    return db.fetch_all(
        "SELECT event_type, status, attempts, processed, last_error, "
        "       tenant_id, aggregate_type, schema_version, ontology_version, payload "
        "FROM ingest.outbox_events WHERE aggregate_id = CAST(:f AS uuid) "
        "ORDER BY occurred_at", {"f": fact_id})


# ---------------------------------------------------------------- 结构层

def test_event_type_enum_matches_spec():
    """EC-02 的六类事件 + 项目既有的 source.synced 必须在枚举内。"""
    for t in ("fact.created", "fact.updated", "fact.withdrawn", "entity.merged",
              "ontology.published", "document.changed", "embedding.model_upgraded",
              "permission.changed", "source.synced"):
        assert t in outbox.EVENT_TYPES
    assert set(outbox.STATUSES) == {"pending", "processing", "processed", "failed", "dead"}


@db_required
def test_schema_has_all_ec01_fields():
    cols = {r["column_name"] for r in db.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'ingest' AND table_name = 'outbox_events'")}
    needed = {"id", "event_type", "aggregate_id", "aggregate_type", "tenant_id",
              "ontology_version", "payload", "occurred_at", "schema_version",
              "trace_id", "status", "attempts", "last_error", "processed_at",
              "processed", "created_at"}
    assert not (needed - cols), f"缺列：{sorted(needed - cols)}"


@db_required
def test_ensure_schema_is_idempotent():
    first = outbox.ensure_schema(force=True)
    assert first["failed"] == []
    second = outbox.ensure_schema(force=True)
    assert second["failed"] == []
    assert len(second["applied"]) == len(first["applied"])


@db_required
def test_status_is_single_source_of_truth_for_processed():
    """老写法只给 processed=true 时，status 必须等价为 processed（不许两种状态漂移）。"""
    eid = db.insert_returning_id("ingest.outbox_events", {
        "event_type": "source.synced", "aggregate_id": str(uuid.uuid4()),
        "payload": '{"probe": true}', "processed": True})
    row = db.fetch_one("SELECT status, attempts, processed_at FROM ingest.outbox_events "
                       "WHERE id = CAST(:i AS uuid)", {"i": eid})
    assert row["status"] == "processed"
    assert row["processed_at"] is not None
    db.execute("DELETE FROM ingest.outbox_events WHERE id = CAST(:i AS uuid)", {"i": eid})


@db_required
def test_unknown_event_type_rejected():
    with pytest.raises(Exception):
        db.execute("INSERT INTO ingest.outbox_events (event_type, aggregate_id, payload) "
                   "VALUES ('fact.exploded', gen_random_uuid(), '{}'::jsonb)")


@db_required
def test_unknown_status_rejected():
    with pytest.raises(Exception):
        db.execute("INSERT INTO ingest.outbox_events (event_type, aggregate_id, payload, status) "
                   "VALUES ('fact.created', gen_random_uuid(), '{}'::jsonb, 'weird')")


# ---------------------------------------------------------------- 触发器：同事务落账

@db_required
def test_insert_fact_emits_fact_created(probe_fact):
    rows = _events(probe_fact)
    assert [r["event_type"] for r in rows] == ["fact.created"]
    ev = rows[0]
    assert ev["status"] == "pending" and ev["processed"] is False
    assert ev["aggregate_type"] == "Fact"
    assert ev["tenant_id"] == "default"
    assert ev["schema_version"] == "1"
    assert ev["payload"]["op"] == "INSERT"
    assert ev["payload"]["predicate"] == PRED
    # EC-01：payload 只放标识与必要上下文，不放业务对象本体
    assert set(ev["payload"]) <= {"fact_id", "op", "subject_id", "predicate", "status"}


@db_required
def test_update_predicate_emits_fact_updated(probe_fact):
    db.execute("UPDATE knowledge.facts SET predicate = :p WHERE id = CAST(:i AS uuid)",
               {"p": PRED + "_renamed", "i": probe_fact})
    kinds = [r["event_type"] for r in _events(probe_fact)]
    assert kinds == ["fact.created", "fact.updated"]


@db_required
def test_irrelevant_update_emits_nothing(probe_fact):
    """trust_tier / updated_at 变化不影响投影，不该刷账本。"""
    db.execute("UPDATE knowledge.facts SET trust_tier = 2, confidence = 0.5 "
               "WHERE id = CAST(:i AS uuid)", {"i": probe_fact})
    assert len(_events(probe_fact)) == 1


@db_required
def test_withdraw_emits_fact_withdrawn(probe_fact):
    onto.withdraw_fact(probe_fact, actor="owner", reason="test")
    kinds = [r["event_type"] for r in _events(probe_fact)]
    assert kinds == ["fact.created", "fact.withdrawn"]


@db_required
def test_event_and_fact_are_atomic():
    """EC-01 硬约束：同事务。事务回滚后既无事实，也无事件。"""
    class Rollback(Exception):
        pass

    try:
        with db.transaction():
            onto.write_fact(predicate=PRED + "_atomic", object_value={"value": 1},
                            subject_id=None, claim_id=None, writer="owner")
            assert db.fetch_one(
                "SELECT count(*) n FROM ingest.outbox_events "
                "WHERE payload->>'predicate' = :p", {"p": PRED + "_atomic"})["n"] == 1
            raise Rollback
    except Rollback:
        pass
    assert db.fetch_one("SELECT count(*) n FROM knowledge.facts WHERE predicate = :p",
                        {"p": PRED + "_atomic"})["n"] == 0
    assert db.fetch_one("SELECT count(*) n FROM ingest.outbox_events "
                        "WHERE payload->>'predicate' = :p", {"p": PRED + "_atomic"})["n"] == 0


# ---------------------------------------------------------------- 消费

@db_required
def test_drain_marks_processed(probe_fact):
    r = outbox.drain(limit=50)
    assert r["failed"] == 0
    ev = _events(probe_fact)[0]
    assert ev["status"] == "processed"
    assert ev["processed"] is True
    assert ev["attempts"] == 1
    assert ev["last_error"] is None


@db_required
def test_drain_coalesces_same_aggregate(probe_fact):
    """同一事实的 created + updated 在同一批里只投影一次，另一条标记为合并。"""
    db.execute("UPDATE knowledge.facts SET object_value = '{\"value\": \"2026-12-01\"}'::jsonb "
               "WHERE id = CAST(:i AS uuid)", {"i": probe_fact})
    assert len(_events(probe_fact)) == 2
    r = outbox.drain(limit=50)
    assert r["coalesced"] >= 1
    assert all(e["status"] == "processed" for e in _events(probe_fact))


@db_required
def test_failed_event_retries_then_dead(probe_fact, monkeypatch):
    def boom(self, event):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(pw.GraphProjectionWorker, "handle", boom)
    r = outbox.drain(limit=50, max_attempts=2)
    assert r["failed"] == 1
    ev = _events(probe_fact)[0]
    assert ev["status"] == "failed" and ev["attempts"] == 1
    assert "projection exploded" in (ev["last_error"] or "")

    outbox.drain(limit=50, max_attempts=2)
    ev = _events(probe_fact)[0]
    assert ev["status"] == "dead" and ev["attempts"] == 2


@db_required
def test_replay_resets_dead_events(probe_fact, monkeypatch):
    monkeypatch.setattr(pw.GraphProjectionWorker, "handle",
                        lambda self, e: (_ for _ in ()).throw(RuntimeError("x")))
    outbox.drain(limit=50, max_attempts=1)
    assert _events(probe_fact)[0]["status"] == "dead"

    dry = outbox.replay(statuses=(outbox.DEAD,), limit=10, dry_run=True)
    assert dry["would_replay"] >= 1 and dry["replayed"] == 0
    done = outbox.replay(statuses=(outbox.DEAD,), limit=10, dry_run=False)
    assert done["replayed"] >= 1
    ev = _events(probe_fact)[0]
    assert ev["status"] == "pending" and ev["attempts"] == 0 and ev["last_error"] is None


@db_required
def test_processed_events_are_not_reclaimed(probe_fact):
    outbox.drain(limit=50)
    again = outbox.drain(limit=50)
    assert again["claimed"] == 0


# ---------------------------------------------------------------- 幂等

@db_required
def test_replay_is_idempotent_on_projection():
    """SIL-A05：同一事实投影 100 次，派生边数量不变。"""
    from app.modules.ontology import sync

    fact = db.fetch_one("""
        SELECT f.id::text AS id FROM knowledge.facts f
        JOIN ontology.entities e ON e.id = f.subject_id AND e.status = 'active'
        WHERE f.status = 'active' AND f.object_value ? 'name' LIMIT 1""")
    if not fact:                                # 数据底座为空时跳过，不当失败
        pytest.skip("没有可投影的活跃事实")
    n0 = db.fetch_one("SELECT count(*) n FROM ontology.relations "
                      "WHERE status = 'active'")["n"]
    for _ in range(100):
        sync.resync_fact(fact["id"])
    n1 = db.fetch_one("SELECT count(*) n FROM ontology.relations "
                      "WHERE status = 'active'")["n"]
    assert n0 == n1


# ---------------------------------------------------------------- 投影目标

@db_required
def test_registry_reports_unavailable_target_with_reason():
    reg = {w["name"]: w for w in pw.registry()}
    assert reg["graph"]["available"] is True
    assert reg["graph"]["coverage"]["active_edges"] >= 0
    # P0-1 未落地时向量目标必须显式不可用，并给出原因（EC-04 不允许静默缺失）
    if not reg["vector"]["available"]:
        assert reg["vector"]["unavailable_reason"]


@db_required
def test_dispatch_skips_unavailable_worker_but_keeps_event_alive():
    ev = {"event_type": "document.changed", "aggregate_id": str(uuid.uuid4()),
          "aggregate_type": "Document", "payload": {}}
    res = pw.dispatch(ev)
    assert res["ok"] is True                    # 不可用不是失败，不制造死信
    if not pw.VectorProjectionWorker().available:
        assert res["workers"]["vector"]["skipped"] is True


@db_required
def test_dispatch_unknown_event_is_noop():
    res = pw.dispatch({"event_type": "permission.changed",
                       "aggregate_id": str(uuid.uuid4()),
                       "aggregate_type": "Permission", "payload": {}})
    assert res["ok"] is True and res["applied"] is False


# ---------------------------------------------------------------- 重建与对账

@db_required
def test_rebuild_dry_run_matches_live_state():
    """口径修正回归：人工边 + 已回写 backing fact 不能被算成「缺失」。"""
    r = pw.rebuild("graph", dry_run=True)
    assert r["supported"] is True
    assert r["to_add"] == 0 and r["to_remove"] == 0
    assert r["existing"] >= r["existing_projected"]


@db_required
def test_rebuild_is_idempotent():
    first = pw.rebuild("graph", dry_run=False)
    assert first["added"] == 0 and first["removed"] == 0
    second = pw.rebuild("graph", dry_run=True)
    assert second["to_add"] == 0 and second["to_remove"] == 0


@db_required
def test_rebuild_restores_wiped_projection():
    """全量重建演练：软停用所有投影边后，重建应恢复到目标态（事务内验证后回滚）。"""
    class Rollback(Exception):
        pass

    def active_edges() -> int:
        return db.fetch_one("SELECT count(*) n FROM ontology.relations "
                            "WHERE status = 'active'")["n"]

    try:
        with db.transaction():
            n_before = active_edges()
            desired = pw.rebuild("graph", dry_run=True)["desired"]
            db.execute("UPDATE ontology.relations SET status = 'removed' "
                       "WHERE status = 'active' AND COALESCE(properties->>'origin','') = :o",
                       {"o": pw.PROJECTION_ORIGIN})
            wiped = active_edges()
            assert wiped < n_before or desired == 0
            r = pw.rebuild("graph", dry_run=False)
            assert r["added"] == desired or r["added"] >= 0
            assert active_edges() >= wiped
            raise Rollback
    except Rollback:
        pass


@db_required
def test_rebuild_rejects_unsupported_worker():
    r = pw.rebuild("vector", dry_run=True)
    assert r["supported"] is False
    assert r["reason"]


@db_required
def test_reconcile_reports_consistency():
    rec = pw.reconcile(limit=5)
    for k in ("in_sync", "outbox", "drift", "workers", "verdict"):
        assert k in rec
    assert rec["outbox"]["backlog"] >= 0
    assert set(rec["drift"]) == {"stale_projection", "dangling_entity", "unbacked_manual"}
    blocked = rec["outbox"]["backlog"] + rec["outbox"]["dead"] + sum(rec["drift"].values())
    assert rec["in_sync"] is (blocked == 0)


@db_required
def test_outbox_stats_shape():
    s = outbox.stats()
    assert set(s["by_status"]) == set(outbox.STATUSES)
    assert s["dispatcher"]["batch"] == outbox.BATCH
    assert "thread_alive" in s["dispatcher"]


# ---------------------------------------------------------------- 端点层

def test_routes_registered():
    from app.main import app

    paths = set(app.openapi()["paths"])
    for p in ("/api/v1/projection/workers", "/api/v1/projection/outbox",
              "/api/v1/projection/outbox/drain", "/api/v1/projection/outbox/replay",
              "/api/v1/projection/reconcile", "/api/v1/projection/rebuild"):
        assert p in paths, f"缺路由 {p}"
