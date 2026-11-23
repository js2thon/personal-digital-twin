"""Approved mapping execution with idempotency, atomic writes and durable lineage.

Only uploaded tabular datasets are executed in this increment. External SQL
connectors are intentionally not opened from user-supplied connection strings.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app.db import session as db
from app.modules.ontology import mapping_registry
from app.modules.ontology.mapping_pipeline import _coerce_value, _schema_parts, validate_mappings
from app.modules.ontology.semantic.service import OntologyService

MAX_EXECUTION_ROWS = 500


def ensure_schema() -> None:
    mapping_registry.ensure_schema()
    db.execute("CREATE SCHEMA IF NOT EXISTS ontology")
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_ingestion_runs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        mapping_set_id UUID NOT NULL REFERENCES ontology.dataset_mapping_sets(id),
        source_name VARCHAR(500) NOT NULL,
        source_sha256 VARCHAR(64) NOT NULL,
        status VARCHAR(20) NOT NULL CHECK (status IN ('running','succeeded','failed')),
        rows_received INTEGER NOT NULL DEFAULT 0,
        rows_inserted INTEGER NOT NULL DEFAULT 0,
        rows_skipped INTEGER NOT NULL DEFAULT 0,
        instances_inserted INTEGER NOT NULL DEFAULT 0,
        error JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by VARCHAR(160) NOT NULL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_ingestion_records (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        mapping_set_id UUID NOT NULL REFERENCES ontology.dataset_mapping_sets(id),
        source_key VARCHAR(128) NOT NULL,
        source_name VARCHAR(500) NOT NULL,
        source_row INTEGER NOT NULL,
        row_hash VARCHAR(64) NOT NULL,
        entity_type VARCHAR(200) NOT NULL,
        instance_id UUID NOT NULL,
        ingestion_run_id UUID NOT NULL REFERENCES ontology.dataset_ingestion_runs(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, mapping_set_id, source_key, source_row, entity_type)
    )
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_source_records (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        mapping_name VARCHAR(200) NOT NULL,
        source_namespace VARCHAR(500) NOT NULL DEFAULT 'default',
        source_record_id VARCHAR(500) NOT NULL,
        entity_type VARCHAR(200) NOT NULL,
        instance_id UUID,
        row_hash VARCHAR(64) NOT NULL,
        mapping_checksum VARCHAR(64) NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT uq_dataset_source_records_source
            UNIQUE (tenant_id, mapping_name, source_namespace, source_record_id, entity_type)
    )
    """)
    db.execute("""
    ALTER TABLE ontology.dataset_source_records
    ADD COLUMN IF NOT EXISTS source_namespace VARCHAR(500) NOT NULL DEFAULT 'default'
    """)
    db.execute("""
    DO $migrate$
    DECLARE old_constraint RECORD;
    BEGIN
        FOR old_constraint IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'ontology.dataset_source_records'::regclass
              AND contype = 'u'
              AND pg_get_constraintdef(oid) =
                  'UNIQUE (tenant_id, mapping_name, source_record_id, entity_type)'
        LOOP
            EXECUTE format('ALTER TABLE ontology.dataset_source_records DROP CONSTRAINT %I',
                           old_constraint.conname);
        END LOOP;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'ontology.dataset_source_records'::regclass
              AND conname = 'uq_dataset_source_records_source'
        ) THEN
            ALTER TABLE ontology.dataset_source_records
            ADD CONSTRAINT uq_dataset_source_records_source
            UNIQUE (tenant_id, mapping_name, source_namespace, source_record_id, entity_type);
        END IF;
    END $migrate$;
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS idx_dataset_source_records_instance
    ON ontology.dataset_source_records (tenant_id, instance_id)
    """)
    db.execute("""
    CREATE TABLE IF NOT EXISTS ontology.dataset_field_lineage (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id VARCHAR(120) NOT NULL,
        ingestion_run_id UUID NOT NULL REFERENCES ontology.dataset_ingestion_runs(id),
        ingestion_record_id UUID NOT NULL REFERENCES ontology.dataset_ingestion_records(id),
        instance_id UUID NOT NULL,
        entity_type VARCHAR(200) NOT NULL,
        property_name VARCHAR(200) NOT NULL,
        source_name VARCHAR(500) NOT NULL,
        source_row INTEGER NOT NULL,
        source_column VARCHAR(200) NOT NULL,
        raw_value JSONB,
        transformed_value JSONB,
        mapping_set_id UUID NOT NULL,
        mapping_version INTEGER NOT NULL,
        ontology_version VARCHAR(100),
        schema_version VARCHAR(100),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (ingestion_record_id, property_name)
    )
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS idx_ingestion_records_instance
    ON ontology.dataset_ingestion_records (tenant_id, instance_id)
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS idx_field_lineage_source
    ON ontology.dataset_field_lineage (tenant_id, source_name, source_row)
    """)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _normalize_source_namespace(value: str | None) -> str:
    """Return a bounded logical source identity; preserve legacy rows under 'default'."""
    namespace = str(value or "default").strip()
    if not namespace:
        raise ValueError("source_namespace 不能为空")
    if len(namespace) > 500:
        raise ValueError("source_namespace 长度不能超过 500")
    return namespace


