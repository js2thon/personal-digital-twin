"""个人知识问答图：
意图识别 → 多路检索 → 证据充分性检查 →（不足→扩大补充检索）
        → LLM 生成 → 引用验证 → 返回回答

铁律：检索不到与问题相关的证据时明确拒答，不用「最近写入的几条数据」凑数。
"""
import re

from langgraph.graph import END, StateGraph

# 统一语义运行时（SIL B2）：Agent 不直接调用底层检索器，蓝图 20.2 第 8 条
from app.modules.semantic_runtime import service as semantic
from app.modules.distillation.prompts import QA_SYSTEM
from app.modules.llm.client import llm_client
from app.modules.twin import trace as tr
from app.modules.twin.state import TwinState


def _intent(state: TwinState) -> TwinState:
    msg = state["input_message"]
    # 调用方显式传入的 retrieval_scope 优先（API 参数此前被这里无条件覆盖而失效）；
    # 未传入时才按关键词推断。
    scope = state.get("retrieval_scope")
    if not scope:
        scope = ["facts", "memories", "documents"]
        if re.search(r"偏好|倾向|喜欢|倾向于", msg):
            scope = ["memories", "preferences"]
        elif re.search(r"文档|报告|资料|说过", msg):
            scope = ["documents", "facts"]
    state["retrieval_scope"] = scope
    tr.mark(state, "意图识别", "ok", "检索范围：" + "、".join(scope))
    return state


def _assign(state: TwinState, result: dict) -> None:
    state["retrieved_facts"] = result["facts"]
    state["retrieved_memories"] = result["memories"]
    state["retrieved_documents"] = result["documents"]


def _retrieve(state: TwinState) -> TwinState:
    result = semantic.agent_retrieve(
        state["input_message"], state.get("retrieval_scope"), top_k=6
    )
    _assign(state, result)
    state["retrieval_diagnostics"] = result.get("diagnostics")
    # 判定必须落在这个节点里：LangGraph 的条件边函数只决定路由，它对 state 的写入
    # 不会回传，只在条件边里赋值会让后续节点 KeyError（曾导致命中文档时问答 500）。
    state["evidence_sufficiency"] = "sufficient" if (
        result["facts"] or result["memories"] or result["documents"]
    ) else "insufficient"
    bound = sum(1 for f in result["facts"] if f.get("subject_name"))
    tr.mark(state, "知识检索",
            "ok" if state["evidence_sufficiency"] == "sufficient" else "empty",
            f"事实 {len(result['facts'])} 条（本体归一 {bound}）· "
            f"记忆 {len(result['memories'])} · 文档 {len(result['documents'])}",
            facts=len(result["facts"]), entity_bound=bound,
            memories=len(result["memories"]), documents=len(result["documents"]))
    return state


def _ontology_report(state: TwinState) -> dict:
    """本次问答「本体层参与度」：给页面和 curl 一个可核对的事实清单。

    判据：命中事实里带 subject_name 的条数——这个名字只有 join ontology.entities
    才拿得到，所以 entity_bound > 0 就证明本体层真的参与了召回。
    """
    facts = state.get("retrieved_facts") or []
    entities = sorted({f.get("subject_name") for f in facts if f.get("subject_name")})
    dims: dict[str, int] = {}
    for f in facts:
        p = f.get("predicate") or "(未命名维度)"
        dims[p] = dims.get(p, 0) + 1
    # mention 明细：detect_mentions 现在带实体 id/类型/别名，供链路详情页回放
    mention_entities = []
    for m in (state.get("retrieval_diagnostics") or {}).get("mention_entities") or []:
        if isinstance(m, dict):
            mention_entities.append({
                "name": m.get("term"), "entity_id": str(m.get("entity_id") or "") or None,
                "entity_type": m.get("entity_type"), "aliases": m.get("aliases") or []})
    return {
        "facts": [
            {"subject": f.get("subject_name"), "predicate": f.get("predicate"),
             "value": f.get("object_value"),
             "subject_id": str(f.get("subject_id") or "") or None,
             "fact_id": str(f.get("fact_id") or "") or None,
             "trust_tier": f.get("trust_tier"), "confidence": f.get("confidence"),
             "valid_from": str(f.get("valid_from") or "")[:19],
             "valid_to": str(f.get("valid_to") or "")[:19] or None,
             "relevance": round(float(f.get("relevance") or 0), 2)
                          if f.get("relevance") is not None else None}
            for f in facts
        ],
        "fact_count": len(facts),
        "entity_bound": sum(1 for f in facts if f.get("subject_name")),
        "entities": entities,
        "mention_entities": mention_entities,
        "dimensions": dims,
        "memory_count": len(state.get("retrieved_memories") or []),
        "document_count": len(state.get("retrieved_documents") or []),
        "terms": (state.get("retrieval_diagnostics") or {}).get("terms", []),
        "mentions": (state.get("retrieval_diagnostics") or {}).get("mentions", []),
        "scope": state.get("retrieval_scope"),
        "sufficiency": state.get("evidence_sufficiency"),
        "supplemented": state.get("evidence_sufficiency") == "supplemented",
        "ontology_used": any(f.get("subject_name") for f in facts),
    }


def _check_sufficiency(state: TwinState) -> str:
    """只做路由：判定结果由 _retrieve 节点写入（条件边的写入不回传 state）。"""
    return state.get("evidence_sufficiency") or "insufficient"


def _merge_unique(old: list, new: list, key: str = "id") -> list:
    merged = list(old or [])
    seen = {str(x.get(key)) for x in merged if isinstance(x, dict)}
    for item in new or []:
        if isinstance(item, dict) and str(item.get(key)) not in seen:
            seen.add(str(item.get(key)))
            merged.append(item)
    return merged


