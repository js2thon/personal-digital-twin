# CQ 驱动的企业本体建设与持续演进技术方案 V2.0

> 版本：V2.0 · 2026-11-13
> 基线：《Competency Questions 驱动的本体建设——需求评估》（`docs/cq-ontology-assessment.md`）、《总体优化与实施蓝图 V1.2》（`docs/blueprint-v1.2.txt`）
> 输入：两份架构评审意见（CQ 质量评价 / 本体工程架构优化）
> 状态：**设计完成 + 代码已实现 + 回归通过**，可直接交研发团队实施与验收
> 代码位置：`backend/app/modules/ontology/cq_v2/`、`backend/app/api/v1/cq_v2.py`、`backend/tests/test_cq_v2.py`、`web/cq-v2.html`

---

## 0. 结论与交付清单

两份评审的方向一致，但关注点不同，本方案把它们整合成**一套**体系，而不是两份叠加：

| 评审 | 核心主张 | 本方案的处理 |
|---|---|---|
| 第一份 | CQ 质量优先于 Schema 质量；六维质量模型、100 分制、硬门槛、标准化模板、集合质量、三层 CQ | 作为 **CQ 质量准入模块**，前置于 Ontology Factory |
| 第二份 | 业务能力四层、Schema Registry、Query Planner、五维覆盖、六类缺口、决策型 CQ、持续演进 | 作为 **执行与演进主干**，覆盖 Planner → 执行 → 评估 → 治理 |

综合后的定位：**从 CQ-Driven Ontology Construction 升级为 CQ-Driven Ontology Evolution**。CQ 不再是本体模块里的一张问题表，而是贯穿「业务能力定义 → 本体构建 → 知识质量 → 问答评估 → 决策能力评估」的统一机制。

### 0.1 交付物

| 类别 | 路径 | 内容 |
|---|---|---|
| 工程设计 | `docs/cq-driven-ontology-v2.0.md`（本文） | 架构、DDL、API、模块边界、流程、状态机、验收标准 |
| 质量分册 | `CQ质量评估与治理_V2.0.md`（工作区） | 六维模型、评分、模板、种子清单、四关 Pipeline |
| DDL | `backend/app/modules/ontology/cq_v2/schema.sql` | 17 张表 + 通用记录表 + 2 视图，全幂等 |
| 核心代码 | `backend/app/modules/ontology/cq_v2/*.py` | 12 个模块，纯逻辑核心 + 可注入 Store |
| API | `backend/app/api/v1/cq_v2.py` | 30 条路径 / 34 个 HTTP 操作，统一前缀 `/api/v1`，注册于 `app/main.py` |
| 回归测试 | `backend/tests/test_cq_v2.py` | 32 项，覆盖 P0/P1/P2 |
| 控制台 | `web/index.html`（主入口）+ `web/cq-v2.html`（独立页） | 已内嵌「本体」页工作台第 4 个页签「能力问题验收」；`cq-v2.html` 保留为独立完整版 |
| 前版 SQL | `cq_quality_v2_schema.sql`（工作区） | 质量分册配套 DDL，已并入本版 schema.sql |

**验证结果**：`pytest tests/ -q` → **243 passed**（其中新增 32 项），V1.2 原有 211 项无回归；`import app.main` 通过。

### 0.2 一个必须先接受的判断

按新质量准入规则重评现有 16 条种子 CQ：**优秀 4 / 良好 2 / 合格 5 / 待优化 4 / 不合格 1，硬门槛未过 4 条，只有 6 条达到「≥80 分且门槛全过」**。

因此，任何直接拿 16 条去算「覆盖率 ≥70%」的做法，分母里都混着 10 条定义不合格的题。本方案把**质量准入门禁（Gate A）**放在**覆盖度门禁（Gate B）**之前，不是流程洁癖，而是数字有效性的前提。

---

## 1. 设计原则与红线

1. **不要让 CQ 适应当前知识库有什么，而要让知识库建设围绕业务真正需要回答的问题展开。**
2. 三类指标严格分离：CQ 质量（定义得好不好）/ 可回答性（当前能否答）/ 回答质量（答得对不对）。**答不了只记知识缺口，不扣质量分。**
3. 结构化事实以 PostgreSQL 为唯一权威来源，图投影可重建；不因引入 CQ 再造一套事实体系。
4. 机器只能写 `draft` / `proposal`；`human_approved`、`active`、Schema 发布、决策执行必须经人确认。
5. LLM 只理解问题、改写、提议与初评；**不生成任意 SQL/Cypher，不生成最终标准答案，不覆盖硬门槛**。
6. 标准答案来自独立业务数据、权威记录或人工标注，禁止同一 LLM 自生成自验证。
7. 权限在查询执行阶段生效，不是答案展示阶段；租户上下文必须进入执行计划。
8. 每次执行记录 Plan 版本、Schema 版本、CQ 版本、数据快照、模型版本与证据来源，否则历史覆盖率不可比。

---

## 2. 总体架构（七层）

