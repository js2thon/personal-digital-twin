"""映射契约：本人一次性审核「规则」，替代逐条审核「值」。

设计依据见 docs/structured-ingestion-assessment.md：
* ADR-003 的 Claim/Fact 闸门在这里等价替换为「契约必须 published 才允许写入」；
  结构化链路不产生 Claim，绝不用假 Claim 冒充审核。
* 契约发布是单事务：draft → published、旧版本 → deprecated、写审计，
  并带 contract_hash，任何改动都会产生新版本而不是覆盖已发布规则。
"""
from __future__ import annotations

import hashlib
import json
import threading

from app.db import session as db

# 可见性词表。检索/列表/画像一律用白名单判定，未知值 fail-closed。
VIS_OWNER = "owner"
VIS_ORGANIZATION = "organization"
VIS_RESTRICTED = "restricted"
RETRIEVABLE_VISIBILITY = (VIS_OWNER, VIS_ORGANIZATION)
VALID_VISIBILITY = set(RETRIEVABLE_VISIBILITY) | {VIS_RESTRICTED}

CONTRACT_DRAFT = "draft"
CONTRACT_PUBLISHED = "published"
CONTRACT_DEPRECATED = "deprecated"
# 一次性导入的规则快照：不进发布/废弃流程，只做追溯锚点。
# 与 draft 的区别是它**不可发布**（不是候选规则），与 published 的区别是它
# **不作为后续同步的闸门**（每次同步都带当次确认过的 mapping）。
CONTRACT_EPHEMERAL = "ephemeral"
# 已删除（标记删除）：从列表与下拉消失，但行仍在，历史同步留痕可按 id 追溯。
# 只有「一次同步都没跑过」的契约才允许物理删除 —— 留痕是「这批事实由哪套规则
# 产生」的唯一线索，物理删掉契约行就等于静默丢掉追溯。
CONTRACT_ARCHIVED = "archived"

# 结构化异常原因码 → 面向本人的处置说明（谁 / 在哪 / 怎么做 / 怎么验证）。
REASON_META: dict[str, dict] = {
    "schema_drift": {
        "label": "源表列与契约不一致",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 映射契约",
        "steps": ["对照 expect_columns 核对新增/删除的列", "修订契约并发布新版本", "重新预览确认零异常"],
        "verify": "新契约 preview 不再出现 schema_drift",
        "suggested_action": "resolve",
    },
    "missing_required": {
        "label": "必填字段为空",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["回源库核对该行主键数据", "补齐后重新同步，或在契约中降级为非必填"],
        "verify": "下次同步该主键不再出现此异常",
        "suggested_action": "resolve",
    },
    "entity_unresolved": {
        "label": "实体锚点未解析",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["核对工号 / 主管工号是否在主数据中存在", "补齐主数据后重新同步"],
        "verify": "实体归一后该行产出断言，异常不再出现",
        "suggested_action": "resolve",
    },
    "multi_manager": {
        "label": "单值维度同批次出现多个值",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["确认是否为调岗 / 换主管等历史变更（建议按生效日期分批同步）",
                  "回源核对该主键的多个值是否都为真",
                  "放行后由单值维度取代链处理，不静默覆盖"],
        "verify": "该主键该谓词仍只有一条 active，历史段无空洞",
        "suggested_action": "resolve",
    },
    "cycle_detected": {
        "label": "汇报关系成环",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["按提示的环路径回源修正 manager_id", "修正后重新同步"],
        "verify": "环检测通过，异常不再出现",
        "suggested_action": "resolve",
    },
    "self_reference": {
        "label": "主管填成本人（不建自环边）",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["确认该员工是否为本组织链顶端（部门负责人常见填自己）",
                  "如需上级关系，回源补真实主管工号后重新同步",
                  "其他人不受影响，汇报关系已照常写入"],
        "verify": "该主键不再出现此异常，或确认其本就是链顶端",
        "suggested_action": "resolve",
    },
    "enum_invalid": {
        "label": "取值不在允许词表",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表 / 映射契约",
        "steps": ["回源修正取值，或在契约 allowed_values 中补入合法值", "发布新契约后重新同步"],
        "verify": "该非法值不再出现",
        "suggested_action": "resolve",
    },
    "restricted_skipped": {
        "label": "敏感字段按契约跳过",
        "owner": "系统（无需人工）",
        "where": "同步批次留痕 counts.restricted_skipped",
        "steps": ["默认不落库；如需落库须先完成合规擦除方案评审"],
        "verify": "同步批次中该字段无对应 fact 产生",
        "suggested_action": "dismiss",
    },
    "unknown": {
        "label": "未分类异常",
        "owner": "本人（owner）",
        "where": "蒸馏任务 → 结构化数据源 → 异常列表",
        "steps": ["查看 detail 原始诊断", "按实际情况处置"],
        "verify": "处置后状态不再是 open",
        "suggested_action": "resolve",
    },
}


