"""个人认知 API：Profile 版本管理 + 候选确认 + 反馈。"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import session as db

router = APIRouter()


@router.get("/cognition/profile")
def current_profile():
    """当前已确认的 Profile 版本（五类：knowledge/preference/belief/decision/communication）。"""
    row = db.fetch_one(
        "SELECT version, profile, evidence_refs, created_at FROM cognition.profile_versions "
        "WHERE review_status = 'confirmed' ORDER BY version DESC LIMIT 1"
    )
    return row or {"version": 0, "profile": {}, "evidence_refs": [],
                   "note": "no confirmed profile yet"}


@router.get("/cognition/versions")
def list_versions():
    return db.fetch_all(
        "SELECT version, review_status, created_at FROM cognition.profile_versions "
        "ORDER BY version DESC"
    )


@router.get("/cognition/proposals")
def list_proposals(status: str = "pending"):
    return db.fetch_all(
        "SELECT id, profile_type, proposal, evidence_count, status, created_at "
        "FROM cognition.profile_proposals WHERE status = :s ORDER BY created_at DESC",
        {"s": status},
    )


@router.post("/cognition/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: str):
    """确认认知候选 → 发布新 Profile Version（版本快照，不覆盖）。"""
    prop = db.fetch_one(
        "SELECT id, profile_type, proposal, status FROM cognition.profile_proposals "
        "WHERE id = :id",
        {"id": proposal_id},
    )
    if not prop:
        raise HTTPException(404, "proposal not found")
    if prop["status"] != "pending":
        raise HTTPException(400, f"proposal status is {prop['status']}")

    # L3：整段放进单事务 + 事务级咨询锁串行化版本分配。
    # 否则两个并发审批会读到同一个 MAX(version)，一个撞 UNIQUE(version) 500，
    # 另一个还会基于同一份旧 Profile 覆盖，造成认知画像更新丢失。
    with db.transaction():
        db.execute("SELECT pg_advisory_xact_lock(817263451)")
        claimed = db.fetch_one(
            "UPDATE cognition.profile_proposals SET status = 'confirmed' "
            "WHERE id = :id AND status = 'pending' RETURNING id",
            {"id": proposal_id},
        )
        if not claimed:
            raise HTTPException(409, "proposal was already approved by another request")

        current = db.fetch_one(
            "SELECT profile FROM cognition.profile_versions "
            "WHERE review_status = 'confirmed' ORDER BY version DESC LIMIT 1"
        )
        base = current["profile"] if current else {}
        profile = dict(base) if isinstance(base, dict) else {}
        profile.setdefault(prop["profile_type"], []).append(prop["proposal"])

        next_version = (db.fetch_one(
            "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM cognition.profile_versions"
        ) or {"v": 1})["v"]
        db.insert_returning_id(
            "cognition.profile_versions",
            {
                "version": next_version,
                "profile": json.dumps(profile, ensure_ascii=False),
                "evidence_refs": json.dumps(
                    {"proposal_id": str(prop["id"]),
                     "claim_ids": _proposal_claim_ids(prop["proposal"])}, ensure_ascii=False),
                "review_status": "confirmed",
            },
        )
    return {"status": "confirmed", "profile_version": next_version}


# 知识中心 → 认知中心：Claim 类型映射到五类 Profile
CLAIM_TO_PROFILE = {
    "preference": "preference", "belief": "belief", "value": "belief",
    "decision": "decision", "decision_pattern": "decision",
    "experience": "knowledge", "fact": "knowledge", "relation": "relationship",
    "communication": "communication",
}


def _proposal_claim_ids(proposal) -> list[str]:
    p = proposal if isinstance(proposal, dict) else {}
    ids = p.get("claim_ids") or ([p["claim_id"]] if p.get("claim_id") else [])
    return [str(x) for x in ids]


def generate_proposal_from_claim(claim_id: str) -> dict:
    """由一条已确认的 Claim 生成认知候选，并写入真实 evidence_claim_ids 引用。

    这是「知识中心 → 认知中心」的连接点：此前认知候选只有 seed_demo 手工数据。
    """
    row = db.fetch_one(
        "SELECT id, claim_type, subject, predicate, object_value "
        "FROM knowledge.claims WHERE id = :id AND status = 'approved'",
        {"id": claim_id},
    )
    if not row:
        return {"skipped": "claim_not_approved"}
    dup = db.fetch_one(
        "SELECT id FROM cognition.profile_proposals "
        "WHERE CAST(:id AS uuid) = ANY(evidence_claim_ids) LIMIT 1",
        {"id": claim_id},
    )
    if dup:
        return {"skipped": "already_generated", "id": str(dup["id"])}
    ev = db.fetch_one(
        "SELECT count(*) AS n FROM knowledge.claim_evidence WHERE claim_id = :id",
        {"id": claim_id},
    )
    subj = row["subject"] if isinstance(row["subject"], dict) else {}
    statement = (f"{subj.get('name') or '我'} · {row['predicate'] or ''}："
                 f"{json.dumps(row['object_value'] or {}, ensure_ascii=False)}")[:300]
    proposal = {
        "claim_id": claim_id, "claim_ids": [claim_id], "claim_type": row["claim_type"],
        "subject": subj, "predicate": row["predicate"],
        "object_value": row["object_value"], "statement": statement,
        "scope": "global", "status": "proposed",
    }
    pid = db.fetch_one(
        "INSERT INTO cognition.profile_proposals "
        "(profile_type, proposal, evidence_count, evidence_claim_ids) "
        "VALUES (:t, CAST(:p AS jsonb), :n, CAST(:ids AS uuid[])) RETURNING id",
        {"t": CLAIM_TO_PROFILE.get(row["claim_type"] or "", "knowledge"),
         "p": json.dumps(proposal, ensure_ascii=False),
         "n": int((ev or {}).get("n") or 1),
         "ids": "{" + claim_id + "}"},
    )["id"]
    return {"created": True, "id": str(pid),
            "profile_type": CLAIM_TO_PROFILE.get(row["claim_type"] or "", "knowledge")}


@router.post("/cognition/proposals/generate")
def generate_proposals(limit: int = 50):
    """从所有已确认事实补齐认知候选（含历史数据），已生成过的自动跳过。"""
    rows = db.fetch_all(
        "SELECT c.id FROM knowledge.claims c WHERE c.status = 'approved' "
        "AND NOT EXISTS (SELECT 1 FROM cognition.profile_proposals p "
        "                WHERE c.id = ANY(p.evidence_claim_ids)) "
        "ORDER BY c.created_at DESC LIMIT :l",
        {"l": max(1, min(limit, 200))},
    )
    created = 0
    for r in rows:
        try:
            if generate_proposal_from_claim(str(r["id"])).get("created"):
                created += 1
        except Exception:  # noqa: BLE001  单条失败不影响整批
            continue
    return {"created": created, "scanned": len(rows)}


class FeedbackIn(BaseModel):
    feedback_type: str  # fact_error / cognition_mismatch / judgment_mismatch
    target_id: str | None = None
    content: str = ""


@router.get("/cognition/feedbacks")
def list_feedbacks(limit: int = 50):
    """反馈处理队列：可查询的待处理工单，而不是只返回一个假想队列名。"""
    return db.fetch_all(
        "SELECT id, feedback_type, target_id, content, handled, created_at "
        "FROM audit.user_feedbacks ORDER BY created_at DESC LIMIT :l",
        {"l": max(1, min(limit, 200))},
    )


@router.post("/cognition/feedback")
def submit_feedback(body: FeedbackIn):
    """反馈落库为待处理工单：事实错误→知识修正；认知错误→画像复核；决策错误→决策评估。

    明确边界：反馈不会自动改写事实或认知画像（那需要人工审核），
    `ticket_id` 可在 /cognition/feedbacks 里追踪，队列名不再是空承诺。
    """
    import re

    from app.modules.audit import service as audit

    queue = {
        "fact_error": "knowledge_correction",
        "cognition_mismatch": "profile_review",
        "judgment_mismatch": "decision_evaluation",
    }.get(body.feedback_type)
    if not queue:
        raise HTTPException(400, "invalid feedback_type")
    target = (str(body.target_id) if body.target_id and re.fullmatch(
        r"[0-9a-fA-F-]{36}", str(body.target_id)) else None)
    with db.transaction():
        fid = db.insert_returning_id(
            "audit.user_feedbacks",
            {"feedback_type": body.feedback_type, "target_id": target,
             "content": body.content, "handled": False},
        )
        audit.log_access("user", "feedback_submitted", "user_feedback", fid,
                         {"queue": queue, "feedback_type": body.feedback_type})
    return {"status": "queued", "ticket_id": fid, "processing_queue": queue,
            "handled": False,
            "note": "反馈已登记为待处理工单；不会自动修改事实或认知画像，需人工复核"}
