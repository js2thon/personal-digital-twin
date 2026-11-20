"""实例 ↔ Fact 双向对齐（V1.3 语义层方案 · 能力 2）。

补齐「本体实例与 Fact 的双向追溯与一致性校验偏弱」：
- 正向：instance.source_fact_ids → knowledge.facts（关联事实明细）；
- 反向：fact 的 subject/predicate → 命中的实例（按 canonical_name / 类型）；
- 一致性：实例状态、属性与关联事实的状态/值冲突检测，只报告不改写。
"""
from __future__ import annotations

from . import store


def _facts_for(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    rows = store.db.fetch_all(
        "SELECT id, predicate, object_value, status, ontology_version, tenant_id "
        "FROM knowledge.facts WHERE id = ANY(CAST(:ids AS uuid[]))",
        {"ids": "{" + ",".join(str(x) for x in ids if x) + "}"})
    return [{**r, "id": str(r["id"])} for r in rows]


def trace(instance_id: str, tenant_id: str = "default") -> dict:
    """正向+反向追溯 + 一致性报告。"""
    inst = store.get_instance(instance_id, tenant_id)
    if not inst:
        raise LookupError(f"实例 {instance_id} 不存在")
    fact_ids = store.jload(inst.get("source_fact_ids"), []) or []
    facts = _facts_for([str(x) for x in fact_ids])

    # 反向：按 canonical_name 在 facts.object_value / 主体侧找引用（只读，best-effort）
    name = inst.get("canonical_name") or ""
    reverse: list[dict] = []
    if name:
        try:
            rows = store.db.fetch_all(
                "SELECT id, predicate, status FROM knowledge.facts "
                "WHERE object_value::text ILIKE :like AND tenant_id = :t LIMIT 20",
                {"like": f"%{name}%", "t": tenant_id})
            reverse = [{**r, "id": str(r["id"])} for r in rows]
        except Exception:  # noqa: BLE001  反向检索是增强项，失败不影响主追溯
            reverse = []

    # 一致性：实例 active 但关联事实已撤回/superseded → 冲突；事实版本与实例版本不一致 → 提示
    conflicts: list[dict] = []
    for f in facts:
        st = (f.get("status") or "active")
        if st not in ("active", "candidate"):
            conflicts.append({"fact_id": f["id"], "kind": "fact_status",
                              "detail": f"关联事实状态={st}，实例仍为 active"})
        if f.get("ontology_version") and f["ontology_version"] != inst.get("ontology_version"):
            conflicts.append({"fact_id": f["id"], "kind": "version_drift",
                              "detail": f"事实版本={f['ontology_version']}，"
                                        f"实例版本={inst.get('ontology_version')}"})
    linked = len(facts)
    declared = len(fact_ids)
    if declared and linked < declared:
        conflicts.append({"kind": "dangling_fact_ref",
                          "detail": f"声明 {declared} 条 Fact，实际存在 {linked} 条"})
    return {
        "instance": {"id": inst["id"], "type": inst.get("type_name"),
                     "name": inst.get("canonical_name"),
                     "ontology_version": inst.get("ontology_version"),
                     "status": inst.get("status")},
        "forward": {"declared_fact_ids": fact_ids, "resolved_facts": facts,
                    "resolved_count": linked},
        "reverse": {"by_name": name, "matched_facts": reverse,
                    "matched_count": len(reverse)},
        "consistency": {"consistent": not conflicts, "conflict_count": len(conflicts),
                        "conflicts": conflicts},
        "bidirectional": bool(facts) and bool(reverse),
    }


def consistency_metrics(tenant_id: str = "default") -> dict:
    """全库实例的一致性比率（可评估指标之一），只读。"""
    rows = store.list_instances(tenant_id, limit=1000)
    total = len(rows)
    conflicted = 0
    for inst in rows:
        ids = store.jload(inst.get("source_fact_ids"), []) or []
        facts = _facts_for([str(x) for x in ids])
        bad = any((f.get("status") not in ("active", "candidate")) for f in facts)
        if ids and (bad or len(facts) < len(ids)):
            conflicted += 1
    return {"instances_checked": total, "with_fact_links":
            sum(1 for r in rows if (store.jload(r.get("source_fact_ids"), []) or [])),
            "conflicted": conflicted,
            "consistency_rate": round(1 - conflicted / total, 4) if total else None}
