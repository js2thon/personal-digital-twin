"""Fact 字段契约（SIL-DC-05 / DC-08 / DC-09，P0-3）。

背景：项目此前的 `knowledge.facts` 只具备 `id / status / version` 三个公共字段，
14 项标准字段里 `tenant_id / datatype / source_id / evidence_id /
verification_status / ontology_version` 六项缺失，`source_ref`、`claim_id` 只是
**间接**替代。能力（冲突、修订、失效、双时间、来源追溯）早已达标，缺的是
**字段契约**——下游无法用统一列名做过滤、审计与多租户隔离。

本模块只做三件事，不改变任何既有业务语义：

1. **补列**：`ensure_schema()` 幂等追加契约列（启动时调用，只加列不动数据）；
2. **回填**：`backfill()` 把历史行的派生值补上，并随后收紧约束（显式调用）；
3. **自证**：`contract_status()` 逐字段给出「是否具备 / 覆盖率 / 类型 / 可空」，
   供 `GET /knowledge/contracts/facts` 直接读取，避免文档与实库再次脱节。

命名与取值口径（DC-09）：
* `status` 走四态显式枚举 ACTIVE / CONTESTED / SUPERSEDED / WITHDRAWN；
* `verification_status` 与 `status` **分离**，回答「这条事实被核验到哪一步」；
* `datatype` 由 `object_value` 的 JSON 实际类型推导，不做语义猜测以外的推断。

回填口径（重要，避免"编造历史"）：
* `tenant_id` 一律 `default`（当前部署是单租户，P1-4 接入真实租户矩阵）；
* `created_at / updated_at` 取 `recorded_at`（Transaction Time 起点，语义等价）；
* `created_by / verification_status` 由 `trust_tier` 反推（1=unknown、2=agent、3=owner），
  这是**已存在的事实**，不是新增判断；
* `ontology_version` 取当前已发布版本：本部署历史上只发布过 v1.0.0，
  回填等同事实；后续写入由 `published_version()` 逐条解析；
* `source_id / evidence_id` 只从 `claim_evidence → evidence_spans` 真实链路解析，
  解析不到就留 NULL —— 无 claim 的事实本就没有证据链，属数据缺口而非结构缺口。
"""
from __future__ import annotations

import json
import re
import threading
import time

from app.db import session as db

# ---------------------------------------------------------------- 词表（DC-09）

FACT_ACTIVE = "active"
FACT_CONTESTED = "contested"
FACT_SUPERSEDED = "superseded"
FACT_WITHDRAWN = "withdrawn"
FACT_STATUSES = (FACT_ACTIVE, FACT_CONTESTED, FACT_SUPERSEDED, FACT_WITHDRAWN)

VERIFY_UNVERIFIED = "unverified"          # 机器抽取、尚未核验
VERIFY_MACHINE_CHECKED = "machine_checked"  # 机器规则/交叉校验通过
VERIFY_HUMAN_CONFIRMED = "human_confirmed"  # 本人审核确认
VERIFY_DISPUTED = "disputed"              # 进入争议通道
VERIFICATION_STATUSES = (VERIFY_UNVERIFIED, VERIFY_MACHINE_CHECKED,
                         VERIFY_HUMAN_CONFIRMED, VERIFY_DISPUTED)

DATATYPE_TEXT = "text"
DATATYPES = ("text", "integer", "number", "boolean", "date", "datetime",
             "duration", "ref", "json")

DEFAULT_TENANT = "default"
DEFAULT_ONTOLOGY_VERSION = "v1.0.0"

