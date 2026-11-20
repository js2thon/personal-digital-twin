# 企业数字员工智能体平台 V1.2 开发计划（重规划）

> 依据：《企业数字员工智能体平台：总体优化与实施蓝图 V1.2》（原文归档 [`blueprint-v1.2.txt`](./blueprint-v1.2.txt)）
> 基线：V1.1 已交付代码与数据（逐节状态见 [`blueprint-v1.1-implementation.md`](./blueprint-v1.1-implementation.md)）
> 本文回答一个问题：**在"不推倒重来"的约束下，接下来 14 周按什么顺序做什么、每步的验收线在哪。**
> 版本日期：2026-11-12　维护规则：每次迭代结束更新「实际进度」列，不允许只改 README 不改本文。

---

## 0. 一句话结论

V1.1 的功能闭环跑得比底座快：**知识蒸馏、事实收口、本体版本、三张任务图、审批审计都已可用**，但 **evaluation 为零、pgvector 未启用、worker 是 eager、Docker 形态从未验证**。因此 V1.2 不再加 Agent 功能，而是按「**底座可靠 → 企业化建模 → 单岗位闭环 → 多岗位复制 → 受控自动化**」推进，前 5 周只解决"能不能重复部署、能不能判断退化、能不能不越权"这三件事。

---

## 1. 现状基线（实测，2026-11-12）

### 1.1 已建成、可直接复用（不要重写）

| 资产 | 规模/位置 | 复用方式 |
|---|---|---|
| 9 schema / 33 表 | `db/init/01~04*.sql` | 作为 Alembic baseline 0001 原样纳入 |
| Claim → 审核 → Fact 唯一收口 | `api/v1/knowledge.py` | 直接继承，仅补 tenant/space 列 |
| 蒸馏状态机（14 状态） | `workers/pipeline_tasks.py` | 阶段 0 从 eager 切真 worker，逻辑不动 |
| 文档解析四级降级 | `modules/document_parser/adapters.py` | 不动 |
| 钉钉四类只读接入（文档/消息/日历/会议） | `modules/dws_source/` | 阶段 1 包装成第一个标准 Connector |
| 三张 LangGraph 图（QA/回复/决策） | `modules/twin/graphs/` | 保留，阶段 2 增加任务链 |
| 权限 D0~D3 + 执行前重校验 | `modules/policy/engine.py` | 阶段 4 升级为动作级授权 |
| 决策快照哈希 + 访问日志 | `modules/audit/` | 不动，补 trace_id |
| 知识图谱 + 本体工作台（刚交付） | `modules/ontology/graph.py`、`workbench.py`、`web/index.html` | **转维护**，阶段 1 增加 space 过滤，不再作主线投入 |
| 测试基线 | `backend/tests/` 166 passed | 每次合入不得低于此数 |

### 1.2 硬缺口（本次规划要解决的）

| # | 缺口 | 实测证据 | 阻塞什么 |
|---|---|---|---|
| G-1 | evaluation 零实现 | 无 `modules/evaluation`、无表、无数据集、无指标 | 任何 Prompt/模型/检索改动都无法证伪退化；D3 不能开放 |
| G-2 | pgvector 未启用 | `semantic_chunks.embedding` 为 TEXT；281 切片仅 45 条有向量；**本机 `.runtime/pgsql/share/extension` 下无 `vector` 文件** | 语义检索接近不可用；岗位 QA 准确率无上限 |
| G-3 | 无迁移体系 | 无 `alembic/`；DDL 靠 `db/init` + 运行期 `ALTER` | 加 tenant 列这种改动无法安全下发 |
| G-4 | worker 是假异步 | `CELERY_TASK_ALWAYS_EAGER=1`，Redis 未运行 | API 承载长任务，任务失败不可重试 |
| G-5 | 无租户/组织/岗位/数字员工对象 | 只有"本人"单一主体 | 无法做岗位化，无法多 DE 隔离 |
| G-6 | MinIO/Docker/备份未验证 | compose 存在但从未跑通；备份无演练 | 不可交付给企业 |
| G-7 | 逻辑堆在 API 层 | `modules/knowledge|memory|cognition` 仅占位 | 岗位化后 API 会失控 |

### 1.3 冻结项维持不变

Neo4j / Qdrant / Temporal / OPA / 模型微调 **继续冻结**，新增一条原则：**评估体系是所有解冻与自动化授权的前置条件**。D3 默认关闭。

---

