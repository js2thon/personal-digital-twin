"""文档 API：上传、查看、重新处理（重新蒸馏生成新版本，不覆盖旧版本）。"""
import json
import os
import threading

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile

from app.auth import current_actor
from app.config import settings
from app.modules.distillation import review as review_svc
from app.db import session as db
from app.modules.ingestion import storage
from app.modules.llm.client import stage_idempotency_key

router = APIRouter()


def _dispatch_distillation(run_id: str) -> None:
    """调度蒸馏任务。本地 eager 模式下用后台线程执行，保证 HTTP 立即返回。"""
    if os.environ.get("CELERY_TASK_ALWAYS_EAGER") == "1":
        from app.workers.pipeline_tasks import run_distillation

        def _run():
            try:
                run_distillation(run_id)
            except Exception:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                # worker 异常退出时也要回填复核验证结论，否则「重新蒸馏」
                # 的 verification 会永远停在 pending，闭环缺一角。
                try:
                    from app.modules.distillation.review import verify_retry

                    verify_retry(run_id, "FAILED",
                                 {"reason": "worker_exception", "published": 0})
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=_run, daemon=True).start()
    else:
        from app.workers.pipeline_tasks import run_distillation

        run_distillation.delay(run_id)


def _enqueue(title: str, text: str, source_type: str, source_name: str,
             meta: dict, engine: str | None = None) -> dict:
    """统一的蒸馏入口：登记数据源 → 存储原文 → 幂等建任务 → 派发。

    幂等语义按 run 状态分流：
    - COMPLETED / 执行中 / 排队中（含 RETRY_WAIT）→ deduplicated，不重复执行；
    - FAILED / NEEDS_REVIEW / CANCELLED → 重置为 PENDING 并重新派发（可重蒸）。
    """
    text = storage.sanitize_text(text)
    source_id = storage.ensure_source(source_type, source_name)
    doc_id = storage.store_document(source_id, title, text, "text/markdown", meta)
    key = stage_idempotency_key(doc_id, storage.content_hash(text), settings.llm_model)
    existing = db.fetch_one(
        "SELECT id, status FROM ingest.pipeline_runs WHERE idempotency_key = :k", {"k": key}
    )
    if existing:
        run_id = str(existing["id"])
        from app.workers.pipeline_tasks import reclaim_run

        if reclaim_run(run_id, existing["status"]) == "reset":
            _dispatch_distillation(run_id)
            return {"document_id": doc_id, "run_id": run_id,
                    "deduplicated": False, "resubmitted": True,
                    "title": title, "engine": engine}
        return {"document_id": doc_id, "run_id": run_id,
                "deduplicated": True, "title": title, "engine": engine}
    run_id = db.insert_returning_id(
        "ingest.pipeline_runs",
        {"document_id": doc_id, "pipeline_version": settings.pipeline_version,
         "idempotency_key": key, "status": "PENDING"},
    )
    _dispatch_distillation(str(run_id))
    return {"document_id": doc_id, "run_id": str(run_id),
            "deduplicated": False, "title": title, "engine": engine}


def _run_rows(limit: int) -> list[dict]:
    """任务列表统一查询：附带 error_info、产物计数与可读的复核原因。"""
    rows = db.fetch_all(
        "SELECT r.id, r.document_id, d.title AS document_title, r.status, "
        "r.retry_count, r.created_at, r.updated_at, r.error_info, "
        "COALESCE(d.metadata->>'source_kind', 'upload') AS source_kind, "
        "d.metadata->>'origin_url' AS origin_url, "
        "(SELECT count(*) FROM knowledge.semantic_chunks ch "
        " WHERE ch.document_id = d.id) AS chunks, "
        "(SELECT count(*) FROM knowledge.claim_evidence ce "
        " JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        " WHERE es.document_id = d.id) AS claims, "
        "(SELECT count(*) FROM ingest.pipeline_steps s WHERE s.run_id = r.id) AS steps "
        "FROM ingest.pipeline_runs r "
        "LEFT JOIN source.documents d ON d.id = r.document_id "
        "ORDER BY r.created_at DESC LIMIT :limit",
        {"limit": max(1, min(int(limit or 20), 500))},
    )
    for r in rows:
        r["id"] = str(r.get("id") or "")
        r["document_id"] = str(r.get("document_id") or "")
        diag = review_svc.classify(r.get("error_info"))
        pending_or_closed = r["status"] in review_svc.REVIEW_STATUSES
        # 正常完成的任务没有「待复核原因」，不要给前端一个误导性的「未分类原因」
        r["review_code"] = diag["code"] if pending_or_closed else ""
        r["review_label"] = diag["label"] if pending_or_closed else ""
        r["review_summary"] = diag["summary"] if pending_or_closed else ""
        r["retry_effective"] = diag["retry_effective"]
        r["error_info"] = r.get("error_info") if isinstance(r.get("error_info"), dict) else {}
        r["created_at"] = str(r.get("created_at") or "")[:19]
        r["updated_at"] = str(r.get("updated_at") or "")[:19]
    return rows


