"""本体工作台的落库层：事实→关系对账、规则校验、模式视图、实体/关系编辑。

与 `graph.py`（只读查询）的分工：
- `graph.py` 回答「图长什么样」，本模块回答「图该是什么样、怎么改」。
- 所有写操作都走同一个入口校验（`validate_relation`），避免「页面拦住了、
  接口没拦住」这种双标。
"""
from __future__ import annotations

import json

from app.db import session as db
from app.modules.ontology import quality
from app.modules.ontology import projection as proj
from app.modules.ontology.graph import RELATION_TYPES, classify

# 与 DDL 中 ontology.entities 的类型域保持一致（发布版本体里的 entities 并集）
DEFAULT_ENTITY_TYPES = {
    "Person", "Organization", "Project", "Role", "System", "Component",
    "Document", "Event", "Concept", "Rule", "Skill", "Policy", "Process",
    "Decision", "Criterion", "Option", "Belief", "Preference", "Value",
    "Principle", "Domain", "Knowledge", "Claim",
}


class WorkbenchError(Exception):
    """业务校验失败。API 层转 400/409，带结构化原因。"""

    def __init__(self, message: str, code: str = "invalid", detail: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


# ---------------------------------------------------------------- 读取
def _load_entities(active_only: bool = False) -> list[dict]:
    sql = ("SELECT id, canonical_name, entity_type, aliases, status, merged_into, "
           "       properties, created_at FROM ontology.entities ")
    if active_only:
        sql += "WHERE status = 'active' "
    sql += "ORDER BY canonical_name LIMIT 20000"
    return db.fetch_all(sql)


def _load_relations(active_only: bool = False) -> list[dict]:
    sql = ("SELECT r.id, r.source_entity_id, r.target_entity_id, r.relation_type, "
           "       r.evidence_claim_id, r.status, r.properties, r.valid_from, "
           "       s.canonical_name AS source_name, s.entity_type AS source_type, "
           "       s.status AS source_status, "
           "       t.canonical_name AS target_name, t.entity_type AS target_type, "
           "       t.status AS target_status, c.status AS claim_status "
           "FROM ontology.relations r "
           "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
           "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
           "LEFT JOIN knowledge.claims c ON c.id = r.evidence_claim_id ")
    if active_only:
        sql += "WHERE r.status = 'active' "
    sql += "ORDER BY r.id LIMIT 50000"
    return db.fetch_all(sql)


def _load_active_facts() -> list[dict]:
    return db.fetch_all(
        "SELECT id, claim_id, subject_id, predicate, object_value, valid_from, trust_tier "
        "FROM knowledge.facts WHERE status = 'active' LIMIT 50000"
    )


# ---------------------------------------------------------------- 事实→关系对账
def relation_origin(rel: dict) -> str:
    """边归属：manual（人工）/ projection（事实投影）/ legacy（历史抽取）。"""
    props = rel.get("properties") or {}
    origin = props.get("origin") if isinstance(props, dict) else None
    if origin == "manual":
        return "manual"
    if origin == "fact_projection":
        return "projection"
    return "legacy"


def legacy_problem(rel: dict) -> str | None:
    """历史边是否「可证明是错的」。只有能说清的错才自动删。

    判据只有三条，宁可留着可疑边让人复核，也不做批量清洗：
    1. 自环；
    2. 两端类型不符合关系目录约束；
    3. 证据 Claim 已被拒绝，或指向不存在的 Claim。
    """
    if rel.get("source_entity_id") == rel.get("target_entity_id"):
        return "自环"
    spec = RELATION_TYPES.get(rel.get("relation_type") or "")
    if not spec:
        return None                                  # 未登记类型不在「可证明错」之列
    if (rel.get("source_type") and "*" not in spec["source"]
            and rel["source_type"] not in spec["source"]):
        return f"源类型 {rel['source_type']} 不在约束内"
    if (rel.get("target_type") and "*" not in spec["target"]
            and rel["target_type"] not in spec["target"]):
        return f"目标类型 {rel['target_type']} 不在约束内"
    if rel.get("evidence_claim_id") and (rel.get("claim_status") or "") == "rejected":
        return "证据 Claim 已被拒绝"
    if rel.get("evidence_claim_id") and rel.get("claim_status") is None:
        return "证据 Claim 不存在"
    return None


def reconcile_relations(dry_run: bool = True) -> dict:
    """用活跃事实对齐图上的边。默认 dry-run，只报告不动手。

    三类边区别对待，这是本函数最重要的语义：

    - `projection`：事实投影产物，事实变了它就跟着增删；
    - `legacy`：历史抽取产物。**只删「可证明是错的」**（自环、类型不符、
      证据被拒），其余保留并列为待复核——批量清洗会误杀；
    - `manual`：人工建的边，永远不动。
    """
    entities = _load_entities(active_only=True)
    facts = _load_active_facts()
    result = proj.project_facts(facts, entities)
    desired = result["edges"]

    existing = [r for r in _load_relations() if (r.get("status") or "active") == "active"]
    bucket: dict[str, list[dict]] = {"manual": [], "projection": [], "legacy": []}
    for r in existing:
        bucket[relation_origin(r)].append(r)

    # 比对基准必须是**全部**活跃边，而不只是投影边：
    # 人工边（已被回写成事实）和仍合规的历史边也在图上，按投影桶去比会把
    # 「图上已有的边」误判成缺的一条，于是每次对账都重复插一条同三元组的边。
    plan = proj.diff(existing, desired)
    # 但「该删」只能落在投影桶上：人工边归人管，历史边只做可证明错的清理。
    desired_keys = {(str(e["source_entity_id"]), e["relation_type"],
                     str(e["target_entity_id"])) for e in desired}
    plan["to_remove"] = [
        r for r in bucket["projection"]
        if (str(r["source_entity_id"]), r["relation_type"],
            str(r["target_entity_id"])) not in desired_keys
    ]
    plan["stats"]["remove"] = len(plan["to_remove"])

    legacy_bad = []
    for r in bucket["legacy"]:
        why = legacy_problem(r)
        if why:
            legacy_bad.append({"relation_id": str(r["id"]), "source": r.get("source_name"),
                               "type": r["relation_type"], "target": r.get("target_name"),
                               "why": why})
    legacy_kept = len(bucket["legacy"]) - len(legacy_bad)

    removed = [{"relation_id": str(r["id"]), "source": r.get("source_name"),
                "type": r["relation_type"], "target": r.get("target_name"),
                "why": "事实已不再支持这条边"}
               for r in plan["to_remove"]] + legacy_bad
    added = [{"source": e["source_name"], "type": e["relation_type"],
              "target": e["target_name"], "target_type": e["target_type"]}
             for e in plan["to_add"]]

    if not dry_run:
        with db.transaction():
            to_delete = [r["id"] for r in plan["to_remove"]] + [x["relation_id"] for x in legacy_bad]
            if to_delete:
                db.execute(
                    "DELETE FROM ontology.relations WHERE id = ANY(CAST(:ids AS uuid[]))",
                    {"ids": "{" + ",".join(str(i) for i in to_delete) + "}"},
                )
            for e in plan["to_add"]:
                db.execute(
                    "INSERT INTO ontology.relations "
                    "(source_entity_id, relation_type, target_entity_id, evidence_claim_id, "
                    " status, valid_from, properties) "
                    "VALUES (CAST(:s AS uuid), :rt, CAST(:t AS uuid), CAST(:c AS uuid), "
                    "        'active', CAST(:vf AS timestamptz), CAST(:p AS jsonb))",
                    {"s": e["source_entity_id"], "rt": e["relation_type"],
                     "t": e["target_entity_id"], "c": e.get("evidence_claim_id"),
                     "vf": e.get("valid_from"), "p": json.dumps(
                         {"origin": "fact_projection", "predicate": e["predicate"],
                          "fact_id": e["fact_id"]}, ensure_ascii=False)},
                )

    return {
        "dry_run": dry_run,
        "applied": not dry_run,
        "projection": result["stats"],
        "plan": plan["stats"],
        "existing": {"projection": len(bucket["projection"]),
                     "legacy": len(bucket["legacy"]),
                     "legacy_kept": legacy_kept,
                     "manual": len(bucket["manual"])},
        "to_add": len(added),
        "to_remove": len(removed),
        "add_samples": added[:20],
        "remove_samples": removed[:20],
        "skipped_samples": result["skipped"][:10],
    }


def relation_drift() -> dict:
    """漂移视图：只算差集，不落库。用于页面上的「待同步 N 条」提示。"""
    r = reconcile_relations(dry_run=True)
    r.pop("skipped_samples", None)
    return r


# ---------------------------------------------------------------- 模式层视图
def _published_entity_types() -> dict[str, list[str]]:
    """发布版本体里的实体类型 → 所属域。取不到就退回内置类型集。"""
    row = db.fetch_one(
        "SELECT schema_content FROM ontology.ontology_versions "
        "ORDER BY version DESC LIMIT 1"
    ) if _table_exists("ontology", "ontology_versions") else None
    out: dict[str, list[str]] = {}
    content = (row or {}).get("schema_content") or {}
    domains = content.get("domains") if isinstance(content, dict) else None
    if isinstance(domains, dict):
        for domain, spec in domains.items():
            for t in (spec or {}).get("entities") or []:
                out.setdefault(t, []).append(domain)
    return out or {t: ["(内置)"] for t in sorted(DEFAULT_ENTITY_TYPES)}


def _table_exists(schema: str, table: str) -> bool:
    row = db.fetch_one(
        "SELECT 1 AS ok FROM information_schema.tables "
        "WHERE table_schema = :s AND table_name = :t", {"s": schema, "t": table}
    )
    return bool(row)


def schema_graph() -> dict:
    """模式层：实体类型节点 + 关系类型允许的类型对边（带实际用量）。"""
    types = _published_entity_types()
    counts = {(r["entity_type"]): int(r["n"]) for r in db.fetch_all(
        "SELECT entity_type, count(*) AS n FROM ontology.entities "
        "WHERE status = 'active' GROUP BY 1")}

    usage = db.fetch_all(
        "SELECT r.relation_type, s.entity_type AS st, t.entity_type AS tt, count(*) AS n "
        "FROM ontology.relations r "
        "JOIN ontology.entities s ON s.id = r.source_entity_id AND s.status = 'active' "
        "JOIN ontology.entities t ON t.id = r.target_entity_id AND t.status = 'active' "
        "WHERE r.status = 'active' GROUP BY 1, 2, 3"
    )
    used = {(r["relation_type"], r["st"], r["tt"]): int(r["n"]) for r in usage}
    per_type: dict[str, int] = {}
    for (rt, st, tt), n in used.items():
        per_type[rt] = per_type.get(rt, 0) + n

    nodes = [{"type": t, "domains": d, "entities": counts.get(t, 0),
              "declared": True} for t, d in sorted(types.items())]
    for t, n in counts.items():
        if t not in types:
            nodes.append({"type": t, "domains": ["(未定义)"], "entities": n,
                          "declared": False})

    edges = []
    for rt, spec in RELATION_TYPES.items():
        for st in spec["source"]:
            for tt in spec["target"]:
                if "*" in (st, tt):
                    continue                        # 无约束的关系在模式图上不画
                edges.append({"relation": rt, "label": spec["label"],
                              "family": spec["family"], "source": st, "target": tt,
                              "cardinality": spec.get("cardinality", "many"),
                              "used": used.get((rt, st, tt), 0)})
    wildcard = [{"relation": rt, "label": spec["label"], "family": spec["family"],
                 "cardinality": spec.get("cardinality", "many"), "used": per_type.get(rt, 0)}
                for rt, spec in RELATION_TYPES.items()
                if "*" in spec["source"] or "*" in spec["target"]]
    unregistered = [{"relation": rt, "used": n} for rt, n in per_type.items()
                    if rt not in RELATION_TYPES]
    return {
        "nodes": nodes,
        "edges": edges,
        "wildcard_relations": wildcard,
        "unregistered": unregistered,
        "summary": {
            "types": len(nodes),
            "declared_types": len([n for n in nodes if n["declared"]]),
            "relation_types": len(RELATION_TYPES),
            "typed_pairs": len(edges),
            "used_pairs": len([e for e in edges if e["used"]]),
            "wildcard": len(wildcard),
            "unregistered_in_use": len(unregistered),
        },
    }


# ---------------------------------------------------------------- 规则校验
def validate() -> dict:
    """对真实数据跑规则，并附上「规则引擎自测」结论 —— 让用户知道尺子本身是准的。"""
    entities = _load_entities()
    relations = _load_relations()
    facts = _load_active_facts()
    result = quality.run_rules(entities, relations, facts, DEFAULT_ENTITY_TYPES)
    result["selftest"] = quality.selftest()
    return result


# ---------------------------------------------------------------- 写操作校验
def validate_relation(source_id: str, relation_type: str, target_id: str) -> dict:
    """建边前的统一校验。返回两端实体信息，失败抛 WorkbenchError。"""
    if source_id == target_id:
        raise WorkbenchError("源和目标不能是同一实体（自环）", "self_loop")
    rows = db.fetch_all(
        "SELECT id, canonical_name, entity_type, status FROM ontology.entities "
        "WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": "{" + source_id + "," + target_id + "}"},
    )
    by_id = {str(r["id"]): r for r in rows}
    src, dst = by_id.get(source_id), by_id.get(target_id)
    if not src or not dst:
        raise WorkbenchError("源或目标实体不存在", "not_found")
    if (src.get("status") or "active") != "active" or (dst.get("status") or "active") != "active":
        raise WorkbenchError("源或目标实体已合并/废弃，不能建边", "inactive_entity")

    spec = RELATION_TYPES.get(relation_type)
    if not spec:
        raise WorkbenchError(
            f"关系类型 {relation_type} 未在本体目录登记", "unregistered_type",
            {"registered": sorted(RELATION_TYPES)})
    if "*" not in spec["source"] and src["entity_type"] not in spec["source"]:
        raise WorkbenchError(
            f"{relation_type} 的源必须是 {'/'.join(spec['source'])}，"
            f"而 {src['canonical_name']} 是 {src['entity_type']}", "type_mismatch")
    if "*" not in spec["target"] and dst["entity_type"] not in spec["target"]:
        raise WorkbenchError(
            f"{relation_type} 的目标必须是 {'/'.join(spec['target'])}，"
            f"而 {dst['canonical_name']} 是 {dst['entity_type']}", "type_mismatch")

    dup = db.fetch_one(
        "SELECT id FROM ontology.relations WHERE source_entity_id = CAST(:s AS uuid) "
        "AND target_entity_id = CAST(:t AS uuid) AND relation_type = :rt "
        "AND status = 'active' LIMIT 1",
        {"s": source_id, "t": target_id, "rt": relation_type})
    if dup:
        raise WorkbenchError("这条关系已存在", "duplicate",
                             {"relation_id": str(dup["id"])})

    if spec.get("cardinality") == "one":
        other = db.fetch_one(
            "SELECT r.id, e.canonical_name FROM ontology.relations r "
            "JOIN ontology.entities e ON e.id = r.target_entity_id "
            "WHERE r.source_entity_id = CAST(:s AS uuid) AND r.relation_type = :rt "
            "AND r.status = 'active' LIMIT 1",
            {"s": source_id, "rt": relation_type})
        if other:
            raise WorkbenchError(
                f"{relation_type} 是单值关系，{src['canonical_name']} 已有指向"
                f"「{other['canonical_name']}」的边；请先删除旧边或改用多值关系",
                "cardinality_conflict", {"existing": str(other["id"])})
    return {"source": src, "target": dst, "spec": spec}


