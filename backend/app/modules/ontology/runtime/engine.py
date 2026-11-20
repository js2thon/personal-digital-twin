"""本体运行时 · 执行引擎：把 compiler 产出的 IR 真正跑出结果。

输入：
- ir        compile_spec(spec) 的产出（对象/属性/函数三型/关系/约束 + 体检结论）
- data      {mode, entities:{对象名:[{字段:值},...]}, relations:[...]}
            由调用方提供真实数据或模拟数据，服务端不臆造。

输出（统一结构，零 I/O、纯函数）：
- metrics        指标名 → {algo, value, note, ok, ...}（算不出 value=None + 原因，绝不填 0）
- derives        派生函数 → {value, error}（回写到派生记录，不改原始数据）
- actions        行动 → {triggered, reason, effect_mode="declared"}（只判触发并留痕，不执行副作用）
- constraint_violations  约束违规清单（unique/not_null/in/matches/referential）
- unexecutable   IR 判定不可执行的函数（照实列出，不隐瞒）
- derived_records / notes

设计铁律：
- 所有取值来自 data.entities，缺失列就是缺失（如实标注），不补默认值；
- 不可执行的函数不尝试"善意猜测"它的口径，直接进 unexecutable；
- 行动 effect 不执行（effect_mode=declared），只记录"若触发应执行什么"。
"""
from __future__ import annotations

from typing import Any

from . import expr as EX
from .metrics import apply_metric


def _entities(data: dict | None, object_name: str) -> list[dict[str, Any]]:
    """取某对象的记录列表；数据缺失返回空列表（由调用方决定是否标 no-data）。"""
    ents = ((data or {}).get("entities") or {})
    rows = ents.get(object_name) or []
    return [r for r in rows if isinstance(r, dict)]


def _column(rows: list[dict[str, Any]], field: str) -> list[Any]:
    """取一列值：只收集该字段确实存在的记录（缺失即跳过，不补 0/空串）。"""
    return [r[field] for r in rows if field in r]


def _owner_object(ir: dict, attr_name: str) -> str:
    for a in ir.get("attrs") or []:
        if a.get("name") == attr_name and a.get("object"):
            return a["object"]
    for o in ir.get("objects") or []:
        for a in (o.get("attrs") or []) + (o.get("declared_attrs") or []):
            if (a.get("name") if isinstance(a, dict) else a) == attr_name:
                return o.get("name", "")
    return ""


def _scope_for_row(row: dict[str, Any], derived: dict[str, Any]) -> dict[str, Any]:
    """构造表达式求值 scope：原始字段 + 本行已算出的派生值。"""
    scope = dict(row)
    scope.update({k: v for k, v in derived.items() if k != "_errors"})
    return scope


def _run_metric(ir: dict, fn: dict, data: dict | None, notes: list[str]) -> dict[str, Any]:
    algo = fn.get("algo", "")
    owner = fn.get("owner_attr", "")
    obj = _owner_object(ir, owner)
    inputs = fn.get("inputs") or ([owner] if owner else [])
    rows = _entities(data, obj) if obj else []
    # 收集输入列：优先显式 inputs，否则用归属属性；都没有则如实标注
    values: list[Any] = []
    for field in inputs:
        col = _column(rows, field)
        values.extend(col)
    if not rows:
        notes.append(f"指标「{fn.get('name')}」无数据：对象「{obj or '未判定'}」无实例记录")
    elif not values:
        notes.append(f"指标「{fn.get('name')}」无取值：字段 {inputs or [owner]} 在记录中不存在")
    result = apply_metric(algo, values)
    return {
        "name": fn.get("name", ""), "label": fn.get("label", ""),
        "algo": algo, "owner": owner, "object": obj,
        "value": result.get("value"), "ok": result.get("ok"),
        "note": result.get("note", ""), "official": result.get("official", False),
        "input": result.get("input", 0), "used": result.get("used", 0),
    }


