"""本体工作台（对齐 ontology_studio_v2）：Schema/实例图谱、路径与邻居、质量检查、对象 Action、JSON/OWL 导出。

全部基于 automodel 任务的物化结果（entities + facts）与本体草案，不额外存储；
Action 修改的是任务内存中的物化结果，需再次「写入」才会落库（与 v2 的会话内编辑一致）。
"""
from __future__ import annotations

import json
import math
import time
import uuid
from collections import deque

from . import graphqa as gq


def _f(v):
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


# ------------------------------------------------------------------ 概览 / 对象表
def overview(job: dict) -> dict:
    s, mat = job["draft"]["schema"], job["materialized"]
    by_cls: dict[str, int] = {}
    for e in mat["entities"]:
        by_cls[e["entity_type"]] = by_cls.get(e["entity_type"], 0) + 1
    rel_n: dict[str, int] = {}
    for f in mat["facts"]:
        if "external_id" in f["object_value"]:
            rel_n[f["predicate"]] = rel_n.get(f["predicate"], 0) + 1
    return {
        "object_types": [{"name": c, "label": v.get("label") or c, "count": by_cls.get(c, 0),
                          "primary_key": ",".join(v.get("key") or []) or None,
                          "source_table": v.get("source_table"),
                          "properties": [a for a, m in s["attributes"].items() if m["owner"] == c]}
                         for c, v in s["entity_types"].items()],
        "link_types": [{"name": r, "label": v.get("label") or r, "source": v["source"], "target": v["target"],
                        "cardinality": v.get("cardinality"), "via": v.get("column") or v.get("via_table"),
                        "count": rel_n.get(r, 0)} for r, v in s["relation_types"].items()],
        "nodes": len(mat["entities"]), "edges": sum(rel_n.values()),
        "actions": len(job.get("actions_log") or []),
    }


def objects(job: dict, cls: str, q: str = "", offset: int = 0, limit: int = 100) -> dict:
    s = job["draft"]["schema"]
    lab = {a: s["attributes"][a].get("label") or a for a in s["attributes"]}
    rows = [e for e in job["materialized"]["entities"] if e["entity_type"] == cls]
    if q:
        rows = [e for e in rows if q.lower() in json.dumps(e, ensure_ascii=False).lower()]
    cols = [a for a, m in s["attributes"].items() if m["owner"] == cls]
    return {"total": len(rows), "columns": [{"key": a, "label": lab[a]} for a in cols],
            "rows": [{"_id": e["external_id"], "_name": e["canonical_name"],
                      **{a: _f(e["properties"].get(a)) for a in cols}} for e in rows[offset:offset + limit]]}


# ------------------------------------------------------------------ 图
def _edges(job: dict) -> list[tuple[str, str, str]]:
    return [(f["subject_external_id"], f["predicate"], f["object_value"]["external_id"])
            for f in job["materialized"]["facts"] if "external_id" in f["object_value"]]


def schema_graph(job: dict, with_props: bool = True) -> dict:
    s = job["draft"]["schema"]
    ov = overview(job)
    cnt = {o["name"]: o["count"] for o in ov["object_types"]}
    nodes = [{"id": f"CLASS:{c}", "label": v.get("label") or c, "kind": "class", "group": c,
              "title": f"{c}｜{cnt.get(c, 0)} 个实例｜主键 {','.join(v.get('key') or []) or '—'}"}
             for c, v in s["entity_types"].items()]
    edges = [{"from": f"CLASS:{v['source']}", "to": f"CLASS:{v['target']}", "label": v.get("label") or r,
              "kind": "object_property", "title": f"{r}｜{v.get('cardinality')}"}
             for r, v in s["relation_types"].items()]
    if with_props:
        for a, m in s["attributes"].items():
            nodes.append({"id": f"PROP:{m['owner']}:{a}", "label": m.get("label") or a, "kind": "property",
                          "group": m["owner"], "title": f"{a}｜{m.get('range')}｜{m.get('role')}"})
            edges.append({"from": f"CLASS:{m['owner']}", "to": f"PROP:{m['owner']}:{a}",
                          "label": "hasProperty", "kind": "has_property"})
    return {"nodes": nodes, "edges": edges}


