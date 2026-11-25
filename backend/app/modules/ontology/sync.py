"""图（ontology.relations）与本体（entities + facts）的双向同步。

## 为什么需要这一层

上一版只做了「事实 → 边」一个方向（审核通过时建边），于是出现三种对不上账：

1. **撤销事实，边还在**：`withdraw_fact` 只改 facts.status，图上那条边仍 active，
   用户看到「已经撤销的知识还能在图上连线」；
2. **人工删边，下一轮对账复活**：对账按事实重算 desired，删掉的投影边又被加回来；
3. **人工建边，本体里查不到**：`create_relation` 只写 relations，不写 facts，
   于是「图上有的、本体没有」，两套账。

## 口径（四条规则，全部收口在本模块）

- 真源：`ontology.entities` + `knowledge.facts`；派生：`ontology.relations`。
- R1 事实 active → 有且只有一条投影边（`upsert_fact_edge`）；
- R2 事实撤销/失效/删除 → 投影边跟着**软停用**（留痕，不物理删）；
- R3 人工建边 → 同步补一条本体事实，`object_value` 用**实体 id** 而非名字，
  这样改名、重名都不会让对账把它算成另一条边；
- R4 人工删边 → 连带**撤销**它背后的事实，否则 R1 的对账必然把它复活。

收敛不了的（类型不符、值匹配不到实体）不静默改写，全部进 `orphans()` /
`drift()`，由 `/ontology/sync` 显式对齐再落库。
"""
from __future__ import annotations

import json
import threading

from app.db import session as db
from app.modules.ontology import projection as proj

# #1：库层唯一约束才是防重复的真正保障（进程锁只在单进程内有效，多 worker / 多实例挡不住）。
# 已有重复边时建索引会失败：先把重复的 active 边软停用（保留最早一条），再建部分唯一索引。
_UNIQUE_DDL = (
    "WITH d AS (SELECT id, row_number() OVER (PARTITION BY source_entity_id, relation_type, "
    " target_entity_id ORDER BY id) AS rn FROM ontology.relations WHERE status = 'active') "
    "UPDATE ontology.relations r SET status = 'removed', properties = COALESCE(r.properties, "
    " '{}'::jsonb) || '{\"removed_by\": \"dedupe_unique_index\"}'::jsonb FROM d "
    "WHERE r.id = d.id AND d.rn > 1",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_relations_active_triple ON ontology.relations "
    "(source_entity_id, relation_type, target_entity_id) WHERE status = 'active'",
)
_unique_ready = False


def ensure_unique_index() -> bool:
    """幂等：启动时调用一次（main.py startup，早于 outbox/fact_events）。"""
    global _unique_ready
    if _unique_ready:
        return True
    for ddl in _UNIQUE_DDL:
        db.execute(ddl)
    _unique_ready = True
    return True


# 所有「事实 → 边」的写入口共用一把可重入锁：API 即时钩子、事件监听线程、
# 手动/启动对账可能同时落库，relations 上没有唯一约束，并发会插出重复边。
# 用 RLock 是因为 sync_fact 内部还会调用同模块的 upsert/remove。
SYNC_LOCK = threading.RLock()

# 人工边回写事实时打的来源标记（`knowledge.facts.source_ref.origin`）
MANUAL_ORIGIN = "manual_relation"
# 投影边停用时写进 properties 的标记，用于区分「事实没了」和「人工删了」
REMOVED_BY = "relation_sync"


# ---------------------------------------------------------------- 小工具
def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _active_fact(fact_id: str) -> dict | None:
    return db.fetch_one(
        "SELECT id, claim_id, subject_id, predicate, object_value, valid_from, status "
        "FROM knowledge.facts WHERE id = CAST(:i AS uuid)", {"i": fact_id})


def _active_entities() -> list[dict]:
    # external_ids 必须带出来：结构化接入的 entity_ref 客体只给工号（file:397078），
    # 名字栏常常也是工号本身，没有外部标识索引就只能判 unresolved。
    return db.fetch_all(
        "SELECT id, canonical_name, entity_type, aliases, "
        "       COALESCE(properties->'external_ids', '[]'::jsonb) AS external_ids "
        "FROM ontology.entities "
        "WHERE status = 'active' LIMIT 20000")


