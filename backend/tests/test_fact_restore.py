"""恢复发布（撤销的逆操作）回归。

背景：批量撤销曾把「全选」理解成「全部筛选结果（含所有分页）」，一次误操作
整库下架。产品层修的是「全选本页」，数据层补的是**可逆**：撤销本来就是软删，
所以必须有正式的恢复能力，而不是只能改库。

覆盖：
* 纯逻辑：恢复留痕字段 / 客体比较键归一化 / 路由已注册；
* 实库：合成事实走「发布 → 撤销 → 恢复」全链路（结尾物理清理自己造的行），
  dry_run 不写库，非 withdrawn 与非法 id 的拒绝口径。
"""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ontology import service as ONT
from app.modules.ontology.semantic import store


def _db_ready() -> bool:
    try:
        return store.ping()
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


@pytest.fixture()
def client():
    return TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ================================================================ 纯逻辑
class TestRestoreNote:
    def test_note_has_who_when_why(self):
        note = ONT._restore_note("owner", "console:restore", None)
        assert note["restored_by"] == "owner"
        assert note["restoration"] == "republished"
        assert note["reason"] == "console:restore"
        assert note["at"]
        assert "previous_withdrawal" not in note

    def test_note_defaults_and_truncation(self):
        note = ONT._restore_note("  ", "  ", None)
        assert note["restored_by"] == "unknown"
        assert note["reason"] == "owner restored from console"
        long = ONT._restore_note("owner", "x" * 1000, None)
        assert len(long["reason"]) == 300

    def test_note_keeps_previous_withdrawal_for_audit(self):
        prev = {"reason": "console:batch-unpublish", "at": "2026-11-16T10:38:31+08:00",
                "withdrawn_by": "owner"}
        note = ONT._restore_note("owner", "console:restore", prev)
        assert note["previous_withdrawal"]["reason"] == "console:batch-unpublish"
        assert note["previous_withdrawal"]["withdrawn_by"] == "owner"


class TestObjectKey:
    def test_dict_order_insensitive(self):
        assert ONT._object_key({"a": 1, "b": 2}) == ONT._object_key({"b": 2, "a": 1})

    def test_none_is_empty(self):
        assert ONT._object_key(None) == ""

    def test_same_shape_differs_by_value(self):
        assert ONT._object_key({"name": "甲"}) != ONT._object_key({"name": "乙"})


class TestRoutesRegistered:
    def test_single_and_batch_restore_routes_exist(self):
        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/v1/knowledge/facts/{fact_id}/restore" in paths
        assert "/api/v1/knowledge/facts/batch-restore" in paths


class TestWithdrawBurstGuard:
    """「小批量高频」形态的闸门：单次上限挡不住循环，只能按时间窗口累计拦。"""

    def test_small_batch_under_limit_passes(self):
        assert ONT.burst_blocked(0, 10, False) is False
        assert ONT.burst_blocked(190, 10, False) is False

    def test_accumulated_batch_is_blocked(self):
        assert ONT.burst_blocked(190, 11, False) is True
        assert ONT.burst_blocked(200, 1, False) is True

    def test_explicit_confirmation_passes(self):
        assert ONT.burst_blocked(900, 500, True) is False

    def test_window_constant_is_sixty_seconds(self):
        assert ONT.BATCH_WITHDRAW_BURST_WINDOW == 60
        assert ONT.BATCH_WITHDRAW_BURST > 0


# ================================================================ 实库
@pytest.fixture()
def synthetic_fact():
    """造一条私有事实（谓词带测试前缀），用完物理删掉，不碰真实数据。"""
    from app.db import session as db

    fid = str(uuid.uuid4())
    predicate = "_test_restore_pred_" + fid[:8]
    db.execute(
        "INSERT INTO knowledge.facts (id, predicate, object_value, status, version, "
        "  source_ref, trust_tier, verification_status, ontology_version, created_by) "
        "VALUES (CAST(:id AS uuid), :p, CAST(:v AS jsonb), 'active', 1, '{}'::jsonb, "
        "  3, 'unverified', 'v1.0.0', 'test')",
        {"id": fid, "p": predicate,
         "v": json.dumps({"label": "恢复测试"}, ensure_ascii=False)},
    )
    try:
        yield {"id": fid, "predicate": predicate}
    finally:
        db.execute("DELETE FROM knowledge.facts WHERE id = CAST(:id AS uuid)", {"id": fid})
        db.execute(
            "DELETE FROM audit.access_logs WHERE resource_id = :id "
            "AND action LIKE 'knowledge.fact.%'", {"id": fid})


