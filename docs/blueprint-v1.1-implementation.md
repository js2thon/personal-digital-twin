# 蓝图 V1.0 实施状态对照（V1.1）

> 本文是《个人知识数字分身工程实施蓝图 V1.0》的**实施状态版**。
> 原文逐字归档于 [`blueprint-v1.0.txt`](./blueprint-v1.0.txt)（来源：本人提供的 `个人知识数字分身工程实施蓝图V.txt`），永不修改；
> 本文按原文顺序逐节标注现状，差异与理由写在「附录 A」，冻结项判据写在「附录 B」。
>
> 维护规则：每次迭代结束更新状态列，**不允许只改 README 不改本文**。
> 最近更新：2026-11-12 17:40（第十六批：知识图谱 + LinkML 决策）

## 状态图例

| 标记 | 含义 |
|---|---|
| ✅ | 已实现，且与蓝图要求一致 |
| ⚠️ | 已实现但降级 / 仅部分覆盖（写明差在哪） |
| ❗ | 实现方式与蓝图不同，但原则未被绕过（写明替代方案） |
| ❌ | 未实现 |
| ⏸ | 蓝图第十五节明确冻结，当前不引入 |

---

## 一、总体原则与架构决策

| 蓝图决策 | 状态 | 现状 |
|---|---|---|
| 数据接入：多渠道 Connector 插件化 | ⚠️ | 有 DWS 四类只读接入 + 文件上传 + 结构化旁路；插件化靠 `dws_source/streams.py` 分派，无通用 Connector 注册表 |
| 部署：完全私有化 | ⚠️ | 数据与存储全本地；**LLM 走企业 OneAPI 网关**，非本地推理（vLLM 未启） |
| 后端：Python 模块化单体 | ✅ | FastAPI + 16 个模块目录 |
| 知识主存储：PostgreSQL + pgvector | ⚠️ | PG 已用；**pgvector 未启用**（`embedding` 为 TEXT，另加 `embedding_vec TEXT`），281 切片仅 45 条有向量，无 HNSW/IVFFlat 索引 |
| RAG 基础设施：RAGFlow 1.0 | ❌ | 仅有 `config.ragflow_base_url / ragflow_api_key` 两个配置项，**无客户端、无调用**；PDF/Office 由 MinerU/docling/pypdf 适配层替代 |
| Agent 编排：LangGraph | ⚠️ | 三张图在用；**无 Checkpointer、无 interrupt/resume** |
| 本体定义：LinkML + Pydantic | ⚠️ | Pydantic ✅；**LinkML 零引用**，本体用 DDL + `ontology_versions.schema_content`(JSONB) 表达 |
| 知识图谱：PG 关系模型，预留 Neo4j | ⏸ | PG 实现（94 实体 / 21 关系）；Neo4j 冻结 |
| 异步处理：Celery + Redis | ⚠️ | 代码齐（14 状态机 + `pipeline_steps`）；本机 `CELERY_TASK_ALWAYS_EAGER=1` 同步执行，Redis 未运行，无独立 worker 进程 |
| 原始文件：MinIO | ⚠️ | 适配 + 无凭据降级内联 DB（超 20 万字符截断并打 `_inline_truncated` 标记）；本机未运行 MinIO |
| 决策权限：建议 → 人工确认 → 分级授权 | ✅ | D0~D3，D2 固定；D3 恒否 |
| **唯一权威事实源** | ✅ | `knowledge.facts` 是唯一收口；检索/画像/问答全部读它，无第二写路径 |

## 二、系统架构与三安全域

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 五子系统（接入蒸馏 / 知识本体 / 认知记忆 / 智能引擎 / 治理） | ✅ | 控制台与模块划分一致 |
| 三安全域（数据域 / AI 域 / 应用域） | ⚠️ | `docker-compose.yml` 按三域编排；**本机无 Docker，从未验证过**，实际是单机裸进程 |
| 外部源按授权接入 | ✅ | DWS 只读 + 显式导入；无爬取 |

## 三、模块清单