@router.get("/documents/runs/recent")
def recent_runs(limit: int = 20):
    return _run_rows(limit)


@router.get("/documents/runs/needs-review")
def needs_review_runs(limit: int = 500):
    """待人工复核汇总：给侧栏角标与复核台提供单一权威口径。"""
    pending = [r for r in _run_rows(limit) if r["status"] == "NEEDS_REVIEW"]
    by_reason: dict[str, int] = {}
    for r in pending:
        by_reason[r["review_code"]] = by_reason.get(r["review_code"], 0) + 1
    return {
        "count": len(pending),
        "by_reason": by_reason,
        "labels": {k: v["label"] for k, v in review_svc.REASON_META.items()},
        "runs": pending,
    }


def _review_target(run_id: str) -> dict:
    try:
        import uuid

        uuid.UUID(run_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "run_id 不是合法 UUID") from None
    row = db.fetch_one(
        "SELECT r.id, r.document_id, r.status, r.error_info, r.retry_count, "
        "r.created_at, r.updated_at, d.title AS document_title "
        "FROM ingest.pipeline_runs r "
        "LEFT JOIN source.documents d ON d.id = r.document_id WHERE r.id = :id",
        {"id": run_id},
    )
    if not row:
        raise HTTPException(404, "run not found")
    return row


@router.get("/documents/runs/{run_id}/review")
def get_run_review(run_id: str):
    """复核台详情：原因诊断 + 产物计数 + 步骤日志 + 历史处置与验证结论。"""
    run = _review_target(run_id)
    did = str(run["document_id"])
    diag = review_svc.classify(run.get("error_info"))
    artifacts = db.fetch_one(
        "SELECT "
        "(SELECT count(*) FROM knowledge.semantic_chunks WHERE document_id = CAST(:d AS uuid)) "
        "  AS chunks, "
        "(SELECT count(*) FROM knowledge.claim_evidence ce "
        " JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        " WHERE es.document_id = CAST(:d AS uuid)) AS claims, "
        "(SELECT count(*) FROM knowledge.claim_evidence ce "
        " JOIN knowledge.claims c ON c.id = ce.claim_id "
        " JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
        " WHERE es.document_id = CAST(:d AS uuid) AND c.status = 'candidate') "
        "  AS candidate_claims",
        {"d": did},
    ) or {}
    steps = db.fetch_all(
        "SELECT step_name, model_version, error_info, created_at "
        "FROM ingest.pipeline_steps WHERE run_id = CAST(:r AS uuid) "
        "ORDER BY created_at, id LIMIT 30",
        {"r": run_id},
    )
    for s in steps:
        s["created_at"] = str(s.get("created_at") or "")[:19]
        s["error_info"] = str(s.get("error_info") or "")[:240]
    return {
        "run": {
            "id": str(run["id"]), "document_id": did,
            "document_title": run.get("document_title") or "-",
            "status": run["status"], "retry_count": run.get("retry_count") or 0,
            "created_at": str(run.get("created_at") or "")[:19],
            "updated_at": str(run.get("updated_at") or "")[:19],
            "error_info": run.get("error_info")
            if isinstance(run.get("error_info"), dict) else {},
        },
        "diagnosis": diag,
        "artifacts": artifacts,
        "steps": steps,
        "history": review_svc.review_history(run_id),
    }