def _candidate_entities(fact: dict) -> list[dict]:
    """单条事实只需要「主体 + 可能的客体」，不必每次读全表（审查 #5）。

    客体候选：object_value 里的 entity_id / external_id 精确命中；名字精确 / 别名命中；
    以及名字包含匹配（交给 projection.match_entity 判歧义）——用 ILIKE 先在库里缩小范围。
    """
    ov = fact.get("object_value") or {}
    if isinstance(ov, str):
        try:
            ov = json.loads(ov)
        except ValueError:
            ov = {"name": ov}
    oid = proj.pick_object_entity_id(ov)
    ext = proj.pick_object_external_id(ov)
    text = proj.pick_object_text(ov) or ""
    rows = db.fetch_all(
        "SELECT id, canonical_name, entity_type, aliases, "
        "       COALESCE(properties->'external_ids', '[]'::jsonb) AS external_ids "
        "FROM ontology.entities WHERE status = 'active' AND ("
        "  (:sid IS NOT NULL AND id = CAST(:sid AS uuid)) OR id::text = :oid "
        "  OR (properties->'external_ids') @> CAST(:ext AS jsonb) "
        "  OR canonical_name = :txt OR aliases @> CAST(:alias AS jsonb) "
        "  OR (length(canonical_name) >= 3 AND :txt <> '' AND position(canonical_name in :txt) > 0) "
        "  OR (length(:txt) >= 4 AND canonical_name LIKE '%' || :txt || '%')) LIMIT 200",
        # subject_id is nullable for legitimate facts. Bind SQL NULL rather than
        # the literal string "None".
        {"sid": fact.get("subject_id") or None, "oid": oid or "",
         "ext": json.dumps([ext] if ext else ["__none__"]),
         "txt": text, "alias": json.dumps([text] if text else ["__none__"])})
    # A narrow lookup can legitimately return no candidates; retain a
    # deterministic full-index fallback for legacy/mock adapters and aliases.
    return rows if rows else _active_entities()


def _soft_remove(sql_where: str, binds: dict, reason: str) -> int:
    """把命中的边软停用，并写下谁停的、为什么。"""
    row = db.fetch_one(
        "WITH u AS ("
        " UPDATE ontology.relations SET status = 'removed', "
        "   properties = COALESCE(properties, '{}'::jsonb) || CAST(:mark AS jsonb) "
        f" WHERE status = 'active' AND {sql_where} RETURNING 1"
        ") SELECT count(*) AS n FROM u",
        {**binds, "mark": _json({"removed_by": REMOVED_BY, "removed_reason": reason})})
    return int((row or {}).get("n") or 0)


