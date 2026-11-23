"""映射契约构建与验证。契约是数据物化的唯一确定性执行依据。"""
from __future__ import annotations

from .profiler import TableProfile


def build_contract(source_id: str, draft: dict, profiles: dict[str, TableProfile]) -> dict:
    schema, class_map = draft["schema"], draft["class_map"]
    rels, attrs = schema["relation_types"], schema["attributes"]
    mappings = []
    for t, cls in class_map.items():
        p = profiles[t]
        obj = schema["entity_types"][cls]
        binding = next((b for b in (obj.get("backing_sources") or [])
                        if b.get("source_id") == source_id and b.get("table") == t), None)
        primary_key = list((binding or {}).get("primary_key") or p.primary_key)
        id_tpl = (f"{source_id}:{t}:" + ":".join("{%s}" % k for k in primary_key)) if primary_key else None
        source_to_property = (binding or {}).get("property_mappings") or {}
        if source_to_property:
            property_columns = {prop: col for col, prop in source_to_property.items()
                                if col in (p.columns or {})}
        else:
            property_columns = {k: v["column"] for k, v in attrs.items()
                                if v["owner"] == cls and not v.get("identifier")
                                and v.get("column") and v["column"] in (p.columns or {})}
        title_property = obj.get("title_property")
        title_column = next((col for col, prop in source_to_property.items()
                             if prop == title_property and col in (p.columns or {})), None)
        m = {"table": t, "class": cls, "id": id_tpl,
             "name": title_column or obj.get("name_column") or None,
             "primary_key": primary_key,
             "attributes": property_columns,
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
            elif p:
                column_profile = p.columns[source_col]
                target_range = str((attrs.get(a) or {}).get("range") or "string").lower()
                source_type = str(column_profile.dtype).lower()
                expected = None
                if any(token in target_range for token in ("integer", "int32", "int64", "long")):
                    expected = {"integer"}
                elif any(token in target_range for token in ("decimal", "double", "float", "number")):
                    expected = {"integer", "number"}
                elif "boolean" in target_range or "bool" in target_range:
                    expected = {"boolean"}
                elif "date" in target_range or "time" in target_range:
                    expected = {"date", "text"}
                if expected and source_type not in expected:
                    issue(table, "PROPERTY_TYPE_MISMATCH",
                          f"属性 {a} 的目标类型 {target_range} 与来源字段 {source_col} 类型 {source_type} 不兼容")
                if (attrs.get(a) or {}).get("required") and column_profile.null_ratio > 0:
                    issue(table, "REQUIRED_PROPERTY_NULL",
                          f"必填属性 {a} 的来源字段 {source_col} 含空值")
        for relation_name, relation_spec in rels.items():
            if relation_spec.get("table") == table and relation_spec.get("cardinality") == "many_to_one":
                if relation_name not in {r.get("type") for r in m.get("relations", [])}:
                    issue(table, "MISSING_RELATION_MAPPING", f"关系 {relation_name} 缺少外键映射")
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
    mapped_links = {link.get("type") for link in contract.get("links", [])}
    for relation_name, relation_spec in rels.items():
        if relation_spec.get("cardinality") == "many_to_many" and relation_name not in mapped_links:
            issue(relation_spec.get("via_table"), "MISSING_JUNCTION_MAPPING",
                  f"多对多关系 {relation_name} 缺少关联表映射")
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
