# Competency Questions 驱动的本体建设 —— 需求评估（2026-11-13）

结论先行：**方向对，而且正好补上当前本体建设最缺的一环——「凭什么建这些类型」。但「平台自动基于 CQ 建本体」不能做成 LLM 直接生成 schema 落库，必须拆成「CQ 定义需求 → 机器提建议 → 人确认 → 机器校验」。** 建议作为蓝图 V1.2 阶段 P1 的第一个工作包（约 1.5 天），而不是独立立项。

三个必须先知道的实测约束：

1. **关系目录是代码常量**：35 个关系类型硬编码在 `modules/ontology/graph.py::RELATION_TYPES`（含源/目标类型约束、基数）；已发布的 `ontology_versions.schema_content`（v1.0.0）**只有 `domains → entities`，没有 relations 段**。所以「自动扩本体」现在做不到配置化，这是前置改造。
2. **可回答面很窄**：464 条活跃事实里只有 **15 条能确定性投影成边（3.2%）**；259 个不同谓词只有 35 个登记为关系类型；其余是 `believes / plans_to / emphasizes` 这类长文本宾语。CQ 覆盖率首轮一定很低，这是价值（让缺口可度量），但要有预期管理。
3. **现有规则只校验「数据对不对」，不校验「数据够不够」**：R1–R10 全是结构自洽（未登记类型/类型越界/单值冲突/自环/缺证据/悬空端点/重复/档案异常/属性值指实体/孤立实体），没有一条回答「这条知识能不能支撑某类问题」。

---

## 1. 这句话其实是四件事，难度差一个量级

| # | 子需求 | 实质 | 难度 | 建议 |
|---|---|---|---|---|
| 1 | 本体增加 CQ | 建 CQ 对象 + 与类型/关系挂钩 + 版本化 | 低（纯建模 + CRUD） | 先做 |
| 2 | 基于 CQ **自动建设本体** | CQ → 需求项 → 类型/关系提案 → 人审 → 发布 | 中高（LLM + 治理） | 做到「提案」，不做到「自动发布」 |
| 3 | 基于 CQ **自动关联数据** | CQ → 可执行计划（事实查询/图路径/时间窗）→ 三态结果 | 中（可复用 projection / graph.paths_query / entity_profile） | 值得做，是核心价值 |
| 4 | 数据缺失**自动提示** | 覆盖率判定 → 缺口四分类 → 对应修复入口 | 中（提示要能一键修，否则是噪声） | 和 3 一起做 |

**关键判断**：2 的「全自动」是伪需求。原因不是技术做不到，而是**做错了没人能发现**——LLM 生成一个 `has_budget` 关系，看着合理，但全库零实例、也说不清哪条 CQ 需要它，半年后就是一张没人读的空表。这与本项目已有的「Claim→审核→Fact 唯一收口」「宁可漏建边不可错建边」是同一套哲学。

## 2. 为什么 CQ 特别适合这个项目

CQ（Competency Questions，Grüninger & Fox）的本意是：**本体必须能回答的问题集合 = 本体的需求规格与验收标准**。

现在的建设顺序是反的：先有抽取谓词（259 个）→ 再回头补目录（35 个）→ 发现 84% 实体孤立、109 条宾语是长文本建不了边。**CQ 把顺序正过来：先定「要回答什么」，再反推「要建什么类型、要什么关系、要补哪批数据」。**

三个额外收益：

- **给 P1 一个依据**：蓝图 V1.2 要在 P1 做「15–25 个实体类型」，CQ 就是「为什么是这 25 个」的书面答案；
- **顺手解决 evaluation 零实现**：CQ 集天然就是评估集，一份投入两处产出（当前 evaluation 模块为空）；
- **缺口可量化**：现在「数据不足」只能靠感觉，有了 CQ 覆盖率就有了分子分母。

## 3. 现状盘点：哪些能直接复用

| 组件 | 位置 | 对 CQ 的作用 | 缺什么 |
|---|---|---|---|
| 本体版本 | `ontology.ontology_versions.schema_content` | 承载 CQ 与类型的绑定、版本化回滚 | 只有 `domains→entities`，**无 relations 段** |
| 关系目录 | `graph.py::RELATION_TYPES`（35 类） | CQ 校验的「合同」：类型约束、基数、家族 | 代码常量，扩类型要改代码 |
| 事实→边投影 | `projection.py`（含 `entity_id` 优先、类型前置校验） | CQ「关联数据」的执行器 | 只处理已登记谓词，长文本宾语不解析 |
| 图查询 | `graph.py::paths_query`（k≤3 跳、环保护） | path 类 CQ 直接可用 | — |
| 实体画像 | `service.py::entity_profile`（asof 时间旅行、单值/多值） | temporal 类 CQ 直接可用 | — |
| 规则引擎 | `quality.py` R1–R10 + 自测 | 修复缺口后复检 | 无覆盖率/够不够用维度 |
| 双向同步 | `sync.py`（R1–R4） | 修完缺口图不漂移 | — |
| 问答链路 | `twin/graphs/qa.py` → `retrieval` | CQ 端到端验收 | 走 facts/chunks，**不走图遍历**；CQ 验证需同时覆盖两条路径 |
| 评估 | 无 | CQ 即评估集 | 零实现 |