```
01 Business Capability Layer     业务能力、场景、角色、CQ 定义        → capability.py / quality.py
       ↓ 质量准入（Gate A：四关 + 六维 + 硬门槛 + 人工签字）
02 Ontology Factory              概念发现、Schema Mapping、Proposal   → registry.py / mapping.py
03 Knowledge Engineering         多源接入、Claim→Fact、Evidence 治理  → V1.2 既有链路（不改动）
04 Ontology Runtime              Schema Registry、事实/图/时间/检索    → registry.py / engine.py
05 CQ Execution & Evaluation     Parser → Planner → 执行器 → 验证     → planner.py / engine.py
06 Ontology Governance           五维覆盖、六类缺口、集合质量、演进闭环 → engine.py / evolution.py
07 Enterprise Digital Employee  问答、决策、审批、工具执行、人工协同   → decision.py
```

**与 V1.2 蓝图的关系**：不推倒重来。V1.1 已跑通的「知识蒸馏、事实收口、本体版本、审批审计、双向同步」全部保留；V2.0 以**新增模块 + 增量列 + 新端点**接入，`cq.py` 的语义与 API 一字未改。V2.0 同时补上蓝图 V1.2 §9 指出的 `evaluation` 零实现问题——正式 CQ 集与测试实例集直接注册为评估集。

### 2.1 链路重排

```
V1.2  业务问句 → CQ 表 → 需求抽取 → LLM 提案 → 人审发版 → 覆盖率 → 门禁
V2.0  业务问题 → 【CQ 质量评估与治理】→ 正式 CQ 集 → Ontology Factory
                 → 本体/知识建设 → 执行/答案验证/回归 → 缺口分析 → 生成提案 → 新基线
```

Ontology Factory 的输入不再是原始问句，而是「通过准入的 Ontology CQ」。V1.2 的 `analyze()`（L1 确定性抽取）与 `warnings_for()`（未登记项）直接复用为质量 Pipeline 第二关的规则引擎。

---

## 3. 四层业务能力规格模型

| 层 | 实体 | 关键字段 | 代码 |
|---|---|---|---|
| 1 Business Capability | `business_capabilities` | code / name / business_domain / owner / priority / risk_level / weight / version | `capability.py` |
| 2 Competency Questions | `competency_questions`（增量列） | question_template / intent / subject_type / 约束 / 质量分 / 三层状态 / version | `quality.py`、`models.py` |
| 3 Ontology Requirements | `cq_requirements` | cq_id / cq_version / kind(entity·relation·attribute·rule·evidence·source) / ref / schema_version / mandatory / status | `capability.py` |
| 4 Knowledge & Evidence | `cq_test_instances`、`cq_execution_runs` | expected_answer / expected_evidence / source_type / evidence_refs / valid_time | `quality.py`、`store.py` |

`capability_cqs` 承载 CQ 与能力的**多对多**：一个能力对应多条 CQ，一条 CQ 可服务多个能力，一个本体概念可被多个能力复用。

### 3.1 CQ 标准化定义模板（A/B/C 三组）

| 组 | 字段 |
|---|---|
| A 问题定义 | `cq_code`、`question_template`（参数化）、`business_domain`、`capability_id`、`intent`、`business_purpose`、`priority`、`owner` |
| B 语义与验收约束 | `subject_type`、`subject_side`、`target_type`、`required_relations`、`required_attributes`、`scope_constraints`、`temporal_constraints`、`answer_shape`、`expected_answer`、`expected_entities`、`expected_relations`、`expected_evidence`、`acceptance_criteria`、`evidence_requirements` |
| C 质量与评估 | `quality_score`、`quality_dimensions`（六维 0–4）、`hard_gates`、`quality_rubric_version`、`review_status`、`layer_states`、`test_dataset_id`、`evaluation_method`、`version`、`schema_version`、`query_plan_version` |

**参数化**：CQ 定义是规格，测试实例是用例，1:N 分离。例：
「`{person}` 在 `{as_of}` 的直属上级是谁？」→ 张三/当前、李四/当前、王五/2025-12 三个实例，各自带独立标准答案与证据要求。

### 3.2 三种 CQ 层次（第一份评审）与 cq_kind（第二份评审）正交

| 概念 | 维度 | 取值 |
|---|---|---|
| 三层 CQ | **定义分层**，共享 `(cq_id, version)`，各自状态机 | Business CQ（`proposed→approved→retired`）/ Ontology CQ（`mapped→reviewed→published`）/ Evaluation CQ（`drafted→validated→active→deprecated`） |
| `cq_kind` | **用途标记**，同一条 CQ 可兼具 | `business`（业务需求）/ `test`（有标准答案，可回归） |

任一层状态推进都会递增 `version`，并使另两层回到待复核——避免「问题改了、本体没跟、测试还在用旧答案」。

---

## 4. CQ 质量准入（第一份评审落地）

### 4.1 六维模型与评分

| 维度 | key | 权重 |
|---|---|---|
| 业务价值 | `target` | 25 |
| 问题明确性 | `clarity` | 20 |
| 可验证性 | `verifiability` | 20 |
| 本体可表达性 | `ontology_fit` | 15 |
| 业务代表性 | `representativeness` | 10 |
| 可执行性 | `executability` | 10 |

`quality_score = Σ(dim_i / 4 × weight_i)`，0–4 分锚点：0 缺失 / 1 模糊 / 2 基本成立缺约束 / 3 可执行可验证 / 4 完整可复用。

