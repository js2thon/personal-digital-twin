"""facts → 语义关系派生（证据驱动 · 只读 facts · 可回链）。

职责：把 knowledge.facts 里「主体与对象都能对齐到已落库语义实例」的人际/组织
事实谓词，派生为语义关系候选；commit=True 时经 OntologyService.write_relation
统一写入口落库（fail-closed 约束照走），并携带 source_fact_ids 回链证据。

原则：
- 纯函数 `candidates()` 离线可测；名称对齐只做「精确 / 去拼音括号 / 首词」三种，
  不做模糊猜测，宁缺毋滥；
- 已存在的关系（含对称等价）不重复派生；对象解析不到实例的进入 unresolved，
  照实返回，不编造；
- 映射表显式声明（PREDICATE_MAP），未映射谓词不动。
"""
from __future__ import annotations

import re

from app.db import session as db

# 事实谓词 → Schema 关系类型（显式映射，未列出的谓词不派生）
PREDICATE_MAP: dict[str, str] = {
    "reports_to": "reports_to",
    "collaborates_with": "collaborates_with",
    "has_colleague": "collaborates_with",      # 同事 ≈ 协作关系
    "same_department_as": "collaborates_with",  # 同部门 ≈ 协作关系
    "knows": "knows",
}

_PAREN = re.compile(r"[（(].*?[)）]")
_SEP = re.compile(r"[、，,;；\s]")


def _name_keys(full_name: str) -> set[str]:
    """实例名称的全部可对齐键：全名 / 去拼音括号 / 括号内拼音。"""
    s = str(full_name or "").strip()
    keys = {s}
    no_paren = _PAREN.sub("", s).strip()
    if no_paren:
        keys.add(no_paren)
    m = _PAREN.search(s)
    if m:
        keys.add(m.group(0)[1:-1].strip())
    return {k for k in keys if k}


def _object_keys(obj: dict) -> set[str]:
    """事实对象的名称键：兼容 {'value':..} / {'name':..} 两种形态。"""
    vals = []
    if isinstance(obj, dict):
        vals = [obj.get("value"), obj.get("name")]
    elif isinstance(obj, str):
        vals = [obj]
    keys: set[str] = set()
    for v in vals:
        v = str(v or "").strip()
        if not v:
            continue
        keys.add(v)
        first = _SEP.split(v)[0].strip()
        if first:
            keys.add(first)
    return keys


def _align(obj_keys: set[str], instance_index: dict[str, tuple[str, str]]
           ) -> tuple[str, str] | None:
    """对象名称键 ↔ 实例名称键 精确对齐，返回 (实例id, 实例名)。"""
    for k in obj_keys:
        hit = instance_index.get(k)
        if hit:
            return hit
    return None


def candidates(facts: list[dict], instances: list[dict],
               existing: list[dict], mapping: dict[str, str] | None = None
               ) -> dict:
    """纯函数：facts + 实例 + 既有关系 → 关系候选（不写库）。

    返回 {candidates, unresolved, skipped_existing}；candidate 携带
    source_fact_ids，供写入时回链证据。
    """
    mapping = mapping or PREDICATE_MAP
    # 实例名称索引（同一名称多实例时取最新写入的）
    idx: dict[str, tuple[str, str]] = {}
    for ins in instances:
        for k in _name_keys(str(ins.get("canonical_name") or "")):
            idx[k] = (str(ins.get("id")), str(ins.get("canonical_name")))
        props = ins.get("properties") or {}
        for k in _name_keys(str(props.get("full_name") or "")):
            idx.setdefault(k, (str(ins.get("id")), str(ins.get("canonical_name"))))

    # 事实主体索引：fact_id → subject_id → 实例（经 source_fact_ids 对齐）
    fact_subject = {str(f.get("id")): str(f.get("subject_id") or "") for f in facts}
    subj_idx: dict[str, tuple[str, str]] = {}
    for ins in instances:
        for fid in (ins.get("source_fact_ids") or []):
            sid = fact_subject.get(str(fid))
            if sid:
                subj_idx[sid] = (str(ins.get("id")), str(ins.get("canonical_name")))

    # 既有关系索引：asserted (src, rel, tgt) 及对称等价
    have: set[tuple[str, str, str]] = set()
    for r in existing:
        if r.get("derived"):
            continue
        have.add((str(r.get("source_id")), str(r.get("relation_type")),
                  str(r.get("target_id"))))

    cands: list[dict] = []
    unresolved: list[dict] = []
    skipped: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for f in facts:
        pred = str(f.get("predicate") or "")
        rel = mapping.get(pred)
        if not rel:
            continue
        src = subj_idx.get(str(f.get("subject_id") or ""))
        if not src:
            continue  # 主体不是已落库实例，跳过（不编造）
        okeys = _object_keys(f.get("object_value") or {})
        tgt = _align(okeys, idx)
        if not tgt:
            unresolved.append({"fact_id": str(f.get("id")), "predicate": pred,
                               "object": sorted(okeys)[:3]})
            continue
        src_id, src_name = src
        tgt_id, tgt_name = tgt
        if src_id == tgt_id:
            continue  # 自环不要
        key = (src_id, rel, tgt_id)
        key_r = (tgt_id, rel, src_id)
        if key in seen:
            continue
        if key in have or key_r in have:
            skipped.append({"source": src_name, "relation_type": rel,
                            "target": tgt_name, "reason": "已存在"})
            continue
        seen.add(key)
        cands.append({"source_id": src_id, "source_name": src_name,
                      "relation_type": rel, "target_id": tgt_id,
                      "target_name": tgt_name,
                      "source_fact_ids": [str(f.get("id"))],
                      "from_predicate": pred})
    return {"candidates": cands, "unresolved": unresolved, "skipped_existing": skipped}


def derive(tenant_id: str = "default", commit: bool = False) -> dict:
    """读取 facts → 生成关系候选；commit=True 时经统一写入口落库。"""
    from . import store
    from .service import OntologyService

    preds = sorted(PREDICATE_MAP) + ["full_name"]  # full_name 仅用于建主体索引
    ph = ", ".join(f":p{i}" for i in range(len(preds)))
    params = {f"p{i}": p for i, p in enumerate(preds)}
    facts = db.fetch_all(
        f"SELECT id, subject_id, predicate, object_value FROM knowledge.facts "
        f"WHERE predicate IN ({ph})", params)
    instances = store.list_instances(tenant_id, limit=2000)
    existing = store.list_relations(tenant_id, limit=2000)
    report = candidates(facts, instances, existing)
    report["predicate_map"] = dict(PREDICATE_MAP)
    report["facts_scanned"] = len(facts)
    report["committed"] = False
    if commit and report["candidates"]:
        svc = OntologyService(tenant_id=tenant_id, actor="fact_derive")
        applied, rejected = 0, []
        for c in report["candidates"]:
            payload = {"source_id": c["source_id"],
                       "relation_type": c["relation_type"],
                       "target_id": c["target_id"],
                       "source_type": "Employee", "target_type": "Employee",
                       "source_fact_ids": c["source_fact_ids"]}
            try:
                svc.write_relation(payload, auto_reasoning=True)
                applied += 1
            except Exception as ex:  # 约束/规则拒绝：照实记录
                rejected.append({"source": c["source_name"],
                                 "relation_type": c["relation_type"],
                                 "target": c["target_name"],
                                 "error": str(ex)[:120]})
        report.update({"committed": True, "applied": applied,
                       "rejected": rejected})
    return report