# 标准 14 字段 → 实库列。`nullable` 为 True 的字段允许为空（DC-05 里非必填，
# 或本部署客观无法重建），覆盖率另行给出，不用"结构缺失"掩盖"数据缺口"。
FACT_FIELD_SPEC: tuple[dict, ...] = (
    {"field": "fact_id", "column": "id", "required": True, "nullable": False,
     "source": "存量列（命名差异）"},
    {"field": "tenant_id", "column": "tenant_id", "required": True, "nullable": False,
     "source": "P0-3 新增"},
    {"field": "subject_id", "column": "subject_id", "required": True, "nullable": True,
     "source": "存量列"},
    {"field": "predicate", "column": "predicate", "required": True, "nullable": False,
     "source": "存量列"},
    {"field": "object_value", "column": "object_value", "required": True,
     "nullable": False, "source": "存量列"},
    {"field": "datatype", "column": "datatype", "required": True, "nullable": False,
     "source": "P0-3 新增（由 object_value 推导）"},
    {"field": "valid_from", "column": "valid_from", "required": True, "nullable": True,
     "source": "存量列"},
    {"field": "valid_to", "column": "valid_to", "required": False, "nullable": True,
     "source": "存量列"},
    {"field": "recorded_at", "column": "recorded_at", "required": True,
     "nullable": True, "source": "存量列"},
    {"field": "source_id", "column": "source_id", "required": True, "nullable": True,
     "source": "P0-3 新增（claim→evidence→document 解析）"},
    {"field": "evidence_id", "column": "evidence_id", "required": True,
     "nullable": True, "source": "P0-3 新增（claim_evidence 首条证据）"},
    {"field": "confidence", "column": "confidence", "required": True, "nullable": True,
     "source": "存量列"},
    {"field": "verification_status", "column": "verification_status", "required": True,
     "nullable": False, "source": "P0-3 新增（与 status 分离）"},
    {"field": "ontology_version", "column": "ontology_version", "required": True,
     "nullable": False, "source": "P0-3 新增"},
)

# DC-08 公共字段（核心表最低要求）
PUBLIC_FIELD_SPEC: tuple[dict, ...] = (
    {"field": "id", "column": "id"},
    {"field": "tenant_id", "column": "tenant_id"},
    {"field": "created_at", "column": "created_at"},
    {"field": "updated_at", "column": "updated_at"},
    {"field": "created_by", "column": "created_by"},
    {"field": "status", "column": "status"},
    {"field": "version", "column": "version"},
)

# 双时间模型：Valid Time 与 Transaction Time 不得混用（DC-08）
BITEMPORAL_COLUMNS = ("valid_from", "valid_to", "recorded_at")

# ---------------------------------------------------------------- 纯函数


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_DURATION_RE = re.compile(
    r"^\d+(\.\d+)?\s*(s|sec|m|min|h|hr|d|day|w|wk|mo|y|yr|秒|分钟|小时|天|周|月|年)$",
    re.IGNORECASE)
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                      re.IGNORECASE)


def _unwrap(value):
    """`{"value": x}` 是项目的事实客体约定：类型判定看 x，不看待壳子。"""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def infer_datatype(object_value) -> str:
    """由客体值推导 `datatype`（DC-05）。规则固定、可单测，不做 LLM 猜测。"""
    v = _unwrap(object_value)
    if v is None:
        return "json"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, (dict, list)):
        return "json"
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return DATATYPE_TEXT
        if _UUID_RE.match(s):
            return "ref"
        if _DATE_RE.match(s):
            return "date"
        if _DATETIME_RE.match(s):
            return "datetime"
        if _DURATION_RE.match(s):
            return "duration"
        return DATATYPE_TEXT
    return "json"


def verification_for_tier(trust_tier: int | None) -> str:
    """信任级 → 核验状态。owner 审核过即人类确认，agent 抽取即机器核验。"""
    try:
        tier = int(trust_tier or 1)
    except (TypeError, ValueError):
        tier = 1
    if tier >= 3:
        return VERIFY_HUMAN_CONFIRMED
    if tier == 2:
        return VERIFY_MACHINE_CHECKED
    return VERIFY_UNVERIFIED


def created_by_for_tier(trust_tier: int | None) -> str:
    try:
        tier = int(trust_tier or 1)
    except (TypeError, ValueError):
        tier = 1
    return {3: "owner", 2: "agent"}.get(tier, "unknown")