| 蓝图模块 | 优先级 | 状态 | 现状 |
|---|---|---|---|
| ingestion | P0 | ✅ | 上传 + 幂等去重 + MinIO/内联双路 |
| source_manager | P0 | ⚠️ | 并入 `source` schema + `dws_source`；无独立同步调度器（手动触发） |
| document_parser | P0 | ✅ | MinerU → opendataloader-pdf → docling → pypdf 四级降级 |
| distillation | P0 | ✅ | Claim/Evidence 抽取、实体消歧、Prompt 版本化、候选审核闸门 |
| knowledge | P0 | ⚠️ | 逻辑在 `api/v1/knowledge.py`，`modules/knowledge/` 仅占位 |
| ontology | P0 | ✅ | 双时间线、取代链、争议、画像、诊断、版本发布 |
| memory | P0 | ⚠️ | 逻辑在 api 层，`modules/memory/` 仅占位；639 条记忆 |
| cognition | P0 | ⚠️ | 版本快照 + 提案审批在 api 层，模块目录占位 |
| retrieval | P0 | ⚠️ | 中文词法（pg_trgm）+ 向量余弦重排；语义分支受 pgvector 缺失限制 |
| twin | P0 | ✅ | Orchestrator + 三图（问答/回复/决策） |
| decision | P1 | ✅ | 决策记录 + 证据 + 快照 + 审批任务 |
| workflow | P1 | ✅ | 审批、执行授权、执行审计 |
| policy | P0 | ✅ | 工具白名单 + D0~D3 + 覆盖度自检 |
| feedback | P1 | ⚠️ | `/cognition/feedback` + `audit.user_feedbacks` 有 API，**控制台无入口**，反馈不进三个队列 |
| **evaluation** | **P0** | ❌ | **完全没有**：无 QA/偏好/决策基准集，无评估模块、无指标看板 |
| audit | P0 | ✅ | HMAC 决策快照 + 访问日志 |
| （蓝图外）structured | — | ✅ | 新增旁路：契约 → 确定性断言 → 写事实，零 LLM |
| （蓝图外）messaging | — | ✅ | 企业 IM消息接管 + 草稿逐条确认 |

## 四、数据库设计

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 9 个 Schema 领域隔离 | ✅ | source / ingest / knowledge / ontology / memory / cognition / decision / workflow / audit 全部存在，共 34 张表 |
| 核心 DDL | ✅ | 蓝图列出的表全部落地，并补齐 `claim_evidence`、`decision_evidence`、`execution_audits`、`outbox_events`、`pipeline_runs/steps` |
| 实体合并不删除 | ✅ | `status='merged' + merged_into`，事实与关系整体迁移 |
| 认知版本快照 | ✅ | `cognition.profile_versions`，60 个版本，`UNIQUE(version)` + 咨询锁防并发 |
| 向量存储 pgvector | ⚠️ | 建表时用 TEXT 存向量，`CREATE EXTENSION vector` 从未执行 |
| 关联表替代 UUID 数组（生产建议） | ⚠️ | `claim_evidence` 已是关联表；`source_claim_ids` / `evidence_refs` 仍为 JSONB 数组 |
| 迁移脚本 | ❌ | 仅 `db/init/*.sql` 初始化 DDL，无 alembic；补列靠运行时 `ALTER ... IF NOT EXISTS` |

## 五、蒸馏状态机

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 14 状态主链 + 异常分支 | ✅ | PENDING→…→COMPLETED，RETRY_WAIT/FAILED/CANCELLED/NEEDS_REVIEW 全实现 |
| `pipeline_runs` + `pipeline_steps` 分阶段留痕 | ✅ | 40 次运行、逐步记录模型/Prompt 版本与耗时 |
| 幂等键（doc+content_hash+pipeline+model+prompt） | ✅ | 命中即 `duplicate`，不重复生成 |
| 不在 HTTP 请求里跑完整流程 | ⚠️ | 本机 eager 模式下仍在 API 进程内同步执行；生产需真 worker |
| RAGFlow 任务经 Adapter 轮询 | ❌ | 未接入 RAGFlow |
| NEEDS_REVIEW 人工闭环 | ✅ | 复核原因 + 处置入口 + SOP（`docs/needs-review-sop.md`） |

