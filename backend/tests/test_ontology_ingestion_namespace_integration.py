"""Live PostgreSQL migration test for source namespace isolation."""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from app.db import session as db
from app.modules.ontology import ingestion_batches, mapping_execution


def test_source_namespace_migration_allows_same_record_key_in_distinct_sources(monkeypatch):
    url = os.getenv("POSTGRES_TEST_URL")
    if not url or os.getenv("TWIN_ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.skip("requires an explicitly designated disposable PostgreSQL test database")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS ontology CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA ontology")
            conn.exec_driver_sql("""
                CREATE TABLE ontology.dataset_mapping_sets (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id VARCHAR(120) NOT NULL DEFAULT 'default',
                    name VARCHAR(200) NOT NULL,
                    version INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
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
            conn.exec_driver_sql("""
                CREATE TABLE ontology.dataset_source_records (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id VARCHAR(120) NOT NULL,
                    mapping_name VARCHAR(200) NOT NULL,
                    source_record_id VARCHAR(500) NOT NULL,
                    entity_type VARCHAR(200) NOT NULL,
                    instance_id UUID,
                    row_hash VARCHAR(64) NOT NULL,
                    mapping_checksum VARCHAR(64) NOT NULL,
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, mapping_name, source_record_id, entity_type)
                )
            """)
            conn.exec_driver_sql("""
                CREATE TABLE ontology.dataset_ingestion_checkpoints (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id VARCHAR(120) NOT NULL,
                    mapping_set_id UUID NOT NULL,
                    snapshot_id VARCHAR(200) NOT NULL,
                    batch_index INTEGER NOT NULL,
                    batch_sha256 VARCHAR(64) NOT NULL,
                    row_offset INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    run_id UUID,
                    status VARCHAR(20) NOT NULL,
                    error JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (tenant_id, mapping_set_id, snapshot_id, batch_index)
                )
            """)

        def execute(sql, params=None):
            with engine.begin() as conn:
                conn.execute(text(sql), params or {})
        monkeypatch.setattr(db, "execute", execute)
        mapping_execution.ensure_schema()
        ingestion_batches.ensure_schema()

        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO ontology.dataset_mapping_sets
                    (tenant_id, name, version, status, checksum, created_by)
                VALUES ('tenant-a', 'mapping', 1, 'approved', 'sum', 'test')
            """))
            for namespace in ("db:crm:public.customers", "db:hr:public.employees"):
                conn.execute(text("""
                    INSERT INTO ontology.dataset_source_records
                        (tenant_id, mapping_name, source_namespace, source_record_id,
                         entity_type, row_hash, mapping_checksum)
                    VALUES ('tenant-a', 'mapping', :ns, '42', 'customer', 'row', 'map')
                """), {"ns": namespace})
            mapping_id = conn.execute(text("""
                SELECT id FROM ontology.dataset_mapping_sets
                WHERE tenant_id='tenant-a' AND name='mapping' AND version=1
            """)).scalar_one()
            for namespace, digest in (("db:crm:public.customers", "sha-a"),
                                      ("db:hr:public.employees", "sha-b")):
                conn.execute(text("""
                    INSERT INTO ontology.dataset_ingestion_checkpoints
                        (tenant_id, mapping_set_id, source_namespace, snapshot_id,
                         batch_index, batch_sha256, row_offset, row_count, status)
                    VALUES ('tenant-a', :mapping_id, :ns, 'same-snapshot',
                            0, :digest, 0, 1, 'succeeded')
                """), {"mapping_id": mapping_id, "ns": namespace, "digest": digest})

        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO ontology.dataset_source_records
                        (tenant_id, mapping_name, source_namespace, source_record_id,
                         entity_type, row_hash, mapping_checksum)
                    VALUES ('tenant-a', 'mapping', 'db:crm:public.customers',
                            '42', 'customer', 'row', 'map')
                """))
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO ontology.dataset_ingestion_checkpoints
                        (tenant_id, mapping_set_id, source_namespace, snapshot_id,
                         batch_index, batch_sha256, row_offset, row_count, status)
                    VALUES ('tenant-a', :mapping_id, 'db:crm:public.customers',
                            'same-snapshot', 0, 'sha-c', 0, 1, 'succeeded')
                """), {"mapping_id": mapping_id})
    finally:
        engine.dispose()