def create_relation(source_id: str, relation_type: str, target_id: str,
                    note: str | None = None) -> dict:
    info = validate_relation(source_id, relation_type, target_id)
    rel_id = db.insert_returning_id(
        "ontology.relations",
        {"source_entity_id": source_id, "relation_type": relation_type,
         "target_entity_id": target_id, "status": "active",
         "properties": json.dumps({"origin": "manual", "note": note or ""},
                                  ensure_ascii=False)},
    )
    # R3：图上新建的边必须同时成为一条本体事实，否则「图上有的、本体没有」。
    # 补事实失败不回滚建边（用户要的边已经建成），但把原因带回给调用方。
    backing = {}
    try:
        from app.modules.ontology import sync as graph_sync
        backing = graph_sync.ensure_backing_fact(str(rel_id))
    except Exception as e:                     # noqa: BLE001
        backing = {"created": False, "reason": f"backing_fact_failed: {e}"}
    return {"id": str(rel_id), "source": info["source"]["canonical_name"],
            "relation": relation_type, "target": info["target"]["canonical_name"],
            "label": info["spec"]["label"], "backing_fact": backing}


def delete_relation(relation_id: str) -> dict:
    row = db.fetch_one(
        "SELECT r.id, r.properties, r.source_entity_id, r.target_entity_id, "
        "       r.relation_type, s.canonical_name AS source_name, "
        "       t.canonical_name AS target_name "
        "FROM ontology.relations r "
        "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
        "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
        "WHERE r.id = CAST(:i AS uuid)", {"i": relation_id})
    if not row:
        raise WorkbenchError("关系不存在", "not_found")
    # 软删：保留行，便于审计与「为什么少了一条边」的追问
    db.execute(
        "UPDATE ontology.relations SET status = 'removed', "
        "  properties = COALESCE(properties, '{}'::jsonb) || '{\"removed_by\":\"owner\"}'::jsonb "
        "WHERE id = CAST(:i AS uuid)", {"i": relation_id})
    # R4：边背后的事实要一起撤，否则下一轮对账会把这条边重建出来
    sync_out = {}
    try:
        from app.modules.ontology import sync as graph_sync
        sync_out = graph_sync.on_relation_removed(row)
    except Exception as e:                     # noqa: BLE001
        sync_out = {"withdrawn": False, "reason": f"fact_withdraw_failed: {e}"}
    return {"id": str(row["id"]), "source": row.get("source_name"),
            "relation": row["relation_type"], "target": row.get("target_name"),
            "backing_fact": sync_out}


