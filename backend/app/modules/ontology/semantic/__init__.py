"""Ontology 语义层增强包（V1.3 语义层方案 · Phase A/B/C 最小闭环）。

在现有 Graph、CQ V2.0、版本机制之上增强，不推倒重来：
  schema_def   Schema-as-Code（Pydantic + JSON Schema，Registry 派生增强层）
  constraints  写入侧约束强制（继承感知类型/必填/枚举/值域/基数）
  reasoning    轻量推理（继承/对称/互逆/有限传递，衍生候选）
  rules        业务规则（condition→action，只产证据）
  versioning   任务快照冻结 ontology_version
  cq_executor  CQ execute（服务端上下文 + 幂等 + 持久化 + 决策 Trace）
  regression   CQ 回归集（结果稳定可重复）
  alignment    实例↔Fact 双向追溯 + 一致性
  metrics      可评估（约束违反率/类型合规率/回归稳定/一致性）
  service      OntologyService 统一写入口 + ensure_ready
  store        PostgreSQL 权威存储（幂等 DDL）

设计铁律：全新增量、零改动既有模块行为；规则/推理产物不改写权威 Fact；
缺口（OWL 推理机 / Alembic / 真实 IAM / 路径正确率标注集）照实披露。
"""
from .service import OntologyService, ensure_ready, load_schema

__all__ = ["OntologyService", "ensure_ready", "load_schema"]