## 六、个人本体

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 基础六域、15~25 实体类型 | ✅ | 12 个在用类型（Project 37 / Person 25 / Organization 10 / System 6 / Event 4 / Component 3 / Concept 2 / Document 2 / Rule 2 / Skill 1 / Policy 1 / Process 1） |
| 新知识不得随意造新类型 | ✅ | 扩展走 `extension_proposals` + 人工审核 + 发布 |
| 本体自动扩展机制 | ✅ | Claim → 匹配 → 候选 → Schema 校验 → 冲突检查 → 人审 → 新版本 |
| 版本兼容性检查（删字段/改类型） | ✅ | 发布单事务内校验 + 旧版置 deprecated |
| 关系类型体系与图查询 | ⚠️→✅ | 2026-11-12 第十六批补齐：35 个关系类型目录（含源/目标约束）、k 跳子图 / 路径 / 质量统计 API、控制台力导向图；类型违规可检测（首测 7 条），但**约束目前只在读取侧标注，写入侧未强制** |
| LinkML Schema | ❌ | 未使用；决策与分阶段方案见本文件「LinkML 决策」一节 |
| 迁移 Neo4j 的触发条件（多跳/路径/图算法） | ⏸ | 未触发，冻结；图查询已在 PG 上实现（递归 CTE），触发条件更远了 |

## LinkML 决策（2026-11-12）

**结论：不引入 LinkML 运行时依赖；先建「本体单一来源 schema」，触发条件满足后再切 LinkML。**

- 蓝图把 LinkML 列为 MVP，但当前 12 个实体类型、35 个关系类型全部由代码常量 + DDL 表达，
  LinkML 的核心价值（多目标代码生成、跨系统交换、版本兼容自动校验）一个都还没被需要。
- 引入成本真实存在：新工具链（linkml-runtime/generator）、YAML schema 维护、94 个存量实体迁移、
  以及「schema 与 DDL 双真源」的漂移风险——比收益先到。
- 已落地的替代：关系类型目录（`modules/ontology/graph.py`）+ 类型违规检测，承担了 LinkML
  最急迫的那部分职责（让本体约束可执行、可观测）。
- **触发条件（满足任一即重评）**：① 实体类型 > 25 或关系类型 > 60；② 本体需要导出给第二个
  消费方（外部图库 / RAG / 数据交换）；③ 版本兼容性检查需要自动化到 schema 级；
  ④ 出现第二条写路径，需要统一的运行时校验。
- 届时的迁移路径：LinkML 只作**设计期单一来源**（YAML → 生成 Pydantic / DDL / 文档），
  运行时仍读 PG，不引入 linkml-runtime 依赖。

## 七、认知模型与反馈

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 五个子 Profile（知识/偏好/信念/决策/沟通） | ⚠️ | `profile` JSONB 内已分维度，但未拆成独立可更新子文档 |
| 候选 → 证据 → 一致性 → 用户确认 → 新版本 | ✅ | `profile_proposals` 全链路 |
| 偏好带适用上下文 `scope` | ⚠️ | 部分偏好有 scope 字段，未强制 |
| 同一材料重复转发不算独立证据 | ✅ | 证据按 `md5(content)` 去重（797 → 88 条） |
| 反馈分三类队列 | ⚠️ | 三类 `feedback_type` 已定义并落库；**无队列化处置**（不自动进知识修正/Profile Review/Decision Evaluation） |
| 不用在线强化学习改参数 | ✅ | 全部走可审计的数据更新 |

## 八、Agent 与决策

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 统一入口 + 任务路由 | ✅ | `/twin/chat` / `reply` / `decision`，按意图分派 |
| 问答图（检索→融合→充分性→生成→引用验证） | ✅ | 证据不足拒答、引用校验、降级分支 |
| 回复图（承诺检查器） | ✅ | 不擅自承诺/立场/报价/时间点 |
| 决策图（证据→规则→风险→权限→审批→执行校验） | ✅ | D2 固定、执行前重校验、HMAC 快照 |
| `TwinState` 字段 | ✅ | 与蓝图字段基本一致（含 `retrieval_scope`） |
| 规划 / 认知推理两条任务链 | ❌ | 蓝图 8.1 表格里的 Planning、Cognitive Reasoning 未实现 |
| Checkpointer 独立 PG 存储 | ❌ | 无 graph checkpointer；会话落 `workflow.twin_sessions` |
| 审批用 interrupt/resume | ❗ | 改为 `workflow.approval_tasks` + HTTP approve/reject，语义等价且不占用长连接 |
| D3 按动作逐项授权 | ⏸ | `workflow.action_authorizations` 表已建，逻辑恒否 |

## 九、API 契约