def _validated_source_ids(rows: list[dict], source_id_column: str | None) -> list[str]:
    """Validate a stable business key for every row in an upsert batch."""
    if not source_id_column or not str(source_id_column).strip():
        raise ValueError("upsert 模式必须指定 source_id_column")
    column = str(source_id_column).strip()
    if len(column) > 200:
        raise ValueError("source_id_column 长度不能超过 200")
    result: list[str] = []
    seen: set[str] = set()
    for row_no, row in enumerate(rows, start=1):
        value = row.get(column)
        if value is None or not str(value).strip():
            raise ValueError(f"第 {row_no} 行缺少稳定源记录标识字段：{column}")
        source_id = str(value).strip()
        if len(source_id) > 500:
            raise ValueError(f"第 {row_no} 行源记录标识长度超过 500")
        if source_id in seen:
            raise ValueError(f"数据集中源记录标识重复：{source_id}")
        seen.add(source_id)
        result.append(source_id)
    return result


def _create_run(*, tenant_id, mapping_set_id, source_name, source_sha256,
                rows_received, actor, status="running", error=None) -> dict:
    return db.fetch_one(
        "INSERT INTO ontology.dataset_ingestion_runs "
        "(tenant_id, mapping_set_id, source_name, source_sha256, status, "
        " rows_received, created_by, error) "
        "VALUES (:t, CAST(:mid AS uuid), :src, :sha, :st, :n, :by, CAST(:err AS jsonb)) "
        "RETURNING id::text AS id",
        {"t": tenant_id, "mid": mapping_set_id, "src": source_name,
         "sha": source_sha256, "st": status, "n": rows_received,
         "by": actor, "err": _json(error or {})},
    )


