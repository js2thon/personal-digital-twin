"""数字分身 API：统一入口 /twin/*。
人工审批通过 LangGraph interrupt/resume 实现，不靠前端长轮询等待。"""
import re
import uuid
from collections import OrderedDict

from fastapi import APIRouter
from pydantic import BaseModel

from app.modules.audit import service as audit
from app.modules.twin import orchestrator
from app.modules.twin import pipeline as pipe

router = APIRouter()

# 会话存储：主存 workflow.twin_sessions（重启不丢），内存仅作兜底。
# L10：兜底内存用有界 LRU，避免异常时无限占用内存。
_SESSIONS: "OrderedDict[str, list]" = OrderedDict()
_SESSION_LIMIT = 200
_TURNS_PER_SESSION = 50


def _remember(session_id: str, item: dict) -> None:
    turns = _SESSIONS.pop(session_id, None) or []
    turns.append(item)
    del turns[:-_TURNS_PER_SESSION]
    _SESSIONS[session_id] = turns
    while len(_SESSIONS) > _SESSION_LIMIT:
        _SESSIONS.popitem(last=False)


def _duration_ms(nodes: list) -> int | None:
    ms = [n.get("ms") for n in (nodes or []) if isinstance(n, dict)
          and isinstance(n.get("ms"), int)]
    return max(ms) if ms else None


def _persist_session(session_id: str, request_id: str, message: str,
                     answer: dict, task_type: str | None) -> None:
    import json

    from app.db import session as db

    try:
        db.execute(
            "INSERT INTO workflow.twin_sessions "
            "(session_id, request_id, message, answer, task_type) "
            "VALUES (:s, :r, :m, CAST(:a AS jsonb), :t) ON CONFLICT (request_id) DO NOTHING",
            {"s": session_id, "r": request_id, "m": message,
             "a": json.dumps(answer or {}, ensure_ascii=False, default=str),
             "t": task_type},
        )
    except Exception:  # noqa: BLE001  表未就绪时不影响问答
        pass


class ChatIn(BaseModel):
    session_id: str = "session_001"
    message: str
    retrieval_scope: list[str] | None = None


class ReplyIn(BaseModel):
    message: str  # 邮件或消息原文


class DecisionIn(BaseModel):
    task_type: str = "project_approval"
    business_context: dict = {}
    decision_mode: str = "human_confirm"   # MVP 仅支持 human_confirm
    evidence_required: bool = True


@router.post("/twin/chat")
def twin_chat(body: ChatIn):
    request_id = str(uuid.uuid4())
    state = {
        "request_id": request_id,
        "user_id": "owner",
        "input_message": body.message,
        "retrieval_scope": body.retrieval_scope,
    }
    result = orchestrator.run_task(None, state)
    fr = result.get("final_response") or {}
    nodes = result.get("trace") or []
    run_id = pipe.record_run(
        "qa", body.message, body.message,
        fr.get("evidence_sufficiency") or "unknown",
        nodes,
        pipe.stages_from_qa(body.message, result, fr),
        _duration_ms(nodes),
    )
    _remember(body.session_id,
              {"request_id": request_id, "message": body.message, "answer": fr})
    _persist_session(body.session_id, request_id, body.message,
                     fr, result.get("task_type"))
    audit.log_access("twin", "chat", "twin_session", request_id,
                     {"task_type": result.get("task_type")})
    return {"request_id": request_id, "task_type": result.get("task_type"),
            "run_id": run_id, **fr}


@router.post("/twin/reply")
def twin_reply(body: ReplyIn):
    request_id = str(uuid.uuid4())
    result = orchestrator.run_task("communication", {
        "request_id": request_id,
        "user_id": "owner",
        "input_message": body.message,
    })
    fr = result.get("final_response") or {}
    nodes = result.get("trace") or []
    run_id = pipe.record_run(
        "reply", body.message[:100], body.message,
        "ok" if fr.get("draft") else "degraded",
        nodes,
        pipe.stages_from_reply(
            {"sender": None, "conversation_name": None,
             "incoming_text": body.message, "trigger_reason": "API /twin/reply"},
            {"draft_text": fr.get("draft"),
             "unconfirmed_points": fr.get("unconfirmed_points")},
            result,
        ),
        _duration_ms(nodes),
    )
    audit.log_access("twin", "reply_draft", "twin_reply", request_id)
    return {"request_id": request_id, "run_id": run_id, **fr}


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _maybe_uuid(v) -> str | None:
    s = str(v or "")
    return s if _UUID_RE.match(s) else None