等级：90–100 优秀（核心验收 CQ）、80–89 良好（正式集）、70–79 合格（补定义）、60–69 待优化、<60 不合格。

**三个硬门槛优先于总分**：`target ≠ 0`、`clarity ≥ 3`、`verifiability ≥ 2`，且总分 ≥80。门槛由规则引擎独占，LLM 不得覆盖。

**关键澄清（消解评审内部张力）**：`executability` 评的是「能否转化为明确执行计划规格」，不是「当前库里有没有数据」。问题定义完整但库里没数据 → 该维满分、`quality_score` 不扣，只记 `answerability = unanswerable` 与知识缺口单。

### 4.2 四关质量 Pipeline

```
草案 → ① 语义质量（明确性/歧义/重复/业务价值）
     → ② 本体映射（复用 analyze() + warnings_for()，未命中进 Proposal）
     → ③ 可验证性（标准答案/证据/验收规则，来源白名单）
     → ④ 质量评分（六维 + 硬门槛 + 等级 + 改写建议）
     → 人工审核签字 → 正式 CQ 集
```

任一关失败回退 `draft` 并给可操作建议，不静默丢弃。职责分离：LLM 做语义分析与初评，规则引擎做结构约束与门槛，业务方做最终确认，独立答案源提供标准答案。

### 4.3 集合质量（Portfolio Quality）

五项检查：业务覆盖度、意图多样性（分布 + 熵）、实体覆盖度、关系覆盖度、难度分布（简单/复杂/跨域）。覆盖矩阵是**规划视图**，空格标注「暂无需求」，不硬填。

**取消三处统计配额**：`lookup:path:aggregate ≈ 5:3:2`、每域 5–8 条上限、种子集 ≥30% 不可回答。理由：比例一旦成为目标，就会诱导为满足比例制造问题。替代做法是按真实业务需求采样，异常集中只触发人工复核提示。

> 六维锚点、16 条种子逐条评分与改写建议、评分校准方法详见质量分册 `CQ质量评估与治理_V2.0.md`。

---

## 5. Schema Registry 与 Schema Mapping

### 5.1 统一 Schema 单一事实源

```
LinkML YAML（统一建模）
   → Entity Types / Relation Types / Attributes / Constraints / Cardinality / Validation Rules
   → Schema Validator → Runtime Metadata
   → Projection Engine（事实→图，可重建） → CQ Planner
```

`schema_registry` 表按 `schema_version` 版本化，导出 LinkML YAML（`GET /ontology/v2/schema/linkml`）。关系目录不再硬编码在 `graph.py::RELATION_TYPES`，运行时目录由 Registry 生成；历史事实、关系投影、CQ 执行计划通过 `pinned_by_cq` 做版本锁定。

### 5.2 Schema 迁移机制

`schema_migrations` 记录：Schema Diff（added/removed/changed 分节）、Migration Plan（add / deprecate / migrate_data / validate）、Compatibility（新增为 compatible，删除或变更为 breaking）、Data Migration、Rollback SQL、Rollback Version、Applied By、Version Pinning。

**红线**：先停用再迁移，禁止直接删除历史事实；breaking 变更必须过 Schema Validator 与 CQ 回归；回滚只切版本指针，数据迁移需人工复核。

### 5.3 Schema Mapping 与六类 Proposal

生成新类型前先做概念标准化：同义词表、`PARENT_CHILD` 父子判断、属性/关系复用判断、新增必要性评估。

| 操作 | 判定 | 示例 |
|---|---|---|
| `reuse` | 已登记类型或同义词归一 | 部门 → OrganizationUnit |
| `extend` | 已登记类型的子类，用分类属性区分 | 项目经理 → Person |
| `merge` | 存在以业务叫法命名的重复旧类型 | 事业部（旧类型）→ OrganizationUnit |
| `split` | 概念过于宽泛 | 对象 / 信息 |
| `create` | 未命中，且必须带 CQ 依据 + 3 条真实例句 | 新业务概念 |
| `deprecate` | 命名已被统一目录废弃 | Org / Department |

`schema_proposals` 只落提案，**不自动改 Schema**；审核通过后才进入版本发布。

---

## 6. CQ Query Planner 与执行引擎

### 6.1 流程

```
CQ Input → CQ Parser（意图/实体/约束/时间）
        → CQ Query Planner（结构化 AST + plan_version + checksum）
        → Schema / 权限 / 复杂度校验
        → Fact / Graph / Temporal / Retrieval Executor
        → Answer Validator（证据、冲突、权限、时效、完整性）
        → CQ Result（五态）
```

### 6.2 计划 AST 与校验

AST 字段：`plan_version`、`cq_code/version`、`schema_version`、`steps[]`、`executor_chain`、`complexity`、`permissions`、`provenance`、`checksum`。

允许的操作白名单：`resolve_entity / filter_attribute / traverse / aggregate / compare / exists / asof / retrieve / validate / decide`。

