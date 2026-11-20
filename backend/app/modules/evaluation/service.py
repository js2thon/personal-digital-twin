"""统一评估运行器（SIL P0-4 / GV-02 / SIL-A10 / SIL-A12）。

职责：
  1. 对固定基准集 golden-v1 跑五组探针（本体 / 抽取 / 检索 / 决策 / 系统）；
  2. 用 metrics.py 的纯函数算实测值，不可测项一律 None + not_measurable 原因，不伪造；
  3. 产出带版本指纹的报告，支持与 baseline 做相对回归对比；
  4. 门禁只判「结构判据 + 相对回归」，不设虚构的绝对性能阈值（规范第 10 节）；
  5. 报告落盘到 backend/.runtime/eval/，供发布流程读取 gate 结果。

只读：不修改任何业务数据；仅写评估自身的报告目录。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import dataset as ds
from . import metrics as mx

from app.config import settings
from app.modules.semantic_runtime import service as sem
from app.modules.semantic_runtime.context import system_context

__all__ = ["run_suite", "latest_report", "report_history", "evaluate_gate",
           "compare_with_baseline", "save_baseline", "load_baseline",
           "to_markdown", "metric_catalog"]

EVAL_VERSION = "eval-1.0"
_GROUPS = mx.GROUPS
_BACKEND = Path(__file__).resolve().parents[3]
_REPORT_DIR = _BACKEND / ".runtime" / "eval"
_BASELINE = _REPORT_DIR / "baseline_golden-v1.json"
_LATEST = _REPORT_DIR / "latest.json"

_LOCK = threading.RLock()
_LAST: dict | None = None
_HISTORY: list[dict] = []
_MAX_HISTORY = 20

# 参与运行时指纹的代码文件（相对 backend/）。任一变更即视为不同运行时版本。
_FINGERPRINT_FILES = (
    "app/modules/semantic_runtime/service.py",
    "app/modules/semantic_runtime/context.py",
    "app/modules/retrieval/service.py",
    "app/modules/retrieval/lexical.py",
    "app/modules/ontology/sync.py",
    "app/modules/ontology/fact_events.py",
    "app/modules/evaluation/service.py",
    "app/modules/evaluation/metrics.py",
)


def metric_catalog() -> dict:
    return {"groups": list(_GROUPS), "metrics": mx.METRIC_CATALOG,
            "rule": "不设绝对性能阈值；门禁 = 结构判据 + 与 baseline 的相对回归"}


def runtime_fingerprint() -> str:
    h = hashlib.sha256()
    for rel in _FINGERPRINT_FILES:
        p = _BACKEND / rel
        try:
            h.update(rel.encode())
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _safe(fn: Callable, default: Any = None, note: str | None = None,
          notes: list[str] | None = None) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001  单探针失败不得让整次评估崩掉
        msg = f"{note or getattr(fn, '__name__', 'probe')}: {type(exc).__name__}: {str(exc)[:160]}"
        if notes is not None:
            notes.append(msg)
        return default


def _check(cid: str, ok: bool, detail: str = "") -> dict:
    return {"id": cid, "ok": bool(ok), "detail": detail}


def _sql_one(sql: str, params: dict | None = None) -> dict | None:
    from app.db import session as db
    return db.fetch_one(sql, params or {})


# ---------------------------------------------------------------- 组 1：本体质量

def _group_ontology() -> dict:
    notes: list[str] = []
    checks: list[dict] = []
    ctx = system_context()
    schema = _safe(lambda: sem.ontology_schema(ctx), {}, "ontology_schema", notes)
    versions = _safe(lambda: sem.ontology_versions(ctx), {}, "ontology_versions", notes)
    meta = _safe(lambda: sem.meta(), {}, "meta", notes)

    # 与 semantic_runtime.ontology_schema() 的真实契约字段对齐，不用臆造的字段名
    required = ("legacy", "cq_v2", "ontology_version", "context", "query_id")
    present = [k for k in required if k in (schema or {})]
    checks.append(_check("ONT-01", len(present) >= 2,
                         f"schema 响应含 {present or '无必需字段'}"))
    checks.append(_check("ONT-02", isinstance((versions or {}).get("versions"), list)
                         or isinstance((versions or {}).get("count"), int),
                         "版本列表可枚举"))
    checks.append(_check("ONT-03", bool((meta or {}).get("spec"))
                         and len((meta or {}).get("contract_endpoints") or []) > 0,
                         "meta 暴露 spec 与契约端点清单"))
    checks.append(_check("ONT-04", len((meta or {}).get("pipeline") or []) == 5,
                         "RP-1~RP-5 五阶段声明完整"))
    checks.append(_check("ONT-05", len((meta or {}).get("context_fields") or []) == 6,
                         "AC-01 六项上下文声明完整"))

    stats = _safe(lambda: _sql_one("""
        SELECT
          (SELECT count(*)::int FROM ontology.ontology_versions) AS versions,
          (SELECT count(*)::int FROM ontology.ontology_versions
             WHERE lower(status)='published') AS published,
          (SELECT count(*)::int FROM ontology.extension_proposals) AS proposals,
          (SELECT count(*)::int FROM ontology.entities) AS entities,
          (SELECT count(*)::int FROM ontology.relations WHERE status='active') AS relations,
          (SELECT count(*)::int FROM ontology.relations WHERE status='active'
             AND relation_type IS NOT NULL AND relation_type <> '') AS typed_relations,
          (SELECT count(*)::int FROM knowledge.facts f WHERE f.status='active') AS active_facts,
          (SELECT count(*)::int FROM knowledge.facts f JOIN ontology.entities e ON e.id=f.subject_id
             WHERE f.status='active') AS aligned_facts
    """), {}, "ontology_stats", notes) or {}

    m = {
        "schema_completeness": mx.rate(len(present), len(required)),
        "entity_alignment_rate": mx.rate(int(stats.get("aligned_facts") or 0),
                                         int(stats.get("active_facts") or 0)),
        "relation_validity_rate": mx.rate(int(stats.get("typed_relations") or 0),
                                          int(stats.get("relations") or 0)),
        "published_versions": stats.get("published"),
        "constraint_pass_rate": mx.rate(sum(1 for c in checks if c["ok"]), len(checks)),
    }
    if (m.get("published_versions") or 0) < 2:
        notes.append("SIL-A04 未达成：已发布本体版本 < 2，属 P1-2 范围，计入报告不阻断门禁")
    return {"metrics": m, "checks": checks, "cases": [{"case_id": "ONT-SET", "count": len(checks)}],
            "notes": notes, "raw": {"stats": stats,
                                    "ontology_version": (meta or {}).get("ontology_version")}}


# ---------------------------------------------------------------- 组 2：抽取质量

def _group_extraction() -> dict:
    notes: list[str] = []
    checks: list[dict] = []
    row = _safe(lambda: _sql_one("""
        SELECT
          count(*)::int AS total,
          count(*) FILTER (WHERE claim_id IS NOT NULL)::int AS grounded,
          count(*) FILTER (WHERE subject_id IS NOT NULL AND predicate IS NOT NULL
                             AND object_value IS NOT NULL AND confidence IS NOT NULL)::int AS complete,
          count(*) FILTER (WHERE contested IS TRUE)::int AS contested
        FROM knowledge.facts WHERE status='active'
    """), {}, "extraction_stats", notes) or {}
    claims = _safe(lambda: _sql_one("SELECT count(*)::int AS n FROM knowledge.claims"),
                   {}, "claims_count", notes) or {}
    total = int(row.get("total") or 0)
    m = {
        "evidence_grounding_rate": mx.rate(int(row.get("grounded") or 0), total),
        "attribute_completeness": mx.rate(int(row.get("complete") or 0), total),
        "conflict_detection_rate": mx.rate(int(row.get("contested") or 0), total),
        "claim_to_fact_rate": mx.rate(total, int(claims.get("n") or 0)),
    }
    checks.append(_check("EXT-01", total > 0, f"active 事实 {total} 条可统计"))
    checks.append(_check("EXT-02", all(m[k] is not None for k in
                                       ("evidence_grounding_rate", "attribute_completeness")),
                         "核心比率可测"))
    notes.append("evidence_grounding_rate 采用 claim_id 非空口径（下界）："
                 "knowledge.evidence_spans 无 fact_id/claim_id 列，无法直接反查，属 P2-2 缺口")
    return {"metrics": m, "checks": checks, "cases": [{"case_id": "EXT-SET", "count": total}],
            "notes": notes, "raw": {"stats": row, "claims": claims.get("n")}}


# ---------------------------------------------------------------- 组 3：检索质量

def _permission_accuracy() -> tuple[float | None, str]:
    """P1-4 权限过滤正确率：判定符合性矩阵 + 实库跨租户探针。

    矩阵与单元测试共用 `policy.conformance_cases()`；实库探针用外租户主体
    对 `knowledge.facts` 走真实 SQL 过滤，跨租户行可见即判失败。只读。
    """
    try:
        from app import policy as pol
    except Exception as exc:  # noqa: BLE001
        return None, f"policy 模块不可用: {type(exc).__name__}"
    cases = pol.conformance_cases()
    if not cases:
        return None, "符合性矩阵为空"
    ok = 0
    bad: list[str] = []
    for i, (ctx, resource, expected) in enumerate(cases):
        got = pol.decide(pol.principal_from(ctx), **resource)["allowed"]
        if got == expected:
            ok += 1
        else:
            bad.append(f"case{i}")
    acc = round(ok / len(cases), 4)
    detail = f"判定矩阵 {ok}/{len(cases)}"
    if bad:
        return acc, detail + f"；失败 {','.join(bad)}"
    # 实库跨租户探针：全部矩阵通过后才执行（只读 COUNT）
    try:
        from app.db import session as db

        probe = pol.principal_from({
            "tenant_id": "tenant_eval_probe", "user_id": "eval_probe",
            "permission_context": {"role": "owner",
                                   "scope": ["owner", "organization"]}})
        params: dict = {}
        clause = pol.read_clause("f", probe, params)
        row = db.fetch_one(
            f"SELECT COUNT(*) AS n FROM knowledge.facts f WHERE {clause}", params)
        visible = int((row or {}).get("n") or 0)
        if visible != 0:
            bad.append("跨租户探针可见行>0")
            return round(acc * 0.5, 4), detail + f"；跨租户探针可见 {visible} 行"
        detail += "；实库跨租户探针 0 行可见"
    except Exception as exc:  # noqa: BLE001  库不可用时不掩盖矩阵结论
        detail += f"；实库探针跳过({type(exc).__name__})"
    return acc, detail


def _group_retrieval() -> tuple[dict, list[float]]:
    d = ds.load_dataset()
    validity = ds.check_validity()
    notes: list[str] = []
    checks: list[dict] = []
    cases: list[dict] = []
    latencies: list[float] = []
    recalls: list[float | None] = []
    precisions: list[float | None] = []
    rrs: list[float] = []
    ndcgs: list[float | None] = []
    trace_ok = trace_total = 0
    degrade_ok = degrade_total = 0
    structure_ok = structure_total = 0
    errors: list[str] = []

    if validity.get("status") != "ok":
        notes.append(f"基准集锚点校验：{validity.get('status')} "
                     f"{validity.get('reason') or ''}（失效 {validity.get('stale_total', 0)} 条）")

    all_relevant = {str(x) for c in d["cases"] for x in (c.get("relevant_ids") or [])}

    for case in d["cases"]:
        cid = case["case_id"]
        k = int(case.get("top_k") or d.get("k_default") or 10)
        relevant = [str(x) for x in (case.get("relevant_ids") or [])]
        v = (validity.get("cases") or {}).get(cid) or {}
        if v.get("status") == "expired":
            notes.append(f"{cid} 锚点全部失效，跳过指标（不伪造回补）")
            continue
        t0 = time.perf_counter()
        try:
            res = sem.search(system_context(), {
                "query": case["query"], "top_k": k,
                "filters": case.get("filters") or {},
            })
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{cid}: {type(exc).__name__}: {str(exc)[:160]}")
            continue
        latencies.append((time.perf_counter() - t0) * 1000)
        items = res.get("items") or []
        ranked = [str(it.get("id")) for it in items if it.get("kind") == "fact"]
        r, p, rr, nd = (mx.recall_at_k(ranked, relevant, k),
                        mx.precision_at_k(ranked, relevant, k),
                        mx.reciprocal_rank(ranked, relevant),
                        mx.ndcg_at_k(ranked, relevant, k))
        recalls.append(r); precisions.append(p); rrs.append(rr); ndcgs.append(nd)

        traces = res.get("retrieval_trace") or []
        enabled = list(((res.get("retrieval_plan") or {}).get("enabled") or {}).keys()) or list(sem.CHANNELS)
        trace_total += 1
        if all(any(t.get("channel") == c for t in traces) for c in enabled):
            trace_ok += 1
        for t in traces:
            if t.get("degraded"):
                degrade_total += 1
                if str(t.get("reason") or "").strip():
                    degrade_ok += 1
        structure_total += 1
        if (res.get("query_id") and len(res.get("context") or {}) >= 6
                and all(isinstance(it.get("id"), str) for it in items)
                and all("channels" in it for it in items)):
            structure_ok += 1
        cases.append({"case_id": cid, "query": case["query"], "k": k,
                      "returned": len(ranked), "relevant": len(relevant),
                      "relevant_active": v.get("active"), "status": v.get("status", "unverified"),
                      "recall_at_k": r, "precision_at_k": p,
                      "reciprocal_rank": rr, "ndcg_at_k": nd,
                      "channels": [t.get("channel") for t in traces],
                      "degraded": res.get("degraded_channels") or [],
                      "latency_ms": round(latencies[-1], 2)})

    # 负例：不应召回任何基准事实，用于精确率与幻觉检查
    false_hits = 0
    for case in d.get("negative_cases") or []:
        try:
            res = sem.search(system_context(), {"query": case["query"],
                                                "top_k": int(case.get("top_k") or 5)})
            ranked = [str(it.get("id")) for it in (res.get("items") or [])
                      if it.get("kind") == "fact"]
            hits = sorted(set(ranked) & all_relevant)
            false_hits += len(hits)
            cases.append({"case_id": case["case_id"], "query": case["query"],
                          "returned": len(ranked), "false_hits": hits,
                          "note": case.get("note")})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{case['case_id']}: {type(exc).__name__}: {str(exc)[:120]}")

    perm_acc, perm_detail = _permission_accuracy()
    m = {
        "recall_at_k": mx.macro(recalls),
        "precision_at_k": mx.macro(precisions),
        "mrr": mx.mrr(rrs),
        "ndcg_at_k": mx.macro(ndcgs),
        "trace_completeness": mx.rate(trace_ok, trace_total),
        "degradation_labeling_rate": mx.rate(degrade_ok, degrade_total),
        "permission_filter_accuracy": perm_acc,
        "reranker_gain": None,
        "negative_false_hits": false_hits,
        "case_errors": errors,
    }
    checks.append(_check("RET-01", len(cases) >= len(d["cases"]),
                         f"基准用例执行 {len([c for c in cases if c['case_id'].startswith('RET')])}/{len(d['cases'])}"))
    checks.append(_check("RET-02", not errors, f"未捕获异常 {len(errors)} 个"))
    checks.append(_check("RET-03", m["trace_completeness"] == 1.0,
                         f"启用通道均有 trace：{trace_ok}/{trace_total}"))
    checks.append(_check("RET-04", m["degradation_labeling_rate"] in (None, 1.0),
                         f"降级通道均标注 reason：{degrade_ok}/{degrade_total}"))
    checks.append(_check("RET-05", mx.rate(structure_ok, structure_total) == 1.0,
                         f"返回结构契约（id/channels/context）：{structure_ok}/{structure_total}"))
    checks.append(_check("RET-06", false_hits == 0, f"负例误命中 {false_hits} 条"))
    checks.append(_check("RET-07", perm_acc is not None and perm_acc >= 0.95,
                         f"权限过滤正确率 {perm_acc}（判定矩阵 + 跨租户探针，P1-4）"
                         if perm_acc is not None else "权限过滤正确率不可测"))
    notes.append("reranker_gain 依 GV-02 属 not_measurable：P1-3 Reranker 未实现"
                 "（依赖 P0-1 向量层），见 known_gaps")
    if perm_detail:
        notes.append(f"permission_filter_accuracy（P1-4）：{perm_detail}")
    if any("vector" in (c.get("degraded") or []) for c in cases):
        notes.append("向量通道在全部用例中降级：外部 Embedding 网关不可用会拖长每次检索，"
                     "system.retrieval_p95_ms 为含该等待的真实端到端耗时，非检索算法耗时")
    return ({"metrics": m, "checks": checks, "cases": cases, "notes": notes,
             "raw": {"validity": validity, "latencies_ms": [round(x, 2) for x in latencies]}},
            latencies)


# ---------------------------------------------------------------- 组 4：决策质量

def _group_decision() -> dict:
    d = ds.load_dataset()
    notes: list[str] = []
    checks: list[dict] = []
    cases: list[dict] = []
    passed = consistent = covered = 0
    for case in d.get("decision_cases") or []:
        results, err = [], None
        for _ in range(3):
            try:
                results.append(sem.reason(system_context(), case["payload"]))
            except Exception as exc:  # noqa: BLE001
                err = f"{type(exc).__name__}: {str(exc)[:140]}"
                break
        if err:
            notes.append(f"{case['case_id']} 执行失败：{err}")
            cases.append({"case_id": case["case_id"], "ok": False, "error": err})
            continue
        first = results[0]
        key = case.get("expect_decision_key") or "decision"
        ok = key in first and first.get("confidence") is not None
        # decision_id / query_id / context 每次调用都会变，比较确定性输出时必须剔除，
        # 否则「决策一致性」会被随机 ID 误判为 0。
        volatile = ("decision_id", "query_id", "context")

        def stable(r: dict) -> str:
            return json.dumps({k: v for k, v in r.items() if k not in volatile},
                              sort_keys=True, default=str, ensure_ascii=False)

        same = all(stable(r) == stable(first) for r in results[1:])
        conf = first.get("confidence") or {}
        cov = all(k in conf for k in ("fact_confidence", "rule_confidence",
                                      "model_confidence"))
        passed += int(ok); consistent += int(same); covered += int(bool(cov))
        cases.append({"case_id": case["case_id"], "ok": ok, "consistent": same,
                      "evidence_covered": bool(cov), "decision": first.get(key),
                      "note": case.get("note")})
    n = len(d.get("decision_cases") or [])
    m = {
        "rule_execution_accuracy": mx.rate(passed, n),
        "decision_consistency": mx.rate(consistent, n),
        "evidence_coverage_rate": mx.rate(covered, n),
    }
    checks.append(_check("DEC-01", n > 0 and all(c.get("ok") for c in cases),
                         f"固定场景结构断言 {passed}/{n}"))
    checks.append(_check("DEC-02", m["decision_consistency"] == 1.0,
                         "同输入重复 3 次结果一致"))
    notes.append("reason() 当前为规则级占位输出，rule/model confidence 为 None；"
                 "A09 三类置信度分列落库仍属 P1-2 范围")
    return {"metrics": m, "checks": checks, "cases": cases, "notes": notes, "raw": {}}


# ---------------------------------------------------------------- 组 5：系统质量

def _group_system(retrieval_latencies: list[float]) -> dict:
    notes: list[str] = []
    checks: list[dict] = []
    def _probe_evidence() -> dict:
        # evidence() 要求至少给一个 id；用真实 fact_id 做探针，不用假造的空入参
        sample = sem.query(system_context(), {"limit": 1})
        fid = ((sample.get("facts") or [{}])[0] or {}).get("id")
        if not fid:
            raise LookupError("无样本事实可供证据链探针")
        return sem.evidence(system_context(), {"fact_ids": [str(fid)], "limit": 3})

    probes: list[tuple[str, Callable]] = [
        ("meta", lambda: sem.meta()),
        ("ontology_schema", lambda: sem.ontology_schema(system_context())),
        ("ontology_versions", lambda: sem.ontology_versions(system_context())),
        ("structured_query", lambda: sem.query(system_context(), {"limit": 3})),
        ("entity_resolve", lambda: sem.entity_resolve(system_context(), {"name": "某某", "limit": 3})),
        ("graph_traverse", lambda: sem.graph_traverse(system_context(),
                                                       {"mode": "subgraph", "depth": 1, "limit": 10})),
        ("evidence", _probe_evidence),
    ]
    times: list[float] = []
    ok_count = 0
    for name, fn in probes:
        t0 = time.perf_counter()
        try:
            fn()
            ok_count += 1
        except Exception as exc:  # noqa: BLE001
            notes.append(f"探针 {name} 失败：{type(exc).__name__}: {str(exc)[:140]}")
        times.append((time.perf_counter() - t0) * 1000)

    sync_stats = _safe(lambda: __import__(
        "app.modules.ontology.fact_events", fromlist=["stats"]).stats(),
        {}, "fact_events.stats", notes) or {}
    sync_status = _safe(lambda: __import__(
        "app.modules.ontology.sync", fromlist=["status"]).status(),
        {}, "sync.status", notes) or {}
    orphans = _safe(lambda: __import__(
        "app.modules.ontology.sync", fromlist=["orphans"]).orphans(limit=5),
        {}, "sync.orphans", notes) or {}

    # sync.status() 已把三类孤儿边汇总成 counts，比自行遍历列表字段更可靠
    orphan_counts = (sync_status or {}).get("orphans") or (orphans or {}).get("counts") or {}
    orphan_total = sum(int(v or 0) for v in orphan_counts.values()) if orphan_counts else 0
    m = {
        "api_p95_ms": mx.percentile(times, 0.95),
        "retrieval_p95_ms": mx.percentile(retrieval_latencies, 0.95),
        "endpoint_availability": mx.rate(ok_count, len(probes)),
        "sync_latency_ms": sync_stats.get("last_latency_ms") or sync_stats.get("avg_latency_ms"),
        "data_consistency": (1.0 if orphan_counts and orphan_total == 0 else None),
        "orphan_edge_total": orphan_total,
        "probe_latencies_ms": [round(x, 2) for x in times],
    }
    if m["sync_latency_ms"] is None:
        notes.append("sync_latency_ms 未从 fact_events.stats() 取得；历史实测 0.13s 仅作参考，"
                     "不写入本次实测值")
    checks.append(_check("SYS-01", ok_count == len(probes),
                         f"端点探针 {ok_count}/{len(probes)} 成功"))
    checks.append(_check("SYS-02", m["api_p95_ms"] is not None, "P95 可测（不设绝对阈值）"))
    listener_alive = (sync_stats or {}).get("thread_alive")
    connected = (sync_stats or {}).get("connected")
    # 直接进程内调用（未走 FastAPI startup）时监听线程本就不存在，属不可测而非失败；
    # 只有在确实启动过（已连接或已处理过批次）后掉线，才判为阻断。
    ever_started = bool(connected) or int((sync_stats or {}).get("batches") or 0) > 0
    if not ever_started:
        notes.append("pg_notify 监听未启动（评估器未运行在服务 startup 环境），SYS-03 记为不可测")
    checks.append(_check("SYS-03", (not ever_started) or bool(listener_alive),
                         "pg_notify 监听线程："
                         f"{'not_started' if not ever_started else listener_alive}"))
    return {"metrics": m, "checks": checks,
            "cases": [{"case_id": "SYS-PROBE", "probes": len(probes)}],
            "notes": notes,
            "raw": {"sync_stats": sync_stats, "sync_status": sync_status,
                    "orphans": orphans}}


# ---------------------------------------------------------------- 版本对照与门禁

_REGRESSION_KEYS = ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k",
                    "rule_execution_accuracy", "decision_consistency")


def load_baseline() -> dict | None:
    try:
        return json.loads(_BASELINE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(report: dict | None = None) -> dict:
    r = report or _LAST
    if not r:
        raise LookupError("尚无评估报告，请先运行 /evaluation/run")
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    slim = {"created_at": _now(), "source_run_id": r["run_id"],
            "dataset_version": r["versions"]["dataset_version"],
            "dataset_fingerprint": r["versions"]["dataset_fingerprint"],
            "runtime_fingerprint": r["versions"]["runtime_fingerprint"],
            "metrics": {g: v["metrics"] for g, v in r["groups"].items()}}
    _BASELINE.write_text(json.dumps(slim, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    return slim


def compare_with_baseline(report: dict, baseline: dict | None = None) -> dict:
    base = baseline if baseline is not None else load_baseline()
    if not base:
        return {"available": False, "reason": "无基线，回归判据暂不适用"}
    out: dict[str, dict] = {}
    for group, gv in report["groups"].items():
        bm = (base.get("metrics") or {}).get(group) or {}
        for key in _REGRESSION_KEYS:
            cur = (gv.get("metrics") or {}).get(key)
            prev = bm.get(key)
            if cur is None or prev is None:
                continue
            out[f"{group}.{key}"] = {"baseline": prev, "current": cur,
                                    "relative_delta": mx.relative_delta(float(cur), float(prev))}
    return {"available": True, "baseline_created_at": base.get("created_at"),
            "source_run_id": base.get("source_run_id"),
            "dataset_fingerprint": base.get("dataset_fingerprint"),
            "runtime_fingerprint": base.get("runtime_fingerprint"),
            "comparisons": out}


def evaluate_gate(report: dict, tolerance: float = 0.10) -> dict:
    """门禁 = 结构判据（硬） + 相对回归（硬） + 已知缺口（warn，不阻断）。

    不设绝对 Recall/P95 阈值：规范第 10 节要求性能阈值在环境与规模确定后另定。
    """
    blocking: list[str] = []
    warnings: list[str] = []
    passed: list[str] = []

    for group, gv in report["groups"].items():
        for c in gv.get("checks") or []:
            (passed if c["ok"] else blocking).append(f"{c['id']} {c.get('detail', '')}".strip())

    requested = set(report.get("groups_requested") or _GROUPS)
    scope = "full" if requested == set(_GROUPS) else "diagnostic"
    groups_present = set(report["groups"])
    missing = set(_GROUPS) - groups_present
    if missing:
        blocking.append(
            f"{'诊断运行' if scope == 'diagnostic' else '发布级运行'}五组指标未跑全，"
            f"缺 {sorted(missing)}；指定 groups 的诊断结果不得用于发布判定")

    comparison = report.get("comparison") or {}
    if not comparison.get("available"):
        warnings.append("无基线：本次仅判结构判据，回归判据未启用")
    else:
        for key, row in (comparison.get("comparisons") or {}).items():
            delta = row.get("relative_delta")
            if delta is None:
                continue
            if delta < -abs(tolerance):
                blocking.append(f"回归超阈值 {key}: {delta:+.2%} (允许 -{abs(tolerance):.0%})")
            elif delta < 0:
                warnings.append(f"轻微下降 {key}: {delta:+.2%}")

    for gap in ds.known_gaps():
        warnings.append(f"已知缺口 {gap['id']}（{gap['gate_effect']}）：{gap['desc']}")

    not_measurable = sorted({k for gv in report["groups"].values()
                             for k, v in (gv.get("metrics") or {}).items()
                             if k in ("permission_filter_accuracy", "reranker_gain",
                                      "sync_latency_ms", "data_consistency")
                             and v is None})
    if not_measurable:
        warnings.append("不可测指标（不计入门禁）：" + ", ".join(not_measurable))

    status = "fail" if blocking else ("warn" if warnings else "pass")
    return {"status": status, "scope": scope, "blocking": blocking, "warnings": warnings,
            "passed_count": len(passed), "checks_passed": passed,
            "regression_tolerance": tolerance,
            "rule": "结构判据 + 相对回归；无绝对性能阈值（SIL-Spec 第 10 节）",
            "release_blocking": bool(blocking)}


# ---------------------------------------------------------------- 运行与落盘

def _persist(report: dict) -> None:
    try:
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        _LATEST.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
        stamp = report["run_id"].replace(":", "").replace("-", "")
        (_REPORT_DIR / f"report_{stamp}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        files = sorted(_REPORT_DIR.glob("report_*.json"))
        for old in files[:-_MAX_HISTORY]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError as exc:  # noqa: BLE001  落盘失败不影响评估返回
        report["persist_error"] = str(exc)[:160]


def run_suite(groups: list[str] | None = None, persist: bool = True,
              regression_tolerance: float = 0.10,
              bootstrap_baseline: bool = True) -> dict:
    """跑一次完整评估。groups 为空表示五组全跑。"""
    t0 = time.perf_counter()
    d = ds.load_dataset()
    if groups is not None and not groups:
        raise ValueError("groups 不能为空列表；不传该字段表示五组全跑")
    selected = [g for g in _GROUPS if groups is None or g in set(groups)]
    unknown = sorted(set(groups or []) - set(_GROUPS))
    if unknown:
        raise ValueError(f"未知指标组 {unknown}；可选 {list(_GROUPS)}")
    report_groups: dict[str, dict] = {}
    latencies: list[float] = []
    global_notes: list[str] = []

    dispatch: dict[str, Callable] = {
        "ontology": lambda: (_group_ontology(), None),
        "extraction": lambda: (_group_extraction(), None),
        "retrieval": lambda: _group_retrieval(),
        "decision": lambda: (_group_decision(), None),
        "system": lambda: (_group_system(latencies), None),
    }
    # retrieval 先跑，供 system 使用实测检索耗时
    order = ["retrieval"] + [g for g in selected if g != "retrieval"]
    for g in order:
        if g not in selected:
            continue
        result, lats = _safe(lambda g=g: dispatch[g](),
                             ({"metrics": {}, "checks": [_check(
                                 f"{g.upper()}-00", False, "组执行异常")],
                               "cases": [], "notes": ["组执行异常"], "raw": {}}, None),
                             f"group_{g}", global_notes)
        report_groups[g] = result
        if lats:
            latencies = lats
    # 按标准组顺序输出，便于阅读与对照
    report_groups = {g: report_groups[g] for g in _GROUPS if g in report_groups}
    for g, gv in report_groups.items():
        gv["notes"] = list(gv.get("notes") or []) + (global_notes if g == "ontology" else [])

    meta = _safe(lambda: sem.meta(), {}, "meta", global_notes) or {}
    versions = {
        "eval_version": EVAL_VERSION,
        "spec_version": meta.get("spec"),
        "ontology_version": meta.get("ontology_version"),
        "dataset_version": d.get("dataset_version"),
        "dataset_fingerprint": ds.dataset_fingerprint(),
        "runtime_fingerprint": runtime_fingerprint(),
        "pipeline_version": settings.pipeline_version,
        "prompt_version": settings.prompt_version,
    }
    report: dict = {
        "run_id": f"{_now()}-{hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:6]}",
        "started_at": _now(),
        "duration_ms": None,
        "groups_requested": selected,
        "versions": versions,
        "dataset_snapshot": d.get("snapshot"),
        "groups": report_groups,
        "notes": global_notes,
    }
    baseline = load_baseline()
    baseline_bootstrapped = False
    # 不把「门禁失败」的运行固化成基线，否则后续回归会被拉高到错误基准
    provisional_gate = evaluate_gate(report, tolerance=regression_tolerance)
    can_bootstrap = (baseline is None and bootstrap_baseline and persist
                     and not provisional_gate["release_blocking"])
    if baseline is None and bootstrap_baseline and persist and not can_bootstrap:
        global_notes.append("本次门禁存在阻断项，未固化为基线；修好后重新运行即可建立基线")
    if can_bootstrap:
        try:
            save_baseline(report)
            baseline_bootstrapped = True
        except Exception as exc:  # noqa: BLE001
            global_notes.append(f"基线固化失败：{type(exc).__name__}: {str(exc)[:120]}")
    report["baseline_bootstrapped"] = baseline_bootstrapped
    report["comparison"] = ({"available": False,
                             "reason": "本次运行已固化为首个基线，回归判据自下次运行启用"}
                            if baseline_bootstrapped
                            else compare_with_baseline(report, baseline))
    report["gate"] = evaluate_gate(report, tolerance=regression_tolerance)
    report["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    if persist:
        _persist(report)
    with _LOCK:
        global _LAST
        _LAST = report
        _HISTORY.append({"run_id": report["run_id"], "started_at": report["started_at"],
                         "gate": report["gate"]["status"],
                         "runtime_fingerprint": versions["runtime_fingerprint"]})
        del _HISTORY[:-_MAX_HISTORY]
    return report


def latest_report() -> dict | None:
    with _LOCK:
        if _LAST is not None:
            return _LAST
    # 进程重启后内存态丢失，从磁盘恢复最近一次报告
    try:
        return json.loads(_LATEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report_history() -> list[dict]:
    with _LOCK:
        return list(_HISTORY)


def to_markdown(report: dict) -> str:
    """把报告渲染为可交付的 Markdown（不虚构结论，None 一律写「不可测」）。"""
    v = report["versions"]
    g = report["gate"]
    lines = [
        f"# ESII 语义基础设施层 · 质量评估报告",
        "",
        f"> 运行 ID：`{report['run_id']}` · 生成时间：{report['started_at']} · 耗时 {report['duration_ms']} ms",
        f"> 规范：{v['spec_version']} · 评估器：{v['eval_version']} · 本体版本：{v['ontology_version']}",
        f"> 基准集：{v['dataset_version']}（指纹 `{v['dataset_fingerprint']}`）· 运行时指纹 `{v['runtime_fingerprint']}`",
        "",
        f"## 门禁结论：{g['status'].upper()}",
        "",
        f"- 结构判据通过 {g['passed_count']} 项，阻断项 {len(g['blocking'])} 个，提示 {len(g['warnings'])} 条",
        f"- 判据规则：{g['rule']}；回归容差 {g['regression_tolerance']:.0%}",
        f"- 发布阻断：`{'是' if g['release_blocking'] else '否'}`",
        "",
    ]
    if g["blocking"]:
        lines += ["**阻断项**", ""] + [f"- {x}" for x in g["blocking"]] + [""]
    lines += ["## 五组指标实测", "", "| 组 | 指标 | 实测 | 口径 |", "|---|---|---:|---|"]
    catalog = {m["key"]: m for group in mx.METRIC_CATALOG.values() for m in group}
    for group, gv in report["groups"].items():
        for key, val in (gv.get("metrics") or {}).items():
            if key in ("probe_latencies_ms", "case_errors", "negative_false_hits"):
                continue
            shown = "不可测" if val is None else (f"{val}" if not isinstance(val, float)
                                                 else f"{val:.4f}")
            definition = (catalog.get(key) or {}).get("definition", "—")
            lines.append(f"| {group} | {key} | {shown} | {definition} |")
    lines += ["", "## 结构判据", "", "| 组 | 编号 | 结果 | 说明 |", "|---|---|---|---|"]
    for group, gv in report["groups"].items():
        for c in gv.get("checks") or []:
            lines.append(f"| {group} | {c['id']} | {'通过' if c['ok'] else '失败'} | "
                         f"{str(c.get('detail') or '').replace('|', '/')} |")
    lines += ["", "## 版本对照", ""]
    comp = report.get("comparison") or {}
    if not comp.get("available"):
        lines.append(f"本次无对照：{comp.get('reason', '—')}")
    else:
        lines += ["| 指标 | 基线 | 本次 | 相对变化 |", "|---|---:|---:|---:|"]
        for key, row in (comp.get("comparisons") or {}).items():
            dlt = row.get("relative_delta")
            dlt_text = "不可比" if dlt is None else format(dlt, "+.2%")
            lines.append(f"| {key} | {row['baseline']} | {row['current']} | {dlt_text} |")
    lines += ["", "## 已知缺口与说明", ""]
    for gap in ds.known_gaps():
        lines.append(f"- **{gap['id']}**（{gap['gate_effect']}）：{gap['desc']}")
    for group, gv in report["groups"].items():
        for n in gv.get("notes") or []:
            lines.append(f"- [{group}] {n}")
    lines += ["", "## 基准集", ""]
    s = ds.dataset_summary()
    lines += [f"- 检索用例 {s['case_count']} 条、负例 {s['negative_case_count']} 条、"
              f"固定决策场景 {s['decision_case_count']} 个，K={s['k_default']}",
              f"- 快照：`{s['snapshot']}`",
              "- 相关集定义为快照时刻该主体 active 事实按 id 稳定排序的前 10 条；"
              "锚点失效时剔除分母并标注，不伪造回补。",
              ""]
    return "\n".join(lines)