## 2. 相对 V1.1 计划的 6 处调整

| # | 调整 | 原因 |
|---|---|---|
| 1 | 主线从"补功能"改为"补底座" | 蓝图 §0.3、§13 明确判断 |
| 2 | **evaluation 提到第 1 周开工**，与 pgvector 并列 P0 | 没有评估就没有"变好"的定义，后续 4 个阶段全部要它做门禁 |
| 3 | pgvector 从"待办"变为**阶段 0 阻塞项** | 本机内嵌 PG 无 vector 扩展，必须先换运行时，否则回填脚本无处落 |
| 4 | 所有新表一律带 `tenant_id`，老表补列回填 | 蓝图 §11「MVP 单企业也要保留 tenant」；事后加列成本是现在加列的 5~10 倍 |
| 5 | 前端从 8 工作区扩到 10，首页改"数字员工运营"视角 | 蓝图 §12；首页现在是知识计数，无法回答"今天数字员工干了什么" |
| 6 | 图谱/本体工作台转维护态 | 已交付且够用；阶段 1 只需给实体加 `space_id` 过滤，不再投入新交互 |

---

## 3. 阶段总览

| 阶段 | 周期 | 周次 | 目标 | 出口门禁 | 人日估算 |
|---|---|---|---|---|---|
| **P0 工程补强** | 2 周 | W1–W2 | 可重复部署、可评估、可恢复 | G0 | 22 |
| **P1 企业领域模型与治理** | 3 周 | W3–W5 | 个人分身 → 岗位化领域模型 | G1 | 30 |
| **P2 单岗位端到端 MVP** | 4 周 | W6–W9 | 一个岗位跑通"问答→任务→决策→审批" | G2 | 40 |
| **P3 岗位复制与能力复用** | 5 周 | W10–W14 | 第二、三岗位靠配置上线 | G3 | 45 |
| **P4 分级自动执行** | 持续 | W15+ | 单动作、限额、可回滚的 D3 | G4 | 按动作增量 |

排期以 **2026-11-16 为 W1 起点**；P3 跨 2027 春节（2/6），已含 1 周效率缓冲，故 W14 落在 2/26 而非 2/19。

```
W1  W2 │ W3  W4  W5 │ W6  W7  W8  W9 │ W10 W11 W12 W13 W14 │ W15+
P0 底座 │ P1 领域模型 │ P2 单岗位 MVP   │ P3 复制与复用        │ P4 自动执行
  ▲G0        ▲G1           ▲G2              ▲G3                 ▲G4
```

**人力假设**：1 名后端为主（本文作者）、0.5 名前端、0.3 名测试/运维，业务侧每阶段投入 2~4 小时做验收。若只有 1 人全职，周期 ×1.4。

---

## 4. 阶段 0：工程补强（W1–W2）

**目标**：让系统从"开发态"变成"可重复部署、可评估、可恢复"。

### W0.1 Alembic 迁移体系（2 人日）

- `backend/alembic/`：`env.py` 读 `app.config` 的 DSN；`versions/0001_baseline.py` 用现有 `db/init/01~04*.sql` 生成（`op.execute` 原样执行，保证与现存库一致）。
- 现存库执行 `alembic stamp 0001`，不做实际变更；此后**禁止运行期 `ALTER TABLE`**（`config.py` 里 `ensure_columns()` 之类的自愈逻辑改为迁移脚本）。
- 验收：空库 `alembic upgrade head` 后表数与现存一致（33 表）；`alembic downgrade base` 可回滚。

### W0.2 pgvector 正式启用（5 人日，**关键路径**）

先决条件：本机内嵌 PG 无 `vector` 扩展，两条路二选一——

| 方案 | 做法 | 代价 | 建议 |
|---|---|---|---|
| A. 换 Docker 镜像 | `pgvector/pgvector:pg16`，数据卷迁到 `.runtime/pgdata-docker` | 需要 Docker Desktop；数据迁移一次 | **首选**，同时验证 G-6 |
| B. 装 Windows 扩展 | 编译/下载 `vector.dll` → `.runtime/pgsql/lib`，控制文件 → `share/extension` | 版本需匹配 PG 16.4，x64 构建 | 备选，若 Docker 不可用 |

切换后：

