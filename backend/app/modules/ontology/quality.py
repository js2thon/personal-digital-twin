"""本体规则校验引擎（纯函数，可离线测）。

设计取向：**规则要能被证明有效**。所以每条规则都自带一个「应该触发」的
反例和「不应该触发」的正例，`selftest()` 用内置 fixture 跑一遍——规则写错、
阈值调飘、正则漏改，自测会直接红，而不是等线上数据出问题。

规则分三档严重度：
- error：结构性错误（类型不符、自环、悬空、重复）——图不可信
- warn ：可疑但可解释（无证据边、单值冲突、未登记类型）
- info ：质量指标（孤立实体、缺锚点）

纯函数：`run_rules` / `selftest`。
"""
from __future__ import annotations

from app.modules.ontology.graph import RELATION_TYPES
from app.modules.ontology.projection import (
    ATTRIBUTE_PREDICATES, build_name_index, normalize_value,
)

RULE_META = {
    "R1": ("未登记关系类型", "warn", "关系类型不在本体目录中，多为抽取噪声"),
    "R2": ("类型不满足约束", "error", "边两端的实体类型不符合关系类型定义"),
    "R3": ("单值关系冲突", "error", "同一主体同一单值关系出现多条有效边"),
    "R4": ("自环关系", "error", "源与目标是同一实体，无语义"),
    "R5": ("关系缺证据", "warn", "边没有 evidence_claim_id，无法追溯来源"),
    "R6": ("悬空端点", "error", "边的端点实体已合并/已废弃或不存在"),
    "R7": ("重复关系", "error", "同一 (源,类型,目标) 存在多条有效边"),
    "R8": ("实体档案异常", "error", "实体名为空或类型不在本体定义内"),
    "R9": ("属性值指向实体", "warn", "属性类谓词的值其实是实体名，应建成关系"),
    "R10": ("孤立实体", "info", "实体没有任何有效关系，图上看不到它"),
}


def _r(rid: str, count: int, samples: list[dict], **extra) -> dict:
    name, severity, desc = RULE_META[rid]
    out = {"id": rid, "name": name, "severity": severity, "description": desc,
           "count": count, "samples": samples[:8], "passed": count == 0}
    out.update(extra)
    return out


def _endpoint(ent: dict | None) -> tuple[str, str]:
    if not ent:
        return ("missing", "missing")
    return (ent.get("entity_type") or "", ent.get("status") or "active")


def run_rules(entities: list[dict], relations: list[dict],
              facts: list[dict] | None = None,
              allowed_types: set[str] | None = None) -> dict:
    """跑全部规则。全部输入都是行字典，函数本身不碰数据库。"""
    facts = facts or []
    by_id = {str(e["id"]): e for e in entities}
    active = [e for e in entities if (e.get("status") or "active") == "active"]

    # 每个规则独立容器——共用同一个 list 会让命中互相污染（曾踩过）
    r1: list[dict] = []
    r2: list[dict] = []
    r3: list[dict] = []
    r4: list[dict] = []
    r5: list[dict] = []
    r6: list[dict] = []
    r7: list[dict] = []
    r8: list[dict] = []
    single_count: dict[tuple, list] = {}
    triples: dict[tuple, list] = {}

    for r in relations:
        if (r.get("status") or "active") != "active":
            continue
        rtype = r.get("relation_type") or ""
        src = by_id.get(str(r.get("source_entity_id")))
        dst = by_id.get(str(r.get("target_entity_id")))
        spec = RELATION_TYPES.get(rtype)
        label = (src or {}).get("canonical_name", "?")
        tlabel = (dst or {}).get("canonical_name", "?")
        item = {"relation_id": str(r.get("id")), "source": label,
                "type": rtype, "target": tlabel}

        if not spec:
            r1.append(item)
        else:
            st, _ = _endpoint(src)
            tt, _ = _endpoint(dst)
            bad = []
            if src and "*" not in spec["source"] and st not in spec["source"]:
                bad.append(f"源 {st}")
            if dst and "*" not in spec["target"] and tt not in spec["target"]:
                bad.append(f"目标 {tt}")
            if bad:
                r2.append({**item, "problem": "、".join(bad) + f" 不在 {rtype} 允许范围"})
            if spec.get("cardinality") == "one" and src:
                single_count.setdefault((str(src["id"]), rtype), []).append(item)

        if src and dst and src["id"] == dst["id"]:
            r4.append(item)
        if not r.get("evidence_claim_id"):
            r5.append(item)
        if not src or not dst:
            r6.append({**item, "problem": "端点实体不存在"})
        elif ((src.get("status") or "active") != "active"
              or (dst.get("status") or "active") != "active"):
            r6.append({**item, "problem": "端点已合并或废弃"})
        triples.setdefault((str(r.get("source_entity_id")), rtype,
                            str(r.get("target_entity_id"))), []).append(item)

    for (src_id, rtype), items in single_count.items():
        if len(items) > 1:
            r3.append({"entity": by_id.get(src_id, {}).get("canonical_name", "?"),
                       "type": rtype, "count": len(items),
                       "values": [i["target"] for i in items]})
    for items in triples.values():
        if len(items) > 1:
            r7.append({**items[0], "count": len(items)})

    for e in entities:
        et = e.get("entity_type") or ""
        if not (e.get("canonical_name") or "").strip() or (allowed_types and et not in allowed_types):
            r8.append({"entity_id": str(e.get("id")), "name": e.get("canonical_name"),
                       "type": et or "(空)",
                       "problem": "名称为空" if not (e.get("canonical_name") or "").strip()
                       else f"类型 {et} 不在本体定义内"})

    # R9：属性谓词的值刚好是某个实体名 → 本该是关系
    by_name, _by_alias = build_name_index(active)
    r9 = []
    for f in facts:
        pred = (f.get("predicate") or "").strip()
        if pred not in ATTRIBUTE_PREDICATES:
            continue
        text = normalize_value((f.get("object_value") or {}).get("value", "")
                               if isinstance(f.get("object_value"), dict) else "")
        hit = by_name.get(text) if text else None
        if hit:
            r9.append({"fact_id": str(f.get("id")), "predicate": pred,
                       "value": text, "match": hit[0].get("canonical_name")})

    # R10：孤立实体
    linked: set[str] = set()
    for r in relations:
        if (r.get("status") or "active") != "active":
            continue
        linked.add(str(r.get("source_entity_id")))
        linked.add(str(r.get("target_entity_id")))
    r10 = [{"entity_id": str(e["id"]), "name": e.get("canonical_name"),
            "type": e.get("entity_type")} for e in active if str(e["id"]) not in linked]

    rules = [
        _r("R1", len(r1), r1),
        _r("R2", len(r2), r2),
        _r("R3", len(r3), r3),
        _r("R4", len(r4), r4),
        _r("R5", len(r5), r5),
        _r("R6", len(r6), r6),
        _r("R7", len(r7), r7),
        _r("R8", len(r8), r8),
        _r("R9", len(r9), r9),
        _r("R10", len(r10), r10),
    ]
    errors = sum(r["count"] for r in rules if r["severity"] == "error")
    warns = sum(r["count"] for r in rules if r["severity"] == "warn")
    return {
        "rules": rules,
        "summary": {
            "rules": len(rules),
            "failing_rules": sum(1 for r in rules if r["count"]),
            "errors": errors,
            "warnings": warns,
            "entities": len(active),
            "relations": len([r for r in relations
                              if (r.get("status") or "active") == "active"]),
            "verdict": "block" if errors else ("warn" if warns else "pass"),
        },
    }


