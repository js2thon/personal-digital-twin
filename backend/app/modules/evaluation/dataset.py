"""固定基准集（golden-v1）加载与有效性校验（SIL P0-4 / GV-02）。

基准集是一次性从实库只读抽样后**固化在代码仓里的 JSON**，不是每次运行时
动态生成 —— 否则检索指标会随数据漂移而失去「版本对照」的意义。

数据变更后不做伪造回填，而是：
  - 校验每个 relevant_id 是否仍为 active；
  - 失效用例标记 stale，从指标分母中剔除，并在报告里单独列出；
  - 需要重建基准集时由维护者显式重新生成并升 dataset_version。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

__all__ = ["DATASET_PATH", "load_dataset", "dataset_summary", "dataset_fingerprint",
           "check_validity", "known_gaps"]

DATASET_PATH = Path(__file__).with_name("golden_v1.json")
TEMPLATE_PATH = Path(__file__).with_name("golden_v1.example.json")
_CACHE: dict | None = None


def active_path() -> Path:
    """真实基准集优先，缺失时回退到脱敏模板。

    `golden_v1.json` 含实库主体名与 fact id，属运行时数据，不入代码仓；
    由 `backend/scripts/build_golden.py` 从实库生成。仓库内只保留
    结构完全一致的 `golden_v1.example.json`，保证 clone 后可直接跑测试。
    """
    return DATASET_PATH if DATASET_PATH.exists() else TEMPLATE_PATH


def load_dataset() -> dict:
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(active_path().read_text(encoding="utf-8"))
    return dict(_CACHE)


def dataset_fingerprint() -> str:
    return hashlib.sha256(active_path().read_bytes()).hexdigest()[:16]


def dataset_summary() -> dict:
    d = load_dataset()
    cases = d.get("cases") or []
    return {
        "dataset_version": d.get("dataset_version"),
        "created_at": d.get("created_at"),
        "source": d.get("source"),
        "fingerprint": dataset_fingerprint(),
        "snapshot": d.get("snapshot"),
        "k_default": d.get("k_default"),
        "case_count": len(cases),
        "negative_case_count": len(d.get("negative_cases") or []),
        "decision_case_count": len(d.get("decision_cases") or []),
        "known_gaps": d.get("known_gaps") or [],
        "cases": [
            {"case_id": c["case_id"], "query": c["query"],
             "top_k": c.get("top_k", d.get("k_default", 10)),
             "filters": c.get("filters") or {},
             "expect_subject": c.get("expect_subject"),
             "relevant_count": len(c.get("relevant_ids") or []),
             "relevant_definition": c.get("relevant_definition"),
             "note": c.get("note")}
            for c in cases
        ],
    }


def known_gaps() -> list[dict]:
    return list(load_dataset().get("known_gaps") or [])


def check_validity() -> dict:
    """对实库校验基准集锚点是否仍有效；DB 不可用时整体标记 unverifiable。

    只读，不修改任何数据。返回 {case_id: {total, active, stale_ids, status}}。
    """
    d = load_dataset()
    all_ids: list[str] = []
    for c in d.get("cases") or []:
        all_ids.extend(c.get("relevant_ids") or [])
    if not all_ids:
        return {"status": "empty", "cases": {}}
    try:
        from app.db import session as db

        placeholders = ", ".join(f"CAST(:i{i} AS uuid)" for i in range(len(set(all_ids))))
        params = {f"i{i}": v for i, v in enumerate(sorted(set(all_ids)))}
        rows = db.fetch_all(
            f"SELECT id::text AS id FROM knowledge.facts "
            f"WHERE status = 'active' AND id IN ({placeholders})", params)
        active = {str(r.get("id")) for r in rows}
    except Exception as exc:  # noqa: BLE001  无库时评估仍可跑结构判据
        return {"status": "unverifiable", "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                "cases": {}}

    out: dict[str, dict] = {}
    stale_total = 0
    for c in d.get("cases") or []:
        ids = [str(x) for x in (c.get("relevant_ids") or [])]
        stale = [x for x in ids if x not in active]
        stale_total += len(stale)
        out[c["case_id"]] = {
            "total": len(ids), "active": len(ids) - len(stale),
            "stale_ids": stale,
            "status": "valid" if not stale else ("stale" if len(stale) < len(ids) else "expired"),
        }
    return {"status": "ok" if not stale_total else "partial",
            "stale_total": stale_total, "checked": len(set(all_ids)), "cases": out}