## 4. 设计要点：CQ 是一等对象，但不写查询语言

新增 3 张表（含 1 张快照）：

```sql
ontology.competency_questions (
  id UUID PK, code VARCHAR(20) UNIQUE,        -- CQ-001
  question TEXT NOT NULL,                      -- 「某某现在的汇报对象是谁？」
  intent VARCHAR(20) NOT NULL,                 -- lookup|path|aggregate|compare|temporal|boolean
  subject_type VARCHAR(40),                    -- Person（问题的主语类型）
  answer_shape VARCHAR(20),                    -- entity|value|list|count|yes_no
  required_relations JSONB DEFAULT '[]',       -- ['reports_to']
  required_attributes JSONB DEFAULT '[]',      -- ['level']
  min_evidence SMALLINT DEFAULT 1,             -- 至少几条证据才算答得出
  priority SMALLINT DEFAULT 3,                 -- 1 高 2 中 3 低
  status VARCHAR(20) DEFAULT 'draft',          -- draft|active|retired
  owner VARCHAR(60), domain VARCHAR(40), notes TEXT
);
ontology.cq_requirements (                     -- CQ 缺什么（供缺口分析反查）
  cq_id UUID, kind VARCHAR(20),                -- entity|relation|attribute|evidence|type
  ref VARCHAR(80), mandatory BOOLEAN DEFAULT TRUE
);
ontology.cq_coverage_snapshots (               -- 每次跑批留档，看趋势
  cq_id UUID, run_at TIMESTAMPTZ, state VARCHAR(20),  -- answered|partial|unanswerable
  score NUMERIC(4,3), gaps JSONB, plan_used JSONB
);
```

**为什么不存 SPARQL/Cypher**：写查询语言等于要求业务方会写图查询。存「意图 + 主语类型 + 需要的关系/属性 + 答案形态」，一套定义能被翻译成三种执行计划（事实查询 / 图路径 / 检索+LLM 兜底），人也能读懂、规则也能校验。

## 5. 自动建本体：三级递进，每级都有人审

| 级别 | 做法 | 产出 | 是否落库 |
|---|---|---|---|
| L1 确定性抽取 | 问句分词 → 名词对现有 23 类型/别名，动词对 35 关系类型及 `PREDICATE_ALIASES` | 需求项清单（命中/未命中） | 只读，未命中进 L2 |
| L2 LLM 提案 | 只允许「在既有类型里选」或「提议新类型（必须带理由 + 3 条真实例句）」；输出结构化 JSON | `schema proposal`（pending） | 落 proposal 表，**不改 schema** |
| L3 人审 + 版本发布 | 审核通过 → 合并进 `schema_content` v1.1（新增 relations 段）→ 发新版本，旧版可回滚 | 新本体版本 | 人工点确认才落 |

**三条红线**（与既有工程原则一致）：

1. 机器只能写 proposal，不能写 schema；
2. 新关系类型必须同时声明 `source` / `target` 约束与基数，否则不予发布——不然 R2「类型不满足约束」立刻爆红；
3. 每个新增类型/关系必须能回答「哪条 CQ 需要它」，查不到 CQ 依据的不进版本。

## 6. 自动关联数据：四类意图 → 四套执行计划

| intent | 典型问题 | 执行计划 | 复用的现成能力 |
|---|---|---|---|
| lookup | 某某的汇报对象是谁 | `facts` where subject+predicate | `_load_active_facts` + projection |
| path | 某某和某某丁怎么关联上的 | ≤3 跳子图 / 路径枚举 | `graph.paths_query` |
| temporal | 他去年是什么级别 | asof 时间切片 + 取代链 | `entity_profile(asof=)` |
| aggregate / compare | 团队里有几个项目经理 | 按类型/谓词聚合 + 时间窗 | facts 聚合 + `graph.stats` |
| boolean | 这个项目是否已立项 | 存在性判定 + 证据阈值 | facts 存在性 + `min_evidence` |

每个 CQ 的执行结果三态：`answered`（有值且有证据）/ `partial`（有实体无关系、或有关联无证据）/ `unanswerable`（关键实体都不在库）。**只有后两态才产生缺口**，这样提示不会变成每天两百条的噪声。

## 7. 缺失提示：四类缺口，四种修复入口

这是「提示」能不能落地的关键——只说「数据缺失」没有用，必须一眼看出该点哪个按钮。

| 缺口 | 判定 | 提示长什么样 | 一键动作 |
|---|---|---|---|
| 缺实体 | 问题里的主体/客体在 `entities` 里找不到 | 「未找到实体『AI填单项目二期』。库里有 1 份相似文档《…》，是否抽取建实体？」 | 建实体 / 从文档抽取 |
| 缺关系 | 两端实体都在，边上没有该关系 | 「某某 → 某某乙 无 `reports_to` 边，但事实库有 1 条候选（置信 0.95）」 | 审核建边（走 `ensure_fact_edge`） |
| 缺证据 | 有边但无 `claim_id`/证据链 | 「该边来自历史抽取，无证据可追溯」 | 补证据 / 降级为待核 |
| 缺类型 | 问题需要本体里没有的概念（如「预算」） | 「『预算』不在本体目录，已生成类型提案（待审）」 | 查看提案 → 审 → 发版 |