1. 迁移 `0002_pgvector`：`CREATE EXTENSION vector`；`semantic_chunks` 增 `embedding vector(1024)`、`embedding_model`、`embedding_version`；保留旧 TEXT 列一版以便对比，下一版删除。
2. 索引：`CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`（先小规模召回测试，若数据量 <10 万且写多读少，评估 IVFFlat）。
3. 回填脚本 `scripts/backfill_embeddings.py`：281 切片全量重算，写入 `embedding_model/version`；断点续跑；失败切片单独落表。
4. 检索改造 `modules/retrieval/`：向量召回改为 SQL 侧 `ORDER BY embedding <=> :q LIMIT k`，元数据过滤前置；保留词法召回，做 RRF 融合。
5. 验收：`SELECT count(*) FROM knowledge.semantic_chunks WHERE embedding IS NULL` = 0；同一 query 的 Recall@5 在固定 20 题上 ≥ 基线（基线由 W0.7 提供）。

### W0.3 真实异步 worker（3 人日）

- 起 Redis（本机或容器）；`CELERY_TASK_ALWAYS_EAGER=0`；`start_hidden.py` 增加 worker 守护进程（与 uvicorn 同款重启策略）。
- 补：`acks_late`、`task_time_limit`、`max_retries` + 指数退避、死信表 `ingest.dead_letters`、幂等键落库（现有 `hash(doc+content_hash+pipeline+model+prompt)` 已具备）。
- 验收：上传一份 50 页 PDF，API 在 300ms 内返回 `run_id`；kill worker 后任务不丢，重启后状态从 `RETRY_WAIT` 继续；同文件重复上传不产生第二条 run。

### W0.4 MinIO 与备份恢复（3 人日）

- MinIO 启用，`ingestion` 走对象存储；无凭据时才降级内联（现状逻辑保留）。
- `scripts/backup.ps1`：`pg_dump -Fc` + MinIO `mc mirror` + 审计表导出，落 `.runtime/backups/<date>/`；`scripts/restore.ps1` 一键恢复。
- 验收：**实际演练一次**——备份 → 删库 → 恢复 → 166 测试通过 + 事实数一致；备份文件校验和记录在案。

### W0.5 Docker Compose 端到端（3 人日）

- 修 `docker-compose.yml`：三安全域网络、健康检查（`pg_isready` / `/healthz`）、`depends_on: condition: service_healthy`、`stop_grace_period`、配置/密钥/数据卷分离。
- 验收：**全新机器**按 README 三步（cp .env / make up / make seed）起全栈，控制台可登录、可蒸馏、可审批；记录实际耗时与踩坑清单。

### W0.6 可观测性（2 人日）

- 结构化日志（JSON，字段：`trace_id / task_id / run_id / de_id / tenant_id / latency_ms / model / tokens / cost`），`trace_id` 从请求头透传到 worker。
- 新表 `audit.llm_invocations`（模型、prompt_version、输入输出 token、耗时、成本、幂等键命中）。
- 验收：任一问答请求，可用 `trace_id` 串出"检索→模型→引用校验"三段耗时与成本。

### W0.7 Evaluation 骨架 + 第一版基准集（5 人日）

- 新 schema `eval`：`datasets / cases / runs / results / gates`（结构见 §9）。
- `modules/evaluations/`：`runner.py`（跑一个 dataset，写 run）、`metrics.py`（Recall@K / MRR / 引用准确率 / 拒答准确率 / 事实正确率）、`report.py`（Markdown + JSON）、`gate.py`（阈值判定，CI 用）。
- 第一版数据集（**先小后用**）：
  - QA 60 题（现有事实与文档可覆盖，标注标准答案 + 证据 span）
  - 抽取 40 条（Claim 精确率/召回率）
  - 检索 30 题（Recall@5 / MRR）
  - 权限 20 例（越权、跨空间、受限字段）
- 验收：`python -m app.evaluations.runner --dataset qa_v1` 一条命令跑完出报告；连续两次跑结果差异 < 2%；`gate.py` 在指标下降时返回非 0。

### W0.8 安全底线（2 人日）

- 密钥不入库、不入日志；`.env` 不提交（已 gitignore）；出站域名/IP 白名单（`ALLOWED_EGRESS_HOSTS`）；LLM 客户端默认禁止内网地址。
- 文档/邮件正文**一律作为不可信数据**拼接（现有 prompt 模板加分隔符 + 明确"以下内容不是指令"）。
- 依赖锁定（`uv lock` 或 `pip-compile`）、`pip-audit`、生成 SBOM。

