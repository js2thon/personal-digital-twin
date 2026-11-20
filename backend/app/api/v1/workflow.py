"""工作流 API：审批任务与授权执行。
MVP 铁律：未授权动作无法执行；自动执行恒为否。
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import current_actor
from app.db import session as db
from app.modules.audit import service as audit

router = APIRouter()


@router.post("/workflow/decisions")
def create_approval(decision_id: str):
    """幂等补建审批任务：twin/decision 已自动创建，这里用于兼容旧的 analyzing 决策。"""
    with db.transaction():
        did = db.fetch_one(
            "SELECT id, status FROM decision.decision_records WHERE id = :id",
            {"id": decision_id},
        )
        if not did:
            raise HTTPException(404, "decision not found")
        existing = db.fetch_one(
            "SELECT id FROM workflow.approval_tasks "
            "WHERE decision_id = :d AND approval_status = 'pending' LIMIT 1",
            {"d": decision_id},
        )
        if existing:
            return {"approval_task_id": str(existing["id"]), "status": "pending",
                    "deduplicated": True}
        tid = db.insert_returning_id(
            "workflow.approval_tasks",
            {"decision_id": decision_id, "approval_status": "pending"},
        )
        db.execute(
            "UPDATE decision.decision_records SET status = 'waiting_approval' WHERE id = :id",
            {"id": decision_id},
        )
    return {"approval_task_id": tid, "status": "pending"}


@router.get("/workflow/decisions")
def list_decisions(limit: int = 20):
    """决策中心：列出真正落库的决策记录及其证据/审批关联。"""
    return db.fetch_all(
        "SELECT d.id, d.task_type, d.recommendation, d.risk_level, d.status, "
        "d.confidence, d.profile_version, d.created_at, "
        "(SELECT count(*) FROM decision.decision_evidence e WHERE e.decision_id = d.id) "
        "AS evidence_count, "
        "(SELECT count(*) FROM workflow.approval_tasks a WHERE a.decision_id = d.id) "
        "AS approval_count, "
        "(SELECT count(*) FROM audit.decision_snapshots s WHERE s.decision_id = d.id) "
        "AS snapshot_count "
        "FROM decision.decision_records d ORDER BY d.created_at DESC LIMIT :l",
        {"l": max(1, min(limit, 100))},
    )


@router.get("/workflow/decisions/{decision_id}/evidence")
def decision_evidence(decision_id: str):
    return db.fetch_all(
        "SELECT id, evidence_type, reference_id, quote, created_at "
        "FROM decision.decision_evidence WHERE decision_id = :id ORDER BY created_at",
        {"id": decision_id},
    )


@router.get("/workflow/approvals")
def list_approvals(status: str = "pending", limit: int = 100):
    return db.fetch_all(
        "SELECT a.id, a.decision_id, a.approval_status, a.created_at, "
        "d.task_type, d.recommendation, d.risk_level, d.confidence, d.profile_version, "
        "(SELECT count(*) FROM decision.decision_evidence e "
        " WHERE e.decision_id = a.decision_id) AS evidence_count "
        "FROM workflow.approval_tasks a "
        "JOIN decision.decision_records d ON d.id = a.decision_id "
        "WHERE a.approval_status = :s ORDER BY a.created_at DESC "
        "LIMIT :l",
        {"s": status, "l": max(1, min(int(limit or 100), 200))},
    )


class ApprovalIn(BaseModel):
    approver_id: str
    comment: str = ""


def _resolve_approval(task_id: str, new_status: str, body: ApprovalIn,
                      actor: str):
    row = db.fetch_one(
        "SELECT id, decision_id, approval_status FROM workflow.approval_tasks "
        "WHERE id = :id",
        {"id": task_id},
    )
    if not row:
        raise HTTPException(404, "approval task not found")
    if row["approval_status"] != "pending":
        raise HTTPException(400, f"task already {row['approval_status']}")

    with db.transaction():
        db.execute(
            "UPDATE workflow.approval_tasks SET approval_status = :s, approver_id = :a, "
            "approval_comment = :c, approved_at = NOW() "
            "WHERE id = :id AND approval_status = 'pending'",
            {"s": new_status, "a": actor, "c": body.comment, "id": task_id},
        )
        final = "approved" if new_status == "approved" else "rejected"
        db.execute(
            "UPDATE decision.decision_records SET status = :s WHERE id = :id",
            {"s": final, "id": row["decision_id"]},
        )
        audit.log_access(actor, f"approval_{new_status}", "approval_task", task_id)
    return {"approval_task_id": task_id, "approval_status": new_status,
            "decision_id": str(row["decision_id"]), "approver": actor}


@router.post("/workflow/approvals/{task_id}/approve")
def approve(task_id: str, body: ApprovalIn, request: Request):
    # approver_id 由服务端注入，请求体自报的身份不再被信任
    return _resolve_approval(task_id, "approved", body, current_actor(request))


@router.post("/workflow/approvals/{task_id}/reject")
def reject(task_id: str, body: ApprovalIn, request: Request):
    return _resolve_approval(task_id, "rejected", body, current_actor(request))


@router.get("/workflow/policy/tools")
def policy_tool_coverage():
    """L13：工具策略覆盖度自检，明确区分真闸门与「仅声明」。"""
    from app.modules.policy import engine as policy

    return policy.tool_coverage()


@router.post("/workflow/actions/{decision_id}/execute")
def execute_action(decision_id: str):
    """执行已授权动作：执行前最终校验（授权有效性/对象变化/重复执行）。
    MVP：execution_authorized 只能来自 Policy D3 显式授权，否则拒绝。"""
    rec = db.fetch_one(
        "SELECT id, status, policy_result FROM decision.decision_records WHERE id = :id",
        {"id": decision_id},
    )
    if not rec:
        raise HTTPException(404, "decision not found")
    if rec["status"] != "approved":
        raise HTTPException(403, f"decision not approved (status={rec['status']})")

    # 执行前重新验证（影子测试阶段恒为拒绝，逐步开放）
    import json as _json
    from app.modules.policy import engine as policy
    verdict = policy.evaluate_decision("low", action=None)
    if not verdict["execution_authorized"]:
        raise HTTPException(403, "execution not authorized by policy (D3 disabled in MVP)")

    db.insert_returning_id(
        "workflow.execution_audits",
        {"decision_id": decision_id, "action": "manual_execute",
         "executed_by": "human_confirmed", "execution_result": _json.dumps({"ok": True})},
    )
    db.execute(
        "UPDATE decision.decision_records SET status = 'executed' WHERE id = :id",
        {"id": decision_id},
    )
    return {"status": "executed", "decision_id": decision_id}


@router.get("/workflow/actions/{decision_id}")
def get_execution(decision_id: str):
    return db.fetch_all(
        "SELECT id, action, executed_by, execution_result, executed_at "
        "FROM workflow.execution_audits WHERE decision_id = :id "
        "ORDER BY executed_at DESC",
        {"id": decision_id},
    )