@router.post("/documents/runs/{run_id}/review")
def review_run(run_id: str, body: dict, request: Request):
    """本人处置待复核任务：retry 重跑 / dismiss 判定无需处理，全程留痕 + 审计。"""
    from app.modules.audit.service import log_access

    actor = current_actor(request)
    run = _review_target(run_id)
    action = str((body or {}).get("action") or "").strip().lower()
    if action not in ("retry", "dismiss"):
        raise HTTPException(400, "action must be retry or dismiss")
    before = str(run["status"]).upper()
    if before not in review_svc.REVIEW_STATUSES:
        raise HTTPException(400, f"status is {before}, only NEEDS_REVIEW / "
                               "REVIEW_DISMISSED can be reviewed")
    note = str((body or {}).get("note") or "").strip()[:500]
    reason = review_svc.classify(run.get("error_info"))["code"]
    preflight = review_svc.llm_preflight() if action == "retry" else {}

    if action == "retry":
        from app.workers.pipeline_tasks import reclaim_run

        after = "PENDING"
    else:
        after = "REVIEW_DISMISSED"

    with db.transaction():
        if action == "retry":
            # 与 _enqueue 同一套状态机：终态重置为 PENDING，活动态不并发双蒸
            if reclaim_run(run_id, before) != "reset":
                raise HTTPException(409, "run is active, please wait for it to finish")
            db.execute(
                "UPDATE ingest.pipeline_runs SET error_info = "
                "error_info || CAST(:extra AS jsonb) WHERE id = CAST(:r AS uuid)",
                {"r": run_id,
                 "extra": json.dumps({"last_review": {
                     "action": "retry", "by": actor, "note": note,
                     "original_reason": reason}}, ensure_ascii=False)},
            )
        else:
            db.execute(
                "UPDATE ingest.pipeline_runs SET status = 'REVIEW_DISMISSED', "
                "error_info = error_info || CAST(:extra AS jsonb), "
                "updated_at = NOW() WHERE id = CAST(:r AS uuid)",
                {"r": run_id,
                 "extra": json.dumps({"last_review": {
                     "action": "dismiss", "by": actor, "note": note,
                     "dismissed_reason": reason}}, ensure_ascii=False)},
            )
        rid = review_svc.record_review(
            run_id, action=action, reason=reason, note=note, actor=actor,
            before_status=before, after_status=after, preflight=preflight,
        )
        log_access(actor, f"ingest.run.review.{action}", "pipeline_run", run_id,
                   {"reason": reason, "note": note, "before": before,
                    "after": after, "preflight": preflight, "review_id": rid})

    if action == "retry":
        _dispatch_distillation(run_id)
    # 状态变化会影响 DWS 列表的可蒸馏判定，主动失效 TTL 索引
    try:
        from app.modules.dws_source import distill_status as _ds

        _ds.invalidate_index()
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": after, "action": action, "run_id": run_id,
        "actor": actor, "reason": reason, "preflight": preflight,
        "message": ("已重新排队蒸馏；结果会自动回填到本次复核记录的验证结论"
                    if action == "retry" else
                    "已判定无需处理并关闭，原始诊断与处置留痕保留"),
    }