校验规则（任一失败即阻断执行）：
- 禁止 `raw_sql / sql / cypher / raw_query / exec / eval` 字段；
- `op` 必须在白名单内，步骤 ≤12，`max_hops ≤ 3`；
- 必须含 `validate` 步骤；`permissions.tenant_scoped = true`；
- 执行上下文必须有 `tenant_id`，`required_scopes` 必须被授权；
- checksum 一致（服务层附加的审核/落库元数据不参与摘要）；
- 未登记的类型/关系产生 warning → Schema Gap，不阻断执行。

### 6.3 执行器

| 执行器 | 职责 | 关键约束 |
|---|---|---|
| Fact | 事实、属性、聚合、存在性 | 只取主语类型内的活跃事实 |
| Graph | 关系、路径、关联 | ≤3 跳、环保护由计划复杂度校验保证 |
| Temporal | 历史状态、时间有效性 | **双时间轨**：Valid Time + Transaction Time |
| Retrieval | 文档/语义证据检索 | **正式执行器**，制度条款/合同约定/项目背景不必全转成图关系 |
| Validator | 证据、冲突、权限、时效、完整性 | 冲突/受限只判定本次执行实际用到且谓词相关的事实 |

**三不原则**：LLM 不生成任意 SQL/Cypher；计划不校验不执行；每次执行不留版本四元组不算数。

### 6.4 运行审计

`cq_execution_runs` 记录 `run_id / plan_version / schema_version / cq_version / data_snapshot_id / model_version / tenant_id / actor / permission_context / answer / evidence_refs / conflict_refs / audit`。没有数据快照与版本信息，今天与明天的覆盖率不可比。

---

## 7. 五维覆盖率、五态与分层门禁

### 7.1 五维

| 维度 | 评价内容 | 计算要点 |
|---|---|---|
| Schema Coverage | 必要类型/关系是否登记 | 已登记需求项 / 需求项总数 |
| Data Coverage | 是否存在所需事实与属性 | 覆盖主体数 / 主体总数 |
| Evidence Coverage | 是否有效证据 | 有效证据 / 证据总数，过期、低可信、冲突均扣分 |
| Answer Coverage | 是否能正确回答 | 有独立标准答案时精确比对；无标注时**不冒充正确**，用 data×evidence 代理并标记 `answer_basis` |
| Temporal Coverage | 数据是否满足时间有效性 | as_of 切片命中率，过期证据扣减 |

### 7.2 五态（判定优先级）

`restricted`（数据存在但无权访问）> `conflict`（未解决事实冲突）> `answered`（五维均值 ≥0.8 且证据达标）> `partial` > `unanswerable`。

**有数据不等于有答案，有答案不等于答案可信**：有关系无证据、证据已过期、两个矛盾事实、用户无权访问来源，都不能算完整回答。

### 7.3 加权覆盖率与分层门禁

`Coverage = Σ w_i · I(answered_i) / Σ w_i`，`w_i` 来自 CQ 业务优先级/能力权重。分母**只取通过 Gate A 的正式 CQ**。

70% 不再是所有场景统一硬门槛，拆成四条独立门禁：

| 门禁 | 口径 |
|---|---|
| 核心业务 CQ 覆盖率 | 独立计算，`min_core = 0.70` |
| 普通 CQ 覆盖率 | `min_normal = 0.60`，核心达标不能替它放行 |
| 高风险 CQ 正确率与证据率 | `min_accuracy = 0.95`、`min_evidence = 0.90`，不被大量简单查询拉高 |
| 证据可追溯率 | 全题集 `evidence_rate ≥ 0.90` |

首轮覆盖率低（演示环境 1/6 = 16.7%）是**正常且可解释的**，是向业务方要数据/要权限的依据，不掩盖、不刷分。

---

## 8. 六类缺口治理

| 类型 | 具体情况 | 修复入口 |
|---|---|---|
| Entity Gap | 实体不存在 | 实体抽取与对齐 |
| Relation Gap | 两端实体在、关系无实例 | 事实审核与关系构建 |
| Attribute Gap | 必要属性未抽取 | 补充属性抽取模板 |
| Evidence Gap | 证据不足/过期/低可信 | 重新检索、补充证据链 |
| Schema Gap | 本体缺少概念或约束 | Schema Proposal → 人审发布 |
| Source / Access Gap | 数据源未接入、接口未授权、用户无权限、同步失败、数据过期 | 数据接入 / 权限申请 |

**Source / Access Gap 必须独立**：企业里很多问题不是知识库没数据，而是数据源没接或没授权；混成「数据缺失」会让本体团队承担不属于自己的修复工作。缺口只报该 CQ 声明需要的数据源，避免噪声。

`GapPriority = BusinessImpact × CQWeight × GapSeverity`，修复按优先级而非缺口数量排序。

---

## 9. 决策型 CQ 与 Decision Evaluation Layer

意图枚举：**六类基础意图**（lookup / path / aggregate / temporal / boolean / compare）+ **两类决策扩展**（decision / recommendation）。

决策型 CQ **不复用知识查询执行器**，独立链路：

```
Decision CQ → Evidence & Context（事实、关系、规则、上下文）
            → Decision Engine（规则命中、约束判断、评分）
            → Decision Result（建议、依据、置信度、风险、不确定性）
            → Human Approval / Authorized Execution
```

