"""Tenant-scoped, immutable mapping-set version registry (PostgreSQL)."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from app.db import session as db
from app.modules.ontology.mapping_contract import to_binding


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)


def ensure_schema() -> None:
    db.execute("CREATE SCHEMA IF NOT EXISTS ontology")
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_mapping_sets (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL DEFAULT 'default',
        name VARCHAR(200) NOT NULL,
        version INTEGER NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','approved','retired')),
        schema_version VARCHAR(100),
        ontology_version VARCHAR(100),
        definition JSONB NOT NULL DEFAULT '{}'::jsonb,
        checksum VARCHAR(64) NOT NULL,
        created_by VARCHAR(160) NOT NULL,
        approved_by VARCHAR(160),
        approved_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, name, version)
    )
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS idx_dataset_mapping_sets_tenant_name
    ON ontology.dataset_mapping_sets (tenant_id, name, version DESC)
    """)


def create_version(*, tenant_id: str, name: str, definition: dict,
                   schema_version: str, ontology_version: str,
                   created_by: str) -> dict:
    ensure_schema()
    stored_definition = deepcopy(definition or {})
    stored_definition["ontology_binding"] = to_binding(
        stored_definition, ontology_version=ontology_version)
    normalized = _json(stored_definition)
    checksum = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    with db.transaction():
        # Serialize version allocation per tenant + mapping name to avoid concurrent
        # requests producing the same next version.
        lock_key = f"{tenant_id}:{name}"
        db.fetch_one("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0)) AS locked",
                     {"k": lock_key})
        current = db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) AS v FROM ontology.dataset_mapping_sets "
            "WHERE tenant_id = :t AND name = :n",
            {"t": tenant_id, "n": name})
        version = int((current or {}).get("v") or 0) + 1
        row = db.fetch_one(
            "INSERT INTO ontology.dataset_mapping_sets "
            "(tenant_id, name, version, status, schema_version, ontology_version, "
            " definition, checksum, created_by) "
            "VALUES (:t, :n, :v, 'draft', :sv, :ov, CAST(:d AS jsonb), :c, :by) "
            "RETURNING id, tenant_id, name, version, status, schema_version, "
            "ontology_version, checksum, created_by, created_at",
            {"t": tenant_id, "n": name, "v": version, "sv": schema_version,
             "ov": ontology_version, "d": normalized, "c": checksum, "by": created_by})
    return _serialize(row)


def approve(mapping_id: str, *, tenant_id: str, approved_by: str) -> dict | None:
    ensure_schema()
    row = db.fetch_one(
        "UPDATE ontology.dataset_mapping_sets SET status = 'approved', "
        " approved_by = :by, approved_at = now() "
        "WHERE id = CAST(:id AS uuid) AND tenant_id = :t AND status = 'draft' "
        "RETURNING id, tenant_id, name, version, status, schema_version, "
        "ontology_version, checksum, created_by, approved_by, approved_at, created_at",
        {"id": mapping_id, "t": tenant_id, "by": approved_by})
    return _serialize(row) if row else None


def get(mapping_id: str, *, tenant_id: str) -> dict | None:
    ensure_schema()
    row = db.fetch_one(
        "SELECT * FROM ontology.dataset_mapping_sets "
        "WHERE id = CAST(:id AS uuid) AND tenant_id = :t",
        {"id": mapping_id, "t": tenant_id})
    if not row:
        return None
    result = _serialize(row)
    definition = row.get("definition")
    if isinstance(definition, str):
        try:
            definition = json.loads(definition)
        except ValueError:
            definition = {}
    result["definition"] = definition or {}
    result["definition"].setdefault(
        "ontology_binding",
        to_binding(result["definition"], ontology_version=result.get("ontology_version")))
    return result


def list_versions(*, tenant_id: str, name: str | None = None,
                  limit: int = 50) -> list[dict]:
    ensure_schema()
    params = {"t": tenant_id, "lim": max(1, min(int(limit), 200))}
    where = "tenant_id = :t"
    if name:
        where += " AND name = :n"
        params["n"] = name
    rows = db.fetch_all(
        "SELECT id, tenant_id, name, version, status, schema_version, ontology_version, "
        "checksum, created_by, approved_by, approved_at, created_at "
        f"FROM ontology.dataset_mapping_sets WHERE {where} "
        "ORDER BY name, version DESC LIMIT :lim", params)
    return [_serialize(row) for row in rows]


def _serialize(row: dict | None) -> dict:
    if not row:
        return {}
    out = dict(row)
    if "id" in out:
        out["id"] = str(out["id"])
    for key in ("created_at", "approved_at"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out
