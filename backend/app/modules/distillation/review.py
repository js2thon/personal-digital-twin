"""蒸馏任务 NEEDS_REVIEW（待人工复核）的完整处置闭环。

设计要点（回答「谁操作 / 在哪操作 / 怎么操作 / 怎么验证」）：

* 谁：只有 owner（本机回环或带有效 API Key 的本人）能处置，身份由
  `auth.current_actor` 从服务端注入，绝不接受请求体自报 reviewer。
* 在哪：控制台「上传文档 → 蒸馏任务状态」的「复核」弹窗；对应 API
  `GET/POST /api/v1/documents/runs/{run_id}/review`。
* 怎么操作：按 reason 给出建议动作；`retry` 重跑流水线，`dismiss` 由本人
  判定「该文档无需蒸馏」并置终态 REVIEW_DISMISSED（不删任何数据）。
* 怎么验证：retry 前做 LLM 端点预检；流水线每次结束自动回填该次复核的
  verification=recovered / still_failing，复核弹窗展示历史与前后对照。

留痕表 `ingest.pipeline_reviews` 故意不建到 pipeline_runs 的外键：
级联删除任务时应保留人工处置记录，审计链不能因删任务而断。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db import session as db

REVIEW_STATUSES = {"NEEDS_REVIEW", "REVIEW_DISMISSED"}

# 原因码 → 面向本人的处置说明。retry_effective=False 表示「重蒸大概率还是
# 同样结果」，UI 会把它排在后面并提示先修源头，避免用户陷入无限重蒸。
REASON_META: dict[str, dict] = {
    "no_chunks": {
        "label": "原文无有效切片",
        "summary": "文档解析后没有可蒸馏的文本（空文件、扫描件未 OCR、或内容被清洗为空）。",
        "owner": "本人（owner）",
        "where": "上传文档 → 蒸馏任务状态 → 复核",
        "steps": [
            "打开原文确认是否为空 / 图片型 PDF / 只读表格",
            "换用可解析的版本重新上传（或先 OCR）",
            "在复核台选择「判定无需处理」关闭本条，或重传后重新蒸馏",
        ],
        "verify": "重传后切片数 > 0，且任务不再回到待复核",
        "retry_effective": False,
        "suggested_action": "dismiss",
    },
    "no_claims": {
        "label": "模型未抽出主张",
        "summary": "切片正常、模型也正常返回，但输出空数组（文档多为通讯录、流程图、模板等无「本人」知识的内容）。",
        "owner": "本人（owner）",
        "where": "上传文档 → 蒸馏任务状态 → 复核",
        "steps": [
            "查看下方切片数与知识产物，确认文档确实不含个人知识",
            "确认无需蒸馏 → 「判定无需处理」；认为应能抽出 → 换模型或调 Prompt 后「重新蒸馏」",
        ],
        "verify": "重蒸后 published > 0 且 verification=recovered；否则复核记录标记 still_failing",
        "retry_effective": True,
        "suggested_action": "dismiss",
    },
    "invalid_json": {
        "label": "模型输出非 JSON",
        "summary": "模型返回了解释文字或 Markdown，无法解析为 Claim 数组；不是文档内容问题。",
        "owner": "本人（owner）",
        "where": "上传文档 → 蒸馏任务状态 → 复核 → 诊断明细",
        "steps": [
            "查看诊断明细里的模型输出片段，确认是否带解释文字 / 代码块",
            "调整 EXTRACT_SYSTEM 或更换对指令遵循更好的模型",
            "选择「重新蒸馏」；预检会先探测端点连通性",
        ],
        "verify": "重蒸后不再出现 invalid_json，且 published > 0",
        "retry_effective": True,
        "suggested_action": "retry",
    },
    "llm_unavailable": {
        "label": "LLM 端点不可用",
        "summary": "所有切片抽取都因网络错误、超时或网关 5xx 失败，原文与切片本身完好。",
        "owner": "本人（owner）",
        "where": "上传文档 → 蒸馏任务状态 → 复核 → 诊断明细",
        "steps": [
            "查看诊断明细里的 HTTP 状态 / 错误信息，确认 LLM_BASE_URL 与网关状态",
            "端点恢复后选择「重新蒸馏」；预检不通时 UI 会明确告警",
        ],
        "verify": "重蒸前预检 ok=true；重蒸后 published > 0 且 verification=recovered",
        "retry_effective": True,
        "suggested_action": "retry",
    },
    "partial_extract": {
        "label": "部分切片抽取失败",
        "summary": "部分切片失败但其余切片成功产出主张；任务仍标记 COMPLETED，此处仅作警告留痕。",
        "owner": "本人（owner）",
        "where": "链路追踪 → 蒸馏步骤日志",
        "steps": ["查看失败切片数与错误摘要", "必要时对同一文档重新蒸馏补齐"],
        "verify": "重蒸后 failed_chunks=0",
        "retry_effective": True,
        "suggested_action": "retry",
    },
    "unknown": {
        "label": "未分类原因",
        "summary": "error_info 中缺少可识别的 reason 字段，需查看原始诊断信息。",
        "owner": "本人（owner）",
        "where": "上传文档 → 蒸馏任务状态 → 复核 → 诊断明细",
        "steps": ["查看原始 error_info", "根据信息选择重新蒸馏或判定无需处理"],
        "verify": "处置后状态不再是 NEEDS_REVIEW",
        "retry_effective": True,
        "suggested_action": "retry",
    },
}


def classify(error_info) -> dict:
    """纯函数：把 error_info 归一成结构化诊断（可直接单测）。"""
    info = error_info if isinstance(error_info, dict) else {}
    code = str(info.get("reason") or "").strip().lower()
    if not code:
        # 重新蒸馏会清空 error_info；原始诊断随复核动作写入 last_review，
        # 这里回退读取，保证「当初为什么待复核」在处置后仍可追溯。
        last = info.get("last_review") if isinstance(info.get("last_review"), dict) else {}
        code = str(last.get("original_reason") or last.get("dismissed_reason")
                   or "").strip().lower()
    if code not in REASON_META:
        code = "unknown"
    meta = REASON_META[code]
    return {
        "code": code,
        "label": meta["label"],
        "summary": meta["summary"],
        "owner": meta["owner"],
        "where": meta["where"],
        "steps": list(meta["steps"]),
        "verify": meta["verify"],
        "retry_effective": meta["retry_effective"],
        "suggested_action": meta["suggested_action"],
        "detail": info,
    }


def extraction_reason(chunk_count: int, claim_count: int,
                      failure_kinds: set[str]) -> str:
    """纯函数：由「切片数 / 主张数 / 失败类型」决定流水线终态原因码。

    返回空串表示本次抽取有可用产出，任务应继续发布（历史 bug 是只要有一个
    切片抛异常就把整批成功结果全部丢弃，这里显式把「部分失败」排除在
    NEEDS_REVIEW 之外）。端点故障优先于输出格式问题，便于先修服务。
    """
    if not chunk_count:
        return "no_chunks"
    if claim_count:
        return ""
    if "llm_unavailable" in failure_kinds:
        return "llm_unavailable"
    if "invalid_json" in failure_kinds:
        return "invalid_json"
    return "no_claims"


def verification_result(final_status: str, outcome: dict | None) -> str:
    """纯函数：重蒸是否真的修复。COMPLETED 且产出主张 > 0 才算 recovered。"""
    published = int((outcome or {}).get("published") or 0)
    return ("recovered" if str(final_status).upper() == "COMPLETED" and published > 0
            else "still_failing")


def ensure_schema() -> None:
    """启动时幂等建复核留痕表（不改既有表结构）。"""
    db.execute(
        "CREATE TABLE IF NOT EXISTS ingest.pipeline_reviews ("
        " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        " run_id UUID NOT NULL,"
        " action VARCHAR(20) NOT NULL,"
        " reason VARCHAR(50),"
        " note TEXT DEFAULT '',"
        " actor VARCHAR(50) NOT NULL,"
        " before_status VARCHAR(30) NOT NULL,"
        " after_status VARCHAR(30) NOT NULL,"
        " verification VARCHAR(20) NOT NULL DEFAULT 'not_applicable',"
        " verified_at TIMESTAMPTZ,"
        " verified_detail JSONB DEFAULT '{}',"
        " created_at TIMESTAMPTZ DEFAULT NOW())"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_reviews_run "
        "ON ingest.pipeline_reviews (run_id, created_at DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pipeline_reviews_pending "
        "ON ingest.pipeline_reviews (run_id) WHERE verification = 'pending'"
    )


def _json(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def record_review(run_id: str, *, action: str, reason: str, note: str,
                  actor: str, before_status: str, after_status: str,
                  preflight: dict | None = None) -> str:
    """写一条不可变的人工处置记录；retry 的 verification 置 pending 等待回填。"""
    verification = "pending" if action == "retry" else "not_applicable"
    detail = {"preflight": preflight or {}}
    return str(db.insert_returning_id(
        "ingest.pipeline_reviews",
        {
            "run_id": run_id, "action": action, "reason": reason,
            "note": note, "actor": actor,
            "before_status": before_status, "after_status": after_status,
            "verification": verification,
            "verified_detail": _json(detail),
        },
    ))


def verify_retry(run_id: str, final_status: str, outcome: dict | None = None) -> dict | None:
    """流水线结束时回填最近一次「重新蒸馏」的验证结论，形成处理前后对照。

    recovered 的判定必须是真的产出了知识：COMPLETED 且 published > 0。
    只是「不再报错」不算修好 —— 否则 no_claims 会被误判为已恢复。
    """
    row = db.fetch_one(
        "SELECT id, reason FROM ingest.pipeline_reviews "
        "WHERE run_id = CAST(:r AS uuid) AND action = 'retry' "
        "AND verification = 'pending' ORDER BY created_at DESC LIMIT 1",
        {"r": run_id},
    )
    if not row:
        return None
    outcome = outcome or {}
    published = int(outcome.get("published") or 0)
    result = {
        "verification": verification_result(final_status, outcome),
        "final_status": final_status,
        "published": published,
        "chunks": int(outcome.get("chunks") or 0),
        "reason": outcome.get("reason") or row.get("reason") or "",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    db.execute(
        "UPDATE ingest.pipeline_reviews SET verification = :v, "
        "verified_at = NOW(), verified_detail = CAST(:d AS jsonb) "
        "WHERE id = CAST(:i AS uuid)",
        {"v": result["verification"], "d": _json(result), "i": str(row["id"])},
    )
    return result


def review_history(run_id: str, limit: int = 20) -> list[dict]:
    rows = db.fetch_all(
        "SELECT id, action, reason, note, actor, before_status, after_status, "
        "verification, verified_at, verified_detail, created_at "
        "FROM ingest.pipeline_reviews WHERE run_id = CAST(:r AS uuid) "
        "ORDER BY created_at DESC LIMIT :l",
        {"r": run_id, "l": max(1, min(int(limit or 20), 100))},
    )
    for r in rows:
        for k in ("id",):
            r[k] = str(r[k])
        for k in ("verified_at", "created_at"):
            r[k] = str(r[k] or "")[:19]
        if not isinstance(r.get("verified_detail"), dict):
            r["verified_detail"] = {}
    return rows


def llm_preflight() -> dict:
    """重蒸前探测 LLM 端点：只做最小 JSON 调用，不写库、不改配置。

    结果仅作告警与留痕，不阻断本人操作（no_chunks 等场景根本不需要 LLM）。
    """
    try:
        from app.modules.llm.client import llm_client

        out = llm_client.chat_json(
            "只输出一个合法 JSON 数组：[]", "输出 []", strict=True
        )
        if not isinstance(out, (list, dict)):
            return {"ok": False, "error": "模型返回类型异常"}
        return {"ok": True, "model": "configured"}
    except Exception as exc:  # noqa: BLE001  预检失败也必须让本人看到原因
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:180]}"}