MVP 只产出建议 + 人工确认：`pending → approved → executed`。以下情况阻止确认或执行：存在 `fact_conflict`、置信度 <0.8、高风险决策要求授权自动执行。Jev 决策模型不替代本体、事实查询或业务规则引擎。

---

## 10. 五个工程设计问题的处理

| 问题 | 处理 |
|---|---|
| CQ 版本管理 | CQ 独立 `version`，绑定 `schema_version` / `query_plan_version` / `testset_version`；任一层变更递增版本 |
| CQ 参数化 | `question_template` + `cq_test_instances.params`；模板变量未绑定则实例校验失败 |
| 证据质量 | 不只是 `claim_id`：检查 `evidence_catalog` 的过期、来源权威度、冲突、可追溯 |
| 多租户权限 | 权限在**执行阶段**生效：`permissions.tenant_scoped`、`required_scopes`、`tenant_id` 校验，避免无权事实参与聚合/推理 |
| 运行审计 | 计划/Schema/数据快照/模型/证据/结果/耗时全留痕；`audit_log` 记录准入、发版、提案、决策、演进 |
| 时间双轨 | facts 保留 `valid_from/valid_to`（Valid Time）与 `recorded_at`（Transaction Time）；`asof` 执行器按业务时点切片 |
| 冲突裁决 | `conflict` 独立状态，未裁决不进入 `answered`，也不允许决策确认 |

---

## 11. 数据库设计

`backend/app/modules/ontology/cq_v2/schema.sql`，全部 `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` / `DO $$ ... $$` 守卫，可重复执行；不重建表、不删列、不改现有数据。

| 层 | 表 | 说明 |
|---|---|---|
| P0 | `business_capabilities`、`capability_cqs`、`cq_requirements` | 四层建模；`cq_requirements` 带主键、外键、`UNIQUE(cq_id, cq_version, kind, ref)` |
| P0 | `competency_questions`（+31 增量列） | 三组字段、版本、质量分、三层状态、风险与权重 |
| P0 | `cq_test_instances` | 参数化实例；`source_type` 白名单 + CHECK 禁止 LLM 草稿直接成为正式答案 |
| P0 | `cq_quality_assessments`、`cq_evaluation_runs`、`cq_portfolio_snapshots` | 评分历史（rubric 版本化，历史不重算）、回归、集合质量趋势 |
| P1 | `schema_registry`、`schema_migrations`、`schema_proposals` | LinkML 单一来源、迁移/回滚/版本锁定、六类提案 |
| P1 | `cq_query_plans`、`cq_execution_runs`、`cq_coverage_v2`、`cq_gaps` | 计划 AST、执行审计、五维覆盖、六类缺口 |
| P2 | `cq_decisions`、`ontology_evolution_runs` | 决策建议与人工确认、演进闭环基线 |
| 通用 | `cq_v2_records` | JSONB 通用存取，Memory/Postgres 双 Store 接口一致 |
| 视图 | `v_cq_admission_v2`、`v_cq_metrics_v2` | Gate A 判定（硬门槛由 SQL 独占）、三指标分离 |

**迁移策略**：`CQ_V2_STORE=postgres` 时 `PostgresStore.ensure_schema()` 执行全部 DDL（DO 块内分号由 `split_statements()` 正确保留）；CQ 同步写入 `competency_questions` 真实表，保证关系表外键成立、视图有数据。默认 `memory` 模式用于开发、演示与回归，不写生产库。

---

## 12. API 契约

统一前缀 `/api/v1/ontology/v2`，与 V1.2 `/ontology/cq` 并存。

| 分组 | 端点 |
|---|---|
| 基础 | `GET /meta`、`POST /bootstrap`、`GET /four-layers`、`GET /metrics`、`GET /audit` |
| 能力 | `GET/POST /capabilities`、`POST /capabilities/{id}/cqs`、`GET /requirements` |
| CQ | `GET/POST /cqs`、`GET/PATCH /cqs/{id}`、`POST /cqs/{id}/quality-check`、`POST /cqs/{id}/admit`、`GET/POST /cqs/{id}/instances` |
| 计划与执行 | `POST /cqs/{id}/plan`、`POST /plan/validate`、`POST /cqs/{id}/run`、`GET /coverage`、`GET /gaps`、`GET /portfolio` |
| Schema | `GET /schema`、`GET /schema/linkml`、`POST /schema/publish`、`POST /schema/rollback`、`POST /schema/pin`、`POST /mapping/propose`、`GET /proposals`、`POST /proposals/{id}/approve` |
| 决策与演进 | `POST /decisions/run`、`POST /decisions/{id}/approve`、`POST /evolution/cycle` |

**请求示例**

```bash
# 1 初始化（force 重建；auto_admit_actor 视为业务负责人批量签字）
curl -X POST http://127.0.0.1:8101/api/v1/ontology/v2/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"force":true,"auto_admit_actor":"业务负责人"}'

# 2 单条四关质检
curl -X POST http://127.0.0.1:8101/api/v1/ontology/v2/cqs/{id}/quality-check -d '{}'

# 3 跑覆盖率（默认只统计准入通过的 CQ）
curl 'http://127.0.0.1:8101/api/v1/ontology/v2/coverage?only_admitted=true'

# 4 演进闭环
curl -X POST http://127.0.0.1:8101/api/v1/ontology/v2/evolution/cycle \
  -d '{"actor":"业务负责人"}'
```