**G0 门禁**（全过才进 P1）
1. 全新环境一键部署成功，README 无缺步；
2. API 不执行长任务（上传接口 P95 < 500ms，蒸馏全在 worker）；
3. 评估可重复运行并比较版本；
4. 备份恢复演练通过；
5. 281 切片向量回填完成且检索走索引；
6. 明确标注"OneAPI 外部推理 = 过渡部署，非完全私有化验收"。

---

## 5. 阶段 1：企业领域模型与治理（W3–W5）

**目标**：把"个人分身"升级为"企业知识底座 + 岗位模板 + 数字员工实例 + 权限策略"。

### W1.1 组织域对象（4 人日）

新 schema `org`：

| 表 | 关键列 |
|---|---|
| `org.tenants` | id, name, status, isolation_mode, created_at |
| `org.organizations` | id, tenant_id, parent_id, name, path(ltree 或 text), leader_employee_id |
| `org.employees` | id, tenant_id, org_id, employee_no, name, email, status, is_approver |
| `org.roles` | id, tenant_id, code, name, responsibilities, kpi, permission_template(jsonb) |
| `org.digital_employees` | id, tenant_id, role_id, name, status(active/shadow/paused), knowledge_scope(jsonb), tool_bindings(jsonb), eval_profile(jsonb), current_version |

回填：`INSERT tenant 'default'`；现有"本人"实体 → 一条 `employees` + 一条 `digital_employees`（role = 暂定"知识助理"）。

### W1.2 知识空间与统一权限上下文（4 人日）

- 新 schema `space`：`knowledge_spaces`（企业公共/部门/项目/岗位/个人，含 `owner_org_id`、`classification`）、`space_members`（subject_type/subject_id/role）、`space_policies`（allowed_purposes、external_sharing、retention）。
- 中间件 `app/core/context.py`：解析 `tenant_id + user_id + org_scope + de_id + task_id`，塞进 `contextvars`；**所有仓储层查询强制带 `tenant_id` 与 `space_id`**，无上下文直接 400。
- 老表补列（迁移 0003）：`source.documents / knowledge.claims / evidence_spans / facts / semantic_chunks / ontology.entities / relations / memory.memory_items / cognition.profile_versions / decision.decision_records` 加 `tenant_id`（默认 default）、`knowledge_space_id`（默认企业公共）、`allowed_purposes text[]`、`classification`。
- 验收：用两个 space 各放一条事实，A space 的检索请求查不到 B；`/docs` 里每个列表接口都体现 space 过滤参数。

### W1.3 数据用途与外发策略（3 人日）

- 检索链路加"用途过滤"：`allowed_purposes && ARRAY[current_purpose]`；`external_sharing=false` 的内容在沟通链（回复/邮件）自动拦截并转人工。
- 评估 PostgreSQL RLS 可行性（PoC 半天，结论写 ADR）；不引入则以仓储层强制条件为准。

### W1.4 认知模型拆分（4 人日）

把单一 `profile` JSONB 演进为五个子 Profile（沿用现有 `cognition.profile_proposals → profile_versions` 提案/版本/回滚机制，不新造轮子）：

`knowledge / preference / belief / decision / communication`，另加 `org.role_profiles`（岗位标准，与个人认知物理隔离）。

- 提案 → 证据聚合 → 一致性校验 → 人工确认 → 新版本发布；**不允许在线强化学习直改生产**。
- 验收：一个 DE 的 `preference` 版本变更后，`role_profile` 不变；回滚可恢复到上一版本且问答行为差异可被评估集捕捉。

### W1.5 企业基础本体第一版（4 人日）

在现有 `ontology` 上扩展，不新建体系。目标 **15~25 个高频实体类型**，优先覆盖试点岗位：

| 域 | 实体 |
|---|---|
| 组织 | Organization, Department, Employee, Role, Position |
| 业务对象 | Customer, Supplier, Contract, Order, Project, Task |
| 流程规则 | Process, SOP, Policy, Approval |
| 知识资产 | Document, Fact, Evidence, Claim, Case, Skill |

- 现有 35 个关系类型目录（`modules/ontology/graph.py`）按域归族；新增关系必须带 `source/target` 类型约束，否则校验直接 error。
- 验收：新增一个实体类型走"扩展提案 → 审批 → 发布版本"全流程；图谱工作台按 space 过滤后仍可用。

### W1.6 Connector 统一契约（4 人日）

