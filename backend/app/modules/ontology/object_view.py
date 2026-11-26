"""对象驱动本体视图：把 schema / automodel 草稿聚合为 ObjectType 目录。

无已发布 schema 时提供 demo 域，保证 Studio 可独立演示四列画布。
"""
from __future__ import annotations

from typing import Any


def demo_catalog() -> dict[str, Any]:
    """医院 + 报警 示例域（对齐设计稿与 Onto 风格截图）。"""
    return {
        "source": "demo",
        "items": [
            _obj(
                "Hospital",
                "医院信息",
                "医疗机构主数据",
                domain="医疗",
                identity=["hospital_id"],
                properties={
                    "hospital_id": _prop("医院标识", "String", "identity",
                                         source={"table": "hospitals", "column": "id"}),
                    "name": _prop("名称", "String", "text",
                                  source={"table": "hospitals", "column": "name"}),
                    "region": _prop("区域", "String", "enum"),
                },
                relations={
                    "HOSPITAL_HAS_DEPARTMENT": _rel(
                        "医院拥有科室", "Department", "1:N",
                        semantic="医院与科室的隶属关系，非协作关系"),
                },
                functions={
                    "dept_count": _fn("科室数量", "aggregate", "Count", "—", "Integer"),
                },
                actions={},
            ),
            _obj(
                "Department",
                "科室",
                "医院内业务科室",
                domain="医疗",
                identity=["department_id"],
                properties={
                    "department_id": _prop("科室标识", "String", "identity",
                                           source={"table": "departments", "column": "id"}),
                    "name": _prop("科室名称", "String", "text"),
                    "hospital_id": _prop("所属医院", "String", "fk",
                                         source={"table": "departments", "column": "hospital_id"}),
                },
                relations={
                    "DEPARTMENT_USES_MATERIAL": _rel(
                        "科室使用耗材", "MaterialUsage", "1:N"),
                },
                functions={},
                actions={},
            ),
            _obj(
                "Alarm",
                "报警事件",
                "设备/工艺报警，可升级为工单",
                domain="智能控制",
                identity=["alarm_no"],
                properties={
                    "alarm_no": _prop("报警编号", "String", "identity"),
                    "alarm_type": _prop("报警类型", "String", "enum"),
                    "zone_name": _prop("关联分区", "String", "text"),
                    "occur_ms": _prop("发生时刻毫秒", "Long", "timestamp"),
                    "recover_ms": _prop("恢复时刻毫秒", "Long", "timestamp"),
                    "dur_min": _prop("持续分钟", "Double", "measure"),
                    "root_cause": _prop("根因分类", "String", "enum"),
                    "status_text": _prop("状态文本", "String", "text"),
                    "escalate_confirm": _prop("升级派单确认位", "Double", "measure"),
                    "escalate_ceiling": _prop("升级派单确认门槛", "Double", "measure"),
                },
                relations={
                    "ESCALATES_TO": _rel("升级为工单", "WorkOrder", "0:1"),
                },
                functions={
                    "last_occur": _fn("最近发生时刻", "aggregate", "OrderedLast",
                                      "occur_ms", "Long"),
                    "max_recover": _fn("最晚恢复时刻", "aggregate", "Max",
                                       "recover_ms", "Long"),
                    "duration_calc": _fn("持续分钟(派生)", "derived",
                                         "(recover_ms-occur_ms)/60000",
                                         "occur_ms,recover_ms", "Double"),
                },
                actions={
                    "escalate": {
                        "label": "报警升级派单",
                        "params": ["level", "assignee"],
                        "effects": [
                            {"type": "create_object", "object": "WorkOrder"},
                            {"type": "write_relation", "relation": "ESCALATES_TO"},
                        ],
                        "policy": "approval_required",
                    },
                    "create_work_order": {
                        "label": "检修工单",
                        "params": ["priority"],
                        "effects": [{"type": "create_object", "object": "WorkOrder"}],
                        "policy": "open",
                    },
                },
            ),
            _obj(
                "WorkOrder",
                "检修工单",
                "由报警或其他事件派生的行动对象",
                domain="智能控制",
                identity=["work_order_id"],
                properties={
                    "work_order_id": _prop("工单号", "String", "identity"),
                    "status": _prop("状态", "String", "enum"),
                    "priority": _prop("优先级", "String", "enum"),
                },
                relations={},
                functions={},
                actions={},
            ),
        ],
    }


def _obj(id_: str, label: str, description: str, *, domain: str, identity: list,
         properties: dict, relations: dict, functions: dict, actions: dict) -> dict:
    unbound = sum(1 for p in properties.values() if not p.get("source"))
    return {
        "id": id_,
        "label": label,
        "description": description,
        "domain": domain,
        "identity": identity,
        "properties": properties,
        "relations": relations,
        "functions": functions,
        "actions": actions,
        "quality": {
            "property_count": len(properties),
            "relation_count": len(relations),
            "function_count": len(functions),
            "action_count": len(actions),
            "unbound_properties": unbound,
        },
    }


def _prop(label: str, value_type: str, role: str, source: dict | None = None) -> dict:
    d: dict[str, Any] = {"label": label, "value_type": value_type, "role": role}
    if source:
        d["source"] = source
    return d


def _rel(label: str, target: str, cardinality: str, semantic: str | None = None) -> dict:
    d: dict[str, Any] = {"label": label, "target": target, "cardinality": cardinality}
    if semantic:
        d["semantic"] = semantic
    return d


def _fn(label: str, kind: str, op_or_expr: str, on: str, output_type: str) -> dict:
    return {
        "label": label,
        "kind": kind,
        "op" if kind == "aggregate" else "expr": op_or_expr,
        "on": on,
        "output_type": output_type,
    }


