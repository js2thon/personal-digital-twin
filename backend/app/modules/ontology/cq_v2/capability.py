"""四层结构第 1/3 层：Business Capability 与 Ontology Requirements。

Business Capability → CQ（多对多）→ Ontology Requirements → Knowledge & Evidence。
一个能力对应多条 CQ，一个本体概念可被多个能力复用。
"""
from __future__ import annotations

from . import models as m


def derive_requirements(cq: dict) -> list[dict]:
    """从一条 CQ 推导本体需求项（写入 ontology.cq_requirements）。"""
    reqs: list[dict] = []
    ver = int(cq.get("version") or 1)

    def add(kind: str, ref: str):
        reqs.append({"id": m.uid(), "cq_id": cq.get("id"), "cq_version": ver,
                     "kind": kind, "ref": ref,
                     "schema_version": cq.get("schema_version", ""),
                     "mandatory": True, "status": "open", "created_at": m.now()})

    if cq.get("subject_type"):
        add("entity", cq["subject_type"])
    if cq.get("target_type"):
        add("entity", cq["target_type"])
    for rel in cq.get("required_relations") or []:
        add("relation", rel)
    for attr in cq.get("required_attributes") or []:
        add("attribute", attr)
    for rule in (m.as_dict(cq.get("acceptance_criteria")).get("rules") or []):
        add("rule", str(rule))
    if cq.get("evidence_requirements"):
        add("evidence", "min_evidence:%s" % (cq.get("min_evidence") or 1))
    for src in (m.as_dict(cq.get("scope_constraints")).get("sources") or []):
        add("source", str(src))
    return reqs


def link(capabilities: list[dict], cqs: list[dict],
         pairs: list[tuple[str, str, float]] | None = None) -> list[dict]:
    """CQ 与能力多对多。pairs = [(capability_id, cq_id, weight), ...]"""
    cap_ids = {c["id"] for c in capabilities}
    cq_ids = {q["id"] for q in cqs}
    out = []
    for cap_id, cq_id, weight in (pairs or []):
        if cap_id in cap_ids and cq_id in cq_ids:
            out.append({"capability_id": cap_id, "cq_id": cq_id,
                        "link_weight": float(weight or 1.0), "created_at": m.now()})
    return out


def capability_coverage(capabilities: list[dict], links: list[dict],
                        cq_states: dict[str, str]) -> list[dict]:
    """按能力聚合覆盖率；高风险能力单独算，不被大量简单查询拉高。"""
    by_cap: dict[str, list[str]] = {}
    for lk in links:
        by_cap.setdefault(lk["capability_id"], []).append(lk["cq_id"])
    out = []
    for cap in capabilities:
        ids = by_cap.get(cap["id"], [])
        states = [cq_states.get(i, "unanswerable") for i in ids]
        answered = sum(1 for s in states if s == "answered")
        ratio = round(answered / len(states), 3) if states else 0.0
        out.append({
            "capability_id": cap["id"], "code": cap.get("code", ""),
            "name": cap.get("name", ""), "risk_level": cap.get("risk_level", "normal"),
            "cq_count": len(ids), "answered": answered,
            "coverage": ratio,
            "has_conflict": any(s == "conflict" for s in states),
            "has_restricted": any(s == "restricted" for s in states),
        })
    return out


def high_risk_gate(cap_rows: list[dict], evaluation_rows: list[dict],
                   min_accuracy: float = 0.95, min_evidence: float = 0.90) -> dict:
    """核心/高风险 CQ 的独立准确率与证据门禁。"""
    high = [c for c in cap_rows if c["risk_level"] in ("high", "critical")]
    high_ids = {c["capability_id"] for c in high}
    rows = [r for r in evaluation_rows if r.get("capability_id") in high_ids]
    acc = (sum(float(r.get("accuracy") or 0) for r in rows) / len(rows)) if rows else 0.0
    ev = (sum(float(r.get("evidence_rate") or 0) for r in rows) / len(rows)) if rows else 0.0
    return {
        "high_risk_capabilities": len(high), "samples": len(rows),
        "accuracy": round(acc, 3), "evidence_rate": round(ev, 3),
        "min_accuracy": min_accuracy, "min_evidence": min_evidence,
        "passed": (not high) or (len(rows) > 0 and acc >= min_accuracy and ev >= min_evidence),
    }


def four_layer_view(capabilities: list[dict], cqs: list[dict],
                    requirements: list[dict], evidence: list[dict]) -> dict:
    """四层结构的可视化数据（能力地图）。"""
    return {
        "layers": [
            {"name": "Business Capability", "count": len(capabilities),
             "note": "数字员工业务能力：项目风险识别、审批判断、人员查询"},
            {"name": "Competency Questions", "count": len(cqs),
             "note": "业务能力需要回答哪些问题"},
            {"name": "Ontology Requirements", "count": len(requirements),
             "note": "需要哪些实体、属性、关系、规则"},
            {"name": "Knowledge & Evidence", "count": len(evidence),
             "note": "需要哪些事实、证据、数据源"},
        ],
        "reuse": len({r["ref"] for r in requirements}) and
                 round(len(requirements) / max(1, len({r["ref"] for r in requirements})), 2),
    }