def _run_derive(ir: dict, fn: dict, data: dict | None, notes: list[str]) -> dict[str, Any]:
    owner = fn.get("owner_attr", "")
    obj = _owner_object(ir, owner)
    rows = _entities(data, obj) if obj else []
    target = fn.get("target") or fn.get("output") or fn.get("name", "")
    mode, expr_text = fn.get("mode", ""), fn.get("expr", "")
    results: list[dict[str, Any]] = []
    derived_records: list[dict[str, Any]] = []
    if not rows:
        notes.append(f"派生「{fn.get('name')}」无数据：对象「{obj or '未判定'}」无实例记录")
        return {"name": fn.get("name", ""), "mode": mode, "target": target,
                "results": [], "note": "无实例数据"}
    for i, row in enumerate(rows):
        derived: dict[str, Any] = {}
        scope = _scope_for_row(row, derived)
        err = ""
        value: Any = None
        try:
            if mode == "assign":
                value = EX.evaluate(expr_text, scope)
            elif mode == "expr":
                value = EX.evaluate(expr_text, scope)
            elif mode == "ifelse":
                # if cond then a else b：按正则三段分别求值（compiler 已校验语法）
                import re
                m = re.match(r"^if\s+(.+?)\s+then\s+(.+?)(?:\s+else\s+(.+))?$",
                             expr_text, re.I | re.S)
                if not m:
                    err = "if/then/else 结构无法解析"
                else:
                    cond = EX.try_bool(m.group(1), scope)
                    value = EX.evaluate(m.group(2) if cond else (m.group(3) or "None"), scope)
            else:
                err = f"未知派生模式 {mode}"
        except EX.ExprError as exc:
            err = str(exc)
        except Exception as exc:  # noqa: BLE001  单行异常不影响整轮
            err = f"{type(exc).__name__}: {exc}"
        if err:
            results.append({"row": i, "value": None, "error": err})
        else:
            derived[target] = value
            results.append({"row": i, "value": value, "error": ""})
            derived_records.append({"row": i, "object": obj, "target": target, "value": value})
    ok_count = sum(1 for r in results if not r["error"])
    return {
        "name": fn.get("name", ""), "mode": mode, "target": target,
        "object": obj, "results": results,
        "ok": bool(results) and ok_count == len(results),
        "note": f"{ok_count}/{len(results)} 行成功回写「{target}」",
    }


