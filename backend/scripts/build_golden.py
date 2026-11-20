"""从实库只读抽样生成固定基准集 golden_v1.json（SIL P0-4 / GV-02）。

为什么基准集不入仓
------------------
`golden_v1.json` 里的 query 是实库真实主体名，relevant_ids 是真实 fact UUID。
这类内容属于运行时数据，不应进入代码仓库。仓库内只保留结构完全一致的
`golden_v1.example.json`（脱敏占位），`dataset.active_path()` 会在真实文件缺失时
自动回退到模板，保证 clone 后 `pytest` 可直接跑通。

何时运行本脚本
--------------
- 首次部署后，需要跑发布级评估门禁之前
- 数据发生结构性变更（主体改名、大批事实撤销）后，需要升级 dataset_version 时

用法
----
    cd backend
    python scripts/build_golden.py                # 生成 golden_v1.json
    python scripts/build_golden.py --min-facts 5  # 调整主体入选阈值
    python scripts/build_golden.py --dry-run      # 只打印摘要，不写文件

注意：本脚本只读数据库，不写入任何业务数据。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "app/modules/evaluation/golden_v1.json"

SNAPSHOT_SQL = """
SELECT 'facts_total' k, count(*)::text v FROM knowledge.facts
UNION ALL SELECT 'facts_active', count(*)::text FROM knowledge.facts WHERE status='active'
UNION ALL SELECT 'chunks', count(*)::text FROM knowledge.semantic_chunks
UNION ALL SELECT 'chunk_vectors', count(*)::text
    FROM knowledge.semantic_chunks WHERE embedding_vec IS NOT NULL AND embedding_vec <> ''