def coverage_report(total: int, counts: dict, spec=FACT_FIELD_SPEC,
                    present: set[str] | None = None) -> list[dict]:
    """纯函数：把「非空计数」翻译成逐字段契约状态（便于脱离数据库单测）。

    `present` 是实库真实存在的列名集合，必须由 information_schema 传入：
    缺列时 counts 里没有键，不能因为「计数为 0」就误判成「列已具备」。
    """
    total = int(total or 0)
    exists = set(counts) if present is None else set(present)
    out = []
    for item in spec:
        col = item["column"]
        filled = int(counts.get(col, 0) or 0)
        ratio = round(filled / total, 4) if total else 0.0
        out.append({
            **item,
            "present": col in exists,
            "filled": filled,
            "coverage": ratio,
            "gap": bool(item.get("required", True)) and col in exists and filled < total,
        })
    return out


def compliance(report: list[dict], public_report: list[dict] | None = None) -> dict:
    required = [r for r in report if r["required"]]
    missing = [r["column"] for r in report if not r["present"]]
    gaps = [r["column"] for r in report if r["gap"]]
    public_missing = [r["column"] for r in (public_report or []) if not r["present"]]
    return {
        "fact_fields_required": len(required),
        "fact_fields_present": sum(1 for r in required if r["present"]),
        "missing_columns": missing,
        "coverage_gaps": gaps,
        "public_fields_missing": public_missing,
        "structurally_compliant": not missing and not public_missing,
        "fully_covered": not missing and not gaps,
    }

# ---------------------------------------------------------------- DDL

# 只加列，不动数据（启动时调用；便携版 PG 无 ALTER 权限时逐条容错）
_ADD_COLUMNS = (
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS datatype VARCHAR(20)",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS source_id UUID",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS evidence_id UUID",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS "
    "verification_status VARCHAR(24)",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS ontology_version VARCHAR(50)",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS created_by VARCHAR(64)",
)

