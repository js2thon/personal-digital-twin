"""Fact 字段契约（SIL-DC-05 / DC-08 / DC-09，P0-3）回归测试。

分两层：
* 纯函数层——类型推导、核验映射、覆盖率与合规判定，不依赖数据库；
* 实库层——补列结果、约束与触发器、写入路径自动填列。写入用例一律放在
  显式事务里并主动回滚，保证测试不留下任何业务数据。
"""
import pytest

from app.modules.knowledge import contract


# ---------------------------------------------------------------- 纯函数：类型推导

@pytest.mark.parametrize("value,expected", [
    ({"value": True}, "boolean"),
    ({"value": 12}, "integer"),
    ({"value": 1.5}, "number"),
    ({"value": "纯文本"}, "text"),
    ({"value": "2026-11-15"}, "date"),
    ({"value": "2026-11-15 10:30:00"}, "datetime"),
    ({"value": "2026-11-15T10:30:00+08:00"}, "datetime"),
    ({"value": "3天"}, "duration"),
    ({"value": "12 h"}, "duration"),
    ({"value": "f02cf927-af1c-427c-b09a-e3082f8b9ed3"}, "ref"),
    ({"value": {"nested": 1}}, "json"),
    ({"value": [1, 2]}, "json"),
    ({"value": None}, "json"),
])
def test_infer_datatype_covers_all_rules(value, expected):
    assert contract.infer_datatype(value) == expected


def test_infer_datatype_unwraps_value_envelope():
    """项目的客体约定是 `{"value": x}`：类型判定看 x，不看待壳子。"""
    assert contract.infer_datatype({"value": 3}) == "integer"
    assert contract.infer_datatype(3) == "integer"


def test_infer_datatype_never_returns_out_of_enum():
    samples = [None, True, 0, 1.0, "", "  ", "abc", {"a": 1}, [1], {"value": "x"}]
    for s in samples:
        assert contract.infer_datatype(s) in contract.DATATYPES


# ---------------------------------------------------------------- 纯函数：核验映射

@pytest.mark.parametrize("tier,expected", [
    (3, "human_confirmed"), (2, "machine_checked"), (1, "unverified"),
    (None, "unverified"), (0, "unverified"), ("bad", "unverified"),
])
def test_verification_for_tier(tier, expected):
    assert contract.verification_for_tier(tier) == expected


@pytest.mark.parametrize("tier,expected", [
    (3, "owner"), (2, "agent"), (1, "unknown"), (None, "unknown"),
])
def test_created_by_for_tier(tier, expected):
    assert contract.created_by_for_tier(tier) == expected


# ---------------------------------------------------------------- 纯函数：覆盖率与合规

def test_coverage_report_flags_missing_column():
    report = contract.coverage_report(
        10, {"tenant_id": 10}, spec=contract.FACT_FIELD_SPEC, present={"tenant_id"})
    by_col = {r["column"]: r for r in report}
    assert by_col["tenant_id"]["present"] is True
    assert by_col["tenant_id"]["coverage"] == 1.0
    # 缺列不能因为「计数为 0」被误判成「已具备」
    assert by_col["datatype"]["present"] is False
    assert by_col["datatype"]["gap"] is False


def test_coverage_report_marks_partial_required_field():
    report = contract.coverage_report(
        10, {"source_id": 4, "valid_to": 0},
        spec=contract.FACT_FIELD_SPEC, present={"source_id", "valid_to"})
    by_col = {r["column"]: r for r in report}
    assert by_col["source_id"]["coverage"] == 0.4
    assert by_col["source_id"]["gap"] is True
    assert by_col["valid_to"]["gap"] is False      # 非必填，缺不算缺口


def test_compliance_separates_structure_from_coverage():
    full = [dict(r, present=True, gap=False) for r in contract.FACT_FIELD_SPEC]
    public = [dict(r, present=True) for r in contract.PUBLIC_FIELD_SPEC]
    ok = contract.compliance(full, public)
    assert ok["structurally_compliant"] is True and ok["fully_covered"] is True

    partial = [dict(r, present=True, gap=(r["column"] == "source_id"))
               for r in contract.FACT_FIELD_SPEC]
    res = contract.compliance(partial, public)
    assert res["structurally_compliant"] is True
    assert res["fully_covered"] is False
    assert res["coverage_gaps"] == ["source_id"]

    missing = [dict(r, present=(r["column"] != "datatype"), gap=False)
               for r in contract.FACT_FIELD_SPEC]
    res2 = contract.compliance(missing, public)
    assert res2["structurally_compliant"] is False
    assert res2["missing_columns"] == ["datatype"]


# ---------------------------------------------------------------- 契约定义自身

def test_fact_field_spec_is_the_standard_fourteen():
    """DC-05 的 14 项标准字段：不多不少、列名不重复、必填 13 项（valid_to 可选）。"""
    assert len(contract.FACT_FIELD_SPEC) == 14
    cols = [r["column"] for r in contract.FACT_FIELD_SPEC]
    assert len(cols) == len(set(cols))
    required = [r for r in contract.FACT_FIELD_SPEC if r["required"]]
    assert len(required) == 13
    optional = [r["column"] for r in contract.FACT_FIELD_SPEC if not r["required"]]
    assert optional == ["valid_to"]


def test_public_field_spec_matches_dc08():
    assert [r["column"] for r in contract.PUBLIC_FIELD_SPEC] == [
        "id", "tenant_id", "created_at", "updated_at", "created_by", "status", "version"]