@db_required
class TestRestoreLifecycle:
    def test_withdraw_then_restore_roundtrip(self, synthetic_fact):
        fid = synthetic_fact["id"]
        w = ONT.withdraw_fact(fid, actor="test", reason="test:withdraw")
        assert w["status"] == ONT.FACT_WITHDRAWN

        dry = ONT.restore_facts([fid], actor="test", reason="test:restore", dry_run=True)
        assert dry["candidates"] == 1 and dry["restored"] == 0
        from app.db import session as db
        assert db.fetch_one(
            "SELECT status FROM knowledge.facts WHERE id = CAST(:i AS uuid)",
            {"i": fid})["status"] == ONT.FACT_WITHDRAWN, "dry_run 不得写库"

        out = ONT.restore_facts([fid], actor="test", reason="test:restore")
        assert out["restored"] == 1
        row = db.fetch_one(
            "SELECT status, valid_to, source_ref FROM knowledge.facts "
            "WHERE id = CAST(:i AS uuid)", {"i": fid})
        assert row["status"] == ONT.FACT_ACTIVE
        assert row["valid_to"] is None
        assert row["source_ref"]["restoration"] == "republished"
        # 撤销时的原始留痕必须还在，恢复不能抹掉历史
        assert row["source_ref"]["previous_withdrawal"]["reason"] == "test:withdraw"
        assert row["source_ref"]["withdrawn_by"] == "test"

    def test_duplicate_active_is_skipped_not_doubled(self, synthetic_fact):
        """同主体同谓词同客体已有活跃值时，恢复要跳过而不是造出第二条。"""
        from app.db import session as db

        fid = synthetic_fact["id"]
        ONT.withdraw_fact(fid, actor="test", reason="test:withdraw")
        twin = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO knowledge.facts (id, predicate, object_value, status, version, "
                "  source_ref, trust_tier, verification_status, ontology_version, created_by) "
                "VALUES (CAST(:id AS uuid), :p, CAST(:v AS jsonb), 'active', 1, "
                "  '{}'::jsonb, 3, 'unverified', 'v1.0.0', 'test')",
                {"id": twin, "p": synthetic_fact["predicate"],
                 "v": json.dumps({"label": "恢复测试"}, ensure_ascii=False)},
            )
            out = ONT.restore_facts([fid], actor="test", reason="test:restore")
            assert out["restored"] == 0
            assert "already active" in out["skipped"][0]["reason"]
            assert out["skipped"][0]["conflict_fact_id"] == twin
        finally:
            db.execute("DELETE FROM knowledge.facts WHERE id = CAST(:i AS uuid)",
                       {"i": twin})


@db_required
class TestRestoreApiContract:
    def test_invalid_uuid_rejected(self, client):
        r = client.post("/api/v1/knowledge/facts/batch-restore",
                        json={"fact_ids": ["not-a-uuid"], "dry_run": True})
        assert r.status_code == 400

    def test_empty_list_rejected(self, client):
        r = client.post("/api/v1/knowledge/facts/batch-restore", json={"fact_ids": []})
        assert r.status_code == 400

    def test_unknown_fact_is_skipped_not_error(self, client):
        missing = str(uuid.uuid4())
        r = client.post("/api/v1/knowledge/facts/batch-restore",
                        json={"fact_ids": [missing], "dry_run": True})
        assert r.status_code == 200
        body = r.json()
        assert body["candidates"] == 0
        assert body["skipped"][0]["reason"] == "fact not found"
