"""Ontology metric engine V1: discovery, safe formula DAG, validation and execution.

All calculations are deterministic and side-effect free. Composite formulas accept metric
IDs as identifiers and only arithmetic operators; eval/exec and arbitrary function calls
are deliberately unavailable.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from typing import Any

AGGREGATIONS = {"count", "distinct_count", "sum", "avg", "min", "max"}
FORMULA_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult,
                 ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Name, ast.Load,
                 ast.Constant, ast.Mod)


def _id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or "").strip())


def discover(spec: dict) -> dict:
    """Create native metric candidates from compiler IR objects and attributes."""
    from .compiler import compile_spec
    ir = compile_spec(spec or {})
    candidates = []
    for obj in ir.get("objects", []):
        candidates.append({
            "metric_id": f"count_{_id(obj.get('name'))}",
            "name": f"{obj.get('label') or obj.get('name')}数量",
            "kind": "native", "subject": obj.get("name"), "aggregation": "count",
            "field": "", "unit": "个", "dimensions": [], "time_grain": None,
            "dependencies": [], "formula": "", "status": "draft",
            "evidence": {"object_node_id": obj.get("id"), "source": "ontology.object"},
        })
        attrs_by_name = {a.get("name"): a for a in
                         (obj.get("declared_attrs", []) + obj.get("attrs", []))
                         if isinstance(a, dict) and a.get("name")}
        for attr in attrs_by_name.values():
            if attr.get("range") not in ("integer", "number", "float", "decimal"):
                continue
            candidates.append({
                "metric_id": f"sum_{_id(obj.get('name'))}_{_id(attr.get('name'))}",
                "name": f"{obj.get('label') or obj.get('name')}{attr.get('label') or attr.get('name')}总和",
                "kind": "native", "subject": obj.get("name"), "aggregation": "sum",
                "field": attr.get("name"), "unit": "待确认", "dimensions": [],
                "time_grain": None, "dependencies": [], "formula": "", "status": "draft",
                "evidence": {"object_node_id": obj.get("id"), "attribute_node_id": attr.get("id"),
                             "source": "ontology.numeric_attribute"},
            })
    return {"items": candidates, "count": len(candidates),
            "diagnostics": ir.get("diagnostics", []),
            "notice": "自动发现结果仅为候选；单位、去重键、过滤口径和时间粒度须业务确认。"}


def _formula_refs(formula: str) -> list[str]:
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"公式语法错误：{exc.msg}") from exc
    refs = []
    for node in ast.walk(tree):
        if not isinstance(node, FORMULA_NODES):
            raise ValueError(f"公式包含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Name):
            refs.append(node.id)
        if isinstance(node, ast.Constant) and (isinstance(node.value, bool) or
                                                not isinstance(node.value, (int, float))):
            raise ValueError("公式常量仅允许数字")
    return sorted(set(refs))


def _eval(node: ast.AST, values: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, values)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in values:
            raise ValueError(f"依赖指标缺少有效值：{node.id}")
        return float(values[node.id])
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, values)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, values), _eval(node.right, values)
        if isinstance(node.op, ast.Add): return a + b
        if isinstance(node.op, ast.Sub): return a - b
        if isinstance(node.op, ast.Mult): return a * b
        if isinstance(node.op, ast.Div):
            if b == 0: raise ValueError("除数为 0")
            return a / b
        if isinstance(node.op, ast.Pow):
            if abs(b) > 8: raise ValueError("指数绝对值不得超过 8")
            return a ** b
        if isinstance(node.op, ast.Mod):
            if b == 0: raise ValueError("取模除数为 0")
            return a % b
    raise ValueError("不支持的公式节点")


def validate(definitions: list[dict]) -> dict:
    issues, by_id, graph = [], {}, {}
    for item in definitions:
        mid = _id(item.get("metric_id"))
        if not mid:
            issues.append({"code": "METRIC_ID_REQUIRED", "message": "指标 ID 不能为空"})
        elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(item.get("metric_id") or "")):
            issues.append({"code": "METRIC_ID_INVALID", "metric_id": mid,
                           "message": "metric_id 必须是字母/下划线开头的 ASCII 标识符"})
        elif mid in by_id:
            issues.append({"code": "METRIC_ID_DUPLICATE", "metric_id": mid,
                           "message": "指标 ID 重复"})
        else:
            by_id[mid] = item
    for mid, item in by_id.items():
        kind = item.get("kind", "native")
        if kind not in ("native", "derived", "composite"):
            issues.append({"code": "METRIC_KIND_INVALID", "metric_id": mid,
                           "message": "指标类型必须为 native/derived/composite"})
        if kind == "native":
            if item.get("aggregation") not in AGGREGATIONS:
                issues.append({"code": "AGGREGATION_INVALID", "metric_id": mid,
                               "message": f"聚合方式仅支持 {sorted(AGGREGATIONS)}"})
            if not item.get("subject"):
                issues.append({"code": "SUBJECT_REQUIRED", "metric_id": mid,
                               "message": "原生指标必须声明业务对象"})
            if item.get("aggregation") not in ("count",) and not item.get("field"):
                issues.append({"code": "FIELD_REQUIRED", "metric_id": mid,
                               "message": "该聚合方式必须声明字段"})
        else:
            try:
                refs = _formula_refs(str(item.get("formula") or ""))
                graph[mid] = refs
                declared = sorted(set(_id(x) for x in (item.get("dependencies") or [])))
                if declared and declared != refs:
                    issues.append({"code": "DEPENDENCY_MISMATCH", "metric_id": mid,
                                   "message": "dependencies 必须与公式引用完全一致"})
                item["_compiled_refs"] = refs
            except ValueError as exc:
                issues.append({"code": "FORMULA_INVALID", "metric_id": mid,
                               "message": str(exc)})
    for mid, refs in graph.items():
        for ref in refs:
            if ref not in by_id:
                issues.append({"code": "DEPENDENCY_NOT_FOUND", "metric_id": mid,
                               "dependency": ref, "message": f"依赖指标 {ref} 不存在"})
    order, visiting, visited = [], set(), set()
    def visit(mid):
        if mid in visiting:
            issues.append({"code": "DEPENDENCY_CYCLE", "metric_id": mid,
                           "message": "指标依赖存在循环"})
            return
        if mid in visited: return
        visiting.add(mid)
        for dep in graph.get(mid, []):
            if dep in by_id: visit(dep)
        visiting.remove(mid)
        visited.add(mid)
        order.append(mid)
    for mid in by_id: visit(mid)
    # Units must match for addition/subtraction. Multiplication/division creates a derived unit.
    for mid, item in by_id.items():
        if item.get("kind") not in ("derived", "composite") or mid not in graph:
            continue
        try:
            tree = ast.parse(str(item.get("formula") or ""), mode="eval")
            if isinstance(tree.body, ast.BinOp) and isinstance(tree.body.op, (ast.Add, ast.Sub)):
                units = {str(by_id[r].get("unit") or "") for r in graph[mid] if r in by_id}
                if len(units) > 1:
                    issues.append({"code": "UNIT_MISMATCH", "metric_id": mid,
                                   "message": "加减运算的依赖指标单位必须一致"})
        except SyntaxError: pass
    errors = [x for x in issues if x["code"] not in ("DEPENDENCY_CYCLE",)]
    return {"valid": not issues, "issues": issues,
            "topological_order": order if not any(x["code"] == "DEPENDENCY_CYCLE" for x in issues) else [],
            "metric_count": len(by_id)}


def preview(definitions: list[dict], data: dict) -> dict:
    report = validate(definitions)
    if not report["valid"]:
        return {"ok": False, "validation": report, "items": []}
    by_id = {_id(x.get("metric_id")): x for x in definitions}
    values, output = {}, []
    rows_by_object = data.get("entities") or {}
    for mid in report["topological_order"]:
        item = by_id[mid]
        value, note = None, ""
        if item.get("kind") == "native":
            rows = [r for r in rows_by_object.get(item.get("subject"), []) if isinstance(r, dict)]
            agg, field = item.get("aggregation"), item.get("field")
            vals = [r.get(field) for r in rows if field in r] if field else [1 for _ in rows]
            present = [v for v in vals if v is not None and str(v).strip() != ""]
            try:
                nums = [float(v) for v in present]
                if agg == "count": value, note = len(rows), f"对象实例数 {len(rows)}"
                elif agg == "distinct_count":
                    value = len({str(v) for v in present}) if present else None
                    note = f"去重计数 {len(present)} → {len({str(v) for v in present})}" if present else "字段无可用值"
                elif agg == "sum":
                    value = sum(nums) if nums else None
                    note = f"{len(nums)} 个数值求和" if nums else "字段无可用数值"
                elif agg == "avg": value, note = (sum(nums) / len(nums) if nums else None), f"{len(nums)} 个数值求平均"
                elif agg == "min": value, note = (min(nums) if nums else None), f"{len(nums)} 个数值取最小"
                elif agg == "max": value, note = (max(nums) if nums else None), f"{len(nums)} 个数值取最大"
            except (TypeError, ValueError):
                note = "字段存在非数值数据，聚合无法执行"
            if not rows:\n                value, note = None, "无实例数据，未生成数值"
            if not vals and agg != "count": note = "字段不存在或无可用值"
        else:
            refs = item.get("_compiled_refs") or _formula_refs(str(item.get("formula") or ""))
            try:
                value = _eval(ast.parse(item["formula"], mode="eval"), values)
                note = "基于已计算依赖指标按安全公式求值"
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                note = str(exc)
        if isinstance(value, float) and not math.isfinite(value):
            value, note = None, "结果不是有限数值"
        if value is not None: values[mid] = float(value)
        output.append({"metric_id": mid, "name": item.get("name", mid),
                       "kind": item.get("kind", "native"), "value": value,
                       "unit": item.get("unit") or "", "ok": value is not None, "note": note,
                       "dependencies": item.get("_compiled_refs", item.get("dependencies") or [])})
    return {"ok": all(x["ok"] for x in output), "validation": report, "items": output,
            "lineage": [{"metric_id": x["metric_id"], "dependencies": x["dependencies"]}
                        for x in output]}


def checksum(definition: dict) -> str:
    raw = json.dumps(definition, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
