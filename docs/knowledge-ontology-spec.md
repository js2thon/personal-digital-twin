# Knowledge & Ontology Specification（摘要）

## 1. 知识模型链路
source.data_sources → source.documents → knowledge.evidence_spans → knowledge.claim_evidence → knowledge.claims → facts / ontology.relations / memory.memory_items

- 每条已发布知识必须可追溯证据（evidence_spans.locator 记录字符区间/页码）。
- 同一 content_hash 只入库一次（重复同步不产生重复知识）。

## 2. Claim 类型
fact / preference / belief / event / relation / experience。

## 3. 基础本体（v1.0.0，六域）
| 领域 | 实体类型 |
|---|---|
| Identity | Person, Organization, Role |
| Knowledge | Knowledge, Skill, Domain, Concept |
| Experience | Experience, Project, Event, Achievement |
| Cognition | Belief, Preference, Value, Principle |
| Decision | Decision, Option, Criterion, Rule |
| Relationship | Person, Relationship, Communication |

15~25 个实体类型；不允许每次新知识产生新实体类型。

## 4. 实体合并
不删除原实体：aliases 扩充 + merged_into 重定向，保证历史证据可追溯。

## 5. 本体扩展流程
新 Claim 无法匹配现有类型 → LLM 提出候选 → Schema 校验 → 与已有类型冲突检查 → 人工审核 → 发布新版本本体（旧版本保留，需兼容性检查）。

## 6. 记忆与偏好
- 偏好必须有适用上下文（scope）与生效时间，非绝对要求。
- 同一材料的重复转发不算多条独立证据。
- 生命周期：candidate → 独立证据收集 → 场景一致性检查 → 用户确认（confirmed）→ deprecated。

## 7. 反馈分流
fact_error → 知识修正；cognition_mismatch → Profile Review；judgment_mismatch → Decision Evaluation。MVP 不做在线强化学习，只做可审计的数据更新。

## 8. 评估指标（首期目标）
知识证据可追溯率 100%；关键事实抽取准确率 ≥95%；实体消歧准确率 ≥95%；问答有依据比例 ≥95%；回复事实错误率 ≤2%；高风险越权执行 0；未经确认自动执行 0。

## 9. 事实可信度：双时间线 · 信任分级 · 争议裁决

借鉴 GBrain v0.42 Life Chronicle 的三条地基，落在 `knowledge.facts` 上（`app/modules/ontology/service.py`）。

**9.1 双时间线**
- `valid_from` / `valid_to`：这个值「何时为真」（有效时间）；
- `recorded_at`：我们「何时得知」（记录时间）。
- `asof` 查询按有效时间切片，历史回填不会被误当成新观测。

**9.2 信任分级（写入者 → 信任级）**
| 写入者 | trust_tier | 来源 |
|---|---|---|
| owner（本人在控制台审核） | 3 | Claim 审核通过 |
| agent（蒸馏 / LLM 推断） | 2 | 观察写入、抽取管线 |
| unknown | 1 | 无法识别的来源（fail-closed） |

`POST /api/v1/ontology/observations` 默认以 agent 身份写入。

**9.3 写入决策（所有事实写入统一收口 `ontology.write_fact`）**
| 情形 | 动作 |
|---|---|
| 无历史值 | inserted，成为当前值 |
| 与当前值相同且信任级不更高 | duplicate，幂等跳过 |
| 与当前值相同但信任级更高 | reinforced，提升当前值可信度 |
| 信任级 ≥ 当前值，且生效时间更晚 | superseded：旧段 `valid_to`=新段 `valid_from`，`superseded_by` 指向新事实 |
| 信任级 ≥ 当前值，但生效时间更早 | backdated_stint：作为更早历史段保留，结束于其后最早一段的生效时间 |
| 信任级 < 当前值 | contested：**不覆盖**，挂起等 owner 裁决 |