# ---------------------------------------------------------------- R1 事实 → 边
def _upsert_fact_edge_unlocked(fact_id: str) -> dict:
    """一条活跃事实 → 它的投影边（已存在就不重复建）。

    只在事实能确定性映射成合法边时写入；映射不上什么都不做——
    宁可图上少一条边，也不要图上多一条错边。
    """
    fact = _active_fact(fact_id)
    if not fact:
        return {"created": False, "reason": "fact_not_active"}
    if (fact.get("status") or "") != "active":
        return {"created": False, "reason": "fact_status_" + str(fact.get("status"))}
    res = proj.project_facts([fact], _candidate_entities(fact))
    if not res["edges"]:
        return {"created": False, "reason": "not_projectable", "stats": res["stats"]}
    e = res["edges"][0]
    exists = db.fetch_one(
        "SELECT id FROM ontology.relations WHERE source_entity_id = CAST(:s AS uuid) "
        "AND target_entity_id = CAST(:t AS uuid) AND relation_type = :rt "
        "AND status = 'active' LIMIT 1",
        {"s": e["source_entity_id"], "t": e["target_entity_id"], "rt": e["relation_type"]})
    if exists:
        # 边已在，但可能是人工/历史边：补上 fact_id，后续撤销才能对上账
        db.execute(
            "UPDATE ontology.relations "
            "SET properties = COALESCE(properties, '{}'::jsonb) || CAST(:p AS jsonb) "
            "WHERE id = CAST(:i AS uuid) AND COALESCE(properties->>'fact_id', '') = ''",
            {"i": str(exists["id"]),
             "p": _json({"fact_id": e["fact_id"], "relation_type": e["relation_type"]})})
        return {"created": False, "reason": "already_exists",
                "relation_id": str(exists["id"])}
    # #1：ON CONFLICT 兜底并发插入（另一进程刚插入同一三元组时不报错、不重复）
    row = db.fetch_one(
        "INSERT INTO ontology.relations (source_entity_id, relation_type, target_entity_id, "
        " evidence_claim_id, status, properties) VALUES (CAST(:s AS uuid), :rt, CAST(:t AS uuid), "
        " CAST(:ev AS uuid), 'active', CAST(:p AS jsonb)) "
        "ON CONFLICT (source_entity_id, relation_type, target_entity_id) WHERE status = 'active' "
        "DO NOTHING RETURNING id",
        {"s": e["source_entity_id"], "rt": e["relation_type"], "t": e["target_entity_id"],
         "ev": e.get("evidence_claim_id"),
         "p": _json({"origin": "fact_projection", "predicate": e["predicate"],
                     "fact_id": e["fact_id"]})})
    if not row:
        return {"created": False, "reason": "already_exists_concurrent"}
    rid = row["id"]
    return {"created": True, "relation_id": str(rid), "source": e["source_name"],
            "relation": e["relation_type"], "target": e["target_name"]}


# ---------------------------------------------------------------- R2 事实撤销 → 边
def _remove_fact_edge_unlocked(fact_id: str, reason: str = "fact_not_active") -> dict:
    """事实不再成立时，把它撑起来的那条边也停用。"""
    n = _soft_remove("properties->>'fact_id' = :fid", {"fid": fact_id}, reason)
    return {"removed": n, "fact_id": fact_id, "reason": reason}


def _remove_fact_edges_unlocked(fact_ids: list[str], reason: str = "fact_not_active") -> dict:
    """批量停用一组事实撑起来的投影边（一次 SQL，供批量撤销复用）。

    与 remove_fact_edge 逐条调用等价，但避免 N 条事实 = N 次往返。
    """
    ids = [str(x) for x in (fact_ids or []) if x]
    if not ids:
        return {"removed": 0, "count": 0, "reason": reason}
    n = _soft_remove("properties->>'fact_id' = ANY(CAST(:fids AS text[]))",
                     {"fids": ids}, reason)
    return {"removed": n, "count": len(ids), "reason": reason}


def _sync_fact_unlocked(fact_id: str) -> dict:
    """事实的任一变更都走这里：active 建/续边，否则停边。"""
    fact = _active_fact(fact_id)
    if not fact:
        return remove_fact_edge(fact_id, "fact_deleted")
    if (fact.get("status") or "") != "active":
        return remove_fact_edge(fact_id, "fact_" + str(fact.get("status")))
    return upsert_fact_edge(fact_id)


