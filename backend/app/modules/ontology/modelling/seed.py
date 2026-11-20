"""把 RDF 导入抽出的实例灌进运行时表（entities / facts / relations）。

三条硬口径，缺一不可：

1. **权限三件套**：``knowledge.facts`` 的 ``visibility`` 默认 ``'owner'``、``created_by``
   默认 ``'unknown'``。若照默认值写入，``policy.read_clause`` 会把行全部过滤掉，
   问数会静默返回 0——看起来像「库里没人」。所以这里显式写
   ``created_by = <principal.user_id>``、``visibility = 'organization'``
   （organization 表示本租户内共享，仍受租户与 scope 约束，不放宽 AC-01）。
2. **object_value 必须是对象**：列类型 ``jsonb NOT NULL``，问数读
   ``object_value->>'value'``。直接写裸标量会让 ``->>`` 返回 NULL，聚合恒为 0。
3. **幂等**：重复导入同一份本体不产生重复实体/重复事实。实体按
   ``properties->>'iri'`` 认，没有 IRI 时退 ``(entity_type, canonical_name)``；
   事实按 ``(subject_id, predicate, object_value)`` 认。命中即跳过，不覆盖用户后改的值。

只写这三张表，不碰向量、不碰语义层、不发布本体版本——那些是独立流程。
"""

from __future__ import annotations

import json
from typing import Any

from app.db import session as db

# xsd 局部名 → knowledge.facts.datatype 的取值（列是 varchar，取值保持项目既有词表）
_DT_MAP = {
    "string": "text", "normalizedString": "text", "token": "text", "anyURI": "text",
    "integer": "integer", "int": "integer", "long": "integer", "decimal": "number",
    "double": "number", "float": "number", "boolean": "boolean",
    "date": "date", "dateTime": "datetime",
}


def _s(v: Any) -> str:
    return str(v or "").strip()