def instance_graph(job: dict, types: list[str] | None = None, q: str = "", focus: str | None = None,
                   hops: int = 1, max_nodes: int = 300) -> dict:
    s = job["draft"]["schema"]
    ents = {e["external_id"]: e for e in job["materialized"]["entities"]}
    types = set(types or s["entity_types"])
    E = _edges(job)
    adj: dict[str, set] = {}
    for a, _, b in E:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    if focus and focus in ents:
        seen, frontier = {focus}, {focus}
        for _ in range(max(1, min(3, hops))):
            nxt = set().union(*[adj.get(n, set()) for n in frontier]) - seen
            seen |= nxt
            frontier = nxt
        cand = [n for n in seen if ents.get(n, {}).get("entity_type") in types]
    else:
        ql = q.strip().lower()
        cand = [eid for eid, e in ents.items() if e["entity_type"] in types and
                (not ql or ql in json.dumps(e, ensure_ascii=False).lower())]
    truncated = len(cand) > max_nodes
    chosen = set(cand[:max_nodes]) | ({focus} if focus in ents else set())
    rl = {k: v.get("label") or k for k, v in s["relation_types"].items()}
    nodes = [{"id": eid, "label": gq._norm(ents[eid]["canonical_name"]), "group": ents[eid]["entity_type"],
              "title": ents[eid]["entity_type"] + "｜" + "；".join(
                  f"{k}:{_f(v)}" for k, v in list(ents[eid]["properties"].items())[:12]),
              "focus": eid == focus} for eid in chosen]
    edges = [{"from": a, "to": b, "label": rl.get(r, r)} for a, r, b in E if a in chosen and b in chosen]
    return {"nodes": nodes, "edges": edges, "truncated": truncated, "total_nodes": len(ents),
            "total_edges": len(E)}


def neighbors(job: dict, eid: str) -> dict:
    s = job["draft"]["schema"]
    ents = {e["external_id"]: e for e in job["materialized"]["entities"]}
    rl = {k: v.get("label") or k for k, v in s["relation_types"].items()}
    out = []
    for a, r, b in _edges(job):
        if a == eid and b in ents:
            out.append({"entity": b, "name": gq._norm(ents[b]["canonical_name"]), "type": ents[b]["entity_type"],
                        "relation": rl.get(r, r), "direction": "由当前实体指向"})
        elif b == eid and a in ents:
            out.append({"entity": a, "name": gq._norm(ents[a]["canonical_name"]), "type": ents[a]["entity_type"],
                        "relation": rl.get(r, r), "direction": "指向当前实体"})
    return {"entity": eid, "count": len(out), "items": out}


def shortest_path(job: dict, src: str, dst: str, directed: bool = False) -> dict:
    s = job["draft"]["schema"]
    ents = {e["external_id"]: e for e in job["materialized"]["entities"]}
    rl = {k: v.get("label") or k for k, v in s["relation_types"].items()}
    adj: dict[str, list] = {}
    for a, r, b in _edges(job):
        adj.setdefault(a, []).append((b, rl.get(r, r), "→"))
        if not directed:
            adj.setdefault(b, []).append((a, rl.get(r, r), "←"))
    prev, dq = {src: None}, deque([src])
    while dq:
        n = dq.popleft()
        if n == dst:
            break
        for m, r, d in adj.get(n, []):
            if m not in prev:
                prev[m] = (n, r, d)
                dq.append(m)
    if dst not in prev:
        return {"found": False}
    steps, cur = [], dst
    while prev[cur]:
        p, r, d = prev[cur]
        steps.append({"from": gq._norm(ents[p]["canonical_name"]), "relation": r, "direction": d,
                      "to": gq._norm(ents[cur]["canonical_name"]), "from_id": p, "to_id": cur})
        cur = p
    steps.reverse()
    return {"found": True, "hops": len(steps), "steps": steps,
            "path": [src] + [x["to_id"] for x in steps]}


