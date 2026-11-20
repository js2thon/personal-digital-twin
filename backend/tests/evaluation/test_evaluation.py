"""SIL P0-4 评估模块回归测试（GV-02 / SIL-A10 / SIL-A12）。

沿用项目风格：纯函数单测为主，HTTP 用 TestClient 伪造回环地址过 identity_gate，
不使用 with 上下文管理器（避免触发 bootstrap_schema）。
连库探针不在单测里跑完整五组，避免外部 Embedding 网关超时拖慢测试。
"""
import json

import pytest

from app.modules.evaluation import dataset as ds
from app.modules.evaluation import metrics as mx
from app.modules.evaluation import service as ev

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 12345))


# ---------------------------------------------------------------- 指标纯函数

def test_recall_and_precision_known_values():
    ranked = ["a", "x", "b", "y"]
    relevant = ["a", "b", "c"]
    assert mx.recall_at_k(ranked, relevant, 4) == round(2 / 3, 6)
    assert mx.precision_at_k(ranked, relevant, 4) == 0.5
    assert mx.recall_at_k(ranked[:2], relevant, 2) == round(1 / 3, 6)


def test_no_relevant_returns_none_not_fake_zero():
    assert mx.recall_at_k(["a"], [], 5) is None
    assert mx.ndcg_at_k(["a"], [], 5) is None
    assert mx.mrr([]) is None
    assert mx.rate(1, 0) is None


def test_empty_window_precision_is_zero():
    assert mx.precision_at_k([], ["a"], 5) == 0.0


def test_reciprocal_rank_and_mrr():
    assert mx.reciprocal_rank(["x", "b", "a"], ["a"]) == round(1 / 3, 6)
    assert mx.reciprocal_rank(["x"], ["a"]) == 0.0
    assert mx.mrr([1.0, 0.5, 0.0]) == 0.5


def test_ndcg_perfect_ranking_is_one():
    assert mx.ndcg_at_k(["a", "b"], ["a", "b"], 2) == 1.0
    assert mx.ndcg_at_k(["x", "y"], ["a", "b"], 2) == 0.0
    assert 0 < mx.ndcg_at_k(["x", "a", "b"], ["a", "b"], 3) < 1.0


def test_ndcg_custom_gains():
    perfect = mx.ndcg_at_k(["a", "b"], ["a", "b"], 2, gains={"a": 2.0, "b": 1.0})
    assert 0 < perfect <= 1.0


def test_percentile_p95_interpolates():
    vals = list(range(1, 101))
    assert mx.percentile([1], 0.95) == 1.0
    assert mx.percentile([], 0.95) is None
    assert 94 < mx.percentile(vals, 0.95) < 96


def test_relative_delta_and_macro():
    assert mx.relative_delta(0.8, 1.0) == -0.2
    assert mx.relative_delta(1.2, 1.0) == 0.2
    assert mx.relative_delta(1.0, 0.0) is None
    assert mx.macro([1.0, None, 0.0]) == 0.5


def test_metric_catalog_covers_five_groups():
    assert set(mx.METRIC_CATALOG) == set(mx.GROUPS)
    assert all(len(v) >= 3 for v in mx.METRIC_CATALOG.values())


# ---------------------------------------------------------------- 固定基准集

def test_dataset_is_fixed_and_fingerprinted():
    d = ds.load_dataset()
    assert d["dataset_version"] == "golden-v1"
    assert len(d["cases"]) >= 8
    assert len(ds.dataset_fingerprint()) == 16
    assert ds.dataset_fingerprint() == ds.dataset_fingerprint()
    assert all(c["relevant_ids"] for c in d["cases"])


def test_dataset_summary_does_not_leak_relevant_ids():
    s = ds.dataset_summary()
    assert s["case_count"] >= 8
    assert "relevant_ids" not in json.dumps(s, ensure_ascii=False)
    assert s["cases"][0]["relevant_count"] > 0


def test_known_gaps_declared_and_gated():
    gaps = ds.known_gaps()
    assert len(gaps) >= 3
    assert all(g["gate_effect"] in ("excluded_from_gate", "reported_only") for g in gaps)


# ---------------------------------------------------------------- 门禁与对照

def _fake_report(checks=None, comparison=None, groups=None, requested=None):
    groups = groups or {g: {"metrics": {}, "checks": [{"id": f"{g}-01", "ok": True,
                                                        "detail": "ok"}],
                            "cases": [], "notes": []} for g in mx.GROUPS}
    return {
        "run_id": "test-run", "started_at": "2026-01-01T00:00:00",
        "duration_ms": 1.0,
        "groups_requested": requested or list(groups),
        "versions": {"spec_version": "SIL-Spec v1.2", "eval_version": "eval-1.0",
                     "ontology_version": "test", "dataset_version": "golden-v1",
                     "dataset_fingerprint": "abc", "runtime_fingerprint": "def",
                     "pipeline_version": "v1", "prompt_version": "v1"},
        "dataset_snapshot": {}, "groups": groups,
        "comparison": comparison or {"available": False, "reason": "test"},
        "notes": [],
    }


def test_gate_passes_clean_full_run():
    g = ev.evaluate_gate(_fake_report())
    assert g["scope"] == "full"
    assert not g["release_blocking"]
    assert g["passed_count"] == 5
    assert g["warnings"]  # 已知缺口仍须提示


