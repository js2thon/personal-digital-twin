# Architecture Decision Records（摘要）

## ADR-001 权威事实源
PostgreSQL 是个人知识、认知、规则和决策记录的唯一权威存储；RAGFlow、向量索引和未来的 Neo4j 均为派生服务，可随时重建。

## ADR-002 模块化单体
Python 模块化单体 + Monorepo，MVP 阶段不拆微服务。模块边界以 Blueprint 清单为准，依赖方向：twin → knowledge/memory/cognition → distillation → ingestion。

## ADR-003 Claim / Fact 分离
Claim 是模型提取的候选主张，Fact 是经规则校验或人工确认后发布的知识。禁止把所有 Claim 自动转为 Fact。

## ADR-004 蒸馏流水线异步化
蒸馏为 Celery 异步任务状态机（14 状态），Outbox 事件分发，幂等键 = hash(document_id + content_hash + pipeline_version + model_version + prompt_version)。版本升级重新蒸馏生成新版本，不覆盖旧版本。

## ADR-005 认知模型版本快照
Cognitive Profile 采用版本快照（cognition.profile_versions），每次更新生成新版本，可回滚、比较、审查。

## ADR-006 决策权限分级
四级权限 D0 分析 / D1 建议 / D2 人工审批 / D3 自动执行。MVP 固定 D2：是否允许自动执行 = 否。未来开放 D3 时按具体动作在 workflow.action_authorizations 逐项授权，并执行前重新验证。

## ADR-007 RAGFlow 对接边界
RAGFlow 固定 v1.0.0 官方镜像，仅通过 HTTP API Adapter 交互；不修改其内部数据库，不混用其内部队列与自建 Redis。

## ADR-008 编排权归属
LangGraph 是数字分身整体任务编排器，RAGFlow 只提供知识处理与检索能力，避免两个 Agent 系统争夺流程控制权。