**9.4 争议裁决**
- `GET /api/v1/ontology/conflicts`：列出争议值 + 当前值（含双方信任级）；
- `POST /api/v1/ontology/conflicts/{fact_id}/resolve` `{action: accept|reject}`：
  accept → 该值成为当前值并关闭旧段；reject → `withdrawn`，旧值不动；
- 状态词表：active / superseded / contested / withdrawn，任何一步都写审计日志。

**9.4.1 撤销发布（软删除）**
- `POST /api/v1/knowledge/facts/{fact_id}/withdraw` `{reason?: string}`：仅 `active` 可撤销；
  单事务内 `status → withdrawn`、`contested=FALSE`、`valid_to=COALESCE(valid_to, NOW())`，
  在 `source_ref` 追加 `withdrawn_by / withdrawal / reason / at`，并写 `audit.access_logs`。
  **不删行**，重复撤销返回 400。
- `GET /api/v1/knowledge/facts?status=withdrawn` 列出撤销留痕；列表与事实召回只查 `active`，
  撤销后立刻从用户视图、检索与问答证据中消失，可见效果与删除一致。
- 与物理删除的区别：决策证据 `reference_id`、不可变审计快照、`superseded_by` 仍按 id 可解析；
  `MAX(version)+1` 不复用版本号；`asof` 时间线无空洞；`lineage.claims_to_facts` 用 EXISTS
  不限 status，链路校验不变红；`entity_profile` 的 `withdrawn` 桶保留留痕可查。
- **不复用 `superseded`**：它表示「被新值取代」，需 `superseded_by` 指向替代事实，且会作为
  「曾经为真」的历史段进入时间线，语义与撤销不符。
- 物理删除仅保留在文档级联删除与合规擦除场景。级联删除现已先收集 fact_id 清理
  `decision_evidence` 并解掉指向这些事实的 `superseded_by` 软指针，不再留下悬空引用。

**9.5 实体画像**
`GET /api/v1/ontology/entities/{id}/profile?asof=&min_confidence=`：维度 → 当前值 + 历史段 + 争议值 + 出入关系，
零 LLM 返回，供回复起草与问答直接消费。`GET /api/v1/ontology/dimensions` 为元本体统计（在跟踪哪些维度、覆盖多少实体、多少争议）。

**9.6 维度基数（单值 vs 多值）**
- 单值维度（部门、职位、职级、汇报关系、状态等，见 `SINGLE_VALUED`）：同一时间只能有一个值为真，
  才使用「新值取代旧值」与取代链；
- 多值维度（观点、参与、提出、负责等，**默认**）：不同客体是并列断言，只做「同值幂等 + 同值提升信任级」，
  绝不关闭其它活跃值；
- 保守原则：把单值误判成多值只会多出可见的重复，把多值误判成单值会**静默丢掉知识**，因此未声明的一律按多值处理。
- 实体画像里每个维度都带 `cardinality`：单值给 `current` + `history`（取代链），多值给 `values`（活跃取值集合）。

**9.7 自检与验证**
本体层最容易「看起来生效、其实没生效」（列加了没回填、回填了 asof 是空的、判定逻辑被改坏）。
自检把可验证的断言集中成一份报告，页面与 CLI 共用同一结论：

- 后端：`GET /api/v1/ontology/diagnostics`，逐项给出 通过/待修 + 明细：
  1. facts 表具备双时间线与可信度列；
  2. 信任级 / valid_from / confidence 回填完整性；
  3. 写入判定矩阵（7 个纯函数用例：inserted / duplicate / reinforced / superseded / contested / backdated_stint）；
  4. asof 时间旅行（3 个探测点必须取到正确历史值）；
  5. 单值/多值维度区分；
  6. 多值维度并列成立（若存在被旧规则误取代的事实，给出待恢复数量与恢复入口）；
  7. 争议与取代链可查询；8. 信任级分布。
- CLI：`python scripts/verify_ontology.py`（只读，不改数据；`--json` 输出机器可读结果），
  另附「真实数据时间旅行抽样」：对比 asof=现在 与 asof=2000-01-01 的单值维度当前值，并打印取代链。
