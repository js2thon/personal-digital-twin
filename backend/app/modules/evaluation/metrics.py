"""GV-02 五组质量指标的纯函数实现（SIL P0-4）。

设计约束：
1. 全部为纯函数，不连库、不读文件、不依赖 FastAPI，便于离线单测；
2. 不内置任何绝对性能阈值 —— 规范第 10 节明确「性能指标在部署环境和数据规模
   确定后单独制定」，本模块只产出实测值，门禁用「结构判据 + 相对回归」；
3. 分母为 0 一律返回 None（不可测），而不是 0（伪失败）或 1（伪通过）。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

__all__ = [
    "recall_at_k", "precision_at_k", "reciprocal_rank", "mrr",
    "ndcg_at_k", "macro", "rate", "percentile", "relative_delta",
    "METRIC_CATALOG", "GROUPS",
]


def _ids(values: Iterable) -> list[str]:
    return [str(v) for v in values if v is not None]


def recall_at_k(ranked: Sequence, relevant: Sequence, k: int) -> float | None:
    """Recall@K：前 K 个结果命中的相关项 / 相关项总数。"""
    rel = set(_ids(relevant))
    if not rel:
        return None
    hit = len(set(_ids(ranked[:k])) & rel)
    return round(hit / len(rel), 6)


def precision_at_k(ranked: Sequence, relevant: Sequence, k: int) -> float | None:
    """Precision@K：前 K 个结果中相关项 / K。"""
    if k <= 0:
        return None
    rel = set(_ids(relevant))
    window = _ids(ranked[:k])
    if not window:
        return 0.0
    return round(len(set(window) & rel) / len(window), 6)


def reciprocal_rank(ranked: Sequence, relevant: Sequence) -> float:
    """单条查询的 MRR 贡献：首个相关结果的排名倒数，未命中为 0。"""
    rel = set(_ids(relevant))
    if not rel:
        return 0.0
    for i, rid in enumerate(_ids(ranked), 1):
        if rid in rel:
            return round(1.0 / i, 6)
    return 0.0


def mrr(per_query: Sequence[float]) -> float | None:
    """跨查询平均 MRR。"""
    vals = [float(x) for x in per_query if x is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def ndcg_at_k(ranked: Sequence, relevant: Sequence, k: int,
              gains: dict[str, float] | None = None) -> float | None:
    """NDCG@K：二元相关性（或自定义增益）的折损累积增益。

    IDCG 用「相关项全排在前面」的理想序列计算，因此结果落在 [0, 1]。
    """
    rel = set(_ids(relevant))
    if not rel or k <= 0:
        return None
    g = {str(key): float(val) for key, val in (gains or {}).items()}

    def gain(rid: str) -> float:
        return g.get(rid, 1.0) if rid in rel else 0.0

    dcg = sum(gain(rid) / math.log2(i + 1)
              for i, rid in enumerate(_ids(ranked[:k]), 1))
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(rel), k) + 1))
    if ideal <= 0:
        return None
    return round(min(1.0, dcg / ideal), 6)


def macro(values: Sequence[float | None]) -> float | None:
    """忽略不可测项后的宏平均；全不可测返回 None。"""
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def rate(passed: int, total: int) -> float | None:
    """结构判据通过率。"""
    if total <= 0:
        return None
    return round(min(1.0, max(0.0, passed / total)), 6)


def percentile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数（P95 用 p=0.95）。不四舍五入到整数毫秒。"""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return round(vals[0], 3)
    pos = (len(vals) - 1) * min(1.0, max(0.0, p))
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vals) - 1)
    return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 3)


def relative_delta(current: float | None, baseline: float | None) -> float | None:
    """相对变化率：(current - baseline) / |baseline|；基线为 0 或缺失时不可比。"""
    if current is None or baseline is None or abs(baseline) < 1e-9:
        return None
    return round((current - baseline) / abs(baseline), 6)


# ---------------------------------------------------------------- 指标目录（GV-02）

GROUPS = ("ontology", "extraction", "retrieval", "decision", "system")

