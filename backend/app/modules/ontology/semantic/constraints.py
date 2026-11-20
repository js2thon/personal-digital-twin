"""写入侧约束引擎（V1.3 语义层方案 · 能力 1/2 · 验收 2）。

补齐方案指出的核心缺口：「约束主要在读取侧标注，写入侧未强制」。
本模块是**纯函数**（无 DB / Web 依赖），输入 schema dict + 载荷，输出结构化
违规报告；由 `OntologyService.write_*` 在写库前强制调用，任一 blocking 违规
即整条拒绝（fail-closed），并把逐条违规作为明确错误返回给调用方。

校验项：
- 实例：类型已定义、必填属性、枚举、正则、值域、类型（range）；
- 关系：关系类型已登记、两端类型（**继承感知**，子类可满足父类约束）、
        自环、基数（在已有边集合上判 one_to_one/many_to_one/many_to_many）。
"""
from __future__ import annotations

import re
from typing import Any


class ConstraintViolation(ValueError):
    """写入被约束引擎拒绝；携带逐条违规（对齐 shacl.ShlViolation 的错误形态）。"""
    def __init__(self, message: str, report: dict):
        super().__init__(message)
        self.report = report


def _violation(constraint: str, path: str, message: str,
               severity: str = "violation") -> dict:
    return {"constraint": constraint, "path": path,
            "message": message, "severity": severity}


# ---------------------------------------------------------------- 继承工具
def ancestors(schema: dict, type_name: str) -> list[str]:
    """返回 type_name 的祖先链（含自身），最近的在前；环保护。"""
    types = schema.get("entity_types") or {}
    chain: list[str] = []
    cur = type_name
    seen: set[str] = set()
    while cur and cur in types and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        node = types.get(cur) or {}
        cur = (node.get("parent") or "").strip()
    return chain


def is_subtype_of(schema: dict, actual: str, expected: str) -> bool:
    """继承感知类型判定：actual 或其任一祖先等于 expected 即命中。"""
    return expected in ancestors(schema, actual)


def type_satisfies(schema: dict, actual: str, allowed: list[str] | str) -> bool:
    """allowed 支持 '*' 通配；逐项做继承感知判定。"""
    allow_list = [allowed] if isinstance(allowed, str) else list(allowed or [])
    if "*" in allow_list or not allow_list:
        return True
    return any(is_subtype_of(schema, actual, exp) for exp in allow_list)


# ---------------------------------------------------------------- 实例约束
_RANGE_CHECK: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float),
    "boolean": (bool,), "date": (str,), "datetime": (str,), "enum": (str,),
}


def validate_instance(schema: dict, instance: dict) -> dict:
    """校验一个实例载荷，返回 {conforms, blocking, violations}。"""
    v: list[dict] = []
    type_name = (instance.get("type_name") or "").strip()
    types = schema.get("entity_types") or {}
    if not type_name:
        v.append(_violation("type_required", "type_name", "实例缺少 type_name"))
    elif type_name not in types:
        v.append(_violation("type_defined", "type_name",
                            f"实体类型 {type_name} 未在语义 Schema 定义"))

    props = instance.get("properties") or {}
    if not isinstance(props, dict):
        v.append(_violation("properties_object", "properties", "properties 必须是对象"))
        props = {}

    node = types.get(type_name) or {}
    specs = {a["name"]: a for a in (node.get("attributes") or [])}
    for name, spec in specs.items():
        present = name in props and props[name] is not None and props[name] != ""
        if spec.get("required") and not present:
            v.append(_violation("minCount", name,
                                f"必填属性 {name} 缺失（{type_name}）"))
            continue
        if not present:
            continue
        value = props[name]
        rng = spec.get("range", "string")
        allowed_types = _RANGE_CHECK.get(rng, (object,))
        if rng == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif rng == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif rng == "boolean":
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, allowed_types)
        if not ok:
            v.append(_violation("datatype", name,
                                f"属性 {name} 应为 {rng}，实际 {type(value).__name__}"))
            continue
        enums = spec.get("enum_values") or []
        if enums and value not in enums:
            v.append(_violation("in", name,
                                f"属性 {name}={value!r} 不在枚举 {enums} 内"))
        pattern = spec.get("pattern")
        if pattern and isinstance(value, str) and not re.match(pattern, value):
            v.append(_violation("pattern", name,
                                f"属性 {name}={value!r} 不匹配 {pattern}"))
        minimum, maximum = spec.get("minimum"), spec.get("maximum")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if minimum is not None and value < minimum:
                v.append(_violation("minInclusive", name, f"属性 {name} 小于最小值 {minimum}"))
            if maximum is not None and value > maximum:
                v.append(_violation("maxInclusive", name, f"属性 {name} 大于最大值 {maximum}"))

    return _report(v)


