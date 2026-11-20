"""事实 → 关系的确定性投影（图的对账重建）。

## 为什么需要这一层

图（`ontology.relations`）此前是「第二套真源」：蒸馏时用 `object_value.name`
做**精确同名**匹配建边，结构化事实（`{"value": ...}`）根本没有 name 字段，
结构化侧从不写边。结果是事实底座里明明有 `reports_to / works_at / level`，
图上却什么都看不到——94 个实体 79 个孤立。

本模块把「哪些事实应该对应哪些边」写成**纯函数规则**，于是：

- 可离线单测（不连库）；
- 可对账重建（`diff` 出缺失/多余，由 service 落库）；
- 审核通过、事实撤销、实体合并后都能重算，不再依赖「写入那一刻实体恰好存在」。

## 三条硬约束

1. **只投影已登记关系类型**：未登记谓词（`will_know_after_signing`、
   `department-3`、`level`）留在事实里，不进图。属性不是边。
2. **只投影类型合法的边**：`Person -has_role-> Project` 这种不再生成。
3. **不删非投影来源的边**：人工建的边必须由人来删。

纯函数：`normalize_value` / `pick_object_text` / `match_entity` /
`project_facts` / `diff`。
"""
from __future__ import annotations

import re

from app.modules.ontology.graph import RELATION_TYPES

# ---------------------------------------------------------------- 谓词映射
# 谓词和关系类型多数同名（reports_to/works_at/participated_in…）。
# 这里只补「语义等价但名字不同」的少数几个，每一条都要能说清方向。
PREDICATE_ALIASES: dict[str, str] = {
    "responsible_for": "is_owner_of",
    "leads": "is_project_manager_of",
    "attends": "participated_in",
    "attended": "participated_in",
    "belongs_to": "belongs_to_organization",
    "employed_by": "works_at",
    "reports": "reports_to",
}

# 明确的属性谓词：即使哪天被误登记成关系类型，也不许变成边。
ATTRIBUTE_PREDICATES = {
    "level", "job_title", "status", "full_name", "email", "phone", "employee_id",
    "emplid", "gender", "birthday", "hire_date", "dept_entry_dt", "action_code",
    "action_cn_name", "age", "location", "company", "department", "manager_id",
}

# 「值 → 实体」匹配阈值。
_MIN_CONTAIN_LEN = 3        # 实体名至少 3 字才允许「值包含名」
_MIN_COVERAGE = 0.5         # 实体名占值文本的比例下限（避免「项目」吃掉「数据安全项目」）


def predicate_to_relation(predicate: str | None) -> str | None:
    """谓词 → 已登记关系类型；不能映射则返回 None。"""
    if not predicate:
        return None
    p = predicate.strip()
    if not p or p in ATTRIBUTE_PREDICATES or p.startswith("department-"):
        return None
    mapped = PREDICATE_ALIASES.get(p, p)
    return mapped if mapped in RELATION_TYPES else None


_ANNOT = re.compile(r"[（(][^（）()]*[）)]")


def normalize_value(text: str) -> str:
    """归一化值文本：去中英文括注、去空白、去首尾标点。

    `"某某乙 (Mou Mou B)"` → `"某某乙"`；
    `"公司"` 保持原样（交给匹配器判断包含关系）。
    """
    s = (text or "").strip()
    s = _ANNOT.sub("", s)          # 去括注
    s = s.strip(" 　\t\r\n·.,，。；;:：、\"'“”‘’")
    return re.sub(r"\s+", " ", s)


def pick_object_text(object_value) -> str | None:
    """从事实的 object_value 里取出「可能是实体」的文本。

    结构化事实是 `{"value": ...}`，文档事实多是 `{"name": ...}`。只取标量，
    数组/对象视为无法判定（宁可漏建边，不可乱建）。
    """
    if isinstance(object_value, str):
        return normalize_value(object_value) or None
    if not isinstance(object_value, dict):
        return None
    for key in ("name", "value", "entity", "target", "org", "person"):
        v = object_value.get(key)
        if isinstance(v, str):
            t = normalize_value(v)
            if t:
                return t
    return None