def reason_meta(code: str) -> dict:
    meta = REASON_META.get(str(code or "").strip().lower(), REASON_META["unknown"])
    return {"code": str(code or "unknown"), **meta, "steps": list(meta["steps"])}


# ---------------------------------------------------------------- 契约校验（纯函数）

_ALLOWED_OBJECT_KIND = {"literal", "entity_ref"}
_ALLOWED_ENTITY_TYPES = {"Person", "Organization", "Role", "Knowledge", "Skill",
                         "Domain", "Concept", "Experience", "Project", "Event",
                         "Achievement", "Decision", "Option", "Criterion", "Rule",
                         "Relationship", "Communication", "Belief", "Preference",
                         "Value", "Principle"}


def validate_mapping(mapping) -> tuple[list[str], dict]:
    """纯函数：校验映射契约结构，返回 (错误列表, 规范化后的 mapping)。

    校验失败一律不允许保存为 draft，更不允许发布 —— 结构错误的契约比没有契约更危险。
    """
    errors: list[str] = []
    if not isinstance(mapping, dict):
        return ["mapping 必须是 JSON 对象"], {}
    entity = mapping.get("entity")
    if not isinstance(entity, dict):
        errors.append("缺少 entity 配置")
        entity = {}
    for key in ("entity_type", "pk_column", "name_column"):
        if not str(entity.get(key) or "").strip():
            errors.append(f"entity.{key} 不能为空")
    etype = str(entity.get("entity_type") or "")
    if etype and etype not in _ALLOWED_ENTITY_TYPES:
        errors.append(f"entity.entity_type「{etype}」不在基础本体六域内（不允许新知识造新类型）")

    fields = mapping.get("fields")
    if not isinstance(fields, list) or not fields:
        errors.append("fields 必须是非空数组")
        fields = []
    seen_predicates: set[str] = set()
    clean_fields = []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            errors.append(f"fields[{i}] 必须是对象")
            continue
        col = str(f.get("column") or "").strip()
        pred = str(f.get("predicate") or "").strip()
        if not col or not pred:
            errors.append(f"fields[{i}].column / predicate 不能为空")
            continue
        if pred in seen_predicates:
            errors.append(f"fields[{i}] 谓词「{pred}」重复")
        seen_predicates.add(pred)
        kind = str(f.get("object_kind") or "literal").strip()
        if kind not in _ALLOWED_OBJECT_KIND:
            errors.append(f"fields[{i}].object_kind「{kind}」不合法")
            continue
        vis = str(f.get("visibility") or mapping.get("sensitivity", {}).get("default")
                  or VIS_OWNER).strip()
        if vis not in VALID_VISIBILITY:
            errors.append(f"fields[{i}].visibility「{vis}」不在词表内")
            continue
        if kind == "entity_ref":
            target = str(f.get("target_entity_type") or "Person").strip()
            if target not in _ALLOWED_ENTITY_TYPES:
                errors.append(f"fields[{i}].target_entity_type「{target}」不合法")
                continue
        clean_fields.append({
            "column": col,
            "predicate": pred,
            "object_kind": kind,
            "target_entity_type": str(f.get("target_entity_type") or "").strip(),
            "relation_type": str(f.get("relation_type") or "").strip(),
            "target_name_column": str(f.get("target_name_column") or "").strip(),
            "visibility": vis,
            "valid_from_column": str(f.get("valid_from_column") or "").strip(),
            "valid_to_column": str(f.get("valid_to_column") or "").strip(),
            "required": bool(f.get("required", False)),
            "target_pk_prefix": str(f.get("target_pk_prefix")
                                    or entity.get("external_id_prefix") or "").strip(),
        })

    expect = mapping.get("expect_columns")
    if expect is not None and (not isinstance(expect, list)
                               or any(not str(c).strip() for c in expect)):
        errors.append("expect_columns 必须是非空字符串数组")
        expect = []
    clean = {
        "entity": {
            "entity_type": etype,
            "pk_column": str(entity.get("pk_column") or "").strip(),
            "name_column": str(entity.get("name_column") or "").strip(),
            "external_id_prefix": str(entity.get("external_id_prefix") or "hr:").strip(),
        },
        "fields": clean_fields,
        "sensitivity": {
            "default": str((mapping.get("sensitivity") or {}).get("default") or VIS_OWNER),
            "restricted_columns": [str(c) for c in
                                   (mapping.get("sensitivity") or {}).get("restricted_columns", [])],
        },
        "allowed_values": mapping.get("allowed_values") or {},
        "expect_columns": [str(c).strip() for c in (expect or [])],
    }
    if clean["sensitivity"]["default"] not in VALID_VISIBILITY:
        errors.append("sensitivity.default 不在可见性词表内")
    if not isinstance(clean["allowed_values"], dict):
        errors.append("allowed_values 必须是对象")
        clean["allowed_values"] = {}
    return errors, clean


