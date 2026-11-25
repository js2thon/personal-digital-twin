"""本体运行时类型目录适配器；不依赖问答或 CQ Registry。"""
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


def _as_list(value):
    if isinstance(value, list):
        return value
    return [value] if value else ["*"]


def registry_patch(current: dict, graph_types: dict) -> dict:
    """将图运行时关系目录并入本体 Schema 草案（只增不删）。"""
    import copy
    out = copy.deepcopy(current)
    entity_types = out.setdefault("entity_types", {})
    for name, meta in EXTRA_TYPES.items():
        entity_types.setdefault(name, meta)
    relation_types = out.setdefault("relation_types", {})
    for name, meta in graph_types.items():
        sources, targets = _as_list(meta.get("source")), _as_list(meta.get("target"))
        if name in relation_types:
            current_meta = relation_types[name]
            current_meta.setdefault("family", meta.get("family"))
            for key, single, extra in (("source_types", "source", sources), ("target_types", "target", targets)):
                mine = current_meta.get(key) or _as_list(current_meta.get(single))
                current_meta[key] = ["*"] if "*" in mine or "*" in extra else sorted(set(mine) | set(extra))
        else:
            relation_types[name] = {
                "label": meta.get("label", name), "family": meta.get("family", "other"),
                "source": sources[0] if len(sources) == 1 else "*",
                "target": targets[0] if len(targets) == 1 else "*",
                "source_types": sources, "target_types": targets,
                "cardinality": GRAPH_TO_CARD.get(meta.get("cardinality"), "many_to_many"),
            }
    for name, fix in FIXES.items():
        if name in relation_types:
            relation_types[name].update(fix)
            relation_types[name]["target_types"] = [fix["target"]]
    return out


def descendants(schema: dict, type_name: str) -> set[str]:
    entity_types = schema.get("entity_types") or {}
    result = {type_name}
    changed = True
    while changed:
        changed = False
        for name, meta in entity_types.items():
            if name not in result and (meta or {}).get("parent") in result:
                result.add(name)
                changed = True
    return result


def derive_relation_types(schema: dict) -> dict:
    """按对象继承关系展开关系端点类型，供图投影和 SHACL 使用。"""
    result = {}
    for name, relation in (schema.get("relation_types") or {}).items():
        def expand(key, single):
            types = relation.get(key) or _as_list(relation.get(single))
            if "*" in types:
                return ["*"]
            return sorted(set().union(*[descendants(schema, item) for item in types]))
        result[name] = {
            "label": relation.get("label") or name,
            "family": relation.get("family") or "other",
            "source": expand("source_types", "source"),
            "target": expand("target_types", "target"),
            "cardinality": CARD_TO_GRAPH.get(relation.get("cardinality"), "many"),
        }
    return result
