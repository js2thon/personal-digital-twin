"""#21：统一类型体系——graph.RELATION_TYPES 由 cq_v2 Registry 派生，并提供一致性断言。

迁移策略（不推倒重来）：
1. `registry_patch()` 把 graph.py 里 35 个已登记关系（含源/目标约束、family、基数）合并进 Registry
   当前版本（只增不删），并修正两处冲突：has_role 目标统一为 Role（与数据一致）、
   believes 目标改为 Concept（观点不是文档）；Organization 作为 OrganizationUnit 的同义类型（parent）。
2. `derive_relation_types()` 运行时由 Registry 生成 graph 所需结构；graph.RELATION_TYPES 在启动时被覆盖。
3. `check()` 供 CI：Registry 与运行时目录、SHACL 端点三者一致，不一致即失败。
"""
from __future__ import annotations

CARD_TO_GRAPH = {"many_to_one": "one", "one_to_one": "one", "one_to_many": "many",
                 "many_to_many": "many", "one": "one", "many": "many"}
GRAPH_TO_CARD = {"one": "many_to_one", "many": "many_to_many"}
EXTRA_TYPES = {
    "Organization": {"label": "组织", "aliases": ["公司", "机构"], "parent": "OrganizationUnit"},
    **{f"DepartmentLevel{i}": {"label": f"{i}级部门", "aliases": [], "parent": "OrganizationUnit"}
       for i in range(1, 8)},
    "Department": {"label": "部门", "aliases": [], "parent": "OrganizationUnit"},
    "Role": {"label": "角色", "aliases": ["岗位"], "parent": ""},
    "Concept": {"label": "概念/观点", "aliases": ["观点"], "parent": ""},
    "Claim": {"label": "主张", "aliases": [], "parent": ""},
    "Event": {"label": "事件", "aliases": ["会议"], "parent": ""},
}
FIXES = {"has_role": {"target": "Role"}, "believes": {"target": "Concept"}}


def _as_list(v):
    if isinstance(v, list):
        return v
    return [v] if v else ["*"]


def registry_patch(current: dict, graph_types: dict) -> dict:
    """graph.RELATION_TYPES → Registry 新版本草案（只增不删）。"""
    import copy
    out = copy.deepcopy(current)
    et = out.setdefault("entity_types", {})
    for k, v in EXTRA_TYPES.items():
        et.setdefault(k, v)
    rt = out.setdefault("relation_types", {})
    for name, g in graph_types.items():
        src, tgt = _as_list(g.get("source")), _as_list(g.get("target"))
        if name in rt:
            cur = rt[name]
            cur.setdefault("family", g.get("family"))
            # 并集：Registry 自身的 source/target + 内置目录的约束（不能用内置覆盖 Registry）
            for key, single, extra in (("source_types", "source", src), ("target_types", "target", tgt)):
                mine = cur.get(key) or _as_list(cur.get(single))
                merged = ["*"] if "*" in mine or "*" in extra else sorted(set(mine) | set(extra))
                cur[key] = merged
        else:
            rt[name] = {"label": g.get("label", name), "family": g.get("family", "other"),
                        "source": src[0] if len(src) == 1 else "*", "target": tgt[0] if len(tgt) == 1 else "*",
                        "source_types": src, "target_types": tgt,
                        "cardinality": GRAPH_TO_CARD.get(g.get("cardinality"), "many_to_many")}
    for name, fix in FIXES.items():
        if name in rt:
            rt[name].update(fix)
            rt[name]["target_types"] = [fix["target"]]
    return out


def descendants(schema: dict, t: str) -> set[str]:
    et = schema.get("entity_types") or {}
    out = {t}
    changed = True
    while changed:
        changed = False
        for k, v in et.items():
            if k not in out and (v or {}).get("parent") in out:
                out.add(k)
                changed = True
    return out


