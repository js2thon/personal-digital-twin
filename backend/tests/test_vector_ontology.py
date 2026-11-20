"""向量本体 P0 单元测试（向量独立召回 + 本体标注 + 统一写入）。

不依赖 embedding 网关：所有向量调用均 monkeypatch。
写库类用例一律在事务内回滚，不污染真实数据。
"""
from __future__ import annotations

import inspect

import pytest

from app.modules.retrieval import service as retrieval
from app.modules.retrieval import vector_store as vs


# ---------------------------------------------------------------- 基础契约

def test_vector_store_enforces_tenant():
    """GV-01：search 必须传 tenant_id，调用方无法省略。"""
    store = vs.TextColumnCosineStore()
    with pytest.raises(ValueError):
        store.search([0.1] * 4, 5, tenant_id="")


def test_rrf_fuse_prefers_multi_channel():
    """RRF 纯函数：双通道命中的证据排在单通道之前。"""
    buckets = {
        "lexical": [{"id": "a", "score": 3.0}, {"id": "b", "score": 1.0}],
        "vector": [{"id": "b", "score": 0.9}, {"id": "c", "score": 0.8}],
    }
    out = vs.rrf_fuse(buckets, 3)
    assert out[0]["id"] == "b"
    assert set(out[0]["fusion_channels"]) == {"lexical", "vector"}
    assert out[0]["rrf_score"] > out[1]["rrf_score"]


def test_vector_store_search_has_no_limit_100(monkeypatch):
    """回归：向量召回不得再被 SQL LIMIT 100 截断。"""
    from app.db import session as db

    captured: dict = {}

    def fake_fetch_all(sql, params=None):
        captured["sql"] = sql
        return []

    monkeypatch.setattr(vs, "ensure_columns", lambda: True)
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    vs.TextColumnCosineStore().search([0.1] * 4, 5, tenant_id="t")

    assert "LIMIT 100" not in captured.get("sql", "")
    assert "entity_types" in captured.get("sql", "")  # 本体坐标随向量一起取回


# ---------------------------------------------------------------- 检索主路径