# ---------------------------------------------------------------- 关系约束
def validate_relation(schema: dict, relation: dict,
                      existing_edges: list[dict] | None = None) -> dict:
    """校验一条关系载荷（写入前）。existing_edges 用于基数判定。"""
    v: list[dict] = []
    rel_type = (relation.get("relation_type") or "").strip()
    src_type = (relation.get("source_type") or "").strip()
    dst_type = (relation.get("target_type") or "").strip()
    src_id = str(relation.get("source_id") or "")
    dst_id = str(relation.get("target_id") or "")
    types = schema.get("entity_types") or {}
    rels = schema.get("relation_types") or {}

    if src_id and dst_id and src_id == dst_id:
        v.append(_violation("self_loop", "target_id", "源和目标不能是同一实体"))
    for label, tname, path in (("源", src_type, "source_type"),
                               ("目标", dst_type, "target_type")):
        if tname and tname not in types:
            v.append(_violation("type_defined", path,
                                f"{label}实体类型 {tname} 未在语义 Schema 定义"))

    if not rel_type:
        v.append(_violation("relation_required", "relation_type", "关系缺少 relation_type"))
        return _report(v)
    spec = rels.get(rel_type)
    if not spec:
        v.append(_violation("registered_type", "relation_type",
                            f"关系类型 {rel_type} 未在语义 Schema 登记"))
        return _report(v)

    src_allow = spec.get("source_types") or ["*"]
    dst_allow = spec.get("target_types") or ["*"]
    if src_type and not type_satisfies(schema, src_type, src_allow):
        v.append(_violation("source_type", "source_type",
                            f"{rel_type} 的源须为 {'/'.join(src_allow)}（含子类），实际 {src_type}"))
    if dst_type and not type_satisfies(schema, dst_type, dst_allow):
        v.append(_violation("target_type", "target_type",
                            f"{rel_type} 的目标须为 {'/'.join(dst_allow)}（含子类），实际 {dst_type}"))

    # 基数：one_to_one / many_to_one 约束「同一 source 只能有一条该类型 active 边」
    card = spec.get("cardinality", "many_to_many")
    if card in ("one_to_one", "many_to_one"):
        for e in (existing_edges or []):
            same_src = str(e.get("source_id") or "") == src_id
            same_type = (e.get("relation_type") or "") == rel_type
            same_target = str(e.get("target_id") or "") == dst_id
            if same_src and same_type and e.get("status", "active") == "active" and not same_target:
                v.append(_violation("cardinality", "target_id",
                                    f"{rel_type} 是 {card} 单值关系，源 {src_id} 已存在目标 "
                                    f"{e.get('target_id')}；请先删除旧边"))
                break
    if card == "one_to_one":
        for e in (existing_edges or []):
            if (str(e.get("target_id") or "") == dst_id
                    and (e.get("relation_type") or "") == rel_type
                    and str(e.get("source_id") or "") != src_id
                    and e.get("status", "active") == "active"):
                v.append(_violation("cardinality", "source_id",
                                    f"{rel_type} 是 one_to_one，目标 {dst_id} 已被占用"))
                break

    return _report(v)


def _report(violations: list[dict]) -> dict:
    blocking = [x for x in violations if x["severity"] == "violation"]
    return {"conforms": not violations, "blocking": bool(blocking),
            "violations": violations, "blocking_count": len(blocking),
            "checked_kinds": sorted({x["constraint"] for x in violations})}


def assert_valid(report: dict, subject: str) -> None:
    """有 blocking 违规即抛明确错误（携带逐条违规）；否则放行。"""
    if report.get("blocking"):
        msgs = "；".join(x["message"] for x in report["violations"]
                        if x["severity"] == "violation")
        raise ConstraintViolation(f"{subject} 未通过写入侧约束：{msgs}", report)
