"""知识管理 API：Claim 审核 → Fact 发布（不自动转 Fact）。"""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import current_actor
from app.db import session as db

router = APIRouter()


def _clamp_limit(limit: int, default: int = 1000, maximum: int = 5000) -> int:
    """分页参数钳制：负数/超大值不得直通 SQL LIMIT。

    控制台的列表分页在浏览器端对完整结果集翻页，所以这里必须一次返回全量；
    早期默认 50/上限 200 会把「共 N 条」永远卡在 50，审核后由后面的行补位，
    看起来就像「转为事实没生效」。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    if limit < 1:              # 0 / 负数不是「取 1 条」，而是明显的非法入参
        return default
    return min(limit, maximum)


# 事实写入结果 → 审核结果。抽出纯函数，便于脱离数据库回归。
_PUBLISHED_ACTIONS = {"inserted", "superseded", "backdated_stint"}
_EXISTING_ACTIONS = {"duplicate", "reinforced"}


def _review_outcome(outcome: dict | None) -> dict:
    """把本体 write_fact 的动作翻译成「这次审核到底发生了什么」。

    旧实现用 `action != "skipped"` 当作发布成功，导致 duplicate / reinforced /
    contested 都被报成已发布，而事实列表只展示 active，用户看到的是「没生效」。
    """
    out = outcome or {}
    action = out.get("action") or "skipped"
    status = out.get("status") or "none"
    if action in _PUBLISHED_ACTIONS:
        return {"fact_published": True, "fact_status": status,
                "needs_conflict_review": False,
                "message": "已发布为版本化事实"}
    if action in _EXISTING_ACTIONS:
        return {"fact_published": False, "fact_status": "active",
                "needs_conflict_review": False,
                "message": "同一断言此前已确认，本次仅登记为重复佐证，未重复发布"}
    if action == "contested":
        return {"fact_published": False, "fact_status": "contested",
                "needs_conflict_review": True,
                "message": "与现有事实冲突，已挂入争议通道，请到本体中心裁决"}
    return {"fact_published": False, "fact_status": status,
            "needs_conflict_review": False,
            "message": out.get("reason") or "事实写入被跳过"}


@router.get("/knowledge/claims")
def list_claims(status: str = "candidate", limit: int = 1000,
                review_priority: str = ""):
    """L15：低置信度（review_priority=low_confidence）默认前排，便于优先复核。"""
    limit = _clamp_limit(limit)
    priority_filter = (" AND c.review_priority = :p"
                       if review_priority in ("normal", "low_confidence") else "")
    params = {"s": status, "l": limit}
    if priority_filter:
        params["p"] = review_priority
    return db.fetch_all(
        "SELECT c.id, c.claim_type, c.subject, c.predicate, c.object_value, "
        "c.confidence, c.status, c.review_priority, c.created_at, "
        "(SELECT count(*) FROM knowledge.claim_evidence ce WHERE ce.claim_id = c.id) "
        "AS evidence_count, src.document_id, src.document_title, src.source_kind "
        "FROM knowledge.claims c "
        "LEFT JOIN LATERAL ("
        "  SELECT d.id AS document_id, d.title AS document_title, "
        "         COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind "
        "  FROM knowledge.claim_evidence ce "
        "  JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        "  JOIN source.documents d ON d.id = es.document_id "
        "  WHERE ce.claim_id = c.id LIMIT 1"
        ") src ON TRUE "
        f"WHERE c.status = :s{priority_filter} "
        "ORDER BY (c.review_priority = 'low_confidence') DESC, "
        "c.created_at DESC LIMIT :l",
        params,
    )


@router.post("/knowledge/maintenance/dedup-evidence")
def dedup_evidence(dry_run: bool = True):
    """L12 存量清理：同文档内容/定位相同的 evidence_spans 合并为一条。

    先把 claim_evidence 与 decision_evidence 的引用迁到保留行，再删重复行；
    默认 dry_run=True 只报数，显式 dry_run=false 才真正执行。全程单事务。
    """
    with db.transaction():
        db.execute(
            "CREATE TEMP TABLE _ev_map ON COMMIT DROP AS "
            "SELECT id AS dup_id, "
            "  first_value(id) OVER (PARTITION BY document_id, md5(content), "
            "    md5(locator::text) ORDER BY created_at, id) AS keeper_id "
            "FROM knowledge.evidence_spans"
        )
        before = db.fetch_one(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE dup_id <> keeper_id) AS dupes FROM _ev_map"
        ) or {"total": 0, "dupes": 0}
        if dry_run:
            return {"dry_run": True, "total": before["total"],
                    "duplicates": before["dupes"], "removed": 0}

        db.execute(
            "INSERT INTO knowledge.claim_evidence (claim_id, evidence_id, support_type) "
            "SELECT ce.claim_id, m.keeper_id, ce.support_type "
            "FROM knowledge.claim_evidence ce "
            "JOIN _ev_map m ON m.dup_id = ce.evidence_id "
            "WHERE m.dup_id <> m.keeper_id "
            "ON CONFLICT (claim_id, evidence_id) DO NOTHING"
        )
        db.execute(
            "DELETE FROM knowledge.claim_evidence ce USING _ev_map m "
            "WHERE ce.evidence_id = m.dup_id AND m.dup_id <> m.keeper_id"
        )
        db.execute(
            "UPDATE decision.decision_evidence de SET reference_id = m.keeper_id "
            "FROM _ev_map m WHERE de.reference_id = m.dup_id AND m.dup_id <> m.keeper_id"
        )
        db.execute(
            "DELETE FROM knowledge.evidence_spans es USING _ev_map m "
            "WHERE es.id = m.dup_id AND m.dup_id <> m.keeper_id"
        )
        after = db.fetch_one("SELECT count(*) AS n FROM knowledge.evidence_spans")
    return {"dry_run": False, "total": before["total"],
            "duplicates": before["dupes"], "removed": before["dupes"],
            "remaining": after["n"]}


_FACT_STATUS_FILTERS = ("active", "contested", "superseded", "withdrawn")


def _fact_status_clause(status: str) -> tuple[str, dict]:
    """事实列表状态过滤：白名单外一律回落 active，任意字符串不得直通 SQL。"""
    st = status if status in _FACT_STATUS_FILTERS else "active"
    return "f.status = :st", {"st": st}


@router.get("/knowledge/facts")
def list_facts(limit: int = 1000, status: str = "active",
               include_restricted: bool = False, request: Request = None):
    """事实列表：active=已发布，contested=争议通道，withdrawn=已撤销留痕。

    G2：默认排除 restricted / 未知可见性的事实（与问答召回、实体画像同一白名单）；
    本人需要核对敏感留痕时须显式带 include_restricted=true（仅 owner 级主体生效）。
    P1-4：叠加租户 / 归属逐点过滤（X-Tenant-Id / X-User-Id / X-Permission-Context），
    无头调用按 owner 级回落，行为与升级前一致。
    """
    from app.policy import is_owner_class, principal_from, read_clause_visibility_only

    principal = None
    if request is not None:
        try:
            from app.modules.semantic_runtime.context import from_request

            principal = principal_from(from_request(request))
        except HTTPException:
            principal = None

    limit = _clamp_limit(limit, default=1000)
    where, extra = _fact_status_clause(status)
    if principal is not None:
        # P1-4 逐点过滤：租户隔离恒生效；可见性白名单在 owner 级显式
        # include_restricted 时放开（敏感留痕核对），其余场景与白名单一致
        extra.setdefault("p_tenant", principal["tenant_id"])
        where += " AND COALESCE(f.tenant_id, 'default') = :p_tenant"
        if not (include_restricted and is_owner_class(principal)):
            where += " AND " + read_clause_visibility_only("f", principal, extra)
    elif not include_restricted:
        from app.visibility import retrievable_clause

        where += f" AND {retrievable_clause('f')}"
    return db.fetch_all(
        "SELECT f.id, f.claim_id, f.subject_id, f.predicate, f.object_value, "
        "f.valid_from, f.valid_to, f.status, f.version, f.confidence, f.trust_tier, "
        "f.visibility, f.contested, f.superseded_by, f.source_ref, "
        "f.tenant_id, f.datatype, f.verification_status, f.ontology_version, "
        "f.source_id, f.evidence_id, f.created_at, f.updated_at, f.created_by, "
        "e.canonical_name AS subject_name, "
        "src.document_id, src.document_title, src.source_kind "
        "FROM knowledge.facts f "
        "LEFT JOIN ontology.entities e ON e.id = f.subject_id "
        "LEFT JOIN LATERAL ("
        "  SELECT d.id AS document_id, d.title AS document_title, "
        "         COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind "
        "  FROM knowledge.claim_evidence ce "
        "  JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        "  JOIN source.documents d ON d.id = es.document_id "
        "  WHERE ce.claim_id = f.claim_id LIMIT 1"
        ") src ON TRUE "
        f"WHERE {where} ORDER BY f.recorded_at DESC NULLS LAST LIMIT :l",
        {"l": limit, **extra},
    )


@router.get("/knowledge/contracts/facts")
def fact_contract_status():
    """SIL-DC-05 / DC-08 / DC-09 自证：14 项标准字段 + 7 项公共字段逐字段状态。

    返回的是**实库直查结果**（information_schema + 非空计数），不是文档声明，
    因此可以用来发现「规范说已具备、库里其实没有」这类脱节。
    """
    from app.modules.knowledge import contract

    return contract.contract_status()


@router.post("/knowledge/contracts/facts/backfill")
def fact_contract_backfill(dry_run: bool = True):
    """历史事实补值 + 收紧约束（NOT NULL / 枚举 CHECK / 索引 / updated_at 触发器）。

    默认 dry_run=True 只报待补行数；显式 dry_run=false 才写库。幂等：
    每条回填都带 `IS NULL` 条件，重复执行不会覆盖已有值。
    """
    from app.modules.knowledge import contract

    return contract.backfill(dry_run=dry_run)


@router.get("/knowledge/facts/{fact_id}/contract")
def fact_contract_row(fact_id: str):
    """单条事实的契约视图（含 source/evidence 解析结果）。"""
    from uuid import UUID

    from app.modules.knowledge import contract

    try:                      # 非法 uuid 是入参问题，不是服务端错误
        UUID(fact_id)
    except (ValueError, TypeError, AttributeError) as e:
        raise HTTPException(400, "fact_id must be a uuid") from e
    row = contract.contract_row(fact_id=fact_id)
    if not row:
        raise HTTPException(404, "fact not found")
    return row


class WithdrawIn(BaseModel):
    reason: str = ""


@router.post("/knowledge/facts/{fact_id}/withdraw")
def withdraw_published_fact(fact_id: str, request: Request,
                            body: WithdrawIn | None = None):
    """撤销发布（软删除）：active → withdrawn，**不删行**。

    所有以 fact_id 软引用的下游（决策证据 reference_id、不可变审计快照、
    superseded_by 版本链）仍可解析；列表与检索只查 active，用户可见效果与删除一致。
    状态、source_ref 留痕与审计日志在同一事务内完成；重复撤销返回 400。
    """
    from app.modules.ontology import service as ontology

    actor = current_actor(request)
    reason = (body.reason if body else "") or ""
    try:
        return ontology.withdraw_fact(fact_id, actor=actor, reason=reason)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class BatchWithdrawIn(BaseModel):
    fact_ids: list[str]
    reason: str = ""
    dry_run: bool = False
    # 60 秒内累计超过上限时，必须显式带上它才放行（防连点 / 脚本循环）
    confirm_scope: bool = False


def _client_info(request: Request) -> dict:
    """调用方标识：出事时能回答「谁在循环调这个接口」。"""
    peer = getattr(request, "client", None)
    return {
        "host": getattr(peer, "host", "") or "",
        "port": getattr(peer, "port", 0) or 0,
        "ua": (request.headers.get("user-agent") or "")[:160],
        "origin": (request.headers.get("origin") or "")[:120],
    }


@router.post("/knowledge/facts/batch-withdraw")
def batch_withdraw_published_facts(body: BatchWithdrawIn, request: Request):
    """一次事务批量撤销已发布事实（软删除，行与证据链保留）。

    整批共用一个事务：状态变更、source_ref 留痕、审计日志同进同退；
    dry_run=true 只返回预检结果不写库。非 active / 不存在的 id 计入 skipped，
    不影响其余行；非法 id 或超过 2000 条会整批拒绝（400），不产生部分写入。
    另有累计闸门：60 秒内累计撤销超过 200 条时，未带 confirm_scope=true 直接 400。
    """
    from app.modules.ontology import service as ontology

    try:
        return ontology.batch_withdraw_facts(
            body.fact_ids, actor=current_actor(request),
            reason=body.reason, dry_run=body.dry_run,
            confirm_scope=body.confirm_scope, client=_client_info(request),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class RestoreIn(BaseModel):
    reason: str = ""


@router.post("/knowledge/facts/{fact_id}/restore")
def restore_withdrawn_fact(fact_id: str, request: Request,
                           body: RestoreIn | None = None):
    """恢复发布（撤销的逆操作）：withdrawn → active，**不删不造行**。

    撤销是软删，所以恢复只是把状态与生效区间改回来，并把「谁/何时/为什么恢复」
    追加进 source_ref；撤销时的原始留痕作为 previous_withdrawal 一起保留。
    与当前活跃值冲突（完全相同 / 单值维度已有新值）会返回 400 并说明原因。
    """
    from app.modules.ontology import service as ontology

    try:
        return ontology.restore_fact(
            fact_id, actor=current_actor(request),
            reason=(body.reason if body else "") or "",
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class BatchRestoreIn(BaseModel):
    fact_ids: list[str]
    reason: str = ""
    dry_run: bool = False


@router.post("/knowledge/facts/batch-restore")
def batch_restore_withdrawn_facts(body: BatchRestoreIn, request: Request):
    """一次事务批量恢复已撤销事实（与 /batch-withdraw 对称的逆操作）。

    整批共用一个事务；非 withdrawn / 不存在的 id 与冲突行计入 skipped 并逐条给出原因，
    不影响其余行。dry_run=true 只预检不写库。
    """
    from app.modules.ontology import service as ontology

    try:
        return ontology.restore_facts(
            body.fact_ids, actor=current_actor(request),
            reason=body.reason, dry_run=body.dry_run,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/knowledge/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    row = db.fetch_one(
        "SELECT e.id, e.document_id, e.content, e.locator, d.title "
        "FROM knowledge.evidence_spans e "
        "JOIN source.documents d ON d.id = e.document_id WHERE e.id = :id",
        {"id": evidence_id},
    )
    if not row:
        raise HTTPException(404, "evidence not found")
    row["id"] = str(row["id"])
    return row


@router.get("/knowledge/claims/{claim_id}/evidences")
def claim_evidences(claim_id: str):
    """候选/已发布知识的证据链：可直接下钻到原文、来源类型与钉钉原文链接。"""
    return db.fetch_all(
        "SELECT e.id, e.content, e.locator, e.evidence_type, "
        "d.id AS document_id, d.title AS document_title, "
        "COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind, "
        "d.metadata->>'origin_url' AS origin_url "
        "FROM knowledge.claim_evidence ce "
        "JOIN knowledge.evidence_spans e ON e.id = ce.evidence_id "
        "JOIN source.documents d ON d.id = e.document_id "
        "WHERE ce.claim_id = :id ORDER BY e.created_at LIMIT 20",
        {"id": claim_id},
    )


@router.post("/knowledge/claims/{claim_id}/approve")
def approve_claim(claim_id: str):
    """人工审核通过：Claim → Fact（版本化写入，旧 Fact 标记 superseded）。"""
    claim = db.fetch_one(
        "SELECT id, claim_type, subject, predicate, object_value, status, "
        "valid_from, confidence FROM knowledge.claims WHERE id = :id",
        {"id": claim_id},
    )
    if not claim:
        raise HTTPException(404, "claim not found")
    if claim["status"] != "candidate":
        raise HTTPException(400, f"claim status is {claim['status']}")

    # 单事务：Fact + Claim 状态 + 关系 + 记忆要么全成、要么全回滚。
    # 事实发布在单事务内完成。
    with db.transaction():
        out = _approve_in_tx(claim_id, claim)
    return out


def _approve_in_tx(claim_id: str, claim: dict) -> dict:
    # 幂等：已发布过 Fact 的 Claim 不重复写下游。
    # 早期这里直接 return，却没把 claim 从 candidate 改成 approved —— 一旦出现
    # 「有 fact 但 claim 仍是候选」的数据，用户反复点「转为事实」都不会有任何变化。
    existing = db.fetch_one(
        "SELECT id FROM knowledge.facts WHERE claim_id = :c AND status = 'active' LIMIT 1",
        {"c": claim_id},
    )
    if existing:
        db.execute(
            "UPDATE knowledge.claims SET status = 'approved' "
            "WHERE id = :id AND status = 'candidate'", {"id": claim_id},
        )
        return {"status": "approved", "fact_published": False,
                "fact_status": "active", "needs_conflict_review": False,
                "fact_id": str(existing["id"]), "fact_write": {"action": "already_published"},
                "message": "该主张此前已发布过事实，已补齐审核状态"}

    # 1) 主体解析：查不到时兜底创建实体，避免 facts.subject_id 悬空
    subject_id = None
    subject = claim["subject"] if isinstance(claim["subject"], dict) else {}
    if subject.get("name"):
        ent = db.fetch_one(
            "SELECT id FROM ontology.entities WHERE canonical_name = :n LIMIT 1",
            {"n": subject["name"]},
        )
        subject_id = str(ent["id"]) if ent else None
        if not subject_id:
            try:
                from app.modules.distillation import service as distill

                subject_id = (distill.resolve_entity(subject) or {}).get("match_id")
            except Exception:  # noqa: BLE001
                subject_id = None
        if not subject_id:
            ent = db.fetch_one(
                "SELECT id FROM ontology.entities WHERE canonical_name = :n LIMIT 1",
                {"n": subject["name"]},
            )
            subject_id = str(ent["id"]) if ent else None

    # 事实写入统一收口到本体服务：本人审核 = 最高信任级，
    # 由信任护栏决定「取代当前值 / 作为更早历史段保留 / 标记争议」。
    from app.modules.ontology import service as ontology

    outcome = ontology.write_fact(
        predicate=claim["predicate"],
        object_value=claim["object_value"] or {},
        subject_id=subject_id,
        claim_id=claim_id,
        valid_from=claim["valid_from"],
        confidence=float(claim["confidence"]) if claim["confidence"] is not None else None,
        writer="owner",
        source_ref={"kind": "claim_approval", "claim_id": claim_id,
                    "claim_type": claim["claim_type"], "reviewed_by": "owner"},
    )
    # 写入被跳过（如谓词为空）时不能把 Claim 标成 approved：否则「审核成功」
    # 但事实表里什么都没有，且该 Claim 再也无法重新审核。整个事务回滚。
    if outcome.get("action") == "skipped":
        raise HTTPException(400, f"无法发布事实：{outcome.get('reason') or '写入被跳过'}")
    review = _review_outcome(outcome)

    # 同值幂等 / 提升信任级：不新增事实行，但必须把本次审核留痕在该事实上，
    # 否则这条 Claim 变成「已审核却查不到对应事实」，证据追溯链就断了。
    if outcome.get("action") in _EXISTING_ACTIONS and outcome.get("fact_id"):
        db.execute(
            "UPDATE knowledge.facts SET source_ref = "
            "  COALESCE(source_ref, '{}'::jsonb) || jsonb_build_object("
            "    'corroborating_claims', "
            "    COALESCE(source_ref->'corroborating_claims', '[]'::jsonb) "
            "      || CAST(:extra AS jsonb)) "
            "WHERE id = CAST(:fid AS uuid)",
            {"fid": outcome["fact_id"], "extra": json.dumps([claim_id])},
        )

    db.execute(
        "UPDATE knowledge.claims SET status = 'approved' WHERE id = :id", {"id": claim_id}
    )

    # 2) 事实确认后激活同批本体关系（蒸馏时写的是 candidate，前端原先看不到）
    db.execute(
        "UPDATE ontology.relations SET status = 'active' WHERE evidence_claim_id = :id",
        {"id": claim_id},
    )
    # 2.1) 按「事实 → 关系」投影补一条边：结构化事实（{"value": ...}）在蒸馏时
    # 根本走不到建边逻辑，只有在这里补，图才不会与事实底座脱节。
    edge_result = {"created": False, "reason": "no_fact_id"}
    if outcome.get("fact_id"):
        from app.modules.ontology import workbench as _wb
        try:
            edge_result = _wb.ensure_fact_edge(str(outcome["fact_id"]))
        except Exception as exc:          # 建边失败不能让审核回滚：事实已经成立
            edge_result = {"created": False, "reason": f"error: {str(exc)[:120]}"}

    # 3) 写入个人记忆，带上真实 claim_id 引用（questions/reply/decision 可检索）
    mem_type = {"preference": "preference", "belief": "semantic",
                "experience": "episodic", "decision": "decision_pattern"
                }.get(claim["claim_type"] or "", "semantic")
    db.execute(
        "INSERT INTO memory.memory_items "
        "(memory_type, content, confidence, source_claim_ids, status) "
        "SELECT :t, CAST(:c AS jsonb), :conf, CAST(:ids AS uuid[]), 'confirmed' "
        "WHERE NOT EXISTS (SELECT 1 FROM memory.memory_items "
        "                  WHERE CAST(:cid AS uuid) = ANY(source_claim_ids))",
        {"t": mem_type,
         "c": json.dumps({"subject": subject, "predicate": claim["predicate"],
                          "object_value": claim["object_value"]}, ensure_ascii=False),
         "conf": claim["confidence"], "ids": "{" + claim_id + "}", "cid": claim_id},
    )

    return {
        "status": "approved",
        "fact_published": review["fact_published"],
        "fact_status": review["fact_status"],
        "needs_conflict_review": review["needs_conflict_review"],
        "message": review["message"],
        "subject_id": subject_id,
        "fact_id": outcome.get("fact_id"),
        "edge_sync": edge_result,
        "fact_write": {k: outcome.get(k) for k in
                       ("action", "fact_id", "version", "status", "reason")},
    }


@router.post("/knowledge/claims/{claim_id}/reject")
def reject_claim(claim_id: str):
    n = db.fetch_one(
        "WITH u AS (UPDATE knowledge.claims SET status = 'rejected' "
        " WHERE id = :id AND status = 'candidate' RETURNING 1) "
        "SELECT count(*) AS n FROM u",
        {"id": claim_id},
    )["n"]
    if not n:
        raise HTTPException(400, "claim not found or not in candidate state")
    return {"status": "rejected"}


@router.post("/knowledge/maintenance/backfill")
def backfill_knowledge():
    """一次性数据修复：清自环关系、回填历史事实的记忆。

    早期版本审核通过 Claim 时未写 memory_items，relation 全是 source=target
    自环；这里补齐历史缺口，让「事实 → 记忆」链路在数据上也闭合。
    可重复执行（各步骤均幂等）。
    """
    stats: dict = {}
    with db.transaction():
        stats["self_loop_relations_deleted"] = (db.fetch_one(
            "WITH d AS (DELETE FROM ontology.relations "
            " WHERE source_entity_id = target_entity_id "
            "   AND relation_type = 'extracted_from' RETURNING 1) "
            "SELECT count(*) AS n FROM d"
        ) or {"n": 0})["n"]
        stats["real_relations_built"] = (db.fetch_one(
            "WITH ins AS ("
            " INSERT INTO ontology.relations "
            " (source_entity_id, relation_type, target_entity_id, evidence_claim_id, status) "
            " SELECT DISTINCT f.subject_id, left(f.predicate, 100), e.id, f.claim_id, 'active' "
            " FROM knowledge.facts f "
            " JOIN knowledge.claims c ON c.id = f.claim_id "
            " JOIN ontology.entities e ON e.status = 'active' AND e.id <> f.subject_id "
            "   AND c.object_value::text LIKE '%' || e.canonical_name || '%' "
            " WHERE f.status = 'active' AND f.subject_id IS NOT NULL "
            "   AND f.predicate IS NOT NULL "
            "   AND NOT EXISTS (SELECT 1 FROM ontology.relations r "
            "     WHERE r.source_entity_id = f.subject_id AND r.target_entity_id = e.id "
            "       AND r.relation_type = left(f.predicate, 100) "
            "       AND r.evidence_claim_id = f.claim_id) "
            " RETURNING 1) SELECT count(*) AS n FROM ins"
        ) or {"n": 0})["n"]
        stats["memories_backfilled"] = (db.fetch_one(
            "WITH ins AS ("
            " INSERT INTO memory.memory_items "
            " (memory_type, content, confidence, source_claim_ids, status) "
            " SELECT CASE c.claim_type "
            "   WHEN 'preference' THEN 'preference' WHEN 'belief' THEN 'semantic' "
            "   WHEN 'experience' THEN 'episodic' WHEN 'decision' THEN 'decision_pattern' "
            "   ELSE 'semantic' END, "
            "   jsonb_build_object('subject', c.subject, 'predicate', c.predicate, "
            "                      'object_value', c.object_value), "
            "   c.confidence, ARRAY[c.id]::uuid[], 'confirmed' "
            " FROM knowledge.claims c WHERE c.status = 'approved' "
            "   AND NOT EXISTS (SELECT 1 FROM memory.memory_items m "
            "                   WHERE c.id = ANY(m.source_claim_ids)) "
            " RETURNING 1) SELECT count(*) AS n FROM ins"
        ) or {"n": 0})["n"]
    return {"status": "ok", **stats}


@router.post("/knowledge/maintenance/rechunk")
def rechunk_documents():
    """为「有原文但没有切片」的历史文档重建切片。

    早期版本 CHUNKING 只生成内存列表不落库，导致 21 份文档检索不到原文。
    这里只从已存原文重新切片（纯本地、不调用 LLM、不产生 Claim/Fact），
    不改变任何审核状态；重复执行幂等。
    """
    from app.modules.document_parser import parser as doc_parser
    from app.modules.ingestion import storage
    from app.modules.embeddings import service as embeddings

    rows = db.fetch_all(
        "SELECT d.id, d.title FROM source.documents d "
        "WHERE NOT EXISTS (SELECT 1 FROM knowledge.semantic_chunks c "
        "                  WHERE c.document_id = d.id) ORDER BY d.created_at"
    )
    stats = {"scanned": len(rows), "rechunked": 0, "chunks": 0,
             "skipped_no_text": 0, "failed": []}
    for row in rows:
        did = str(row["id"])
        try:
            raw = storage.get_document_text(did)
            if not raw or not raw.strip():
                stats["skipped_no_text"] += 1
                continue
            chunks = doc_parser.chunk_text(doc_parser.parse_plain(raw))
            # 统一走 embed_chunks：与首次蒸馏同一套写入，补齐向量 + 本体坐标 + model/version
            n = embeddings.embed_chunks(
                did, chunks, rebuild_from="maintenance/rechunk"
            )
            stats["rechunked"] += 1
            stats["chunks"] += n
        except Exception as e:  # noqa: BLE001  单份失败不影响其它文档
            stats["failed"].append({"id": did, "error": str(e)[:120]})
    return {"status": "ok", **stats}


@router.post("/knowledge/claims/batch-review")
def batch_review(body: dict):
    """批量审核候选知识：action=approve → 转事实；action=reject → 拒绝。

    逐条执行并保留单条审核的校验（仅 candidate 可审），返回成功/跳过明细，
    避免一条失败导致整批回滚。
    """
    ids = (body or {}).get("claim_ids") or []
    action = (body or {}).get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "claim_ids must be a non-empty list")
    ids = [str(i) for i in ids[:200]]  # 单批上限 200，防误操作

    succeeded, skipped = [], []
    summary = {"published": 0, "already_exists": 0,
               "conflict_review": 0, "rejected": 0}
    for cid in ids:
        try:
            if action == "approve":
                res = approve_claim(cid)
                if res.get("fact_published"):
                    summary["published"] += 1
                elif res.get("needs_conflict_review"):
                    summary["conflict_review"] += 1
                else:
                    summary["already_exists"] += 1
            else:
                reject_claim(cid)
                summary["rejected"] += 1
            succeeded.append(cid)
        except HTTPException as e:
            skipped.append({"id": cid, "reason": str(e.detail)})
        except Exception as e:  # noqa: BLE001
            skipped.append({"id": cid, "reason": str(e)[:120]})
    return {"action": action, "total": len(ids),
            "succeeded": len(succeeded), "skipped": skipped, **summary}