# ------------------------------------------------------------------ 质量
def quality(job: dict) -> dict:
    """Deterministic governance report. Scores only checks backed by task data."""
    mat, s = job["materialized"], job["draft"]["schema"]
    entities, facts = mat.get("entities") or [], mat.get("facts") or []
    ids = {e["external_id"] for e in entities}
    E = _edges(job)
    linked = {a for a, _, _ in E} | {b for _, _, b in E}
    seen, dup = set(), []
    for edge in E:
        (dup.append(edge) if edge in seen else seen.add(edge))
    dangling = [{"relation": f["predicate"], "source": f["subject_external_id"],
                 "target_value": f["object_value"]["external_id"]}
                for f in mat.get("dangling") or []]
    invalid = [r for r, v in s["relation_types"].items()
               if v.get("source") not in s["entity_types"] or v.get("target") not in s["entity_types"]]
    loops = [{"source": a, "relation": r} for a, r, b in E if a == b]
    duplicates = [{"source": a, "relation": r, "target": b} for a, r, b in dup]
    issues = list(job.get("issues") or [])
    pending = list(job.get("draft", {}).get("review") or [])
    mapped = {a for m in (job.get("contract") or {}).get("mappings", [])
              for a in (m.get("attributes") or {})}
    all_attrs = set(s.get("attributes") or {})
    mapping_coverage = 1.0 if not all_attrs else len(mapped & all_attrs) / len(all_attrs)
    dangling_rate = len(dangling) / max(1, len(E))
    duplicate_rate = len(duplicates) / max(1, len(E))
    error_count = sum(1 for x in issues if str(x.get("level", "")).lower() == "error")
    warning_count = sum(1 for x in issues if str(x.get("level", "")).lower() != "error")
    penalties = {
        "contract_errors": min(40, error_count * 10),
        "dangling_references": min(25, round(dangling_rate * 100)),
        "duplicate_edges": min(10, round(duplicate_rate * 100)),
        "invalid_relations": min(20, len(invalid) * 10),
        "self_loops": min(10, len(loops) * 5),
        "unmapped_properties": round((1 - mapping_coverage) * 15),
        "pending_reviews": min(10, len(pending) * 2),
    }
    score = max(0, min(100, 100 - sum(penalties.values())))
    findings = []
    def finding(code, level, count, message):
        if count:
            findings.append({"code": code, "level": level, "count": count, "message": message})
    finding("CONTRACT_ERROR", "error", error_count, "映射契约存在阻断性错误")
    finding("DANGLING_REFERENCE", "error", len(dangling), "关系事实引用了不存在的目标实体")
    finding("INVALID_RELATION", "error", len(invalid), "关系端点类型未在本体中定义")
    finding("DUPLICATE_EDGE", "warning", len(duplicates), "存在重复关系事实")
    finding("SELF_LOOP", "warning", len(loops), "存在自环关系")
    finding("UNMAPPED_PROPERTY", "warning", len(all_attrs - mapped), "部分本体属性未映射到来源字段")
    finding("REVIEW_PENDING", "warning", len(pending), "仍有待人工确认的建模候选")
    return {
        "quality_score": score,
        "score_label": "excellent" if score >= 90 else "good" if score >= 75 else "needs_attention" if score >= 60 else "critical",
        "score_method": "100 - deterministic issue penalties; not an ML confidence score",
        "score_breakdown": penalties,
        "coverage": {"mapped_properties": len(mapped & all_attrs), "total_properties": len(all_attrs),
                     "mapping_ratio": round(mapping_coverage, 4)},
        "node_count": len(ids), "edge_count": len(E), "fact_count": len(facts),
        "isolated_nodes": sorted(ids - linked)[:500],
        "dangling_references": dangling[:500], "invalid_relation_types": invalid,
        "self_loops": loops[:200], "duplicate_edges": duplicates[:200],
        "contract_issues": issues, "review_pending": pending,
        "findings": findings, "warning_count": warning_count,
    }


def provenance(job: dict, entity: str, predicate: str | None = None, limit: int = 200) -> dict:
    """Return persisted in-task fact lineage without guessing missing source coordinates."""
    entities = {e["external_id"]: e for e in (job.get("materialized") or {}).get("entities", [])}
    target = entities.get(entity)
    if target is None:
        raise LookupError("实例不存在")
    schema = job["draft"]["schema"]
    labels = {k: v.get("label") or k for k, v in schema.get("attributes", {}).items()}
    rows = []
    for fact in (job.get("materialized") or {}).get("facts", []):
        if fact.get("subject_external_id") != entity:
            continue
        pred = fact.get("predicate")
        if predicate and pred != predicate:
            continue
        ref = fact.get("source_ref") or {}
        rows.append({"entity_id": entity, "entity_name": target.get("canonical_name"),
                     "entity_type": target.get("entity_type"), "predicate": pred,
                     "property_label": labels.get(pred, pred), "value": _f(fact.get("object_value", {}).get("value")),
                     "target_entity": fact.get("object_value", {}).get("external_id"),
                     "source_ref": ref, "origin": ref.get("origin"),
                     "source": ref.get("source"), "source_table": ref.get("table"),
                     "source_column": ref.get("column"), "contract_version": ref.get("contract_version"),
                     "trust_tier": fact.get("trust_tier"),
                     "verification_status": fact.get("verification_status")})
    return {"entity": {"id": entity, "name": target.get("canonical_name"),
                       "type": target.get("entity_type")},
            "items": rows[:max(1, min(limit, 500))], "total": len(rows),
            "lineage_completeness": round(sum(1 for x in rows if x.get("source_table")) / len(rows), 4) if rows else 0,
            "note": "仅返回事实中实际持久化的 source_ref；未存储的源字段坐标不会推断。"}


