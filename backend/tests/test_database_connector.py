import json

import pytest

from app.modules.ontology import database_connector as connector


def test_profile_listing_hides_secret_and_filters_tenant(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"label": "CRM", "url_env": "CRM_URL", "tenant_ids": ["t1"],
                "schemas": ["public"], "tables": ["public.customers"]},
        "hr": {"url_env": "HR_URL", "tenant_ids": ["t2"], "tables": ["employees"]},
    }))
    result = connector.list_profiles(tenant_id="t1")
    assert [x["name"] for x in result] == ["crm"]
    assert "url_env" not in result[0]
    assert "password" not in str(result)


def test_rejects_invalid_profile_and_unsupported_driver(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "bad": {"url_env": "DB_URL", "tables": ["t"]}
    }))
    monkeypatch.setenv("DB_URL", "sqlite:///local.db")
    with pytest.raises(connector.ConnectorConfigError):
        connector._profile("bad", "default")


def test_requires_server_side_url_environment_variable(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_READONLY_URL", "tables": ["public.customer"]}
    }))
    monkeypatch.delenv("CRM_READONLY_URL", raising=False)
    with pytest.raises(connector.ConnectorConfigError):
        connector._profile("crm", "default")


def test_tenant_isolation(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_URL", "tenant_ids": ["tenant-a"], "tables": ["customers"]}
    }))
    with pytest.raises(PermissionError):
        connector._profile("crm", "tenant-b")


def test_allowlist_parser_supports_schema_qualified_and_unqualified():
    allowed = connector._allowed_tables({
        "tables": ["public.users", "orders", {"schema": "sales", "name": "invoices"}]
    })
    assert ("public", "users") in allowed
    assert (None, "orders") in allowed
    assert ("sales", "invoices") in allowed


def test_requested_table_must_be_allowlisted(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_URL", "tables": ["public.customers"], "schemas": ["public"]}
    }))
    monkeypatch.setenv("CRM_URL", "postgresql://readonly:secret@localhost/crm")
    with pytest.raises(PermissionError):
        connector.inspect_database(profile_name="crm", tenant_id="default",
                                   schema_name="public", table_names=["payments"])


def test_sensitive_columns_are_redacted():
    assert connector._is_sensitive_column("phone_number")
    assert connector._is_sensitive_column("身份证号码")
    assert connector._is_sensitive_column("custom_field", {"custom_field"})
    assert not connector._is_sensitive_column("department_name")


def test_mysql_and_postgresql_url_query_cannot_override_read_only_options(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_URL", "dialect": "postgresql",
                "tables": ["public.customers"]}
    }))
    monkeypatch.setenv("CRM_URL", "postgresql://readonly:secret@localhost/crm?options=-c%20default_transaction_read_only%3Doff")
    _, url = connector._profile("crm", "default")
    assert "options" not in __import__("sqlalchemy").engine.make_url(url).query


def test_profile_table_limit_and_sample_limit_constants():
    assert connector.MAX_SAMPLE_ROWS == 100
    assert connector.MAX_TABLES_PER_REQUEST == 50
