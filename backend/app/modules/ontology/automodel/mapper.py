"""映射契约构建与验证。契约是数据物化的唯一确定性执行依据。"""
from __future__ import annotations

from .profiler import TableProfile


def build_contract(source_id: str, draft: dict, profiles: dict[str, TableProfile]) -> dict:
    schema, class_map = draft["schema"], draft["class_map"]
    rels, attrs = schema["relation_types"], schema["attributes"]
    mappings = []
    for t, cls in class_map.items():
        p = profiles[t]
        id_tpl = (f"{source_id}:{t}:" + ":".join("{%s}" % k for k in p.primary_key)) if p.primary_key else None
        m = {"table": t, "class": cls, "id": id_tpl,
             "name": schema["entity_types"][cls].get("name_column") or None,
             "primary_key": list(p.primary_key),
             "attributes": {k: v["column"] for k, v in attrs.items()
                            if v["owner"] == cls and not v.get("identifier")
                            and v.get("column") and v["column"] in (p.columns or {})},
             "relations": []}
        for rn, r in rels.items():
            if r.get("table") == t and r["cardinality"] == "many_to_one":
                fk = next((fk for fk in p.foreign_keys if fk["column"] == r["column"]), None)
                if fk:
                    m["relations"].append({
                        "type": rn, "column": r["column"], "target": r["target"],
                        "target_table": fk["ref_table"],
                        "target_id": f"{source_id}:{fk['ref_table']}:" + "{value}",
                        "by": "external_id"})
        mappings.append(m)
    links = []
    for rn, r in rels.items():
        if r["cardinality"] == "many_to_many":
            p = profiles[r["via_table"]]
            if len(p.foreign_keys) >= 2:
                a, b = p.foreign_keys[:2]
                links.append({"table": r["via_table"], "type": rn,
                              "source_column": a["column"], "source_table": a["ref_table"],
                              "target_column": b["column"], "target_table": b["ref_table"],
                              "by": "external_id"})
    return {"contract_version": "1", "source": source_id,
            "schema_version": schema["schema_version"], "mappings": mappings, "links": links}


def validate_contract(contract: dict, schema: dict,
                      profiles: dict[str, TableProfile] | None = None) -> list[dict]:
    """验证本体端点、源表/列存在性、主键稳定性、FK 与契约类型一致性。"""
    issues: list[dict] = []
    types, rels, attrs = schema["entity_types"], schema["relation_types"], schema["attributes"]
    profile_map = profiles or {}

    def issue(table, code, msg, level="error", **extra):
        issues.append({"level": level, "code": code, "table": table, "msg": msg, **extra})

    mapped_tables = {m.get("table") for m in contract.get("mappings", [])}
    for m in contract.get("mappings", []):
        table, cls = m.get("table"), m.get("class")
        p = profile_map.get(table)
        if cls not in types:
            issue(table, "UNKNOWN_OBJECT_TYPE", f"类 {cls} 未定义")
        if not m.get("id"):
            issue(table, "MISSING_STABLE_KEY", "无主键，无法生成稳定 external_id（请指定主键列）")
        if p:
            columns = p.columns or {}
            if m.get("name") and m["name"] not in columns:
                issue(table, "MISSING_NAME_COLUMN", f"名称列 {m['name']} 不存在")
            pk = list(m.get("primary_key") or [])
            if not pk:
                issue(table, "MISSING_PRIMARY_KEY", "映射缺少主键列")
            for key in pk:
                if key not in columns:
                    issue(table, "MISSING_PRIMARY_KEY_COLUMN", f"主键列 {key} 不存在")
                else:
                    cp = columns[key]
                    if cp.null_ratio > 0:
                        issue(table, "NULL_PRIMARY_KEY", f"主键列 {key} 含空值", null_ratio=cp.null_ratio)
                    if len(pk) == 1 and not cp.unique:
                        issue(table, "NON_UNIQUE_PRIMARY_KEY", f"主键列 {key} 在发现样本中不唯一")
        for a, source_col in (m.get("attributes") or {}).items():
            if a not in attrs or attrs[a]["owner"] != cls:
                issue(table, "PROPERTY_OWNER_MISMATCH", f"属性 {a} 归属不符")
            if p and source_col not in (p.columns or {}):
                issue(table, "MISSING_SOURCE_COLUMN", f"来源字段 {source_col} 不存在")
        for r in m.get("relations", []):
            spec = rels.get(r.get("type"))
            if not spec:
                issue(table, "UNKNOWN_LINK_TYPE", f"关系 {r.get('type')} 未定义")
            elif spec["source"] != cls or spec["target"] != r.get("target"):
                issue(table, "RELATION_ENDPOINT_MISMATCH", f"关系 {r.get('type')} 端点类型不符")
            if p and r.get("column") not in (p.columns or {}):
                issue(table, "MISSING_FK_COLUMN", f"外键字段 {r.get('column')} 不存在")
            target_table = r.get("target_table")
            if target_table and target_table not in mapped_tables:
                issue(table, "UNMAPPED_RELATION_TARGET", f"关系目标表 {target_table} 未映射", "error")
    for link in contract.get("links", []):
        table = link.get("table")
        p = profile_map.get(table)
        if p:
            for col in (link.get("source_column"), link.get("target_column")):
                if col not in (p.columns or {}):
                    issue(table, "MISSING_JUNCTION_COLUMN", f"关联表字段 {col} 不存在")
    for rn, spec in rels.items():
        if spec.get("source") not in types or spec.get("target") not in types:
            issue(None, "INVALID_RELATION_ENDPOINT", f"关系 {rn} 的端点类型未定义")
    return issues


def to_yaml(contract: dict) -> str:
    try:
        import yaml
        return yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)
    except ImportError:  # pragma: no cover
        import json
        return json.dumps(contract, ensure_ascii=False, indent=2)
