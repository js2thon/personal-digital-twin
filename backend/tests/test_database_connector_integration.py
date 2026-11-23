"""Live PostgreSQL/MySQL connector integration tests.

These tests run only when the workflow provides disposable database services.
They verify real metadata discovery, bounded samples, redaction, and server-enforced
read-only transactions rather than only mocking SQLAlchemy.
"""
import json
import os
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.modules.ontology import database_connector as connector


CASES = [
    ("postgresql", "POSTGRES_TEST_URL"),
    ("mysql", "MYSQL_TEST_URL"),
]


@pytest.mark.parametrize(("dialect", "url_env"), CASES)
def test_live_database_discovery_redaction_and_read_only(
    dialect, url_env, monkeypatch
):
    url = os.getenv(url_env)
    if not url:
        pytest.skip(f"{url_env} is not configured")

    admin_engine = create_engine(url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE IF EXISTS ontology_connector_probe")
            conn.exec_driver_sql(
                "CREATE TABLE ontology_connector_probe ("
                "id INTEGER PRIMARY KEY, "
                "full_name VARCHAR(100), "
                "phone_number VARCHAR(32), "
                "department_name VARCHAR(64))"
            )
            conn.exec_driver_sql(
                "INSERT INTO ontology_connector_probe "
                "(id, full_name, phone_number, department_name) "
                "VALUES (1, '张三', '13800138000', '研发部')"
            )
            conn.execute(
                text(
                    "INSERT INTO ontology_connector_probe "
                    "(id, full_name, phone_number, department_name) "
                    "VALUES (:id, :name, :phone, :department)"
                ),
                [
                    {
                        "id": row_id,
                        "name": f"用户{row_id}",
                        "phone": f"1390000{row_id:04d}",
                        "department": "测试部",
                    }
                    for row_id in range(2, 122)
                ],
            )
    finally:
        admin_engine.dispose()

    profile_name = f"{dialect}_integration"
    monkeypatch.setenv(url_env, url)
    monkeypatch.setenv(
        connector._PROFILE_ENV,
        json.dumps({
            profile_name: {
                "label": f"{dialect} integration",
                "url_env": url_env,
                "dialect": dialect,
                "tenant_ids": ["integration-tenant"],
                "tables": ["ontology_connector_probe"],
            }
        }),
    )

    result = connector.inspect_database(
        profile_name=profile_name,
        tenant_id="integration-tenant",
        sample_rows=connector.MAX_SAMPLE_ROWS + 25,
    )
    assert result["read_only"] is True
    assert result["tables_discovered"] == 1
    table = result["tables"][0]
    assert table["table"] == "ontology_connector_probe"
    assert result["sample_limit"] == connector.MAX_SAMPLE_ROWS
    assert table["sample_count"] == connector.MAX_SAMPLE_ROWS
    assert all(row["phone_number"] == "<redacted>" for row in table["sample_rows"])
    assert "phone_number" in table["redacted_columns"]

    # Tenant and identifier boundaries must reject requests before any user
    # supplied identifier can become part of a SQL statement.
    with pytest.raises(PermissionError):
        connector.inspect_database(
            profile_name=profile_name, tenant_id="another-tenant",
            sample_rows=0,
        )
    malicious_table = "ontology_connector_probe; DROP TABLE ontology_connector_probe; --"
    with pytest.raises(PermissionError):
        connector.inspect_database(
            profile_name=profile_name, tenant_id="integration-tenant",
            table_names=[malicious_table], sample_rows=0,
        )
    with pytest.raises(PermissionError):
        list(connector.stream_table_rows(
            profile_name=profile_name, tenant_id="integration-tenant",
            schema_name=None, table_name=malicious_table,
            columns=["full_name"], primary_key="id", batch_size=10,
        ))
    with pytest.raises(PermissionError):
        list(connector.stream_table_rows(
            profile_name=profile_name, tenant_id="another-tenant",
            schema_name=None, table_name="ontology_connector_probe",
            columns=["full_name"], primary_key="id", batch_size=10,
        ))
    with pytest.raises(LookupError):
        list(connector.stream_table_rows(
            profile_name=profile_name, tenant_id="integration-tenant",
            schema_name=None, table_name="ontology_connector_probe",
            columns=["full_name; DROP TABLE ontology_connector_probe; --"],
            primary_key="id", batch_size=10,
        ))
    # The malicious table/column names above must not alter the source table.
    assert connector.inspect_database(
        profile_name=profile_name, tenant_id="integration-tenant",
        sample_rows=0,
    )["tables_discovered"] == 1

    # The production keyset stream uses the same allowlist and defaults to
    # blocking sensitive columns until the profile explicitly authorizes them.
    with pytest.raises(PermissionError, match="敏感字段未获准"):
        list(connector.stream_table_rows(
            profile_name=profile_name, tenant_id="integration-tenant",
            schema_name=None, table_name="ontology_connector_probe",
            columns=["full_name", "phone_number"], primary_key="id", batch_size=50,
        ))
    profile = json.loads(os.getenv(connector._PROFILE_ENV))
    profile[profile_name]["ingest_sensitive_columns"] = ["phone_number"]
    monkeypatch.setenv(connector._PROFILE_ENV, json.dumps(profile))
    streamed = list(connector.stream_table_rows(
        profile_name=profile_name, tenant_id="integration-tenant",
        schema_name=None, table_name="ontology_connector_probe",
        columns=["full_name", "phone_number"], primary_key="id", batch_size=50,
    ))
    assert len(streamed) == 121
    assert streamed[0]["full_name"] == "张三"
    assert streamed[0]["phone_number"] == "13800138000"

    # Prove the connector's session policy is enforced by the actual database.
    read_engine = connector._engine(url)
    try:
        with read_engine.connect() as conn:
            connector._enforce_read_only(conn, read_engine.dialect.name)
            with pytest.raises(SQLAlchemyError):
                conn.exec_driver_sql(
                    "INSERT INTO ontology_connector_probe "
                    "(id, full_name, phone_number, department_name) "
                    "VALUES (2, '不应写入', '13900139000', '测试部')"
                )
            conn.rollback()
    finally:
        read_engine.dispose()



@pytest.mark.parametrize(("dialect", "url_env"), CASES)
def test_database_statement_timeout_is_enforced(dialect, url_env):
    """The actual server/driver must interrupt a SELECT exceeding five seconds."""
    url = os.getenv(url_env)
    if not url:
        pytest.skip(f"{url_env} is not configured")

    engine = connector._engine(url)
    statement = "SELECT pg_sleep(6)" if dialect == "postgresql" else "SELECT SLEEP(6)"
    try:
        with engine.connect() as conn:
            connector._enforce_read_only(conn, engine.dialect.name)
            started = time.monotonic()
            with pytest.raises(SQLAlchemyError):
                conn.exec_driver_sql(statement)
            elapsed = time.monotonic() - started
            # Both the server statement timeout and the MySQL driver read timeout
            # are configured at five seconds. Allow CI scheduling jitter, but not
            # a silently unbounded six-second query.
            assert 4.0 <= elapsed < 10.0
            conn.rollback()
    finally:
        engine.dispose()