四类缺口恰好对应四个不同的修复入口（建实体 / 建边 / 补证据 / 改本体），这也是为什么不能把它们压成一个「数据缺失」标签。

## 8. 落地路径（约 6.5 人日，分三段）

| 阶段 | 人日 | 内容 | 验收 |
|---|---|---|---|
| A 骨架 | 2 | 3 张表；`schema_content` v1.1 加 relations 段；CQ CRUD API（`GET/POST/PATCH /ontology/cq`）；控制台 CQ 清单（含 12 条种子 CQ） | 能录入/编辑/按域筛选；种子 CQ 覆盖 lookup/path/temporal 三种意图 |
| B 覆盖与缺口 | 2.5 | 意图解析（确定性优先）+ 五种执行计划 + `POST /ontology/cq/coverage`（可 dry-run）+ 缺口四分类 + 页面缺口列表（带一键动作） | 跑批 < 3s（<100 条 CQ）；每条缺口都有可点动作；两次跑批结果一致 |
| C 自动建议 | 2 | L1 需求抽取 + L2 LLM 提案（受约束 JSON）+ 审核流 + schema diff 视图 | 提案必须带 CQ 依据与例句才允许提交；未审提案不进版本 |

**不含**：schema 自动发布（永远人工确认）、CQ 自动撰写（LLM 可起草，必须人改）。

## 9. 三个必须现在拍板的决策

1. **CQ 由谁写** —— 业务方写最权威但慢；从现有 94 实体/21 边反推最快，但会过拟合。**建议**：我们先反推 12 条种子（其中 4 条刻意写成「现在答不了」），业务方确认后转正。
2. **CQ 覆盖率是否作为发布门禁** —— **建议是**。蓝图 P2 门禁「关键指标达标」里，「首岗位 20 条 CQ 覆盖 ≥70%」比任何自造指标都硬。
3. **关系目录是否数据化** —— 这是「自动建本体」的硬前提。**建议做，但只做「目录数据化 + 版本化」**，不做运行时热加载改约束（热改约束会让历史边突然违规，R2 大面积爆红）。

## 10. 风险清单

| # | 风险 | 说明 | 缓解 |
|---|---|---|---|
| 1 | CQ 过拟合现有数据 | 用已有事实反写问题 → 覆盖率虚高、什么都证明不了 | 种子集必须有 ≥30% 是当前答不了的；覆盖率低不掩盖 |
| 2 | LLM 造无用的类型 | 生成 `has_budget` 之类零实例概念 | 只落 proposal + 必带 3 条真实例句 + 必挂 CQ 依据 + 人审 |
| 3 | CQ 膨胀 | 半年后 300 条没人管 | 强制 priority + owner + retired 机制；每域 5–8 条上限 |
| 4 | 覆盖率变成 KPI 造假 | 把问题写简单以刷分 | 约束意图分布（lookup:path:aggregate ≈ 5:3:2）+ 抽检 |
| 5 | 缺口提示变噪声 | 每条 CQ 报十几个缺口 | 只提示 mandatory 缺口，按修复成本排序，同实体合并 |
| 6 | 与 evaluation 重复建设 | 两套题、两套跑批 | 同表复用：CQ 即评估用例，只多一层「是否可答」判定 |
| 7 | 与 sync 冲突 | 修缺口时手改边，可能与事实投影对不上 | 一律走 `sync` 的四个入口，禁止直插 relations |

## 11. 用现有数据粗算的首轮覆盖率（预期管理）

- **lookup 类**：能答的取决于「主体 + 谓词」是否有事实。464 条事实分布在 94 实体上，多数实体只有 1–3 个维度 → 单条 CQ 命中率尚可，但**换个人就问不出来**（同一问题对不同主体不可移植）。
- **path 类**：图上只有 21 条边、75 个孤立实体 → 绝大多数实体对不连通，path 类 CQ 首轮覆盖率预计 <10%。
- **aggregate 类**：`level / job_title` 等属性类事实存在但被 projection 排除（属性不是边），聚合要走 facts 而不是图 → 反而最容易达标。

**结论**：如果种子 CQ 按「现在能答的」来写，覆盖率会很漂亮但毫无意义。按「岗位真正要问的」写，首轮 20–30% 是正常且可解释的——这个数字本身就是向业务方要数据/要权限的依据。

## 12. 建议

做，但按以下顺序，不要一次铺开：

1. **本周**：把 CQ 表结构 + 12 条种子 CQ 落进 P1 工作包（1 天，只做建模和清单，不做执行）；
2. **P1 中段**：覆盖率跑批 + 缺口四分类（这是真正产生价值的部分）；
3. **P1 末**：L1/L2 提案 + 人审，此时已有真实 CQ 需求作为约束，LLM 才不会乱造；
4. **P2**：把 CQ 覆盖率接进岗位门禁，同时把 CQ 集注册为评估集，结束 evaluation 的零实现状态。
