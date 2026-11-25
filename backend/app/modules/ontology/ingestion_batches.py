"""Resumable, bounded ingestion orchestration over approved mapping execution.

This wrapper consumes trusted row iterables; it does not open arbitrary database URLs.
Each <=500-row chunk is independently atomic and idempotent.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Iterator

from app.db import session as db
from app.modules.ontology import mapping_registry
from app.modules.ontology.mapping_execution import (
    execute_approved_dataset, ensure_schema as ensure_mapping_schema,
    MAX_EXECUTION_ROWS, _normalize_source_namespace,
)


def _digest(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _chunks(rows: Iterable[dict], size: int) -> Iterator[tuple[int, list[dict]]]:
    batch: list[dict] = []
    index = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("每条源数据必须是对象/字典")
        batch.append(row)
        if len(batch) >= size:
            yield index, batch
            index += 1
            batch = []
    if batch:
        yield index, batch


def ensure_schema() -> None:
    ensure_mapping_schema()
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_ingestion_checkpoints (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        mapping_set_id UUID NOT NULL,
        source_namespace VARCHAR(500) NOT NULL DEFAULT 'default',
        snapshot_id VARCHAR(200) NOT NULL,
        batch_index INTEGER NOT NULL,
        batch_sha256 VARCHAR(64) NOT NULL,
        row_offset INTEGER NOT NULL,
        row_count INTEGER NOT NULL,
        run_id UUID,
        status VARCHAR(20) NOT NULL CHECK (status IN ('running','succeeded','failed')),
        error JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_dataset_ingestion_checkpoints_source
            UNIQUE (tenant_id, mapping_set_id, source_namespace, snapshot_id, batch_index)
    )
    """)
    db.execute("""
    ALTER TABLE ontology.dataset_ingestion_checkpoints
    ADD COLUMN IF NOT EXISTS source_namespace VARCHAR(500) NOT NULL DEFAULT 'default'
    """)
    db.execute("""
    DO $migrate$
    DECLARE old_constraint RECORD;
    BEGIN
        FOR old_constraint IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'ontology.dataset_ingestion_checkpoints'::regclass
              AND contype = 'u'
              AND pg_get_constraintdef(oid) =
                  'UNIQUE (tenant_id, mapping_set_id, snapshot_id, batch_index)'
        LOOP
            EXECUTE format('ALTER TABLE ontology.dataset_ingestion_checkpoints DROP CONSTRAINT %I',
                           old_constraint.conname);
        END LOOP;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'ontology.dataset_ingestion_checkpoints'::regclass
              AND conname = 'uq_dataset_ingestion_checkpoints_source'
        ) THEN
            ALTER TABLE ontology.dataset_ingestion_checkpoints
            ADD CONSTRAINT uq_dataset_ingestion_checkpoints_source
            UNIQUE (tenant_id, mapping_set_id, source_namespace, snapshot_id, batch_index);
        END IF;
    END $migrate$;
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_ingestion_snapshots (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        mapping_set_id UUID NOT NULL REFERENCES ontology.dataset_mapping_sets(id),
        source_namespace VARCHAR(500) NOT NULL DEFAULT 'default',
        snapshot_id VARCHAR(200) NOT NULL,
        source_sha256 VARCHAR(64) NOT NULL,
        batch_count INTEGER,
        total_rows INTEGER,
        status VARCHAR(20) NOT NULL CHECK (status IN ('running','succeeded','failed')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_dataset_ingestion_snapshots_source
            UNIQUE (tenant_id, mapping_set_id, source_namespace, snapshot_id)
    )
    """)
    db.execute("""
    ALTER TABLE ontology.dataset_source_records
    ADD COLUMN IF NOT EXISTS source_active BOOLEAN NOT NULL DEFAULT TRUE
    """)


