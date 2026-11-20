"""工作流决策图：
解析业务上下文 → 检索知识/历史决策/认知 → 证据充分性判断
→ (不足→要求补充材料) → LLM 决策分析 → 确定性规则检查 → 风险与权限评估
→ {不允许自动执行 → 生成人工审批任务} → 执行前最终校验 → 保存决策与审计
MVP：是否允许自动执行 = 否（Policy Engine D2 固定）。
"""
import json

from langgraph.graph import END, StateGraph

from app.modules.policy import engine as policy
# 统一语义运行时（SIL B2）：Agent 不直接调用底层检索器，蓝图 20.2 第 8 条
from app.modules.semantic_runtime import service as semantic
from app.modules.distillation.prompts import DECISION_SYSTEM
from app.modules.llm.client import llm_client
from app.modules.twin import trace as tr
from app.modules.twin.state import TwinState

# 确定性规则示例：高风险动作必须人工审批（规则引擎 MVP 版）
_HARD_RULES = [
    {"condition": lambda ctx: float(ctx.get("budget", 0) or 0) > 500000,
     "verdict": "approval_required", "reason": "预算超过 50 万，必须人工审批"},
]


def _analyze(state: TwinState) -> TwinState:
    ctx = state.get("business_context") or {}
    retrieval_result = semantic.agent_retrieve(
        json.dumps(ctx, ensure_ascii=False) + " " + state.get("input_message", ""),
        ["facts", "memories", "documents"],
    )
    state["retrieved_facts"] = retrieval_result["facts"]
    state["retrieved_memories"] = retrieval_result["memories"]
    state["retrieved_documents"] = retrieval_result["documents"]
    has_evidence = bool(
        state.get("retrieved_facts") or state.get("retrieved_memories")
        or state.get("retrieved_documents")
    )
    state["evidence_sufficiency"] = "sufficient" if has_evidence else "insufficient"
    bound = sum(1 for f in state.get("retrieved_facts") or [] if f.get("subject_name"))
    tr.mark(state, "证据检索",
            "ok" if has_evidence else "empty",
            f"事实 {len(state.get('retrieved_facts') or [])} 条（本体归一 {bound}）· "
            f"记忆 {len(state.get('retrieved_memories') or [])} · "
            f"文档 {len(state.get('retrieved_documents') or [])}",
            facts=len(state.get("retrieved_facts") or []), entity_bound=bound)
    return state


def _reason(state: TwinState) -> TwinState:
    if state["evidence_sufficiency"] == "insufficient":
        state["reasoning_result"] = {
            "recommendation": "needs_info",
            "confidence": 0,
            "risk_level": "unknown",
            "reasoning": ["证据不足"],
            "missing_evidence": ["个人历史决策", "相关项目资料"],
        }
        tr.mark(state, "决策分析", "blocked", "证据不足，建议补充材料")
        return state

    docs = state.get("retrieved_documents") or []
    memories = state.get("retrieved_memories") or []
    facts = state.get("retrieved_facts") or []
    # 证据充分性把 facts 也算进去，这里必须同步拼给模型，否则会出现
    # 「判定有证据、Prompt 里却（无）」的自相矛盾。
    evidence = "\n".join(
        [f"[E{i}] 文档：{str(d.get('content'))[:400]}" for i, d in
         enumerate(docs, 1)]
        + [f"[E{i}] 记忆：{str(m.get('content'))[:200]}" for i, m in
           enumerate(memories, len(docs) + 1)]
        + [f"[E{i}] 事实：{f.get('predicate')} = {f.get('object_value')}" for i, f in
           enumerate(facts, len(docs) + len(memories) + 1)]
    )
    try:
        outcome = llm_client.chat_json(
            DECISION_SYSTEM,
            f"业务上下文：{json.dumps(state.get('business_context') or {}, ensure_ascii=False)}\n"
            f"认知画像：{json.dumps(state.get('cognitive_profile') or {}, ensure_ascii=False)}\n"
            f"证据：\n{evidence or '（无）'}",
        )
    except Exception:  # noqa: BLE001  LLM 未启动 → 降级为 needs_info
        outcome = {
            "recommendation": "needs_info", "confidence": 0,
            "risk_level": "unknown",
            "reasoning": ["LLM 推理服务未启动，仅完成证据检索与规则检查"],
        }
    if not isinstance(outcome, dict) or "recommendation" not in outcome:
        outcome = {"recommendation": "needs_info", "reasoning": ["模型输出解析失败"]}
    state["reasoning_result"] = outcome
    tr.mark(state, "决策分析",
            "degraded" if outcome.get("confidence") == 0 else "ok",
            str((outcome.get("reasoning") or [""])[0])[:80])
    return state


def _policy_check(state: TwinState) -> TwinState:
    ctx = state.get("business_context") or {}
    risk = (state.get("reasoning_result") or {}).get("risk_level", "medium")
    # 确定性规则优先于模型判断
    for rule in _HARD_RULES:
        if rule["condition"](ctx):
            state["policy_result"] = {"policy_result": rule["verdict"],
                                      "reason": rule["reason"],
                                      "execution_authorized": False}
            break
    else:
        action = ctx.get("action")  # MVP 传 None → 恒走人工审批
        state["policy_result"] = policy.evaluate_decision(risk, action)
    pol = state.get("policy_result") or {}
    tr.mark(state, "策略检查", "blocked" if pol.get("policy_result") != "allowed" else "ok",
            str(pol.get("reason") or pol.get("policy_result") or "")[:80])
    return state


def _finalize(state: TwinState) -> TwinState:
    reasoning = state.get("reasoning_result") or {}
    policy_result = state.get("policy_result") or {}
    state["approval_required"] = policy_result.get("policy_result") != "allowed"
    state["execution_authorized"] = bool(policy_result.get("execution_authorized", False))
    state["recommendation"] = {
        "recommendation": reasoning.get("recommendation"),
        "confidence": reasoning.get("confidence"),
        "risk_level": reasoning.get("risk_level"),
        "reasoning": reasoning.get("reasoning", []),
        "missing_evidence": reasoning.get("missing_evidence", []),
        "requires_human_approval": state["approval_required"],
        "execution_authorized": state["execution_authorized"],
    }
    state["final_response"] = state["recommendation"]
    tr.mark(state, "落库审批", "pending",
            f"approval_required={state['approval_required']}（决策与快照待审批流推进）")
    return state


def build_decision_graph():
    g = StateGraph(TwinState)
    g.add_node("analyze", _analyze)
    g.add_node("reason", _reason)
    g.add_node("policy_check", _policy_check)
    g.add_node("finalize", _finalize)
    g.set_entry_point("analyze")
    g.add_edge("analyze", "reason")
    g.add_edge("reason", "policy_check")
    g.add_edge("policy_check", "finalize")
    g.add_edge("finalize", END)
    return g.compile()
