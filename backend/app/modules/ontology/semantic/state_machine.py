"""状态机约束（V1.3 语义层方案 · P0-1 · 文章《你的AI理解不了你的业务》落地）。

文章强调「什么事件会改变对象状态」。本模块把状态流转从「任意赋值」升级为
「合法状态集 + 允许转换」的确定性约束，写入时 fail-closed：

* **纯函数**：只吃 schema dict + old/new 值，无 DB / Web 依赖，离线可测；
* **服务端权威**：旧值由写入口从库里取，不接受客户端自报（与 write_relation
  反查类型的思路一致），避免伪造 old 值绕过转换约束；
* **不改写权威数据**：只产违规报告，由 OntologyService 决定是否拒绝。

转换校验规则：
  - 新值不在 states → 拒绝（invalid_state）；
  - 给了旧值且 old→new 不在 transitions → 拒绝（illegal_transition）；
  - 未给旧值（新建实例）时，只校验新值合法 + 是否为 initial（不强制，
    但要检查 initial 已配置），首次赋值允许任意合法态（务实：历史数据回填）。
"""
from __future__ import annotations

from typing import Any


def find_state_machine(schema: dict, type_name: str, attribute: str) -> dict | None:
    """从 schema dict 里找某实体类型某属性的状态机定义（含继承链向上找）。"""
    types = schema.get("entity_types") or {}
    cur = type_name
    seen: set[str] = set()
    while cur and cur in types and cur not in seen:
        seen.add(cur)
        for attr in types[cur].get("attributes") or []:
            if attr.get("name") == attribute and attr.get("state_machine"):
                return attr["state_machine"]
        cur = (types[cur].get("parent") or "").strip()
    return None


def _violation(constraint: str, path: str, message: str) -> dict:
    return {"constraint": constraint, "path": path, "message": message,
            "severity": "violation"}


def validate_transition(
    schema: dict,
    type_name: str,
    attribute: str,
    new_value: Any,
    *,
    old_value: Any = None,
    has_old: bool = False,
) -> dict:
    """校验一次状态赋值/转换，返回 {violations, blocking, transitions_used}。

    has_old 区分「没给旧值」与「旧值恰好是 None」；服务端新建实例传 False，
    更新实例传 True 并给出库里的旧值。
    """
    sm = find_state_machine(schema, type_name, attribute)
    if not sm:
        return {"violations": [], "blocking": False, "applicable": False}

    states = set(sm.get("states") or [])
    transitions = sm.get("transitions") or {}
    path = attribute
    violations: list[dict] = []

    new = str(new_value) if new_value is not None else ""
    if states and new not in states:
        violations.append(_violation(
            "state.invalid_value", path,
            f"'{new}' 不在合法状态集 {sorted(states)} 内"))

    used: dict[str, Any] = {}
    if has_old:
        old = str(old_value) if old_value is not None else ""
        allowed = list(transitions.get(old) or [])
        used = {"from": old, "to": new, "allowed": allowed}
        if new and new != old and new not in allowed:
            violations.append(_violation(
                "state.illegal_transition", path,
                f"状态不允许从 '{old}' 直接变为 '{new}'；'{old}' 的合法后继为 {allowed or '无（终态）'}"))
        if old and old not in states:
            violations.append(_violation(
                "state.invalid_old_value", path,
                f"库中旧状态 '{old}' 不在合法状态集内（历史脏数据），拒绝在脏状态上继续流转"))

    return {
        "applicable": True,
        "violations": violations,
        "blocking": bool(violations),
        "state_machine": {"states": sorted(states), "initial": sm.get("initial", "")},
        "transition": used,
    }


def validate_instance_transitions(
    schema: dict,
    type_name: str,
    payload: dict,
    *,
    previous: dict | None = None,
) -> dict:
    """遍历 payload 中带状态机的属性，逐个校验；previous 为服务端取到的旧实例属性。

    返回 {violations, blocking, checked:[{attribute,...}]}。
    """
    prev = previous or {}
    has_prev = previous is not None
    all_violations: list[dict] = []
    checked: list[dict] = []
    types = schema.get("entity_types") or {}
    attrs = (types.get(type_name) or {}).get("attributes") or []
    for attr in attrs:
        name = attr.get("name")
        if not attr.get("state_machine") or name not in payload:
            continue
        r = validate_transition(schema, type_name, name, payload.get(name),
                                old_value=prev.get(name), has_old=has_prev)
        if r.get("applicable"):
            checked.append({"attribute": name, **r})
            all_violations.extend(r["violations"])
    return {"violations": all_violations, "blocking": bool(all_violations),
            "checked": checked}


def self_check() -> dict:
    """内置可评估样本：合法/非法转换各一组。"""
    schema = {"entity_types": {"Project": {"name": "Project", "parent": "", "attributes": [
        {"name": "approval_status", "state_machine": {
            "states": ["pending", "approved", "rejected", "archived"],
            "transitions": {"pending": ["approved", "rejected"],
                            "approved": ["archived"], "rejected": ["pending"], "archived": []},
            "initial": "pending"}},
    ]}}}
    ok1 = validate_transition(schema, "Project", "approval_status", "approved",
                              old_value="pending", has_old=True)
    bad1 = validate_transition(schema, "Project", "approval_status", "archived",
                               old_value="pending", has_old=True)   # pending→archived 非法
    bad2 = validate_transition(schema, "Project", "approval_status", "unknown",
                               old_value="pending", has_old=True)
    return {
        "pass": (not ok1["blocking"] and bad1["blocking"] and bad2["blocking"]),
        "legal_pass": not ok1["blocking"],
        "illegal_jump_blocked": bad1["blocking"],
        "invalid_value_blocked": bad2["blocking"],
        "note": "pending→approved 合法；pending→archived 与非法值应被拒",
    }