def execute_approved_dataset_stream(*, tenant_id: str, mapping_set_id: str,
                                    rows: Iterable[dict], source_name: str,
                                    snapshot_id: str, actor: str,
                                    source_sha256: str | None = None,
                                    sync_mode: str = "upsert",
                                    source_id_column: str | None = None,
                                    batch_size: int = MAX_EXECUTION_ROWS,
                                    reconcile_missing: bool = False,
                                    source_namespace: str = "default",
                                    source_table: str | None = None) -> dict:
    """Execute one immutable full-source snapshot in resumable batches.

    snapshot_id must uniquely identify one immutable snapshot. Replaying the same
    snapshot is idempotent; reusing its ID with changed content fails.
    reconcile_missing marks absent source records inactive only after all batches
    succeed. It does not physically delete ontology instances.
    """
    ensure_schema()
    namespace = _normalize_source_namespace(source_namespace)
    record_namespace = (
        _normalize_source_namespace(f"{namespace}:{source_table}")
        if source_table else namespace
    )
    size = max(1, min(int(batch_size), MAX_EXECUTION_ROWS))
    if reconcile_missing and (sync_mode != "upsert" or not source_id_column):
        raise ValueError("缺失记录对账仅支持提供稳定源记录 ID 的 upsert 模式")
    mapping_set = mapping_registry.get(mapping_set_id, tenant_id=tenant_id)
    if not mapping_set or mapping_set.get("status") != "approved":
        raise PermissionError("只有当前租户已审核的映射版本可以执行")
    mapping_name = str(mapping_set.get("name") or "")
    snapshot_started = None
    if reconcile_missing:
        started = db.fetch_one("SELECT clock_timestamp() AS started_at")
        if not started or not started.get("started_at"):
            raise RuntimeError("无法读取目标数据库时间，拒绝执行缺失记录对账")
        snapshot_started = started["started_at"]
    base_sha = source_sha256 or _digest({"source": source_name, "snapshot": snapshot_id})
    manifest = db.fetch_one(
        "SELECT source_sha256, batch_count, status FROM ontology.dataset_ingestion_snapshots "
        "WHERE tenant_id=:t AND mapping_set_id=CAST(:m AS uuid) "
        "AND source_namespace=:ns AND snapshot_id=:s",
        {"t": tenant_id, "m": mapping_set_id, "ns": namespace, "s": snapshot_id})
    if manifest and manifest["source_sha256"] != base_sha:
        raise ValueError("同一 snapshot_id 的源快照指纹发生变化；请使用新的 snapshot_id")
    expected_batch_count = (
        int(manifest["batch_count"])
        if manifest and manifest.get("status") == "succeeded"
        and manifest.get("batch_count") is not None else None
    )
    if not manifest:
        db.execute(
            "INSERT INTO ontology.dataset_ingestion_snapshots "
            "(tenant_id, mapping_set_id, source_namespace, snapshot_id, source_sha256, status) "
            "VALUES (:t, CAST(:m AS uuid), :ns, :s, :h, 'running') "
            "ON CONFLICT (tenant_id, mapping_set_id, source_namespace, snapshot_id) DO NOTHING",
            {"t": tenant_id, "m": mapping_set_id, "ns": namespace, "s": snapshot_id, "h": base_sha})
    summaries, offset, total_rows = [], 0, 0

    for batch_index, batch in _chunks(rows, size):
        if expected_batch_count is not None and batch_index >= expected_batch_count:
            raise ValueError("同一 snapshot_id 的批次数量发生变化；请使用新的 snapshot_id")
        batch_sha = _digest({"base": base_sha, "index": batch_index, "rows": batch})
        checkpoint = db.fetch_one(
            "SELECT batch_sha256, status, run_id::text AS run_id FROM ontology.dataset_ingestion_checkpoints "
            "WHERE tenant_id=:t AND mapping_set_id=CAST(:m AS uuid) "
            "AND source_namespace=:ns AND snapshot_id=:s AND batch_index=:i",
            {"t": tenant_id, "m": mapping_set_id, "ns": namespace,
             "s": snapshot_id, "i": batch_index})
        if checkpoint and checkpoint["batch_sha256"] != batch_sha:
            raise ValueError("同一 snapshot_id 的批次内容发生变化；请使用新的 snapshot_id")
        if checkpoint and checkpoint["status"] == "succeeded":
            result = {"run_id": checkpoint.get("run_id"), "status": "succeeded",
                      "rows_received": len(batch), "records_inserted": 0,
                      "records_skipped_idempotent": len(batch), "resumed": True}
        else:
            db.execute(
                "INSERT INTO ontology.dataset_ingestion_checkpoints "
                "(tenant_id, mapping_set_id, source_namespace, snapshot_id, batch_index, batch_sha256, "
                " row_offset, row_count, status) "
                "VALUES (:t, CAST(:m AS uuid), :ns, :s, :i, :h, :o, :n, 'running') "
                "ON CONFLICT (tenant_id, mapping_set_id, source_namespace, snapshot_id, batch_index) "
                "DO UPDATE SET status='running', error='{}'::jsonb, updated_at=now()",
                {"t": tenant_id, "m": mapping_set_id, "ns": namespace,
                 "s": snapshot_id, "i": batch_index, "h": batch_sha,
                 "o": offset, "n": len(batch)})
            try:
                result = execute_approved_dataset(
                    tenant_id=tenant_id, mapping_set_id=mapping_set_id, rows=batch,
                    source_name=f"{source_name}#snapshot:{snapshot_id}#batch:{batch_index}",
                    source_sha256=batch_sha, actor=actor, max_rows=size,
                    sync_mode=sync_mode, source_id_column=source_id_column,
                    source_namespace=namespace, source_table=source_table)
                db.execute(
                    "UPDATE ontology.dataset_ingestion_checkpoints SET status='succeeded', "
                    "run_id=CAST(:r AS uuid), updated_at=now() WHERE tenant_id=:t "
                    "AND mapping_set_id=CAST(:m AS uuid) AND source_namespace=:ns "
                    "AND snapshot_id=:s AND batch_index=:i",
                    {"r": result["run_id"], "t": tenant_id, "m": mapping_set_id,
                     "ns": namespace, "s": snapshot_id, "i": batch_index})
            except Exception as exc:
                db.execute(
                    "UPDATE ontology.dataset_ingestion_checkpoints SET status='failed', "
                    "error=CAST(:e AS jsonb), updated_at=now() WHERE tenant_id=:t "
                    "AND mapping_set_id=CAST(:m AS uuid) AND source_namespace=:ns "
                    "AND snapshot_id=:s AND batch_index=:i",
                    {"e": json.dumps({"type": type(exc).__name__, "message": str(exc)[:1000]}),
                     "t": tenant_id, "m": mapping_set_id, "ns": namespace,
                     "s": snapshot_id, "i": batch_index})
                raise

        if sync_mode == "upsert":
            for row in batch:
                source_id = row.get(source_id_column) if source_id_column else None
                if source_id is not None:
                    db.execute(
                        "UPDATE ontology.dataset_source_records SET last_seen_at=now(), source_active=TRUE "
                        "WHERE tenant_id=:t AND mapping_name=:mn AND source_namespace=:ns "
                        "AND source_record_id=:sid",
                        {"t": tenant_id, "mn": mapping_name, "ns": record_namespace,
                         "sid": str(source_id).strip()})
        summaries.append(result)
        offset += len(batch)
        total_rows += len(batch)

    if expected_batch_count is not None and len(summaries) != expected_batch_count:
        raise ValueError("同一 snapshot_id 的批次数量发生变化；请使用新的 snapshot_id")

    tombstones = 0
    if reconcile_missing and total_rows == 0:
        raise ValueError("空快照不会执行缺失记录对账；请确认源数据后再重试")
    if reconcile_missing:
        result = db.fetch_one(
            "WITH stale AS (UPDATE ontology.dataset_source_records SET source_active=FALSE "
            "WHERE tenant_id=:t AND mapping_name=:mn AND source_namespace=:ns "
            "AND source_active=TRUE AND last_seen_at < :started "
            "RETURNING 1) SELECT count(*) AS n FROM stale",
            {"t": tenant_id, "mn": mapping_name, "ns": record_namespace,
             "started": snapshot_started})
        tombstones = int((result or {}).get("n") or 0)

    db.execute(
        "UPDATE ontology.dataset_ingestion_snapshots SET status='succeeded', "
        "batch_count=:b, total_rows=:n, updated_at=now() WHERE tenant_id=:t "
        "AND mapping_set_id=CAST(:m AS uuid) AND source_namespace=:ns AND snapshot_id=:s",
        {"b": len(summaries), "n": total_rows, "t": tenant_id, "m": mapping_set_id,
         "ns": namespace, "s": snapshot_id})

    return {
        "status": "succeeded", "snapshot_id": snapshot_id,
        "source_namespace": namespace,
        "rows_received": total_rows, "batches": len(summaries),
        "runs": summaries, "reconcile_missing": reconcile_missing,
        "source_records_marked_inactive": tombstones,
        "physical_deletes_performed": 0,
        "resume_strategy": "replay same immutable snapshot_id; completed batches are skipped",
    }