- `backend/app/connectors/`：`base.py` 定义 `validate_config / connect / list_changes / pull / normalize / get_permissions / health_check / checkpoint / disconnect`。
- 把 `dws_source/` 改造为第一个实现（文档/消息/日历/会议四个 stream 复用同一契约）。
- 每条数据携带：`tenant_id / source_id / source_type / source_object_id / owner / visibility / allowed_purposes / sync_cursor`。
- 验收：新增一个"本地目录 Connector"仅写一个类、不碰业务代码即可接入并出现在控制台。

### W1.7 岗位模板与能力包数据结构（4 人日）

新 schema `skill`：`role_templates`（岗位模板）、`skills`（能力定义，含 inputs/outputs JSON Schema、preconditions、steps、tools、risk_level、approval、evaluation_set）、`skill_versions`、`de_skill_bindings`。

- 模板 → 实例是"生成差异 → 跑回归 → 审批 → 灰度"，**不允许静默覆盖**。
- 验收：改模板一版，已部署实例状态不变，仅生成待审差异。

**G1 门禁**
1. 不同组织/空间数据不能越权检索（用 W0.7 的权限集跑，越权命中 = 0）；
2. 一条企业事实可被多个授权 DE 复用，且各自认知/权限/运行记录互不可见；
3. 岗位模板更新不覆盖线上实例；
4. 新增实体类型/Connector 均"配置化"完成，无业务代码分支。

---

## 6. 阶段 2：单岗位端到端 MVP（W6–W9）

**目标**：**只做一个岗位**，跑通"能力 → 任务 → 决策 → 审批 → 审计"完整链。

> 默认建议试点岗位：**交付/项目管理助理**（资料最丰富、流程稳定、结果可验证：周报、风险清单、里程碑偏差、客户答复草稿）。备选：IT 运维工单、财务发票审核。最终由 §12 决策 2 拍板。

### W2.1 岗位 Role Profile 书面化（2 人日）

职责 / 输入 / 输出 / SOP / KPI / 边界 / 升级路径 / 禁止事项，落 `org.role_profiles`，业务方签字确认。**没有这份文件不开工。**

### W2.2 3~5 个岗位能力（8 人日）

示例（按试点岗位取舍）：`project_status_report`（周报）、`risk_extraction`（风险识别）、`milestone_deviation`（里程碑偏差分析）、`customer_reply_draft`（客户答复草稿）、`meeting_action_followup`（行动项跟踪）。

每个能力必须含：输入输出 Schema、前置/后置条件、异常分支、工具依赖、超时/重试/幂等/补偿、风险等级、审批条件、评估样本。**缺任何一项不算交付。**

### W2.3 真实业务系统只读 Connector ×1~2（4 人日）

优先选有 API 的系统（钉钉项目/表格、Jira、ERP 查询接口）。只读，写入一律走 W2.4。

### W2.4 Tool Gateway（6 人日）

- `backend/app/policies/` + `backend/app/tools/`：四级分层 `Read / Draft / Write / ExternalSideEffect`。
- 统一入口：参数 Schema 校验 → 出站白名单 → 凭据托管（不落 Agent）→ 幂等键 → 调用 → 结果摘要入库 `tool.invocations`。
- Agent 永不持有数据库高权限凭据，永不直连内网任意地址。
- 验收：构造一次越权调用（未绑定工具 / 参数越界 / 内网地址）三种情形全部 fail-closed 并留审计。

### W2.5 统一任务状态机（6 人日）

`CREATED → CONTEXT_READY → PLANNED → WAITING_APPROVAL → RUNNING → VERIFYING → SUCCEEDED`
异常：`NEEDS_INPUT / NEEDS_REVIEW / REJECTED / RETRY_WAIT / FAILED / CANCELLED / COMPENSATING`

- 新表 `workflow.tasks` + `workflow.task_steps`；每步记录：输入摘要+引用、结构化输出、模型/Prompt/能力版本、工具参数与返回摘要、执行主体、审批记录、幂等键、重试次数、耗时、前后置状态。
- 审批沿用 `approval_tasks` + HTTP approve/reject，**不占长连接**；Checkpointer 仍不做（蓝图 §5.2 明确非第一优先级）。

### W2.6 一个可人工审批的写操作（4 人日）

建议：**提交周报/工单到目标系统**。D2 语义：生成草稿 → 人工确认 → 系统执行 → 结果核验。执行后必须回读校验。

### W2.7 Shadow Mode 与人工确认模式（3 人日）

