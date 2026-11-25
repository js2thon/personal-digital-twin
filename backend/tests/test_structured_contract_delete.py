"""删除映射契约回归。

口径（与「事实永不物理删除」同源）：
* 零同步留痕 → 物理删除，用于清掉试探性的废弃草稿；
* 有留痕（sync_runs / anomalies）→ 只标记删除（status=archived），从列表与下拉
  消失但行仍在，历史批次仍能复盘「这批事实由哪套规则产生」；
* 显式 purge=true 且存在留痕 → 400，不做「看着删了其实只是停了」的静默降级。

覆盖：路由与常量、列表默认排除 archived、实库「删除 → 彻底删除 / 标记删除 /
拒绝 purge」三条路径（结尾清理自己造的行）。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db import session as db
from app.main import app
from app.modules.ontology.semantic import store
from app.modules.structured import contracts as ct

MIN_MAPPING = {
    "entity": {"entity_type": "Person", "pk_column": "employee_id",
               "name_column": "employee_name", "external_id_prefix": "hr:"},
    "fields": [{"column": "dept_name", "predicate": "belongs_to_department",
                "object_kind": "literal", "visibility": "organization"}],
    "sensitivity": {"default": "organization", "restricted_columns": []},
    "allowed_values": {},
    "expect_columns": ["employee_id", "employee_name", "dept_name"],
}


def _db_ready() -> bool:
    try:
        return store.ping()
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


@pytest.fixture()
def client():
    return TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


@pytest.fixture()
def make_contract():
    """造一份唯一命名的草稿契约，用完删干净（含留痕）。"""
    created: list[str] = []

    def _make(with_run: bool = False) -> dict:
        errors, clean = ct.validate_mapping(MIN_MAPPING)
        assert not errors, errors
        row = ct.create_contract(
            name=f"del_test_{uuid.uuid4().hex[:10]}", source_ref={"system": "test"},
            mapping=clean, note="pytest", actor="pytest",
        )
        created.append(row["id"])
        if with_run:
            db.insert_returning_id("ingest.structured_sync_runs", {
                "contract_id": row["id"], "contract_version": row["version"],
                "contract_hash": "h" * 64, "batch_id": "pytest-run",
                "dry_run": True, "status": "completed", "row_count": 0,
                "counts": "{}", "cursor_before": "{}", "cursor_after": "{}",
                "actor": "pytest", "error_info": "{}",
            })
        return row

    yield _make

    for cid in created:
        db.execute("DELETE FROM ingest.structured_anomalies "
                   "WHERE contract_id = CAST(:i AS uuid)", {"i": cid})
        db.execute("DELETE FROM ingest.structured_sync_runs "
                   "WHERE contract_id = CAST(:i AS uuid)", {"i": cid})
        db.execute("DELETE FROM ingest.structured_contracts "
                   "WHERE id = CAST(:i AS uuid)", {"i": cid})


# ================================================================ 纯逻辑
class TestContractDeleteSurface:
    def test_archived_status_is_distinct_from_others(self):
        states = {ct.CONTRACT_DRAFT, ct.CONTRACT_PUBLISHED,
                  ct.CONTRACT_DEPRECATED, ct.CONTRACT_EPHEMERAL,
                  ct.CONTRACT_ARCHIVED}
        assert len(states) == 5  # 五个状态互不相同，archived 不是复用某个旧值

    def test_route_registered(self):
        # Resolve deferred included routers through the generated OpenAPI route map.
        methods = app.openapi()["paths"]["/api/v1/structured/contracts/{contract_id}"]
        assert "delete" in methods

    def test_list_query_excludes_archived_by_default(self, monkeypatch):
        captured: list[str] = []
        monkeypatch.setattr(ct, "ensure_schema", lambda: None)
        monkeypatch.setattr(ct.db, "fetch_all",
                            lambda sql, params=None: captured.append(sql) or [])
        ct.list_contracts(10)
        assert "archived" in captured[-1]
        # 留痕次数随列表一起返回：前端要靠它提前说清「删除」到底是哪种删除
        assert "sync_runs" in captured[-1] and "anomalies" in captured[-1]

    def test_list_query_can_include_archived(self, monkeypatch):
        captured: list[str] = []
        monkeypatch.setattr(ct, "ensure_schema", lambda: None)
        monkeypatch.setattr(ct.db, "fetch_all",
                            lambda sql, params=None: captured.append(sql) or [])
        ct.list_contracts(10, include_archived=True)
        assert "c.status <> 'archived'" not in captured[-1]


# ================================================================ 实库
@db_required
class TestContractDeleteReal:
    def test_contract_without_runs_is_purged(self, client, make_contract):
        row = make_contract()
        r = client.delete(f"/api/v1/structured/contracts/{row['id']}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == "purged" and body["deleted"] is True
        assert body["previous_status"] == ct.CONTRACT_DRAFT
        assert ct.get_contract(row["id"]) is None      # 行真的没了

    def test_contract_with_runs_is_archived_not_dropped(self, client, make_contract):
        row = make_contract(with_run=True)
        r = client.delete(f"/api/v1/structured/contracts/{row['id']}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mode"] == ct.CONTRACT_ARCHIVED
        assert body["sync_runs"] == 1
        kept = ct.get_contract(row["id"])
        assert kept is not None and kept["status"] == ct.CONTRACT_ARCHIVED
        # 从可管理列表里消失，但按 id 仍可追溯
        ids = {c["id"] for c in ct.list_contracts(200)}
        assert row["id"] not in ids
        ids_all = {c["id"] for c in ct.list_contracts(200, include_archived=True)}
        assert row["id"] in ids_all

    def test_archived_contract_cannot_be_written(self, client, make_contract):
        row = make_contract(with_run=True)
        client.delete(f"/api/v1/structured/contracts/{row['id']}")
        contract, err = ct.published_contract_or_error(row["id"])
        assert contract is None and err["error"] == "contract_not_published"

    def test_purge_with_runs_is_rejected_and_nothing_changes(self, client, make_contract):
        row = make_contract(with_run=True)
        r = client.delete(f"/api/v1/structured/contracts/{row['id']}?purge=true")
        assert r.status_code == 400
        assert "留痕" in r.json()["detail"]
        kept = ct.get_contract(row["id"])
        assert kept is not None and kept["status"] == ct.CONTRACT_DRAFT  # 未被降级

    def test_delete_missing_contract_is_404(self, client):
        r = client.delete(f"/api/v1/structured/contracts/{uuid.uuid4()}")
        assert r.status_code == 404