- 页面：本体中心 → 「本体自检」卡（含一键恢复被误取代多值事实，两次点击确认制）
  与 「实体画像 · 时间旅行」卡（选实体 + asof + 最低置信度，查看该时点画像）。

时间旅行语义补充：单值维度按「生效区间包含 asof」取当前值；多值维度每条断言各自判断
`valid_from ≤ asof < (valid_to 或至今)`，互不作边界——回看过去不会把今天的观点带回去。
`asof` 参数接受不带时区的时间（按服务器本地时区解释）；历史曾因 naive/aware 比较抛 TypeError 导致画像 500，已修复并加回归测试。

**9.8 问答的本体参与度验证**
问「一个问题是否调用了本体层语义」不再靠猜，回答自带可核对的检索明细（`/twin/chat` 返回 `retrieval` 字段，
页面在回答下方以「本次检索」折叠区展示）：

- `entity_bound`：命中事实里带 `subject_name` 的条数——这个名字只有 join `ontology.entities` 才有，
  大于 0 即证明本体层参与了召回；`ontology_used` 为汇总布尔值；
- `mentions`：问题里被本体识别到的实体（`detect_mentions` 用 canonical_name + aliases 做 mention 识别，
  权重 3.0 置顶）——这是本体参与「查询理解」的入口，没有它人名会被「事情」这类常见词压过去；
- `facts`：逐条列出 `主体 · 谓词 = 值`（主体为归一后名称），`dimensions` 统计命中维度。

配套修复（均为实测发现的真实缺陷）：
1. LangGraph 条件边函数对 state 的写入不回传，首轮命中证据时 `evidence_sufficiency` KeyError → 问答 500；
   判定移入 `_retrieve` 节点。
2. 事实值短（谓词英文 + 短中文值），中文 2-gram 权重 0.55 达不到 `_MIN_SCORE=2.0`，事实永远召不回；
   `_facts` 改为值/主体名/谓词三路打分并放大权重（`_FACT_BOOST=4`），一个实义 2 字词即可召回。
3. 主体名命中加权 ×2：问「某人负责什么」时，该实体的事实排在最前。

验证方式：页面上直接提问看「本次检索」；或
`curl -s -X POST http://127.0.0.1:8101/api/v1/twin/chat -H "Content-Type: application/json" -d '{"message":"某某丙负责哪些事情？"}'`
看返回的 `retrieval.ontology_used / entity_bound / mentions`。

**9.9 链路追踪（页面动态呈现）**
数据 → 知识 → 本体语义 → 生成的全链路不再黑盒：一次请求的每个节点记录
`{node, status, detail, ms}`（`app/modules/twin/trace.py`，起点时间用 ContextVar 保存，
避免被 LangGraph 的 TypedDict state 过滤掉），随回答/草稿/决策一并返回，页面渲染成
逐步点亮的步骤条（颜色即状态：绿=命中，灰=未命中/跳过，黄=降级或提醒，红=被护栏拦下，
蓝=待人工确认；悬停看详情，节点间标耗时）。

- 问答（`/twin/chat`）：意图识别 → 知识检索（事实 N 条·本体归一 M）→（补充检索）→ 生成回答 → 引用校验；
- 回复起草（消息扫描）：知识检索 → 起草回复 → 承诺检查，trace 随草稿落库
  （`workflow.message_replies.trace` JSONB），草稿卡直接点亮；
- 决策（`/twin/decision`）：证据检索 → 决策分析 → 策略检查 → 落库审批。
- 「本体归一 M」即 9.8 的 entity_bound：M > 0 说明本体层语义在该请求里真实生效。

**9.10 链路详情页（证据级完整回放）**
9.9 的步骤条只回答「哪个节点生效了」，本节把快照升级到证据级：每次问答 / 回复起草 / 决策
把四层完整明细落库到 `workflow.pipeline_runs`（nodes + stages JSONB），页面「链路详情」按次回放：