`digital_employees.status` 支持 `shadow`：全链路真实运行，但**丢弃所有外部副作用**，只落"本应执行的动作"供比对。观察 1~2 周无异常后切 `active`（仍 D2）。

### W2.8 岗位评估集与运营看板（6 人日）

- 岗位专项集：QA 80 题、偏好/规范判断 50 题、决策案例 30 个、表单/报告字段标注 100 份、安全用例 20 个。
- 看板首页改为运营视角：在线 DE 数与状态、今日任务量/完成率/人工接管率、待审批与高风险任务、知识新鲜度与待审事实、评估分数与版本变化、工具异常/越权拦截/成本。

**G2 门禁**
1. 关键任务达到事先定义准确率（建议：周报事实错误率 ≤2%、风险召回 ≥80%、引用准确率 ≥95%）；
2. 所有结论可追溯到证据 span；
3. **未经确认的外部副作用 = 0**（Shadow 期间日志可证）；
4. 失败任务可恢复或安全终止；
5. 人工接管与反馈进入改进队列（不是丢在反馈表里）。

---

## 7. 阶段 3：岗位复制与能力复用（W10–W14）

| 工作包 | 内容 | 人日 |
|---|---|---|
| W3.1 第二、三岗位模板 | 选与试点岗位"共享知识多、动作差异小"的岗位，验证复用率 | 8 |
| W3.2 能力复用机制 | 能力被引用计数、兼容性检查（Schema 破坏性变更拦截）、版本约束 | 10 |
| W3.3 岗位间协作与交接 | 任务移交协议（含上下文包）、共享知识、隔离认知；不做自由协商多 Agent | 10 |
| W3.4 三元版本管理 | 模板版本 / 能力版本 / 实例版本 + 灰度（按任务比例）+ 一键回滚 | 8 |
| W3.5 统一评估基准 | 跨岗位公共集 + 岗位专项集；新岗位上线必须跑公共集 | 9 |

**G3 门禁**
1. 新岗位**靠模板 + 能力包配置**上线，代码改动 ≤ 1 个 Connector 适配；
2. 共享知识更新不破坏其他岗位权限边界（跑跨岗位越权集，命中 = 0）；
3. 能力包变更可回归、灰度、回滚，且回滚后评估分回到基线 ±2%。

---

## 8. 阶段 4：分级自动执行（W15+，持续）

**只对稳定、低风险、可回滚动作开放**，且按动作粒度授权：

- 授权模型：`tenant + digital_employee + tool + action + resource_scope + limit(额度/频率/时间窗) + validity_period`，可随时撤销。
- 执行前九项重校验（蓝图 §6.2 全部）：用户身份与组织关系、DE 身份、数据可见性与用途、工具白名单、动作级授权、额度/频率/时间/对象范围、风险规则、审批要求、状态重校验。任一不满足 **fail-closed**。
- 失败熔断（连续 N 次失败自动降级 D2）、人工抽检（按比例）、自动撤权、补偿与回滚脚本。
- **G4 门禁**：评估稳定跨越 10 个工作日、回滚演练成功、越权/注入/跨租户测试集全绿、独立安全验收签字。未过则 D3 保持关闭。

---

## 9. 评估体系落地细则

### 9.1 表结构（`eval` schema）

| 表 | 用途 |
|---|---|
| `eval.datasets` | name, version, layer(qa/retrieval/extraction/decision/security/ops), split(train/val/test), owner, created_at |
| `eval.cases` | dataset_id, input(jsonb), expected(jsonb), evidence_refs, scoring_rule, difficulty, risk_level |
| `eval.runs` | dataset_id, target(prompt/model/retrieval/profile 版本组合), started_at, finished_at, status |
| `eval.results` | run_id, case_id, output, score, metrics(jsonb), failure_reason |
| `eval.gates` | name, dataset_id, threshold, blocking(bool), applies_to(变更类型) |

### 9.2 指标（先做能自动判分的，再做人工评分）

| 层 | 本期必做指标 |
|---|---|
| 检索 | Recall@5、MRR、权限过滤正确率 |
| 抽取 | Claim 精确率/召回率、冲突识别率 |
| 问答 | 引用准确率、拒答准确率、事实正确率（抽样人工） |
| 决策 | 规则符合率、历史案例一致性（抽样人工） |
| 安全 | 越权命中数（必须为 0）、提示注入成功率 |
| 运营 | 任务完成率、人工接管率、P95 耗时、单任务成本 |

### 9.3 发布门禁（CI 化）