def test_documents_no_vector_degrades_to_lexical(monkeypatch):
    """网关异常时纯词法零阻塞，输出与改造前逐行一致。"""
    def boom(*args, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(retrieval.llm_client, "embed", boom)
    monkeypatch.setattr(
        retrieval, "_lexical_document_rows",
        lambda *a, **k: [{"id": "L1", "document_id": "d1", "content": "x",
                          "title": "T", "relevance": 5.0}],
    )
    diag: dict = {}
    out = retrieval._documents("q", [{"term": "x", "weight": 2.6}],
                               5, diag, {"tenant_id": "default"})
    assert len(out) == 1
    assert out[0]["via"] == "lexical" and out[0]["score"] == 5.0
    assert diag["vector_used"] is False
    assert diag["vector_recall_used"] is False
    assert diag["fusion"] == "lexical_only"
    assert "vector_error" in diag


def test_documents_vector_recall_without_lexical_terms(monkeypatch):
    """核心回归：词法 0 命中（terms=[]）时向量必须能独立召回。"""
    # 本测试聚焦 RRF 融合本身，隔离可选精排层（精排行为由 test_retrieval_quality 覆盖）
    monkeypatch.setattr(retrieval.settings, "rerank_model", "", raising=False)
    monkeypatch.setattr(retrieval, "_lexical_document_rows", lambda *a, **k: [])
    monkeypatch.setattr(
        retrieval.llm_client, "embed",
        lambda texts, **k: [[0.1] * 1024 for _ in texts],
    )

    class FakeStore:
        backend = "text"

        def search(self, vec, top_k, *, tenant_id, kinds=None, filters=None):
            assert tenant_id == "default"
            return [{
                "id": "V1", "ref_id": "V1", "document_id": "d1",
                "content": "语义命中", "title": "语义文档",
                "score": 0.77, "similarity": 0.77, "via": "vector",
                "entity_ids": ["e1"], "entity_types": ["Person"],
                "ontology_version": "v2.1.0",
            }]

    monkeypatch.setattr(vs, "get_vector_store", lambda: FakeStore())
    diag: dict = {}
    out = retrieval._documents("语义问题", [], 5, diag, {"tenant_id": "default"})

    assert len(out) == 1
    assert out[0]["via"] == "vector"           # 不是 lexical，说明向量独立召回
    assert out[0]["entity_types"] == ["Person"]
    assert out[0]["ontology_version"] == "v2.1.0"
    assert diag["vector_recall_used"] is True
    assert diag["vector_candidates"] == 1
    assert diag["vector_top_sim"] == 0.77
    assert diag["fusion"] == "rrf" and diag["rrf_k"] == 60


def test_documents_rrf_fusion_marks_both_channels(monkeypatch):
    """词法与向量同时命中同一切片时，via 标记为 fused。"""
    monkeypatch.setattr(retrieval.settings, "rerank_model", "", raising=False)
    lex = [{"id": "S1", "document_id": "d1", "content": "c", "title": "T",
            "relevance": 4.0}]
    monkeypatch.setattr(retrieval, "_lexical_document_rows", lambda *a, **k: lex)
    monkeypatch.setattr(
        retrieval.llm_client, "embed",
        lambda texts, **k: [[0.1] * 1024 for _ in texts],
    )

    class FakeStore:
        backend = "text"

        def search(self, vec, top_k, *, tenant_id, kinds=None, filters=None):
            return [dict(lex[0], ref_id="S1", score=0.8, similarity=0.8, via="vector")]

    monkeypatch.setattr(vs, "get_vector_store", lambda: FakeStore())
    diag: dict = {}
    out = retrieval._documents("q", [{"term": "x", "weight": 2.6}],
                               5, diag, {"tenant_id": "default"})
    assert len(out) == 1
    assert out[0]["via"] == "fused(lexical+vector)"
    assert set(out[0]["fusion_channels"]) == {"lexical", "vector"}


# ---------------------------------------------------------------- 写入链路

def test_embed_chunks_writes_ontology_coordinates(monkeypatch):
    """统一写入：本体坐标 + model/version/embedded_at 一应俱全（事务回滚）。"""
    from app.db import session as db
    from app.modules.embeddings import service as emb

    did = db.fetch_one("SELECT id FROM source.documents LIMIT 1")["id"]
    monkeypatch.setattr(
        retrieval, "detect_mentions",
        lambda text: [{"entity_id": "e-test", "entity_type": "Person"}],
    )
    monkeypatch.setattr(
        emb.llm_client, "embed",
        lambda texts, **k: [[0.1] * 1024 for _ in texts],
    )

    class Rollback(Exception):
        pass

    try:
        with db.transaction():
            n = emb.embed_chunks(did, [{"content": "测试内容", "locator": {}}],
                                 run_id="probe")
            row = db.fetch_one(
                "SELECT metadata, embedding_vec, embedding_model, embedding_version, "
                "embedded_at, entity_ids, entity_types, ontology_version "
                "FROM knowledge.semantic_chunks WHERE document_id = :d", {"d": did}
            )
            assert n == 1
            assert row["entity_ids"] == ["e-test"]
            assert row["entity_types"] == ["Person"]
            assert row["ontology_version"] == "v2.1.0"
            assert row["embedding_model"] == emb.settings.embedding_model
            assert row["embedding_version"] == emb.EMBEDDING_VERSION
            assert row["embedded_at"] is not None
            assert row["embedding_vec"]
            assert row["metadata"]["entity_ids"] == ["e-test"]
            raise Rollback()
    except Rollback:
        pass


def test_rechunk_uses_unified_writer():
    """静态守护：rechunk 不得再出现第二套 INSERT。"""
    from app.api.v1 import knowledge

    src = inspect.getsource(knowledge.rechunk_documents)
    assert "embed_chunks" in src
    assert "INSERT INTO knowledge.semantic_chunks" not in src


# ---------------------------------------------------------------- 自证端点

def test_vector_stats_endpoint():
    """只读自证端点：coverage 与 stale 如实输出，不编造 Recall。"""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, client=("127.0.0.1", 12345))
    resp = client.get("/api/v1/ontology/semantic/vector/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["backend"] == "text"
    assert "total" in data["chunks"] and "coverage" in data["chunks"]
    assert "stale" in data["chunks"]