@router.delete("/documents/runs/{run_id}")
def delete_run(run_id: str, cascade: bool = True):
    """删除蒸馏任务。cascade=True 时同时删除关联文档及其蒸馏产物（证据/知识/切片/步骤）。"""
    from sqlalchemy import text as sql_text

    run = db.fetch_one(
        "SELECT id, document_id FROM ingest.pipeline_runs WHERE id = :id", {"id": run_id}
    )
    if not run:
        raise HTTPException(404, "run not found")
    did = str(run["document_id"])
    title = (db.fetch_one("SELECT title FROM source.documents WHERE id = :id",
                          {"id": did}) or {}).get("title") or "-"
    deleted = {"run": 0, "steps": 0, "claims": 0, "evidences": 0,
               "chunks": 0, "document": 0, "title": title,
               "memories": 0, "proposals": 0, "decision_evidence": 0,
               "facts": 0, "dangling_refs_cleared": 0}

    engine = db.get_engine()
    with engine.begin() as conn:
        def ex(sql, params=None):
            return conn.execute(sql_text(sql), params or {})

        # 该文档证据链上的 Claim（含已发布 Fact 的来源）与切片 ID
        cid_sql = (
            "SELECT DISTINCT ce.claim_id FROM knowledge.claim_evidence ce "
            "JOIN knowledge.evidence_spans es ON es.id = ce.evidence_id "
            "WHERE es.document_id = :did"
        )
        cids = [str(r[0]) for r in ex(cid_sql, {"did": did})] if cascade else []
        chunk_ids = [str(r[0]) for r in ex(
            "SELECT id FROM knowledge.semantic_chunks WHERE document_id = :did",
            {"did": did},
        )] if cascade else []
        binds = {f"c{i}": c for i, c in enumerate(cids)}
        in_clause = ", ".join(f":{k}" for k in binds)
        cid_arr = "{" + ",".join(cids) + "}" if cids else "{}"
        # 决策证据的 reference_id 既可能是 claim_id / chunk_id，也可能是 fact_id
        # （M3 修复后事实型证据写的是 fact_id）。删 facts 之前先收集其 id，
        # 否则这些证据会永久悬空、决策链失效检测也看不到它们。
        fact_ids = [str(r[0]) for r in ex(
            f"SELECT id FROM knowledge.facts WHERE claim_id IN ({in_clause})", binds
        )] if cids else []
        ref_ids = cids + chunk_ids + fact_ids
        ref_arr = "{" + ",".join(ref_ids) + "}" if ref_ids else "{}"
        fact_arr = "{" + ",".join(fact_ids) + "}" if fact_ids else "{}"

        # 清理引用这些 Claim 的下游表（顺序：外键从外向内）
        if cids:
            deleted["memories"] = ex(
                "DELETE FROM memory.memory_items "
                "WHERE source_claim_ids && CAST(:ids AS uuid[])", {"ids": cid_arr}
            ).rowcount
            deleted["proposals"] = ex(
                "DELETE FROM cognition.profile_proposals "
                "WHERE status = 'pending' "
                "AND evidence_claim_ids && CAST(:ids AS uuid[])", {"ids": cid_arr}
            ).rowcount
            ex(f"DELETE FROM ontology.relations WHERE evidence_claim_id IN ({in_clause})", binds)
            # superseded_by 是无 FK 的软指针：先解引用，避免删行后留下悬空版本号
            if fact_ids:
                deleted["dangling_refs_cleared"] = ex(
                    "UPDATE knowledge.facts SET superseded_by = NULL "
                    "WHERE superseded_by = ANY(CAST(:ids AS uuid[]))",
                    {"ids": fact_arr},
                ).rowcount
            ex(f"DELETE FROM knowledge.facts WHERE claim_id IN ({in_clause})", binds)
            deleted["facts"] = len(fact_ids)
            ex(f"DELETE FROM knowledge.claim_evidence WHERE claim_id IN ({in_clause})", binds)
            deleted["claims"] = ex(
                f"DELETE FROM knowledge.claims WHERE id IN ({in_clause})", binds
            ).rowcount
        if ref_ids:
            affected = [str(r[0]) for r in ex(
                "SELECT DISTINCT decision_id FROM decision.decision_evidence "
                "WHERE reference_id = ANY(CAST(:ids AS uuid[]))", {"ids": ref_arr}
            )]
            deleted["decision_evidence"] = ex(
                "DELETE FROM decision.decision_evidence "
                "WHERE reference_id = ANY(CAST(:ids AS uuid[]))", {"ids": ref_arr}
            ).rowcount
            if affected:
                # 决策记录与其不可变快照保留（审计完整性），仅标记证据链失效
                ex(
                    "UPDATE decision.decision_records AS d SET status = 'evidence_invalidated' "
                    "WHERE d.id = ANY(CAST(:ids AS uuid[])) AND d.status <> 'executed' "
                    "AND NOT EXISTS (SELECT 1 FROM decision.decision_evidence e "
                    "                WHERE e.decision_id = d.id)",
                    {"ids": "{" + ",".join(affected) + "}"},
                )
        if cascade:
            deleted["evidences"] = ex(
                "DELETE FROM knowledge.evidence_spans WHERE document_id = :did", {"did": did}
            ).rowcount
            deleted["chunks"] = ex(
                "DELETE FROM knowledge.semantic_chunks WHERE document_id = :did", {"did": did}
            ).rowcount
        # 删除任务与步骤（cascade 时删该文档的全部任务）
        if cascade:
            ex("DELETE FROM ingest.pipeline_steps WHERE run_id IN "
               "(SELECT id FROM ingest.pipeline_runs WHERE document_id = :did)", {"did": did})
            deleted["run"] = ex(
                "DELETE FROM ingest.pipeline_runs WHERE document_id = :did", {"did": did}
            ).rowcount
            deleted["document"] = ex(
                "DELETE FROM source.documents WHERE id = :did", {"did": did}
            ).rowcount
        else:
            ex("DELETE FROM ingest.pipeline_steps WHERE run_id = :id", {"id": run_id})
            deleted["run"] = ex(
                "DELETE FROM ingest.pipeline_runs WHERE id = :id", {"id": run_id}
            ).rowcount
    # 任务删除会改变 DWS 列表的蒸馏状态索引，主动失效其 TTL 缓存
    try:
        from app.modules.dws_source import distill_status as _ds

        _ds.invalidate_index()
    except Exception:  # noqa: BLE001
        pass
    return {"deleted": deleted, "cascade": cascade}