def execute_approved_dataset(*, tenant_id: str, mapping_set_id: str,
                              rows: list[dict], source_name: str,
                              source_sha256: str, actor: str,
                              max_rows: int = MAX_EXECUTION_ROWS,
                              sync_mode: str = "append",
                              source_id_column: str | None = None,
                              source_namespace: str = "default") -> dict:
    """Atomically instantiate rows using a tenant-owned approved mapping set.

    Append mode uses source file hash + row number. Upsert mode requires a stable
    source ID column and updates the existing ontology instance when the row or
    approved mapping checksum changes. Missing rows are never deleted implicitly.
    """
    ensure_schema()
    if not rows:
        raise ValueError("数据集没有数据行")
    cap = max(1, min(int(max_rows), MAX_EXECUTION_ROWS))
    if len(rows) > cap:
        raise ValueError(f"本次数据行数 {len(rows)} 超过执行上限 {cap}；请分批执行")
    namespace = _normalize_source_namespace(source_namespace)
    if sync_mode not in {"append", "upsert"}:
        raise ValueError("sync_mode 仅支持 append 或 upsert")
    if sync_mode == "upsert":
        stable_source_ids = _validated_source_ids(rows, source_id_column)
    else:
        if source_id_column:
            raise ValueError("仅 upsert 模式允许指定 source_id_column")
        stable_source_ids = [None] * len(rows)
    mapping_set = mapping_registry.get(mapping_set_id, tenant_id=tenant_id)
    if not mapping_set:
        raise LookupError("映射版本不存在或不属于当前租户")
    if mapping_set.get("status") != "approved":
        raise PermissionError("只有 approved 状态的映射版本可以执行")
    definition = mapping_set.get("definition") or {}
    schema = definition.get("ontology_schema") or {}
    mappings = definition.get("mappings") or []
    validation = validate_mappings(mappings, schema)
    if not validation["valid"]:
        raise ValueError("映射版本当前未通过结构校验")
    _, attrs = _schema_parts(schema)
    attr_index = {(a["entity_type"], a["name"]): a for a in attrs}
    source_key = hashlib.sha256(
        f"{source_sha256}:{mapping_set_id}".encode("utf-8")).hexdigest()
    run_id = None
    inserted_records, skipped_records, inserted_instances, updated_instances = 0, 0, 0, 0
    mapping_checksum = str(mapping_set.get("checksum") or _digest(definition))
    try:
        with db.transaction():
            run = _create_run(
                tenant_id=tenant_id, mapping_set_id=mapping_set_id,
                source_name=source_name, source_sha256=source_sha256,
                rows_received=len(rows), actor=actor)
            run_id = run["id"]
            service = OntologyService(schema=schema, tenant_id=tenant_id, actor=actor)
            # Bind the instance records to the versions frozen in the approved mapping.
            service.ov = str(mapping_set.get("ontology_version") or service.ov)
            for row_index, row in enumerate(rows):
                row_no = row_index + 1
                row_hash = _digest(row)
                stable_source_id = stable_source_ids[row_index]
                grouped: dict[str, dict] = {}
                source_columns: dict[str, dict] = {}
                for mapping in mappings:
                    entity, prop, source = (mapping["entity_type"],
                                            mapping["property"], mapping["source"])
                    spec = attr_index.get((entity, prop), {})
                    raw = row.get(source)
                    value, error = _coerce_value(raw, spec.get("range", "string"))
                    if error:
                        raise ValueError(f"第 {row_no} 行字段 {source} 转换失败：{error}")
                    grouped.setdefault(entity, {})[prop] = value
                    source_columns.setdefault(entity, {})[prop] = {
                        "column": source, "raw": raw, "value": value}
                for entity, properties in grouped.items():
                    source_state = None
                    if sync_mode == "upsert":
                        source_state = db.fetch_one(
                            "SELECT id::text AS id, instance_id::text AS instance_id, "
                            "row_hash, mapping_checksum FROM ontology.dataset_source_records "
                            "WHERE tenant_id=:t AND mapping_name=:mn AND source_namespace=:ns "
                            "AND source_record_id=:sid AND entity_type=:et FOR UPDATE",
                            {"t": tenant_id, "mn": str(mapping_set.get("name") or ""),
                             "ns": namespace, "sid": stable_source_id, "et": entity},
                        )
                        if not source_state:
                            created_state = db.fetch_one(
                                "INSERT INTO ontology.dataset_source_records "
                                "(tenant_id, mapping_name, source_namespace, source_record_id, entity_type, "
                                " instance_id, row_hash, mapping_checksum) "
                                "VALUES (:t, :mn, :ns, :sid, :et, NULL, :rh, :mc) "
                                "ON CONFLICT (tenant_id, mapping_name, source_namespace, source_record_id, entity_type) "
                                "DO NOTHING RETURNING id::text AS id",
                                {"t": tenant_id, "mn": str(mapping_set.get("name") or ""),
                                 "ns": namespace, "sid": stable_source_id, "et": entity,
                                 "rh": row_hash, "mc": mapping_checksum},
                            )
                            if created_state:
                                source_state = {"id": created_state["id"], "instance_id": None,
                                                "row_hash": row_hash, "mapping_checksum": mapping_checksum}
                            else:
                                # Another transaction may have reserved the same source ID.
                                source_state = db.fetch_one(
                                    "SELECT id::text AS id, instance_id::text AS instance_id, "
                                    "row_hash, mapping_checksum FROM ontology.dataset_source_records "
                                    "WHERE tenant_id=:t AND mapping_name=:mn AND source_namespace=:ns "
                                    "AND source_record_id=:sid AND entity_type=:et FOR UPDATE",
                                    {"t": tenant_id, "mn": str(mapping_set.get("name") or ""),
                                     "ns": namespace, "sid": stable_source_id, "et": entity},
                                )
                        if not source_state:
                            raise RuntimeError("无法锁定稳定源记录，事务已中止")

                    # A file-level reservation preserves retry idempotency for both modes.
                    reservation = db.fetch_one(
                        "INSERT INTO ontology.dataset_ingestion_records "
                        "(tenant_id, mapping_set_id, source_key, source_name, source_row, "
                        " row_hash, entity_type, instance_id, ingestion_run_id) "
                        "VALUES (:t, CAST(:mid AS uuid), :sk, :src, :rn, :rh, :et, "
                        " gen_random_uuid(), CAST(:run AS uuid)) "
                        "ON CONFLICT (tenant_id, mapping_set_id, source_key, source_row, entity_type) "
                        "DO NOTHING RETURNING id::text AS id, instance_id::text AS instance_id",
                        {"t": tenant_id, "mid": mapping_set_id, "sk": source_key,
                         "src": source_name, "rn": row_no, "rh": row_hash,
                         "et": entity, "run": run_id},
                    )
                    if not reservation:
                        skipped_records += 1
                        continue

                    if (source_state and source_state.get("instance_id")
                            and source_state.get("row_hash") == row_hash
                            and source_state.get("mapping_checksum") == mapping_checksum):
                        iid = source_state["instance_id"]
                        db.execute(
                            "UPDATE ontology.dataset_ingestion_records SET instance_id=CAST(:iid AS uuid) "
                            "WHERE id=CAST(:id AS uuid)",
                            {"iid": iid, "id": reservation["id"]},
                        )
                        skipped_records += 1
                        continue

                    instance_payload = {
                        "type_name": entity,
                        "canonical_name": str(properties.get("name") or properties.get("full_name") or ""),
                        "properties": properties,
                    }
                    if source_state and source_state.get("instance_id"):
                        instance_payload["id"] = source_state["instance_id"]
                    result = service.write_instance(instance_payload)
                    iid = result["id"]
                    if source_state:
                        db.execute(
                            "UPDATE ontology.dataset_source_records SET instance_id=CAST(:iid AS uuid), "
                            "row_hash=:rh, mapping_checksum=:mc, last_seen_at=now() "
                            "WHERE id=CAST(:id AS uuid)",
                            {"iid": iid, "rh": row_hash, "mc": mapping_checksum,
                             "id": source_state["id"]},
                        )
                        if source_state.get("instance_id"):
                            updated_instances += 1
                        else:
                            inserted_instances += 1
                    else:
                        inserted_instances += 1
                    db.execute(
                        "UPDATE ontology.dataset_ingestion_records SET instance_id = CAST(:iid AS uuid) "
                        "WHERE id = CAST(:id AS uuid)",
                        {"iid": iid, "id": reservation["id"]},
                    )
                    inserted_records += 1
                    for prop, item in source_columns[entity].items():
                        db.execute(
                            "INSERT INTO ontology.dataset_field_lineage "
                            "(tenant_id, ingestion_run_id, ingestion_record_id, instance_id, "
                            " entity_type, property_name, source_name, source_row, source_column, "
                            " raw_value, transformed_value, mapping_set_id, mapping_version, "
                            " ontology_version, schema_version) "
                            "VALUES (:t, CAST(:run AS uuid), CAST(:rid AS uuid), CAST(:iid AS uuid), "
                            " :et, :pn, :src, :rn, :col, CAST(:raw AS jsonb), CAST(:val AS jsonb), "
                            " CAST(:mid AS uuid), :mv, :ov, :sv)",
                            {"t": tenant_id, "run": run_id, "rid": reservation["id"],
                             "iid": iid, "et": entity, "pn": prop, "src": source_name,
                             "rn": row_no, "col": item["column"], "raw": _json(item["raw"]),
                             "val": _json(item["value"]), "mid": mapping_set_id,
                             "mv": int(mapping_set.get("version") or 1),
                             "ov": mapping_set.get("ontology_version"),
                             "sv": mapping_set.get("schema_version")},
                        )
            db.execute(
                "UPDATE ontology.dataset_ingestion_runs SET status='succeeded', "
                " rows_inserted=:ri, rows_skipped=:rs, instances_inserted=:ii, finished_at=now() "
                "WHERE id=CAST(:id AS uuid)",
                {"ri": inserted_records, "rs": skipped_records,
                 "ii": inserted_instances, "id": run_id},
            )
    except Exception as exc:
        # The execution transaction has rolled back; preserve a separate failure record.
        with db.transaction():
            failed = _create_run(
                tenant_id=tenant_id, mapping_set_id=mapping_set_id,
                source_name=source_name, source_sha256=source_sha256,
                rows_received=len(rows), actor=actor, status="failed",
                error={"type": type(exc).__name__, "message": str(exc)[:1000]})
            db.execute(
                "UPDATE ontology.dataset_ingestion_runs SET finished_at=now() "
                "WHERE id=CAST(:id AS uuid)", {"id": failed["id"]})
        raise
    return {
        "run_id": run_id, "status": "succeeded",
        "rows_received": len(rows), "records_inserted": inserted_records,
        "records_skipped_idempotent": skipped_records,
        "instances_inserted": inserted_instances,
        "instances_updated": updated_instances,
        "sync_mode": sync_mode,
        "source_id_column": source_id_column,
        "source_namespace": namespace,
        "mapping_set_id": mapping_set_id,
        "mapping_version": mapping_set.get("version"),
        "ontology_version": mapping_set.get("ontology_version"),
        "schema_version": mapping_set.get("schema_version"),
        "source_sha256": source_sha256,
        "atomic": True, "idempotent": True,
    }


