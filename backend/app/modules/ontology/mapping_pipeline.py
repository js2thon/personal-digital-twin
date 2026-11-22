"""Deterministic dataset-to-ontology mapping suggestions and validation.

This module deliberately does not execute SQL or write business data. It produces
explainable candidates for human review; a later connector/execution stage can
consume only explicitly approved mappings.
"""
from __future__ import annotations

import re
from typing import Any


def _norm(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _schema_parts(schema: dict | None) -> tuple[list[dict], list[dict]]:
    """Accept SemanticSchema.to_schema_dict shape or a compact JSON schema."""
    schema = schema or {}
    raw_types = schema.get("entity_types") or {}
    types = list(raw_types.values()) if isinstance(raw_types, dict) else list(raw_types)
    entities: list[dict] = []
    attributes: list[dict] = []
    for entity in types:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        label = str(entity.get("label") or name)
        aliases = [str(x) for x in (entity.get("aliases") or [])]
        entities.append({"name": name, "label": label, "aliases": aliases})
        for attr in entity.get("attributes") or []:
            if not isinstance(attr, dict) or not attr.get("name"):
                continue
            attributes.append({
                "entity_type": name,
                "entity_label": label,
                "name": str(attr["name"]),
                "label": str(attr.get("label") or attr.get("description") or attr["name"]),
                "aliases": [str(x) for x in (attr.get("aliases") or [])],
                "range": str(attr.get("range") or "string"),
                "required": bool(attr.get("required")),
                "enum_values": list(attr.get("enum_values") or []),
            })
    # Compact schema form: {"classes":[...], "properties":[...]}
    for entity in schema.get("classes") or []:
        if isinstance(entity, dict) and entity.get("name"):
            entities.append({"name": str(entity["name"]),
                             "label": str(entity.get("label") or entity["name"]),
                             "aliases": list(entity.get("aliases") or [])})
    for prop in schema.get("properties") or []:
        if isinstance(prop, dict) and prop.get("name"):
            attributes.append({
                "entity_type": str(prop.get("domain") or ""),
                "entity_label": str(prop.get("domain") or ""),
                "name": str(prop["name"]),
                "label": str(prop.get("label") or prop.get("description") or prop["name"]),
                "aliases": list(prop.get("aliases") or []),
                "range": str(prop.get("range") or "string"),
                "required": bool(prop.get("required")),
                "enum_values": list(prop.get("enum_values") or []),
            })
    return entities, attributes


def _type_compatible(values: list[Any], target_range: str) -> tuple[float, str]:
    present = [v for v in values if v is not None and str(v).strip() != ""]
    if not present:
        return 0.0, "没有非空样例值，无法判断数据类型"
    def kind(v: Any) -> str:
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, int):
            return "integer"
        if isinstance(v, float):
            return "number"
        s = str(v).strip()
        if re.fullmatch(r"-?\d+", s):
            return "integer"
        if re.fullmatch(r"-?\d+\.\d+", s):
            return "number"
        if s.lower() in {"true", "false", "yes", "no", "是", "否"}:
            return "boolean"
        if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
            return "date"
        return "string"
    kinds = [kind(v) for v in present]
    inferred = max(set(kinds), key=kinds.count)
    target = target_range.lower()
    compatible = (target in {"string", "enum"} or inferred == target
                  or (target == "number" and inferred == "integer")
                  or (target == "date" and inferred == "string"))
    if not compatible:
        return 0.0, f"样例值推断为 {inferred}，本体属性值域为 {target}"
    purity = kinds.count(inferred) / len(kinds)
    return round(0.12 * purity, 4), f"样例值类型主要为 {inferred}（一致率 {purity:.0%}）"