UNION ALL SELECT 'evidence_spans', count(*)::text FROM knowledge.evidence_spans
UNION ALL SELECT 'entities', count(*)::text FROM ontology.entities
UNION ALL SELECT 'relations_active', count(*)::text FROM ontology.relations WHERE status='active'
UNION ALL SELECT 'ontology_versions', count(*)::text FROM ontology.ontology_versions
UNION ALL SELECT 'extension_proposals', count(*)::text FROM ontology.extension_proposals
UNION ALL SELECT 'outbox_events', count(*)::text FROM ingest.outbox_events
"""

SUBJECTS_SQL = """
SELECT e.canonical_name, count(*) AS n
FROM knowledge.facts f JOIN ontology.entities e ON e.id = f.subject_id
WHERE f.status = 'active'
GROUP BY 1 HAVING count(*) >= :min_facts
ORDER BY n DESC, e.canonical_name LIMIT :lim
"""

CASE_FACTS_SQL = """
SELECT f.id::text AS fid, f.predicate
FROM knowledge.facts f JOIN ontology.entities e ON e.id = f.subject_id
WHERE f.status = 'active' AND e.canonical_name = :name
ORDER BY f.id LIMIT 10
"""

FILTERED_FACTS_SQL = """
SELECT f.id::text AS fid
FROM knowledge.facts f JOIN ontology.entities e ON e.id = f.subject_id
WHERE f.status = 'active' AND e.canonical_name = :name AND f.predicate = :pred
ORDER BY f.id LIMIT 10
"""

KNOWN_GAPS = [
    {"id": "GAP-P1-4", "desc": "租户 / ABAC 逐点权限过滤未实现，权限过滤正确率不可测",
     "gate_effect": "excluded_from_gate"},
    {"id": "GAP-P0-1", "desc": "向量覆盖率不足且无 pgvector 扩展与 ANN 索引，Reranker 增益不可测",
     "gate_effect": "excluded_from_gate"},
    {"id": "GAP-P0-3", "desc": "Fact 字段契约未补齐，属性完整性按现有列集合计算",
     "gate_effect": "reported_only"},
    {"id": "GAP-P1-1", "desc": "Outbox 账本为空，事件一致性以 pg_notify 实时投影自检代替",
     "gate_effect": "reported_only"},
]


def build(min_facts: int, limit: int) -> dict:
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        snapshot = {r.k: int(r.v) for r in conn.execute(text(SNAPSHOT_SQL))}
        subjects = [r.canonical_name for r in conn.execute(
            text(SUBJECTS_SQL), {"min_facts": min_facts, "lim": limit})]

        cases: list[dict] = []
        for i, name in enumerate(subjects, 1):
            rows = list(conn.execute(text(CASE_FACTS_SQL), {"name": name}))
            cases.append({
                "case_id": f"RET-{i:03d}", "group": "retrieval", "query": name,
                "top_k": 10, "filters": {}, "expect_subject": name,
                "relevant_ids": [r.fid for r in rows],
                "relevant_definition": "快照时刻该主体 active 事实按 id 稳定排序的前 10 条",
                "predicates": sorted({r.predicate for r in rows}),
                "note": "主体名单词查询，四通道均应有召回",
            })

        # 谓词过滤用例：取前两个主体的首个谓词
        for base in cases[:2]:
            pred = (base["predicates"] or [None])[0]
            if not pred:
                continue
            rows = list(conn.execute(
                text(FILTERED_FACTS_SQL), {"name": base["expect_subject"], "pred": pred}))
            if not rows:
                continue
            cases.append({
                "case_id": f"RET-{len(cases) + 1:03d}", "group": "retrieval",
                "query": base["expect_subject"], "top_k": 10,
                "filters": {"predicate": pred}, "expect_subject": base["expect_subject"],
                "relevant_ids": [r.fid for r in rows],
                "relevant_definition": f"快照时刻该主体 active 且 predicate={pred} 的事实（前 10）",
                "predicates": [pred], "note": "结构化谓词过滤回归",
            })

    return {
        "dataset_version": "golden-v1",
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "personal_twin 实库只读抽样（knowledge.facts / ontology.entities）",
        "snapshot": snapshot,
        "k_default": 10,
        "cases": cases,
        "negative_cases": [
            {"case_id": "NEG-001", "query": "zzqxx-不存在的实体-9f3a2b", "top_k": 5,
             "relevant_ids": [], "note": "负例：不应命中任何基准事实，用于精确率与幻觉检查"},
            {"case_id": "NEG-002", "query": "量子引力弦论实验室 2099 年度报告", "top_k": 5,
             "relevant_ids": [], "note": "负例：中文长尾无相关事实"},
        ],
        "decision_cases": [
            {"case_id": "DEC-001",
             "payload": {"risk_level": "high", "action": "send_email",
                         "facts": [{"fact_id": "f1", "confidence": 0.9}]},
             "expect_decision_key": "decision", "note": "高风险外发必须进入策略判定且结构完整"},
            {"case_id": "DEC-002",
             "payload": {"risk_level": "low", "action": "internal_note",
                         "facts": [{"fact_id": "f2", "confidence": 0.8}]},
             "expect_decision_key": "decision", "note": "低风险内部动作回归"},
            {"case_id": "DEC-003",
             "payload": {"risk_level": "medium", "action": "summarize", "facts": []},
             "expect_decision_key": "decision", "note": "信息不足场景应显式标注"},
        ],
        "known_gaps": KNOWN_GAPS,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="从实库生成固定基准集 golden_v1.json")
    ap.add_argument("--min-facts", type=int, default=4, help="主体入选的最少 active 事实数")
    ap.add_argument("--limit", type=int, default=8, help="最多取多少个主体作为检索用例")
    ap.add_argument("--dry-run", action="store_true", help="只打印摘要，不写文件")
    args = ap.parse_args()

    data = build(args.min_facts, args.limit)
    if not data["cases"]:
        print("未取到任何主体用例：请确认实库有 active 事实，或调低 --min-facts", file=sys.stderr)
        return 1

    print(f"用例 {len(data['cases'])} 条 / 负例 {len(data['negative_cases'])} / "
          f"决策 {len(data['decision_cases'])}")
    print("快照:", data["snapshot"])
    for c in data["cases"][:5]:
        print(f"  {c['case_id']} query={c['query']!r} relevant={len(c['relevant_ids'])} "
              f"filters={c['filters']}")

    if args.dry_run:
        print("\n--dry-run：未写文件")
        return 0

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}（{OUT.stat().st_size} 字节）")
    print("提示：该文件含实库主体名与 fact id，已在 .gitignore 中排除，勿提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
