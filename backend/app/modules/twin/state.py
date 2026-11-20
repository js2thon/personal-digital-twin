"""LangGraph 统一状态对象（蓝图 8.3）。"""
from typing import Any, TypedDict


class TwinState(TypedDict, total=False):
    request_id: str
    user_id: str
    task_type: str                     # knowledge_qa / communication / decision / planning / cognitive
    input_message: str
    business_context: dict
    retrieval_scope: list[str]
    retrieved_documents: list[dict]
    retrieved_facts: list[dict]
    retrieved_memories: list[dict]
    cognitive_profile: dict
    evidence_sufficiency: str          # sufficient / insufficient / supplemented
    evidence_count: int
    retrieval_diagnostics: dict
    reasoning_result: dict
    policy_result: dict
    recommendation: dict
    approval_required: bool
    execution_authorized: bool
    final_response: dict
    trace: list[dict]                  # 链路追踪：各节点 状态/说明/耗时，前端逐步点亮