def contract_hash(mapping: dict, source_ref: dict) -> str:
    """规范化 JSON 的 sha256：字段顺序变化不改哈希，任何实质改动都会改哈希。"""
    body = json.dumps({"mapping": mapping, "source_ref": source_ref},
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 存储

_ensured = False
_ensure_lock = threading.Lock()


def ensure_schema() -> None:
    """启动时幂等建表（DDL 与 db/init/04_structured_ingestion.sql 保持一致）。"""
    global _ensured
    if _ensured:
        return
    with _ensure_lock:
        if _ensured:
            return
        db.execute(
            "CREATE TABLE IF NOT EXISTS ingest.structured_contracts ("
            " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            " name VARCHAR(120) NOT NULL, version INTEGER NOT NULL DEFAULT 1,"
            " status VARCHAR(20) NOT NULL DEFAULT 'draft',"
            " source_ref JSONB NOT NULL DEFAULT '{}',"
            " mapping JSONB NOT NULL DEFAULT '{}',"
            " contract_hash VARCHAR(128) NOT NULL,"
            " note TEXT DEFAULT '', created_by VARCHAR(50) NOT NULL,"
            " published_by VARCHAR(50), published_at TIMESTAMPTZ,"
            " created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(),"
            " UNIQUE (name, version))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_structured_contracts_status "
            "ON ingest.structured_contracts (status, name)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS ingest.structured_sync_runs ("
            " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            " contract_id UUID NOT NULL, contract_version INTEGER NOT NULL,"
            " contract_hash VARCHAR(128) NOT NULL, batch_id VARCHAR(128) NOT NULL,"
            " dry_run BOOLEAN NOT NULL DEFAULT TRUE,"
            " status VARCHAR(20) NOT NULL DEFAULT 'completed',"
            " row_count INTEGER NOT NULL DEFAULT 0, counts JSONB NOT NULL DEFAULT '{}',"
            " cursor_before JSONB DEFAULT '{}', cursor_after JSONB DEFAULT '{}',"
            " actor VARCHAR(50) NOT NULL, error_info JSONB DEFAULT '{}',"
            " created_at TIMESTAMPTZ DEFAULT NOW())"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_structured_sync_runs_contract "
            "ON ingest.structured_sync_runs (contract_id, created_at DESC)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS ingest.structured_anomalies ("
            " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            " sync_run_id UUID NOT NULL, contract_id UUID NOT NULL,"
            " reason VARCHAR(50) NOT NULL, pk VARCHAR(200) DEFAULT '',"
            " field VARCHAR(200) DEFAULT '', detail JSONB DEFAULT '{}',"
            " status VARCHAR(20) NOT NULL DEFAULT 'open',"
            " actor VARCHAR(50), note TEXT DEFAULT '',"
            " verification VARCHAR(20) NOT NULL DEFAULT 'pending',"
            " verified_at TIMESTAMPTZ, verified_detail JSONB DEFAULT '{}',"
            " created_at TIMESTAMPTZ DEFAULT NOW())"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_structured_anomalies_open "
            "ON ingest.structured_anomalies (contract_id, status, created_at DESC)"
        )
        # 上传快照：文件解析后的行暂存于此，preview/sync 按 upload_id 复用。
        # 存快照而不是让浏览器回传全量行，是为了 5000 行级别的批不经过前端；
        # 敏感列确实会随快照留存在库，因此提供显式删除入口（合规要求）。
        db.execute(
            "CREATE TABLE IF NOT EXISTS ingest.structured_uploads ("
            " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
            " file_name VARCHAR(255) NOT NULL, format VARCHAR(10) NOT NULL,"
            " size_bytes INTEGER NOT NULL DEFAULT 0, sha256 VARCHAR(64) NOT NULL,"
            " columns JSONB NOT NULL DEFAULT '[]', row_count INTEGER NOT NULL DEFAULT 0,"
            " rows JSONB NOT NULL DEFAULT '[]', warnings JSONB NOT NULL DEFAULT '[]',"
            " contract_id UUID, uploaded_by VARCHAR(50) NOT NULL,"
            " created_at TIMESTAMPTZ DEFAULT NOW())"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_structured_uploads_created "
            "ON ingest.structured_uploads (created_at DESC)"
        )
        _ensured = True


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def create_contract(*, name: str, source_ref: dict, mapping: dict,
                    actor: str, note: str = "") -> dict:
    """创建 draft 契约；同名自动递增版本，绝不覆盖已发布版本。"""
    ensure_schema()
    errors, clean = validate_mapping(mapping)
    if errors:
        return {"error": "invalid_mapping", "errors": errors}
    name = str(name or "").strip()
    if not name:
        return {"error": "invalid_name", "errors": ["name 不能为空"]}
    with db.transaction():
        row = db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v "
            "FROM ingest.structured_contracts WHERE name = :n", {"n": name}
        )
        version = int(row["v"] if row else 1)
        cid = str(db.insert_returning_id(
            "ingest.structured_contracts",
            {"name": name, "version": version, "status": CONTRACT_DRAFT,
             "source_ref": _json(source_ref), "mapping": _json(clean),
             "contract_hash": contract_hash(clean, source_ref),
             "note": note, "created_by": actor},
        ))
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.contract.create",
                         "ingest.structured_contracts", cid,
                         {"name": name, "version": version})
    return {"id": cid, "name": name, "version": version, "status": CONTRACT_DRAFT}


