"""统一语义运行时（SIL B2）回归测试。

沿用项目既有风格：以纯函数单测为主，不依赖 conftest、不启动 startup 连库。
涉及 HTTP 的用例用 TestClient(app, client=("127.0.0.1", 12345)) 伪造回环地址
过 identity_gate，且不使用 with 上下文管理器（避免触发 bootstrap_schema）。
"""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.main import app  # noqa: E402
from app.modules.semantic_runtime import service as sem  # noqa: E402
from app.modules.semantic_runtime.context import (  # noqa: E402
    CONTEXT_FIELDS, SemanticContext, from_request, system_context,
)


def _request(headers: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "POST", "path": "/api/v1/semantic/search",
             "headers": raw, "query_string": b"", "client": ("127.0.0.1", 12345),
             "server": ("testserver", 80), "scheme": "http"}
    req = Request(scope)
    req.state.actor = "owner"
    return req


def _client() -> TestClient:
    return TestClient(app, client=("127.0.0.1", 12345))


# ---------------------------------------------------------------- AC-01 上下文

def test_context_auto_derives_all_six_fields():
    ctx = from_request(_request())
    payload = ctx.as_dict()
    for field in CONTEXT_FIELDS:
        assert field in payload and payload[field] not in (None, "")
    assert payload["context_source"] == "derived"
    assert set(payload["derived_fields"]) == set(CONTEXT_FIELDS)
    assert payload["actor"] == "owner"


def test_context_explicit_headers_not_derived():
    ctx = from_request(_request({
        "X-Tenant-Id": "tenant_001", "X-User-Id": "user_001",
        "X-Request-Id": "req_fixed", "X-Trace-Id": "trc_fixed",
        "X-Permission-Context": '{"role":"analyst","scope":["organization"]}',
        "X-Schema-Version": "1.2.3",
    }))
    assert ctx.tenant_id == "tenant_001"
    assert ctx.schema_version == "1.2.3"
    assert ctx.permission_context["role"] == "analyst"
    assert ctx.derived == []
    assert ctx.as_dict()["context_source"] == "explicit"


def test_strict_mode_rejects_missing_context():
    with pytest.raises(HTTPException) as ei:
        from_request(_request({"X-Semantic-Strict": "1"}))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "MISSING_SEMANTIC_CONTEXT"
    assert set(ei.value.detail["missing"]) == set(CONTEXT_FIELDS)


def test_invalid_permission_context_is_400():
    with pytest.raises(HTTPException) as ei:
        from_request(_request({"X-Permission-Context": "not-json"}))
    assert ei.value.status_code == 400


def test_system_context_is_complete():
    ctx = system_context()
    assert ctx.user_id == "system:agent"
    assert all(getattr(ctx, f) not in (None, "") for f in CONTEXT_FIELDS)


# ---------------------------------------------------------------- 融合与序列化

def test_rrf_dedupes_across_channels():
    a = {"kind": "fact", "id": "f1", "score": 0.9, "data": {}}
    b = {"kind": "fact", "id": "f1", "score": 0.5, "data": {}}
    c = {"kind": "fact", "id": "f2", "score": 0.8, "data": {}}
    out = sem._rrf({"keyword": [a, c], "structured": [b]}, 10)
    assert len(out) == 2
    assert out[0]["id"] == "f1"
    assert set(out[0]["channels"]) == {"keyword", "structured"}
    assert out[0]["rrf_score"] > 0


def test_public_strips_private_keys_and_serializes():
    import uuid
    out = sem.public({"_legacy": {"facts": []}, "a": uuid.UUID(int=1), "b": 2})
    assert "_legacy" not in out
    assert out["a"] == str(uuid.UUID(int=1))
    assert out["b"] == 2