- ① 数据层 · 输入与查询理解：原始输入、检索范围、分词词项、是否启用向量重排、
  本体实体识别结果（命中谁、实体类型、别名）；
- ② 本体层 · 实体与维度：命中的实体（ontology.entities）、命中维度（谓词 × 次数）、
  涉及实体的关系链（ontology.relations 双向查询，含状态）；
- ③ 知识层 · 召回明细：事实逐条（主体 / 维度 / 值 / 信任级 / 置信度 / 生效区间 / 相关度），
  其中主体与信任级只有经 ontology 层才有——这是「本体语义生效」的直接证据；
  记忆（类型 / 内容 / 置信度）与文档切片（标题 / 内容 / 得分 / 召回方式 lexical|lexical+vector）；
- ④ 生成层：问答给最终回答 / 引用编号 / 充分性 / 是否降级；回复起草给草稿全文与待确认项；
  决策给建议 / 置信度 / 风险级 / 策略结果与理由 / 进入决策的证据。

实现：`app/modules/twin/pipeline.py`（record_run / stages_from_qa|reply|decision / entity_relations），
`GET /api/v1/pipeline/runs`（列表，支持 kind 过滤）与 `/pipeline/runs/{id}`（详情）。
问答与决策响应新增 `run_id`，对话与决策中心在步骤条下附「🔍 查看完整链路」按钮直跳详情；
消息扫描生成的草稿在生成时自动落库一次 reply 运行。
detect_mentions 现返回实体 id/类型/别名（mention_entities），facts 检索带出
trust_tier / confidence / valid_to / subject_id / fact_id 供快照引用。
追踪失败绝不阻塞主链路（record_run 吞异常）。

**9.10.1 链路详情 UI（Astryx 流水线样式）**
详情区按流水线隐喻重排：左侧纵向连接线（四层语义色渐变）串起圆形图标节点
（线性 SVG：数据=下载 / 本体=图结构 / 知识=书 / 生成=星芒，语义色软底 + 同色左边条），
每层顶部先给**重点指标块**（大数字 + 标签：检索词项 / 实体识别 / 命中实体 / 维度 / 关系链 /
事实（本体归一 N）/ 记忆 / 文档 / 证据块 / 引用 / 充分性 / 待确认项 / 风险级 / 置信度），
再展开明细；输入问题与待回复消息用引用块突出，命中实体用语义色高亮 chip（成功色软底）。
详情顶部新增总览条：`数据层 N → 本体层 N → 知识层 N → 生成层`，一眼看清链路瓶颈所在层。
全部样式只用 Astryx 令牌（语义色、间距、圆角、focus ring），明暗主题自适应。

**9.10.2 回形针蛇形布局 + 折叠摘要 + 详细弹窗（当前版）**
- 布局改为两列网格蛇形（回形针）流水：第 1/2 层从左到右、第 3/4 层从右到左往返折叠；
  卡内圆形图标节点（数据=下载 / 本体=图结构 / 知识=书 / 生成=星芒）随排向置于左/右，
  同排卡片之间用横向箭头连接，换排处用纵向折返箭头（宽 48px 间距 / 行距 64px，
  连接线取当前层语义色）。窄屏（≤760px）自动退化为单列并隐藏箭头。
- 卡片默认**折叠**：只显示重点指标块 + 一行式摘要（输入引用截断 120 字、命中实体/
  维度 chip、事实「主体 · 谓词」chip、产出截断 140 字）+ 右下「🔍 查看详细内容」按钮。
- 点击「查看详细内容」弹出**加宽弹窗**（modal.wide，960px），完整回放该层全部证据
  （明细表格 / 记忆 / 文档切片 / 引用校验等）；支持关闭按钮、点遮罩关闭、ESC 关闭。
  完整事实表、记忆、文档切片内容照旧，仅从页内平铺改为按层弹窗按需查看。