def publish_contract(contract_id: str, actor: str) -> dict:
    """单事务发布：draft → published，同名其它 published → deprecated。"""
    ensure_schema()
    with db.transaction():
        row = db.fetch_one(
            "SELECT id, name, version, status FROM ingest.structured_contracts "
            "WHERE id = CAST(:i AS uuid) FOR UPDATE", {"i": contract_id}
        )
        if not row:
            return {"error": "not_found", "errors": ["契约不存在"]}
        if row["status"] != CONTRACT_DRAFT:
            return {"error": "not_draft",
                    "errors": [f"只有 draft 可发布，当前为 {row['status']}；修改请新建版本"]}
        db.execute(
            "UPDATE ingest.structured_contracts SET status = :d, updated_at = NOW() "
            "WHERE name = :n AND status = :p AND id <> CAST(:i AS uuid)",
            {"d": CONTRACT_DEPRECATED, "p": CONTRACT_PUBLISHED,
             "n": row["name"], "i": contract_id},
        )
        db.execute(
            "UPDATE ingest.structured_contracts SET status = :p, published_by = :a, "
            "published_at = NOW(), updated_at = NOW() WHERE id = CAST(:i AS uuid)",
            {"p": CONTRACT_PUBLISHED, "a": actor, "i": contract_id},
        )
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.contract.publish",
                         "ingest.structured_contracts", contract_id,
                         {"name": row["name"], "version": row["version"]})
    return {"id": contract_id, "name": row["name"], "version": row["version"],
            "status": CONTRACT_PUBLISHED, "published_by": actor}


