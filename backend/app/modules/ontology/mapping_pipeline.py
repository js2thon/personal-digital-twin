"""Deterministic dataset-to-ontology mapping suggestions and validation.

This module deliberately does not execute SQL or write business data. It produces
explainable candidates for human review; a later connector/execution stage can
consume only explicitly approved mappings.
"""
from __future__ import annotations

import re
from typing import Any


def _norm(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).lower()
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
                overlap = max((len(set(a) & set(b)) / max(1, len(set(a) | set(b)))
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


def _coerce_value(value: Any, target_range: str) -> tuple[Any, str | None]:
    """Conservatively convert tabular strings to declared ontology ranges."""
    if value is None or value == "":
        return None, None
    rng = (target_range or "string").lower()
    try:
        if rng == "integer":
            if isinstance(value, bool):
                raise ValueError()
            return int(str(value).strip()), None
        if rng == "number":
            if isinstance(value, bool):
                raise ValueError()
            return float(str(value).strip()), None
        if rng == "boolean":
            if isinstance(value, bool):
                return value, None
            token = str(value).strip().lower()
            if token in {"true", "1", "yes", "y", "是"}:
                return True, None
            if token in {"false", "0", "no", "n", "否"}:
                return False, None
            raise ValueError()
        if rng == "enum":
            return str(value).strip(), None
        return str(value).strip(), None
    except (TypeError, ValueError):
        return value, f"无法将值 {value!r} 转换为 {rng}"


def preview_rows(rows: list[dict], mappings: list[dict],
                 ontology_schema: dict | None = None,
                 source_name: str = "dataset",
                 limit: int = 20) -> dict:
    """Transform rows using explicitly approved mappings and validate instances.

    This is a preview only: no database writes, connector side effects or IDs
    are created. Each output property carries source-column lineage.
    """
    from app.modules.ontology.semantic.constraints import validate_instance

    schema = ontology_schema or {}
    _, attrs = _schema_parts(schema)
    attr_index = {(a["entity_type"], a["name"]): a for a in attrs}
    approved = validate_mappings(mappings, schema)
    if not approved["valid"]:
        return {"valid": False, "stage": "mapping_validation",
                "issues": approved["issues"], "execution": "not_executed",
                "preview": []}
    if not rows:
        return {"valid": False, "stage": "dataset_validation",
                "issues": [{"code": "empty_dataset", "message": "数据集没有数据行"}],
                "execution": "not_executed", "preview": []}

    # A missing source header must not silently become NULL, especially for optional
    # ontology properties where instance validation may otherwise pass.
    missing_columns = sorted({
        str(mapping["source"])
        for mapping in mappings
        if any(mapping["source"] not in row for row in rows)
    })
    if missing_columns:
        return {
            "valid": False, "stage": "source_schema_validation",
            "issues": [{"code": "missing_source_column",
                        "message": f"源数据缺少映射字段：{column}", "source": column}
                       for column in missing_columns],
            "execution": "not_executed", "preview": [],
        }

    preview, row_errors = [], []
    for row_no, row in enumerate(rows[:max(1, min(int(limit), 100))], start=1):
        grouped: dict[str, dict] = {}
        lineage: dict[str, dict] = {}
        conversion_errors = []
        for mapping in mappings:
            entity = mapping["entity_type"]
            prop = mapping["property"]
            spec = attr_index.get((entity, prop), {})
            raw = row.get(mapping["source"])
            value, error = _coerce_value(raw, spec.get("range", "string"))
            grouped.setdefault(entity, {})[prop] = value
            lineage.setdefault(entity, {})[prop] = {
                "source": source_name, "row": row_no,
                "column": mapping["source"], "raw_value": raw,
                "transformed_value": value,
            }
            if error:
                conversion_errors.append({"property": prop, "message": error})
        for entity, properties in grouped.items():
            report = validate_instance(schema, {
                "type_name": entity, "properties": properties,
            })
            issues = list(report.get("violations") or []) + conversion_errors
            preview.append({
                "source_row": row_no, "type_name": entity,
                "properties": properties, "lineage": lineage.get(entity, {}),
                "valid": not bool(report.get("blocking") or conversion_errors),
                "validation": report, "conversion_errors": conversion_errors,
            })
            if report.get("blocking") or conversion_errors:
                row_errors.append({"row": row_no, "entity_type": entity,
                                   "issues": issues})
    return {
        "valid": not row_errors, "stage": "preview",
        "source": source_name, "rows_received": len(rows),
        "rows_previewed": min(len(rows), max(1, min(int(limit), 100))),
        "instances_previewed": len(preview), "invalid_instances": len(row_errors),
        "preview": preview, "row_errors": row_errors,
        "execution": "not_executed",
        "note": "预览结果未写入本体实例库；请审核映射、类型转换和逐字段血缘后再进入受控写入阶段。",
    }


def read_tabular_file(file_name: str, content: bytes,
                      sheet_name: str | None = None) -> dict:
    """Read CSV/XLSX bytes into column metadata and row dictionaries.

    SQL/database connectors intentionally live behind a separate adapter contract;
    this function handles user-provided tabular files without external side effects.
    """
    import csv
    import io
    from pathlib import Path

    suffix = Path(file_name or "").suffix.lower()
    if not content:
        raise ValueError("文件内容为空")
    if len(content) > 10 * 1024 * 1024:
        raise ValueError("文件超过 10 MiB 限制")
    if suffix == ".csv":
        decoded = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                decoded = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("CSV 编码不支持，请使用 UTF-8 或 GB18030")
        sample = decoded[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(decoded), dialect=dialect)
        original_headers = list(reader.fieldnames or [])
        headers = [str(x or "").strip() for x in original_headers]
        if not headers or any(not h for h in headers):
            raise ValueError("CSV 首行必须包含非空字段名")
        if len(set(headers)) != len(headers):
            raise ValueError("CSV 字段名重复，请先重命名重复列")
        rows = [{headers[i]: row.get(original_headers[i]) for i in range(len(headers))}
                for row in reader]
    elif suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:
            raise ValueError(f"Excel 文件解析失败：{exc}") from exc
        if sheet_name and sheet_name not in workbook.sheetnames:
            workbook.close()
            raise ValueError(f"工作表不存在：{sheet_name}")
        sheet = workbook[sheet_name] if sheet_name else workbook.active
        iterator = sheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        headers = [str(x or "").strip() for x in (header_row or [])]
        if not headers or any(not h for h in headers):
            workbook.close()
            raise ValueError("Excel 首行必须包含非空字段名")
        if len(set(headers)) != len(headers):
            workbook.close()
            raise ValueError("Excel 字段名重复，请先重命名重复列")
        rows = [{headers[i]: value for i, value in enumerate(values or ())
                 if i < len(headers)} for values in iterator]
        workbook.close()
    else:
        raise ValueError("仅支持 .csv、.xlsx、.xlsm 文件")
    # Add the same normalized aliases used by Automodel's source connector while
    # retaining original headers. This lets a full source file map to the normalized
    # Discovery contract without losing the original source-column name in evidence.
    import re
    normalized = {}
    for header in headers:
        alias = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", header).strip("_").lower() or "column"
        normalized.setdefault(alias, []).append(header)
    collisions = {alias: names for alias, names in normalized.items() if len(names) > 1}
    if collisions:
        raise ValueError("字段规范化后重名，无法安全映射：" + "; ".join(
            alias + " <- " + ", ".join(names) for alias, names in collisions.items()))
    for row in rows:
        for alias, names in normalized.items():
            original = names[0]
            if alias != original:
                row[alias] = row.get(original)
    columns = []
    for name in headers:
        samples = [row.get(name) for row in rows[:100] if row.get(name) not in (None, "")]
        columns.append({"name": name, "samples": samples[:20]})
    return {"file_name": file_name, "columns": columns,
            "rows": rows, "row_count": len(rows),
            "sheet_name": sheet_name if suffix in {".xlsx", ".xlsm"} else None}