**错误码**：400 参数/校验/红线拒绝（如机器签字、LLM 草稿答案、提案证据不足）；404 CQ/能力/Proposal 不存在。

控制台主入口：「本体」页 → 本体工作台 → 第 4 个页签「能力问题验收」（`http://127.0.0.1:8101/?view=ontology&tab=cq`）。页签内按业务闭环分四段：**质量准入 → 覆盖与缺口 → 演进闭环 → 问题明细**（明细段承载 V1.2 的 CQ 文本编辑）。本体页同时完成了一次信息架构重排：工作台卡片置顶、页签按「结构 → 编辑 → 校验 → 验收」排序、删除了与模式视图重复的「本体版本」原始 JSON 卡、移除了侧边栏独立入口。`web/cq-v2.html` 保留为独立完整版（支持自定义 API 基址，以文件打开时默认 `http://127.0.0.1:8101/api/v1/ontology/v2`）。

---

## 13. 模块边界

```
backend/app/modules/ontology/cq_v2/
├── models.py       常量、枚举、状态机、默认结构（纯标准库）
├── quality.py      六维评分、硬门槛、四关 Pipeline、参数化/实例校验
├── capability.py   四层建模、需求推导、能力覆盖、高风险门禁
├── registry.py     Schema Registry、Diff/迁移/回滚/版本锁定、LinkML 导出
├── mapping.py      概念标准化、同义词、父子判断、六类 Proposal
├── planner.py      Parser、Query Plan AST、Schema/权限/复杂度校验
├── engine.py       四执行器、Answer Validator、五维覆盖、五态、六类缺口
├── decision.py     Decision Evaluation Layer、人工确认状态机
├── evolution.py    Portfolio Quality、覆盖矩阵、7 步演进闭环、运行审计
├── store.py        MemoryStore / PostgresStore、DDL 幂等执行、关系表双写
├── service.py      CQEngine 门面（API/CLI/测试唯一入口）
├── schema.sql      PostgreSQL 增量 DDL
└── __init__.py     get_engine() / reset_engine()
```

依赖方向单向：`service → 各纯逻辑模块 → models`；只有 `store.py` 触碰数据库。核心逻辑全部是纯函数 + 普通 dict，**不连库即可跑完整闭环**，这也是 32 项测试能用假 DB 覆盖全量能力的原因。

---

## 14. 执行流程与状态机

### 14.1 端到端时序

```
业务问题/文档 → 参数化 CQ 草案
  → 四关质检 → 业务负责人签字 → 正式 CQ 集（Gate A）
  → 推导 Ontology Requirements → Schema Mapping 提案 → 人审发布 Schema 版本
  → 生成并校验 Query Plan AST（绑定 schema_version）
  → Fact/Graph/Temporal/Retrieval 执行 → Answer Validator
  → 五维覆盖 + 五态 → 六类缺口（按 GapPriority 排序）
  → 决策型 CQ 走 Decision Layer（建议 + 人工确认）
  → 集合质量报告 → 演进闭环 7 步 → 新基线
```

### 14.2 状态机汇总

| 对象 | 状态 |
|---|---|
| Business CQ | `proposed → approved → retired` |
| Ontology CQ | `mapped → reviewed → published` |
| Evaluation CQ | `drafted → validated → active → deprecated` |
| 质量准入 | `pending → machine_passed → human_approved / rejected` |
| CQ 结果 | `answered / partial / unanswerable / conflict / restricted` |
| Proposal | `pending → approved / rejected → published` |
| Decision | `pending → approved / rejected → executed`（executed 需额外授权） |
| 演进闭环 | `running → completed / failed`（任一步失败即停并留审计） |

---

## 15. 开发验收标准

| # | 验收项 | 判定 |
|---|---|---|
| 1 | 16 条种子全量产出六维分、等级、硬门槛 | `POST /bootstrap` 返回 16 条，6 条准入 |
| 2 | 硬门槛不可被 LLM 覆盖、机器不能签字 | 单测 `test_hard_gates_override_total_score`、`test_admit_requires_human_signature` |
| 3 | 标准答案来源白名单 | `llm_draft_unverified` + `active` 被 DDL CHECK 与 `validate_instance` 双重拒绝 |
| 4 | Query Plan 拒绝任意 SQL/Cypher、缺租户、checksum 篡改 | 单测 3 项；计划不校验不执行 |
| 5 | 五维齐全、五态可区分 | 演示数据产出 answered / partial / conflict / restricted |
| 6 | 六类缺口 + GapPriority 公式 | 单测逐条校验公式；Source/Access Gap 独立且只报声明的数据源 |
| 7 | 三指标分离 | `metrics()` 分列 quality / answerability / answer_accuracy |
| 8 | Schema 六操作、Diff、兼容、回滚、版本锁定 | 单测覆盖 6 种 operation + breaking 判定 + rollback |
| 9 | 决策层只建议、人工确认、高风险禁自动执行 | 单测 2 项 |
| 10 | 演进闭环 7 步 + 审计 + 基线前后对比 | 单测 + `evolution_runs` 落库 |
| 11 | 参数化模板与实例分离、变量绑定校验 | 单测 2 项 |
| 12 | V1.2 无回归 | `pytest tests/ -q` → 243 passed |
| 13 | 权限在执行阶段生效 | 计划校验 `tenant_id` / `required_scopes`；受限事实判 `restricted` |
| 14 | 执行留版本四元组与证据 | `cq_execution_runs` 字段完整 |
| 15 | 幂等迁移 | DDL 重复执行不报错、不覆盖已审核内容 |