# 回填：每条都带 `IS NULL` 条件，可重复执行，绝不复写既有非空值
_BACKFILL = (
    ("tenant_id",
     "UPDATE knowledge.facts SET tenant_id = :t WHERE tenant_id IS NULL",
     {"t": DEFAULT_TENANT}),
    ("datatype",
     # 与 `infer_datatype()` 同口径：先看 JSON 原生类型，字符串再按 UUID / 日期 /
     # 时间 / 时长逐级判定，最后一律 text。顺序不能反（datetime 会先命中 date 前缀）。
     "UPDATE knowledge.facts SET datatype = CASE "
     "  WHEN jsonb_typeof(object_value->'value') = 'boolean' THEN 'boolean' "
     "  WHEN jsonb_typeof(object_value->'value') = 'number' THEN CASE "
     "    WHEN (object_value->>'value') ~ '^-?[0-9]+$' THEN 'integer' "
     "    ELSE 'number' END "
     "  WHEN jsonb_typeof(object_value->'value') = 'string' THEN CASE "
     "    WHEN (object_value->>'value') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
     "[0-9a-f]{4}-[0-9a-f]{12}$' THEN 'ref' "
     "    WHEN (object_value->>'value') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:"
     "[0-9]{2}' THEN 'datetime' "
     "    WHEN (object_value->>'value') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN 'date' "
     "    WHEN (object_value->>'value') ~* '^[0-9]+(\\.[0-9]+)?\\s*(s|sec|m|min|h|hr|"
     "d|day|w|wk|mo|y|yr|秒|分钟|小时|天|周|月|年)$' THEN 'duration' "
     "    ELSE 'text' END "
     "  ELSE 'json' END "
     "WHERE datatype IS NULL", {}),
    ("verification_status",
     "UPDATE knowledge.facts SET verification_status = CASE "
     "  WHEN COALESCE(trust_tier, 1) >= 3 THEN 'human_confirmed' "
     "  WHEN COALESCE(trust_tier, 1) = 2 THEN 'machine_checked' "
     "  ELSE 'unverified' END WHERE verification_status IS NULL", {}),
    ("created_by",
     "UPDATE knowledge.facts SET created_by = CASE "
     "  WHEN COALESCE(trust_tier, 1) >= 3 THEN 'owner' "
     "  WHEN COALESCE(trust_tier, 1) = 2 THEN 'agent' "
     "  ELSE 'unknown' END WHERE created_by IS NULL", {}),
    ("created_at",
     "UPDATE knowledge.facts SET created_at = COALESCE(recorded_at, NOW()) "
     "WHERE created_at IS NULL", {}),
    ("updated_at",
     "UPDATE knowledge.facts SET updated_at = COALESCE(recorded_at, NOW()) "
     "WHERE updated_at IS NULL", {}),
    ("ontology_version",
     "UPDATE knowledge.facts SET ontology_version = :v WHERE ontology_version IS NULL",
     {"v": DEFAULT_ONTOLOGY_VERSION}),
    ("source_id",
     "UPDATE knowledge.facts f SET source_id = p.document_id "
     "FROM (SELECT DISTINCT ON (ce.claim_id) ce.claim_id, es.document_id "
     "      FROM knowledge.claim_evidence ce "
     "      JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
     "      ORDER BY ce.claim_id, ce.evidence_id) p "
     "WHERE p.claim_id = f.claim_id AND f.source_id IS NULL", {}),
    ("evidence_id",
     "UPDATE knowledge.facts f SET evidence_id = p.evidence_id FROM ("
     "  SELECT DISTINCT ON (ce.claim_id) ce.claim_id, es.id AS evidence_id "
     "  FROM knowledge.claim_evidence ce "
     "  JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
     "  ORDER BY ce.claim_id, ce.evidence_id) p "
     "WHERE p.claim_id = f.claim_id AND f.evidence_id IS NULL", {}),
)

# 收紧：默认值 + NOT NULL + 枚举 CHECK + 索引 + updated_at 触发器
_HARDEN = (
    "ALTER TABLE knowledge.facts ALTER COLUMN tenant_id SET DEFAULT 'default'",
    "ALTER TABLE knowledge.facts ALTER COLUMN tenant_id SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN datatype SET DEFAULT 'text'",
    "ALTER TABLE knowledge.facts ALTER COLUMN datatype SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN verification_status "
    "SET DEFAULT 'unverified'",
    "ALTER TABLE knowledge.facts ALTER COLUMN verification_status SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN ontology_version SET DEFAULT 'v1.0.0'",
    "ALTER TABLE knowledge.facts ALTER COLUMN ontology_version SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN created_at SET DEFAULT NOW()",
    "ALTER TABLE knowledge.facts ALTER COLUMN created_at SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN updated_at SET DEFAULT NOW()",
    "ALTER TABLE knowledge.facts ALTER COLUMN updated_at SET NOT NULL",
    "ALTER TABLE knowledge.facts ALTER COLUMN created_by SET DEFAULT 'unknown'",
    "ALTER TABLE knowledge.facts ALTER COLUMN created_by SET NOT NULL",
    # 四态显式枚举（DC-09）：与 verification_status 分离，不再用 status+contested 二义表达
    "DO $$ BEGIN ALTER TABLE knowledge.facts ADD CONSTRAINT facts_status_enum "
    "CHECK (status IN ('active','contested','superseded','withdrawn')); "
    "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "DO $$ BEGIN ALTER TABLE knowledge.facts ADD CONSTRAINT facts_verification_enum "
    "CHECK (verification_status IN ('unverified','machine_checked',"
    "'human_confirmed','disputed')); "
    "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "DO $$ BEGIN ALTER TABLE knowledge.facts ADD CONSTRAINT facts_datatype_enum "
    "CHECK (datatype IN ('text','integer','number','boolean','date','datetime',"
    "'duration','ref','json')); "
    "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    # 有效时间窗不得倒置（双时间模型的一致性底线）
    "DO $$ BEGIN ALTER TABLE knowledge.facts ADD CONSTRAINT facts_valid_window "
    "CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from); "
    "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    "CREATE INDEX IF NOT EXISTS idx_facts_tenant_status "
    "ON knowledge.facts (tenant_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_facts_source ON knowledge.facts (source_id)",
    "CREATE INDEX IF NOT EXISTS idx_facts_evidence ON knowledge.facts (evidence_id)",
    "CREATE INDEX IF NOT EXISTS idx_facts_verification "
    "ON knowledge.facts (verification_status) WHERE status = 'active'",
    "CREATE OR REPLACE FUNCTION knowledge.touch_updated_at() RETURNS trigger AS "
    "$$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END $$ LANGUAGE plpgsql",
    "DROP TRIGGER IF EXISTS trg_facts_touch_updated_at ON knowledge.facts",
    "CREATE TRIGGER trg_facts_touch_updated_at BEFORE UPDATE ON knowledge.facts "
    "FOR EACH ROW EXECUTE FUNCTION knowledge.touch_updated_at()",
)