def _supplement(state: TwinState) -> TwinState:
    """首轮无相关证据时扩大检索范围再试一次；仍无证据则保持拒答。"""
    wider = semantic.agent_retrieve(state["input_message"], ["all"], top_k=12)
    _assign(state, {
        "facts": _merge_unique(state.get("retrieved_facts"), wider["facts"]),
        "memories": _merge_unique(state.get("retrieved_memories"), wider["memories"]),
        "documents": _merge_unique(state.get("retrieved_documents"), wider["documents"]),
    })
    still_empty = not (
        state.get("retrieved_facts") or state.get("retrieved_memories")
        or state.get("retrieved_documents")
    )
    state["evidence_sufficiency"] = "insufficient" if still_empty else "supplemented"
    state["retrieval_diagnostics"] = wider.get("diagnostics")
    tr.mark(state, "补充检索", "empty" if still_empty else "ok",
            ("扩大范围后仍无证据" if still_empty
             else f"补充后事实 {len(state.get('retrieved_facts') or [])} 条"))
    return state


def _generate(state: TwinState) -> TwinState:
    facts = state.get("retrieved_facts") or []
    memories = state.get("retrieved_memories") or []
    docs = state.get("retrieved_documents") or []
    profile = state.get("cognitive_profile") or {}

    evidence_blocks = [
        f"[E{i}] {d.get('content', '')[:500]}"
        for i, d in enumerate(docs, 1)
    ] + [
        f"[E{i}] {str(m.get('content'))[:300]}（记忆：{m.get('memory_type')}）"
        for i, m in enumerate(memories, len(docs) + 1)
    ] + [
        f"[E{i}] 事实：{f.get('subject_name') or '本人'} · {f['predicate']} = {f.get('object_value')}"
        for i, f in enumerate(facts, len(docs) + len(memories) + 1)
    ]
    state["evidence_count"] = len(evidence_blocks)
    if state.get("evidence_sufficiency") == "insufficient" or not evidence_blocks:
        state["final_response"] = {
            "answer": "证据不足，无法基于个人知识回答这个问题。"
                      "请补充相关资料后再试。",
            "citations": [],
            "evidence_sufficiency": "insufficient",
            "retrieval_terms": (state.get("retrieval_diagnostics") or {}).get("terms", []),
            "retrieval": _ontology_report(state),
        }
        tr.mark(state, "生成回答", "blocked", "证据不足，按铁律拒答")
        return state

    user_prompt = (
        f"个人认知画像：{profile}\n\n"
        f"个人知识证据：\n" + "\n".join(evidence_blocks) +
        f"\n\n问题：{state['input_message']}"
    )
    try:
        answer = llm_client.chat(QA_SYSTEM, user_prompt)
    except Exception:  # noqa: BLE001  LLM 未启动 → 返回证据摘要（降级模式）
        snippets = [
            f"[E{i}] {d.get('content', '')[:200]}" for i, d in enumerate(docs, 1)
        ] + [
            f"[E{i}] 记忆：{str(m.get('content'))[:200]}"
            for i, m in enumerate(memories, len(docs) + 1)
        ]
        state["final_response"] = {
            "answer": "（LLM 推理服务未启动，以下为检索到的原始证据）\n"
                      + "\n".join(snippets),
            "citations": [],
            "evidence_sufficiency": state.get("evidence_sufficiency") or "sufficient",
            "degraded": True,
            "retrieval": _ontology_report(state),
        }
        tr.mark(state, "生成回答", "degraded",
                f"LLM 不可用，返回原始证据摘要（{len(snippets)} 条证据）")
        return state
    state["final_response"] = {
        "answer": answer,
        "citations": re.findall(r"\[E(\d+)\]", answer),
        "evidence_sufficiency": state.get("evidence_sufficiency") or "sufficient",
        "retrieval": _ontology_report(state),
    }
    tr.mark(state, "生成回答", "ok", f"{state.get('evidence_count', 0)} 条证据进入生成")
    return state


def _verify_citations(state: TwinState) -> TwinState:
    """引用验证：剔除指向不存在证据的引用编号，避免模型编造出处。"""
    resp = state.get("final_response") or {}
    if resp.get("degraded") or resp.get("evidence_sufficiency") == "insufficient":
        return state
    total = int(state.get("evidence_count") or 0)
    raw = [int(c) for c in resp.get("citations", []) if str(c).isdigit()]
    valid = sorted({c for c in raw if 1 <= c <= total})
    resp["citations"] = valid
    if raw and len(valid) != len(set(raw)):
        resp["citation_warning"] = "已剔除超出证据范围的引用编号"
    elif not raw and total:
        resp["citation_warning"] = "回答未标注证据编号，请核对原文"
    state["final_response"] = resp
    tr.mark(state, "引用校验",
            "warn" if resp.get("citation_warning") else "ok",
            resp.get("citation_warning") or f"引用 {len(resp.get('citations') or [])} 处")
    return state


def build_qa_graph():
    g = StateGraph(TwinState)
    g.add_node("intent", _intent)
    g.add_node("retrieve", _retrieve)
    g.add_node("supplement", _supplement)
    g.add_node("generate", _generate)
    g.add_node("verify", _verify_citations)
    g.set_entry_point("intent")
    g.add_edge("intent", "retrieve")
    g.add_conditional_edges(
        "retrieve", _check_sufficiency,
        {"insufficient": "supplement", "sufficient": "generate"},
    )
    g.add_edge("supplement", "generate")
    g.add_edge("generate", "verify")
    g.add_edge("verify", END)
    return g.compile()