def delete_contract(contract_id: str, actor: str, purge: bool = False) -> dict | None:
    """删除契约：没有任何同步留痕才物理删除，有留痕一律降级为标记删除。

    留痕（structured_sync_runs / structured_anomalies）是「这批事实由哪套规则产生」
    的唯一线索，契约行被物理删掉后历史批次就无法复盘；因此：
    * 零留痕 → DELETE 行（真删除，用于清掉试探性的废弃草稿）；
    * 有留痕 → status=archived（从列表与下拉消失，行与留痕都保留，可按 id 追溯）。
    显式要求 purge 但存在留痕时直接报错，不做「看起来删了其实只是停了」的静默降级。
    已写入的事实不受影响：它们带的是批次 / 契约快照，不依赖契约行是否存在。
    """
    ensure_schema()
    row = db.fetch_one(
        "SELECT c.id, c.name, c.version, c.status, "
        "  (SELECT count(*) FROM ingest.structured_sync_runs r "
        "     WHERE r.contract_id = c.id) AS sync_runs, "
        "  (SELECT count(*) FROM ingest.structured_anomalies a "
        "     WHERE a.contract_id = c.id) AS anomalies "
        "FROM ingest.structured_contracts c WHERE c.id = CAST(:i AS uuid)",
        {"i": contract_id},
    )
    if not row:
        return None
    runs = int(row.get("sync_runs") or 0)
    anomalies = int(row.get("anomalies") or 0)
    if runs or anomalies:
        if purge:
            raise ValueError(
                f"该契约有 {runs} 次同步留痕、{anomalies} 条异常记录，不能物理删除；"
                "请用标记删除（从列表移除，留痕保留）"
            )
        mode = CONTRACT_ARCHIVED
        db.execute(
            "UPDATE ingest.structured_contracts SET status = :s, updated_at = NOW() "
            "WHERE id = CAST(:i AS uuid)",
            {"s": CONTRACT_ARCHIVED, "i": contract_id},
        )
    else:
        mode = "purged"
        db.execute(
            "DELETE FROM ingest.structured_contracts WHERE id = CAST(:i AS uuid)",
            {"i": contract_id},
        )
    from app.modules.audit import service as audit
    audit.log_access(actor, "structured.contract.delete",
                     "ingest.structured_contracts", contract_id,
                     {"name": row["name"], "version": row["version"],
                      "mode": mode, "previous_status": row["status"],
                      "sync_runs": runs, "anomalies": anomalies})
    return {"deleted": True, "mode": mode, "id": contract_id,
            "name": row["name"], "version": row["version"],
            "previous_status": row["status"],
            "sync_runs": runs, "anomalies": anomalies}


def list_contracts(limit: int = 50, include_ephemeral: bool = False,
                   include_archived: bool = False) -> list[dict]:
    """默认不列 ephemeral / archived：前者是自动规则快照，后者是已删除的契约。"""
    ensure_schema()
    conds = []
    if not include_ephemeral:
        conds.append("c.status <> 'ephemeral'")
    if not include_archived:
        conds.append("c.status <> 'archived'")
    where = ("WHERE " + " AND ".join(conds) + " ") if conds else ""
    rows = db.fetch_all(
        "SELECT c.id, c.name, c.version, c.status, c.source_ref, c.mapping, "
        "c.contract_hash, c.note, c.created_by, c.published_by, c.published_at, "
        "c.created_at, "
        "  (SELECT count(*) FROM ingest.structured_sync_runs r "
        "     WHERE r.contract_id = c.id) AS sync_runs, "
        "  (SELECT count(*) FROM ingest.structured_anomalies a "
        "     WHERE a.contract_id = c.id) AS anomalies "
        f"FROM ingest.structured_contracts c {where}"
        "ORDER BY c.name, c.version DESC LIMIT :l",
        {"l": max(1, min(int(limit or 50), 200))},
    )
    for r in rows:
        r["id"] = str(r["id"])
        for k in ("published_at", "created_at"):
            r[k] = str(r[k] or "")[:19]
    return rows