- 路由文件 12 个、端点 **97 个**；蓝图 9.1–9.5 列出的端点**除 SSE 外全部存在**。
- 唯一缺口：`GET /twin/tasks/{id}/stream`（SSE 进度流）——`GET /twin/tasks/{id}` 有，流式没有。
- 蓝图外新增：`/structured/*`（20 个）、`/documents/dws/*`（6 个）、`/messages/*`（8 个）、`/lineage/*`（2 个）、`/knowledge/maintenance/*`。
- 分页/错误码/幂等：分页已钳制（`max(1, min(limit, 100))` 等），错误统一 400/403/404/413/500。

## 十、部署与模型

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| twin-api / worker / web / postgres / redis / minio / ragflow / vllm / langfuse | ⚠️ | compose 定义齐全；实际运行只有 api(+console) 与 postgres，其余未启动 |
| RAGFlow 1.0 固定版本、不改内部库 | ⏸ | 未部署 |
| 三类模型服务（推理 / Embedding BGE-M3 / Reranker） | ⚠️ | 推理走 OneAPI；Embedding 部分可用（45/281 有向量）；**Reranker 模型未部署**（只有向量余弦重排） |
| 私有化验证八条 | ⚠️ | 数据/存储/日志本地 ✅；推理不在本地 ❌；无离线运行验证 ❌ |
| 供应链安全 | ❌ | 无镜像/依赖校验流程 |

## 十一、安全、权限、审计

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 数据访问权限分级 | ✅ | `app/visibility.py` 白名单 `owner / organization`，`restricted` 与未知值 fail-closed，检索期过滤而非生成后脱敏 |
| 数据使用范围标签（privacy_level / allowed_purposes） | ⚠️ | 只有 `visibility` 一维，无 purposes / external_sharing |
| 工具白名单（MCP 只是协议） | ✅ | 8 条策略 + `wired/implicit/declared` 覆盖度自检 |
| 决策快照不可篡改 | ✅ | HMAC-SHA256（密钥来自配置） |
| 审计可答九问 | ✅ | 输入/证据/认知版本/模型/Prompt/建议/确认人/是否执行/结果 |
| 越权与敏感信息泄露测试 | ⚠️ | 有可见性单测与链路校验；无系统化越权用例集 |

## 十二、10 周计划

功能面已覆盖第 1–9 周全部范围，且超出蓝图部分：结构化旁路、消息接管、链路观测、Astryx 设计系统。
第 10 周只完成决策引擎 / Policy / 审批 / 审计，**「完整评估测试」未做**。
未完成的是**第 1 周的工程底座在生产形态上的验证**（Docker、RAGFlow、vLLM、真实 worker）与**第 10 周的评估测试**。

## 十三、测试数据与评估体系

| 蓝图要求 | 目标 | 现状 |
|---|---|---|
| 个人历史对话 | 500–1000 条 | 639 条记忆、41 篇文档（来源结构不同，不可直接等同） |
| 文档 | 100–300 份 | 41 |
| 邮件/会议 | 100–300 条 | 未接入邮件；钉钉会议/日历只读可用 |
| QA 评估题 | 100–200 题 | **0** |
| 偏好判断题 | 50–100 题 | **0** |
| 历史决策案例 | 30–50 个 | 12 个真实决策，未整理为评估集 |
| 证据可追溯率 100% | — | ✅ 链路 8/8 |
| 关键事实抽取准确率 ≥95% | — | 未测（无标注集） |
| 实体消歧准确率 ≥95% | — | 未测 |
| 问答有依据 ≥95% | — | 未测 |
| 回复事实错误率 ≤2% | — | 未测 |
| 高风险越权执行 0 / 未经确认自动执行 0 | — | ✅ 结构上为 0（D3 恒否 + 外发开关默认关） |

**结论：评估体系是本项目相对蓝图最大的缺口**（蓝图把 evaluation 列为 P0）。

## 十四、界面

| 蓝图要求 | 状态 | 现状 |
|---|---|---|
| 六个核心工作区 | ✅ 超额 | 8 个：数字分身 / 知识生产 / 知识库 / 本体 / 认知 / 决策 / 消息 / 链路观测 |
| 命名 | ❗ | 按 2026-11-12 决策改名：蒸馏任务中心 → 知识生产；知识中心 → 知识库；其余去掉「中心」后缀 |
| 工作台徽章计数 | ✅ | 各模块待办徽章实时刷新 |

## 十五、冻结项（原文第 1216–1233 行）

原文表格（逐字）：

