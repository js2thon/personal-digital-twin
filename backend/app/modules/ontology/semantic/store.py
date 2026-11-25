"""语义层持久化（PostgreSQL 权威存储 · 全新增量表）。

只依赖 app.db.session 的标准 fetch/execute/transaction；JSONB 一律用
`CAST(:p AS jsonb)` + json.dumps 字符串，避免驱动类型歧义。
DDL 幂等，逐条独立执行，可反复调用。所有读路径带 tenant_id，
供 app/policy.read_clause 逐点过滤。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.db import session as db

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def jdump(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, default=str)


def jload(value: Any, default: Any = None):
    """兼容 psycopg3（已反序列化）与手写字符串两种形态。"""
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def split_statements(sql: str) -> list[str]:
    """按分号切分 DDL；跳过纯注释行（schema.sql 无 $$ 块，逻辑足够）。"""
    out, buf = [], []
    for line in sql.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        if s.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    if "\n".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


def ensure_schema() -> dict:
    """启动/维护时幂等建表；返回执行统计。失败向上抛（由调用方 try/except）。"""
    stmts = split_statements(SCHEMA_PATH.read_text(encoding="utf-8"))
    for stmt in stmts:
        db.execute(stmt)
    return {"statements": len(stmts), "schema_file": SCHEMA_PATH.name,
            "tables": 7, "durable": True}


def ping() -> bool:
    try:
        return db.fetch_one("SELECT 1 AS ok") is not None
    except Exception:  # noqa: BLE001
        return False


def counts() -> dict:
    """各语义表规模，供 health / metrics 自证。"""
    tables = ["semantic_schemas", "entity_instances", "instance_relations",
              "business_rules", "runtime_task_snapshots",
              "decision_rule_trace", "semantic_write_audit"]
    out: dict[str, int] = {}
    for t in tables:
        try:
            row = db.fetch_one(f"SELECT count(*) AS n FROM ontology.{t}")
            out[t] = int(row["n"]) if row else 0
        except Exception:  # noqa: BLE001
            out[t] = -1
    return out


# ---------------------------------------------------------------- Schema 快照
def upsert_semantic_schema(schema: dict) -> None:
    db.execute(
        "INSERT INTO ontology.semantic_schemas "
        "(schema_version, ontology_version, tenant_id, definition, status, created_by) "
        "VALUES (:sv, :ov, 'default', CAST(:def AS jsonb), 'published', 'system') "
        "ON CONFLICT (schema_version) DO UPDATE SET "
        " definition = EXCLUDED.definition, ontology_version = EXCLUDED.ontology_version, "
        " status = EXCLUDED.status",
        {"sv": schema.get("schema_version", "sem-1.0.0"),
         "ov": schema.get("ontology_version", "v1.0.0"),
         "def": jdump(schema)},
    )


def load_semantic_schema(schema_version: str | None = None) -> dict | None:
    if schema_version:
        row = db.fetch_one(
            "SELECT definition FROM ontology.semantic_schemas "
            "WHERE schema_version = :sv AND status = 'published'", {"sv": schema_version})
    else:
        row = db.fetch_one(
            "SELECT definition FROM ontology.semantic_schemas "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1")
    return jload(row["definition"]) if row else None


def list_schema_versions() -> list[dict]:
    return db.fetch_all(
        "SELECT schema_version, ontology_version, status, created_at "
        "FROM ontology.semantic_schemas ORDER BY created_at DESC")


# ---------------------------------------------------------------- 实例 / 关系
def insert_instance(rec: dict) -> str:
    return str(db.insert_returning_id("ontology.entity_instances", {
        "tenant_id": rec.get("tenant_id", "default"),
        "space_id": rec.get("space_id", "default"),
        "type_name": rec["type_name"],
        "canonical_name": rec.get("canonical_name", ""),
        "properties": jdump(rec.get("properties") or {}),
        "source_fact_ids": jdump(rec.get("source_fact_ids") or []),
        "ontology_version": rec.get("ontology_version", "v1.0.0"),
        "status": "active",
        "created_by": rec.get("created_by", "system"),
    }))


def update_instance(rec: dict) -> str:
    """更新已有实例的可变部分（canonical_name / properties）。

    用于状态机流转等更新场景：调用方须先用 get_instance 取旧值做转换校验，
    这里只落库，不重复校验。带 tenant_id 防越租户改。
    """
    iid = str(rec["id"])
    db.execute(
        "UPDATE ontology.entity_instances SET canonical_name = :n, "
        "properties = CAST(:p AS jsonb), ontology_version = :ov, updated_at = now() "
        "WHERE id = CAST(:i AS uuid) AND tenant_id = :t",
        {"n": rec.get("canonical_name", ""),
         "p": jdump(rec.get("properties") or {}),
         "ov": rec.get("ontology_version", "v1.0.0"),
         "i": iid, "t": rec.get("tenant_id", "default")},
    )
    return iid


def insert_relation(rec: dict) -> str:
    return str(db.insert_returning_id("ontology.instance_relations", {
        "tenant_id": rec.get("tenant_id", "default"),
        "space_id": rec.get("space_id", "default"),
        "source_id": str(rec["source_id"]),
        "relation_type": rec["relation_type"],
        "target_id": str(rec["target_id"]),
        "source_type": rec.get("source_type"),
        "target_type": rec.get("target_type"),
        "derived": bool(rec.get("derived", False)),
        "derived_by": rec.get("derived_by"),
        "ontology_version": rec.get("ontology_version", "v1.0.0"),
        "properties": jdump(rec.get("properties") or {}),
        "source_fact_ids": jdump(rec.get("source_fact_ids") or []),
        "status": rec.get("status", "active"),
        "created_by": rec.get("created_by", "system"),
    }))


def list_instances(tenant_id: str, where: str = "", params: dict | None = None,
                   limit: int = 200) -> list[dict]:
    p = {"t": tenant_id, "lim": limit}
    p.update(params or {})
    rows = db.fetch_all(
        "SELECT * FROM ontology.entity_instances WHERE tenant_id = :t "
        + (f" AND {where}" if where else "")
        + " ORDER BY created_at DESC LIMIT :lim", p)
    return [_decode(r) for r in rows]


def list_relations(tenant_id: str, where: str = "", params: dict | None = None,
                   limit: int = 500) -> list[dict]:
    p = {"t": tenant_id, "lim": limit}
    p.update(params or {})
    rows = db.fetch_all(
        "SELECT * FROM ontology.instance_relations WHERE tenant_id = :t "
        + (f" AND {where}" if where else "")
        + " ORDER BY created_at DESC LIMIT :lim", p)
    return [_decode(r) for r in rows]


def lock_relation_cardinality(tenant_id: str, relation_type: str,
                              source_id: str, target_id: str,
                              cardinality: str) -> None:
    """Serialize writes that compete for a one-valued relation endpoint.

    Must be called inside db.transaction(); transaction-scoped advisory locks
    prevent two concurrent writers from both passing the same cardinality check.
    """
    keys = []
    base = f"ontology-relation:{tenant_id}:{relation_type}"
    if cardinality in ("one_to_one", "many_to_one"):
        keys.append(f"{base}:source:{source_id}")
    if cardinality in ("one_to_one", "one_to_many"):
        keys.append(f"{base}:target:{target_id}")
    for key in sorted(set(keys)):
        db.fetch_one(
            "SELECT pg_advisory_xact_lock(hashtext(:lock_key)::bigint) AS locked",
            {"lock_key": key},
        )


def find_relation_cardinality_conflicts(tenant_id: str, relation_type: str,
                                      source_id: str, target_id: str,
                                      cardinality: str) -> list[dict]:
    """Fetch only active authoritative edges that can conflict with this relation.

    The result is intentionally bounded: one conflicting row is enough to reject
    a write. This avoids loading a tenant-wide relation list for every insert.
    """
    conditions = []
    if cardinality in ("one_to_one", "many_to_one"):
        conditions.append(
            "(source_id = CAST(:s AS uuid) AND target_id <> CAST(:tgt AS uuid))"
        )
    if cardinality in ("one_to_one", "one_to_many"):
        conditions.append(
            "(target_id = CAST(:tgt AS uuid) AND source_id <> CAST(:s AS uuid))"
        )
    if not conditions:
        return []
    rows = db.fetch_all(
        "SELECT id, source_id, relation_type, target_id, status, derived "
        "FROM ontology.instance_relations "
        "WHERE tenant_id = :t AND relation_type = :rt "
        "AND derived = false AND status = 'active' "
        "AND (" + " OR ".join(conditions) + ") LIMIT 2",
        {"t": tenant_id, "rt": relation_type, "s": source_id, "tgt": target_id},
    )
    return [_decode(r) for r in rows]


def graph_for_impact(tenant_id: str) -> tuple[list[dict], list[dict]]:
    """为影响分析提供完整图数据：图谱实体 + active 关系（汇报链等在此）。

    语义层实例表只有少量断言关系（reports_to 常仅 1 条），覆盖不了组织汇报链；
    权威汇报链在 ontology.relations。这里直接读图谱，保持只读。
    """
    nodes = db.fetch_all(
        "SELECT id::text, canonical_name, entity_type FROM ontology.entities "
        "WHERE status IN ('active','candidate') AND tenant_id = :t LIMIT 5000",
        {"t": tenant_id})
    edges = db.fetch_all(
        "SELECT source_entity_id::text AS source_id, target_entity_id::text AS target_id, "
        "relation_type FROM ontology.relations "
        "WHERE status = 'active' AND tenant_id = :t LIMIT 20000",
        {"t": tenant_id})
    return [dict(r) for r in nodes], [dict(r) for r in edges]


def get_instance(instance_id: str, tenant_id: str) -> dict | None:
    row = db.fetch_one(
        "SELECT * FROM ontology.entity_instances WHERE id = CAST(:i AS uuid) "
        "AND tenant_id = :t", {"i": instance_id, "t": tenant_id})
    return _decode(row) if row else None


def _decode(row: dict) -> dict:
    d = dict(row)
    for key in ("properties", "source_fact_ids", "definition", "condition", "action",
                "schema_snapshot", "server_context", "cases", "parameters", "answer",
                "evidence_refs", "applied_rules", "result",
                "derived_candidates", "violations", "result_fingerprint"):
        if key in d and isinstance(d[key], str):
            d[key] = jload(d[key], d[key])
    if "id" in d:
        d["id"] = str(d["id"])
    return d


# ---------------------------------------------------------------- 业务规则
def upsert_rule(rule: dict, tenant_id: str = "default") -> None:
    db.execute(
        "INSERT INTO ontology.business_rules "
        "(tenant_id, version_id, name, rule_type, condition, action, priority, enabled, created_by) "
        "VALUES (:t, :v, :n, :rt, CAST(:c AS jsonb), CAST(:a AS jsonb), :p, :e, 'system') "
        "ON CONFLICT (tenant_id, name) DO UPDATE SET "
        " rule_type = EXCLUDED.rule_type, condition = EXCLUDED.condition, "
        " action = EXCLUDED.action, priority = EXCLUDED.priority, enabled = EXCLUDED.enabled",
        {"t": tenant_id, "v": rule.get("version_id"),
         "n": rule["name"], "rt": rule.get("rule_type", "integrity"),
         "c": jdump(rule.get("condition") or {}),
         "a": jdump(rule.get("action") or {}),
         "p": int(rule.get("priority") or 100),
         "e": bool(rule.get("enabled", True))},
    )


def list_rules(tenant_id: str) -> list[dict]:
    rows = db.fetch_all(
        "SELECT name, rule_type, condition, action, priority, enabled "
        "FROM ontology.business_rules WHERE tenant_id = :t "
        "ORDER BY priority ASC, name ASC", {"t": tenant_id})
    return [_decode(r) for r in rows]


def audit_write(rec: dict) -> None:
    db.execute(
        "INSERT INTO ontology.semantic_write_audit "
        "(tenant_id, operation, target_ref, outcome, violations, ontology_version, created_by) "
        "VALUES (:t, :op, :ref, :out, CAST(:v AS jsonb), :ov, :by)",
        {"t": rec.get("tenant_id", "default"), "op": rec["operation"],
         "ref": rec.get("target_ref"), "out": rec["outcome"],
         "v": jdump(rec.get("violations") or []),
         "ov": rec.get("ontology_version"), "by": rec.get("created_by", "system")},
    )