def test_reason_decision_structure_and_separate_confidences():
    ctx = system_context()
    out = sem.reason(ctx, {"risk_level": "high", "action": "send_email",
                           "facts": [{"fact_id": "f1", "confidence": 0.8}]})
    for key in ("decision_id", "decision", "risk_level", "reasons", "rules_triggered",
                "evidence_ids", "missing_information", "recommended_action",
                "requires_human_confirmation"):
        assert key in out
    assert out["decision"] == "REJECT"  # send_email 在 TOOL_WHITELIST 中为 denied
    assert out["requires_human_confirmation"] is True
    conf = out["confidence"]
    assert conf["fact_confidence"] == 0.8
    assert conf["rule_confidence"] == 1.0
    assert conf["model_confidence"] is None


def test_reason_insufficient_information_without_evidence():
    out = sem.reason(system_context(), {"risk_level": "low", "facts": []})
    assert out["decision"] == "INSUFFICIENT_INFORMATION"
    assert out["missing_information"]


def test_search_rejects_empty_query_and_all_channels_off():
    ctx = system_context()
    with pytest.raises(ValueError):
        sem.search(ctx, {"query": ""})
    with pytest.raises(ValueError):
        sem.search(ctx, {"query": "x", "retrieval": {c: False for c in sem.CHANNELS}})


# ---------------------------------------------------------------- 路由与 HTTP

def test_router_registers_contract_endpoints():
    from app.api.v1 import semantic as api

    paths = {r.path for r in api.router.routes}
    for suffix in ("/semantic/understand", "/semantic/search", "/semantic/query",
                   "/semantic/reason", "/semantic/entity/resolve",
                   "/semantic/graph/traverse", "/semantic/evidence",
                   "/semantic/ontology/schema", "/semantic/ontology/proposals",
                   "/semantic/ontology/versions", "/semantic/meta",
                   "/ontology/versions"):
        assert suffix in paths


def test_app_openapi_contains_semantic_routes():
    paths = app.openapi()["paths"]
    assert "/api/v1/semantic/search" in paths
    assert "/api/v1/ontology/versions" in paths


def test_meta_endpoint_exposes_context_and_channel_status():
    resp = _client().get("/api/v1/semantic/meta")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runtime"] == "semantic_runtime"
    assert len(body["contract_endpoints"]) == 10
    assert body["channels"] == list(sem.CHANNELS)
    for field in CONTEXT_FIELDS:
        assert field in body["context"]
    assert "indexed_chunks" in body["vector"]


def test_search_empty_query_returns_400():
    resp = _client().post("/api/v1/semantic/search", json={"query": ""})
    assert resp.status_code == 400


def test_strict_header_missing_context_returns_400():
    resp = _client().post("/api/v1/semantic/understand", json={"text": "你好"},
                          headers={"X-Semantic-Strict": "1"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "MISSING_SEMANTIC_CONTEXT"


def test_post_idempotency_replays_same_request_id():
    c = _client()
    headers = {"X-Request-Id": "req_semantic_fixed"}
    first = c.post("/api/v1/semantic/understand", json={"text": "某某负责什么"}, headers=headers)
    second = c.post("/api/v1/semantic/understand", json={"text": "某某负责什么"}, headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert second.json().get("idempotent_replay") is True
    assert first.json()["query_id"] == "req_semantic_fixed"


def test_legacy_ontology_routes_still_work():
    """语义层接入不得破坏既有的 /ontology/schema 契约。"""
    resp = _client().get("/api/v1/ontology/schema")
    assert resp.status_code == 200, resp.text
    assert "version" in resp.json()


# ---------------------------------------------------------------- Agent 收口

def test_three_graphs_no_longer_import_legacy_retriever():
    from app.modules.twin.graphs import decision, qa, reply

    for mod in (qa, reply, decision):
        assert hasattr(mod, "semantic"), f"{mod.__name__} 未接入统一语义运行时"
        assert not hasattr(mod, "retrieval"), f"{mod.__name__} 仍直连底层检索器"


def test_agent_retrieve_keeps_legacy_shape():
    result = sem.agent_retrieve("某某负责什么", ["facts", "memories", "documents"], 2)
    assert set(("facts", "memories", "documents", "diagnostics")) <= set(result)
    diag = result["diagnostics"]
    assert "semantic_runtime" in diag or "semantic_runtime_degraded" in diag
