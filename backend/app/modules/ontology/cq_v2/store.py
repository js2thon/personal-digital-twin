"""持久化层：MemoryStore（零依赖、可测）与 PostgresStore（默认，真实落库）。

契约（两种 Store 接口一致，核心逻辑无需改动）：
- `durable`：是否具备跨进程生命周期。memory=False，postgres=True。
- `insert/get/all/update/delete/clear`：对象读写。
- `snapshot()/restore(...)`：整机快照与回灌。快照含 `tables`（各表整行）与
  `state`（Schema Registry / 审计 / 基线的 KV），进程重启即靠它水合。
- `read_state/write_state/state_all`：非表结构的运行态键值。
- `status()/drift()`：内存态与库内态的规模与差异自证。

通过 `CQ_V2_STORE`（缺省 postgres）选择实现；库不可用时 `default_store()`
显式降级为 memory 并给出 `degraded_reason`，不静默假装持久化。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import models as m

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
STATE_TABLE = "_state"          # cq_v2_records 里承载运行态 KV 的伪表名

# ---------------------------------------------------------------- 双轨对照表
# 左：JSONB 副本（cq_v2_records）里的表名；右：schema.sql 里真正的规范化关系表。
# 两轨的键不一致（关系表有复合主键、业务唯一键），对账必须按自然键而不是 id。
TRACK_TABLES: dict[str, dict] = {
    "capabilities": {"table": "ontology.business_capabilities",
                     "keys": ("id",), "label": "业务能力"},
    "capability_cqs": {"table": "ontology.capability_cqs",
                       "keys": ("capability_id", "cq_id"), "label": "能力-CQ 关联"},
    "cq_requirements": {"table": "ontology.cq_requirements",
                        "keys": ("cq_id", "cq_version", "kind", "ref"),
                        "label": "本体需求项"},
    "schema_registry": {"table": "ontology.schema_registry",
                        "keys": ("schema_version",), "label": "Schema 版本"},
    "schema_migrations": {"table": "ontology.schema_migrations",
                          "keys": ("id",), "label": "Schema 迁移"},
    "schema_proposals": {"table": "ontology.schema_proposals",
                         "keys": ("id",), "label": "Schema 提案"},
    "extension_proposals": {"table": "ontology.extension_proposals",
                            "keys": ("id",), "label": "本体扩展提案"},
}

# 业务唯一键 → 库内主键对齐表（写之前先按这些列回查，避免另造 UUID）
_IDENTITY: dict[str, tuple[str, str]] = {
    "cqs": ("ontology.competency_questions", "code"),
    "capabilities": ("ontology.business_capabilities", "code"),
    "schema_proposals": ("ontology.schema_proposals", "code"),
    "schema_registry": ("ontology.schema_registry", "schema_version"),
}


def track_key(table: str, row: dict) -> str:
    """按自然键算一致性键；缺键的行走 id 兜底，绝不同表混用两种键。"""
    spec = TRACK_TABLES.get(table)
    if not spec:
        return str(row.get("id"))
    parts = []
    for k in spec["keys"]:
        val = row.get(k)
        if val is None and k == "id":
            val = row.get("id")
        parts.append("" if val is None else str(val))
    return "|".join(parts)


def split_statements(sql: str) -> list[str]:
    """按分号切分 DDL，保留 DO $$ ... $$ 块内部的分号。"""
    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        s = line.strip()
        if s.startswith("--") and not in_dollar:
            continue
        if "$$" in s:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and s.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    if "\n".join(buf).strip():
        out.append("\n".join(buf).strip())
    return out


def ddl_statements() -> list[str]:
    return split_statements(SCHEMA_PATH.read_text(encoding="utf-8"))


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ 关系轨写入
# 每条 SQL 都按「自然唯一键」冲突升级：重复播种/重复同步只更新，不新增行。
def _sql_capability(rec: dict) -> tuple[str, dict]:
    params = {"id": str(rec.get("id")), "code": rec.get("code") or "",
              "name": rec.get("name") or rec.get("code") or "",
              "domain": rec.get("business_domain") or "",
              "desc": rec.get("description"), "owner": rec.get("owner"),
              "pri": int(rec.get("priority") or 2),
              "risk": rec.get("risk_level") or "normal",
              "weight": float(rec.get("weight") or 1.0),
              "status": rec.get("status") or "active",
              "ver": int(rec.get("version") or 1)}
    sql = ("INSERT INTO ontology.business_capabilities "
           "(id, code, name, business_domain, description, owner, priority, "
           " risk_level, weight, status, version) VALUES "
           "(CAST(:id AS uuid), :code, :name, :domain, :desc, :owner, :pri, "
           " :risk, :weight, :status, :ver) "
           "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, "
           " business_domain = EXCLUDED.business_domain, "
           " description = EXCLUDED.description, owner = EXCLUDED.owner, "
           " priority = EXCLUDED.priority, risk_level = EXCLUDED.risk_level, "
           " weight = EXCLUDED.weight, status = EXCLUDED.status, "
           " version = EXCLUDED.version, updated_at = NOW()")
    return sql, params


def _sql_capability_cq(rec: dict) -> tuple[str, dict]:
    sql = ("INSERT INTO ontology.capability_cqs (capability_id, cq_id, link_weight) "
           "VALUES (CAST(:cap AS uuid), CAST(:cq AS uuid), :w) "
           "ON CONFLICT (capability_id, cq_id) DO UPDATE SET "
           " link_weight = EXCLUDED.link_weight")
    return sql, {"cap": str(rec.get("capability_id")), "cq": str(rec.get("cq_id")),
                 "w": float(rec.get("link_weight") or 1.0)}


def _sql_cq_requirement(rec: dict) -> tuple[str, dict]:
    sql = ("INSERT INTO ontology.cq_requirements "
           "(id, cq_id, cq_version, kind, ref, schema_version, mandatory, status) "
           "VALUES (CAST(:id AS uuid), CAST(:cq AS uuid), :v, :k, :ref, :sv, :m, :st) "
           "ON CONFLICT (cq_id, cq_version, kind, ref) DO UPDATE SET "
           " schema_version = EXCLUDED.schema_version, "
           " mandatory = EXCLUDED.mandatory, status = EXCLUDED.status")
    return sql, {"id": str(rec.get("id")), "cq": str(rec.get("cq_id")),
                 "v": int(rec.get("cq_version") or 1), "k": rec.get("kind") or "entity",
                 "ref": str(rec.get("ref") or ""),
                 "sv": rec.get("schema_version") or None,
                 "m": bool(rec.get("mandatory", True)),
                 "st": rec.get("status") or "open"}


def _sql_schema_migration(rec: dict) -> tuple[str, dict]:
    sql = ("INSERT INTO ontology.schema_migrations "
           "(id, from_version, to_version, diff, migration_plan, compatibility, "
           " data_migration, rollback_sql, rollback_version, applied_at, applied_by) "
           "VALUES (CAST(:id AS uuid), :fv, :tv, CAST(:diff AS jsonb), "
           " CAST(:plan AS jsonb), :comp, CAST(:dm AS jsonb), :rs, :rv, "
           " CAST(:at AS timestamptz), :by) "
           "ON CONFLICT (id) DO UPDATE SET diff = EXCLUDED.diff, "
           " migration_plan = EXCLUDED.migration_plan, "
           " compatibility = EXCLUDED.compatibility, "
           " data_migration = EXCLUDED.data_migration, "
           " rollback_sql = EXCLUDED.rollback_sql")
    return sql, {"id": str(rec.get("id")), "fv": rec.get("from_version") or None,
                 "tv": rec.get("to_version") or "",
                 "diff": _json(rec.get("diff") or {}),
                 "plan": _json(rec.get("migration_plan") or []),
                 "comp": rec.get("compatibility") or "unknown",
                 "dm": _json(rec.get("data_migration") or {}),
                 "rs": rec.get("rollback_sql"), "rv": rec.get("rollback_version"),
                 "at": rec.get("applied_at"), "by": rec.get("applied_by")}


def _sql_schema_proposal(rec: dict) -> tuple[str, dict]:
    sql = ("INSERT INTO ontology.schema_proposals "
           "(id, code, cq_id, term, operation, target_ref, reason, examples, "
           " synonyms, necessity, status, proposed_by, reviewed_by, reviewed_at, "
           " schema_version) VALUES "
           "(CAST(:id AS uuid), :code, CAST(NULLIF(:cq, '') AS uuid), :term, :op, "
           " :tr, :reason, CAST(:ex AS jsonb), CAST(:syn AS jsonb), "
           " CAST(:nec AS jsonb), :st, :pb, :rb, CAST(:ra AS timestamptz), :sv) "
           "ON CONFLICT (code) DO UPDATE SET term = EXCLUDED.term, "
           " operation = EXCLUDED.operation, target_ref = EXCLUDED.target_ref, "
           " reason = EXCLUDED.reason, examples = EXCLUDED.examples, "
           " synonyms = EXCLUDED.synonyms, necessity = EXCLUDED.necessity, "
           " status = EXCLUDED.status, reviewed_by = EXCLUDED.reviewed_by, "
           " reviewed_at = EXCLUDED.reviewed_at, schema_version = EXCLUDED.schema_version")
    return sql, {"id": str(rec.get("id")), "code": rec.get("code") or "",
                 "cq": str(rec.get("cq_id") or ""), "term": rec.get("term") or "",
                 "op": rec.get("operation") or "create", "tr": rec.get("target_ref"),
                 "reason": rec.get("reason") or "", "ex": _json(rec.get("examples") or []),
                 "syn": _json(rec.get("synonyms") or []),
                 "nec": _json(rec.get("necessity") or {}),
                 "st": rec.get("status") or "pending",
                 "pb": rec.get("proposed_by") or "llm", "rb": rec.get("reviewed_by"),
                 "ra": rec.get("reviewed_at"), "sv": rec.get("schema_version") or None}


def _sql_schema_version(rec: dict) -> tuple[str, dict]:
    """schema_registry 的 linkml_yaml 在这里生成：关系轨自己就是可复原的建模源。"""
    from . import linkml

    yaml_text = rec.get("linkml_yaml") or linkml.dumps(rec)
    sql = ("INSERT INTO ontology.schema_registry "
           "(id, schema_version, linkml_yaml, entity_types, relation_types, "
           " attributes, constraints, validation_rules, status, pinned_by_cq, "
           " published_by) VALUES "
           "(CAST(:id AS uuid), :ver, :yaml, CAST(:et AS jsonb), "
           " CAST(:rt AS jsonb), CAST(:at AS jsonb), CAST(:cs AS jsonb), "
           " CAST(:vr AS jsonb), :st, CAST(:pin AS jsonb), :by) "
           "ON CONFLICT (schema_version) DO UPDATE SET "
           " linkml_yaml = EXCLUDED.linkml_yaml, entity_types = EXCLUDED.entity_types, "
           " relation_types = EXCLUDED.relation_types, attributes = EXCLUDED.attributes, "
           " constraints = EXCLUDED.constraints, "
           " validation_rules = EXCLUDED.validation_rules, status = EXCLUDED.status, "
           " pinned_by_cq = EXCLUDED.pinned_by_cq, published_by = EXCLUDED.published_by")
    return sql, {"id": str(rec.get("id")), "ver": rec.get("schema_version") or "",
                 "yaml": yaml_text,
                 "et": _json(rec.get("entity_types") or {}),
                 "rt": _json(rec.get("relation_types") or {}),
                 "at": _json(rec.get("attributes") or {}),
                 "cs": _json(rec.get("constraints") or {}),
                 "vr": _json(rec.get("validation_rules") or {}),
                 "st": rec.get("status") or "published",
                 "pin": _json(rec.get("pinned_by_cq") or []),
                 "by": rec.get("published_by")}


def _sql_extension_proposal(rec: dict) -> tuple[str, dict]:
    sql = ("INSERT INTO ontology.extension_proposals (id, proposal, rationale, status) "
           "VALUES (CAST(:id AS uuid), CAST(:p AS jsonb), :r, :st) "
           "ON CONFLICT (id) DO UPDATE SET proposal = EXCLUDED.proposal, "
           " rationale = EXCLUDED.rationale, status = EXCLUDED.status")
    return sql, {"id": str(rec.get("id")),
                 "p": _json(rec.get("proposal") or {}),
                 "r": rec.get("rationale"), "st": rec.get("status") or "pending"}


_REL_WRITERS = {
    "capabilities": _sql_capability,
    "capability_cqs": _sql_capability_cq,
    "cq_requirements": _sql_cq_requirement,
    "schema_migrations": _sql_schema_migration,
    "schema_proposals": _sql_schema_proposal,
    "schema_registry": _sql_schema_version,
    "extension_proposals": _sql_extension_proposal,
}

_REL_DELETES = {
    "capabilities": ("DELETE FROM ontology.business_capabilities WHERE id = CAST(:id AS uuid)",
                     lambda r: {"id": str(r.get("id"))}),
    "capability_cqs": ("DELETE FROM ontology.capability_cqs "
                       "WHERE capability_id = CAST(:cap AS uuid) "
                       "  AND cq_id = CAST(:cq AS uuid)",
                       lambda r: {"cap": str(r.get("capability_id")),
                                  "cq": str(r.get("cq_id"))}),
    "cq_requirements": ("DELETE FROM ontology.cq_requirements WHERE id = CAST(:id AS uuid)",
                        lambda r: {"id": str(r.get("id"))}),
    "schema_migrations": ("DELETE FROM ontology.schema_migrations WHERE id = CAST(:id AS uuid)",
                          lambda r: {"id": str(r.get("id"))}),
    "schema_proposals": ("DELETE FROM ontology.schema_proposals WHERE id = CAST(:id AS uuid)",
                         lambda r: {"id": str(r.get("id"))}),
    "schema_registry": ("DELETE FROM ontology.schema_registry "
                        "WHERE schema_version = :ver",
                        lambda r: {"ver": r.get("schema_version") or ""}),
    "extension_proposals": ("DELETE FROM ontology.extension_proposals "
                            "WHERE id = CAST(:id AS uuid)",
                            lambda r: {"id": str(r.get("id"))}),
}


class MemoryStore:
    """进程内存储；所有 V2 对象按表名分桶，行为与关系表一致。

    `durable=False`：进程退出即丢。单测与演示用它换速度与隔离；生产必须用
    PostgresStore，否则重启后只能靠重新播种（旧行为，P1-5 要消灭的正是这个）。
    """

    mode = "memory"
    durable = False
    degraded_reason = ""

    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}
        self.kv: dict[str, dict] = {}

    def ensure_schema(self) -> dict:
        return {"mode": self.mode, "durable": self.durable,
                "ddl_statements": len(ddl_statements()),
                "applied": False, "note": "memory store 不需要执行 DDL"}

    # ------------------------------------------------------------ 运行态 KV
    def read_state(self, key: str) -> dict | None:
        return self.kv.get(key)

    def write_state(self, key: str, value: dict) -> None:
        self.kv[str(key)] = value

    def state_all(self) -> dict[str, dict]:
        return dict(self.kv)

    # ------------------------------------------------------------ 整机快照
    def snapshot(self) -> dict:
        return {"tables": {t: [dict(r) for r in bucket.values()]
                           for t, bucket in self.tables.items()},
                "state": {k: v for k, v in self.kv.items()}}

    def restore(self, snapshot: dict) -> dict:
        """直接用快照重建内存态，绕开 insert/update，避免反向写库。"""
        self.tables.clear()
        self.kv.clear()
        for table, rows in (snapshot.get("tables") or {}).items():
            self.tables[table] = {str(r["id"]): dict(r) for r in rows if r.get("id")}
        self.kv = {str(k): v for k, v in (snapshot.get("state") or {}).items()}
        return self.status()

    def status(self) -> dict:
        counts = {t: len(b) for t, b in self.tables.items()}
        return {"mode": self.mode, "durable": self.durable,
                "degraded_reason": self.degraded_reason,
                "tables": counts, "total_objects": sum(counts.values()),
                "state_keys": sorted(self.kv)}

    def drift(self, snapshot: dict | None = None) -> dict:
        """与给定快照比对：缺哪些、多哪些。memory 模式下无外部副本可比。"""
        if snapshot is None:
            return {"comparable": False, "in_sync": False,
                    "reason": "memory store 无外部副本"}
        mine = {t: set(b) for t, b in self.tables.items()}
        other = {t: {str(r["id"]) for r in rows if r.get("id")}
                 for t, rows in (snapshot.get("tables") or {}).items()}
        missing = {t: sorted(ids - mine.get(t, set())) for t, ids in other.items()}
        extra = {t: sorted(ids - other.get(t, set())) for t, ids in mine.items()}
        missing = {k: v for k, v in missing.items() if v}
        extra = {k: v for k, v in extra.items() if v}
        state_missing = sorted(set(snapshot.get("state") or {}) - set(self.kv))
        return {"comparable": True,
                "missing_in_memory": missing, "extra_in_memory": extra,
                "state_missing_in_memory": state_missing,
                "in_sync": not (missing or extra or state_missing)}

    def insert(self, table: str, row: dict) -> dict:
        rec = dict(row)
        rec.setdefault("id", m.uid())
        self.tables.setdefault(table, {})[str(rec["id"])] = rec
        return rec

    def get(self, table: str, rid: str) -> dict | None:
        return self.tables.get(table, {}).get(str(rid))

    def all(self, table: str, **filters) -> list[dict]:
        rows = list(self.tables.get(table, {}).values())
        if filters:
            rows = [r for r in rows
                    if all(r.get(k) == v for k, v in filters.items() if v is not None)]
        return rows

    def update(self, table: str, rid: str, patch: dict) -> dict | None:
        cur = self.get(table, rid)
        if not cur:
            return None
        cur.update(patch)
        cur["updated_at"] = m.now()
        return cur

    def delete(self, table: str, rid: str) -> bool:
        return self.tables.get(table, {}).pop(str(rid), None) is not None

    def clear(self) -> None:
        self.tables.clear()
        self.kv.clear()

    def write_coverage(self, items: list[dict], gaps: list[dict], run_id: str) -> None:
        for it in items:
            self.insert("cq_coverage_v2", {**it, "run_id": run_id})
        for g in gaps:
            self.insert("cq_gaps", {**g, "run_id": run_id})

    # 双轨对账在 memory 模式下无意义：没有第二轨可比，如实说明而不是假装一致。
    def tracks(self, sample: int = 20) -> dict:
        return {"comparable": False, "in_sync": False,
                "reason": self.degraded_reason or "memory store 无关系轨"}

    def sync_tracks(self, prune: bool = False) -> dict:
        return {"comparable": False, "in_sync": False, "skipped": True,
                "reason": self.degraded_reason or "memory store 无关系轨"}

    def write_execution(self, record: dict) -> None:
        self.insert("cq_execution_runs", record)

    def write_assessment(self, record: dict) -> None:
        self.insert("cq_quality_assessments", record)


class PostgresStore(MemoryStore):
    """真实持久化：通用 JSONB 表 + 关键关系表双写（供视图与报表消费）。

    内存态是读模型（快），`ontology.cq_v2_records` 是权威副本（稳）。写入即
    双写；进程重启后靠 `load_snapshot()` 把副本整机回灌，无需重新播种。
    """

    mode = "postgres"
    durable = True

    def __init__(self, db=None):
        super().__init__()
        if db is None:
            from app.db import session as db  # noqa: N813
        self.db = db

    # ------------------------------------------------------------ 连通性
    def ping(self) -> bool:
        try:
            self.db.fetch_one("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001
            return False

    def ensure_schema(self) -> dict:
        stmts = ddl_statements()
        applied, errors = 0, []
        for stmt in stmts:
            try:
                self.db.execute(stmt)
                applied += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc)[:160])
        return {"mode": self.mode, "durable": self.durable,
                "ddl_statements": len(stmts), "applied": applied, "errors": errors}

    # ------------------------------------------------------------ 运行态 KV
    def write_state(self, key: str, value: dict) -> None:
        super().write_state(key, value)
        self._put_record(STATE_TABLE, str(key), {"id": str(key), "value": value})

    def _put_record(self, table: str, rid: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO ontology.cq_v2_records (table_name, id, payload) "
            "VALUES (:t, :i, CAST(:p AS jsonb)) "
            "ON CONFLICT (table_name, id) DO UPDATE SET "
            "payload = EXCLUDED.payload, updated_at = NOW()",
            {"t": table, "i": str(rid), "p": _json(payload)})

    # ------------------------------------------------------------ 整机快照
    def load_snapshot(self) -> dict:
        """从 cq_v2_records 读出完整副本：`_state` 行进 KV，其余按表名归桶。"""
        rows = self.db.fetch_all(
            "SELECT table_name, id, payload FROM ontology.cq_v2_records")
        tables: dict[str, list[dict]] = {}
        state: dict[str, dict] = {}
        for r in rows:
            payload = r.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            if r["table_name"] == STATE_TABLE:
                state[str(r["id"])] = payload.get("value") if isinstance(payload, dict) else None
            else:
                rec = dict(payload) if isinstance(payload, dict) else {}
                rec.setdefault("id", r["id"])
                tables.setdefault(r["table_name"], []).append(rec)
        state = {k: v for k, v in state.items() if v is not None}
        return {"tables": tables, "state": state}

    def flush_all(self, tables: list[str] | None = None) -> dict:
        """把内存态全量写回库（用于漂移修复；正常路径是写穿透，不必调用）。"""
        snap = self.snapshot()
        written = 0
        for table, rows in snap["tables"].items():
            if tables and table not in tables:
                continue
            for row in rows:
                self._put_record(table, str(row["id"]), row)
                written += 1
        for key, value in snap["state"].items():
            self._put_record(STATE_TABLE, key, {"id": key, "value": value})
            written += 1
        return {"written": written, "tables": len(snap["tables"]),
                "state_keys": len(snap["state"])}

    def clear(self) -> None:
        super().clear()
        # 权威副本一并清空，否则下一次水合会把刚清掉的数据又灌回来
        self.db.execute("DELETE FROM ontology.cq_v2_records")

    def drift(self, snapshot: dict | None = None) -> dict:
        snap = snapshot if snapshot is not None else self.load_snapshot()
        d = super().drift(snap)
        if d.get("comparable"):
            d["db_objects"] = sum(len(v) for v in (snap.get("tables") or {}).values())
        return d

    def status(self) -> dict:
        st = super().status()
        try:
            row = self.db.fetch_one(
                "SELECT count(*) AS n FROM ontology.cq_v2_records "
                "WHERE table_name <> :t", {"t": STATE_TABLE})
            st["db_objects"] = int((row or {}).get("n") or 0)
        except Exception as exc:  # noqa: BLE001
            st["db_objects"] = None
            st["db_error"] = str(exc)[:160]
        return st

    def insert(self, table: str, row: dict) -> dict:
        row = self._align_identity(table, row)
        rec = super().insert(table, row)
        self._put_record(table, str(rec["id"]), rec)
        if table == "cqs":
            # 关系表的外键指向 ontology.competency_questions，必须同步落一条真实 CQ
            self._upsert_cq(rec)
        self._dual_write(table, rec)
        return rec

    # ------------------------------------------------------------ 双轨同步
    def _dual_write(self, table: str, rec: dict) -> bool:
        """写穿透的第二轨：JSONB 副本之外，同步落规范化关系表。

        关系表是给视图/报表/外部 SQL 消费的，但此前只有 `cqs` 一路写，其余
        （能力、能力-CQ 关联、需求项、Schema 版本/迁移/提案、扩展提案）只存在
        JSONB 里 —— 这就是「双轨分叉」。失败不抛：JSONB 已成功，关系轨可回来补。
        """
        writer = _REL_WRITERS.get(table)
        if not writer:
            return False
        try:
            sql, params = writer(rec)
            self.db.execute(sql, params)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("关系轨写入失败（%s）：%s", table, str(exc)[:160])
            return False

    def _dual_delete(self, table: str, rec: dict) -> bool:
        spec = _REL_DELETES.get(table)
        if not spec or not rec:
            return False
        sql, params_fn = spec
        try:
            self.db.execute(sql, params_fn(rec))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("关系轨删除失败（%s）：%s", table, str(exc)[:160])
            return False

    def tracks(self, sample: int = 20) -> dict:
        """JSONB 副本 vs 规范化关系表逐表对账（两侧都读库，不看内存读模型）。"""
        payloads: dict[str, dict[str, dict]] = {}
        try:
            for r in self.db.fetch_all(
                    "SELECT table_name, id, payload FROM ontology.cq_v2_records "
                    "WHERE table_name <> :t", {"t": STATE_TABLE}):
                payload = r.get("payload") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except ValueError:
                        payload = {}
                payload.setdefault("id", r["id"])
                payloads.setdefault(r["table_name"], {})[str(r["id"])] = payload
        except Exception:  # noqa: BLE001
            payloads = {}

        tables: dict[str, dict] = {}
        all_sync = True
        for name, spec in TRACK_TABLES.items():
            try:
                rows = self.db.fetch_all(f"SELECT * FROM {spec['table']}")
            except Exception as exc:  # noqa: BLE001
                tables[name] = {"table": spec["table"], "label": spec["label"],
                                "error": str(exc)[:160], "in_sync": False}
                all_sync = False
                continue
            copy_keys = {track_key(name, p) for p in (payloads.get(name) or {}).values()}
            rel_keys = {track_key(name, r) for r in rows}
            missing = sorted(copy_keys - rel_keys)
            extra = sorted(rel_keys - copy_keys)
            tables[name] = {
                "table": spec["table"], "label": spec["label"],
                "keys": list(spec["keys"]),
                "copy_rows": len(copy_keys), "relational_rows": len(rel_keys),
                "missing_in_relational": missing[:sample],
                "missing_count": len(missing),
                "extra_in_relational": extra[:sample],
                "extra_count": len(extra),
                "in_sync": not (missing or extra),
            }
            if missing or extra:
                all_sync = False
        return {"comparable": True, "in_sync": all_sync, "tables": tables,
                "copy_total": sum(t.get("copy_rows", 0) for t in tables.values()),
                "relational_total": sum(t.get("relational_rows", 0) for t in tables.values())}

    def sync_tracks(self, prune: bool = False) -> dict:
        """以 JSONB 副本为准补齐关系表；prune=True 时顺带删掉关系表里的多余行。"""
        snap = self.load_snapshot()
        written: dict[str, int] = {}
        failed: dict[str, str] = {}
        for table, rows in (snap.get("tables") or {}).items():
            if table not in _REL_WRITERS:
                continue
            ok = 0
            for row in rows:
                if self._dual_write(table, row):
                    ok += 1
                else:
                    failed[table] = "写入失败，详见日志"
            written[table] = ok
        pruned = 0
        if prune:
            for name in TRACK_TABLES:
                spec = TRACK_TABLES[name]
                try:
                    rows = self.db.fetch_all(f"SELECT * FROM {spec['table']}")
                except Exception:  # noqa: BLE001
                    continue
                copy_keys = {track_key(name, p)
                             for p in (snap.get("tables") or {}).get(name, [])}
                for row in rows:
                    if track_key(name, row) in copy_keys:
                        continue
                    if self._dual_delete(name, row):
                        pruned += 1
        report = self.tracks()
        return {"written": written, "written_total": sum(written.values()),
                "failed": failed, "pruned": pruned,
                "in_sync": report.get("in_sync"), "tables": report.get("tables")}

    def _align_identity(self, table: str, row: dict) -> dict:
        """写入前对齐「业务唯一键 → 库内主键」，避免 JSONB 轨与关系轨主键分叉。

        `competency_questions.code` 有唯一约束、且 `cq_coverage_v2 / cq_gaps /
        cq_test_instances` 等表都以外键指向它的 `id`。若 V2 播种另造 UUID，
        实时行仍是旧 id，后续写覆盖率就会撞外键。这里统一「以库为准」。
        能力（code）、Schema 提案（code）、Schema 版本（schema_version）同理。
        """
        spec = _IDENTITY.get(table)
        if not spec:
            return row
        tbl, col = spec
        if not row.get(col):
            return row
        try:
            hit = self.db.fetch_one(
                f"SELECT id::text AS id FROM {tbl} WHERE {col} = :v", {"v": row[col]})
        except Exception:  # noqa: BLE001  表还没建时直接走新 id
            return row
        if hit and hit.get("id") and str(hit["id"]) != str(row.get("id")):
            return {**row, "id": hit["id"]}
        return row

    def _upsert_cq(self, rec: dict) -> None:
        cols = {
            "i": str(rec["id"]), "c": rec.get("code", ""),
            "q": rec.get("question", ""), "sq": rec.get("sample_question") or None,
            "it": rec.get("intent", "lookup"), "st": rec.get("subject_type") or None,
            "ss": rec.get("subject_side", "source"), "sh": rec.get("answer_shape", "entity"),
            "rr": _json(rec.get("required_relations") or []),
            "ra": _json(rec.get("required_attributes") or []),
            "me": int(rec.get("min_evidence") or 1), "pr": int(rec.get("priority") or 2),
            "dm": rec.get("domain") or None, "sta": rec.get("status", "draft"),
            "ow": rec.get("owner") or None, "nt": rec.get("notes") or None,
            "qt": rec.get("question_template") or None,
            "bd": rec.get("business_domain") or None,
            "bp": rec.get("business_purpose") or None,
            "ck": rec.get("cq_kind", "business"), "tt": rec.get("target_type") or None,
            "sc": _json(rec.get("scope_constraints") or {}),
            "tc": _json(rec.get("temporal_constraints") or {}),
            "ea": _json(rec.get("expected_answer") or {}),
            "ac": _json(rec.get("acceptance_criteria") or {}),
            "er": _json(rec.get("evidence_requirements") or {}),
            "em": rec.get("evaluation_method") or None,
            "qs": rec.get("quality_score"), "qd": _json(rec.get("quality_dimensions") or {}),
            "hg": _json(rec.get("hard_gates") or {}),
            "rv": rec.get("review_status", "pending"),
            "ls": _json(rec.get("layer_states") or {}),
            "v": int(rec.get("version") or 1),
            "sv": rec.get("schema_version") or None,
            "cw": float(rec.get("cq_weight") or 1.0),
            "rl": rec.get("risk_level", "normal"),
        }
        self.db.execute(
            "INSERT INTO ontology.competency_questions "
            "(id, code, question, sample_question, intent, subject_type, subject_side, "
            " answer_shape, required_relations, required_attributes, min_evidence, "
            " priority, domain, status, owner, notes, question_template, business_domain, "
            " business_purpose, cq_kind, target_type, scope_constraints, "
            " temporal_constraints, expected_answer, acceptance_criteria, "
            " evidence_requirements, evaluation_method, quality_score, "
            " quality_dimensions, hard_gates, review_status, layer_states, version, "
            " schema_version, cq_weight, risk_level) VALUES "
            "(CAST(:i AS uuid), :c, :q, :sq, :it, :st, :ss, :sh, "
            " CAST(:rr AS jsonb), CAST(:ra AS jsonb), :me, :pr, :dm, :sta, :ow, :nt, "
            " :qt, :bd, :bp, :ck, :tt, CAST(:sc AS jsonb), CAST(:tc AS jsonb), "
            " CAST(:ea AS jsonb), CAST(:ac AS jsonb), CAST(:er AS jsonb), :em, "
            " :qs, CAST(:qd AS jsonb), CAST(:hg AS jsonb), :rv, CAST(:ls AS jsonb), "
            " :v, :sv, :cw, :rl) "
            "ON CONFLICT (code) DO UPDATE SET "
            "question = EXCLUDED.question, question_template = EXCLUDED.question_template, "
            "quality_score = EXCLUDED.quality_score, "
            "quality_dimensions = EXCLUDED.quality_dimensions, "
            "hard_gates = EXCLUDED.hard_gates, review_status = EXCLUDED.review_status, "
            "version = EXCLUDED.version, updated_at = NOW()", cols)

    def update(self, table: str, rid: str, patch: dict) -> dict | None:
        rec = super().update(table, rid, patch)
        if rec:
            self._put_record(table, str(rid), rec)
            if table == "cqs":
                self.db.execute(
                    "UPDATE ontology.competency_questions SET "
                    "quality_score = :qs, quality_dimensions = CAST(:qd AS jsonb), "
                    "hard_gates = CAST(:hg AS jsonb), review_status = :rv, "
                    "version = :v, updated_at = NOW() WHERE id = CAST(:i AS uuid)",
                    {"qs": rec.get("quality_score"),
                     "qd": _json(rec.get("quality_dimensions") or {}),
                     "hg": _json(rec.get("hard_gates") or {}),
                     "rv": rec.get("review_status", "pending"),
                     "v": int(rec.get("version") or 1), "i": str(rid)})
            self._dual_write(table, rec)
        return rec

    def delete(self, table: str, rid: str) -> bool:
        rec = self.get(table, rid)
        ok = super().delete(table, rid)
        if ok:
            self.db.execute(
                "DELETE FROM ontology.cq_v2_records WHERE table_name = :t AND id = :i",
                {"t": table, "i": str(rid)})
            if rec:
                self._dual_delete(table, rec)
        return ok

    def write_coverage(self, items: list[dict], gaps: list[dict], run_id: str) -> None:
        super().write_coverage(items, gaps, run_id)
        for it in items:
            self.db.execute(
                "INSERT INTO ontology.cq_coverage_v2 "
                "(run_id, cq_id, state, schema_coverage, data_coverage, "
                " evidence_coverage, answer_coverage, temporal_coverage, "
                " weighted_score, cq_weight, detail) VALUES "
                "(:r, CAST(:c AS uuid), :s, :sc, :dc, :ec, :ac, :tc, :w, :cw, CAST(:d AS jsonb))",
                {"r": run_id, "c": it.get("cq_id"), "s": it.get("state"),
                 "sc": it["dimensions"]["schema"], "dc": it["dimensions"]["data"],
                 "ec": it["dimensions"]["evidence"], "ac": it["dimensions"]["answer"],
                 "tc": it["dimensions"]["temporal"], "w": it.get("weighted_score"),
                 "cw": it.get("cq_weight"), "d": _json(it.get("validation") or {})})
        for g in gaps:
            self.db.execute(
                "INSERT INTO ontology.cq_gaps "
                "(run_id, cq_id, kind, ref, severity, business_impact, cq_weight, "
                " gap_priority, action) VALUES "
                "(:r, CAST(:c AS uuid), :k, :ref, :sev, :bi, :cw, :gp, CAST(:a AS jsonb))",
                {"r": run_id, "c": g.get("cq_id"), "k": g["kind"], "ref": g.get("ref"),
                 "sev": g["severity"], "bi": g["business_impact"],
                 "cw": g["cq_weight"], "gp": g["gap_priority"], "a": _json(g["action"])})

    def write_execution(self, record: dict) -> None:
        super().write_execution(record)
        self.db.execute(
            "INSERT INTO ontology.cq_execution_runs "
            "(run_id, cq_id, state, plan_version, schema_version, cq_version, "
            " data_snapshot_id, model_version, tenant_id, actor, answer, "
            " evidence_refs, conflict_refs, audit) VALUES "
            "(:r, CAST(:c AS uuid), :s, :pv, :sv, :cv, :ds, :mv, :ti, :a, "
            " CAST(:ans AS jsonb), CAST(:ev AS jsonb), CAST(:cf AS jsonb), CAST(:ad AS jsonb))",
            {"r": record.get("run_id"), "c": record.get("cq_id"), "s": record.get("state"),
             "pv": record.get("plan_version"), "sv": record.get("schema_version"),
             "cv": record.get("cq_version"), "ds": record.get("data_snapshot_id"),
             "mv": record.get("model_version"), "ti": record.get("tenant_id"),
             "a": record.get("actor"), "ans": _json(record.get("answer") or {}),
             "ev": _json(record.get("evidence_refs") or []),
             "cf": _json(record.get("conflicts") or []), "ad": _json(record.get("audit") or {})})


def requested_mode() -> str:
    """显式 env > settings > 缺省 postgres。env 优先，便于单测强制 memory。"""
    env = os.getenv("CQ_V2_STORE")
    if env:
        return env.strip().lower()
    try:
        from app.config import settings

        return str(getattr(settings, "cq_v2_store", "") or "postgres").lower()
    except Exception:  # noqa: BLE001
        return "postgres"


def production_mode() -> bool:
    """APP_ENV / settings.app_env 判定生产环境（大小写不敏感）。

    env 优先于 settings，便于启动闸门与单测在不改配置文件的前提下切换。
    """
    env = os.getenv("APP_ENV")
    if not env:
        try:
            from app.config import settings

            env = str(getattr(settings, "app_env", "") or "dev")
        except Exception:  # noqa: BLE001
            env = "dev"
    return env.strip().lower() in ("production", "prod")


def assert_production_store() -> dict:
    """启动闸门：生产环境禁止 MemoryStore（V1.3 Sprint 1 验收铁律第 1 条）。

    非生产环境直接返回 checked=false，行为与升级前逐行一致；生产环境下
    CQ_V2_STORE 非 postgres、或 PostgreSQL 不可达，一律抛 RuntimeError，由
    startup 冒泡使进程 fail-fast —— 绝不静默降级为进程内存储。
    """
    if not production_mode():
        return {"checked": False, "environment": "dev",
                "reason": "非生产环境，跳过 MemoryStore 强制检查"}
    if requested_mode() != "postgres":
        raise RuntimeError(
            "生产环境禁止 MemoryStore：CQ_V2_STORE 必须为 postgres "
            f"（当前 {requested_mode()!r}）")
    try:
        s = PostgresStore()
        if not s.ping():
            raise RuntimeError("生产环境 CQ V2 持久化不可用：PostgreSQL 不可达")
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "生产环境 CQ V2 持久化健康检查失败："
            f"{type(exc).__name__}: {str(exc)[:160]}") from exc
    return {"checked": True, "environment": "production", "mode": "postgres",
            "memory_store_allowed": False}


def default_store():
    """默认走 PostgresStore；连不上库才降级为 memory，并把原因记下来。

    production 下双闸拦截：显式 memory 配置与隐式降级都直接 raise，
    避免「验收时是 PostgreSQL、上线后悄悄变进程内存储」。
    """
    prod = production_mode()
    if requested_mode() != "postgres":
        if prod:
            raise RuntimeError(
                "生产环境禁止 MemoryStore：CQ_V2_STORE 必须为 postgres "
                f"（当前 {requested_mode()!r}）")
        return MemoryStore()
    try:
        s = PostgresStore()
        if s.ping():
            return s
        reason = "数据库不可达，已降级为进程内存储（重启后需重新播种）"
    except Exception as exc:  # noqa: BLE001
        reason = f"{type(exc).__name__}: {str(exc)[:120]}"
    if prod:
        raise RuntimeError(f"生产环境禁止降级为 MemoryStore：{reason}")
    log.warning("CQ V2 持久化不可用：%s", reason)
    mem = MemoryStore()
    mem.degraded_reason = reason
    return mem