_ensured = False
_ensure_lock = threading.Lock()

_version_cache: dict = {"at": 0.0, "value": None}
_VERSION_TTL = 60.0


def published_version() -> str:
    """当前已发布本体版本（60s 缓存）。查库失败回落默认值，不阻断写入。"""
    now = time.time()
    if _version_cache["value"] and now - _version_cache["at"] < _VERSION_TTL:
        return _version_cache["value"]
    value = DEFAULT_ONTOLOGY_VERSION
    try:
        row = db.fetch_one(
            "SELECT version FROM ontology.ontology_versions "
            "WHERE status = 'published' ORDER BY created_at DESC LIMIT 1"
        )
        if row and row.get("version"):
            value = str(row["version"])
    except Exception:  # noqa: BLE001  本体表缺失/未发布时用默认版本
        pass
    _version_cache.update({"at": now, "value": value})
    return value


def ensure_schema() -> None:
    """幂等补列（只加列、不动数据）。单条失败不阻断，服务启动可继续。"""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        for stmt in _ADD_COLUMNS:
            try:
                db.execute(stmt)
            except Exception:  # noqa: BLE001
                continue
        # Integrity objects that do not require historical backfill belong to
        # schema initialization, not the opt-in data-hardening operation.
        # Keep the temporal check and updated_at trigger present on a fresh DB.
        for stmt in (
            "DO $ BEGIN ALTER TABLE knowledge.facts ADD CONSTRAINT facts_valid_window "
            "CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_to >= valid_from); "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $",
            "CREATE OR REPLACE FUNCTION knowledge.touch_updated_at() RETURNS trigger AS "
            "$ BEGIN NEW.updated_at = NOW(); RETURN NEW; END $ LANGUAGE plpgsql",
            "DROP TRIGGER IF EXISTS trg_facts_touch_updated_at ON knowledge.facts",
            "CREATE TRIGGER trg_facts_touch_updated_at BEFORE UPDATE ON knowledge.facts "
            "FOR EACH ROW EXECUTE FUNCTION knowledge.touch_updated_at()",
        ):
            try:
                db.execute(stmt)
            except Exception:  # noqa: BLE001
                continue
        _ensured = True


def _null_counts() -> dict:
    """逐列统计待回填行数（dry-run 用，纯只读）。"""
    cols = ("tenant_id", "datatype", "verification_status", "created_by",
            "created_at", "updated_at", "ontology_version", "source_id", "evidence_id")
    exprs = ", ".join(f"count(*) FILTER (WHERE {c} IS NULL) AS {c}" for c in cols)
    row = db.fetch_one(f"SELECT {exprs} FROM knowledge.facts") or {}
    return {c: int(row.get(c) or 0) for c in cols}