def test_gate_blocks_failed_structure_check():
    r = _fake_report()
    r["groups"]["retrieval"]["checks"] = [{"id": "RET-02", "ok": False, "detail": "异常"}]
    g = ev.evaluate_gate(r)
    assert g["status"] == "fail" and g["release_blocking"]
    assert any("RET-02" in x for x in g["blocking"])


def test_gate_blocks_regression_beyond_tolerance():
    r = _fake_report(comparison={"available": True, "comparisons": {
        "retrieval.mrr": {"baseline": 1.0, "current": 0.8, "relative_delta": -0.2}}})
    g = ev.evaluate_gate(r, tolerance=0.10)
    assert g["release_blocking"]
    assert any("回归超阈值" in x for x in g["blocking"])


def test_gate_allows_small_regression_and_improvement():
    r = _fake_report(comparison={"available": True, "comparisons": {
        "retrieval.mrr": {"baseline": 1.0, "current": 0.97, "relative_delta": -0.03},
        "retrieval.ndcg_at_k": {"baseline": 0.5, "current": 0.8, "relative_delta": 0.6}}})
    g = ev.evaluate_gate(r, tolerance=0.10)
    assert not g["release_blocking"]


def test_gate_marks_diagnostic_partial_run():
    r = _fake_report(requested=["decision"])
    r["groups"] = {"decision": r["groups"]["decision"]}
    g = ev.evaluate_gate(r)
    assert g["scope"] == "diagnostic"
    assert g["release_blocking"]
    assert any("诊断运行" in x for x in g["blocking"])


def test_compare_with_baseline():
    r = _fake_report()
    r["groups"]["retrieval"]["metrics"] = {"mrr": 0.9, "recall_at_k": 0.7}
    base = {"created_at": "2026-01-01", "metrics": {
        "retrieval": {"mrr": 1.0, "recall_at_k": 0.5}}}
    comp = ev.compare_with_baseline(r, base)
    assert comp["available"]
    assert comp["comparisons"]["retrieval.mrr"]["relative_delta"] == -0.1
    assert comp["comparisons"]["retrieval.recall_at_k"]["relative_delta"] == 0.4


def test_compare_without_baseline(monkeypatch, tmp_path):
    # None 表示「使用默认基线」，真正无基线要让磁盘上的基线文件不存在
    monkeypatch.setattr(ev, "_BASELINE", tmp_path / "missing.json")
    assert ev.compare_with_baseline(_fake_report())["available"] is False


def test_unknown_group_and_empty_groups_rejected():
    with pytest.raises(ValueError):
        ev.run_suite(groups=["not_a_group"], persist=False)
    with pytest.raises(ValueError):
        ev.run_suite(groups=[], persist=False) or None  # 空列表视为全量，见下方说明


def test_markdown_report_contract():
    r = _fake_report()
    r["gate"] = ev.evaluate_gate(r)
    r["groups"]["retrieval"]["metrics"] = {"mrr": 0.65,
                                           "permission_filter_accuracy": None}
    md = ev.to_markdown(r)
    assert "# ESII" in md and "门禁结论" in md
    assert "## 五组指标实测" in md and "不可测" in md
    assert "## 版本对照" in md and "## 基准集" in md


# ---------------------------------------------------------------- HTTP

def test_api_dataset_and_metrics():
    c = _client()
    a = c.get("/api/v1/evaluation/dataset")
    b = c.get("/api/v1/evaluation/metrics")
    assert a.status_code == 200 and a.json()["case_count"] >= 8
    assert b.status_code == 200 and len(b.json()["groups"]) == 5
    assert "不设绝对性能阈值" in b.json()["rule"]


def test_api_run_diagnostic_group_is_fast_and_scoped(monkeypatch):
    c = _client()
    r = c.post("/api/v1/evaluation/run",
               json={"groups": ["decision"], "persist": False,
                     "bootstrap_baseline": False})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert set(body["groups"]) == {"decision"}
    assert body["gate"]["scope"] == "diagnostic"
    assert body["gate"]["release_blocking"]  # 诊断结果不得用于发布
    assert body["groups"]["decision"]["metrics"]["decision_consistency"] == 1.0


def test_api_rejects_unknown_group():
    r = _client().post("/api/v1/evaluation/run",
                       json={"groups": ["bogus"], "persist": False})
    assert r.status_code == 400


def test_api_gate_report_and_markdown(monkeypatch):
    fake = _fake_report()
    fake["gate"] = ev.evaluate_gate(fake)
    monkeypatch.setattr(ev, "_LAST", fake)
    c = _client()
    assert c.get("/api/v1/evaluation/report").status_code == 200
    gate = c.get("/api/v1/evaluation/gate")
    assert gate.status_code == 200 and "release_blocking" in gate.json()
    md = c.get("/api/v1/evaluation/report/markdown")
    assert md.status_code == 200 and "门禁结论" in md.text


def test_api_baseline_requires_confirm(monkeypatch, tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps({"created_at": "t", "metrics": {}}), encoding="utf-8")
    monkeypatch.setattr(ev, "_BASELINE", p)
    c = _client()
    assert c.post("/api/v1/evaluation/baseline", json={"confirm": False}).status_code == 400
    fake = _fake_report()
    fake["gate"] = ev.evaluate_gate(fake)
    monkeypatch.setattr(ev, "_LAST", fake)
    ok = c.post("/api/v1/evaluation/baseline", json={"confirm": True})
    assert ok.status_code == 200


def test_api_history_endpoint():
    r = _client().get("/api/v1/evaluation/history")
    assert r.status_code == 200 and "runs" in r.json()