**安全验收**：机器写入只能落 `draft`/`pending`；发布、准入、提案审核、决策确认必须由具名业务方执行并留审计。

---

## 16. 实施路线

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | CQ 建模、业务能力关联、标准答案与独立评估、CQ 独立版本、质量准入 | **已实现** |
| P1 | Schema Registry（LinkML）、Query Planner AST、五维覆盖、五态、六类缺口、执行审计、分层门禁 | **已实现** |
| P2 | Schema Mapping 六操作、决策型 CQ、集合治理、本体持续演进闭环 | **已实现** |

后续工程化（不在本轮）：LinkML 真实编译与 Pydantic 代码生成、关系目录从 `RELATION_TYPES` 常量迁移为 Registry 运行时加载、真实多租户 RLS、LLM 提案接入真实模型与 Prompt 版本管理、文档检索执行器接 pgvector、生产 PostgreSQL 全量切换（`CQ_V2_STORE=postgres`）。

**与 V1.2 蓝图的对应**：P0/P1 落在蓝图 P1「企业化建模」工作包，P2 与 P2「单岗位闭环 / 评估中心」衔接；CQ 集注册为 `EvaluationSet`，结束 evaluation 零实现状态。

---

## 17. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 评分主观、被协商 | 双盲校准 + 0–4 锚点 + `quality_rubric_version`；可机械判定维度与硬门槛由规则引擎独占 |
| 标准答案污染 | 来源白名单 + LLM 草稿必须人工转正 + 记录 `source_ref`/`annotated_by` |
| 质量门禁被绕过 | 跑批默认 `only_admitted=true`；`human_approved` 只能由人写；准入全量审计 |
| 覆盖率 KPI 造假 | 硬门槛 + 锁定分母 + 意图异常集中触发人工复核 |
| 本体膨胀 / 概念重复 | Schema Mapping 六操作 + 新类型必带 CQ 依据与 3 条例句 + 只落 proposal |
| Schema 版本漂移 | Diff / Migration Plan / Compatibility / Rollback / Version Pinning + 版本四元组 |
| 缺口提示变噪声 | 只报 mandatory 与本 CQ 声明的数据源；按 GapPriority 排序、同实体合并 |
| 决策越权自动执行 | MVP 只建议；高风险、冲突、低置信度一律阻止确认/执行 |
| 与 V1.2 重复建设 | 正式 CQ 集复用为评估集；`analyze()`/`warnings_for()` 复用为第二关规则引擎 |

---

## 18. 两份评审意见的逐条处理对照

| 来源 | 评审条目 | 落地位置 | 状态 |
|---|---|---|---|
| 一 | CQ 质量优先、独立一级模块前置 | §2 链路重排、`quality.py`、Gate A | 完成 |
| 一 | 六维质量模型 / 100 分制 / 三个硬门槛 | §4.1、质量分册、`quality.py` | 完成 |
| 一 | 三指标分离 | §1/§7、`v_cq_metrics_v2`、`metrics()` | 完成 |
| 一 | 三层 CQ + 共享 ID/版本 | §3.2、`layer_states`、`m.transition()` | 完成 |
| 一 | 标准化模板 A/B/C | §3.1、DDL 31 增量列 | 完成 |
| 一 | 参数化模板与测试实例分离 | §3.1、`cq_test_instances` | 完成 |
| 一 | 集合质量、覆盖矩阵、取消统计配额 | §4.3、`evolution.portfolio_quality()` | 完成 |
| 一 | 12 条种子无清单 / 意图枚举不一致 / 70% / 30% | §0.2 与质量分册 §7/§10 | 完成 |
| 一 | 四关 Pipeline、职责分离、答案独立性 | §4.2、`four_gates()`、DDL CHECK | 完成 |
| 二 | 业务能力四层、多对多 | §3、`capability.py`、3 张表 | 完成 |
| 二 | 标准答案与独立评估、业务需求 CQ/测试 CQ | §3.1/§3.2、`cq_kind` | 完成 |
| 二 | Schema Mapping 六操作 | §5.3、`mapping.py` | 完成 |
| 二 | 统一 Schema Registry / LinkML / 迁移机制 | §5.1/§5.2、`registry.py` | 完成 |
| 二 | Query Planner AST / 执行器 / 检索正式化 | §6、`planner.py`、`engine.py` | 完成 |
| 二 | 五维覆盖、conflict/restricted、加权、分层门禁 | §7、`engine.py` | 完成 |
| 二 | 六类缺口、Source/Access、GapPriority | §8、`detect_gaps()` | 完成 |
| 二 | 决策型 CQ / Decision Layer | §9、`decision.py` | 完成 |
| 二 | 版本/参数化/证据/权限/审计 5 问题 + 时间双轨 | §10、DDL、`answer_validator()` | 完成 |
| 二 | 文档不一致修正（意图种类、人日、配额、独立测试集） | §0.2、§9、§16、质量分册 §10 | 完成 |
| 二 | 7 层架构、P0/P1/P2、演进闭环 | §2、§16、`evolution.run_loop()` | 完成 |
| 二 | DDL / API / 模块边界 / 流程 / 状态机 / 验收标准 | §11–§15 | 完成 |