@router.get("/documents/dws/list")
def dws_list(query: str = " ", limit: int = 30, cursor: str | None = None,
             all_pages: bool = Query(False, alias="all"), max_pages: int = 20):
    """列出/搜索我的钉钉文档（DWS CLI）。

    - 默认单页：返回 has_more / next_cursor，配合 cursor 参数翻页
    - all=true：自动顺序翻页抓取有权限的全部文档并去重（max_pages 为安全上限）
    """
    from app.modules.dws_source import client as dws

    try:
        return dws.list_documents(query, limit, cursor, all_pages, max_pages)
    except dws.DwsError as e:
        raise HTTPException(502, str(e))


@router.get("/documents/dws/streams")
def dws_streams(kind: str = "document", query: str = "", days: int = 7,
                limit: int = 30, all_pages: bool = Query(True, alias="all"),
                refresh: bool = False):
    """统一列出四类钉钉数据源：文档 / 会话消息 / 日历日程 / 会议听记。

    默认 120 秒 TTL 缓存；refresh=true 强制重新抓取（会调用 DWS CLI）。
    """
    from app.modules.dws_source import streams

    try:
        return streams.list_items(kind, query, days, limit, all_pages, refresh)
    except streams.DwsError as e:
        raise HTTPException(502, str(e))


@router.post("/documents/dws/distill-status")
def dws_distill_status(body: dict):
    """按 DWS 列表条目批量返回蒸馏状态（completed / active / none）。

    轻量只读接口：不调用 dws CLI，只比对本地库中的文档与流水线任务，
    供前端将已完成蒸馏的「蒸馏」按钮替换为置灰的「已蒸馏」。
    """
    from app.modules.dws_source import distill_status

    body = body or {}
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "items must be a list")
    if len(items) > 1000:
        raise HTTPException(400, "too many items (max 1000)")
    return distill_status.states_for(str(body.get("kind") or "document"), items)