# ---------------------------------------------------------------- R3 人工边 → 事实
def ensure_backing_fact(relation_id: str) -> dict:
    """人工边缺一条本体事实时补上（图 → 本体方向）。

    用 `object_value.entity_id` 而不是名字：名字会改、会重名，
    而对账是按三元组比对的，靠名字匹配迟早对不上。
    """
    row = db.fetch_one(
        "SELECT r.id, r.source_entity_id, r.target_entity_id, r.relation_type, "
        "       r.properties, s.canonical_name AS source_name, "
        "       t.canonical_name AS target_name "
        "FROM ontology.relations r "
        "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
        "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
        "WHERE r.id = CAST(:i AS uuid) AND r.status = 'active'", {"i": relation_id})
    if not row:
        return {"created": False, "reason": "relation_not_found_or_inactive"}
    props = row.get("properties") or {}
    if props.get("fact_id"):
        return {"created": False, "reason": "already_backed", "fact_id": props["fact_id"]}

    fact_id = db.insert_returning_id(
        "knowledge.facts",
        {"claim_id": None,
         "subject_id": row["source_entity_id"],
         "predicate": row["relation_type"],
         "object_value": _json({"entity_id": str(row["target_entity_id"]),
                                "name": row.get("target_name") or ""}),
         "status": "active",
         "source_ref": _json({"origin": MANUAL_ORIGIN, "relation_id": str(row["id"]),
                              "note": (props.get("note") or "")[:500]}),
         "trust_tier": 3,           # 人工确认：最高信任级
         "visibility": "owner"},
    )
    db.execute(
        "UPDATE ontology.relations "
        "SET properties = COALESCE(properties, '{}'::jsonb) || CAST(:p AS jsonb) "
        "WHERE id = CAST(:i AS uuid)",
        {"i": relation_id, "p": _json({"fact_id": str(fact_id)})})
    return {"created": True, "fact_id": str(fact_id),
            "source": row.get("source_name"), "relation": row["relation_type"],
            "target": row.get("target_name")}


# ---------------------------------------------------------------- R4 删边 → 撤事实
def _fact_of_relation(row: dict) -> str | None:
    props = row.get("properties") or {}
    if props.get("fact_id"):
        return str(props["fact_id"])
    # 老数据：人工边可能只有 source_ref 反查
    hit = db.fetch_one(
        "SELECT id FROM knowledge.facts WHERE status = 'active' "
        "AND source_ref->>'origin' = :o AND source_ref->>'relation_id' = :r LIMIT 1",
        {"o": MANUAL_ORIGIN, "r": str(row.get("id"))})
    return str(hit["id"]) if hit else None


def withdraw_backing_fact(fact_id: str, actor: str = "owner",
                          reason: str = "relation_removed") -> dict:
    """撤掉边背后的事实。走 service.withdraw_fact 以保留审计与版本痕迹。"""
    from app.modules.ontology import service as ontology

    try:
        ontology.withdraw_fact(fact_id, actor=actor, reason=reason)
        return {"withdrawn": True, "fact_id": fact_id}
    except ValueError as e:                      # 已撤销/不存在：不阻断删边
        return {"withdrawn": False, "fact_id": fact_id, "reason": str(e)}


def on_relation_removed(relation_row: dict, actor: str = "owner") -> dict:
    """删边的善后：投影边背后的认定被撤掉，人工边背后的自建事实也撤掉。

    不这么做的话，下一次 `reconcile_relations` 会按事实把这条边重新建出来，
    用户会觉得「删了又回来」。
    """
    fid = _fact_of_relation(relation_row)
    if not fid:
        return {"withdrawn": False, "reason": "no_backing_fact"}
    return withdraw_backing_fact(fid, actor=actor, reason="relation_removed")


# ---------------------------------------------------------------- 实体变更
def sync_entity(entity_id: str) -> dict:
    """实体改名/改类型/停用后，重算与它相关的边。

    - 改名：边的两端存的是 id，不用动；但事实里以名字为宾语的那些边要重投影
      （名字变了，旧边可能再也映射不回来）；
    - 改类型：可能让已有边不再满足目录约束，交给 `quality` 报，这里只统计；
    - 停用：相关边一律软停用，避免图上有边、点开实体 404。
    """
    ent = db.fetch_one(
        "SELECT id, canonical_name, entity_type, status FROM ontology.entities "
        "WHERE id = CAST(:i AS uuid)", {"i": entity_id})
    if not ent:
        return {"found": False}
    if (ent.get("status") or "") != "active":
        n = _soft_remove(
            "(source_entity_id = CAST(:e AS uuid) OR target_entity_id = CAST(:e AS uuid))",
            {"e": entity_id}, "entity_" + str(ent.get("status")))
        return {"found": True, "entity_status": ent["status"], "edges_removed": n}

    rows = db.fetch_all(
        "SELECT id FROM knowledge.facts WHERE status = 'active' AND "
        "(subject_id = CAST(:e AS uuid) OR object_value->>'entity_id' = :es) "
        "LIMIT 5000", {"e": entity_id, "es": entity_id})
    created = removed = kept = 0
    for r in rows:
        out = sync_fact(str(r["id"]))
        if out.get("created"):
            created += 1
        elif out.get("relation_id") or out.get("reason") == "already_exists":
            kept += 1
        elif out.get("removed"):
            removed += 1
    return {"found": True, "entity": ent["canonical_name"],
            "entity_type": ent["entity_type"], "facts": len(rows),
            "edges_created": created, "edges_removed": removed, "edges_kept": kept}