def get_contract(contract_id: str, require_published: bool = False) -> dict | None:
    ensure_schema()
    row = db.fetch_one(
        "SELECT * FROM ingest.structured_contracts WHERE id = CAST(:i AS uuid)",
        {"i": contract_id},
    )
    if not row:
        return None
    row["id"] = str(row["id"])
    for k in ("published_at", "created_at", "updated_at"):
        row[k] = str(row.get(k) or "")[:19]
    if require_published and row["status"] != CONTRACT_PUBLISHED:
        return None
    return row


def published_contract_or_error(contract_id: str) -> tuple[dict | None, dict | None]:
    row = get_contract(contract_id)
    if not row:
        return None, {"error": "not_found", "errors": ["契约不存在"]}
    if row["status"] != CONTRACT_PUBLISHED:
        return None, {"error": "contract_not_published",
                      "errors": ["映射契约未经本人审核发布，禁止写入任何事实（ADR-003 等价闸门）"]}
    return row, None


# ---------------------------------------------------------------- 一次性导入的规则快照
#
# 第一性原理：闸门要审的是「规则」，而一次性导入时人已经在 preview 里看过
# 「这批值会写成什么」——规则的效果被完整包含在被审对象里。此时再强制先发布
# 一份契约，是同一个判断做两遍。因此一次性导入改为：
#   读取数据 → 自动推导建议 → 人确认（等价于审核规则+值）→ 写入
# 规则本身仍以 ephemeral 契约落库，作为事实的追溯锚点，绝不静默丢弃。
#
# ephemeral 的哈希只由 mapping 决定（source_ref 固定为 {"system": "file"}），
# 因此同一套规则反复导入只落一条记录；具体来源由 facts.source_ref 的
# file_name / file_sha256 / upload_id 精确承担，不会混淆。

_EPHEMERAL_SOURCE = {"system": "file"}


def ephemeral_key(mapping: dict) -> str:
    return contract_hash(mapping, _EPHEMERAL_SOURCE)


def get_or_create_ephemeral(*, mapping: dict, actor: str,
                            note: str = "") -> dict:
    """按 mapping 内容幂等复用：同一套规则只保留一条 ephemeral 记录。"""
    ensure_schema()
    errors, clean = validate_mapping(mapping)
    if errors:
        return {"error": "invalid_mapping", "errors": errors}
    if not clean.get("fields"):
        return {"error": "empty_mapping",
                "errors": ["没有任何列被确认入库；请至少给一列指定谓词"]}
    key = ephemeral_key(clean)
    with db.transaction():
        row = db.fetch_one(
            "SELECT * FROM ingest.structured_contracts "
            "WHERE status = :s AND contract_hash = :h ORDER BY created_at LIMIT 1",
            {"s": CONTRACT_EPHEMERAL, "h": key},
        )
        if row:
            row["id"] = str(row["id"])
            for k in ("published_at", "created_at", "updated_at"):
                row[k] = str(row.get(k) or "")[:19]
            return row
        name = f"adhoc_{key[:12]}"
        version = 1
        cid = str(db.insert_returning_id(
            "ingest.structured_contracts",
            {"name": name, "version": version, "status": CONTRACT_EPHEMERAL,
             "source_ref": _json(_EPHEMERAL_SOURCE), "mapping": _json(clean),
             "contract_hash": key, "note": note or "一次性导入的规则快照（自动生成）",
             "created_by": actor, "published_by": actor, "published_at": None},
        ))
        from app.modules.audit import service as audit
        audit.log_access(actor, "structured.contract.ephemeral",
                         "ingest.structured_contracts", cid,
                         {"name": name, "columns": len(clean["fields"])})
    return {"id": cid, "name": name, "version": version, "status": CONTRACT_EPHEMERAL,
            "source_ref": _EPHEMERAL_SOURCE, "mapping": clean, "contract_hash": key,
            "note": note or "一次性导入的规则快照（自动生成）"}
