"""本体建模包：把建模过程做成可运行工作流（画布 7 类设计节点 + 收尾区验证门禁）。

- node_schema  7 类节点 JSON Schema 契约（四层覆盖；Schema 映射内化到属性/类型）
- validator    完整验证引擎：结构/覆盖/一致/可执行四类检查
- workflow     执行引擎：拓扑排序 + dry-run + 发布门禁（按验证报告判定）
- sample       可直接运行的示例工作流（含模拟数据）
- assistant    设计助手：草稿摘要 + LLM 对话（端点不可用时如实降级，不编造）

实例化/验证/发布在收尾区，不属于画布节点；缺口（无真实数据源）如实标注 no-data，不编造指标。
"""
from . import node_schema, validator, workflow, assistant

__all__ = ["node_schema", "validator", "workflow", "assistant"]