# ---------------------------------------------------------------- 对账视图
def orphans(limit: int = 50) -> dict:
    """三类对不上的边，全部只读，不改数据。

    1. `stale_projection`：投影边背后的事实已不是 active；
    2. `dangling_entity`：边的某一端实体已停用/不存在；
    3. `unbacked_manual`：人工边没有本体事实支撑（图上有、本体没有）。
    """
    stale = db.fetch_all(
        "SELECT r.id, r.relation_type, f.status AS fact_status, "
        "       s.canonical_name AS source_name, t.canonical_name AS target_name "
        "FROM ontology.relations r "
        "LEFT JOIN knowledge.facts f ON f.id::text = r.properties->>'fact_id' "
        "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
        "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
        "WHERE r.status = 'active' AND r.properties->>'fact_id' IS NOT NULL "
        "  AND (f.id IS NULL OR f.status <> 'active') LIMIT :n", {"n": limit})
    dangling = db.fetch_all(
        "SELECT r.id, r.relation_type, s.canonical_name AS source_name, "
        "       t.canonical_name AS target_name, "
        "       COALESCE(s.status, 'missing') AS source_status, "
        "       COALESCE(t.status, 'missing') AS target_status "
        "FROM ontology.relations r "
        "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
        "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
        "WHERE r.status = 'active' AND (s.status <> 'active' OR t.status <> 'active') "
        "LIMIT :n", {"n": limit})
    unbacked = db.fetch_all(
        "SELECT r.id, r.relation_type, s.canonical_name AS source_name, "
        "       t.canonical_name AS target_name "
        "FROM ontology.relations r "
        "LEFT JOIN ontology.entities s ON s.id = r.source_entity_id "
        "LEFT JOIN ontology.entities t ON t.id = r.target_entity_id "
        "WHERE r.status = 'active' AND r.properties->>'origin' = 'manual' "
        "  AND r.properties->>'fact_id' IS NULL LIMIT :n", {"n": limit})

    def slim(rows):
        return [{"relation_id": str(r["id"]), "relation": r["relation_type"],
                 "source": r.get("source_name"), "target": r.get("target_name"),
                 **{k: v for k, v in r.items()
                    if k in ("fact_status", "source_status", "target_status")}}
                for r in rows]
    return {"stale_projection": slim(stale), "dangling_entity": slim(dangling),
            "unbacked_manual": slim(unbacked),
            "counts": {"stale_projection": len(stale), "dangling_entity": len(dangling),
                       "unbacked_manual": len(unbacked)}}


def status() -> dict:
    """同步状态一屏：边按来源分桶 + 事实/实体基数。"""
    rows = db.fetch_all(
        "SELECT COALESCE(properties->>'origin', 'legacy') AS origin, "
        "       count(*) AS n, count(*) FILTER (WHERE properties->>'fact_id' IS NOT NULL) AS backed "
        "FROM ontology.relations WHERE status = 'active' GROUP BY 1")
    facts = db.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS total "
        "FROM knowledge.facts")
    ents = db.fetch_one(
        "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS total "
        "FROM ontology.entities")
    edges = {r["origin"]: {"active": int(r["n"]), "with_fact": int(r["backed"])}
             for r in rows}
    o = orphans(limit=500)["counts"]
    return {"edges": edges,
            "edges_total": sum(int(r["n"]) for r in rows),
            "facts": {"active": int((facts or {}).get("active") or 0),
                      "total": int((facts or {}).get("total") or 0)},
            "entities": {"active": int((ents or {}).get("active") or 0),
                         "total": int((ents or {}).get("total") or 0)},
            "orphans": o,
            "in_sync": not any(o.values())}