def _database_source_namespace(profile_name: str, schema_name: str | None,
                               table_name: str) -> str:
    profile = str(profile_name or "").strip()
    schema = str(schema_name or "default").strip()
    table = str(table_name or "").strip()
    if not profile or not table:
        raise ValueError("数据库源必须提供 profile_name 和 table_name")
    return _normalize_source_namespace(f"db:{profile}:{schema}.{table}")


def execute_approved_database_snapshot(*, tenant_id: str, mapping_set_id: str,
                                       profile_name: str, schema_name: str | None,
                                       table_name: str, primary_key: str,
                                       snapshot_id: str, actor: str,
                                       batch_size: int = 500,
                                       reconcile_missing: bool = False) -> dict:
    """Stream one allowlisted database table through an approved immutable mapping."""
    from app.modules.ontology import database_connector

    mapping_set = mapping_registry.get(mapping_set_id, tenant_id=tenant_id)
    if not mapping_set or mapping_set.get("status") != "approved":
        raise PermissionError("只有当前租户已审核的映射版本可以执行")
    definition = mapping_set.get("definition") or {}
    mappings = definition.get("mappings") or []
    table_mapping = next((item for item in mappings
                          if item.get("table") == table_name), None)
    junction_mapping = next((item for item in (definition.get("links") or [])
                             if item.get("table") == table_name), None)
    if not table_mapping and not junction_mapping:
        raise ValueError(f"已审核映射版本中不存在源表：{table_name}")
    source_columns = set()
    if table_mapping:
        source_columns.update(str(column) for column in
                              (table_mapping.get("attributes") or {}).values() if column)
        source_columns.update(str(item.get("column")) for item in
                              (table_mapping.get("relations") or []) if item.get("column"))
        if table_mapping.get("name"):
            source_columns.add(str(table_mapping["name"]))
    if junction_mapping:
        source_columns.update(str(junction_mapping.get(key)) for key in
                              ("source_column", "target_column") if junction_mapping.get(key))
    source_columns.add(primary_key)
    source_columns = sorted(source_columns)
    source_name = f"db:{profile_name}:{schema_name or 'default'}.{table_name}"
    source_namespace = f"db:{profile_name}:{schema_name or 'default'}"
    source_sha = _digest({
        "profile": profile_name, "schema": schema_name, "table": table_name,
        "primary_key": primary_key, "snapshot_id": snapshot_id,
        "mapping_checksum": mapping_set.get("checksum"),
    })
    rows = database_connector.stream_table_rows(
        profile_name=profile_name, tenant_id=tenant_id, schema_name=schema_name,
        table_name=table_name, columns=source_columns, primary_key=primary_key,
        batch_size=max(1, min(int(batch_size), MAX_EXECUTION_ROWS)),
    )
    return execute_approved_dataset_stream(
        tenant_id=tenant_id, mapping_set_id=mapping_set_id, rows=rows,
        source_name=source_name, snapshot_id=snapshot_id, actor=actor,
        source_sha256=source_sha, sync_mode="upsert",
        source_id_column=primary_key, batch_size=batch_size,
        reconcile_missing=reconcile_missing, source_namespace=source_namespace,
        source_table=table_name,
    )