def backfill(dry_run: bool = True) -> dict:
    """历史行补值 + 收紧约束。

    默认 dry_run：只返回待补行数，不写库。显式 dry_run=False 才执行，
    执行后立即跑 `_HARDEN`（NOT NULL / CHECK / 索引 / updated_at 触发器）——
    顺序不能反：先补值再加约束，否则 NOT NULL 必然失败。
    """
    ensure_schema()
    pending = _null_counts()
    total = int((db.fetch_one("SELECT count(*) AS n FROM knowledge.facts") or {}).get("n") or 0)
    if dry_run:
        return {"dry_run": True, "total_facts": total, "pending": pending,
                "would_update": sum(pending.values())}
    updated = {}
    with db.transaction():
        for col, sql, params in _BACKFILL:
            before = int((db.fetch_one(
                f"SELECT count(*) FILTER (WHERE {col} IS NULL) AS n "
                f"FROM knowledge.facts") or {}).get("n") or 0)
            if before:
                db.execute(sql, params or None)
            after = int((db.fetch_one(
                f"SELECT count(*) FILTER (WHERE {col} IS NULL) AS n "
                f"FROM knowledge.facts") or {}).get("n") or 0)
            updated[col] = before - after
    hardened, failed = [], []
    for stmt in _HARDEN:
        try:
            db.execute(stmt)
            hardened.append(stmt.split("(")[0].strip()[:80])
        except Exception as e:  # noqa: BLE001  单项失败不影响其余约束
            failed.append({"stmt": stmt.split("(")[0].strip()[:80],
                           "error": str(e)[:160]})
    return {"dry_run": False, "total_facts": total, "filled": updated,
            "hardened": len(hardened), "failed": failed}

# ---------------------------------------------------------------- 读取侧


def resolve_provenance(claim_id: str | None) -> dict:
    """claim → 首条证据 → 文档。解析不到返回空 dict（留 NULL，不编造）。"""
    if not claim_id:
        return {}
    try:
        row = db.fetch_one(
            "SELECT es.id AS evidence_id, es.document_id AS source_id "
            "FROM knowledge.claim_evidence ce "
            "JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
            "WHERE ce.claim_id = CAST(:c AS uuid) "
            "ORDER BY ce.evidence_id LIMIT 1",
            {"c": str(claim_id)},
        )
        return dict(row) if row else {}
    except Exception:  # noqa: BLE001  非法 uuid / 表缺失都不阻断写入
        return {}


def write_columns(*, object_value, trust_tier: int | None, claim_id: str | None = None,
                  source_ref: dict | None = None, tenant_id: str | None = None,
                  ontology_version: str | None = None) -> dict:
    """写入侧统一取值：所有事实写入路径都经此组装契约列。"""
    prov = resolve_provenance(claim_id)
    ref = source_ref if isinstance(source_ref, dict) else {}
    return {
        "tenant_id": tenant_id or ref.get("tenant_id") or DEFAULT_TENANT,
        "datatype": infer_datatype(object_value),
        "source_id": prov.get("source_id"),
        "evidence_id": prov.get("evidence_id"),
        "verification_status": verification_for_tier(trust_tier),
        "ontology_version": ontology_version or published_version(),
        "created_by": created_by_for_tier(trust_tier),
    }


def _columns_meta() -> dict:
    rows = db.fetch_all(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'knowledge' AND table_name = 'facts'"
    )
    return {r["column_name"]: r for r in rows}


