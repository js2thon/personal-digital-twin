"""CQ 驱动本体工程 V2.0：常量、状态机与数据结构。

纯标准库，无 DB / Web 框架依赖。所有核心逻辑都基于普通 dict，
便于假 DB 测试、 CLI 演示和后续移植。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

# ---------------------------------------------------------------- 枚举
# 六类基础意图（V1.2 §6 的「四类/五类」说法已统一），另加两类决策扩展。
BASE_INTENTS = ("lookup", "path", "aggregate", "temporal", "boolean", "compare")
DECISION_INTENTS = ("decision", "recommendation")
INTENTS = BASE_INTENTS + DECISION_INTENTS
INTENT_LABELS = {
    "lookup": "查询取值", "path": "关系链路", "aggregate": "聚合统计",
    "temporal": "时点追溯", "boolean": "存在判定", "compare": "对比排序",
    "decision": "决策判断", "recommendation": "优先建议",
}

ANSWER_SHAPES = ("entity", "value", "list", "count", "yes_no", "decision")
CQ_KINDS = ("business", "test")
REVIEW_STATUSES = ("pending", "machine_passed", "human_approved", "rejected")
CQ_LIFECYCLE = ("draft", "active", "retired")

# 第一层：CQ 质量（定义得好不好）；第二层：可回答性；第三层：回答质量
QUALITY_DIMENSIONS = (
    "target", "clarity", "verifiability",
    "ontology_fit", "representativeness", "executability",
)
QUALITY_WEIGHTS = {
    "target": 25, "clarity": 20, "verifiability": 20,
    "ontology_fit": 15, "representativeness": 10, "executability": 10,
}
QUALITY_LABELS = {
    "target": "业务价值", "clarity": "问题明确性", "verifiability": "可验证性",
    "ontology_fit": "本体可表达性", "representativeness": "业务代表性",
    "executability": "可执行性",
}
RUBRIC_VERSION = "rubric-2.0.0"

# 五维覆盖 + 五态（评审第 6 点）
COVERAGE_DIMENSIONS = ("schema", "data", "evidence", "answer", "temporal")
ANSWER_STATES = ("answered", "partial", "unanswerable", "conflict", "restricted")
STATE_LABELS = {
    "answered": "答案正确、证据充分、权限允许",
    "partial": "部分信息满足，存在可定位缺口",
    "unanswerable": "缺少必要信息",
    "conflict": "存在未解决的事实冲突",
    "restricted": "数据存在，但当前用户无权访问",
}

# 六类缺口（评审第 7 点）：把「数据源/权限」从「数据缺失」里独立出来
GAP_KINDS = ("entity", "relation", "attribute", "evidence", "schema", "source_access")
GAP_LABELS = {
    "entity": "实体不存在", "relation": "关系不存在", "attribute": "必要属性缺失",
    "evidence": "缺乏有效证据", "schema": "本体缺少概念或约束",
    "source_access": "数据源未接入或授权不足",
}
GAP_SEVERITY = {
    "entity": 1.0, "relation": 1.0, "attribute": 0.8,
    "evidence": 1.2, "schema": 1.5, "source_access": 1.3,
}

PROPOSAL_OPERATIONS = ("reuse", "extend", "merge", "split", "create", "deprecate")
# Query Plan AST 允许的操作；任何 raw_sql / cypher 字段都会被校验器拒绝
PLAN_OPS = (
    "resolve_entity", "filter_attribute", "traverse", "aggregate", "compare",
    "exists", "asof", "retrieve", "validate", "decide",
)
FORBIDDEN_PLAN_KEYS = ("raw_sql", "sql", "cypher", "raw_query", "exec", "eval")
MAX_PLAN_STEPS = 12
MAX_HOPS = 3

LAYER_STATES = {
    "business": ("proposed", "approved", "retired"),
    "ontology": ("mapped", "reviewed", "published"),
    "evaluation": ("drafted", "validated", "active", "deprecated"),
}

# ---------------------------------------------------------------- 工具
def uid() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def checksum(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def json_dumps(obj) -> str:
    """统一的 JSONB 文本序列化：日期/uuid 等不可直接序列化的值降级为 str。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def clamp01(v) -> float:
    try:
        return round(max(0.0, min(1.0, float(v))), 3)
    except (TypeError, ValueError):
        return 0.0


def as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except (ValueError, TypeError):
            return []
    return []


def as_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            out = json.loads(v)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