METRIC_CATALOG: dict[str, list[dict]] = {
    "ontology": [
        {"key": "schema_completeness", "label": "本体结构完整率", "unit": "rate",
         "definition": "schema 响应中必需字段出现数 / 必需字段数"},
        {"key": "entity_alignment_rate", "label": "实体对齐率", "unit": "rate",
         "definition": "active 事实中 subject_id 可关联实体的占比"},
        {"key": "relation_validity_rate", "label": "关系有效率", "unit": "rate",
         "definition": "active 关系中未标注类型违规的占比"},
        {"key": "published_versions", "label": "已发布本体版本数", "unit": "count",
         "definition": "ontology_versions 行数（A04 要求 >= 2，当前为已知缺口）"},
        {"key": "constraint_pass_rate", "label": "约束通过率", "unit": "rate",
         "definition": "固定断言集通过数 / 断言数"},
    ],
    "extraction": [
        {"key": "evidence_grounding_rate", "label": "证据绑定率", "unit": "rate",
         "definition": "active 事实中 claim_id 非空或存在 evidence_span 的占比"},
        {"key": "attribute_completeness", "label": "属性完整率", "unit": "rate",
         "definition": "active 事实中现有核心字段非空占比（P0-3 后口径会变）"},
        {"key": "conflict_detection_rate", "label": "冲突可发现率", "unit": "rate",
         "definition": "contested 状态事实占比，作为冲突发现能力的下界观测"},
        {"key": "claim_to_fact_rate", "label": "Claim→Fact 转化率", "unit": "rate",
         "definition": "facts / claims"},
    ],
    "retrieval": [
        {"key": "recall_at_k", "label": "Recall@K", "unit": "rate", "definition": "前 K 命中相关数 / 相关总数"},
        {"key": "precision_at_k", "label": "Precision@K", "unit": "rate", "definition": "前 K 命中相关数 / K"},
        {"key": "mrr", "label": "MRR", "unit": "rate", "definition": "各查询首个相关结果排名倒数的平均"},
        {"key": "ndcg_at_k", "label": "NDCG@K", "unit": "rate", "definition": "二元增益折损累积增益 / 理想增益"},
        {"key": "trace_completeness", "label": "检索轨迹完整率", "unit": "rate",
         "definition": "启用通道均在 retrieval_trace 中有记录的查询占比"},
        {"key": "degradation_labeling_rate", "label": "降级标注率", "unit": "rate",
         "definition": "降级通道中带非空 reason 的占比"},
        {"key": "permission_filter_accuracy", "label": "权限过滤正确率", "unit": "rate",
         "definition": "八项安全测试通过率（P1-4 未实现，标记 not_measurable）"},
        {"key": "reranker_gain", "label": "Reranker 增益", "unit": "delta",
         "definition": "启用 Reranker 前后 NDCG 差（P1-3 未实现，标记 not_measurable）"},
    ],
    "decision": [
        {"key": "rule_execution_accuracy", "label": "规则执行正确率", "unit": "rate",
         "definition": "固定决策场景中结构契约断言通过数 / 场景数"},
        {"key": "decision_consistency", "label": "决策一致性", "unit": "rate",
         "definition": "同一输入重复 3 次结果一致的场景占比"},
        {"key": "evidence_coverage_rate", "label": "决策证据覆盖率", "unit": "rate",
         "definition": "输出带 confidence 与 fact_confidence 的场景占比"},
    ],
    "system": [
        {"key": "api_p95_ms", "label": "API P95", "unit": "ms", "definition": "固定端点探针耗时线性插值 P95"},
        {"key": "retrieval_p95_ms", "label": "检索 P95", "unit": "ms", "definition": "基准检索用例耗时 P95"},
        {"key": "endpoint_availability", "label": "端点可用性", "unit": "rate", "definition": "探针成功数 / 探针数"},
        {"key": "sync_latency_ms", "label": "投影同步延迟", "unit": "ms",
         "definition": "fact_events 自检上报值；无上报时为历史实测 130ms 基线"},
        {"key": "data_consistency", "label": "数据一致性", "unit": "rate",
         "definition": "孤儿投影边为 0 记 1.0，否则按比例扣减"},
    ],
}
