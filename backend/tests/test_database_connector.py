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


def test_custom_redaction_names_are_normalized():
    assert connector._is_sensitive_column("customer_phone", {"customer phone"})
    assert connector._is_sensitive_column("contact_email", {"CONTACT EMAIL"})


def test_mysql_url_query_cannot_inject_init_command(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_URL", "dialect": "mysql", "tables": ["customers"]}
    }))
    monkeypatch.setenv("CRM_URL", "mysql://readonly:secret@localhost/crm?init_command=SET%20GLOBAL%20read_only%3D0&autocommit=true")
    _, url = connector._profile("crm", "default")
    parsed = __import__("sqlalchemy").engine.make_url(url)
    assert "init_command" not in parsed.query
    assert "autocommit" not in parsed.query


def test_profile_output_does_not_expose_connection_secret(monkeypatch):
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps({
        "crm": {"url_env": "CRM_URL", "tables": ["customers"], "label": "CRM"}
    }))
    monkeypatch.setenv("CRM_URL", "mysql://dbuser:very-secret@db.internal/crm")
    assert "very-secret" not in str(connector.list_profiles(tenant_id="default"))



def test_keyset_stream_reads_all_rows_in_stable_order(monkeypatch):
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE employees (employee_no INTEGER PRIMARY KEY, full_name TEXT, dept TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO employees VALUES "
            "(1, '张三', '研发'), (2, '李四', '产品'), (3, '王五', '研发'), "
            "(4, '赵六', '销售'), (5, '钱七', '研发')"
        )
    monkeypatch.setattr(connector, "_profile", lambda name, tenant: (
        {"schemas": [], "tables": ["employees"]}, "sqlite://"
    ))
    monkeypatch.setattr(connector, "_engine", lambda url: engine)
    monkeypatch.setattr(connector, "_enforce_read_only", lambda conn, dialect: None)

    rows = list(connector.stream_table_rows(
        profile_name="hr", tenant_id="tenant-a", schema_name=None,
        table_name="employees", columns=["full_name", "dept"],
        primary_key="employee_no", batch_size=2,
    ))
    assert [row["employee_no"] for row in rows] == [1, 2, 3, 4, 5]
    assert rows[0] == {"full_name": "张三", "dept": "研发", "employee_no": 1}


def test_keyset_stream_rejects_non_primary_key_cursor(monkeypatch):
    from sqlalchemy import create_engine
    from app.modules.ontology.database_connector import ConnectorConfigError

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE employees (employee_no INTEGER, full_name TEXT)")
        conn.exec_driver_sql("INSERT INTO employees VALUES (1, '张三')")
    monkeypatch.setattr(connector, "_profile", lambda name, tenant: (
        {"schemas": [], "tables": ["employees"]}, "sqlite://"
    ))
    monkeypatch.setattr(connector, "_engine", lambda url: engine)
    monkeypatch.setattr(connector, "_enforce_read_only", lambda conn, dialect: None)

    with pytest.raises(ConnectorConfigError, match="单列主键"):
        list(connector.stream_table_rows(
            profile_name="hr", tenant_id="tenant-a", schema_name=None,
            table_name="employees", columns=["full_name"],
            primary_key="employee_no", batch_size=10,
        ))
    engine.dispose()



def test_sensitive_columns_require_explicit_ingestion_allowlist(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    db_url = f"sqlite:///{tmp_path / 'contacts.db'}"
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE contacts (id INTEGER PRIMARY KEY, phone_number TEXT)"
        )
        conn.exec_driver_sql("INSERT INTO contacts VALUES (1, '13800138000')")
    engine.dispose()
    profile = {"schemas": [], "tables": ["contacts"]}
    monkeypatch.setattr(connector, "_profile", lambda name, tenant: (profile, db_url))
    monkeypatch.setattr(connector, "_engine", lambda url: create_engine(url))
    monkeypatch.setattr(connector, "_enforce_read_only", lambda conn, dialect: None)

    with pytest.raises(PermissionError, match="敏感字段未获准"):
        list(connector.stream_table_rows(
            profile_name="crm", tenant_id="tenant-a", schema_name=None,
            table_name="contacts", columns=["phone_number"], primary_key="id",
        ))

    profile["ingest_sensitive_columns"] = ["phone_number"]
    rows = list(connector.stream_table_rows(
        profile_name="crm", tenant_id="tenant-a", schema_name=None,
        table_name="contacts", columns=["phone_number"], primary_key="id",
    ))
    assert rows[0]["phone_number"] == "13800138000"
    engine.dispose()