| 技术或能力 | MVP | 后续 |
|---|---|---|
| RAGFlow 1.0 | （MVP） | 持续升级 |
| FastAPI / LangGraph / PostgreSQL + pgvector / MinIO / Celery + Redis / LinkML | （MVP） | — |
| Neo4j | 暂缓 | 复杂图查询 |
| Qdrant | 暂缓 | 独立向量服务 |
| Temporal | 暂缓 | 复杂长流程 |
| OPA | 暂缓 | 复杂授权策略 |
| 模型微调 | 暂缓 | 有高质量样本后评估 |
| 自动决策执行 | 禁止 | 分场景授权 |

**现状**：六项暂缓 + 一项禁止，**全部未引入**，与蓝图一致。
唯一需要更正的表述：此前 README 写「全部预留接口」，实际代码中只有 D3 真预留（`workflow.action_authorizations` 表 + `policy/engine.py` D3 分支 + 执行前校验），其余五项**没有任何 adapter 代码**，只是架构允许后补。已在 README 更正。

判据见「附录 B」。

## 十六、启动顺序

蓝图建议 Sprint 1→4 顺序推进，实际执行是「主链路优先」：先把 数据→Claim→Fact→问答/回复/决策 打通，再回头补工程底座。
结果：功能闭环比蓝图预期快，**底座（Docker/RAGFlow/pgvector/worker/评估）滞后**。
后续排期应以「补底座」为主线，而不是继续加功能。

---

## 附录 A：与蓝图的实质偏差（按严重度）

| # | 偏差 | 严重度 | 说明 |
|---|---|---|---|
| A1 | **pgvector 未启用** | 高 | 蓝图把 `PostgreSQL + pgvector` 列为 MVP 与向量存储方案；当前 TEXT 列 + 45/281 向量，语义检索实际接近不可用 |
| A2 | **evaluation 模块缺失** | 高 | 蓝图列为 P0，且有明确的题量与指标；当前零实现，导致任何检索/Prompt 改动都无法判断退化 |
| A3 | **RAGFlow 未接入** | 中 | 蓝图 MVP 项；当前用自建解析适配层替代，效果可接受，但蓝图要求的「持续升级」路径不存在 |
| A4 | **Celery 未真异步** | 中 | 状态机与幂等齐备，但 eager 模式意味着长任务仍占 API 进程 |
| A5 | **LinkML 未使用** | 低 | 本体用 DDL + JSONB 表达；已用「关系类型目录 + 类型违规检测」承担最急迫的约束职责，切换 LinkML 的触发条件见上文「LinkML 决策」 |
| A6 | **SSE / Checkpointer / interrupt 缺失** | 低 | 蓝图 8.3 与 9.4 的实现细节；当前用会话表 + HTTP 审批等价替代，不影响安全语义 |
| A7 | **数据用途标签只有一维** | 低 | 蓝图要求 `privacy_level + allowed_purposes + external_sharing`，当前只有 `visibility` |

## 附录 B：冻结项解冻判据

任何一项解冻，必须同时满足「触发条件」与「回滚方案」，并由本人书面确认。

| 项 | 触发条件（量化） | 解冻要做的工程 | 回滚方式 |
|---|---|---|---|
| Neo4j | 关系边 > 10 万，或多跳查询 P95 > 2s | outbox 双写 + 全量图投影重建 + 一致性校验 | 关双写，读回落 PG |
| Qdrant | 切片 > 50 万，或需多向量/多模态 | 向量写管道 + 索引重建 + 召回融合调参 | 读回落 PG 余弦 |
| Temporal | 多机 worker、跨天补偿、定时编排 | 状态机迁移 + 幂等重放验证（风险最高，不建议动） | 保留 Celery 路径并行跑 |
| OPA | 多租户，或策略需外部审计方维护 | 策略抽 Rego + 决策日志 + 双侧一致性测试 | 回退 Python 常量 |
| 模型微调 | 标注样本 > 5k 且通用模型在评估集上稳定低于阈值 | 标注 → 训练 → 评估回归 → 灰度 | 切回通用模型（配置项） |
| D3 自动执行 | 某动作人工审批连续一致 ≥ N 次 + 可回滚 + 有额度上限 | 逐动作授权 + 执行前重校验 + 熔断 + 审计 | 授权表置 disabled |

**前置条件**：A2（评估体系）必须先建。没有基准指标，「是否该解冻」和「解冻后是否变好」都无法回答。