def from_schema(schema: dict | None) -> dict[str, Any] | None:
    """从 ontology schema_content / automodel draft.schema 聚合对象目录。"""
    if not schema or not isinstance(schema, dict):
        return None
    entity_types = schema.get("entity_types") or schema.get("classes") or {}
    if not entity_types:
        return None
    attributes = schema.get("attributes") or {}
    relation_types = schema.get("relation_types") or {}
    # metrics may live outside schema; ignore if absent
    items = []
    for key, ent in entity_types.items():
        if not isinstance(ent, dict):
            ent = {"label": str(ent)}
        eid = str(key)
        props: dict[str, Any] = {}
        for pk, pv in attributes.items():
            if not isinstance(pv, dict):
                continue
            owner = str(pv.get("owner") or pv.get("domain") or "")
            if owner and owner != eid:
                continue
            if not owner and pk not in (ent.get("key") or []):
                # 无 owner 时仅挂到带同名 key 的对象，避免全量复制
                continue
            props[str(pk)] = {
                "label": pv.get("label") or pk,
                "value_type": pv.get("range") or pv.get("value_type") or "String",
                "role": pv.get("role") or "text",
            }
            if pv.get("source") or pv.get("column"):
                props[str(pk)]["source"] = pv.get("source") or {
                    "table": pv.get("table"), "column": pv.get("column"),
                }
        # identity from key
        for k in ent.get("key") or ent.get("identity") or []:
            ks = str(k)
            props.setdefault(ks, {
                "label": ks, "value_type": "String", "role": "identity",
            })
        rels: dict[str, Any] = {}
        for rk, rv in relation_types.items():
            if not isinstance(rv, dict):
                continue
            src = str(rv.get("source") or rv.get("domain") or "")
            if src and src != eid:
                continue
            if not src:
                continue
            rels[str(rk)] = {
                "label": rv.get("label") or rk,
                "target": rv.get("target") or rv.get("range") or "",
                "cardinality": rv.get("cardinality") or "1:N",
            }
        items.append(_obj(
            eid,
            str(ent.get("label") or eid),
            str(ent.get("description") or ""),
            domain=str(ent.get("domain") or ent.get("source_table") or ""),
            identity=[str(x) for x in (ent.get("key") or ent.get("identity") or [])],
            properties=props,
            relations=rels,
            functions={},
            actions={},
        ))
    if not items:
        return None
    return {"source": "schema", "items": items}


def list_objects(*, schema: dict | None = None, force_demo: bool = False) -> dict[str, Any]:
    if force_demo:
        return demo_catalog()
    built = from_schema(schema)
    return built if built else demo_catalog()


def get_object(object_id: str, *, schema: dict | None = None,
               force_demo: bool = False) -> dict[str, Any] | None:
    cat = list_objects(schema=schema, force_demo=force_demo)
    oid = str(object_id)
    for it in cat["items"]:
        if it["id"] == oid or it.get("label") == oid:
            return {**it, "source": cat["source"]}
    return None


def object_graph_payload(object_id: str, *, schema: dict | None = None,
                         force_demo: bool = False) -> dict[str, Any] | None:
    """四列画布数据：center + properties/types/functions/actions 节点与边。"""
    obj = get_object(object_id, schema=schema, force_demo=force_demo)
    if not obj:
        return None
    props, types, fns, acts, edges = [], [], [], [], []
    type_seen: set[str] = set()
    for name, p in (obj.get("properties") or {}).items():
        pid = f"prop:{name}"
        props.append({
            "id": pid, "name": name, "label": p.get("label") or name,
            "role": p.get("role"), "value_type": p.get("value_type"),
            "source": p.get("source"),
        })
        edges.append({"from": "object", "to": pid, "kind": "owns"})
        vt = str(p.get("value_type") or "String")
        tid = f"type:{vt}"
        if vt not in type_seen:
            type_seen.add(vt)
            types.append({"id": tid, "label": vt})
        edges.append({"from": pid, "to": tid, "kind": "typed"})
    for name, f in (obj.get("functions") or {}).items():
        fid = f"fn:{name}"
        fns.append({
            "id": fid, "name": name, "label": f.get("label") or name,
            "kind": f.get("kind"), "op": f.get("op") or f.get("expr"),
            "on": f.get("on"), "output_type": f.get("output_type"),
        })
        # link from dependency properties when possible
        on = str(f.get("on") or "")
        linked = False
        for part in on.replace(" ", "").split(","):
            if part and f"prop:{part}" in {p["id"] for p in props}:
                edges.append({"from": f"prop:{part}", "to": fid, "kind": "derives"})
                linked = True
        if not linked:
            edges.append({"from": "object", "to": fid, "kind": "owns"})
        ot = f.get("output_type")
        if ot:
            tid = f"type:{ot}"
            if ot not in type_seen:
                type_seen.add(str(ot))
                types.append({"id": tid, "label": str(ot)})
            edges.append({"from": fid, "to": tid, "kind": "returns"})
    for name, a in (obj.get("actions") or {}).items():
        aid = f"act:{name}"
        acts.append({
            "id": aid, "name": name, "label": a.get("label") or name,
            "policy": a.get("policy"), "effects": a.get("effects") or [],
            "params": a.get("params") or [],
        })
        edges.append({"from": "object", "to": aid, "kind": "action"})
    return {
        "object": {
            "id": obj["id"], "label": obj["label"],
            "description": obj.get("description"), "domain": obj.get("domain"),
            "quality": obj.get("quality"),
        },
        "columns": {
            "properties": props,
            "types": types,
            "functions": fns,
            "actions": acts,
        },
        "edges": edges,
        "source": obj.get("source"),
    }