def contract_status() -> dict:
    """逐字段契约自证：是否具备 / 类型 / 可空 / 覆盖率 + DC-08 公共字段。"""
    ensure_schema()
    meta = _columns_meta()
    spec = FACT_FIELD_SPEC + PUBLIC_FIELD_SPEC
    counted = [r["column"] for r in spec if r["column"] in meta]
    exprs = ", ".join(f"count({c}) AS {c}" for c in counted)
    total = int((db.fetch_one("SELECT count(*) AS n FROM knowledge.facts") or {}).get("n") or 0)
    counts = db.fetch_one(f"SELECT {exprs} FROM knowledge.facts") if exprs else {}
    counts = {k: int(v or 0) for k, v in (counts or {}).items()}

    report = coverage_report(total, counts, present=set(meta))
    public_report = coverage_report(total, counts, PUBLIC_FIELD_SPEC, present=set(meta))
    for r in report + public_report:
        col_meta = meta.get(r["column"])
        r["db_type"] = (col_meta or {}).get("data_type")
        r["db_nullable"] = ((col_meta or {}).get("is_nullable") == "YES"
                            if col_meta else None)

    dist = {
        "datatype": db.fetch_all(
            "SELECT datatype, count(*) AS n FROM knowledge.facts "
            "GROUP BY 1 ORDER BY 2 DESC"),
        "verification_status": db.fetch_all(
            "SELECT verification_status, count(*) AS n FROM knowledge.facts "
            "GROUP BY 1 ORDER BY 2 DESC"),
        "status": db.fetch_all(
            "SELECT status, count(*) AS n FROM knowledge.facts "
            "GROUP BY 1 ORDER BY 2 DESC"),
        "tenant_id": db.fetch_all(
            "SELECT tenant_id, count(*) AS n FROM knowledge.facts "
            "GROUP BY 1 ORDER BY 2 DESC"),
    }
    provenance = db.fetch_one(
        "SELECT count(*) FILTER (WHERE source_id IS NOT NULL) AS with_source, "
        "count(*) FILTER (WHERE evidence_id IS NOT NULL) AS with_evidence, "
        "count(*) FILTER (WHERE claim_id IS NOT NULL) AS with_claim "
        "FROM knowledge.facts") or {}
    return {
        "spec": "SIL-DC-05 / DC-08 / DC-09",
        "table": "knowledge.facts",
        "total_facts": total,
        "fields": report,
        "public_fields": public_report,
        "compliance": compliance(report, public_report),
        "distribution": dist,
        "provenance_coverage": {k: int(v or 0) for k, v in provenance.items()},
        "enums": {
            "status": list(FACT_STATUSES),
            "verification_status": list(VERIFICATION_STATUSES),
            "datatype": list(DATATYPES),
        },
        "bitemporal": {
            "columns": list(BITEMPORAL_COLUMNS),
            "valid_window_violations": int((db.fetch_one(
                "SELECT count(*) AS n FROM knowledge.facts "
                "WHERE valid_from IS NOT NULL AND valid_to IS NOT NULL "
                "  AND valid_to < valid_from") or {}).get("n") or 0),
            "updated_at_trigger": bool(db.fetch_one(
                "SELECT 1 AS ok FROM pg_trigger WHERE tgrelid = 'knowledge.facts'::regclass "
                "AND tgname = 'trg_facts_touch_updated_at'")),
        },
        "notes": [
            "source_id / evidence_id 仅从 claim_evidence→evidence_spans 真实链路解析，"
            "无 claim 的事实留 NULL（数据缺口，非结构缺口）",
            "ontology_version 回填取当前已发布版本；本部署历史仅发布过 v1.0.0",
            "tenant_id 当前恒为 default，真实租户矩阵与 ABAC 逐点过滤属 P1-4",
        ],
    }


def contract_row(*, fact_id: str) -> dict | None:
    """单条事实的契约视图（含 source/evidence 解析结果）。"""
    return db.fetch_one(
        "SELECT id, tenant_id, subject_id, predicate, object_value, datatype, "
        "       valid_from, valid_to, recorded_at, source_id, evidence_id, "
        "       confidence, verification_status, ontology_version, "
        "       created_at, updated_at, created_by, status, version "
        "FROM knowledge.facts WHERE id = CAST(:id AS uuid)",
        {"id": str(fact_id)},
    )