# ---------------------------------------------------------------- 自测 fixture
def _ent(eid: str, name: str, etype: str, status: str = "active") -> dict:
    return {"id": eid, "canonical_name": name, "entity_type": etype, "status": status}


def _rel(rid: str, s: str, t: str, rt: str, ev: str | None = "c1",
         status: str = "active") -> dict:
    return {"id": rid, "source_entity_id": s, "target_entity_id": t,
            "relation_type": rt, "evidence_claim_id": ev, "status": status}


def selftest() -> dict:
    """规则引擎自测：干净集必须全绿，脏集每条规则都必须命中。"""
    allowed = {"Person", "Project", "Organization", "Role", "System"}

    clean_entities = [_ent("p1", "张三", "Person"), _ent("p2", "李四", "Person"),
                      _ent("o1", "某公司", "Organization"), _ent("j1", "某项目", "Project")]
    clean_relations = [_rel("r1", "p1", "p2", "reports_to"),
                       _rel("r2", "p1", "o1", "works_at")]
    clean = run_rules(clean_entities, clean_relations, [], allowed)
    clean_ok = all(r["count"] == 0 for r in clean["rules"] if r["id"] != "R10")

    dirty_entities = clean_entities + [
        _ent("p3", "", "Person"),                      # R8 空名
        _ent("x1", "幽灵", "Gadget"),                   # R8 类型不在定义内
        _ent("p9", "已合并", "Person", status="merged"),  # 悬空端点素材
    ]
    dirty_relations = clean_relations + [
        _rel("d1", "p1", "j1", "made_up_type"),        # R1 未登记
        _rel("d2", "p1", "j1", "reports_to"),          # R2 Person→Project 非法 + R3 冲突
        _rel("d3", "p1", "p1", "manages"),             # R4 自环
        _rel("d4", "p1", "o1", "works_at", ev=None),   # R5 无证据 + R3 works_at 单值冲突
        _rel("d5", "p1", "p9", "manages"),             # R6 端点已合并
        _rel("d6", "p2", "o1", "works_at", ev="c9"),
        _rel("d7", "p2", "o1", "works_at", ev="c10"),  # R7 重复
        _rel("d8", "p3", "o1", "works_at"),            # 空名实体上的边
    ]
    dirty_facts = [{"id": "f1", "subject_id": "p1", "predicate": "manager_id",
                    "object_value": {"value": "李四"}}]  # R9 属性值是实体名
    dirty = run_rules(dirty_entities, dirty_relations, dirty_facts, allowed)
    hit = {r["id"] for r in dirty["rules"] if r["count"]}
    expected = {"R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"}
    missing = sorted(expected - hit)

    checks = [
        {"name": "干净数据无 error/warn", "passed": clean_ok,
         "detail": "全部规则 0 命中" if clean_ok else
         [r["id"] for r in clean["rules"] if r["count"]]},
        {"name": "脏数据触发全部预期规则", "passed": not missing,
         "detail": "无遗漏" if not missing else f"未触发: {','.join(missing)}"},
    ]
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "expected_rules": sorted(expected), "hit_rules": sorted(hit),
            "clean": clean["summary"], "dirty": dirty["summary"]}