# ---------------------------------------------------------------- 全量对齐
def _sync_all_unlocked(dry_run: bool = True) -> dict:
    """一键对齐：事实→边（对账）+ 三类孤儿修掉 + 人工边补事实。

    dry_run 只报告条数，不落库；页面上先看再点「执行」。
    """
    from app.modules.ontology import workbench

    recon = workbench.reconcile_relations(dry_run=dry_run)
    orph = orphans(limit=500)

    backfilled = 0
    if not dry_run:
        for item in db.fetch_all(
                "SELECT id FROM ontology.relations WHERE status = 'active' "
                "AND properties->>'origin' = 'manual' "
                "AND properties->>'fact_id' IS NULL LIMIT 500"):
            try:
                r = ensure_backing_fact(str(item["id"]))
                backfilled += 1 if r.get("created") else 0
            except Exception:              # noqa: BLE001  单条失败不打断整批
                continue
        stale_ids = [x["relation_id"] for x in orph["stale_projection"]]
        if stale_ids:
            db.execute(
                "UPDATE ontology.relations SET status = 'removed', "
                "  properties = COALESCE(properties, '{}'::jsonb) || "
                "  CAST(:p AS jsonb) WHERE id = ANY(CAST(:ids AS uuid[]))",
                {"ids": "{" + ",".join(stale_ids) + "}",
                 "p": _json({"removed_by": REMOVED_BY,
                             "removed_reason": "stale_projection"})})
        dangling_ids = [x["relation_id"] for x in orph["dangling_entity"]]
        if dangling_ids:
            db.execute(
                "UPDATE ontology.relations SET status = 'removed', "
                "  properties = COALESCE(properties, '{}'::jsonb) || "
                "  CAST(:p AS jsonb) WHERE id = ANY(CAST(:ids AS uuid[]))",
                {"ids": "{" + ",".join(dangling_ids) + "}",
                 "p": _json({"removed_by": REMOVED_BY,
                             "removed_reason": "dangling_entity"})})

    return {"dry_run": dry_run,
            "reconcile": {"to_add": recon["to_add"], "to_remove": recon["to_remove"],
                          "plan": recon["plan"], "existing": recon["existing"]},
            "orphans": orph["counts"],
            "manual_backfilled": len(orph["unbacked_manual"]) if dry_run else backfilled,
            "after": status() if not dry_run else None}


# ---------------------------------------------------------------- 实时事件同步
# 上面的 *_unlocked 是原始实现；对外仍用原名，统一套一层可重入锁。
# 这样 API 即时钩子、LISTEN 事件线程、手动/启动对账不会并发插出重复边。
def upsert_fact_edge(fact_id: str) -> dict:
    with SYNC_LOCK:
        return _upsert_fact_edge_unlocked(fact_id)


def remove_fact_edge(fact_id: str, reason: str = "fact_not_active") -> dict:
    with SYNC_LOCK:
        return _remove_fact_edge_unlocked(fact_id, reason)


def remove_fact_edges(fact_ids: list[str], reason: str = "fact_not_active") -> dict:
    with SYNC_LOCK:
        return _remove_fact_edges_unlocked(fact_ids, reason)


def upsert_fact_edges(fact_ids: list[str]) -> dict:
    """批量补建投影边（恢复发布后把被停用的边补回来）。

    等价于逐条 upsert_fact_edge，但只加一次锁、失败逐条记录不打断整批：
    边是派生数据，某条映射不上（实体缺失等）不该让其余 600 条都建不起来。
    """
    ids = [str(x) for x in (fact_ids or []) if x]
    if not ids:
        return {"created": 0, "count": 0, "failed": [], "failed_count": 0}
    created, failed = 0, []
    with SYNC_LOCK:
        for fid in ids:
            try:
                created += 1 if _upsert_fact_edge_unlocked(fid).get("created") else 0
            except Exception as e:              # noqa: BLE001  单条失败不打断整批
                failed.append({"fact_id": fid, "error": str(e)[:200]})
    return {"created": created, "count": len(ids),
            "failed": failed[:20], "failed_count": len(failed)}


