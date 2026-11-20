"""Policy Engine：Agent 工具白名单 + 决策四级权限（D0~D3）。

MVP 铁律：
- D3（自动执行）固定禁止，第一版"是否允许自动执行"恒为否；
- 工具调用全部走白名单，MCP 只是接入协议不是权限系统；
- 所有执行前必须重新验证授权有效性。
"""
from app.config import settings
from app.db import session as db

# 工具白名单（初始权限）。
# L13：每条都标注真实执行点。没有执行点的条目只是「已声明策略」，
# 绝不能当成「已经被闸门拦住」——通过 tool_coverage() 可查询覆盖度。
#   wired   = 代码里确实调用 check_tool 后再执行
#   implicit= 只读路径，无副作用，靠只读实现而非 check_tool 保证
#   declared= 仅声明，当前版本没有对应执行入口（不可执行）
TOOL_WHITELIST = {
    "query_personal_knowledge": "allowed",
    "query_history_projects": "allowed",
    "draft_email": "allowed",
    "send_email": "denied",
    # 企业 IM消息外发：默认关闭总开关，且必须由本人逐条确认（见 messaging.send_reply）
    "send_message": "requires_human_confirmation",
    "create_approval_comment": "draft_only",
    "submit_approval": "requires_human_confirmation",
    "modify_business_data": "denied",
}

TOOL_BINDINGS = {
    "send_message": ("wired", "modules/messaging/service.py:send_reply（四重闸门）"),
    "query_personal_knowledge": ("implicit", "modules/twin/graphs/qa.py:retrieve（只读检索）"),
    "query_history_projects": ("implicit", "modules/twin/graphs/qa.py:retrieve（只读检索）"),
    "draft_email": ("implicit", "modules/twin/graphs/reply.py:_draft（只生成草稿，不外发）"),
    "send_email": ("declared", "无邮件外发实现；策略恒 denied"),
    "create_approval_comment": ("declared", "审批意见仅草稿，未开放写入口"),
    "submit_approval": ("declared", "审批提交不在 Agent 执行面，需本人在控制台操作"),
    "modify_business_data": ("declared", "无业务写接口；策略恒 denied"),
}

# 决策级别：D0 分析 / D1 建议 / D2 人工审批 / D3 自动执行
DECISION_LEVELS = {
    "D0": "analyze",
    "D1": "recommend",
    "D2": "approval_required",
    "D3": "auto_execute",
}


def check_tool(tool_name: str) -> dict:
    verdict = TOOL_WHITELIST.get(tool_name, "denied")  # 未登记工具一律拒绝
    enforcement, binding = TOOL_BINDINGS.get(
        tool_name, ("declared", "未登记工具，无执行点"))
    return {"tool": tool_name, "verdict": verdict,
            "enforcement": enforcement, "binding": binding}


def tool_coverage() -> dict:
    """策略覆盖度自检：区分「真闸门」「只读隐含」「仅声明」，杜绝装饰性误判。"""
    items = []
    for name, verdict in TOOL_WHITELIST.items():
        enforcement, binding = TOOL_BINDINGS.get(name, ("declared", "未标注"))
        items.append({"tool": name, "verdict": verdict,
                      "enforcement": enforcement, "binding": binding})
    return {
        "total": len(items),
        "wired": sum(1 for i in items if i["enforcement"] == "wired"),
        "implicit": sum(1 for i in items if i["enforcement"] == "implicit"),
        "declared_only": sum(1 for i in items if i["enforcement"] == "declared"),
        "denied": sum(1 for i in items if i["verdict"] == "denied"),
        "items": items,
        "note": "declared_only 的策略当前没有执行入口，不构成运行时拦截",
    }


def evaluate_decision(risk_level: str, action: str | None = None) -> dict:
    """返回 policy_result：MVP 中自动执行恒为 False。
    后续开放 D3 时按 workflow.action_authorizations 逐动作授权。"""
    if not settings.d3_auto_exec_enabled or action is None:
        return {
            "policy_result": "approval_required" if risk_level != "high" else "denied",
            "execution_authorized": False,
            "level": "D2",
        }

    row = db.fetch_one(
        "SELECT enabled, scope, requires_confirmation, expires_at "
        "FROM workflow.action_authorizations WHERE action = :a",
        {"a": action},
    )
    if not row or not row["enabled"]:
        return {"policy_result": "approval_required", "execution_authorized": False,
                "level": "D2", "reason": "action_not_authorized"}

    from datetime import datetime, timezone

    expires = row["expires_at"]
    if expires and expires < datetime.now(timezone.utc):
        return {"policy_result": "approval_required", "execution_authorized": False,
                "level": "D2", "reason": "authorization_expired"}

    return {
        "policy_result": "allowed",
        "execution_authorized": True,
        "level": "D3",
        "requires_confirmation": row["requires_confirmation"],
    }