任何 Prompt / 模型 / 检索参数 / 岗位 Profile / 能力包 / 策略变更，合并前必须：单测 → 离线评估回归 → 安全与权限集 → 关键指标对比 → （高风险变更）人工抽样。评估降分则阻止合并。

---

## 10. 数据模型演进清单

| 动作 | 对象 | 说明 |
|---|---|---|
| 新增 schema | `org` `space` `skill` `eval` `tool` | 5 个，见 §5/§9 |
| 新增表 | ~22 张 | org(6)、space(3)、skill(4)、eval(5)、tool(3)、workflow.tasks/task_steps/dead_letters |
| 加列 | 10 张老表 | `tenant_id` `knowledge_space_id` `allowed_purposes` `classification` |
| 改类型 | `semantic_chunks.embedding` | TEXT → `vector(1024)`，保留旧列一版 |
| 迁移顺序 | 0001 baseline → 0002 pgvector → 0003 tenant/space 列 → 0004 org/space → 0005 skill → 0006 eval → 0007 tool/tasks | 每步可独立回滚 |

**回填策略**：默认 tenant = `default`，默认 space = `企业公共`，`classification = internal`；回填脚本幂等，可重跑。

---

## 11. 代码结构与前端演进

### 11.1 后端目录（目标形态，仍模块化单体）

```
backend/app/
├── api/v1/          # 仅协议适配、认证、参数校验（逐步从 api 下沉逻辑）
├── modules/         # 领域服务、用例编排
├── repositories/    # 数据库读写（强制带 tenant/space 上下文）
├── workers/         # Celery 任务
├── connectors/      # 外部系统适配器（统一契约）
├── policies/        # 授权、风险、执行规则
├── evaluations/     # 数据集、评测器、报告、门禁
└── tools/           # Tool Gateway 与工具实现
```

**下沉优先级**：`knowledge` → `memory` → `cognition`（这三个当前逻辑在 `api/v1/`，是 P1 期间最大的可维护性风险）。

### 11.2 控制台 8 → 10 工作区

| # | 工作区 | 来源 | 本期动作 |
|---|---|---|---|
| 1 | 数字员工 | 新增 | 创建/配置/启停/版本/运行状态 |
| 2 | 岗位与能力 | 新增 | 模板、SOP、能力包、能力评估 |
| 3 | 知识生产 | 现有 | 加失败任务视图、游标状态 |
| 4 | 企业知识库 | 现有 | 加 space/用途/敏感级筛选 |
| 5 | 企业本体 | 现有（含工作台） | 加 space 过滤，转维护 |
| 6 | 认知管理 | 现有 | 拆五子 Profile 页 |
| 7 | 任务与决策 | 现有 | 加任务状态机视图 |
| 8 | 集成与工具 | 新增 | Connector、工具授权、MCP |
| 9 | 评估中心 | 新增 | 数据集、跑批、报告、趋势 |
| 10 | 治理与审计 | 现有（合并） | 权限、策略、审计、告警、成本 |

首页改为"数字员工运营"看板（见 W2.8）。

---

## 12. 需拍板的 7 个决策（P1 开工前书面确认）

| # | 决策 | 我的默认建议 | 影响 |
|---|---|---|---|
| 1 | 首个试点企业/业务域 | 当前交付业务域（数据最全） | 决定本体与能力包范围 |
| 2 | 首个数字员工岗位 | 交付/项目管理助理 | 决定 W2.2 能力清单 |
| 3 | 复现岗位标准 vs 复现个人经验 | **以岗位标准为主**，个人经验仅作可选增强且需授权脱敏 | 决定 Profile 权重与合规路径 |
| 4 | 第一批数据源 | 钉钉文档/消息/日历/会议（已通）+ 1 个业务系统只读 | 决定 W2.3 |
| 5 | 多租户 SaaS 或单企业私有化 | **先单企业完全私有化**，表结构保留 tenant | 决定是否上 RLS/运维复杂度 |
| 6 | 首个可写系统与动作 | 周报/工单提交（人工审批后执行） | 决定 W2.6 与 G2 风险面 |
| 7 | 制度/部门规则/岗位 SOP/个人经验冲突优先级 | 制度 > 部门规则 > 岗位 SOP > 历史做法 > 个人建议 | 决定检索权威性排序与决策链 |

**在这 7 项确认前，不启动 W3.1 多岗位与任何 D3。**

---

