"""本体可信度层：双时间线事实、信任护栏、争议裁决与实体画像。

设计借鉴 GBrain v0.42 Life Chronicle 的三条地基，只取与本地数据模型匹配的部分：

1. 双时间线（bi-temporal）
   - `valid_from` / `valid_to` 记录「这个值在什么时间区间内为真」（有效时间）；
   - `recorded_at` 记录「我们是什么时候知道的」（记录时间）。
   两者不混用，`asof` 查询走有效时间，历史回填不会被误认为新观测。

2. 信任分级（trust tier）
   - owner=3：本人在控制台审核确认；agent=2：蒸馏/LLM 抽取；unknown=1。
   - 低信任写入**永不覆盖**高信任当前值，而是落为 `contested` 交 owner 裁决。
   这堵住了「模型推断悄悄改写你确认过的结论」这条风险路径。

3. 回溯观察 ≠ 佐证（backdated stint）
   - 若新观测的 `valid_from` 早于当前值的生效时间，它描述的是更早的一段历史，
     必须作为独立 stint 保留，而不是把当前值作废——否则 asof 时间旅行会错。

状态词表：active（当前值/有效历史段）| superseded（被取代）| contested（有争议待裁决）| withdrawn（裁决驳回/撤回）。
`superseded_by` 指向取代它的那条事实，形成可追溯的版本链。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from uuid import UUID

from app import policy as policy_mod
from app.db import session as db
from app.modules.knowledge import contract as fact_contract

FACT_ACTIVE = "active"
FACT_SUPERSEDED = "superseded"
FACT_CONTESTED = "contested"
FACT_WITHDRAWN = "withdrawn"

TRUST_OWNER = 3
TRUST_AGENT = 2
TRUST_UNKNOWN = 1
TRUST_LABEL = {TRUST_OWNER: "owner", TRUST_AGENT: "agent", TRUST_UNKNOWN: "unknown"}
TIER_TO_TRUST = {"owner": TRUST_OWNER, "agent": TRUST_AGENT, "unknown": TRUST_UNKNOWN}

# 维度基数：单值维度同一时间只能有一个值为真（换言之「新值取代旧值」是正确语义）；
# 多值维度（观点、参与、提出、负责…）不同客体是**并列断言**，不存在取代关系。
# 默认多值：误判成多值只会多出可见的重复，误判成单值会静默丢知识——保守优先。
SINGLE_VALUED = {
    "belongs_to_department", "department", "works_at", "employed_by", "reports_to",
    "has_role", "job_title", "title", "position", "role", "level", "grade",
    "status", "current_status", "located_in", "location", "manager",
    "account_manager", "owner_of", "primary_contact", "owns",
}


def cardinality_of(predicate: str) -> str:
    return "single" if (predicate or "").strip().lower() in SINGLE_VALUED else "multi"

# facts 可信度相关列（本地老库升级：CREATE TABLE IF NOT EXISTS 不会补列）
_MIGRATIONS = [
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS confidence NUMERIC(4,3)",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS source_ref JSONB DEFAULT '{}'::jsonb",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS trust_tier SMALLINT DEFAULT 1",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS visibility VARCHAR(20) DEFAULT 'owner'",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS superseded_by UUID",
    "ALTER TABLE knowledge.facts ADD COLUMN IF NOT EXISTS contested BOOLEAN DEFAULT FALSE",
    "CREATE INDEX IF NOT EXISTS idx_facts_current ON knowledge.facts "
    "(subject_id, predicate, status)",
    "CREATE INDEX IF NOT EXISTS idx_facts_contested ON knowledge.facts (contested) "
    "WHERE contested IS TRUE",
]

_ensured = False
_ensure_lock = threading.Lock()

_COLS = ("id, claim_id, subject_id, predicate, object_value, valid_from, valid_to, "
         "recorded_at, status, version, confidence, trust_tier, visibility, "
         "superseded_by, contested")


def ensure_schema() -> None:
    """幂等补齐可信度列；单条失败不阻断（服务启动时数据库可能还没就绪）。"""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        for stmt in _MIGRATIONS:
            try:
                db.execute(stmt)
            except Exception:  # noqa: BLE001
                continue
        # SIL-DC-05 / DC-08：事实字段契约列（只加列，不动数据）
        try:
            fact_contract.ensure_schema()
        except Exception:  # noqa: BLE001
            pass
        _ensured = True


# ---------------------------------------------------------------- 信任分级


def trust_tier_of(writer: str | None) -> int:
    """写入者 → 信任级。默认按最低信任处理（fail-closed）。"""
    w = (writer or "").strip().lower()
    if w in ("owner", "self", "human", "user", "reviewer"):
        return TRUST_OWNER
    if w.startswith("agent") or w in ("llm", "distiller", "extractor", "system", "tool"):
        return TRUST_AGENT
    return TRUST_UNKNOWN


def trust_label(tier: int | None) -> str:
    return TRUST_LABEL.get(int(tier or TRUST_UNKNOWN), "unknown")


# ---------------------------------------------------------------- 纯决策逻辑


_MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


def _as_dt(v) -> datetime | None:
    """统一解析成**带时区**的 datetime。

    库里 valid_from/valid_to 是 timestamptz，读回来带偏移；而 asof 参数可能是不带
    时区的 "2000-01-01T00:00:00"。两者直接比较会抛
    TypeError: can't compare offset-naive and offset-aware datetimes，
    所以这里把 naive 一律按服务器本地时区补齐（与原写入语义一致）。
    """
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.astimezone()
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _is_backdated(current: dict, incoming: dict) -> bool:
    """新观测的生效时间早于当前值 → 这是一段更早的历史，不是对当前值的修改。"""
    new_from = _as_dt(incoming.get("valid_from"))
    cur_from = _as_dt(current.get("valid_from"))
    return bool(new_from and cur_from and new_from < cur_from)


def decide_write(current: dict | None, incoming: dict) -> dict:
    """决定一次事实写入该如何落地（纯函数，便于单测）。

    current: 当前有效值 {same_value, trust_tier, valid_from} 或 None
    incoming: {trust_tier, valid_from, object_value}
    返回 action / status / close_current / reason：
      inserted        无历史值，直接成为当前值
      duplicate       与当前值相同且信任级不更高，幂等跳过
      reinforced      与当前值相同但信任级更高，提升当前值可信度
      backdated_stint 回溯观察，作为更早的历史段独立保留
      superseded      信任级不低于当前值，取代为新的当前值
      contested       信任级低于当前值，不覆盖，标记争议待裁决
    """
    new_tier = int(incoming.get("trust_tier") or TRUST_UNKNOWN)
    if not current:
        return {"action": "inserted", "status": FACT_ACTIVE, "close_current": False,
                "reason": "无历史值，直接作为当前有效值"}

    cur_tier = int(current.get("trust_tier") or TRUST_UNKNOWN)
    if current.get("same_value"):
        if new_tier > cur_tier:
            return {"action": "reinforced", "status": FACT_ACTIVE,
                    "close_current": False, "upgrade_trust": True,
                    "reason": "与当前值一致且信任级更高，提升当前值可信度"}
        return {"action": "duplicate", "status": FACT_ACTIVE, "close_current": False,
                "reason": "与当前值一致，幂等跳过"}

    if new_tier >= cur_tier:
        if _is_backdated(current, incoming):
            return {"action": "backdated_stint", "status": FACT_ACTIVE,
                    "close_current": False, "close_before": True,
                    "reason": "回溯观察：早于当前值生效时间，作为更早历史段保留"}
        return {"action": "superseded", "status": FACT_ACTIVE, "close_current": True,
                "reason": "信任级不低于当前值，取代为新的当前值"}

    return {"action": "contested", "status": FACT_CONTESTED, "close_current": False,
            "reason": "信任级低于当前值，不覆盖；标记争议待 owner 裁决"}


# ---------------------------------------------------------------- 写入


def _as_json_text(value) -> str:
    """任意值 → jsonb 文本。字符串先尝试按 JSON 解析，失败则包成 {"text": ...}。"""
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except (ValueError, TypeError):
            return json.dumps({"text": value}, ensure_ascii=False)
    return json.dumps(value if value is not None else {}, ensure_ascii=False,
                      default=str)


def _current_stint(subject_id: str, predicate: str, obj_json: str) -> dict | None:
    """当前有效值：status=active 且未结束（valid_to 为空），取生效时间最新的一条。"""
    return db.fetch_one(
        "SELECT id, valid_from, valid_to, confidence, trust_tier, version, "
        "       (object_value = CAST(:ov AS jsonb)) AS same_value "
        "FROM knowledge.facts "
        "WHERE subject_id = CAST(:sid AS uuid) AND predicate = :p "
        "  AND status = 'active' AND valid_to IS NULL "
        "ORDER BY COALESCE(valid_from, recorded_at) DESC NULLS LAST LIMIT 1",
        {"sid": subject_id, "p": predicate, "ov": obj_json},
    )


def _next_version(subject_id: str | None, predicate: str) -> int:
    row = db.fetch_one(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM knowledge.facts "
        "WHERE predicate = :p AND subject_id IS NOT DISTINCT FROM CAST(:sid AS uuid)",
        {"p": predicate, "sid": subject_id},
    )
    return int((row or {"v": 1})["v"])


def _insert_fact(*, subject_id, predicate, obj_json, claim_id, valid_from=None,
                 valid_to=None, status=FACT_ACTIVE, confidence=None,
                 trust_tier=TRUST_UNKNOWN, visibility="owner", source_ref=None,
                 contested=False, tenant_id: str | None = None) -> tuple[str, int]:
    version = _next_version(subject_id, predicate)
    try:
        parsed = json.loads(obj_json)
    except (TypeError, ValueError):
        parsed = {"text": obj_json}
    # SIL-DC-05：契约列（tenant_id / datatype / source_id / evidence_id /
    # verification_status / ontology_version / created_by）统一在此组装，
    # 不散落到各写入路径；解析不到的证据链路留 NULL，不编造。
    contract_cols = fact_contract.write_columns(
        object_value=parsed, trust_tier=trust_tier, claim_id=claim_id,
        source_ref=source_ref, tenant_id=tenant_id,
    )
    fid = db.insert_returning_id(
        "knowledge.facts",
        {
            "claim_id": claim_id,
            "subject_id": subject_id,
            "predicate": predicate,
            "object_value": obj_json,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "status": status,
            "version": version,
            "confidence": confidence,
            "trust_tier": int(trust_tier),
            "visibility": visibility or "owner",
            "source_ref": json.dumps(source_ref or {}, ensure_ascii=False, default=str),
            "contested": bool(contested),
            **contract_cols,
        },
    )
    return str(fid), version


def _close(current_id: str, *, by_fact_id: str | None, end_at) -> None:
    db.execute(
        "UPDATE knowledge.facts SET status = 'superseded', "
        "  valid_to = COALESCE(CAST(:vf AS timestamptz), NOW()), "
        "  superseded_by = CAST(:by AS uuid) "
        "WHERE id = CAST(:id AS uuid) AND status = 'active'",
        {"id": current_id, "by": by_fact_id, "vf": end_at},
    )


def write_fact(*, predicate: str, object_value, subject_id: str | None = None,
               claim_id: str | None = None, valid_from=None, confidence=None,
               writer: str = "agent", source_ref: dict | None = None,
               visibility: str = "owner", tenant_id: str | None = None) -> dict:
    """带信任护栏 + SHACL 校验的事实写入。所有事实写入路径都应走这里。

    返回值 action 见 decide_write；额外带 fact_id / version / closed_fact_id，
    以及 `shacl` 校验结论（warn 模式下可能含违规但不阻断，如实返回）。
    tenant_id 缺省回落 source_ref.tenant_id → 'default'（P1-4 写入打标）。
    """
    verdict = _shacl_gate(predicate=predicate, object_value=object_value,
                          subject_id=subject_id, confidence=confidence,
                          writer=writer, valid_from=valid_from,
                          visibility=visibility)
    out = _write_fact_impl(
        predicate=predicate, object_value=object_value, subject_id=subject_id,
        claim_id=claim_id, valid_from=valid_from, confidence=confidence,
        writer=writer, source_ref=source_ref, visibility=visibility,
        tenant_id=tenant_id)
    if verdict is not None:
        out["shacl"] = verdict
    return out


_TYPE_CACHE: dict[str, tuple[str | None, float]] = {}


def subject_type_of(subject_id: str | None) -> str | None:
    """实体类型（R2 关系端点判定用）。带 300s 缓存，避免每次写入多查一次库。"""
    if not subject_id:
        return None
    import time

    hit = _TYPE_CACHE.get(str(subject_id))
    if hit and time.monotonic() - hit[1] < 300:
        return hit[0]
    etype = None
    try:
        row = db.fetch_one(
            "SELECT entity_type FROM ontology.entities WHERE id = CAST(:i AS uuid)",
            {"i": str(subject_id)})
        etype = (row or {}).get("entity_type")
    except Exception:  # noqa: BLE001  实体表不可用就不做端点判定
        etype = None
    if len(_TYPE_CACHE) > 2048:
        _TYPE_CACHE.clear()
    _TYPE_CACHE[str(subject_id)] = (etype, time.monotonic())
    return etype


def _shacl_gate(*, predicate, object_value, subject_id, confidence, writer,
                valid_from, visibility) -> dict | None:
    """写入前的 Shape 校验；strict 模式拦截并抛 ShaclViolation。"""
    if not predicate:
        return None
    from app.modules.ontology import shacl

    if shacl.mode() == "off":
        return None
    payload = {"predicate": predicate, "object_value": object_value,
               "confidence": confidence, "trust_tier": trust_tier_of(writer),
               "status": FACT_ACTIVE, "visibility": visibility or "owner",
               "valid_from": valid_from}
    verdict = shacl.check_write(payload, subject_type=subject_type_of(subject_id))
    if verdict.get("blocked"):
        raise shacl.ShaclViolation(verdict)
    return verdict


def _write_fact_impl(*, predicate: str, object_value, subject_id: str | None = None,
                     claim_id: str | None = None, valid_from=None, confidence=None,
                     writer: str = "agent", source_ref: dict | None = None,
                     visibility: str = "owner",
                     tenant_id: str | None = None) -> dict:
    """实际写入实现；SHACL 闸门在 `write_fact` 包装层，保证所有路径都过一遍。"""
    ensure_schema()
    tier = trust_tier_of(writer)
    obj_json = _as_json_text(object_value)
    predicate = (predicate or "").strip()
    if not predicate:
        return {"action": "skipped", "reason": "predicate 不能为空"}

    with db.transaction():
        # 无主体事实同样受信任护栏保护（与 _DECISION_CASES 承诺一致）：
        # 同值 → 幂等/提升信任级，绝不作废既有行；
        # 异值 → 信任级不低于现有活跃值时并列新增，低于时落 contested 等 owner 裁决。
        # 旧规则「同客体即作废」不看信任级，会让 unknown 级观察静默顶掉 owner 确认的事实。
        if not subject_id:
            same = db.fetch_one(
                "SELECT id, version, trust_tier FROM knowledge.facts "
                "WHERE predicate = :p AND status = 'active' AND subject_id IS NULL "
                "  AND object_value = CAST(:ov AS jsonb) LIMIT 1",
                {"p": predicate, "ov": obj_json},
            )
            if same:
                if tier > int(same.get("trust_tier") or TRUST_UNKNOWN):
                    db.execute(
                        "UPDATE knowledge.facts SET trust_tier = :t, "
                        "  confidence = COALESCE(CAST(:c AS numeric), confidence), "
                        "  verification_status = CASE WHEN :t >= 3 THEN 'human_confirmed' "
                        "    WHEN :t = 2 THEN 'machine_checked' ELSE 'unverified' END "
                        "WHERE id = CAST(:id AS uuid)",
                        {"t": tier, "c": confidence, "id": str(same["id"])},
                    )
                    return {"action": "reinforced", "fact_id": str(same["id"]),
                            "version": same.get("version"), "trust_tier": tier,
                            "status": FACT_ACTIVE,
                            "reason": "无主体同值且信任级更高，提升可信度"}
                return {"action": "duplicate", "fact_id": str(same["id"]),
                        "version": same.get("version"), "trust_tier": tier,
                        "status": FACT_ACTIVE, "reason": "无主体同值已存在，幂等跳过"}
            top = db.fetch_one(
                "SELECT MAX(trust_tier) AS t FROM knowledge.facts "
                "WHERE predicate = :p AND status = 'active' AND subject_id IS NULL",
                {"p": predicate},
            )
            max_tier = int((top or {}).get("t") or 0)
            contested = tier < max_tier
            fid, version = _insert_fact(
                subject_id=None, predicate=predicate, obj_json=obj_json,
                claim_id=claim_id, valid_from=valid_from, confidence=confidence,
                trust_tier=tier, visibility=visibility, source_ref=source_ref,
                status=FACT_CONTESTED if contested else FACT_ACTIVE,
                contested=contested, tenant_id=tenant_id,
            )
            if contested:
                return {"action": "contested", "fact_id": fid, "version": version,
                        "trust_tier": tier, "status": FACT_CONTESTED,
                        "reason": "无主体异值且信任级低于现有活跃值，挂起等 owner 裁决"}
            return {"action": "inserted", "fact_id": fid, "version": version,
                    "trust_tier": tier, "status": FACT_ACTIVE,
                    "reason": "无主体异值，作为并列断言新增"}

        # 多值维度：不同客体是并列断言（观点、参与、提出、负责…），不存在「谁取代谁」。
        # 只做「同值幂等 + 同值提升信任级」，绝不关闭其它活跃值。
        if cardinality_of(predicate) == "multi":
            dup = db.fetch_one(
                "SELECT id, version, trust_tier FROM knowledge.facts "
                "WHERE subject_id = CAST(:sid AS uuid) AND predicate = :p "
                "  AND status = 'active' AND object_value = CAST(:ov AS jsonb) LIMIT 1",
                {"sid": subject_id, "p": predicate, "ov": obj_json},
            )
            if dup:
                if tier > int(dup.get("trust_tier") or TRUST_UNKNOWN):
                    db.execute(
                        "UPDATE knowledge.facts SET trust_tier = :t, "
                        "  confidence = COALESCE(CAST(:c AS numeric), confidence), "
                        "  verification_status = CASE WHEN :t >= 3 THEN 'human_confirmed' "
                        "    WHEN :t = 2 THEN 'machine_checked' ELSE 'unverified' END "
                        "WHERE id = CAST(:id AS uuid)",
                        {"t": tier, "c": confidence, "id": str(dup["id"])},
                    )
                    return {"action": "reinforced", "fact_id": str(dup["id"]),
                            "version": dup.get("version"), "trust_tier": tier,
                            "status": FACT_ACTIVE,
                            "reason": "多值维度：同值且信任级更高，提升可信度"}
                return {"action": "duplicate", "fact_id": str(dup["id"]),
                        "version": dup.get("version"), "trust_tier": tier,
                        "status": FACT_ACTIVE, "reason": "多值维度：同值已存在，幂等跳过"}
            fid, version = _insert_fact(
                subject_id=subject_id, predicate=predicate, obj_json=obj_json,
                claim_id=claim_id, valid_from=valid_from, confidence=confidence,
                trust_tier=tier, visibility=visibility, source_ref=source_ref,
                tenant_id=tenant_id,
            )
            return {"action": "inserted", "fact_id": fid, "version": version,
                    "trust_tier": tier, "status": FACT_ACTIVE,
                    "reason": "多值维度：作为并列断言新增，不取代其它值"}

        cur = _current_stint(subject_id, predicate, obj_json)
        decision = decide_write(
            cur,
            {"trust_tier": tier, "valid_from": valid_from, "object_value": obj_json},
        )
        action = decision["action"]

        if action == "duplicate":
            return {"action": "duplicate", "fact_id": str(cur["id"]),
                    "version": cur.get("version"), "trust_tier": tier,
                    "status": FACT_ACTIVE, "reason": decision["reason"]}

        if action == "reinforced":
            db.execute(
                "UPDATE knowledge.facts SET trust_tier = :t, "
                "  confidence = COALESCE(CAST(:c AS numeric), confidence), "
                "  verification_status = CASE WHEN :t >= 3 THEN 'human_confirmed' "
                "    WHEN :t = 2 THEN 'machine_checked' ELSE 'unverified' END "
                "WHERE id = CAST(:id AS uuid)",
                {"t": tier, "c": confidence, "id": str(cur["id"])},
            )
            return {"action": "reinforced", "fact_id": str(cur["id"]),
                    "version": cur.get("version"), "trust_tier": tier,
                    "status": FACT_ACTIVE, "reason": decision["reason"]}

        # 回溯观察：新段结束于「它之后最早一段的生效时间」，避免时间窗重叠，
        # 当前值保持不变（这正是 asof 时间旅行能算对的前提）。
        valid_to = None
        if decision.get("close_before"):
            valid_to = cur.get("valid_from")
            if valid_from:
                nxt = db.fetch_one(
                    "SELECT MIN(valid_from) AS nxt FROM knowledge.facts "
                    "WHERE subject_id = CAST(:sid AS uuid) AND predicate = :p "
                    "  AND status IN ('active', 'superseded') "
                    "  AND valid_from > CAST(:vf AS timestamptz)",
                    {"sid": subject_id, "p": predicate, "vf": valid_from},
                )
                if nxt and nxt.get("nxt"):
                    valid_to = nxt["nxt"]
        fid, version = _insert_fact(
            subject_id=subject_id, predicate=predicate, obj_json=obj_json,
            claim_id=claim_id, valid_from=valid_from, valid_to=valid_to,
            status=decision["status"], confidence=confidence, trust_tier=tier,
            visibility=visibility, source_ref=source_ref, tenant_id=tenant_id,
            contested=(decision["status"] == FACT_CONTESTED),
        )
        closed = None
        if decision["close_current"]:
            _close(str(cur["id"]), by_fact_id=fid, end_at=valid_from)
            closed = str(cur["id"])
        return {"action": action, "fact_id": fid, "version": version,
                "trust_tier": tier, "status": decision["status"],
                "closed_fact_id": closed, "reason": decision["reason"]}


# ---------------------------------------------------------------- 争议裁决


def list_conflicts(limit: int = 50) -> list[dict]:
    """有争议的事实：同主体同维度存在两个「都自称当前」的值，来源/信任级不同。"""
    ensure_schema()
    rows = db.fetch_all(
        "SELECT f.id, f.subject_id, e.canonical_name AS subject_name, f.predicate, "
        "       f.object_value, f.valid_from, f.recorded_at, f.confidence, "
        "       f.trust_tier, f.claim_id, "
        "       cur.id AS current_fact_id, cur.object_value AS current_value, "
        "       cur.confidence AS current_confidence, cur.trust_tier AS current_trust_tier, "
        "       cur.valid_from AS current_valid_from "
        "FROM knowledge.facts f "
        "LEFT JOIN ontology.entities e ON e.id = f.subject_id "
        "LEFT JOIN LATERAL ("
        "  SELECT c.id, c.object_value, c.confidence, c.trust_tier, c.valid_from "
        "  FROM knowledge.facts c "
        "  WHERE c.status = 'active' AND c.valid_to IS NULL "
        "    AND c.predicate = f.predicate "
        "    AND c.subject_id IS NOT DISTINCT FROM f.subject_id "
        "  ORDER BY COALESCE(c.valid_from, c.recorded_at) DESC NULLS LAST LIMIT 1"
        ") cur ON TRUE "
        "WHERE f.status = 'contested' "
        "ORDER BY f.recorded_at DESC NULLS LAST LIMIT :l",
        {"l": max(1, min(int(limit or 50), 200))},
    )
    for r in rows:
        r["proposed_by"] = trust_label(r.get("trust_tier"))
        r["current_by"] = trust_label(r.get("current_trust_tier"))
    return rows


def resolve_conflict(fact_id: str, action: str, actor: str = "owner") -> dict:
    """裁决争议事实：accept=采纳为当前值（取代旧值）；reject=驳回（withdrawn）。"""
    ensure_schema()
    if action not in ("accept", "reject"):
        raise ValueError("action must be accept or reject")
    fact = db.fetch_one(
        f"SELECT {_COLS} FROM knowledge.facts WHERE id = CAST(:id AS uuid)", {"id": fact_id}
    )
    if not fact:
        raise ValueError("fact not found")
    if fact.get("status") != FACT_CONTESTED:
        raise ValueError(f"fact status is {fact.get('status')}, not contested")

    with db.transaction():
        if action == "reject":
            db.execute(
                "UPDATE knowledge.facts SET status = 'withdrawn', contested = FALSE, "
                "  valid_to = COALESCE(valid_to, NOW()), "
                "  source_ref = COALESCE(source_ref, '{}'::jsonb) "
                "    || CAST(:note AS jsonb) "
                "WHERE id = CAST(:id AS uuid)",
                {"id": fact_id,
                 "note": json.dumps({"resolved_by": actor, "resolution": "rejected"},
                                    ensure_ascii=False, default=str)},
            )
            return {"status": "withdrawn", "fact_id": fact_id}

        cur = _current_stint(str(fact["subject_id"]), fact["predicate"],
                             _as_json_text(fact["object_value"]))
        db.execute(
            "UPDATE knowledge.facts SET status = 'active', contested = FALSE, "
            "  valid_to = NULL, "
            "  source_ref = COALESCE(source_ref, '{}'::jsonb) "
            "    || CAST(:note AS jsonb) "
            "WHERE id = CAST(:id AS uuid)",
            {"id": fact_id,
             "note": json.dumps({"resolved_by": actor, "resolution": "accepted"},
                                ensure_ascii=False, default=str)},
        )
        closed = None
        if cur and str(cur["id"]) != fact_id:
            fv = fact.get("valid_from")
            cur_from = cur.get("valid_from")
            if fv and cur_from and fv < cur_from:
                # 回溯型争议：采纳值生效更早。不能把当前值关到过去时点（会制造
                # asof 空洞区间）；改为当前值结束于采纳段生效时间，采纳段作为
                # 更早历史段保留（valid_to = 当前值生效时间），与 write_fact
                # 的 backdated_stint 语义对齐。
                db.execute(
                    "UPDATE knowledge.facts SET valid_to = CAST(:vt AS timestamptz) "
                    "WHERE id = CAST(:id AS uuid)",
                    {"id": fact_id, "vt": cur_from},
                )
            else:
                _close(str(cur["id"]), by_fact_id=fact_id, end_at=fv)
                closed = str(cur["id"])
        return {"status": "active", "fact_id": fact_id, "closed_fact_id": closed}


# ---------------------------------------------------------------- 撤销发布（软删除）
#
# 「已发布事实能否删除」评估结论（docs/fact-deletion-assessment.md）：物理删得掉，
# 但决策证据 reference_id、审计快照、superseded_by 都是无 FK 的软引用，删行会让它们
# 永远悬空且不可复现。这里改为 withdrawn 软删：用户可见效果与删除一致，追溯链不断。


def _withdrawal_note(actor: str, reason: str) -> dict:
    """撤销留痕（纯函数）：谁、何时、为什么，写进 facts.source_ref。"""
    return {
        "withdrawn_by": (actor or "unknown").strip()[:80],
        "withdrawal": "unpublished",
        "reason": (reason or "").strip()[:300] or "owner unpublished from console",
        "at": datetime.now().astimezone().isoformat(),
    }


def withdraw_fact(fact_id: str, actor: str = "owner", reason: str = "") -> dict:
    """撤销发布一条 active 事实：status → withdrawn，**行永不删除**。

    相对物理删除的收益：
    - 决策证据 reference_id / 审计快照 / superseded_by 仍可按 id 解析；
    - list_facts 与事实召回只查 active，撤销后立刻从列表与检索中消失；
    - lineage.claims_to_facts 用 EXISTS 不限 status，链路校验不会变红；
    - 版本号不会被复用（MAX(version)+1 仍能看到这一行），时间线无空洞；
    - 行保留后同一 Claim 若被重新审核，会写入新的版本段，语义清晰。

    状态变更、source_ref 留痕与审计日志在同一事务内完成，任一失败整体回滚。
    """
    ensure_schema()
    try:
        UUID(str(fact_id))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid fact id") from None

    note = _withdrawal_note(actor, reason)
    with db.transaction():
        row = db.fetch_one(
            "SELECT id, claim_id, predicate, subject_id, version, status "
            "FROM knowledge.facts WHERE id = CAST(:id AS uuid) FOR UPDATE",
            {"id": fact_id},
        )
        if not row:
            raise ValueError("fact not found")
        if row["status"] != FACT_ACTIVE:
            raise ValueError(
                f"fact status is {row['status']}, only active facts can be withdrawn")

        updated = db.fetch_one(
            "WITH u AS ("
            " UPDATE knowledge.facts SET status = 'withdrawn', contested = FALSE, "
            "   valid_to = COALESCE(valid_to, NOW()), "
            "   source_ref = COALESCE(source_ref, '{}'::jsonb) || CAST(:note AS jsonb) "
            " WHERE id = CAST(:id AS uuid) AND status = 'active' RETURNING 1"
            ") SELECT count(*) AS n FROM u",
            {"id": fact_id, "note": json.dumps(note, ensure_ascii=False, default=str)},
        )["n"]
        if not updated:      # FOR UPDATE 下理论上不会发生，保留并发兜底
            raise ValueError("fact was withdrawn concurrently, please retry")

        from app.modules.audit import service as audit

        audit.log_access(
            note["withdrawn_by"], "knowledge.fact.withdraw", "knowledge.fact", fact_id,
            {"claim_id": str(row["claim_id"]) if row["claim_id"] else None,
             "predicate": row["predicate"],
             "subject_id": str(row["subject_id"]) if row["subject_id"] else None,
             "version": row["version"], "reason": note["reason"]},
        )

    # R2：事实撤销后，图上的投影边必须一起停用，否则「已撤销的知识还能连出线」。
    # 放在事务外：边是派生数据，删边失败不该回滚「撤销」这个业务动作；
    # 真失败了还有 /ontology/sync 兜底对齐。
    edge_sync: dict = {}
    try:
        from app.modules.ontology import sync as graph_sync
        edge_sync = graph_sync.remove_fact_edge(fact_id, "fact_withdrawn")
    except Exception as e:                      # noqa: BLE001
        edge_sync = {"removed": 0, "error": str(e)}

    return {
        "fact_id": fact_id,
        "status": FACT_WITHDRAWN,
        "previous_status": FACT_ACTIVE,
        "claim_id": str(row["claim_id"]) if row["claim_id"] else None,
        "version": row["version"],
        "withdrawn_by": note["withdrawn_by"],
        "reason": note["reason"],
        "withdrawn_at": note["at"],
        "edges_synced": edge_sync,
        "message": "已撤销发布：事实从列表与检索中移除，记录与证据链保留",
    }


BATCH_WITHDRAW_MAX = 2000

# 连点 / 脚本循环的刹车：60 秒窗口内累计撤销超过这个数，必须显式确认「我就是要撤这么多」。
# 起因是 2026-11-16 两次事故：一次「全选=全部」撤了 653 条，一次 15 分钟内 93 次
# 批量调用撤了 782 条（每次 8~12 条，看起来像人在逐页点，实际是循环）。单次批量
# 大小限制挡不住这种「小批量高频」形态，只能在时间窗口上做累计闸门。
BATCH_WITHDRAW_BURST = 200
BATCH_WITHDRAW_BURST_WINDOW = 60


def recent_withdrawn_count(seconds: int = BATCH_WITHDRAW_BURST_WINDOW) -> int:
    """窗口内已被撤销的事实条数（撤销即写 valid_to，因此用它做时间窗口）。"""
    row = db.fetch_one(
        "SELECT count(*) AS n FROM knowledge.facts WHERE status = 'withdrawn' "
        "AND valid_to IS NOT NULL "
        "AND valid_to > NOW() - make_interval(secs => CAST(:s AS int))",
        {"s": int(seconds)},
    )
    return int((row or {}).get("n") or 0)


def burst_blocked(recent: int, n: int, confirm_scope: bool,
                  limit: int = BATCH_WITHDRAW_BURST) -> bool:
    """累计闸门判定（纯函数）：窗口内已撤 + 本批 会超过上限、且未显式确认 → 拦下。"""
    return (not confirm_scope) and (int(recent) + int(n) > int(limit))


def batch_withdraw_facts(fact_ids: list[str], actor: str = "owner",
                         reason: str = "", dry_run: bool = False,
                         confirm_scope: bool = False,
                         client: dict | None = None) -> dict:
    """一次事务批量撤销发布：active → withdrawn，**行永不删除**。

    与单条 withdraw_fact 保持同一套语义（source_ref 留痕、审计日志、图投影边停用），
    但整批共用**一个事务** + 批量 SQL + 一次边清理，避免 N 条事实 = N 个事务 /
    N 次往返。非 active / 不存在的 id 只计入 skipped，不拖累其余行；
    dry_run=True 仅做预检，不写库。

    另有累计闸门：60 秒内累计撤销超过 BATCH_WITHDRAW_BURST 条时，
    除非 confirm_scope=True，否则整批拒绝（400），把「连点 / 脚本循环」拦在写入之前。
    """
    ensure_schema()
    if not isinstance(fact_ids, list) or not fact_ids:
        raise ValueError("fact_ids must be a non-empty list")
    # 去重且保序；任一非法 id 直接拒绝整批，不静默吞掉用户的输入
    seen: set[str] = set()
    ids: list[str] = []
    for raw in fact_ids:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            UUID(s)
        except (ValueError, TypeError, AttributeError):
            raise ValueError(f"invalid fact id: {s[:40]}") from None
        if s not in seen:
            seen.add(s)
            ids.append(s)
    if not ids:
        raise ValueError("fact_ids must be a non-empty list")
    if len(ids) > BATCH_WITHDRAW_MAX:
        raise ValueError(f"batch size {len(ids)} exceeds limit {BATCH_WITHDRAW_MAX}")

    rows = db.fetch_all(
        "SELECT id::text AS id, claim_id, predicate, subject_id, version, status "
        "FROM knowledge.facts WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": ids},
    )
    by_id = {r["id"]: r for r in rows}
    active, skipped = [], []
    for fid in ids:
        row = by_id.get(fid)
        if not row:
            skipped.append({"fact_id": fid, "reason": "fact not found"})
        elif row["status"] != FACT_ACTIVE:
            skipped.append({"fact_id": fid, "reason": f"fact status is {row['status']}"})
        else:
            active.append(row)

    result: dict = {
        "requested": len(ids),
        "withdrawn": 0,
        "candidates": len(active),
        "skipped": skipped,
        "dry_run": bool(dry_run),
        "message": "",
    }
    if dry_run:
        result["message"] = (f"预检完成：可撤销 {len(active)} 条，"
                             f"跳过 {len(skipped)} 条（未写库）")
        return result
    if not active:
        result["message"] = f"没有可撤销的事实，跳过 {len(skipped)} 条"
        return result

    # 累计闸门：单次批量上限挡不住「小批量高频」，这里按 60 秒窗口累计拦截。
    recent = recent_withdrawn_count()
    result["recent_withdrawn"] = recent
    if burst_blocked(recent, len(active), confirm_scope):
        raise ValueError(
            f"{BATCH_WITHDRAW_BURST_WINDOW} 秒内已撤销 {recent} 条，本次再撤 {len(active)} 条"
            f"将超过上限 {BATCH_WITHDRAW_BURST} 条：疑似连点或脚本循环，本批未执行。"
            "确属本人操作，请确认范围后在请求里带 confirm_scope=true 重试"
        )

    note = _withdrawal_note(actor, reason)
    mark = json.dumps(note, ensure_ascii=False, default=str)
    active_ids = [r["id"] for r in active]
    with db.transaction():
        # RETURNING 拿到真正被更新的 id：预检与写入之间若被并发撤销，
        # 审计明细只记实际撤销的行，不虚报。
        n_row = db.fetch_one(
            "WITH u AS ("
            " UPDATE knowledge.facts SET status = 'withdrawn', contested = FALSE, "
            "   valid_to = COALESCE(valid_to, NOW()), "
            "   source_ref = COALESCE(source_ref, '{}'::jsonb) || CAST(:note AS jsonb) "
            " WHERE id = ANY(CAST(:ids AS uuid[])) AND status = 'active' "
            " RETURNING id::text AS id"
            ") SELECT count(*) AS n, "
            "  COALESCE(array_agg(id), ARRAY[]::text[]) AS ids FROM u",
            {"ids": active_ids, "note": mark},
        ) or {}
        updated_ids = list(n_row.get("ids") or [])
        result["withdrawn"] = len(updated_ids)

        from app.modules.audit import service as audit
        audit.log_access(note["withdrawn_by"], "knowledge.fact.batch_withdraw",
                         "knowledge.fact", None,
                         {"count": len(updated_ids), "reason": note["reason"],
                          # 调用方写进审计：再出现「谁在循环撤」时，按 UA/端口就能定位
                          "client": client or {},
                          "recent_withdrawn_60s": recent,
                          "fact_ids": updated_ids[:50]})
        done = set(updated_ids)
        db.executemany(
            "INSERT INTO audit.access_logs (actor, action, resource_type, resource_id, detail) "
            "VALUES (:a, :act, :rt, :rid, CAST(:d AS jsonb))",
            [{"a": note["withdrawn_by"], "act": "knowledge.fact.withdraw",
              "rt": "knowledge.fact", "rid": r["id"],
              "d": json.dumps(
                  {"claim_id": str(r["claim_id"]) if r["claim_id"] else None,
                   "predicate": r["predicate"],
                   "subject_id": str(r["subject_id"]) if r["subject_id"] else None,
                   "version": r["version"], "reason": note["reason"]},
                  ensure_ascii=False, default=str)}
             for r in active if r["id"] in done],
        )

    # R2：图投影边统一停用；事务外执行 —— 边是派生数据，删边失败不该回滚业务动作
    try:
        from app.modules.ontology import sync as graph_sync
        result["edges_synced"] = graph_sync.remove_fact_edges(
            updated_ids, "fact_withdrawn")
    except Exception as e:                      # noqa: BLE001
        result["edges_synced"] = {"removed": 0, "error": str(e)}
    result["message"] = (f"已批量撤销 {result['withdrawn']} 条事实"
                         + (f"，跳过 {len(skipped)} 条" if skipped else "")
                         + "（记录与证据链保留）")
    return result


# ---------------------------------------------------------------- 恢复发布（撤销的逆操作）
#
# 撤销本身是软删（行与证据链都在），所以「按错了」不该只能靠改库：这里把恢复做成
# 正式能力，并保证三条底线：
# 1. 只恢复 withdrawn 行 —— superseded 走 restore_superseded 的另一套语义，不混用；
# 2. 不制造双真 —— 单值维度（部门/职位…）若已有更新的活跃值，跳过并说明冲突，
#    否则「恢复一条旧部门」会让同一个主体同时有两个当前部门；
# 3. 可审计 —— source_ref 记下谁/何时/为什么恢复，并保留撤销时的原始留痕。
RESTORE_MAX = BATCH_WITHDRAW_MAX


def _restore_note(actor: str, reason: str, prev: dict | None) -> dict:
    """恢复留痕（纯函数）：谁、何时、为什么，并回带撤销时的原始留痕。"""
    note: dict = {
        "restored_by": (actor or "").strip()[:80] or "unknown",
        "restoration": "republished",
        "reason": (reason or "").strip()[:300] or "owner restored from console",
        "at": datetime.now().astimezone().isoformat(),
    }
    if isinstance(prev, dict) and (prev.get("reason") or prev.get("at")):
        note["previous_withdrawal"] = {
            "reason": prev.get("reason"),
            "at": prev.get("at"),
            "withdrawn_by": prev.get("withdrawn_by"),
        }
    return note


def _object_key(value) -> str:
    """客体的比较键：JSON 归一化，避免 {'a':1} 与 {'a': 1} 被当成两个值。"""
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def restore_facts(fact_ids: list[str], actor: str = "owner",
                  reason: str = "", dry_run: bool = False) -> dict:
    """把已撤销的事实恢复为 active（撤销的逆操作），**不删不造行**。

    与 batch_withdraw_facts 对称：整批一个事务 + 批量 SQL + 一次边补建。
    只有 withdrawn 行可恢复；与现有活跃值冲突的（完全相同 / 单值维度已有新值）
    计入 skipped 并逐条说明原因，不静默覆盖。dry_run=True 只预检不写库。
    """
    ensure_schema()
    if not isinstance(fact_ids, list) or not fact_ids:
        raise ValueError("fact_ids must be a non-empty list")
    seen: set[str] = set()
    ids: list[str] = []
    for raw in fact_ids:
        s = str(raw or "").strip()
        if not s:
            continue
        try:
            UUID(s)
        except (ValueError, TypeError, AttributeError):
            raise ValueError(f"invalid fact id: {s[:40]}") from None
        if s not in seen:
            seen.add(s)
            ids.append(s)
    if not ids:
        raise ValueError("fact_ids must be a non-empty list")
    if len(ids) > RESTORE_MAX:
        raise ValueError(f"batch size {len(ids)} exceeds limit {RESTORE_MAX}")

    rows = db.fetch_all(
        "SELECT id::text AS id, claim_id, predicate, subject_id::text AS subject_id, "
        "       object_value, version, status, source_ref "
        "FROM knowledge.facts WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": ids},
    )
    by_id = {r["id"]: r for r in rows}
    picked, skipped = [], []
    for fid in ids:
        row = by_id.get(fid)
        if not row:
            skipped.append({"fact_id": fid, "reason": "fact not found"})
        elif row["status"] != FACT_WITHDRAWN:
            skipped.append({"fact_id": fid,
                            "reason": f"fact status is {row['status']}, only withdrawn facts can be restored"})
        else:
            picked.append(row)

    # 冲突检查只看「同主体 + 同谓词」的现有活跃值：足够覆盖单值维度的双真风险，
    # 又不会因为别的谓词把整批挡住。
    subjects = sorted({r["subject_id"] for r in picked if r["subject_id"]})
    # 主体为空的事实（自由文本断言）也要参与冲突判断，否则「已有一个同名活跃值」
    # 这种情况查不出来，恢复后就会出现两条一模一样的活跃事实。
    null_ok = any(r["subject_id"] is None for r in picked)
    active_rows = db.fetch_all(
        "SELECT id::text AS id, subject_id::text AS subject_id, predicate, object_value "
        "FROM knowledge.facts WHERE status = 'active' "
        "  AND (subject_id = ANY(CAST(:s AS uuid[])) "
        "       OR (CAST(:null_ok AS boolean) AND subject_id IS NULL))",
        {"s": subjects, "null_ok": bool(null_ok)},
    ) if (subjects or null_ok) else []
    active_by_key: dict[tuple, list[dict]] = {}
    for a in active_rows:
        k = (a["subject_id"], (a["predicate"] or "").strip().lower())
        active_by_key.setdefault(k, []).append(a)

    restorable = []
    for row in picked:
        key = (row["subject_id"], (row["predicate"] or "").strip().lower())
        peers = active_by_key.get(key, [])
        mine = _object_key(row["object_value"])
        same = next((a for a in peers if _object_key(a["object_value"]) == mine), None)
        if same:
            skipped.append({"fact_id": row["id"],
                            "reason": "identical fact is already active",
                            "conflict_fact_id": same["id"]})
            continue
        if peers and cardinality_of(row["predicate"]) == "single":
            skipped.append({"fact_id": row["id"],
                            "reason": "single-valued predicate already has a newer active value",
                            "conflict_fact_id": peers[0]["id"]})
            continue
        restorable.append(row)

    result: dict = {
        "requested": len(ids),
        "restored": 0,
        "candidates": len(restorable),
        "skipped": skipped,
        "dry_run": bool(dry_run),
        "message": "",
    }
    if dry_run:
        result["message"] = (f"预检完成：可恢复 {len(restorable)} 条，"
                             f"跳过 {len(skipped)} 条（未写库）")
        return result
    if not restorable:
        result["message"] = f"没有可恢复的事实，跳过 {len(skipped)} 条"
        return result

    payload = json.dumps(
        [{"fid": r["id"],
          "note": _restore_note(actor, reason, r.get("source_ref"))}
         for r in restorable], ensure_ascii=False, default=str)
    with db.transaction():
        # RETURNING 拿到真正被更新的 id：预检与写入之间若被并发恢复/撤销，
        # 只对实际生效的行记审计，不虚报。
        done = db.fetch_all(
            "WITH v AS ("
            "  SELECT (e->>'fid')::uuid AS fid, COALESCE(e->'note', '{}'::jsonb) AS note "
            "  FROM jsonb_array_elements(CAST(:p AS jsonb)) e"
            ") "
            "UPDATE knowledge.facts f SET status = 'active', valid_to = NULL, "
            "  source_ref = COALESCE(f.source_ref, '{}'::jsonb) || v.note "
            "FROM v WHERE f.id = v.fid AND f.status = 'withdrawn' "
            "RETURNING f.id::text AS id",
            {"p": payload},
        )
        updated_ids = [r["id"] for r in done]
        result["restored"] = len(updated_ids)

        from app.modules.audit import service as audit

        audit.log_access((actor or "owner")[:80], "knowledge.fact.batch_restore",
                         "knowledge.fact", None,
                         {"count": len(updated_ids),
                          "reason": (reason or "").strip()[:300],
                          "fact_ids": updated_ids[:50]})
        if updated_ids:
            index = {r["id"]: r for r in restorable}
            db.executemany(
                "INSERT INTO audit.access_logs "
                "(actor, action, resource_type, resource_id, detail) "
                "VALUES (:a, :act, :rt, :rid, CAST(:d AS jsonb))",
                [{"a": (actor or "owner")[:80], "act": "knowledge.fact.restore",
                  "rt": "knowledge.fact", "rid": fid,
                  "d": json.dumps(
                      {"predicate": index[fid]["predicate"],
                       "version": index[fid]["version"],
                       "reason": (reason or "").strip()[:300]},
                      ensure_ascii=False, default=str)}
                 for fid in updated_ids if fid in index],
            )

    # 撤销时把投影边停掉了，恢复要把边一起补回来；边是派生数据，失败不该回滚业务动作
    try:
        from app.modules.ontology import sync as graph_sync
        result["edges_synced"] = graph_sync.upsert_fact_edges(updated_ids)
    except Exception as e:                      # noqa: BLE001
        result["edges_synced"] = {"created": 0, "error": str(e)}
    result["message"] = (f"已恢复 {result['restored']} 条事实"
                         + (f"，跳过 {len(skipped)} 条" if skipped else "")
                         + "（行与证据链原样保留）")
    return result


def restore_fact(fact_id: str, actor: str = "owner", reason: str = "") -> dict:
    """恢复单条已撤销事实（restore_facts 的单条封装，语义完全一致）。"""
    out = restore_facts([fact_id], actor=actor, reason=reason, dry_run=False)
    if not out["restored"]:
        why = (out["skipped"][0].get("reason") if out["skipped"] else "unknown")
        raise ValueError(f"cannot restore: {why}")
    return {"fact_id": fact_id, "status": FACT_ACTIVE,
            "reason": (reason or "").strip()[:300],
            "edges_synced": out.get("edges_synced", {}),
            "message": "已恢复发布：事实重新出现在列表与检索中"}


# ---------------------------------------------------------------- 实体画像


def _series(rows: list[dict], infer: bool = True) -> list[dict]:
    """把同一维度的多行按生效时间升序排好，并（仅单值维度）推断每段的结束时间。

    单值维度里老数据的 superseded 行没有 valid_to（历史遗留），用「下一段的 valid_from」
    补齐边界，保证 asof 时间旅行与当前值判定都正确，而不需要改历史行。
    多值维度的各值并列成立，不能互相充当边界，否则活跃值会被误判成已结束。
    """
    series = sorted(rows, key=lambda r: (_as_dt(r.get("valid_from")) is None,
                                         _as_dt(r.get("valid_from")) or _MIN_DT))
    for i, row in enumerate(series):
        end = row.get("valid_to")
        if infer and end is None and i + 1 < len(series):
            end = series[i + 1].get("valid_from")
        row["_effective_to"] = end
    return series


def _within(row: dict, asof: datetime) -> bool:
    start = _as_dt(row.get("valid_from"))
    end = _as_dt(row.get("_effective_to"))
    if start and start > asof:
        return False
    if end and end <= asof:
        return False
    return True


def _pick_current(series: list[dict], asof: datetime | None) -> dict | None:
    if asof is not None:
        hits = [r for r in series if _within(r, asof)]
        return hits[-1] if hits else None
    active = [r for r in series if r.get("status") == FACT_ACTIVE and r.get("_effective_to") is None]
    return active[-1] if active else None


def _timeline_rows(rows: list[dict]) -> list[dict]:
    """只有 active / superseded 才是「曾经或正在为真」的取值段。

    contested / withdrawn 是待裁决或已撤销的候选，只作留痕、不参与时间线；
    否则会给上一段凭空制造结束时间，当前值就会消失。
    """
    return [r for r in rows if r.get("status") in (FACT_ACTIVE, FACT_SUPERSEDED)]


def _slim_fact(row: dict) -> dict:
    return {
        "fact_id": str(row.get("id")),
        "value": row.get("object_value"),
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("_effective_to", row.get("valid_to")),
        "recorded_at": row.get("recorded_at"),
        "status": row.get("status"),
        "version": row.get("version"),
        "confidence": float(row["confidence"]) if row.get("confidence") is not None else None,
        "trust_tier": trust_label(row.get("trust_tier")),
        "visibility": row.get("visibility"),
        "claim_id": str(row["claim_id"]) if row.get("claim_id") else None,
    }


def entity_profile(entity_id: str, asof: str | datetime | None = None,
                   min_confidence: float | None = None,
                   include_relations: bool = True,
                   include_restricted: bool = False,
                   principal: dict | None = None) -> dict | None:
    """实体画像：维度 → 当前值 + 历史段 + 争议值，支持 asof 时间旅行。

    G2：默认按可见性白名单过滤，避免「按实体反查」绕过问答召回的敏感闸门。
    P1-4：传入 principal（AC-01 上下文归一化主体）时叠加 租户/归属 逐点过滤；
    不传保持原行为（owner 级等价），内部调用（图/管线）不受影响。
    """
    ensure_schema()
    entity = db.fetch_one(
        "SELECT id, entity_type, canonical_name, aliases, properties, status, merged_into "
        "FROM ontology.entities WHERE id = CAST(:id AS uuid)",
        {"id": entity_id},
    )
    if not entity:
        return None

    asof_dt = _as_dt(asof) if asof else None
    extra: dict = {}
    if include_restricted and (principal is None or policy_mod.is_owner_class(principal)):
        vis_clause = ""  # 仅 owner 级主体可显式越白名单看 restricted 留痕
    elif principal is not None:
        vis_clause = " AND " + policy_mod.read_clause("", principal, extra)
    else:
        from app.visibility import retrievable_clause

        vis_clause = f" AND {retrievable_clause()}"
    rows = db.fetch_all(
        f"SELECT {_COLS} FROM knowledge.facts WHERE subject_id = CAST(:id AS uuid) "
        f"{vis_clause} "
        "ORDER BY predicate, COALESCE(valid_from, recorded_at) ASC NULLS FIRST",
        {"id": entity_id, **extra},
    )

    by_predicate: dict[str, list[dict]] = {}
    for r in rows:
        by_predicate.setdefault(r["predicate"] or "(未命名维度)", []).append(r)

    dimensions = []
    contested_count = 0
    withdrawn_count = 0
    for predicate, group in sorted(by_predicate.items()):
        # contested / withdrawn 只作留痕，不进时间线（见 _timeline_rows）
        value_rows = _timeline_rows(group)
        open_rows = [r for r in group if r.get("status") not in (FACT_ACTIVE, FACT_SUPERSEDED)]
        card = cardinality_of(predicate)
        series = _series(value_rows, infer=(card == "single"))
        # 单值维度：一段时间只有一个值为真 → 有「当前值 + 取代链」；
        # 多值维度：多个值并列成立 → 给「该时点成立的取值集合」，不伪造时间线。
        current = _pick_current(series, asof_dt) if card == "single" else None
        if card == "multi" and asof_dt is not None:
            # asof 回看：多值断言各自判断当时是否成立，互不作边界。
            # 少了这一步，回看 2000 年会看到今天的观点，时间旅行就名不副实。
            live = [r for r in series if _within(r, asof_dt)]
        else:
            live = [r for r in series
                    if r.get("status") == FACT_ACTIVE and r.get("_effective_to") is None]
        history, contested, withdrawn, values = [], [], [], []
        for r in series:
            item = _slim_fact(r)
            if min_confidence is not None:
                if item["confidence"] is not None and item["confidence"] < min_confidence:
                    continue
            history.append(item)
        for r in live:
            item = _slim_fact(r)
            if min_confidence is not None and item["confidence"] is not None \
                    and item["confidence"] < min_confidence:
                continue
            values.append(item)
        for r in open_rows:
            if r.get("status") == FACT_CONTESTED:
                contested.append(_slim_fact(r))
                contested_count += 1
            elif r.get("status") == FACT_WITHDRAWN:
                # 已撤销只作留痕：不进时间线，但必须在画像里查得到，
                # 否则「撤销后去哪了」无从回答（评估文档的留痕缺口）。
                withdrawn.append(_slim_fact(r))
                withdrawn_count += 1
        cur_item = _slim_fact(current) if current else None
        if cur_item and min_confidence is not None and cur_item["confidence"] is not None \
                and cur_item["confidence"] < min_confidence:
            cur_item = None
        if not cur_item and not values and not history and not contested and not withdrawn:
            continue
        dimensions.append({
            "dimension": predicate,
            "cardinality": card,
            "current": cur_item,
            "values": values if card == "multi" else None,
            "history": history,
            "contested": contested,
            "withdrawn": withdrawn,
        })

    out = {
        "entity": {
            "id": str(entity["id"]),
            "canonical_name": entity["canonical_name"],
            "entity_type": entity["entity_type"],
            "aliases": entity.get("aliases") or [],
            "status": entity.get("status"),
            "merged_into": str(entity["merged_into"]) if entity.get("merged_into") else None,
        },
        "asof": asof_dt.isoformat() if asof_dt else None,
        "dimensions": dimensions,
        "counts": {
            "facts": len(rows),
            "active": sum(1 for r in rows if r.get("status") == FACT_ACTIVE),
            "contested": contested_count,
            "withdrawn": withdrawn_count,
            "dimensions": len(dimensions),
        },
        "trust_legend": {"owner": "本人确认", "agent": "AI 抽取", "unknown": "来源未知"},
    }
    if include_relations:
        out["relations"] = db.fetch_all(
            "SELECT r.id, r.relation_type, r.status, r.valid_from, r.valid_to, "
            "       r.evidence_claim_id, t.id AS target_id, "
            "       t.canonical_name AS target_name, t.entity_type AS target_type "
            "FROM ontology.relations r "
            "JOIN ontology.entities t ON t.id = r.target_entity_id "
            "WHERE r.source_entity_id = CAST(:id AS uuid) "
            "ORDER BY r.status, r.relation_type LIMIT 100",
            {"id": entity_id},
        )
        out["relations_incoming"] = db.fetch_all(
            "SELECT r.id, r.relation_type, r.status, s.id AS source_id, "
            "       s.canonical_name AS source_name, s.entity_type AS source_type "
            "FROM ontology.relations r "
            "JOIN ontology.entities s ON s.id = r.source_entity_id "
            "WHERE r.target_entity_id = CAST(:id AS uuid) "
            "ORDER BY r.status, r.relation_type LIMIT 100",
            {"id": entity_id},
        )
    return out


def backfill_trust() -> dict:
    """一次性回填历史事实的可信度元数据（幂等，不改变任何事实内容）。

    - 信任级：历史事实全部来自「本人审核通过 Claim」，应为 owner；
      只处理 trust_tier 仍为默认值 1 的事实，agent 写入始终显式带 2，不会被误升级。
    - 置信度：从对应 Claim 复制。
    - 来源快照：从 Claim 证据里取文档 id/标题。
    - 生效时间：早期 Claim 没填 valid_from，用 recorded_at 兜底，
      否则时间线全是空值，asof 查询等于失效。
    """
    ensure_schema()
    with db.transaction():
        owner_rows = db.fetch_one(
            "WITH u AS ("
            " UPDATE knowledge.facts f SET trust_tier = :t "
            " FROM knowledge.claims c "
            " WHERE c.id = f.claim_id AND c.status = 'approved' "
            "   AND COALESCE(f.trust_tier, 1) = 1 "
            " RETURNING 1) SELECT count(*) AS n FROM u",
            {"t": TRUST_OWNER},
        )
        conf_rows = db.fetch_one(
            "WITH u AS ("
            " UPDATE knowledge.facts f SET confidence = round(c.confidence, 3) "
            " FROM knowledge.claims c "
            " WHERE c.id = f.claim_id AND f.confidence IS NULL "
            "   AND c.confidence IS NOT NULL RETURNING 1) SELECT count(*) AS n FROM u"
        )
        src_rows = db.fetch_one(
            "WITH u AS ("
            " UPDATE knowledge.facts f SET source_ref = jsonb_build_object("
            "   'kind', 'claim_approval', 'claim_id', f.claim_id, "
            "   'document_id', ("
            "     SELECT d.id FROM knowledge.claim_evidence ce "
            "     JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
            "     JOIN source.documents d ON d.id = es.document_id "
            "     WHERE ce.claim_id = f.claim_id LIMIT 1), "
            "   'document_title', ("
            "     SELECT d.title FROM knowledge.claim_evidence ce "
            "     JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
            "     JOIN source.documents d ON d.id = es.document_id "
            "     WHERE ce.claim_id = f.claim_id LIMIT 1)) "
            " WHERE f.claim_id IS NOT NULL "
            "   AND (f.source_ref IS NULL OR f.source_ref = '{}'::jsonb) "
            " RETURNING 1) SELECT count(*) AS n FROM u"
        )
    valid_rows = db.fetch_one(
        "WITH u AS ("
        " UPDATE knowledge.facts SET valid_from = recorded_at "
        " WHERE valid_from IS NULL AND recorded_at IS NOT NULL "
        " RETURNING 1) SELECT count(*) AS n FROM u"
    )
    return {"trust_upgraded": int((owner_rows or {"n": 0})["n"]),
            "confidence_filled": int((conf_rows or {"n": 0})["n"]),
            "source_ref_filled": int((src_rows or {"n": 0})["n"]),
            "valid_from_filled": int((valid_rows or {"n": 0})["n"])}


def restore_superseded(apply: bool = False) -> dict:
    """恢复被旧规则误取代的多值事实。

    旧版发布逻辑只要「同主体 + 同谓词」就把此前所有活跃事实置为 superseded，
    对「观点/参与/提出/负责」这类多值维度等于静默丢知识。这里列出这些被埋掉的事实：
    默认只预览（apply=False），显式 apply=True 才复位为 active（不删任何行、可审计）。
    单值维度（部门/职位等）的取代是正确的，一律不动。
    """
    ensure_schema()
    rows = db.fetch_all(
        "SELECT f.id, f.predicate, f.subject_id, f.object_value, f.recorded_at "
        "FROM knowledge.facts f WHERE f.status = 'superseded' "
        "  AND NOT EXISTS (SELECT 1 FROM knowledge.facts a WHERE a.status = 'active' "
        "    AND a.subject_id IS NOT DISTINCT FROM f.subject_id "
        "    AND a.predicate = f.predicate AND a.object_value = f.object_value) "
        "ORDER BY f.predicate"
    )
    multi = [r for r in rows if cardinality_of(r["predicate"]) == "multi"]
    by_predicate: dict[str, int] = {}
    for r in multi:
        by_predicate[r["predicate"]] = by_predicate.get(r["predicate"], 0) + 1
    out = {"candidates": len(multi),
           "skipped_single_valued": len(rows) - len(multi),
           "by_predicate": dict(sorted(by_predicate.items(),
                                       key=lambda kv: -kv[1])[:10]),
           "applied": 0}
    if not apply or not multi:
        return out
    with db.transaction():
        for r in multi:
            db.execute(
                "UPDATE knowledge.facts SET status = 'active', valid_to = NULL, "
                "  superseded_by = NULL WHERE id = CAST(:id AS uuid) "
                "  AND status = 'superseded'",
                {"id": str(r["id"])},
            )
    out["applied"] = len(multi)
    return out


# ---------------------------------------------------------------- 元本体统计

def dimensions() -> list[dict]:
    """元本体：当前在跟踪哪些维度，各覆盖多少实体、多少观测。"""
    ensure_schema()
    rows = db.fetch_all(
        "SELECT predicate AS dimension, count(*) AS observations, "
        "       count(DISTINCT subject_id) AS entities, "
        "       count(*) FILTER (WHERE status = 'active' AND valid_to IS NULL) AS current_values, "
        "       count(*) FILTER (WHERE status = 'contested') AS contested, "
        "       max(COALESCE(valid_from, recorded_at)) AS last_seen "
        "FROM knowledge.facts GROUP BY predicate ORDER BY observations DESC"
    )
    for r in rows:
        r["current_values"] = int(r.get("current_values") or 0)
        r["contested"] = int(r.get("contested") or 0)
    return rows


# ---------------------------------------------------------------- 自检
#
# 本体层最容易「看起来生效、其实没生效」：列加了但没回填、回填了但 asof 是空的、
# 判定逻辑被改坏了却没人发现。这里把可验证的断言集中成一份自检报告，
# 页面「本体中心」和 scripts/verify_ontology.py 共用同一份结论。

_DECISION_CASES = [
    # (标题, current, incoming, 期望 action)
    ("无历史值 → 直接成为当前值", None,
     {"trust_tier": TRUST_OWNER, "object_value": {"v": 1}}, "inserted"),
    ("本人重复确认同值 → 幂等跳过",
     {"same_value": True, "trust_tier": TRUST_OWNER},
     {"trust_tier": TRUST_OWNER, "object_value": {"v": 1}}, "duplicate"),
    ("AI 观察到与本人一致的值 → 提升可信度",
     {"same_value": True, "trust_tier": TRUST_AGENT},
     {"trust_tier": TRUST_OWNER, "object_value": {"v": 1}}, "reinforced"),
    ("本人改口 → 取代旧值",
     {"same_value": False, "trust_tier": TRUST_OWNER},
     {"trust_tier": TRUST_OWNER, "object_value": {"v": 2}}, "superseded"),
    ("AI 与本人结论冲突 → 只挂争议，不覆盖",
     {"same_value": False, "trust_tier": TRUST_OWNER},
     {"trust_tier": TRUST_AGENT, "object_value": {"v": 9}}, "contested"),
    ("来源未知 vs 来源未知 → 同级可取代",
     {"same_value": False, "trust_tier": TRUST_UNKNOWN},
     {"trust_tier": TRUST_UNKNOWN, "object_value": {"v": 3}}, "superseded"),
    ("回溯到更早的时间 → 独立历史段，不作废当前值",
     {"same_value": False, "trust_tier": TRUST_OWNER,
      "valid_from": "2026-06-01T00:00:00"},
     {"trust_tier": TRUST_OWNER, "valid_from": "2026-01-01T00:00:00",
      "object_value": {"v": 0}}, "backdated_stint"),
]


def _check_matrix() -> list[dict]:
    """判定矩阵：纯函数跑一遍，任何一条不符就说明写入护栏被改坏了。"""
    out = []
    for title, current, incoming, expect in _DECISION_CASES:
        try:
            got = decide_write(current, incoming).get("action")
        except Exception as exc:  # noqa: BLE001
            got = f"error: {exc}"
        out.append({"name": title, "expect": expect, "actual": got,
                    "ok": got == expect})
    return out


def _check_timetravel() -> list[dict]:
    """时间旅行：同一维度两段历史，asof 落在不同区间必须取到不同值。"""
    def seg(fid, frm, to, val):
        return {"id": fid, "status": FACT_ACTIVE, "valid_from": frm, "valid_to": to,
                "_effective_to": to, "object_value": {"v": val}}

    series = [
        seg("a", datetime(2026, 1, 1), datetime(2026, 6, 1), "旧值"),
        seg("b", datetime(2026, 6, 1), None, "新值"),
    ]
    probes = [
        ("2026-03-01", "旧值"),
        ("2026-09-01", "新值"),
        ("2025-06-01", None),   # 该区间还没开始
    ]
    out = []
    for asof, expect in probes:
        hit = _pick_current(series, _as_dt(asof))
        got = (hit or {}).get("object_value", {}).get("v") if hit else None
        out.append({"asof": asof, "expect": expect, "actual": got, "ok": got == expect})
    return out


def diagnostics() -> dict:
    """本体自检：结构 → 回填 → 判定 → 基数 → 数据形态，逐项给结论。"""
    ensure_schema()
    checks: list[dict] = []

    def add(cid, name, ok, detail, value=None):
        checks.append({"id": cid, "name": name, "ok": bool(ok),
                       "detail": detail, "value": value})

    # 1. 表结构
    cols = {r["column_name"] for r in db.fetch_all(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'knowledge' AND table_name = 'facts'")}
    need = {"confidence", "trust_tier", "visibility", "superseded_by",
            "contested", "source_ref", "valid_from", "valid_to", "recorded_at"}
    missing = sorted(need - cols)
    add("schema", "facts 表具备双时间线与可信度列", not missing,
        "缺列：" + "、".join(missing) if missing else "9 个关键列全部存在")

    # 2. 历史回填
    b = db.fetch_one(
        "SELECT count(*) AS total, "
        "  count(*) FILTER (WHERE COALESCE(trust_tier, 1) <= 1) AS untrusted, "
        "  count(*) FILTER (WHERE confidence IS NULL) AS no_conf, "
        "  count(*) FILTER (WHERE valid_from IS NULL) AS no_from "
        "FROM knowledge.facts") or {}
    total = int(b.get("total") or 0)
    untrusted = int(b.get("untrusted") or 0)
    no_from = int(b.get("no_from") or 0)
    no_conf = int(b.get("no_conf") or 0)
    add("backfill_trust", "已确认事实的信任级已回填",
        total > 0 and untrusted == 0,
        f"共 {total} 条，其中信任级仍为 unknown 的 {untrusted} 条",
        {"total": total, "untrusted": untrusted})
    add("backfill_valid_from", "事实都有生效时间（asof 可用）",
        total > 0 and no_from == 0,
        f"缺 valid_from 的 {no_from} 条", {"missing": no_from})
    add("backfill_confidence", "事实带置信度",
        total > 0 and no_conf < max(1, total // 5),
        f"缺 confidence 的 {no_conf} 条（{round(no_conf * 100 / total, 1) if total else 0}%）",
        {"missing": no_conf, "total": total})

    # 3. 判定矩阵
    matrix = _check_matrix()
    bad = [m["name"] for m in matrix if not m["ok"]]
    add("decisions", "写入判定矩阵（6 类动作）", not bad,
        "全部符合预期" if not bad else "不符：" + "；".join(bad), matrix)

    # 4. 时间旅行
    tt = _check_timetravel()
    bad_tt = [t["asof"] for t in tt if not t["ok"]]
    add("time_travel", "asof 时间旅行取到正确历史值", not bad_tt,
        "3 个探测点全部命中" if not bad_tt else "不符：" + "、".join(bad_tt), tt)

    # 5. 维度基数
    singles = db.fetch_all(
        "SELECT DISTINCT predicate FROM knowledge.facts") or []
    declared = sorted(p for p in SINGLE_VALUED
                      if any((r["predicate"] or "").lower() == p for r in singles))
    sample_single = declared[0] if declared else "works_at"
    sample_multi = "believes"
    add("cardinality", "单值/多值维度区分正确",
        cardinality_of(sample_single) == "single" and cardinality_of(sample_multi) == "multi",
        f"样例：{sample_single} → single，{sample_multi} → multi；"
        f"已声明单值维度 {len(SINGLE_VALUED)} 个，库中命中 {len(declared)} 个",
        {"single_declared": len(SINGLE_VALUED), "single_in_db": len(declared)})

    # 6. 多值并存（旧规则会把它压成 1 条）
    multi = db.fetch_one(
        "SELECT count(*) AS n FROM ("
        "  SELECT subject_id, predicate FROM knowledge.facts "
        "  WHERE status = 'active' AND valid_to IS NULL "
        "  GROUP BY subject_id, predicate HAVING count(*) > 1) t") or {}
    multi_n = int(multi.get("n") or 0)
    # 被旧规则误埋的多值事实：不自动改数据，只报数并给出恢复入口
    try:
        pending = restore_superseded(apply=False)
    except Exception:  # noqa: BLE001
        pending = {"candidates": 0, "by_predicate": {}, "skipped_single_valued": 0}
    cand_n = int(pending.get("candidates") or 0)
    if cand_n:
        detail = (f"当前 {multi_n} 组并列活跃值；另有 {cand_n} 条多值断言被旧规则误取代，"
                  f"可在下方一键恢复（单值维度 {pending.get('skipped_single_valued', 0)} 条不受影响）")
    elif multi_n:
        detail = f"{multi_n} 组（主体, 维度）下同时存在多个活跃值"
    else:
        detail = "当前每组的取值都只有一条，暂无多值断言可验证"
    add("multi_value", "多值维度允许并列成立（未被误取代）",
        multi_n > 0 and cand_n == 0, detail,
        {"groups": multi_n, "restore_candidates": cand_n,
         "by_predicate": pending.get("by_predicate") or {}})

    # 7. 争议通道
    c = db.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'contested') AS contested, "
        "  count(*) FILTER (WHERE status = 'superseded') AS superseded, "
        "  count(*) FILTER (WHERE status = 'withdrawn') AS withdrawn "
        "FROM knowledge.facts") or {}
    contested = int(c.get("contested") or 0)
    superseded = int(c.get("superseded") or 0)
    withdrawn = int(c.get("withdrawn") or 0)
    add("conflict_channel", "争议、取代链与撤销留痕可查询", True,
        f"待裁决 {contested} 条，已取代（历史段）{superseded} 条，已撤销 {withdrawn} 条",
        {"contested": contested, "superseded": superseded, "withdrawn": withdrawn})

    # 8. 信任级分布
    dist = db.fetch_all(
        "SELECT COALESCE(trust_tier, 1) AS tier, count(*) AS n "
        "FROM knowledge.facts GROUP BY 1 ORDER BY 1 DESC") or []
    named = [{"tier": trust_label(int(r["tier"])), "count": int(r["n"])} for r in dist]
    add("trust_dist", "信任级分布可观测", bool(named),
        "、".join(f"{x['tier']}={x['count']}" for x in named), named)

    passed = sum(1 for x in checks if x["ok"])
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "summary": {"total": len(checks), "passed": passed,
                    "failed": len(checks) - passed},
        "checks": checks,
    }
