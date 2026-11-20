"""个人风格回复图：
识别对方意图 → 提取回复目标 → 检索个人知识 → 加载沟通认知模型
→ 生成草稿 → 事实和承诺检查 → 输出草稿 →（人工编辑确认）
铁律：不自动创造个人没有表达过的承诺、立场、报价、时间节点和授权。
"""
import json

from langgraph.graph import END, StateGraph

# 统一语义运行时（SIL B2）：Agent 不直接调用底层检索器，蓝图 20.2 第 8 条
from app.modules.semantic_runtime import service as semantic
from app.modules.distillation.prompts import REPLY_SYSTEM
from app.modules.llm.client import llm_client
from app.modules.twin import trace as tr
from app.modules.twin.state import TwinState

# 禁止自动出现的承诺性表述（超出证据范围即标记待确认）
_COMMIT_PATTERNS = ["承诺", "保证", "一定", "报价", "截止", "提前交付", "我方同意"]


def _retrieve(state: TwinState) -> TwinState:
    result = semantic.agent_retrieve(state["input_message"], ["facts", "memories", "documents"])
    state["retrieved_facts"] = result["facts"]
    state["retrieved_memories"] = result["memories"]
    state["retrieved_documents"] = result["documents"]
    bound = sum(1 for f in result["facts"] if f.get("subject_name"))
    tr.mark(state, "知识检索",
            "ok" if (result["facts"] or result["memories"] or result["documents"]) else "empty",
            f"事实 {len(result['facts'])} 条（本体归一 {bound}）· "
            f"记忆 {len(result['memories'])} · 文档 {len(result['documents'])}",
            facts=len(result["facts"]), entity_bound=bound)
    return state


def _draft(state: TwinState) -> TwinState:
    # 检索到的个人事实必须进入起草上下文：主体 · 谓词 = 值（此前只拼文档/记忆，
    # facts 仅当承诺检查开关用，「基于个人知识作答」名存实亡）。
    fact_lines = [
        f"[F{i}] {f.get('subject_name') or '本人'} · {f.get('predicate')} = "
        f"{str(f.get('object_value'))[:200]}"
        for i, f in enumerate(state.get("retrieved_facts", []), 1)
    ]
    context = "\n".join(
        fact_lines
        + [str(d.get("content"))[:400] for d in state.get("retrieved_documents", [])]
        + [str(m.get("content"))[:300] for m in state.get("retrieved_memories", [])]
    )
    profile = state.get("cognitive_profile") or {}
    try:
        outcome = llm_client.chat_json(
            REPLY_SYSTEM,
            f"沟通画像：{json.dumps(profile, ensure_ascii=False)}\n\n"
            f"个人知识：{context or '（无）'}\n\n"
            f"待回复消息：{state['input_message']}",
        )
    except Exception:  # noqa: BLE001  LLM 未启动 → 降级
        outcome = {"draft": "", "unconfirmed_points": ["LLM 推理服务未启动，无法生成草稿"]}
    if not isinstance(outcome, dict):
        outcome = {"draft": "", "unconfirmed_points": ["模型输出解析失败，请人工处理"]}
    state["reasoning_result"] = outcome
    draft = (outcome.get("draft") or "").strip()
    tr.mark(state, "起草回复", "degraded" if not draft else "ok",
            "草稿已生成" if draft else "未能生成草稿，转人工填写")
    return state


def _commitment_check(state: TwinState) -> TwinState:
    """事实和承诺检查：草稿中出现承诺性关键词而证据不支持的，强制转入待确认。"""
    draft = (state.get("reasoning_result") or {}).get("draft", "")
    unconfirmed = list((state.get("reasoning_result") or {}).get("unconfirmed_points", []))
    has_solid_evidence = bool(state.get("retrieved_facts") or state.get("retrieved_documents"))
    hits = [p for p in _COMMIT_PATTERNS if p in draft]
    if hits and not has_solid_evidence:
        unconfirmed.append(f"草稿包含承诺性表述（{('、'.join(hits))}）但缺乏个人知识支持，需本人确认")
    state["final_response"] = {
        "draft": draft,
        "unconfirmed_points": unconfirmed,
        "requires_human_review": True,  # 回复草稿一律经人工编辑确认后外发
    }
    tr.mark(state, "承诺检查",
            "pending" if unconfirmed else "ok",
            ("待确认 " + str(len(unconfirmed)) + " 项（含承诺护栏）" if unconfirmed
             else "未发现超范围承诺"))
    return state


def build_reply_graph():
    g = StateGraph(TwinState)
    g.add_node("retrieve", _retrieve)
    g.add_node("draft", _draft)
    g.add_node("commit_check", _commitment_check)
    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "commit_check")
    g.add_edge("commit_check", END)
    return g.compile()