def source_provenance(job: dict, table: str, column: str, limit: int = 200) -> dict:
    """Reverse lookup: source table/column to ontology facts and instances."""
    entities = {e["external_id"]: e for e in (job.get("materialized") or {}).get("entities", [])}
    rows = []
    for fact in (job.get("materialized") or {}).get("facts", []):
        ref = fact.get("source_ref") or {}
        if ref.get("table") != table or ref.get("column") != column:
            continue
        entity = entities.get(fact.get("subject_external_id"), {})
        rows.append({"entity_id": fact.get("subject_external_id"),
                     "entity_name": entity.get("canonical_name"),
                     "entity_type": entity.get("entity_type"),
                     "predicate": fact.get("predicate"),
                     "value": _f(fact.get("object_value", {}).get("value")),
                     "target_entity": fact.get("object_value", {}).get("external_id"),
                     "source_ref": ref, "trust_tier": fact.get("trust_tier")})
    return {"source": {"table": table, "column": column}, "items": rows[:max(1, min(limit, 500))],
            "total": len(rows), "note": "反向追踪仅匹配事实中已持久化的 source_ref.table 与 source_ref.column。"}


def governance(job: dict, principal: dict | None = None) -> dict:
    """Read-only governance snapshot; authorization remains enforced by the API layer."""
    q = quality(job)
    pending = q["review_pending"]
    blockers = [x for x in q["findings"] if x["level"] == "error"]
    principal = principal or {}
    return {
        "asset": {"job_id": job.get("id"), "status": job.get("status"),
                  "schema_version": job.get("schema_version"), "published": job.get("status") in ("published", "written")},
        "quality": {"score": q["quality_score"], "label": q["score_label"],
                    "blockers": len(blockers), "warnings": q["warning_count"]},
        "review": {"pending": len(pending), "required": bool(pending)},
        "release_gate": {"ready": not blockers and not pending,
                         "reasons": [x["message"] for x in blockers] +
                                    (["仍有待审核候选"] if pending else [])},
        "access": {"authenticated_principal": bool(principal),
                   "tenant_scoped_lookup": True, "authorization_enforced_by": "backend",
                   "client_write_enabled": False},
        "audit": {"action_events": len(job.get("actions_log") or []),
                  "last_action": (job.get("actions_log") or [])[-1] if job.get("actions_log") else None},
        "limitations": ["权限矩阵与角色策略未由当前 API 暴露；此处不推断用户角色。"],
    }


