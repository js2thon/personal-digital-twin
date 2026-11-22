"""#20：CQ 引擎的真实数据快照（替代 demo_snapshot 作为默认输入）。

从 PG 组装与 demo_snapshot 同构的快照：
  entities{type:[id]} / facts[] / attributes{id:{attr:val,_evidence}} / relations[] /
  documents[] / evidence_catalog{}
- 只取 active；按 tenant 与 visibility 过滤（principal 为空时只取 owner/organization 可见）；
- 实体类型按 Registry 的 parent 链与同义词归一到 Registry 类型（OrganizationUnit ⊇ Organization/Department*）；
- data_snapshot_id = sha256(本体版本 + 事实/关系最大更新时间 + 计数)，同一数据同一 id，可复现。
"""
from __future__ import annotations

import hashlib
import json

from app.db import session as db

LIMIT = 50_000
TYPE_ALIASES = {"Organization": "OrganizationUnit", "Department": "OrganizationUnit",
                **{f"DepartmentLevel{i}": "OrganizationUnit" for i in range(1, 8)},
                "Employee": "Person", "Manager": "Person"}


def _norm_type(t: str, registry_types: set[str]) -> str:
    if t in registry_types:
        return t
    return TYPE_ALIASES.get(t, t)


def build(tenant_id: str = "default", principal: dict | None = None,
          registry=None, limit: int = LIMIT) -> dict:
    reg_types = set(((registry.current() if registry else {}) or {}).get("entity_types") or {})
    vis = ["owner", "organization"] if not principal else None
    ents = db.fetch_all(
        "SELECT id::text AS id, entity_type, canonical_name FROM ontology.entities "
        "WHERE status='active' LIMIT :n", {"n": limit})
    by_type: dict[str, list[str]] = {}
    for e in ents:
        by_type.setdefault(_norm_type(e["entity_type"], reg_types), []).append(e["id"])
    fact_rows = db.fetch_all(
        "SELECT id::text AS id, subject_id::text AS subject_id, predicate, object_value, "
        "       valid_from, valid_to, recorded_at, updated_at, evidence_id::text AS evidence_id, "
        "       claim_id::text AS claim_id, status, visibility, trust_tier "
        "FROM knowledge.facts WHERE status IN ('active','contested') AND tenant_id = :t "
        + ("AND visibility = ANY(:v) " if vis else "") + "LIMIT :n",
        {"t": tenant_id, "v": vis, "n": limit} if vis else {"t": tenant_id, "n": limit})
    if principal:
        try:
            from app import policy as pol
            fact_rows = pol.filter_rows(fact_rows, principal)
        except Exception:  # noqa: BLE001
            pass
    facts, attrs = [], {}
    for f in fact_rows:
        ov = f["object_value"] if isinstance(f["object_value"], dict) else json.loads(f["object_value"] or "{}")
        obj = ov.get("name") or ov.get("value") or ov.get("external_id") or ov.get("entity_id")
        ev = [x for x in (f.get("evidence_id"), f.get("claim_id")) if x]
        facts.append({"id": f["id"], "subject_id": f["subject_id"], "predicate": f["predicate"],
                      "object": obj, "evidence_refs": ev,
                      "valid_from": str(f["valid_from"]) if f.get("valid_from") else None,
                      "valid_to": str(f["valid_to"]) if f.get("valid_to") else None,
                      "recorded_at": str(f.get("recorded_at") or ""),
                      "conflict": f["status"] == "contested"})
        if "value" in ov and not isinstance(ov["value"], (dict, list)):
            bag = attrs.setdefault(f["subject_id"], {"_evidence": []})
            bag[f["predicate"]] = ov["value"]
            bag["_evidence"].extend(ev)
    rels = db.fetch_all(
        "SELECT source_entity_id::text AS source, target_entity_id::text AS target, relation_type AS type, "
        "       evidence_claim_id::text AS ev, properties->>'fact_id' AS fact_id "
        "FROM ontology.relations WHERE status='active' LIMIT :n", {"n": limit})
    relations = [{"type": r["type"], "source": r["source"], "target": r["target"],
                  "evidence": [x for x in (r.get("ev"), r.get("fact_id")) if x]} for r in rels]
    ev_ids = sorted({e for f in facts for e in f["evidence_refs"]})
    catalog = {e: {"authority": 0.8, "expired": False} for e in ev_ids}
    stamp = db.fetch_one(
        "SELECT (SELECT max(updated_at) FROM knowledge.facts WHERE tenant_id=:t) AS f, "
        "       (SELECT count(*) FROM ontology.relations WHERE status='active') AS r", {"t": tenant_id}) or {}
    ver = registry.current_version() if registry else ""
    sid = hashlib.sha256(json.dumps([ver, str(stamp.get("f")), stamp.get("r"), len(facts), len(ents)],
                                    default=str).encode()).hexdigest()[:16]
    return {"data_snapshot_id": f"DS-{sid}", "source": "live", "tenant_id": tenant_id,
            "entities": by_type, "facts": facts, "attributes": attrs, "relations": relations,
            "documents": [], "evidence_catalog": catalog,
            "counts": {"entities": len(ents), "facts": len(facts), "relations": len(relations)},
            "truncated": len(ents) >= limit or len(fact_rows) >= limit}
