"""V1.3 换栈 Phase 0 回归：只读 IdentityContext。

覆盖 Sprint 1 三条验收铁律中本轮真实落地的部分：
- `/me` 身份来自全局中间件，请求体提权字段不参与判定、只读、不可反写；
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


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