def derive_relation_types(schema: dict) -> dict:
    """Registry → graph.RELATION_TYPES 结构；源/目标约束按继承展开（Person 约束接受其子类）。"""
    out = {}
    for name, r in (schema.get("relation_types") or {}).items():
        def expand(key, single):
            types = r.get(key) or _as_list(r.get(single))
            if "*" in types:
                return ["*"]
            return sorted(set().union(*[descendants(schema, t) for t in types]))
        out[name] = {"label": r.get("label") or name, "family": r.get("family") or "other",
                     "source": expand("source_types", "source"),
                     "target": expand("target_types", "target"),
                     "cardinality": CARD_TO_GRAPH.get(r.get("cardinality"), "many")}
    return out


_BUILTIN: dict | None = None


def _builtin() -> dict:
    """graph.py 代码内置目录的快照（install 覆盖前保存一次）。"""
    global _BUILTIN
    if _BUILTIN is None:
        import copy
        from app.modules.ontology import graph
        _BUILTIN = copy.deepcopy(graph.RELATION_TYPES)
    return _BUILTIN


def pending_patch(registry) -> dict:
    """Registry 当前版本缺了哪些内置关系/类型（发布前预览）。"""
    cur = registry.current()
    merged = registry_patch(cur, _builtin())
    return {"missing_relations": sorted(set(merged["relation_types"]) - set(cur.get("relation_types") or {})),
            "missing_types": sorted(set(merged["entity_types"]) - set(cur.get("entity_types") or {})),
            "fixed": sorted(k for k in FIXES if (cur.get("relation_types") or {}).get(k, {}).get("target")
                            != FIXES[k]["target"])}


def publish_patch(engine, actor: str = "system") -> dict:
    """把内置目录并入 Registry 并发布为新版本（一次性迁移，之后 Registry 即唯一来源）。"""
    p = pending_patch(engine.registry)
    if not any(p.values()):
        return {"published": False, "reason": "already_in_sync"}
    cur = engine.registry.current_version() or "v2.0.0"
    schema = registry_patch(engine.registry.current(), _builtin())
    schema["schema_version"] = f"{cur}-types-unified"
    return {"published": True, **engine.registry_publish(schema, actor=actor), "patch": p}


def install(registry) -> dict:
    """启动时调用：graph.RELATION_TYPES ← derive(Registry ∪ 内置目录)，原地更新同一 dict，
    projection / quality 中 `from graph import RELATION_TYPES` 的引用同步生效。

    Registry 尚未完成迁移（publish_patch）时，内置目录中 Registry 缺的类型仍保留——
    绝不因为 Registry 较小而让投影丢失关系类型。
    """
    from app.modules.ontology import graph
    builtin = _builtin()
    derived = derive_relation_types(registry_patch(registry.current(), builtin))
    graph.RELATION_TYPES.clear()
    graph.RELATION_TYPES.update(derived)
    p = pending_patch(registry)
    return {"installed": True, "relation_types": len(derived),
            "schema_version": registry.current_version(),
            "registry_migrated": not any(p.values()), "pending": p}


def check(registry) -> list[str]:
    """CI 一致性断言：Registry ↔ graph 运行时 ↔ SHACL 端点。"""
    from app.modules.ontology import graph, shacl
    errs = []
    schema = registry.current()
    p = pending_patch(registry)
    if any(p.values()):
        errs.append(f"Registry 尚未并入内置类型目录：{p}（运行 type_registry.publish_patch）")
    want = derive_relation_types(registry_patch(schema, _builtin()))
    have = graph.RELATION_TYPES
    for k in sorted(set(want) ^ set(have)):
        errs.append(f"关系 {k} 只存在于 {'Registry' if k in want else 'graph'}")
    for k in set(want) & set(have):
        if want[k] != have[k]:
            errs.append(f"关系 {k} 定义不一致：registry={want[k]} graph={have[k]}")
    ends = (shacl.shapes_from_schema(schema).get("relation_endpoints") or {})
    for k in want:
        if k not in ends:
            errs.append(f"SHACL 缺少关系端点 {k}")
    return errs
