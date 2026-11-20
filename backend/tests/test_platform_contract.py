"""V1.3 换栈 Phase 0 回归：只读 IdentityContext + CQ 存储禁降级闸门。

覆盖 Sprint 1 三条验收铁律中本轮真实落地的部分：
- `/me` 身份来自全局中间件，请求体提权字段不参与判定、只读、不可反写；
- `/health/cq-store` 如实报出实际 store / 降级原因 / 库连通性；
- production 下显式 memory 与隐式降级都 fail-fast，dev 行为逐行不变。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.ontology.cq_v2 import store as st


@pytest.fixture()
def client():
    # 回环地址让 auth.current_actor 视为本人（单主体部署既有契约）
    return TestClient(app, client=("127.0.0.1", 12345), raise_server_exceptions=False)


# ---------------------------------------------------------------- /me

def test_me_returns_readonly_identity_from_global_middleware(client):
    r = client.get("/api/v1/me")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["source"] == "global_middleware"
    assert d["writable"] is False
    assert d["request_body_trusted"] is False
    ident = d["identity"]
    for key in ("tenant_id", "user_id", "actor", "role", "scope",
                "data_classification", "is_owner_class"):
        assert key in ident
    assert ident["is_owner_class"] is True           # 回环无头回落 owner
    assert isinstance(ident["scope"], list)
    ctx = d["context"]
    assert ctx["request_id"] and ctx["trace_id"]
    assert ctx["schema_version"]
    assert "\x00" not in str(d)                      # fail-closed 哨兵不得外泄
    assert "is_admin" not in d and "authorized_execution" not in d


def test_me_context_headers_match_response(client):
    r = client.get("/api/v1/me", headers={
        "X-Tenant-Id": "default", "X-User-Id": "u-1",
        "X-Request-Id": "req_fixed", "X-Trace-Id": "trc_fixed",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["context"]["request_id"] == "req_fixed"
    assert d["context"]["trace_id"] == "trc_fixed"
    assert d["identity"]["user_id"] == "u-1"
    assert r.headers.get("x-semantic-request-id") == "req_fixed"


def test_me_query_params_cannot_elevate(client):
    """请求参数里的 is_admin / tenant_id 不得改变服务端主体。"""
    r = client.get("/api/v1/me", params={
        "is_admin": "true", "role": "owner",
        "authorized_execution": "true", "tenant_id": "victim-tenant",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["identity"]["tenant_id"] == "default"
    assert d["identity"]["is_owner_class"] is True   # 本就是回环 owner，非参数提权
    assert "is_admin" not in d and "authorized_execution" not in d
    assert d["request_body_trusted"] is False


def test_me_viewer_context_is_not_owner(client):
    r = client.get("/api/v1/me", headers={
        "X-Permission-Context": '{"role":"viewer","scope":[]}',
    })
    assert r.status_code == 200, r.text
    ident = r.json()["identity"]
    assert ident["role"] == "viewer"
    assert ident["is_owner_class"] is False


def test_me_discloses_iam_boundary(client):
    """缺口照实披露：生产真实 IAM 接入前不得宣称防伪造已达成。"""
    d = client.get("/api/v1/me").json()
    assert "X-Permission-Context" in d["security_boundary"]
    assert "尚未达成" in d["security_boundary"]
    assert d["environment"] == "dev"


# --------------------------------------------------- /health/cq-store

def test_cq_store_health_reports_real_facts(client):
    r = client.get("/api/v1/health/cq-store")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["service"] == "cq-v2"
    assert d["requested_mode"] == "postgres"
    assert d["authoritative_table"] == "ontology.cq_v2_records"
    assert isinstance(d["healthy"], bool)
    assert "actual_store" in d and "db_ok" in d
    assert "persistence" in d or "persistence_error" in d
    assert d["memory_store_allowed"] is True          # dev 环境
    assert d["startup_gate"]["checked"] is False      # 非生产跳过


def test_cq_store_health_is_not_just_liveness(client):
    """/healthz 只报存活；本端点必须带持久化事实。"""
    h = client.get("/healthz").json()
    d = client.get("/api/v1/health/cq-store").json()
    assert h == {"status": "ok"}
    assert {"using_postgres", "degraded_reason", "db_ok", "hydrated"} & set(d)


# --------------------------------------------- production 禁 MemoryStore

def test_dev_mode_allows_explicit_memory(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("CQ_V2_STORE", "memory")
    assert st.production_mode() is False
    assert st.assert_production_store()["checked"] is False
    assert type(st.default_store()).__name__ == "MemoryStore"


def test_production_rejects_explicit_memory(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CQ_V2_STORE", "memory")
    assert st.production_mode() is True
    with pytest.raises(RuntimeError, match="禁止 MemoryStore"):
        st.assert_production_store()
    with pytest.raises(RuntimeError, match="禁止 MemoryStore"):
        st.default_store()


@pytest.mark.parametrize("env_value", ["production", "PROD", " Production "])
def test_production_mode_alias(monkeypatch, env_value):
    monkeypatch.setenv("APP_ENV", env_value)
    assert st.production_mode() is True


def test_production_rejects_db_unreachable(monkeypatch):
    """生产 + postgres 但库不可达：禁止隐式降级，必须 fail-fast。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CQ_V2_STORE", "postgres")
    monkeypatch.setattr(st.PostgresStore, "ping", lambda self: False)
    with pytest.raises(RuntimeError, match="禁止降级为 MemoryStore"):
        st.default_store()
    with pytest.raises(RuntimeError, match="PostgreSQL 不可达"):
        st.assert_production_store()


def test_dev_db_unreachable_still_degrades(monkeypatch):
    """dev 行为逐行不变：库不可达仍降级并带 degraded_reason。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("CQ_V2_STORE", "postgres")
    monkeypatch.setattr(st.PostgresStore, "ping", lambda self: False)
    s = st.default_store()
    assert type(s).__name__ == "MemoryStore"
    assert "降级" in (s.degraded_reason or "")


def test_production_gate_passes_when_db_ok(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("CQ_V2_STORE", "postgres")
    monkeypatch.setattr(st.PostgresStore, "ping", lambda self: True)
    out = st.assert_production_store()
    assert out["checked"] is True
    assert out["mode"] == "postgres"
    assert out["memory_store_allowed"] is False