## 13. 风险清单（对蓝图 §15 的本地化补充）

| 风险 | 本项目的具体表现 | 应对 |
|---|---|---|
| pgvector 装不上 | 内嵌 PG 无扩展文件，可能卡 2~3 天 | W1 第一天就验证方案 A/B，失败则先上 Docker 形态并行 |
| 评估集造假/污染 | 60 题容易"照着答案调" | 训练/验证/测试隔离，测试集只由业务方持有，代码库不落标准答案 |
| 单人开发串行阻塞 | 前端 0.5 人、后端 1 人，P2 会排队 | 前端工作包提前 1 周并行；评估/文档类任务穿插在等编译/等评审时做 |
| 岗位选错 | 选了大而全的岗位导致 4 周做不完 | G1 出口前用 1 天做"数据可得性 + 流程稳定性"打分，低于阈值换岗位 |
| 老数据回填出错 | 10 张表加 tenant 列，历史数据可能为空 | 迁移脚本带 `NOT NULL DEFAULT`，回填后跑一致性 SQL（每表 count 对比） |
| 概念膨胀 | 22 张新表一次性上，没人用 | 每张表必须有对应 API + 控制台入口，否则不进 P1 |
| 图谱/本体返工 | 刚交付的工作台与新 space 模型不一致 | P1 只加 `space_id` 过滤，不改交互；重构留到 P3 |

---

## 14. 立刻可做的 5 件事（本周）

1. **验 pgvector**：`docker run --rm pgvector/pgvector:pg16 psql -c "CREATE EXTENSION vector"`，确认方案 A 可行；同时查 Windows 版 vector 16.4 二进制是否可得（半天）。
2. **Alembic baseline**：把 4 个 init SQL 包成 0001，现存库 `stamp`，随后停用 `ensure_columns()`（1 天）。
3. **评估骨架**：先建 `eval` 五张表 + `runner.py`，用 20 题跑通"跑批 → 出 JSON → 出 Markdown"，不求指标全（1.5 天）。
4. **Redis + 真 worker**：本机起 Redis，`ALWAYS_EAGER=0`，把蒸馏挪到独立进程，验证 kill 后续跑（1 天）。
5. **发 7 个决策的确认单**给业务方，明确"不确认不开工 P1"（0.5 天，等回复期间做前 4 项）。

---

## 15. 进度追踪表（每迭代末更新）

| 阶段 | 计划 | 实际 | 门禁 | 备注 |
|---|---|---|---|---|
| P0 工程补强 | W1–W2 | 未开始 | G0 ⬜ | pgvector 为关键路径 |
| P1 领域模型 | W3–W5 | 未开始 | G1 ⬜ | 依赖 7 项决策 |
| P2 单岗位 MVP | W6–W9 | 未开始 | G2 ⬜ | 岗位待定 |
| P3 岗位复制 | W10–W14 | 未开始 | G3 ⬜ | 跨春节，含缓冲 |
| P4 自动执行 | W15+ | 未开始 | G4 ⬜ | 默认关闭 |

---

## 附：与 V1.2 蓝图的对应关系

| 蓝图章节 | 本文落点 |
|---|---|
| §0 执行摘要 / 架构原则 | §0、§2、§3 |
| §1 产品与领域模型 | §5 W1.1、W1.4、W1.7 |
| §2 目标架构 / 六引擎 | §11.1 目录结构、§6 能力分层 |
| §3 知识/本体/认知模型 | §5 W1.4、W1.5；§4 W0.2 |
| §4 能力与数字员工工厂 | §5 W1.7、§6 W2.2 |
| §5 Runtime 与任务执行 | §6 W2.5、W2.6 |
| §6 决策引擎与权限 | §6 W2.4、§8 P4 |
| §7 接入与蒸馏 | §5 W1.6、§4 W0.3 |
| §8 检索与模型 | §4 W0.2、§4 W0.6 |
| §9 评估体系（P0） | §4 W0.7、§6 W2.8、§9 |
| §10 生产工程架构 | §4 W0.1~W0.6、§11.1 |
| §11 多租户与权限 | §5 W1.2、W1.3、§10 |
| §12 产品工作台 | §11.2 |
| §13 实施路线图 | §3~§8 |
| §14 MVP 范围 | §6、§7（暂不做项见 §1.3） |
| §15 风险清单 | §13 |
| §16 冻结项 | §1.3 |
| §17 产品决策 | §12 |
| §18 最终建议 | §0、§2 |