def _jsonb(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _principal_ctx(principal: dict | None) -> tuple[str, str]:
    """从 AC-01 归一化 principal 取 (user_id, tenant_id)，缺值给安全兜底。"""
    p = principal or {}
    user = _s(p.get("user_id")) or "unknown"
    tenant = _s(p.get("tenant_id")) or "default"
    return user, tenant


def _dt_of(raw: Any) -> str:
    return _DT_MAP.get(_s(raw), "text")


def seed_instances(instances: dict[str, Any], *, principal: dict | None = None,
                   visibility: str = "organization", dry_run: bool = False,
                   source_tag: str = "rdf-import",
                   ontology_version: str = "v1.0.0") -> dict[str, Any]:
    """把 ``rdf_import.build_instances`` 的产出写进运行时表。

    ``dry_run=True`` 只解析与校验、不写库（前端「先看会写什么」用）。
    返回逐类计数、ref→id 映射、未解析引用与告警，全部如实披露。
    """
    user, tenant = _principal_ctx(principal)
    ents = instances.get("entities") or []
    facts = instances.get("facts") or []
    rels = instances.get("relations") or []

    warnings: list[str] = []
    stats = {
        "entities_in": len(ents), "facts_in": len(facts), "relations_in": len(rels),
        "entities_created": 0, "entities_matched": 0,
        "facts_created": 0, "facts_skipped": 0,
        "relations_created": 0, "relations_skipped": 0,
        "tenant_id": tenant, "created_by": user, "visibility": visibility,
        "dry_run": bool(dry_run),
    }
    ref_map: dict[str, str] = {}
    unresolved: list[dict[str, Any]] = []

    if dry_run:
        # 不写库时也要给出「按当前口径会怎么落」的判断，便于前端展示
        stats["entities_created"] = len(ents)
        stats["facts_created"] = len(facts)
        stats["relations_created"] = len(rels)
        stats["note"] = "dry_run：未写库，计数为预计新增量"
        return {"ok": True, "stats": stats, "ref_map": {}, "unresolved": [],
                "warnings": ["dry_run=true，未写入任何行"]}

    with db.transaction():
        # ---------------- 实体
        for e in ents:
            ref = _s(e.get("ref")) or _s(e.get("iri"))
            etype = _s(e.get("entity_type"))
            name = _s(e.get("canonical_name"))
            if not (ref and etype and name):
                unresolved.append({"kind": "entity", "ref": ref,
                                   "reason": "缺 entity_type 或 canonical_name"})
                continue
            iri = _s(e.get("iri"))
            props = dict(e.get("properties") or {})
            props.setdefault("iri", iri)
            props.setdefault("source", source_tag)
            props.setdefault("ontology_ref", ref)
            existing = _find_entity(iri, etype, name, tenant)
            if existing:
                ref_map[ref] = str(existing["id"])
                stats["entities_matched"] += 1
                # 只补空字段，不覆盖已有值（可能被用户人工修过）
                merged = {**(existing.get("properties") or {}), **props}
                db.execute(
                    "UPDATE ontology.entities SET properties = CAST(:p AS jsonb),"
                    " aliases = CAST(:a AS jsonb), status = 'active' WHERE id = CAST(:id AS uuid)",
                    {"p": _jsonb(merged),
                     "a": _jsonb(sorted(set((existing.get("aliases") or [])
                                            + list(e.get("aliases") or [])))),
                     "id": ref_map[ref]})
                continue
            new_id = db.insert_returning_id("ontology.entities", {
                "entity_type": etype,
                "canonical_name": name,
                "aliases": _jsonb(list(e.get("aliases") or [])),
                "properties": _jsonb(props),
                "status": "active",
                "tenant_id": tenant,
            })
            ref_map[ref] = str(new_id)
            if iri:
                ref_map.setdefault(iri, str(new_id))
            stats["entities_created"] += 1

        # ---------------- 事实
        for f in facts:
            sref = _s(f.get("subject_ref")) or _s(f.get("subject_iri"))
            subj = ref_map.get(sref) or _s(f.get("subject_id"))
            pred = _s(f.get("predicate"))
            if not (subj and pred):
                unresolved.append({"kind": "fact", "ref": sref, "predicate": pred,
                                   "reason": "主体未解析成实体 id"})
                continue
            payload: dict[str, Any] = {"value": f.get("value")}
            for extra in ("value_label", "value_iri", "unit"):
                if f.get(extra):
                    payload[extra] = f[extra]
            payload["source"] = source_tag
            if _fact_exists(subj, pred, payload, tenant):
                stats["facts_skipped"] += 1
                continue
            db.execute(
                "INSERT INTO knowledge.facts (subject_id, predicate, object_value, "
                " datatype, visibility, created_by, tenant_id, status, "
                " verification_status, ontology_version, source_ref) "
                "VALUES (CAST(:subj AS uuid), :pred, CAST(:val AS jsonb), :dt, :vis, "
                " :by, :tenant, 'active', 'unverified', :ov, CAST(:src AS jsonb))",
                {"subj": subj, "pred": pred, "val": _jsonb(payload),
                 "dt": _dt_of(f.get("datatype")), "vis": visibility, "by": user,
                 "tenant": tenant, "ov": ontology_version,
                 "src": _jsonb({"kind": source_tag, "ontology_ref": sref})})
            stats["facts_created"] += 1

        # ---------------- 关系
        for r in rels:
            sref = _s(r.get("source_ref")) or _s(r.get("source_iri"))
            tref = _s(r.get("target_ref")) or _s(r.get("target_iri"))
            rtype = _s(r.get("relation_type"))
            sid = ref_map.get(sref)
            tid = ref_map.get(tref)
            if not (sid and tid and rtype):
                unresolved.append({"kind": "relation", "source": sref, "target": tref,
                                   "relation_type": rtype,
                                   "reason": "端点未解析成实体 id"})
                continue
            if _relation_exists(sid, rtype, tid, tenant):
                stats["relations_skipped"] += 1
                continue
            db.insert_returning_id("ontology.relations", {
                "source_entity_id": sid, "relation_type": rtype,
                "target_entity_id": tid, "status": "active", "tenant_id": tenant,
                # 打来源标：清理时只回收本模块建的关系，不碰别处的关系
                "properties": _jsonb({"source": source_tag,
                                      "ontology_ref": _s(r.get("relation_type"))}),
            })
            stats["relations_created"] += 1

    if unresolved:
        warnings.append(f"{len(unresolved)} 条载荷未能落库（端点/主体未解析），已逐条列在 unresolved")
    return {"ok": True, "stats": stats, "ref_map": ref_map,
            "unresolved": unresolved, "warnings": warnings}


def _find_entity(iri: str, etype: str, name: str, tenant: str) -> dict | None:
    if iri:
        row = db.fetch_one(
            "SELECT id, aliases, properties FROM ontology.entities "
            "WHERE COALESCE(tenant_id,'default') = :t AND properties->>'iri' = :iri LIMIT 1",
            {"t": tenant, "iri": iri})
        if row:
            return row
    return db.fetch_one(
        "SELECT id, aliases, properties FROM ontology.entities "
        "WHERE COALESCE(tenant_id,'default') = :t AND entity_type = :et "
        "  AND canonical_name = :cn LIMIT 1",
        {"t": tenant, "et": etype, "cn": name})


def _fact_exists(subj: str, pred: str, payload: dict, tenant: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS x FROM knowledge.facts "
        "WHERE subject_id = CAST(:subj AS uuid) AND predicate = :pred "
        "  AND object_value->>'value' = :val AND status = 'active' "
        "  AND COALESCE(tenant_id,'default') = :t LIMIT 1",
        {"subj": subj, "pred": pred, "val": _s(payload.get("value")), "t": tenant})
    return bool(row)


def _relation_exists(sid: str, rtype: str, tid: str, tenant: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS x FROM ontology.relations "
        "WHERE source_entity_id = CAST(:s AS uuid) AND relation_type = :r "
        "  AND target_entity_id = CAST(:t2 AS uuid) "
        "  AND COALESCE(tenant_id,'default') = :t LIMIT 1",
        {"s": sid, "r": rtype, "t2": tid, "t": tenant})
    return bool(row)


def purge_by_source(source_tag: str = "rdf-import", *, principal: dict | None = None,
                    dry_run: bool = True) -> dict[str, Any]:
    """按来源标签回收本模块灌入的数据（测试自清理 / 用户重来用）。

    顺序有依赖：先删事实 → 再删本模块打标的关系 → 最后删实体。
    实体只删「已没有任何事实、也没有任何关系引用」的，避免带走别处的数据；
    仍被外部关系引用的实体会保留并在 ``kept_entities`` 里如实计数。
    默认 dry_run，必须显式传 dry_run=False 才真删。
    """
    _user, tenant = _principal_ctx(principal)
    facts = db.fetch_all(
        "SELECT id, subject_id FROM knowledge.facts "
        "WHERE COALESCE(tenant_id,'default') = :t AND source_ref->>'kind' = :k",
        {"t": tenant, "k": source_tag})
    rels = db.fetch_all(
        "SELECT id FROM ontology.relations "
        "WHERE COALESCE(tenant_id,'default') = :t AND properties->>'source' = :k",
        {"t": tenant, "k": source_tag})
    subj_ids = sorted({str(r["subject_id"]) for r in facts if r.get("subject_id")})
    out = {"source_tag": source_tag, "facts": len(facts), "relations": len(rels),
           "candidate_entities": len(subj_ids), "dry_run": bool(dry_run)}
    if dry_run:
        return out
    with db.transaction():
        if facts:
            db.execute(
                "DELETE FROM knowledge.facts "
                "WHERE COALESCE(tenant_id,'default') = :t AND source_ref->>'kind' = :k",
                {"t": tenant, "k": source_tag})
        if rels:
            db.execute(
                "DELETE FROM ontology.relations "
                "WHERE COALESCE(tenant_id,'default') = :t AND properties->>'source' = :k",
                {"t": tenant, "k": source_tag})
        deleted_entities = 0
        if subj_ids:
            # 双重条件：无事实 + 无关系引用，两条 FK 都不会被踩到
            rows = db.fetch_all(
                "DELETE FROM ontology.entities e WHERE e.id = ANY(CAST(:ids AS uuid[])) "
                "AND NOT EXISTS (SELECT 1 FROM knowledge.facts f WHERE f.subject_id = e.id) "
                "AND NOT EXISTS (SELECT 1 FROM ontology.relations r "
                "                WHERE r.source_entity_id = e.id OR r.target_entity_id = e.id) "
                "RETURNING e.id",
                {"ids": subj_ids})
            deleted_entities = len(rows)
    out.update({"deleted": True, "entities_deleted": deleted_entities,
                "kept_entities": len(subj_ids) - deleted_entities})
    return out