def sync_fact(fact_id: str) -> dict:
    with SYNC_LOCK:
        return _sync_fact_unlocked(fact_id)


def sync_all(dry_run: bool = True) -> dict:
    with SYNC_LOCK:
        return _sync_all_unlocked(dry_run=dry_run)


PROJECTION_ORIGIN = "fact_projection"


def resync_fact(fact_id: str) -> dict:
    """一条事实发生变更后的最小重算单元（供 LISTEN 事件线程调用）。

    与 sync_fact 的区别：sync_fact 只管「active 建边 / 非 active 撤边」，
    处理不了事实**内容**改了（主体、谓词、客体变化）的情况——旧边三元组
    已不匹配，必须先撤旧再建新，否则同一个 fact_id 会挂着两条 active 边。

    三条约束：
    - 只动 origin='fact_projection' 的边，人工边/历史边即便被补过 fact_id 也不碰；
    - 三元组没变时零写入，避免每次小改动都堆一条新建 + 一条停用；
    - 软停用留痕，不物理删除。
    """
    with SYNC_LOCK:
        fact = _active_fact(fact_id)
        active = bool(fact and (fact.get("status") or "") == "active")
        desired = (proj.project_facts([fact], _active_entities())["edges"]
                   if active else [])
        want = {(str(e["source_entity_id"]), e["relation_type"],
                 str(e["target_entity_id"])) for e in desired}
        cur = db.fetch_all(
            "SELECT id, source_entity_id::text AS s, relation_type AS rt, "
            "       target_entity_id::text AS t FROM ontology.relations "
            "WHERE status = 'active' AND properties->>'fact_id' = :fid "
            "  AND COALESCE(properties->>'origin', '') = :origin",
            {"fid": fact_id, "origin": PROJECTION_ORIGIN})
        stale = [str(r["id"]) for r in cur
                 if (r["s"], r["rt"], r["t"]) not in want]
        removed = 0
        if stale:
            removed = _soft_remove(
                "id = ANY(CAST(:ids AS uuid[]))",
                {"ids": "{" + ",".join(stale) + "}"}, "fact_changed")
        created, reason = False, "unchanged"
        if want:
            r = _upsert_fact_edge_unlocked(fact_id)
            created = bool(r.get("created"))
            reason = str(r.get("reason") or "")
        elif not active:
            reason = "fact_" + str((fact or {}).get("status") or "deleted")
        return {"fact_id": fact_id, "removed": removed, "created": created,
                "reason": reason}


def catchup_relations() -> dict:
    """启动补偿：补上服务停机期间发生、没被 LISTEN 收到的变更。

    刻意**不复用 sync_all(dry_run=false)**：那条路径会给人工边自动补一条
    trust_tier=3、无证据链的 active 事实（绕过审核门），还会物理 DELETE 边。
    这里只做「投影边」这一件事：撤失效、补缺失，软删留痕，不碰事实表。
    """
    with SYNC_LOCK:
        removed = _soft_remove(
            "COALESCE(properties->>'origin', '') = :origin AND ("
            "  properties->>'fact_id' IS NULL OR NOT EXISTS ("
            "    SELECT 1 FROM knowledge.facts f "
            "    WHERE f.id::text = properties->>'fact_id' AND f.status = 'active'))",
            {"origin": PROJECTION_ORIGIN}, "startup_catchup")
        facts = db.fetch_all(
            "SELECT id::text AS id, claim_id, subject_id, predicate, object_value, "
            "       valid_from, status FROM knowledge.facts WHERE status = 'active'")
        ents = _active_entities()
        edges = proj.project_facts(facts, ents)["edges"]
        added = 0
        for e in edges:
            r = _upsert_fact_edge_unlocked(str(e["fact_id"]))
            added += 1 if r.get("created") else 0
        return {"removed": removed, "desired": len(edges), "added": added,
                "active_facts": len(facts)}
