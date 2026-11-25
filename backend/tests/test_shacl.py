"""SHACL 写入侧校验（P1-2 / SIL-DC-04）回归测试。

分两层：
* 纯逻辑层——Shape 派生、逐条约束判定（datatype / in / minCount / pattern /
  windowOrder / endpoint）、warn·strict·off 三种模式、统计与最近违规；
* 实库层——写入路径（ontology.write_fact）是否真的带上校验结论，strict 模式
  是否把非法写入挡在库外。写库用例包事务并回滚。
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.ontology import shacl


def _db_ready() -> bool:
    try:
        from app.db.session import fetch_one

        fetch_one("SELECT 1 AS ok")
        return True
    except Exception:  # noqa: BLE001
        return False


db_required = pytest.mark.skipif(not _db_ready(), reason="需要可用的 PostgreSQL")


@pytest.fixture(autouse=True)
def _warn_mode(monkeypatch):
    """默认按 warn 跑；需要别的模式就在用例里再 patch 一次。"""
    monkeypatch.setenv("SHACL_MODE", "warn")
    shacl.reset_stats()
    yield
    shacl.reset_stats()


def _fact(**kw) -> dict:
    base = {"predicate": "works_at", "object_value": {"text": "公司"},
            "confidence": 0.9, "trust_tier": 2, "status": "active",
            "visibility": "owner"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- Shape 派生

def test_shapes_derived_from_registry_schema():
    shapes = shacl.current_shapes(refresh=True)
    assert shapes["shapes_version"] == "shacl-1.0"
    assert shapes["target"] == "knowledge.facts"
    assert shapes["schema_version"]
    for path in ("predicate", "object_value", "confidence", "trust_tier",
                 "status", "visibility"):
        assert path in shapes["properties"], path
    assert "works_at" in shapes["relation_endpoints"]
    assert shapes["relation_endpoints"]["works_at"]["target"] == "OrganizationUnit"


def test_shapes_override_file_merges(monkeypatch, tmp_path):
    """shapes.yaml 只写要改的项：properties 逐项覆盖，其余保持派生值。"""
    override = tmp_path / "shapes.yaml"
    override.write_text(
        "properties:\n"
        "  confidence:\n"
        "    maxInclusive: 0.5\n"
        "    severity: warning\n",
        encoding="utf-8")
    base = shacl.shapes_from_schema({"schema_version": "vX"})
    merged = shacl._merge_override(base, override)
    assert merged["properties"]["confidence"]["maxInclusive"] == 0.5
    assert merged["properties"]["confidence"]["severity"] == "warning"
    assert merged["properties"]["predicate"]["minCount"] == 1   # 未覆盖项不动
    assert merged["schema_version"] == "vX"


# ---------------------------------------------------------------- 逐条约束

def test_valid_fact_conforms():
    report = shacl.validate(_fact(), subject_type="Person",
                            object_type="OrganizationUnit")
    assert report["conforms"] is True and report["violations"] == []


@pytest.mark.parametrize("patch,path,constraint", [
    ({"confidence": 1.5}, "confidence", "sh:maxInclusive"),
    ({"confidence": -0.1}, "confidence", "sh:minInclusive"),
    ({"trust_tier": 9}, "trust_tier", "sh:in"),
    ({"status": "deleted"}, "status", "sh:in"),
    ({"visibility": "secret"}, "visibility", "sh:in"),
    ({"predicate": "bad predicate!"}, "predicate", "sh:pattern"),
    ({"predicate": ""}, "predicate", "sh:minCount"),
    ({"confidence": "高"}, "confidence", "sh:datatype"),
])
def test_each_constraint_is_enforced(patch, path, constraint):
    report = shacl.validate(_fact(**patch))
    assert report["conforms"] is False
    hits = [(v["path"], v["constraint"]) for v in report["violations"]]
    assert (path, constraint) in hits, hits


def test_window_order_violation():
    report = shacl.validate(_fact(valid_from="2026-05-01", valid_to="2026-01-01"))
    assert any(v["constraint"] == "sh:windowOrder" for v in report["violations"])


def test_window_order_accepts_correct_order():
    report = shacl.validate(_fact(valid_from="2026-01-01", valid_to="2026-05-01"))
    assert report["conforms"] is True


def test_relation_endpoint_r2():
    """R2：关系两端类型必须匹配 Schema 的 source/target。"""
    bad = shacl.validate(_fact(), subject_type="Organization",
                         object_type="Project")
    hits = [v for v in bad["violations"] if v["constraint"] == "sh:endpoint"]
    assert len(hits) == 2
    good = shacl.validate(_fact(), subject_type="Person",
                          object_type="OrganizationUnit")
    assert good["conforms"] is True


def test_unknown_predicate_is_not_blocking_by_default():
    report = shacl.validate(_fact(predicate="department-2"))
    assert report["conforms"] is True          # 未开启 predicate_in_schema_only


def test_predicate_in_schema_only_flag():
    shapes = shacl.current_shapes(refresh=True)
    strict_shapes = {**shapes,
                    "constraints": {**shapes["constraints"],
                                    "predicate_in_schema_only": True}}
    report = shacl.validate(_fact(predicate="department-2"), shapes=strict_shapes)
    assert any(v["constraint"] == "sh:closed" for v in report["violations"])
    assert report["blocking"] is False          # warning 级别，不阻断


def test_decimal_from_db_is_accepted():
    """回归：pg 回读 NUMERIC 是 Decimal，不能因为类型不是 float 就判违规。"""
    report = shacl.validate(_fact(confidence=Decimal("0.85"),
                                  trust_tier=Decimal("2")))
    assert report["conforms"] is True, report["violations"]


def test_empty_object_value_is_warning_only():
    report = shacl.validate(_fact(object_value={}))
    assert report["conforms"] is False
    assert report["blocking"] is False          # warning，不在 strict 下拦
    assert report["violations"][0]["constraint"] == "sh:nonEmpty"


# ---------------------------------------------------------------- 模式

def test_warn_mode_records_but_does_not_block():
    verdict = shacl.check_write(_fact(confidence=2))
    assert verdict["mode"] == "warn"
    assert verdict["conforms"] is False
    assert verdict["blocked"] is False
    assert shacl.stats()["checks"] == 1
    assert shacl.stats()["by_constraint"]["sh:maxInclusive"] == 1


def test_strict_mode_blocks(monkeypatch):
    monkeypatch.setenv("SHACL_MODE", "strict")
    verdict = shacl.check_write(_fact(confidence=2))
    assert verdict["mode"] == "strict"
    assert verdict["blocked"] is True
    with pytest.raises(shacl.ShaclViolation) as exc:
        raise shacl.ShaclViolation(verdict)
    assert "confidence" in str(exc.value)


def test_off_mode_skips_everything(monkeypatch):
    monkeypatch.setenv("SHACL_MODE", "off")
    verdict = shacl.check_write(_fact(confidence=99))
    assert verdict["conforms"] is True and verdict["blocked"] is False
    assert shacl.stats()["checks"] == 0


def test_invalid_mode_falls_back_to_warn(monkeypatch):
    monkeypatch.setenv("SHACL_MODE", "whatever")
    assert shacl.mode() == "warn"


def test_recent_violations_are_kept():
    shacl.check_write(_fact(trust_tier=9))
    shacl.check_write(_fact(trust_tier=9))
    items = shacl.recent(5)
    assert len(items) == 2
    assert items[0]["violations"][0]["path"] == "trust_tier"


# ---------------------------------------------------------------- 抽样体检

@db_required
def test_audit_facts_reports_distribution():
    from app.db import session as _db
    active = _db.fetch_one(
        "SELECT COUNT(*) AS n FROM knowledge.facts WHERE status = 'active'")
    if not active or not active["n"]:
        # 库内可能已被 console:batch-unpublish 全量下架；环境状态不是代码回归，照实跳过。
        pytest.skip("库内当前无 active 事实（可能已被批量下架）")
    report = shacl.audit_facts(limit=100)
    assert report["checked"] > 0
    assert 0.0 <= report["conform_rate"] <= 1.0
    assert isinstance(report["by_constraint"], dict)
    assert report["shapes"]["version"] == shacl.current_shapes()["shapes_version"]


# ---------------------------------------------------------------- 写入路径

@db_required
def test_write_fact_returns_shacl_verdict():
    from app.db import session as db
    from app.modules.ontology import service as ontology

    class _Rollback(Exception):
        pass

    try:
        with db.transaction():
            out = ontology.write_fact(predicate="shacl_probe_predicate",
                                      object_value={"value": "x"}, writer="owner")
            assert "shacl" in out
            assert out["shacl"]["mode"] in shacl.MODES
            assert out["shacl"]["conforms"] is True
            raise _Rollback
    except _Rollback:
        pass


@db_required
def test_strict_mode_blocks_write_before_touching_db(monkeypatch):
    from app.db import session as db
    from app.modules.ontology import service as ontology

    monkeypatch.setenv("SHACL_MODE", "strict")
    before = db.fetch_one("SELECT count(*)::int AS n FROM knowledge.facts")["n"]
    with pytest.raises(shacl.ShaclViolation):
        ontology.write_fact(predicate="shacl_probe_predicate",
                            object_value={"value": "x"},
                            confidence=5, writer="owner")
    after = db.fetch_one("SELECT count(*)::int AS n FROM knowledge.facts")["n"]
    assert before == after, "被拦截的写入不得落库"


# ---------------------------------------------------------------- 装配层

def test_shapes_routes_registered():
    pytest.importorskip("fastapi")
    from app.api.v1 import ontology_semantic as api

    paths = {r.path for r in api.router.routes}
    for path in ("/ontology/semantic/shapes", "/ontology/semantic/shapes/validate",
                 "/ontology/semantic/shapes/stats", "/ontology/semantic/shapes/audit"):
        assert path in paths, path
