"""③ 映射契约：schema 草案 + 画像 → 声明式映射（YAML/JSON），以及契约校验。

契约是物化的唯一依据：不经 LLM，确定性、可审核、可版本化。
"""
from __future__ import annotations

from .profiler import TableProfile


def build_contract(source_id: str, draft: dict, profiles: dict[str, TableProfile]) -> dict:
    schema, class_map = draft["schema"], draft["class_map"]
    rels, attrs = schema["relation_types"], schema["attributes"]
    mappings = []
    for t, cls in class_map.items():
        p = profiles[t]
        if not p.primary_key:
            id_tpl = None
        else:
            id_tpl = f"{source_id}:{t}:" + ":".join("{%s}" % k for k in p.primary_key)
        m = {"table": t, "class": cls, "id": id_tpl,
             "name": schema["entity_types"][cls].get("name_column") or None,
             # 只映射源表真实存在的列；设计器新建的虚拟属性（无列）由 Action 填写，不参与物化
             "attributes": {k: v["column"] for k, v in attrs.items()
                            if v["owner"] == cls and not v.get("identifier")
                            and v.get("column") and v["column"] in (p.columns or {})},
             "relations": []}
        for rn, r in rels.items():
            if r.get("table") == t and r["cardinality"] == "many_to_one":
                ref_table = next(fk["ref_table"] for fk in p.foreign_keys
                                 if fk["column"] == r["column"])
                m["relations"].append({
                    "type": rn, "column": r["column"], "target": r["target"],
                    "target_id": f"{source_id}:{ref_table}:" + "{value}", "by": "external_id"})
        mappings.append(m)
    links = []
    for rn, r in rels.items():
        if r["cardinality"] == "many_to_many":
            p = profiles[r["via_table"]]
            a, b = p.foreign_keys[:2]
            links.append({"table": r["via_table"], "type": rn,
                          "source_column": a["column"], "source_table": a["ref_table"],
                          "target_column": b["column"], "target_table": b["ref_table"],
                          "by": "external_id"})
    return {"contract_version": "1", "source": source_id,
            "schema_version": schema["schema_version"], "mappings": mappings, "links": links}


def validate_contract(contract: dict, schema: dict) -> list[dict]:
    """契约 ↔ schema 一致性：类、关系、端点类型、属性归属、缺主键。"""
    issues = []
    types, rels, attrs = schema["entity_types"], schema["relation_types"], schema["attributes"]
    for m in contract["mappings"]:
        if m["class"] not in types:
            issues.append({"level": "error", "table": m["table"], "msg": f"类 {m['class']} 未定义"})
        if not m["id"]:
            issues.append({"level": "error", "table": m["table"],
                           "msg": "无主键，无法生成稳定 external_id（请指定主键列）"})
        for a in m["attributes"]:
            if a not in attrs or attrs[a]["owner"] != m["class"]:
                issues.append({"level": "error", "table": m["table"], "msg": f"属性 {a} 归属不符"})
        for r in m["relations"]:
            spec = rels.get(r["type"])
            if not spec:
                issues.append({"level": "error", "table": m["table"], "msg": f"关系 {r['type']} 未定义"})
            elif spec["source"] != m["class"] or spec["target"] != r["target"]:
                issues.append({"level": "error", "table": m["table"],
                               "msg": f"关系 {r['type']} 端点类型不符"})
    return issues


def to_yaml(contract: dict) -> str:
    try:
        import yaml
        return yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)
    except ImportError:  # pragma: no cover
        import json
        return json.dumps(contract, ensure_ascii=False, indent=2)
