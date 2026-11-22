"""Live PostgreSQL/MySQL connector integration tests.

These tests run only when the workflow provides disposable database services.
They verify real metadata discovery, bounded samples, redaction, and server-enforced
read-only transactions rather than only mocking SQLAlchemy.
"""
import json
import os

import pytest
from sqlalchemy import create_engine
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
        sample_rows=2,
    )
    assert result["read_only"] is True
    assert result["tables_discovered"] == 1
    table = result["tables"][0]
    assert table["table"] == "ontology_connector_probe"
    assert table["sample_count"] == 1
    assert table["sample_rows"][0]["full_name"] == "张三"
    assert table["sample_rows"][0]["phone_number"] == "<redacted>"
    assert "phone_number" in table["redacted_columns"]

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