def get_run(run_id: str, *, tenant_id: str) -> dict | None:
    ensure_schema()
    row = db.fetch_one(
        "SELECT id::text AS id, mapping_set_id::text AS mapping_set_id, source_name, "
        " source_sha256, status, rows_received, rows_inserted, rows_skipped, "
        " instances_inserted, error, created_by, started_at, finished_at "
        "FROM ontology.dataset_ingestion_runs WHERE id=CAST(:id AS uuid) AND tenant_id=:t",
        {"id": run_id, "t": tenant_id})
    if not row:
        return None
    if isinstance(row.get("error"), str):
        try:
            row["error"] = json.loads(row["error"])
        except ValueError:
            row["error"] = {}
    return {k: (str(v) if isinstance(v, datetime) else v) for k, v in row.items()}


def get_instance_lineage(instance_id: str, *, tenant_id: str) -> list[dict]:
    ensure_schema()
    return db.fetch_all(
        "SELECT entity_type, property_name, source_name, source_row, source_column, "
        " raw_value, transformed_value, mapping_set_id::text AS mapping_set_id, "
        " mapping_version, ontology_version, schema_version, created_at "
        "FROM ontology.dataset_field_lineage WHERE instance_id=CAST(:id AS uuid) "
        " AND tenant_id=:t ORDER BY property_name",
        {"id": instance_id, "t": tenant_id},
    )