def _run_action(ir: dict, fn: dict, data: dict | None, notes: list[str]) -> dict[str, Any]:
    owner = fn.get("owner_attr", "")
    obj = _owner_object(ir, owner)
    rows = _entities(data, obj) if obj else []
    condition = fn.get("condition", "")
    effect = fn.get("effect", "")
    triggered_rows: list[int] = []
    errors: list[dict[str, Any]] = []
    if not rows:
        notes.append(f"行动「{fn.get('name')}」无数据：对象「{obj or '未判定'}」无实例记录")
    for i, row in enumerate(rows):
        try:
            if EX.try_bool(condition, _scope_for_row(row, {})):
                triggered_rows.append(i)
        except EX.ExprError as exc:
            errors.append({"row": i, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            errors.append({"row": i, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "name": fn.get("name", ""), "object": obj,
        "condition": condition, "effect": effect,
        "effect_mode": "declared",  # 只声明，不执行外部副作用
        "triggered_rows": triggered_rows, "triggered": len(triggered_rows),
        "errors": errors, "note": f"{len(triggered_rows)}/{len(rows)} 行命中触发条件",
    }


def _run_constraints(ir: dict, data: dict | None) -> list[dict[str, Any]]:
    """约束校验：unique / not_null / in / matches / referential（照实列违规）。"""
    violations: list[dict[str, Any]] = []
    for c in ir.get("constraints") or []:
        if not c.get("executable"):
            violations.append({"name": c.get("name"), "kind": c.get("kind"),
                               "row": None, "value": None,
                               "reason": "约束不可判定：" + (c.get("reason") or "")})
            continue
        kind, target, args = c.get("kind", ""), c.get("target", ""), c.get("args") or []
        obj = _owner_object(ir, target)
        rows = _entities(data, obj) if obj else []
        if not rows:
            continue  # 无数据不臆造违规，由验证层标 no-data
        if kind == "not_null":
            for i, r in enumerate(rows):
                v = r.get(target)
                if v is None or (isinstance(v, str) and not v.strip()):
                    violations.append({"name": c.get("name"), "kind": kind,
                                       "row": i, "value": v, "reason": f"字段「{target}」为空"})
        elif kind == "unique":
            seen: dict[Any, int] = {}
            for i, r in enumerate(rows):
                v = r.get(target)
                if v is None:
                    continue
                if v in seen:
                    violations.append({"name": c.get("name"), "kind": kind,
                                       "row": i, "value": v,
                                       "reason": f"字段「{target}」值重复（首见第 {seen[v]} 行）"})
                else:
                    seen[v] = i
        elif kind == "in":
            allowed = set(args)
            for i, r in enumerate(rows):
                v = r.get(target)
                if v is not None and v not in allowed:
                    violations.append({"name": c.get("name"), "kind": kind,
                                       "row": i, "value": v,
                                       "reason": f"字段「{target}」值不在允许集合 {sorted(allowed)}"})
        elif kind == "matches":
            import re
            pat = args[0] if args else ""
            try:
                rx = re.compile(pat)
                for i, r in enumerate(rows):
                    v = r.get(target)
                    if v is not None and not rx.search(str(v)):
                        violations.append({"name": c.get("name"), "kind": kind,
                                           "row": i, "value": v,
                                           "reason": f"字段「{target}」不匹配正则 {pat}"})
            except re.error as exc:
                violations.append({"name": c.get("name"), "kind": kind, "row": None,
                                   "value": None, "reason": f"正则非法：{exc}"})
        elif kind == "referential":
            # references(child.fk = parent.pk)：args=[child.fk, parent.pk]
            if len(args) >= 2:
                child_fk, parent_pk = args[0].split(".")[-1], args[1].split(".")[-1]
                parent_obj = args[1].split(".")[0] if "." in args[1] else ""
                parent_vals = {r.get(parent_pk) for r in _entities(data, parent_obj) if parent_pk in r}
                for i, r in enumerate(rows):
                    v = r.get(child_fk)
                    if v is not None and v not in parent_vals:
                        violations.append({"name": c.get("name"), "kind": kind,
                                           "row": i, "value": v,
                                           "reason": f"外键「{child_fk}」在父对象「{parent_obj}」无对应「{parent_pk}」"})
    return violations


def run(ir: dict, data: dict | None = None) -> dict[str, Any]:
    """执行 IR（纯函数）：产出指标值 / 派生回写 / 行动触发 / 约束违规 / 不可执行项。"""
    notes: list[str] = []
    funcs = ir.get("functions") or []
    unexecutable = [
        {"name": f.get("name", ""), "type": f.get("type", ""), "reason": f.get("reason", "")}
        for f in funcs if not f.get("executable")
    ]
    metrics = [_run_metric(ir, f, data, notes) for f in funcs
               if f.get("type") == "metric" and f.get("executable")]
    derives = [_run_derive(ir, f, data, notes) for f in funcs
               if f.get("type") == "derive" and f.get("executable")]
    actions = [_run_action(ir, f, data, notes) for f in funcs
               if f.get("type") == "action" and f.get("executable")]
    violations = _run_constraints(ir, data)
    derived_records = [rec for d in derives for rec in d.get("results", [])
                       if not rec.get("error")]
    return {
        "mode": (data or {}).get("mode", "unknown"),
        "hash": ir.get("hash", ""),
        "metrics": metrics, "derives": derives, "actions": actions,
        "constraint_violations": violations, "unexecutable": unexecutable,
        "derived_records": derived_records, "notes": notes,
        "summary": {
            "metrics_ok": sum(1 for m in metrics if m.get("ok")),
            "derives_ok": sum(1 for d in derives if d.get("ok")),
            "actions_triggered": sum(a.get("triggered", 0) for a in actions),
            "violations": len(violations),
            "unexecutable": len(unexecutable),
        },
    }


def self_check() -> dict[str, Any]:
    """纯函数自检：用本地构造的 IR + 数据验证五类输出（数据非业务真值）。"""
    ir = {
        "hash": "test",
        "attrs": [{"name": "月薪", "object": "员工"}, {"name": "工号", "object": "员工"}],
        "objects": [{"name": "员工", "declared_attrs": []}],
        "constraints": [
            {"name": "工号唯一", "kind": "unique", "target": "工号", "args": [], "executable": True},
            {"name": "月薪非空", "kind": "not_null", "target": "月薪", "args": [], "executable": True},
        ],
        "functions": [
            {"name": "月薪合计", "type": "metric", "executable": True, "algo": "Sum",
             "owner_attr": "月薪", "inputs": ["月薪"], "label": "月薪合计"},
            {"name": "翻倍", "type": "derive", "executable": True, "mode": "assign",
             "expr": "月薪 * 2", "target": "翻倍月薪", "owner_attr": "月薪"},
            {"name": "高薪提醒", "type": "action", "executable": True,
             "condition": "月薪 > 100", "effect": "标记高薪", "owner_attr": "月薪"},
            {"name": "瞎写", "type": "derive", "executable": False, "reason": "logic 不可求值"},
        ],
    }
    data = {"mode": "test", "entities": {"员工": [
        {"工号": "E1", "月薪": 50}, {"工号": "E2", "月薪": 80}, {"工号": "E1", "月薪": 90},
    ]}}
    r = run(ir, data)
    ok = (
        r["summary"]["metrics_ok"] == 1
        and next(m for m in r["metrics"] if m["name"] == "月薪合计")["value"] == 220
        and r["summary"]["derives_ok"] == 1
        and r["summary"]["actions_triggered"] == 0  # 50/80/90 都 <= 100
        and r["summary"]["violations"] == 1  # 工号 E1 重复
        and r["summary"]["unexecutable"] == 1
        and r["summary"]["actions_triggered"] == 0
    )
    empty = run(ir, {})
    ok = ok and all(m["value"] is None for m in empty["metrics"])  # 无数据不填 0
    return {"pass": bool(ok), "summary": r["summary"]}
