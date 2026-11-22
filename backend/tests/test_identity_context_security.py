"""Focused remote identity-context trust-boundary regression."""
import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings as cfg
from app.main import app


def test_remote_api_key_cannot_spoof_tenant_user_role_or_schema(monkeypatch):
    # The deployment-only PR carries this test before the identity hardening PR
    # is merged. Once the helper lands, the same test becomes an active gate.
    if not hasattr(auth, "allow_identity_context_headers"):
        pytest.skip("awaiting server-side identity context boundary")
    monkeypatch.setattr(cfg, "api_key", "remote-owner-test-key")
    client = TestClient(
        app, client=("198.51.100.24", 54321),
        raise_server_exceptions=False,
    )
    headers = {
        "X-Twin-API-Key": "remote-owner-test-key",
        "X-Tenant-Id": "tenant-victim",
        "X-User-Id": "attacker",
        "X-Permission-Context": json.dumps({
            "role": "owner", "scope": ["owner", "organization"],
            "data_classification": "restricted",
        }),
        "X-Schema-Version": "attacker-controlled-version",
    }
    response = client.get("/api/v1/semantic/meta", headers=headers)
    assert response.status_code == 200
    assert response.headers["x-semantic-tenant-id"] == "default"
    assert response.headers["x-semantic-user-id"] == "owner"
    permission = json.loads(response.headers["x-semantic-permission-context"])
    assert permission["role"] == "owner"
    assert permission["scope"] == ["owner", "organization"]
    assert response.headers["x-semantic-schema-version"] != "attacker-controlled-version"
    assert response.headers["x-semantic-context-source"] == "derived"



def test_trusted_proxy_without_verified_client_ip_is_not_loopback(monkeypatch):
    monkeypatch.setattr(cfg, "api_key", "")
    monkeypatch.setattr(cfg, "trust_proxy_headers", True)
    monkeypatch.setattr(cfg, "trusted_proxy_hosts", "127.0.0.1")
    client = TestClient(
        app, client=("127.0.0.1", 54321),
        raise_server_exceptions=False,
    )
    response = client.get(
        "/api/v1/semantic/meta",
        headers={"X-Forwarded-For": "127.0.0.1, ::1"},
    )
    assert response.status_code == 401