def pick_object_entity_id(object_value) -> str | None:
    """object_value 里显式给出的实体 id（人工建边回写事实时用）。

    有 id 就不该再走名字匹配：名字会改名、会重名，id 不会。
    """
    if not isinstance(object_value, dict):
        return None
    for key in ("entity_id", "target_id", "object_id"):
        v = object_value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def pick_object_external_id(object_value) -> str | None:
    """object_value 里显式给出的外部标识（如 `file:397078`）。

    结构化接入用 entity_ref 时，客体写的是源系统的工号/主键，只有 external_id
    能唯一定位到实体；name 那一栏常常就是工号本身，走名字匹配必然落空。
    """
    if not isinstance(object_value, dict):
        return None
    for key in ("external_id", "external_ids", "target_external_id"):
        v = object_value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def build_external_index(entities: list[dict]) -> dict[str, list[dict]]:
    """external_id → [实体]。同一标识命中多条时不猜，交给调用方按歧义处理。"""
    idx: dict[str, list[dict]] = {}
    for e in entities:
        ids = e.get("external_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            continue
        for x in ids:
            if isinstance(x, str) and x.strip():
                idx.setdefault(x.strip(), []).append(e)
    return idx


def build_name_index(entities: list[dict]) -> tuple[dict, dict]:
    """→ (canonical_name → [实体], alias → [实体])。

    同一名字可能对应多条档案（同名同型已在写入侧归一，这里兜底取多条）。
    """
    by_name: dict[str, list[dict]] = {}
    by_alias: dict[str, list[dict]] = {}
    for e in entities:
        name = (e.get("canonical_name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(e)
        aliases = e.get("aliases") or []
        if isinstance(aliases, list):
            for a in aliases:
                if isinstance(a, str) and a.strip():
                    by_alias.setdefault(a.strip(), []).append(e)
    return by_name, by_alias


def _type_ok(relation_type: str, entity: dict) -> bool:
    """目标实体类型是否满足关系类型的目标约束。"""
    spec = RELATION_TYPES.get(relation_type) or {}
    allowed = spec.get("target") or ["*"]
    return "*" in allowed or (entity.get("entity_type") in allowed)


def _source_ok(relation_type: str, entity: dict) -> bool:
    spec = RELATION_TYPES.get(relation_type) or {}
    allowed = spec.get("source") or ["*"]
    return "*" in allowed or (entity.get("entity_type") in allowed)


def match_entity(text: str, by_name: dict, by_alias: dict,
                 relation_type: str | None = None) -> dict | None:
    """文本 → 唯一实体。命中不了或命中多个直接返回 None（宁可漏，不可错）。

    优先级：全名 → 别名 → 文本包含实体名（长名优先、覆盖率达标）
            → 实体名包含文本（文本足够长）。
    给了 relation_type 时，同名多条档案按目标类型约束裁决。
    """
    if not text:
        return None
    cands = by_name.get(text) or by_alias.get(text) or []
    if not cands:
        pool = []
        for name, group in by_name.items():
            if len(name) >= _MIN_CONTAIN_LEN and name in text and len(name) / len(text) >= _MIN_COVERAGE:
                pool.extend(group)
        if not pool:
            for name, group in by_name.items():
                if len(text) >= 4 and text in name and len(text) / len(name) >= 0.6:
                    pool.extend(group)
        cands = pool
    if not cands:
        return None
    if relation_type:
        typed = [c for c in cands if _type_ok(relation_type, c)]
        if typed:
            cands = typed
    ids = {c.get("id") for c in cands}
    if len(ids) != 1:
        return None                       # 歧义：不猜
    return cands[0]


def project_facts(facts: list[dict], entities: list[dict]) -> dict:
    """事实集合 → 应有的边集合。

    facts: [{id, subject_id, predicate, object_value, trust_tier}]
    entities: [{id, canonical_name, entity_type, aliases}]
    返回 {edges, stats, skipped, ambiguous}
    """
    by_name, by_alias = build_name_index(entities)
    by_ext = build_external_index(entities)
    by_id = {str(e["id"]): e for e in entities}   # UUID 对象与字符串键必须统一
    edges: list[dict] = []
    seen: set[tuple] = set()
    stats = {"facts": len(facts), "projected": 0, "attribute": 0,
             "unregistered": 0, "unresolved": 0, "type_mismatch": 0,
             "self_loop": 0, "duplicate": 0, "by_entity_id": 0, "by_external_id": 0}
    skipped: list[dict] = []

    for f in facts:
        pred = (f.get("predicate") or "").strip()
        rtype = predicate_to_relation(pred)
        if not rtype:
            stats["attribute" if pred in ATTRIBUTE_PREDICATES or pred.startswith("department-")
                  else "unregistered"] += 1
            continue
        subj = by_id.get(str(f.get("subject_id") or ""))
        if not subj:
            continue
        if not _source_ok(rtype, subj):
            stats["type_mismatch"] += 1
            skipped.append({"fact": str(f.get("id")), "predicate": pred,
                            "reason": "源类型不符", "relation": rtype})
            continue
        # 显式 entity_id 优先：人工建的边回写成事实时，名字可能已改或有重名，
        # 只有 id 是确定的。其次认 external_id（源系统工号），最后才退回名字匹配。
        oid = pick_object_entity_id(f.get("object_value"))
        target = by_id.get(oid) if oid else None
        if target:
            stats["by_entity_id"] += 1
        else:
            ext = pick_object_external_id(f.get("object_value"))
            hits = by_ext.get(ext) if ext else None
            if ext and not hits:
                stats["external_missing"] = stats.get("external_missing", 0) + 1
            if hits and len(hits) == 1:
                target = hits[0]
                stats["by_external_id"] += 1
            else:
                text = pick_object_text(f.get("object_value"))
                if not text:
                    stats["unresolved"] += 1
                    continue
                target = match_entity(text, by_name, by_alias, rtype)
        if not target:
            stats["unresolved"] += 1
            skipped.append({"fact": str(f.get("id")), "predicate": pred,
                            "value": (pick_object_text(f.get("object_value")) or "")[:60],
                            "reason": "值未匹配到唯一实体"})
            continue
        if target["id"] == subj["id"]:
            stats["self_loop"] += 1
            continue
        if not _type_ok(rtype, target):
            stats["type_mismatch"] += 1
            skipped.append({"fact": str(f.get("id")), "predicate": pred,
                            "value": text[:60], "target": target.get("canonical_name"),
                            "reason": f"目标类型 {target.get('entity_type')} 不满足 {rtype} 约束"})
            continue
        key = (subj["id"], rtype, target["id"])
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        stats["projected"] += 1
        edges.append({"source_entity_id": subj["id"], "relation_type": rtype,
                      "target_entity_id": target["id"],
                      "evidence_claim_id": str(f.get("claim_id")) if f.get("claim_id") else None,
                      "fact_id": str(f.get("id")),
                      "source_name": subj.get("canonical_name"),
                      "target_name": target.get("canonical_name"),
                      "target_type": target.get("entity_type"),
                      "predicate": pred})
    return {"edges": edges, "stats": stats, "skipped": skipped}


def diff(existing: list[dict], desired: list[dict]) -> dict:
    """已有边 vs 应有边 → 增/删（按 source,type,target 三元组比对，不看证据）。"""
    cur = {}
    for e in existing:
        cur[(str(e["source_entity_id"]), e["relation_type"], str(e["target_entity_id"]))] = e
    want = {(str(e["source_entity_id"]), e["relation_type"], str(e["target_entity_id"])): e
            for e in desired}
    to_add = [want[k] for k in want if k not in cur]
    to_remove = [cur[k] for k in cur if k not in want]
    kept = [k for k in want if k in cur]
    return {"to_add": to_add, "to_remove": to_remove,
            "stats": {"existing": len(cur), "desired": len(want),
                      "add": len(to_add), "remove": len(to_remove),
                      "keep": len(kept)}}