**意图枚举冲突的处理说明**：第一份评审要求统一原文档 §4/§6/§8 的打架（原文分别写 6 种、5 行、「四类/五种」），第二份评审建议「修正为五类」，但同一份评审在第 8 点又扩展出 `compare / decision / recommendation`。综合后统一为 **6 类基础意图 + 2 类决策扩展**，决策类不复用知识查询执行器，单独走 Decision Layer；`meta.base_intents` / `meta.decision_intents` 分别暴露，避免再次出现多套口径。

**70% 门禁冲突的处理说明**：第一份要求「70% 之前先有质量准入」，第二份要求「70% 不是统一硬门槛」。综合为 **Gate A 质量准入（前置）+ Gate B 分层覆盖门禁**（核心 / 普通 / 高风险 / 证据四条独立），既不跳过准入，也不让一个数字掩盖高风险问题。

---

## 19. 运行与验证

```bash
cd personal-digital-twin/backend

# 默认 MemoryStore，零数据库依赖
python -m pytest tests/test_cq_v2.py -q      # 32 passed
python -m pytest tests/ -q                    # 243 passed（含 V1.2 全量）

# 本机实跑形态：8101，API 与控制台同源（与 start-services.bat 保持一致）
uvicorn app.main:app --host 127.0.0.1 --port 8101 --no-proxy-headers
# 控制台：http://127.0.0.1:8101/?view=ontology&tab=cq
#   本体页 → 本体工作台 → 「能力问题验收」页签；独立完整版在 /console/cq-v2.html

# 切换真实持久化（执行幂等 DDL；外露访问需先配置 API_KEY，见 app/auth.py）
CQ_V2_STORE=postgres uvicorn app.main:app --host 0.0.0.0 --port 8101 --no-proxy-headers
```

> **部署前必读：后端代码改动后必须重启 8101。** 本机形态由 `start-services.bat` /
> `start_hidden.py` 以**无 `--reload`** 的 uvicorn 启动，守护逻辑只做「崩溃后重启」，
> 且 8101 已通过 `/healthz` 健康探测时会直接退出、不接管已运行进程——重复双击启动
> 脚本**不会**加载新代码。而 `web/*.html` 由 `StaticFiles` 按磁盘实时读取，于是会出现
> 「新页面能打开、新接口全 404」的迷惑现象。正确做法是先 `stop-services.bat` 再启动。
> 验收时先确认运行进程已加载 V2 路由（无输出即为旧进程）：
>
> ```bash
> curl -s http://127.0.0.1:8101/openapi.json | grep ontology/v2
> ```

**装配层回归**：`tests/test_cq_v2.py::test_app_main_registers_v2_routes` 直接对 `app.main`
的 ASGI app 发真实请求，校验 `/api/v1` 前缀、`identity_gate` 身份闸门、V2 路由注册，
以及 V1.2 `/ontology/cq/meta` 不受遮蔽。仅检查子 router 裸 path 的测试无法发现
「main.py 漏注册」或「运行进程加载旧代码」，故补上这一层。

演示数据 `demo_snapshot()` 可复现，包含：有效/过期/低可信证据、事实冲突、受限事实、未接入与未授权数据源，用于真实呈现五态与六类缺口。首轮加权覆盖率偏低是有意设计——它正是向业务方要数据、要权限的依据。

**未改动**：`backend/app/modules/ontology/cq.py`、`backend/app/api/v1/ontology.py` 的既有端点语义、V1.2 数据、`db/init/*` 初始化脚本、既有路由与启动脚本。存量文件的改动仅两处：`app/main.py` 增加一行 import 与一行 router 注册；`app/api/v1/__init__.py` 补齐与 `main.py` 对齐的 12 个模块 re-export（原先只导出 8 个）；另在 `web/index.html` 增加一个导航入口。其余均为新增文件：`cq_v2/` 包、`api/v1/cq_v2.py`、`tests/test_cq_v2.py`、`web/cq-v2.html` 与本文。

---

## 20. 后续工作建议

1. 把 `graph.py::RELATION_TYPES` 的 35 类关系正式迁入 Schema Registry，运行时改为按 `schema_version` 加载（需同步改造 `projection.py` 与 `sync.py` 的校验入口）。
2. 接入真实 LLM 做 CQ 起草与语义初评，但保留「规则引擎独占硬门槛」与「独立答案源」两条红线。
3. 文档检索执行器接 pgvector，并把制度条款、合同约定、会议决策纳入证据目录。
4. 生产环境启用 PostgreSQL RLS，把 `tenant_id` 从执行上下文下沉到数据库层。
5. 将 CQ 集注册为蓝图 V1.2 的 `EvaluationSet`，接进评估中心发布门禁与回归看板。