def test_bitemporal_columns_kept_apart():
    """Valid Time 与 Transaction Time 不得混用：recorded_at 不在有效时间列里。"""
    assert contract.BITEMPORAL_COLUMNS == ("valid_from", "valid_to", "recorded_at")
    assert "recorded_at" not in ("valid_from", "valid_to")


def test_enum_dictionaries_follow_dc09():
    assert contract.FACT_STATUSES == ("active", "contested", "superseded", "withdrawn")
    assert set(contract.VERIFICATION_STATUSES) == {
        "unverified", "machine_checked", "human_confirmed", "disputed"}
    assert "text" in contract.DATATYPES and "date" in contract.DATATYPES


# ---------------------------------------------------------------- 路由契约


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    # 伪造回环地址过 identity_gate；不使用 with 上下文，避免触发 bootstrap_schema
    return TestClient(app, client=("127.0.0.1", 12345))


def test_contract_route_rejects_bad_uuid():
    assert _client().get(
        "/api/v1/knowledge/facts/not-a-uuid/contract").status_code == 400


def test_contract_route_registered():
    paths = set(_client().app.openapi()["paths"])
    assert "/api/v1/knowledge/contracts/facts" in paths
    assert "/api/v1/knowledge/contracts/facts/backfill" in paths
    assert "/api/v1/knowledge/facts/{fact_id}/contract" in paths


# ---------------------------------------------------------------- 实库层


def _db_ready() -> bool:
    try:
        from app.db.session import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


@db_required
def test_live_columns_cover_all_standard_fields():
    from app.db.session import fetch_all

    cols = {r["column_name"] for r in fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'knowledge' AND table_name = 'facts'")}
    needed = {r["column"] for r in contract.FACT_FIELD_SPEC}
    needed |= {r["column"] for r in contract.PUBLIC_FIELD_SPEC}
    assert not (needed - cols), f"缺列：{sorted(needed - cols)}"


@db_required
def test_live_contract_status_is_structurally_compliant():
    status = contract.contract_status()
    assert status["compliance"]["missing_columns"] == []
    assert status["compliance"]["public_fields_missing"] == []
    assert status["compliance"]["structurally_compliant"] is True
    assert status["total_facts"] > 0
    for field in status["fields"]:
        assert field["present"] is True
        # 必填且非空（不可重建的 subject_id/valid_from 等按覆盖率单列，不在此断言）
        if field["column"] in ("tenant_id", "datatype", "verification_status",
                               "ontology_version", "created_at", "updated_at",
                               "created_by"):
            assert field["coverage"] == 1.0


@db_required
def test_live_enum_values_stay_in_dictionary():
    from app.db.session import fetch_all

    for row in fetch_all("SELECT DISTINCT status FROM knowledge.facts"):
        assert row["status"] in contract.FACT_STATUSES
    for row in fetch_all("SELECT DISTINCT verification_status FROM knowledge.facts"):
        assert row["verification_status"] in contract.VERIFICATION_STATUSES
    for row in fetch_all("SELECT DISTINCT datatype FROM knowledge.facts"):
        assert row["datatype"] in contract.DATATYPES


@db_required
def test_live_valid_window_and_updated_at_trigger():
    status = contract.contract_status()
    assert status["bitemporal"]["valid_window_violations"] == 0
    assert status["bitemporal"]["updated_at_trigger"] is True


@db_required
def test_live_backfill_dry_run_is_idempotent():
    """补列+回填已完成，再跑一次预检：派生列应为 0 待补。

    `source_id` / `evidence_id` 例外——无 claim 的事实本就没有证据链路，
    补不出来也不该编造，待补数恒等于「无 claim 的事实数」。
    """
    from app.db.session import fetch_one

    pending = contract.backfill(dry_run=True)["pending"]
    for col in ("tenant_id", "datatype", "verification_status", "created_by",
                "created_at", "updated_at", "ontology_version"):
        assert pending[col] == 0, (col, pending[col])
    unresolvable = int(fetch_one(
        "SELECT count(*) AS n FROM knowledge.facts f "
        "WHERE NOT EXISTS (SELECT 1 FROM knowledge.claim_evidence ce "
        "                  WHERE ce.claim_id = f.claim_id)")["n"])
    assert pending["source_id"] == unresolvable
    assert pending["evidence_id"] == unresolvable


@db_required
def test_write_path_fills_contract_columns():
    """写入路径自动填契约列，且整笔事务回滚后不留痕。"""
    from app.db import session as db
    from app.modules.ontology import service as ontology

    class _Rollback(Exception):
        pass

    fid = None
    try:
        with db.transaction():
            out = ontology.write_fact(
                predicate="contract_test_predicate",
                object_value={"value": "2026-11-15"},
                writer="owner",
            )
            assert out["action"] in ("inserted", "duplicate", "reinforced")
            fid = out["fact_id"]
            row = contract.contract_row(fact_id=fid)
            assert row["tenant_id"] == contract.DEFAULT_TENANT
            assert row["datatype"] == "date"
            assert row["verification_status"] == contract.VERIFY_HUMAN_CONFIRMED
            assert row["created_by"] == "owner"
            assert row["ontology_version"]
            assert row["created_at"] is not None and row["updated_at"] is not None
            raise _Rollback
    except _Rollback:
        pass
    if fid:
        assert db.fetch_one(
            "SELECT count(*) AS n FROM knowledge.facts WHERE id = CAST(:i AS uuid)",
            {"i": fid})["n"] == 0
