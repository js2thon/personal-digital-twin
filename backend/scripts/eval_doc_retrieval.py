"""文档级语义检索质量评测（golden-doc-v1）。

用法（必须在仓库根执行，脚本会把根 .env 注入进程环境，避免 CWD 漂移）：
    python backend/scripts/eval_doc_retrieval.py --out backend/.runtime/eval/doc_after.json
    python backend/scripts/eval_doc_retrieval.py --baseline backend/.runtime/eval/doc_before.json ...

指标全部由 evaluation.metrics 纯函数计算；分母为 0 返回 None，不编造数值。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# 关键：必须在 import app.config 之前注入根 .env。
# 否则 env_file=".env" 按 CWD 解析，会回落到 bge-m3@8001，与已落库向量不同源。
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from app.config import settings  # noqa: E402
from app.modules.evaluation import metrics as mx  # noqa: E402
from app.modules.retrieval import service as retrieval  # noqa: E402

DEFAULT_DATASET = ROOT / "backend" / ".runtime" / "eval" / "golden_doc_v1.json"


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(dataset: dict, top_k: int = 5, verbose: bool = True) -> dict:
    cases = dataset.get("cases") or []
    tenant = dataset.get("tenant_id") or "default"
    principal = {"tenant_id": tenant}
    results = []
    for case in cases:
        q = case["query"]
        relevant = [str(r["document_id"]) for r in case.get("relevant") or []]
        res = retrieval.retrieve(q, scope=["documents"], top_k=top_k,
                                 principal=principal)
        docs = res.get("documents") or []
        seen: set[str] = set()
        ranked: list[dict] = []
        for d in docs:
            did = str(d.get("document_id") or "")
            if not did or did in seen:
                continue
            seen.add(did)
            ranked.append({"document_id": did, "title": d.get("title") or "",
                           "via": d.get("via"), "score": d.get("score"),
                           "rrf_score": d.get("rrf_score"),
                           "rerank_score": d.get("rerank_score")})
        ids = [r["document_id"] for r in ranked]
        cm: dict = {
            "recall_at_1": mx.recall_at_k(ids, relevant, 1),
            "recall_at_3": mx.recall_at_k(ids, relevant, 3),
            "recall_at_5": mx.recall_at_k(ids, relevant, 5),
            "reciprocal_rank": mx.reciprocal_rank(ids, relevant),
            "ndcg_at_5": mx.ndcg_at_k(ids, relevant, 5),
            "hit_at_1": bool(relevant and ids[:1] and ids[0] in relevant),
            "hit_at_3": bool(relevant and set(ids[:3]) & set(relevant)),
            "hit_at_5": bool(relevant and set(ids[:5]) & set(relevant)),
        }
        if not relevant:
            cm["negative_false_hit"] = bool(set(ids[:5]))
            cm["negative_hit_count"] = len(set(ids[:5]) & {"__tx_dedup_test__"})
        diag = res.get("diagnostics") or {}
        results.append({
            "id": case["id"], "query": q,
            "tier": case.get("tier") or ("negative" if not relevant else "lexical"),
            "relevant": relevant,
            "evidence": case.get("relevant") or [],
            "ranked": ranked, "metrics": cm,
            "diagnostics": {k: diag.get(k) for k in (
                "vector_recall_used", "vector_candidates", "vector_top_sim",
                "fusion", "embedding_model", "rerank_used", "rerank_candidates",
                "rerank_kept", "rerank_top_score", "rerank_min_score", "rerank_error")},
        })
        if verbose:
            flag = "HIT " if cm["hit_at_5"] else "MISS"
            print(f"[{case['id']:>11}] {flag} R@5={cm['recall_at_5']} "
                  f"RR={cm['reciprocal_rank']}  {q[:28]}")
            print(f"             top3: {' | '.join(r['title'][:22] for r in ranked[:3])}")

    def summarize(subset: list[dict]) -> dict:
        pos = [r for r in subset if r["relevant"]]
        neg = [r for r in subset if not r["relevant"]]
        def agg(key: str):
            if key in ("recall_at_1", "recall_at_3", "recall_at_5",
                       "reciprocal_rank", "ndcg_at_5"):
                return mx.macro([r["metrics"][key] for r in pos])
            return (round(sum(bool(r["metrics"][key]) for r in pos) / len(pos), 6)
                    if pos else None)
        return {
            "n_positive": len(pos), "n_negative": len(neg),
            "recall_at_1": agg("recall_at_1"),
            "recall_at_3": agg("recall_at_3"),
            "recall_at_5": agg("recall_at_5"),
            "mrr_at_5": agg("reciprocal_rank"),
            "ndcg_at_5": agg("ndcg_at_5"),
            "hit_at_1": agg("hit_at_1"),
            "hit_at_3": agg("hit_at_3"),
            "hit_at_5": agg("hit_at_5"),
            "negative_false_hit_rate": (
                round(sum(bool(r["metrics"].get("negative_false_hit")) for r in neg) / len(neg), 6)
                if neg else None),
        }

    by_tier = {t: summarize([r for r in results if r["tier"] == t])
               for t in sorted({r["tier"] for r in results})}
    summary = summarize(results)
    return {
        "dataset_version": dataset.get("dataset_version"),
        "dataset_sha256": hashlib.sha256(
            json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16],
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {
            "embedding_model": settings.embedding_model,
            "embedding_base_url": settings.embedding_base_url,
            "vector_backend": settings.vector_backend,
        },
        "top_k": top_k,
        "summary": summary,
        "by_tier": by_tier,
        "cases": results,
    }


def compare(report: dict, baseline: dict) -> dict:
    out = {}
    for key, cur in report["summary"].items():
        base = baseline.get("summary", {}).get(key)
        if not isinstance(cur, (int, float)) or not isinstance(base, (int, float)):
            continue
        out[key] = {"baseline": base, "current": cur, "delta": round(cur - base, 6)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    ds = load_dataset(args.dataset)
    rep = evaluate(ds, top_k=args.top_k, verbose=not args.quiet)
    if args.baseline:
        rep["comparison"] = compare(rep, json.loads(
            args.baseline.read_text(encoding="utf-8")))
        print("\n--- 与基线对比 ---")
        for k, v in rep["comparison"].items():
            print(f"  {k:<24} {v['baseline']} -> {v['current']}  (Δ {v['delta']})")
    print("\n--- 汇总（overall）---")
    for k, v in rep["summary"].items():
        print(f"  {k:<24} {v}")
    print("\n--- 分层 ---")
    for tier, sm in rep["by_tier"].items():
        print(f"  [{tier}] n+={sm['n_positive']} n-={sm['n_negative']} "
              f"R@5={sm['recall_at_5']} MRR={sm['mrr_at_5']} "
              f"Hit@1={sm['hit_at_1']} 误命中={sm['negative_false_hit_rate']}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.out}")


if __name__ == "__main__":
    main()
