"""知识蒸馏流水线：可恢复、可重试、可追踪的异步任务状态机。

PENDING → INGESTING → PARSING → CHUNKING → EXTRACTING → VALIDATING
        → ENTITY_RESOLUTION → KNOWLEDGE_REVIEW → PUBLISHING → COMPLETED
异常分支：RETRY_WAIT / FAILED / CANCELLED / NEEDS_REVIEW
"""
import json

from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings
from app.db import session as db
from app.modules.document_parser import parser as doc_parser
from app.modules.distillation import service as distill
from app.modules.distillation.review import extraction_reason
from app.modules.llm.client import LLMJsonError, llm_client, stage_idempotency_key
from app.workers.celery_app import celery_app

RETRYABLE_STATES = {"INGESTING", "PARSING", "CHUNKING", "EXTRACTING", "VALIDATING"}

# 全部状态分类：活动态（含排队/重试等待）不允许重置，防止并发双蒸；
# 只有终态异常 / 人工复核结论 / 完成 / 取消才允许重置为 PENDING 重新执行。
ACTIVE_STATUSES = {
    "PENDING", "INGESTING", "PARSING", "CHUNKING", "EXTRACTING", "VALIDATING",
    "ENTITY_RESOLUTION", "KNOWLEDGE_REVIEW", "PUBLISHING", "RETRY_WAIT",
}
# REVIEW_DISMISSED 是本人「判定无需处理」的终态，但允许反悔：
# 之后从复核台或 DWS 列表仍可重新发起蒸馏，不把人工结论变成永久锁死。
RESETTABLE_STATUSES = {"FAILED", "NEEDS_REVIEW", "REVIEW_DISMISSED",
                       "COMPLETED", "CANCELLED"}


def reclaim_run(run_id: str, status: str) -> str:
    """复用已有 run：可重置终态 → 重置为 PENDING 并返回 "reset"；
    活动态 → 原样返回 "already_running"；其余防御性返回 "already_running"。"""
    s = str(status or "").upper()
    if s in RESETTABLE_STATUSES:
        db.execute(
            "UPDATE ingest.pipeline_runs SET status = 'PENDING', retry_count = 0, "
            "error_info = CAST('{}' AS jsonb), updated_at = NOW() WHERE id = :id",
            {"id": run_id},
        )
        return "reset"
    return "already_running"


def _set_run_status(run_id: str, status: str, error: dict | None = None) -> None:
    db.execute(
        "UPDATE ingest.pipeline_runs SET status = :s, error_info = CAST(:e AS jsonb), "
        "updated_at = NOW() WHERE id = :id",
        {"s": status, "e": json.dumps(error or {}, ensure_ascii=False, default=str),
         "id": run_id},
    )


def _verify_review(run_id: str, status: str, outcome: dict | None = None) -> None:
    """把本次运行结果回填到最近一条「重新蒸馏」复核记录，异常不影响主流程。"""
    try:
        from app.modules.distillation.review import verify_retry

        verify_retry(run_id, status, outcome)
    except Exception:  # noqa: BLE001  验证留痕失败不能反过来弄挂蒸馏
        pass


def _log_step(run_id: str, step: str, model_version: str, error: str | None = None) -> None:
    db.execute(
        "INSERT INTO ingest.pipeline_steps "
        "(run_id, step_name, model_version, prompt_version, error_info) "
        "VALUES (:r, :s, :m, :p, :e)",
        {"r": run_id, "s": step, "m": model_version,
         "p": settings.prompt_version, "e": error},
    )