@router.post("/twin/decision")
def twin_decision(body: DecisionIn):
    """决策分析并落库：decision_records + decision_evidence + 不可变审计快照。

    此前只返回内存 uuid，审批列表永远看不到新决策；现在返回的 decision_id
    可直接提交 /workflow/decisions 生成人工审批任务。
    """
    import json

    from app.db import session as db

    request_id = str(uuid.uuid4())
    result = orchestrator.run_task("decision", {
        "request_id": request_id,
        "user_id": "owner",
        "input_message": body.task_type,
        "business_context": body.business_context,
    })
    rec = result.get("recommendation") or {}
    pol = result.get("policy_result") or {}
    profile = db.fetch_one(
        "SELECT version FROM cognition.profile_versions "
        "WHERE review_status = 'confirmed' ORDER BY version DESC LIMIT 1"
    )

    evidence = []
    for ev_type, rows in (("fact", result.get("retrieved_facts") or []),
                          ("document", result.get("retrieved_documents") or []),
                          ("memory", result.get("retrieved_memories") or [])):
        for d in rows[:3]:
            quote = str(d.get("content") or d.get("object_value") or "")[:300]
            if not quote:
                continue
            # 事实检索返回的主键字段是 fact_id（retrieval._facts），取 id 会恒为空，
            # 导致文档删除后的 fact 证据失效检测失明。
            reference_id = _maybe_uuid(d.get("id") or d.get("fact_id"))
            evidence.append({"evidence_type": ev_type,
                             "reference_id": reference_id,
                             "quote": quote})

    # 单事务：决策记录 + 证据 + 审批任务 + 不可变快照 + 审计要么全成、要么全回滚。
    # 状态直接推进到 waiting_approval，不再停在 analyzing 等人工再调一次接口。
    with db.transaction():
        decision_id = str(db.insert_returning_id(
            "decision.decision_records",
            {
                "task_type": body.task_type,
                "input_context": json.dumps({"business_context": body.business_context,
                                             "decision_mode": body.decision_mode,
                                             "evidence_required": body.evidence_required,
                                             "request_id": request_id}, ensure_ascii=False),
                "recommendation": str(rec.get("recommendation") or "needs_info")[:100],
                "reasoning": json.dumps(rec.get("reasoning") or [], ensure_ascii=False),
                "evidence_refs": json.dumps(evidence, ensure_ascii=False, default=str),
                "risk_level": str(rec.get("risk_level") or "unknown")[:30],
                "confidence": rec.get("confidence") or 0,
                "policy_result": str(pol.get("policy_result") or "approval_required")[:30],
                "status": "waiting_approval",
                "profile_version": (profile or {}).get("version"),
            },
        ))

        for e in evidence:
            db.execute(
                "INSERT INTO decision.decision_evidence "
                "(decision_id, evidence_type, reference_id, quote) "
                "VALUES (:d, :t, CAST(:r AS uuid), :q)",
                {"d": decision_id, "t": e["evidence_type"],
                 "r": e["reference_id"], "q": e["quote"]},
            )

        approval_task_id = str(db.insert_returning_id(
            "workflow.approval_tasks",
            {"decision_id": decision_id, "approval_status": "pending"},
        ))
        snapshot_id = audit.save_decision_snapshot(decision_id, {
            "request_id": request_id, "task_type": body.task_type,
            "input_context": body.business_context,
            "profile_version": (profile or {}).get("version"),
            "evidence_sufficiency": result.get("evidence_sufficiency"),
            "recommendation": rec, "policy_result": pol,
            "evidence": evidence,
        })
        audit.log_access("twin", "decision", "decision_record", decision_id,
                         {"task_type": body.task_type, "snapshot": snapshot_id})

    nodes = result.get("trace") or []
    run_id = pipe.record_run(
        "decision", body.task_type, json.dumps(body.business_context, ensure_ascii=False),
        str(pol.get("policy_result") or "unknown")[:30],
        nodes,
        pipe.stages_from_decision(body.task_type, body.business_context, result, evidence),
        _duration_ms(nodes),
    )
    return {"decision_id": decision_id, **rec,
            "status": "waiting_approval", "request_id": request_id,
            "profile_version": (profile or {}).get("version"),
            "evidence_sufficiency": result.get("evidence_sufficiency"),
            "audit_snapshot_id": snapshot_id,
            "approval_task_id": approval_task_id,
            "evidence": evidence,
            "run_id": run_id,
            "trace": ((result.get("final_response") or {}).get("trace")
                      or result.get("trace") or []),
            "next_action": "决策中心已生成待审批任务，请在「决策中心 → 人工审批」逐条确认"}


@router.get("/twin/tasks/{request_id}")
def get_task(request_id: str):
    from app.db import session as db

    try:
        row = db.fetch_one(
            "SELECT session_id, request_id, message, answer, task_type, created_at "
            "FROM workflow.twin_sessions WHERE request_id = :r", {"r": request_id}
        )
    except Exception:  # noqa: BLE001
        row = None
    if row:
        return {"session_id": row["session_id"], "request_id": row["request_id"],
                "message": row["message"], "answer": row["answer"],
                "task_type": row["task_type"], "created_at": row["created_at"]}
    for sid, items in _SESSIONS.items():
        for item in items:
            if item["request_id"] == request_id:
                return {"session_id": sid, **item}
    return {"request_id": request_id, "status": "not_found"}


@router.post("/twin/feedback")
def twin_feedback(body: dict):
    from app.api.v1.cognition import submit_feedback
    return submit_feedback(
        type("F", (), {"feedback_type": body.get("feedback_type", ""),
                       "target_id": body.get("target_id"),
                       "content": body.get("content", "")})()
    )
