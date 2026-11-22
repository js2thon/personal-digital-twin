"""Read-only enterprise database connector for ontology mapping discovery.

Connection profiles are server-side allowlists. Secrets are referenced by environment
variable name and are never persisted in mapping definitions or returned by APIs.
Only metadata inspection and bounded SQLAlchemy Core SELECT samples are supported.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.engine import make_url

_PROFILE_ENV = "TWIN_DB_CONNECTOR_PROFILES"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ALLOWED_DRIVERS = {
    "postgresql": "postgresql+psycopg",
    "postgresql+psycopg": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mysql+pymysql": "mysql+pymysql",
}
MAX_SAMPLE_ROWS = 100
MAX_TABLES_PER_REQUEST = 50


class ConnectorConfigError(ValueError):
    pass


def _load_profiles() -> dict[str, dict]:
    raw = os.getenv(_PROFILE_ENV, "{}")
    try:
        profiles = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConnectorConfigError(f"{_PROFILE_ENV} 必须是合法 JSON") from exc
    if not isinstance(profiles, dict):
        raise ConnectorConfigError("连接器配置必须是对象")
    return profiles


def list_profiles(*, tenant_id: str) -> list[dict]:
    """Return non-secret profile metadata visible to the given tenant."""
    output = []
    for name, cfg in _load_profiles().items():
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name) or not isinstance(cfg, dict):
            continue
        tenants = cfg.get("tenant_ids") or []
        if tenants and tenant_id not in tenants:
            continue
        output.append({
            "name": name,
            "label": str(cfg.get("label") or name),
            "dialect": str(cfg.get("dialect") or "auto"),
            "schemas": list(cfg.get("schemas") or []),
            "tables_allowlisted": len(cfg.get("tables") or []),
            "read_only": True,
        })
    return sorted(output, key=lambda item: item["name"])


def _profile(name: str, tenant_id: str) -> tuple[dict, str]:
    if not _PROFILE_NAME.fullmatch(name or ""):
        raise ConnectorConfigError("连接配置名称格式不合法")
    cfg = _load_profiles().get(name)
    if not isinstance(cfg, dict):
        raise LookupError("连接配置不存在")
    tenants = cfg.get("tenant_ids") or []
    if tenants and tenant_id not in tenants:
        raise PermissionError("当前租户无权使用该连接配置")
    url_env = cfg.get("url_env")
    if not isinstance(url_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", url_env):
        raise ConnectorConfigError("连接配置缺少合法的 url_env")
    raw_url = os.getenv(url_env)
    if not raw_url:
        raise ConnectorConfigError(f"服务端未配置连接环境变量：{url_env}")
    try:
        parsed = make_url(raw_url)
    except Exception as exc:
        raise ConnectorConfigError("数据库连接 URL 格式错误") from exc
    driver = _ALLOWED_DRIVERS.get(parsed.drivername)
    if not driver:
        raise ConnectorConfigError("仅支持 PostgreSQL(psycopg) 和 MySQL(PyMySQL)")
    parsed = parsed.set(drivername=driver)
    expected = str(cfg.get("dialect") or "auto").lower()
    actual = "postgresql" if driver.startswith("postgresql") else "mysql"
    if expected not in {"auto", actual}:
        raise ConnectorConfigError("连接 URL 数据库类型与配置不一致")
    if not parsed.host or not parsed.database:
        raise ConnectorConfigError("连接 URL 必须包含 host 和 database")
    return cfg, parsed.render_as_string(hide_password=False)


def _engine(url: str):
    parsed = make_url(url)
    connect_args: dict[str, Any] = {}
    if parsed.drivername.startswith("postgresql"):
        connect_args["connect_timeout"] = 5
        connect_args["options"] = "-c statement_timeout=5000 -c default_transaction_read_only=on"
    else:
        connect_args["connect_timeout"] = 5
        connect_args["read_timeout"] = 5
        connect_args["write_timeout"] = 5
    return create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0,
                         pool_timeout=5, pool_recycle=300, connect_args=connect_args)


def _allowed_tables(cfg: dict) -> set[tuple[str | None, str]]:
    result = set()
    for item in cfg.get("tables") or []:
        if isinstance(item, str) and item:
            if "." in item:
                schema, table = item.split(".", 1)
                result.add((schema, table))
            else:
                result.add((None, item))
        elif isinstance(item, dict) and item.get("name"):
            result.add((item.get("schema"), str(item["name"])))
    return result


def inspect_database(*, profile_name: str, tenant_id: str,
                     schema_name: str | None = None,
                     table_names: list[str] | None = None,
                     sample_rows: int = 5) -> dict:
    """Discover allowlisted tables and return bounded samples; never execute raw SQL."""
    cfg, url = _profile(profile_name, tenant_id)
    allowed_schemas = set(cfg.get("schemas") or [])
    allowed_tables = _allowed_tables(cfg)
    if not allowed_tables:
        raise ConnectorConfigError("连接配置必须显式配置 tables 白名单")
    limit = max(0, min(int(sample_rows), MAX_SAMPLE_ROWS))
    requested = list(dict.fromkeys(table_names or []))
    if len(requested) > MAX_TABLES_PER_REQUEST:
        raise ConnectorConfigError(f"单次最多检查 {MAX_TABLES_PER_REQUEST} 张表")
    if schema_name and allowed_schemas and schema_name not in allowed_schemas:
        raise PermissionError("请求的 schema 不在白名单中")
    if requested:
        for table in requested:
            if not any(t == table and (s is None or s == schema_name) for s, t in allowed_tables):
                raise PermissionError(f"表不在白名单中：{table}")

    engine = _engine(url)
    try:
        with engine.connect() as conn:
            # PostgreSQL is additionally forced read-only by server-side connection
            # options; this transaction mode is applied before metadata/sample reads.
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            inspector = inspect(conn)
            schemas = [schema_name] if schema_name else sorted(allowed_schemas)
            if not schemas:
                schemas = [None]
            found = []
            for schema in schemas:
                for table in inspector.get_table_names(schema=schema):
                    if (schema, table) in allowed_tables or (None, table) in allowed_tables:
                        if not requested or table in requested:
                            found.append((schema, table))
            if requested:
                found_names = {table for _, table in found}
                missing = sorted(set(requested) - found_names)
                if missing:
                    raise LookupError("白名单表不存在或不可见：" + ", ".join(missing))
            results = []
            metadata = MetaData()
            for schema, table_name in found[:MAX_TABLES_PER_REQUEST]:
                table = Table(table_name, metadata, schema=schema, autoload_with=conn)
                columns = [{
                    "name": col.name,
                    "data_type": str(col.type),
                    "nullable": bool(col.nullable),
                    "primary_key": bool(col.primary_key),
                } for col in table.columns]
                samples = []
                if limit:
                    rows = conn.execute(select(table).limit(limit)).mappings().all()
                    samples = [{str(k): _safe_sample(v) for k, v in row.items()} for row in rows]
                results.append({
                    "schema": schema,
                    "table": table_name,
                    "columns": columns,
                    "sample_rows": samples,
                    "sample_count": len(samples),
                })
            return {
                "profile": profile_name, "dialect": engine.dialect.name,
                "read_only": True, "sample_limit": limit,
                "tables_discovered": len(results), "tables": results,
                "execution": "metadata_and_bounded_select_only",
            }
    finally:
        engine.dispose()


def _safe_sample(value: Any) -> Any:
    """Keep API samples JSON-safe and bounded; do not expose binary/large objects."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value)
    return text[:500] + "…" if len(text) > 500 else text