# ---------------------------------------------------------------- 默认结构
def new_cq(**kw) -> dict:
    """一条 CQ 的完整规格（四层的第 2 层）。字段与 schema.sql 对齐。"""
    base = {
        "id": uid(), "code": kw.pop("code", ""),
        "question": kw.pop("question", ""),
        "question_template": kw.pop("question_template", ""),
        "sample_question": kw.pop("sample_question", ""),
        "business_domain": kw.pop("business_domain", ""),
        "business_purpose": kw.pop("business_purpose", ""),
        "capability_ids": kw.pop("capability_ids", []),
        "cq_kind": kw.pop("cq_kind", "business"),
        "intent": kw.pop("intent", "lookup"),
        "answer_shape": kw.pop("answer_shape", "entity"),
        "subject_type": kw.pop("subject_type", ""),
        "subject_side": kw.pop("subject_side", "source"),
        "target_type": kw.pop("target_type", ""),
        "required_relations": kw.pop("required_relations", []),
        "required_attributes": kw.pop("required_attributes", []),
        "scope_constraints": kw.pop("scope_constraints", {}),
        "temporal_constraints": kw.pop("temporal_constraints", {}),
        "expected_answer": kw.pop("expected_answer", {}),
        "expected_entities": kw.pop("expected_entities", []),
        "expected_relations": kw.pop("expected_relations", []),
        "expected_evidence": kw.pop("expected_evidence", {}),
        "acceptance_criteria": kw.pop("acceptance_criteria", {}),
        "evidence_requirements": kw.pop("evidence_requirements", {}),
        "evaluation_method": kw.pop("evaluation_method", "exact_match"),
        "min_evidence": kw.pop("min_evidence", 1),
        "priority": kw.pop("priority", 2),
        "cq_weight": kw.pop("cq_weight", 1.0),
        "risk_level": kw.pop("risk_level", "normal"),
        "owner": kw.pop("owner", ""),
        "domain": kw.pop("domain", ""),
        "quality_score": None, "quality_dimensions": {}, "hard_gates": {},
        "quality_rubric_version": RUBRIC_VERSION,
        "review_status": "pending",
        "layer_states": {"business": "proposed", "ontology": "mapped",
                         "evaluation": "drafted"},
        "status": "draft",
        "version": 1, "schema_version": "", "query_plan_version": "",
        "test_dataset_id": "", "admitted_at": "", "admitted_by": "",
        "valid_from": "", "valid_to": "", "notes": kw.pop("notes", ""),
        "created_at": now(), "updated_at": now(),
    }
    base.update(kw)
    return base


def new_capability(**kw) -> dict:
    return {
        "id": uid(), "code": kw.pop("code", ""), "name": kw.pop("name", ""),
        "business_domain": kw.pop("business_domain", ""),
        "description": kw.pop("description", ""), "owner": kw.pop("owner", ""),
        "priority": kw.pop("priority", 2), "risk_level": kw.pop("risk_level", "normal"),
        "weight": kw.pop("weight", 1.0), "status": "active", "version": 1,
        "created_at": now(), **kw,
    }


def new_instance(**kw) -> dict:
    return {
        "id": uid(), "cq_id": kw.pop("cq_id", ""), "cq_version": 1,
        "instance_code": kw.pop("instance_code", ""),
        "params": kw.pop("params", {}), "question_text": kw.pop("question_text", ""),
        "expected_answer": kw.pop("expected_answer", {}),
        "expected_entities": kw.pop("expected_entities", []),
        "expected_relations": kw.pop("expected_relations", []),
        "expected_evidence": kw.pop("expected_evidence", {}),
        "acceptance_criteria": kw.pop("acceptance_criteria", {}),
        "evidence_refs": kw.pop("evidence_refs", []),
        "source_type": kw.pop("source_type", "human_annotation"),
        "source_ref": kw.pop("source_ref", ""),
        "annotated_by": kw.pop("annotated_by", ""),
        "valid_time": kw.pop("valid_time", {}),
        "status": kw.pop("status", "draft"), "created_at": now(), **kw,
    }


# ---------------------------------------------------------------- 状态机
def can_transition(layer: str, frm: str, to: str) -> bool:
    seq = LAYER_STATES.get(layer)
    if not seq or frm not in seq or to not in seq:
        return False
    return seq.index(to) >= seq.index(frm)


def transition(cq: dict, layer: str, to: str) -> dict:
    """三层状态独立；任一层推进都要抬 CQ 版本，其余层回到待复核。"""
    cur = dict(as_dict(cq.get("layer_states")) or
               {"business": "proposed", "ontology": "mapped", "evaluation": "drafted"})
    frm = cur.get(layer, LAYER_STATES[layer][0])
    if not can_transition(layer, frm, to):
        raise ValueError(f"非法状态迁移：{layer} {frm} -> {to}")
    cur[layer] = to
    return {"layer_states": cur, "version": int(cq.get("version") or 1) + 1,
            "updated_at": now()}


def grade_of(score) -> str:
    if score is None:
        return "未评分"
    s = float(score)
    if s >= 90:
        return "优秀"
    if s >= 80:
        return "良好"
    if s >= 70:
        return "合格"
    if s >= 60:
        return "待优化"
    return "不合格"


ARG_KEYS = ("person", "project", "department", "as_of", "entity", "topic")


def render_template(template: str, params: dict) -> str:
    """参数化模板渲染：{person} 在 {as_of} 的直属上级是谁？"""
    out = template or ""
    for key, val in (params or {}).items():
        out = out.replace("{" + key + "}", str(val))
    return out


def unresolved_params(template: str) -> list[str]:
    import re
    return sorted(set(re.findall(r"\{(\w+)\}", template or "")))
