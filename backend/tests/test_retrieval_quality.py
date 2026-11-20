"""检索质量门禁：精排拒答、降级路径、配置来源与网关契约。

不依赖真实 gateway / golden 集，可在离线 CI 跑。
真实指标由 backend/scripts/eval_doc_retrieval.py 单独产出（见方案文档 §9）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # tests/ -> backend/ -> 仓库根
sys.path.insert(0, str(ROOT))

from app.config import Settings, settings  # noqa: E402
from app.modules.llm.client import llm_client  # noqa: E402
from app.modules.retrieval import service as retrieval  # noqa: E402


def _items() -> list[dict]:
    return [{"id": str(i), "document_id": f"doc-{i}", "content": f"text-{i}",
             "title": f"t{i}", "score": 2.0 + i, "via": "lexical",
             "fusion_channels": ["lexical"], "rrf_score": 0.03}
            for i in range(3)]


def test_env_file_anchored_to_repo_root():
    """CWD 漂移防护：env_file 必须是仓库根的绝对路径。"""
    env_file = Settings.model_config["env_file"]
    assert Path(env_file).is_absolute()
    assert Path(env_file).name == ".env"
    assert Path(env_file).parent == ROOT


def test_rerank_disabled_returns_original(monkeypatch):
    monkeypatch.setattr(settings, "rerank_model", "", raising=False)
    diag: dict = {}
    out = retrieval._rerank_candidates("q", _items(), diag)
    assert out and out[0]["via"] == "lexical"
    assert diag["rerank_used"] is False


def test_rerank_rejects_when_top_score_below_threshold(monkeypatch):
    """无覆盖 query：top1 低于门槛 → 返回空，绝不硬塞无关文档。"""
    monkeypatch.setattr(settings, "rerank_model", "test-reranker", raising=False)
    monkeypatch.setattr(settings, "rerank_min_score", 0.05, raising=False)
    monkeypatch.setattr(llm_client, "rerank",
                        lambda q, docs, **kw: [(0, 0.0273), (1, 0.002)])
    diag: dict = {}
    assert retrieval._rerank_candidates("语料没覆盖的问题", _items(), diag) == []
    assert diag["rerank_used"] is True
    assert diag["rerank_rejected"] is True
    assert diag["rerank_top_score"] == 0.0273


def test_rerank_keeps_every_candidate_above_top_gate(monkeypatch):
    """只按 top1 拒答：低分但相关的候选不能被逐条过滤误杀。"""
    monkeypatch.setattr(settings, "rerank_model", "test-reranker", raising=False)
    monkeypatch.setattr(settings, "rerank_min_score", 0.05, raising=False)
    monkeypatch.setattr(llm_client, "rerank",
                        lambda q, docs, **kw: [(0, 0.9), (2, 0.0545), (1, 0.01)])
    diag: dict = {}
    out = retrieval._rerank_candidates("q", _items(), diag)
    assert [x["id"] for x in out] == ["0", "2", "1"]
    assert out[1]["rerank_score"] == 0.0545
    assert out[1]["score"] == 0.0545          # 量纲已统一为相关性分
    assert out[1]["via"].endswith("+rerank")
    assert diag["rerank_rejected"] is False


def test_rerank_gateway_failure_degrades_without_blocking(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("gateway down")
    monkeypatch.setattr(settings, "rerank_model", "test-reranker", raising=False)
    monkeypatch.setattr(llm_client, "rerank", boom)
    diag: dict = {}
    out = retrieval._rerank_candidates("q", _items(), diag)
    assert len(out) == 3
    assert diag["rerank_used"] is False
    assert "gateway down" in diag["rerank_error"]


def test_gateway_contract_uses_rerank_endpoint_and_documents_key(monkeypatch):
    captured: dict = {}

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.8}]}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Resp()

    monkeypatch.setattr(settings, "rerank_model", "test-reranker", raising=False)
    monkeypatch.setattr(llm_client._client, "post", fake_post)
    out = llm_client.rerank("q", ["a", "b"], timeout=5)
    assert captured["url"].endswith("/rerank")
    assert "/v1/v1/" not in captured["url"]
    assert "documents" in captured["json"] and "inputs" not in captured["json"]
    assert out == [(1, 0.8)]


def test_metrics_never_fabricate_empty_denominator():
    from app.modules.evaluation import metrics as mx
    assert mx.recall_at_k(["a"], [], 5) is None
    assert mx.mrr([]) is None
    assert mx.ndcg_at_k(["a"], [], 5) is None