@router.post("/documents/dws/stream-import")
def dws_stream_import(body: dict):
    """把任意一类数据源的条目读成文本，进入统一蒸馏流水线。"""
    from datetime import datetime, timezone

    from app.modules.dws_source import streams

    body = body or {}
    kind = body.get("kind") or "document"
    item = body.get("item") or {}
    if not item.get("node_id"):
        raise HTTPException(400, "item.node_id is required")
    if kind not in streams.KINDS:
        raise HTTPException(400, f"unsupported kind: {kind}")
    try:
        parsed = streams.fetch_text(kind, item)
    except streams.DwsError as e:
        raise HTTPException(502, str(e))
    meta = {
        "source_kind": kind,
        "source_label": streams.KINDS[kind],
        "external_ref": str(item.get("node_id")),
        "origin_url": item.get("doc_url") or "",
        "dws_engine": parsed.get("engine"),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    result = _enqueue(item.get("name") or streams.KINDS[kind], parsed["text"],
                      f"dws_{kind}", f"DWS · {streams.KINDS[kind]}",
                      meta, parsed.get("engine"))
    # 新建/重置蒸馏任务会改变 DWS 列表状态索引，主动失效其 TTL 缓存
    if not result.get("deduplicated"):
        try:
            from app.modules.dws_source import distill_status as _ds

            _ds.invalidate_index()
        except Exception:  # noqa: BLE001
            pass
    return result


@router.post("/documents/dws/import")
def dws_import(body: dict):
    """兼容旧入口：导入钉钉文档并蒸馏（新界面统一走 stream-import）。"""
    from app.modules.dws_source import client as dws

    node_id = (body or {}).get("node_id")
    name = (body or {}).get("name") or "钉钉文档"
    extension = (body or {}).get("extension") or "adoc"
    if not node_id:
        raise HTTPException(400, "node_id is required")
    try:
        parsed = dws.fetch_document_text(node_id, extension, name)
    except dws.DwsError as e:
        raise HTTPException(502, str(e))
    meta = {"source_kind": "document", "source_label": "钉钉文档",
            "external_ref": str(node_id),
            "origin_url": (body or {}).get("doc_url") or "",
            "dws_engine": parsed.get("engine")}
    return _enqueue(name, parsed["text"], "dws_document", "DWS · 钉钉文档",
                    meta, parsed.get("engine"))


@router.post("/documents/upload")
def upload_document(file: UploadFile = File(...), source_id: str | None = None):
    """上传文件并蒸馏：文本直读，PDF/Office 走解析适配层。"""
    from app.modules.document_parser import adapters

    cap = max(1024, int(settings.max_upload_bytes))
    raw_bytes = file.file.read(cap + 1)
    if len(raw_bytes) > cap:
        raise HTTPException(413, f"文件超过上传上限 {cap // (1024 * 1024)} MiB，请先拆分再上传")
    filename = file.filename or "untitled"
    try:
        parsed = adapters.parse_bytes(raw_bytes, filename)
    except Exception as e:  # noqa: BLE001 - 解析层任何异常都返回可读错误，避免裸 500
        raise HTTPException(422, f"文件解析失败：{type(e).__name__}: {e}")
    raw = parsed["text"]
    if not raw.strip():
        raise HTTPException(422, f"文件无法解析为文本：{parsed.get('warning') or '空内容'}")
    return _enqueue(filename, raw, "upload", "本地上传",
                    {"source_kind": "upload", "source_label": "本地上传",
                     "mime_type": file.content_type or "text/plain",
                     "dws_engine": parsed.get("engine")}, parsed.get("engine"))


@router.get("/documents/parsers")
def parser_status():
    """返回文档解析引擎可用性（含 MinerU / opendataloader-pdf / docling）。"""
    from app.modules.document_parser import adapters

    return adapters.available_engines()


@router.get("/documents/{document_id}")
def get_document(document_id: str):
    row = db.fetch_one(
        "SELECT id, source_id, title, mime_type, document_status, created_at, metadata "
        "FROM source.documents WHERE id = :id",
        {"id": document_id},
    )
    if not row:
        raise HTTPException(404, "document not found")
    row["id"] = str(row["id"])
    return row


@router.post("/documents/{document_id}/reprocess")
def reprocess(document_id: str):
    from app.config import settings as cfg
    from app.modules.llm.client import stage_idempotency_key
    from app.modules.ingestion import storage as st
    from app.workers.pipeline_tasks import reprocess_document

    if os.environ.get("CELERY_TASK_ALWAYS_EAGER") == "1":
        doc = db.fetch_one(
            "SELECT id, content_hash FROM source.documents WHERE id = :id",
            {"id": document_id},
        )
        if not doc:
            raise HTTPException(404, "document not found")
        key = stage_idempotency_key(
            str(doc["id"]), doc["content_hash"], cfg.llm_model, "distill"
        )
        from app.workers.pipeline_tasks import reclaim_run

        row = db.fetch_one(
            "SELECT id, status FROM ingest.pipeline_runs WHERE idempotency_key = :k",
            {"k": key},
        )
        if row:
            run_id = str(row["id"])
            # 与 _enqueue / 复核台共用同一状态机：活动态不得并发双蒸，
            # 待复核 / 已关闭 / 失败态先重置再派发。
            if reclaim_run(run_id, row["status"]) != "reset":
                return {"status": "already_running", "run_id": run_id}
        else:
            run_id = str(db.insert_returning_id(
                "ingest.pipeline_runs",
                {
                    "document_id": str(doc["id"]),
                    "pipeline_version": cfg.pipeline_version,
                    "idempotency_key": key,
                    "status": "PENDING",
                },
            ))
        _dispatch_distillation(run_id)
        return {"status": "queued", "run_id": run_id}
    result = reprocess_document.delay(document_id)
    return {"status": "queued", "task_id": result.id}


@router.get("/documents/{document_id}/runs")
def get_runs(document_id: str):
    return db.fetch_all(
        "SELECT id, status, retry_count, created_at, updated_at "
        "FROM ingest.pipeline_runs WHERE document_id = :id ORDER BY created_at DESC",
        {"id": document_id},
    )