def suggest_mappings(columns: list[dict], ontology_schema: dict | None = None,
                     min_score: float = 0.25, top_k: int = 3) -> dict:
    """Rank attribute candidates using exact/normalized names, aliases and sample types."""
    _, attrs = _schema_parts(ontology_schema)
    results = []
    for col in columns:
        source = str(col.get("name") or col.get("column") or "").strip()
        if not source:
            results.append({"source": col, "candidates": [], "status": "invalid",
                            "reason": "字段缺少 name"})
            continue
        source_labels = [source, str(col.get("label") or ""),
                         *[str(x) for x in (col.get("aliases") or [])]]
        source_norms = {_norm(x) for x in source_labels if _norm(x)}
        samples = list(col.get("samples") or [])[:100]
        candidates = []
        for attr in attrs:
            target_labels = [attr["name"], attr["label"], *attr["aliases"]]
            target_norms = {_norm(x) for x in target_labels if _norm(x)}
            exact = bool(source_norms & target_norms)
            score = 0.78 if exact else 0.0
            evidence = ["字段名/别名与本体属性名称完全匹配"] if exact else []
            if not exact:
                overlap = max((len(a & b) / max(1, len(a | b))
                               for a in source_norms for b in target_norms), default=0.0)
                if overlap >= 0.5:
                    score += 0.38 * overlap
                    evidence.append(f"名称相似度 {overlap:.2f}")
            type_bonus, type_evidence = _type_compatible(samples, attr["range"])
            score += type_bonus
            if samples:
                evidence.append(type_evidence)
            if attr["required"]:
                evidence.append("目标属性为本体必填属性")
            score = min(0.99, round(score, 4))
            if score >= min_score:
                candidates.append({
                    "entity_type": attr["entity_type"],
                    "entity_label": attr["entity_label"],
                    "property": attr["name"],
                    "property_label": attr["label"],
                    "range": attr["range"],
                    "required": attr["required"],
                    "score": score,
                    "confidence": "high" if score >= 0.85 else ("medium" if score >= 0.55 else "low"),
                    "evidence": evidence,
                })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        candidates = candidates[:max(1, min(int(top_k), 10))]
        best = candidates[0]["score"] if candidates else 0.0
        second = candidates[1]["score"] if len(candidates) > 1 else 0.0
        status = "review_required"
        if best >= 0.85 and best - second >= 0.15:
            status = "high_confidence_candidate"
        elif not candidates:
            status = "unmapped"
        results.append({"source": {"name": source, "label": col.get("label"),
                                   "data_type": col.get("data_type"),
                                   "samples": samples[:5]},
                        "candidates": candidates, "status": status})
    return {
        "algorithm": "deterministic-name-alias-type-v1",
        "mapping_count": len(results),
        "high_confidence_count": sum(x["status"] == "high_confidence_candidate" for x in results),
        "review_count": sum(x["status"] == "review_required" for x in results),
        "unmapped_count": sum(x["status"] == "unmapped" for x in results),
        "mappings": results,
        "execution": "not_executed",
        "note": "分数是规则启发式排序，不是经标注集校准的概率；所有映射须经人工确认。",
    }


def validate_mappings(mappings: list[dict], ontology_schema: dict | None = None) -> dict:
    """Validate user-approved source→ontology property mappings without writing data."""
    _, attrs = _schema_parts(ontology_schema)
    allowed = {(a["entity_type"], a["name"]): a for a in attrs}
    issues, seen_sources, seen_targets = [], set(), set()
    checked = []
    for index, item in enumerate(mappings):
        source = str(item.get("source") or "").strip()
        entity = str(item.get("entity_type") or "").strip()
        prop = str(item.get("property") or "").strip()
        key = (entity, prop)
        if not source:
            issues.append({"index": index, "code": "source_required", "message": "source 不能为空"})
        elif source in seen_sources:
            issues.append({"index": index, "code": "duplicate_source", "message": f"源字段重复：{source}"})
        seen_sources.add(source)
        if key not in allowed:
            issues.append({"index": index, "code": "unknown_target",
                           "message": f"目标本体属性不存在：{entity}.{prop}"})
        if key in seen_targets:
            issues.append({"index": index, "code": "duplicate_target",
                           "message": f"多个源字段映射到同一目标：{entity}.{prop}"})
        seen_targets.add(key)
        if key in allowed:
            checked.append({"source": source, "entity_type": entity,
                            "property": prop, "range": allowed[key]["range"],
                            "required": allowed[key]["required"]})
    return {"valid": not issues, "approved_mapping_count": len(checked),
            "issues": issues, "mappings": checked,
            "execution": "not_executed",
            "note": "校验通过仅代表映射结构合法；尚未执行数据转换或写入。"}