def update_entity(entity_id: str, canonical_name: str | None = None,
                  entity_type: str | None = None,
                  aliases: list[str] | None = None) -> dict:
    cur = db.fetch_one(
        "SELECT id, canonical_name, entity_type, aliases, status FROM ontology.entities "
        "WHERE id = CAST(:i AS uuid)", {"i": entity_id})
    if not cur:
        raise WorkbenchError("实体不存在", "not_found")
    new_name = (canonical_name if canonical_name is not None
                else cur["canonical_name"] or "").strip()
    new_type = (entity_type if entity_type is not None
                else cur["entity_type"] or "").strip()
    if not new_name:
        raise WorkbenchError("实体名不能为空", "empty_name")
    if new_type not in DEFAULT_ENTITY_TYPES:
        raise WorkbenchError(f"实体类型 {new_type} 不在本体定义内", "bad_type",
                             {"allowed": sorted(DEFAULT_ENTITY_TYPES)})
    clash = db.fetch_one(
        "SELECT id FROM ontology.entities WHERE canonical_name = :n "
        "AND entity_type = :t AND status = 'active' AND id <> CAST(:i AS uuid) LIMIT 1",
        {"n": new_name, "t": new_type, "i": entity_id})
    if clash:
        raise WorkbenchError(f"已存在同名同类型实体 {new_name}，请改用「合并」而不是改名",
                             "name_conflict", {"entity_id": str(clash["id"])})
    merged_aliases = cur.get("aliases") or []
    if aliases is not None:
        merged_aliases = [a.strip() for a in aliases if a and a.strip()]
    if new_name != cur["canonical_name"] and cur["canonical_name"] not in merged_aliases:
        merged_aliases.append(cur["canonical_name"])      # 旧名保留为别名，检索不断链
    db.execute(
        "UPDATE ontology.entities SET canonical_name = :n, entity_type = :t, "
        "  aliases = CAST(:a AS jsonb) WHERE id = CAST(:i AS uuid)",
        {"n": new_name, "t": new_type, "a": json.dumps(merged_aliases, ensure_ascii=False),
         "i": entity_id})
    # 实体改名/改类型后，以名字为宾语的事实要重投影（旧边可能再也映射不回来）
    sync_out = {}
    if new_name != cur["canonical_name"] or new_type != cur["entity_type"]:
        try:
            from app.modules.ontology import sync as graph_sync
            sync_out = graph_sync.sync_entity(entity_id)
        except Exception as e:                 # noqa: BLE001
            sync_out = {"error": f"entity_sync_failed: {e}"}
    return {"id": entity_id, "before": {"name": cur["canonical_name"],
                                        "type": cur["entity_type"]},
            "after": {"name": new_name, "type": new_type, "aliases": merged_aliases},
            "sync": sync_out}


def ensure_fact_edge(fact_id: str) -> dict:
    """单条活跃事实 → 一条边。审核通过时即时生效，不必等全量对账。

    实现已收口到 `sync.upsert_fact_edge`：撤销、实体改名等场景要走同一套
    规则，否则「审核建边」和「对账建边」迟早长成两套语义。
    """
    from app.modules.ontology import sync as graph_sync

    return graph_sync.upsert_fact_edge(fact_id)