# ------------------------------------------------------------------ Action
def _log(job, action, cls, eid, **kw):
    job.setdefault("actions_log", []).append({"action": action, "type": cls, "pk": eid,
                                              "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **kw})


def _pk_template(job, cls):
    m = next((x for x in job["contract"]["mappings"] if x["class"] == cls), None)
    return m


def create_object(job: dict, cls: str, data: dict) -> str:
    s = job["draft"]["schema"]
    if cls not in s["entity_types"]:
        raise ValueError(f"类型 {cls} 不存在")
    m = _pk_template(job, cls)
    key = [k for k in (s["entity_types"][cls].get("key") or [])]
    pk = str(data.get(key[0])) if key and data.get(key[0]) not in (None, "") else uuid.uuid4().hex[:8]
    if key:
        data[key[0]] = pk
    eid = f"{job['source']}:{m['table'] if m else cls}:{pk}"
    if any(e["external_id"] == eid for e in job["materialized"]["entities"]):
        raise ValueError(f"主键 {pk} 已存在")
    own = {a for a, mm in s["attributes"].items() if mm["owner"] == cls}
    props = {k: v for k, v in data.items() if k in own and v not in (None, "")}
    name_col = s["entity_types"][cls].get("name_column")
    job["materialized"]["entities"].append({"external_id": eid, "entity_type": cls,
                                            "canonical_name": str(props.get(name_col) or pk),
                                            "properties": props})
    ref = {"origin": "studio_action", "source": job["source"]}
    for a, v in props.items():
        job["materialized"]["facts"].append({"subject_external_id": eid, "predicate": a,
                                             "object_value": {"value": v}, "trust_tier": 3, "source_ref": ref})
    _log(job, "create", cls, eid, data=props)
    return eid


def update_object(job: dict, eid: str, updates: dict) -> dict:
    s = job["draft"]["schema"]
    e = next((x for x in job["materialized"]["entities"] if x["external_id"] == eid), None)
    if not e:
        raise LookupError("对象不存在")
    key = s["entity_types"][e["entity_type"]].get("key") or []
    if any(k in updates and str(updates[k]) != str(e["properties"].get(k)) for k in key):
        raise ValueError("不允许通过更新操作修改主键")
    own = {a for a, mm in s["attributes"].items() if mm["owner"] == e["entity_type"]}
    clean = {k: (None if v == "" else v) for k, v in updates.items() if k in own}
    e["properties"].update({k: v for k, v in clean.items() if v is not None})
    for k, v in clean.items():
        if v is None:
            e["properties"].pop(k, None)
    name_col = s["entity_types"][e["entity_type"]].get("name_column")
    if name_col in clean and clean[name_col]:
        e["canonical_name"] = str(clean[name_col])
    facts = job["materialized"]["facts"]
    facts[:] = [f for f in facts if not (f["subject_external_id"] == eid and f["predicate"] in clean
                                         and "value" in f["object_value"])]
    for k, v in clean.items():
        if v is not None:
            facts.append({"subject_external_id": eid, "predicate": k, "object_value": {"value": v},
                          "trust_tier": 3, "source_ref": {"origin": "studio_action", "source": job["source"]}})
    _log(job, "update", e["entity_type"], eid, updates=clean)
    return e


def delete_object(job: dict, eid: str) -> int:
    mat = job["materialized"]
    e = next((x for x in mat["entities"] if x["external_id"] == eid), None)
    if not e:
        raise LookupError("对象不存在")
    mat["entities"] = [x for x in mat["entities"] if x["external_id"] != eid]
    before = len(mat["facts"])
    mat["facts"] = [f for f in mat["facts"] if f["subject_external_id"] != eid
                    and f["object_value"].get("external_id") != eid]
    _log(job, "delete", e["entity_type"], eid)
    return before - len(mat["facts"])


# ------------------------------------------------------------------ 导出
def export_json(job: dict) -> dict:
    s, mat = job["draft"]["schema"], job["materialized"]
    return {"object_types": s["entity_types"], "properties": s["attributes"], "link_types": s["relation_types"],
            "objects": {c: {e["external_id"]: e["properties"] for e in mat["entities"] if e["entity_type"] == c}
                        for c in s["entity_types"]},
            "links": [{"source": a, "type": r, "target": b} for a, r, b in _edges(job)],
            "data_mapping": job.get("contract"), "actions_log": job.get("actions_log") or [],
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def export_owl(job: dict) -> str:
    from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef
    s, mat = job["draft"]["schema"], job["materialized"]
    g = Graph()
    ex = Namespace(f"http://example.org/ontology/{job['source']}#")
    g.bind("ex", ex)
    g.add((URIRef(str(ex)[:-1]), RDF.type, OWL.Ontology))
    safe = lambda x: "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in str(x))  # noqa: E731
    for c, v in s["entity_types"].items():
        g.add((ex[safe(c)], RDF.type, OWL.Class))
        g.add((ex[safe(c)], RDFS.label, Literal(v.get("label") or c, lang="zh")))
    for a, m in s["attributes"].items():
        p = ex[safe(f"{m['owner']}_{a}")]
        g.add((p, RDF.type, OWL.DatatypeProperty))
        g.add((p, RDFS.domain, ex[safe(m["owner"])]))
        g.add((p, RDFS.label, Literal(m.get("label") or a, lang="zh")))
    for r, v in s["relation_types"].items():
        g.add((ex[safe(r)], RDF.type, OWL.ObjectProperty))
        g.add((ex[safe(r)], RDFS.domain, ex[safe(v["source"])]))
        g.add((ex[safe(r)], RDFS.range, ex[safe(v["target"])]))
        g.add((ex[safe(r)], RDFS.label, Literal(v.get("label") or r, lang="zh")))
    for e in mat["entities"]:
        i = ex[safe(e["external_id"])]
        g.add((i, RDF.type, ex[safe(e["entity_type"])]))
        g.add((i, RDFS.label, Literal(gq._norm(e["canonical_name"]))))
        for k, val in e["properties"].items():
            if val is not None and k in s["attributes"]:
                g.add((i, ex[safe(f"{e['entity_type']}_{k}")], Literal(str(val))))
    for a, r, b in _edges(job):
        g.add((ex[safe(a)], ex[safe(r)], ex[safe(b)]))
    return g.serialize(format="xml")