@celery_app.task(bind=True, max_retries=3, soft_time_limit=600)
def run_distillation(self, run_id: str):
    run = db.fetch_one(
        "SELECT r.id, r.retry_count, d.id AS document_id, d.content_hash, d.object_uri, "
        "d.mime_type, d.title FROM ingest.pipeline_runs r "
        "JOIN source.documents d ON d.id = r.document_id WHERE r.id = :id",
        {"id": run_id},
    )
    if not run:
        return {"status": "missing_run"}
    doc_id = run["document_id"]

    try:
        # 1. INGESTING：从 MinIO 拉取原文
        _set_run_status(run_id, "INGESTING")
        raw = _load_document_text(doc_id)
        _log_step(run_id, "INGESTING", settings.llm_model)

        # 2. PARSING + 3. CHUNKING
        _set_run_status(run_id, "PARSING")
        text = doc_parser.parse_plain(raw)
        _log_step(run_id, "PARSING", "plain")

        _set_run_status(run_id, "CHUNKING")
        chunks = doc_parser.chunk_text(text)
        _log_step(run_id, "CHUNKING", "sliding_window", None if chunks else "empty_chunks")
        if not chunks:
            _set_run_status(run_id, "NEEDS_REVIEW", {
                "reason": "no_chunks", "chunks": 0,
                "summary": "原文解析后无有效文本，重蒸不会改变结果，需检查源文件",
            })
            _verify_review(run_id, "NEEDS_REVIEW", {"chunks": 0, "published": 0,
                                                    "reason": "no_chunks"})
            return {"status": "needs_review", "reason": "no_chunks", "chunks": 0}
        # 切片落库：此前 CHUNKING 只生成内存列表，semantic_chunks 恒空，
        # 导致文档检索分支永远无结果、问答/决策拿不到原文证据。
        _persist_chunks(run_id, doc_id, chunks)

        # 4. EXTRACTING + 5. VALIDATING
        _set_run_status(run_id, "EXTRACTING")
        claims: list[dict] = []
        failures: list[dict] = []
        # 按切片统计：过去只要有一个切片抛异常就把整批成功结果全部丢弃，
        # 25 个切片里 1 个超时也会把 24 个切片的主张一起报废。
        for idx, chunk in enumerate(chunks):
            try:
                claims.extend(distill.extract_claims(chunk))
            except LLMJsonError as exc:
                failures.append({"chunk": idx, "kind": "invalid_json",
                                 "detail": exc.raw[:160]})
            except Exception as exc:  # noqa: BLE001  端点不可用 / 超时 / 5xx
                failures.append({"chunk": idx, "kind": "llm_unavailable",
                                 "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
        diagnostic = {
            "chunks": len(chunks),
            "success_chunks": len(chunks) - len(failures),
            "failed_chunks": len(failures),
            "model": settings.llm_model,
        }
        reason = extraction_reason(len(chunks), len(claims),
                                   {f["kind"] for f in failures})
        _log_step(run_id, "EXTRACTING", settings.llm_model,
                  None if claims else
                  (reason if reason else "no_claims"))
        if reason:
            info = {**diagnostic, "reason": reason,
                    "sample_error": failures[0]["detail"] if failures else ""}
            _set_run_status(run_id, "NEEDS_REVIEW", info)
            _verify_review(run_id, "NEEDS_REVIEW",
                           {**diagnostic, "published": 0, "reason": reason})
            return {"status": "needs_review", "reason": reason, **diagnostic}

        # 部分切片失败但仍有产出：不制造新的待复核任务，只留 warning 与步骤日志
        warnings = {}
        if failures:
            warnings = {**diagnostic, "warning": "partial_extract",
                        "sample_error": failures[0]["detail"]}
            _log_step(run_id, "EXTRACTING_PARTIAL", settings.llm_model,
                      f"{len(failures)}/{len(chunks)}_chunks_failed")

        _set_run_status(run_id, "VALIDATING")
        valid = [c for c in claims if c.get("claim_type") and c.get("subject")]
        dropped = len(claims) - len(valid)
        _log_step(run_id, "VALIDATING", "schema", None)

        # 6. ENTITY_RESOLUTION
        _set_run_status(run_id, "ENTITY_RESOLUTION")
        subject_cache: dict[str, str] = {}
        for claim in valid:
            key = json.dumps(claim.get("subject") or {}, ensure_ascii=False)
            if key not in subject_cache:
                subject_cache[key] = (distill.resolve_entity(claim["subject"]) or {}).get(
                    "match_id"
                )
        _log_step(run_id, "ENTITY_RESOLUTION", settings.llm_model)

        # 7. KNOWLEDGE_REVIEW：低于阈值的 Claim 落库标记优先复核，审核队列前排
        _set_run_status(run_id, "KNOWLEDGE_REVIEW")
        threshold = float(settings.claim_review_threshold)
        def is_low(c) -> bool:
            try:
                return float(c.get("confidence", 0)) < threshold
            except (TypeError, ValueError):
                return True
        needs_review = [c for c in valid if is_low(c)]
        _log_step(run_id, "KNOWLEDGE_REVIEW",
                  f"threshold_{threshold:g}:{len(needs_review)}_flagged")

        # 8. PUBLISHING：写入 Claim + Evidence（candidate，等待人工 approve → Fact）
        _set_run_status(run_id, "PUBLISHING")
        published = 0
        # 所有 Claim/Evidence/关系在一个事务内写入：中途异常不会留下半截知识
        with db.transaction():
            for claim in valid:
                subject_key = json.dumps(claim.get("subject") or {}, ensure_ascii=False)
                distill.publish_claim(claim, str(doc_id),
                                      subject_cache.get(subject_key), is_low(claim))
                published += 1
        _log_step(run_id, "PUBLISHING", settings.llm_model)
        db.execute(
            "UPDATE source.documents SET document_status = 'parsed' WHERE id = :id",
            {"id": str(doc_id)},
        )

        _set_run_status(run_id, "COMPLETED", warnings)
        outcome = {
            "status": "completed",
            "chunks": len(chunks),
            "published": published,
            "dropped_invalid": dropped,
            "low_confidence": len(needs_review),
            "failed_chunks": len(failures),
        }
        _verify_review(run_id, "COMPLETED", outcome)
        return outcome

    except SoftTimeLimitExceeded:
        retries = run.get("retry_count") or 0
        if retries < 3:
            db.execute(
                "UPDATE ingest.pipeline_runs SET retry_count = retry_count + 1 "
                "WHERE id = :id",
                {"id": run_id},
            )
            _set_run_status(run_id, "RETRY_WAIT", {"reason": "timeout"})
            raise self.retry(countdown=60)
        # 超限不再 retry，否则以未捕获的 MaxRetriesExceededError 结束卡在 RETRY_WAIT
        _set_run_status(run_id, "FAILED", {"error": "soft time limit exceeded, retries exhausted"})
        _log_step(run_id, "FAILED", settings.llm_model, "timeout_retries_exhausted")
        _verify_review(run_id, "FAILED", {"reason": "timeout", "published": 0})
        return {"status": "failed", "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        retries = run.get("retry_count") or 0
        if retries < 3:
            db.execute(
                "UPDATE ingest.pipeline_runs SET retry_count = retry_count + 1 "
                "WHERE id = :id",
                {"id": run_id},
            )
            _set_run_status(run_id, "RETRY_WAIT", {"error": str(exc)})
            raise self.retry(countdown=30 * (retries + 1))
        _set_run_status(run_id, "FAILED", {"error": str(exc)})
        _log_step(run_id, "FAILED", settings.llm_model, str(exc))
        _verify_review(run_id, "FAILED", {"reason": str(exc)[:160], "published": 0})
        return {"status": "failed", "error": str(exc)}


def _persist_chunks(run_id: str, doc_id, chunks: list[dict]) -> int:
    """把切片写入 knowledge.semantic_chunks，供混合检索召回原文。

    统一走 embeddings.embed_chunks（向量本体 P0）：本体标注、向量、
    model/version/embedded_at 与 rechunk 共用同一套写入逻辑，杜绝第二套 INSERT。
    重复蒸馏先清理旧切片，保证幂等。
    """
    from app.modules.embeddings import service as embeddings

    return embeddings.embed_chunks(doc_id, chunks, run_id=str(run_id))


def _load_document_text(document_id: str) -> str:
    """从 MinIO 读取原文（文本类直接读，其他类型经 RAGFlow 解析）。"""
    from app.modules.ingestion.storage import get_document_text

    return get_document_text(document_id)


@celery_app.task
def reprocess_document(document_id: str):
    """重新蒸馏：版本升级时按新幂等键生成新版本，不覆盖旧版本。
    同版本已存在 run 时复用（不重复插入）；仅终态（FAILED/NEEDS_REVIEW/COMPLETED/
    CANCELLED）才重置为 PENDING 重新执行，执行中的任务返回 already_running 防并发双蒸。"""
    doc = db.fetch_one(
        "SELECT id, content_hash FROM source.documents WHERE id = :id",
        {"id": document_id},
    )
    if not doc:
        return {"status": "missing_document"}
    key = stage_idempotency_key(
        str(doc["id"]), doc["content_hash"], settings.llm_model, "distill"
    )
    existing = db.fetch_one(
        "SELECT id, status FROM ingest.pipeline_runs WHERE idempotency_key = :k",
        {"k": key},
    )
    if existing:
        run_id = str(existing["id"])
        action = reclaim_run(run_id, existing["status"])
        if action != "reset":
            return {"status": "already_running", "run_id": run_id}
    else:
        run_id = str(db.insert_returning_id(
            "ingest.pipeline_runs",
            {
                "document_id": str(doc["id"]),
                "pipeline_version": settings.pipeline_version,
                "idempotency_key": key,
                "status": "PENDING",
            },
        ))
    run_distillation.delay(run_id)
    return {"status": "queued", "run_id": run_id}
