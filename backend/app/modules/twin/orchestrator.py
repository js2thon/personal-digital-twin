"""Twin Orchestrator：统一入口 + 意图路由（不采用多个独立 Agent 各自维护记忆）。"""
import re

from app.modules.twin.graphs.decision import build_decision_graph
from app.modules.twin.graphs.qa import build_qa_graph
from app.modules.twin.graphs.reply import build_reply_graph
from app.modules.twin.state import TwinState

_task_graphs = {
    "knowledge_qa": build_qa_graph,
    "communication": build_reply_graph,
    "decision": build_decision_graph,
}

# 任务路由表（蓝图 8.1）：意图关键词 → 任务处理链
_ROUTING_RULES = [
    (r"帮我回复|回复这封|回信", "communication"),
    (r"批准|审批|应该吗|要不要|是否同意|决策", "decision"),
    (r"帮我制定|计划|规划", "knowledge_qa"),
    (r"我(之前|过去|曾经)|我认为|我的(偏好|观点|看法|原则)", "knowledge_qa"),
]


def route_task(message: str) -> str:
    for pattern, task in _ROUTING_RULES:
        if re.search(pattern, message):
            return task
    return "knowledge_qa"


def run_task(task_type: str | None, initial_state: dict) -> dict:
    """执行任务处理链，返回 TwinState（含 final_response + trace 链路追踪）。"""
    from app.modules.twin import trace as tr

    task = task_type or route_task(initial_state.get("input_message", ""))
    initial_state.setdefault("task_type", task)
    tr.start(initial_state)
    # 认知模块已下线：不再注入 cognitive_profile
    initial_state.setdefault("cognitive_profile", {})
    graph = _task_graphs.get(task, build_qa_graph)()
    final: TwinState = graph.invoke(initial_state)
    final.setdefault("task_type", task)
    resp = dict(final.get("final_response") or {})
    resp.setdefault("trace", final.get("trace") or [])
    final["final_response"] = resp
    return dict(final)
