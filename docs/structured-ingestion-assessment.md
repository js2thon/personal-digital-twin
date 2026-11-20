# 结构化接入技术方案评估：映射契约 · 确定性断言

> 评估日期：2026-11-12　范围：员工汇报关系 / 部门 / 岗位 / 绩效等存储在数据库、湖仓中的结构化数据
> 评估依据：`docs/ADR-architecture-decisions.md`、`docs/knowledge-ontology-spec.md`、
> `docs/decision-authorization-spec.md`、`docs/optimization-review-2026-11-11.md`、
> `docs/fact-deletion-assessment.md`、`README.md` 铁律与冻结项，以及 `backend/app` 现行实现。
> 目的：在动手前确认「结构化接入」这条旁路**不破坏数字分身既定的目标设定与安全铁律**。

---

## 0. 评估结论

**方案整体可行，且与项目目标一致**，但上一轮展示的方案有 3 处必须修正，另有 2 处既有实现的兼容性缺口
必须先补齐。这 5 条不是可选优化，是「符合目标设定」的前提条件。

| # | 修正 / 缺口 | 结论 |
|---|---|---|
| C1 | **信任级从 `owner`(3) 改为 `agent`(2)** | 契约批准的是「规则」不是「值」。用 tier=3 会让下一次同步把本人手工修正过的事实**静默顶回源库的错误值**；用 tier=2 时 `write_fact` 信任护栏原生把这种情况落成 `contested` 等本人裁决，零额外代码 |
| C2 | **默认 dry-run，同步必须本人显式触发 + `confirm=true`** | 目标设定「未经确认自动执行 0」。不提供自动/定时同步，不引入后台无人值守写入 |
| C3 | **绩效默认不落库（L0 只读联邦）** | 避免把高敏感人事数据复制进本地库，从而不在本次引入新的合规擦除债务；物理擦除/tombstone 仍列为待确认冻结项 |
| G1 | **`link_checks.facts_to_memory` 分母必须排除结构化事实** | 该校验用 `active_facts` 做分母、只数带 `claim_id` 且有 `memory_items` 的事实。结构化事实无 Claim、按设计也不进「个人记忆」，一旦落库会把 7/7 全绿拉红 —— 属分母口径错误，不是放宽校验 |
| G2 | **`facts.visibility` 是「已写入但未被消费」的假实现** | `retrieval/service.py:104` 只有 `WHERE f.status='active'`，`knowledge.py:list_facts` 同样未过滤。不先接通，任何 restricted 字段一落库就能被通用问答召回 |

另外两条已在上一轮列出、本次在开发层强制：单值维度 `reports_to` 的 `multi_manager` 必须在接入层预检
（否则 `write_fact` 会静默「取代」成一条）；结构化事实必须带完整 `source_ref` 并新增第 8 项链路校验。

---

## 1. 方案定位：旁路，不是第二条蒸馏流水线

```
现有：source.data_sources → documents → evidence_spans → claims →(人工审核)→ facts
新增：source.data_sources(hr_structured) → 映射契约(本人一次审核并发布)
        → 确定性断言(纯函数，零 LLM) → knowledge.facts / ontology.relations
两条链路在 ontology.write_fact 收口，共用双时间线、检索、实体画像、审计与链路校验。
```

结构化数据是权威源直读，**没有推断、没有不确定性**，因此：
不产生 `semantic_chunks`、不产生 `claims`、不产生 `evidence_spans`（字符区间对数据库行无意义），
证据可追溯改由 `facts.source_ref` 承担（`system / table / pk / batch_id / row_hash / synced_at / mapping_version`）。

**不这样做的代价**（作为方案取舍依据）：把 HR 表导成文本再让 LLM 抽取，会把 100% 准确的字段
降级为约 95% 的推断；汇报关系（边）与绩效（周期度量）被压成一句文本后丢失时序语义；
蒸馏是一次性快照而组织关系持续变化；且当前 `LLM_BASE_URL` 指向企业 OneAPI 网关、
`evidence_spans` 会把原文复制进本地库，等于同时外发并扩散一份员工绩效副本。

### 视角边界：这是「我的数字分身」，不是 HR 数仓

| 数据 | 处置 | 理由 |
|---|---|---|
| 我的部门 / 岗位 / 汇报线、直属团队 | **L1 落库**：facts + relations，走双时间线 | 低频变、需回答「去年谁带我 / 我当时在哪个部门」 |
| 团队绩效明细 | **L0 只读联邦**：不落库，仅显式授权接口按权限查聚合 | 周期快照、高敏感、只需当期；落库即产生删除权义务 |
| 全员花名册 | **不进** | 与「我」无关，仅保留解析汇报链所需的最小集合 |

---

## 2. 逐条核对项目目标设定

| 约束 | 出处 | 核对结论 | 本方案落地方式 |
|---|---|---|---|
| PostgreSQL 是个人知识/认知/规则/决策的**唯一权威存储** | ADR-001 | ✅ 不冲突 | 外部 HR 库是「数据源」，本地 PG 仍是分身的权威存储；`source_ref` 记录回源坐标，`sync_cursor` 记录水位。双时间线用 `valid_from`(何时为真) 与 `recorded_at`(何时得知) 区分源库变更与同步滞后 |
| 模块化单体，依赖方向 `twin → knowledge → distillation → ingestion` | ADR-002 | ✅ 不冲突 | 新模块 `modules/structured/` 属 ingestion 层；`assertions.py` 是**纯函数**（输入契约+行，输出断言列表，不碰 DB / 不调 twin）；落库编排在 API 层调用 `ontology.write_fact`，与现有 `knowledge.py` 的调用方向一致，无反向依赖 |
| **Claim / Fact 分离，禁止把所有 Claim 自动转 Fact** | ADR-003 / 评审铁律 | ✅ 等价替换，非绕过 | 结构化链路**根本不产生 Claim**（无推断），所以不适用「逐条审核」。等价控制点上移为**映射契约审核**：契约必须 `published` 才允许写入，代码级强制（`draft` 直接 400）；契约发布单事务落库、版本化、带 actor 与哈希审计。**绝不用假 Claim 冒充审核** |
| 蒸馏异步状态机 + 幂等键；版本升级生成新版本不覆盖旧版本 | ADR-004 | ✅ 语义对齐 | 不复用 14 状态文档状态机（对象不同），但保留同一幂等语义：键 = `(contract, version, table, pk, row_hash, valid_from)`；值不变 → `write_fact` 返回 `duplicate` 跳过；值变化 → 生成新段 `superseded`，**旧段不覆盖**；契约升版重新同步生成新段 |
| 认知模型版本快照 | ADR-005 | ✅ 不参与 | HR 字段不是个人认知，不生成 `profile_proposals`；因此需 G1 的分母修正 |
| D0~D3，MVP 固定 D2、D3 自动执行恒否 | ADR-006 / 授权规范 | ✅ 不触发但需 C2 | 数据写入不是「决策执行」，不进入 D3 判定。但无人值守的定时同步等价于「未经确认的自动写入」→ 默认 dry-run、显式 `confirm=true`、不提供自动开关；高风险字段一律 `restricted` |
| LangGraph 是唯一编排器，不引入第二个 Agent | ADR-008 | ✅ 强化 | 写入路径零 LLM、零 Agent。LLM 只允许出现在两处：① 把字段注释翻成映射**草案**（人工确认后固化，不直接执行）② 查询时把自然语言翻成只读结构化查询（挂 policy 工具白名单，D3 恒否）。本次实现不含 ② |
| 证据可追溯率 100% | 本体规范 §1 / §8 | ✅ 换承载物 | `evidence_spans.locator` 的字符区间对行数据无意义，改由 `source_ref` 记录库表主键+批次+行哈希+契约版本；新增 `structured_facts_traced` 校验要求 6 个字段 100% 齐备 |
| 基础本体六域、15~25 实体类型，不允许新知识造新类型 | §3 | ✅ | 只复用 `Person / Organization / Role`，不新增类型；工号存 `properties.external_ids`（姓名会重名/改名，不能做锚点），姓名存 `aliases`，改名走 `merged_into` |
| 双时间线 + `asof` 时间旅行 | §9.1 | ✅ 天然契合 | 调岗/换主管 → 单值维度 `superseded` 链；迟到补录 → 现有 `backdated_stint`；离职 → `valid_to` / `withdrawn` 不硬删 |
| 信任分级 owner=3 / agent=2 / unknown=1，低信任永不覆盖高信任 | §9.2 | ✅ **修正 C1** | 结构化写入用 `agent`(2)：契约批准规则、不是批准值。与存量 unknown(1) 可正常取代；与本人手工修正(3) 冲突时自动落 `contested`，绝不静默覆盖 |
| 维度基数单值/多值 | §9.6 | ✅ | 复用 `SINGLE_VALUED` 中已有的 `belongs_to_department / reports_to / manager`；`performance_rating` 保持默认多值（多周期并列）；`multi_manager` 接入层预检 |
| 本体自检 / diagnostics | §9.7 | ✅ 不回归 | 不改 `decide_write` 矩阵与 7 个纯函数用例；新增校验独立成项 |
| 问答有依据、事实召回 | §9.8 | ✅ | 结构化事实与文档事实共用同一 `retrieval._facts`；同时按 G2 先接通 visibility 白名单 |
| 关键事实抽取准确率 ≥95% | §8 | ✅ 提高口径 | 结构化链路**不抽取**：抽样 100 行与源库逐字段比对必须 **100% 一致**，任一不一致即整批回滚并落异常 |
| 高风险越权执行 0 / 未经确认自动执行 0 | §8 / 评审铁律 | ✅ | 无外发动作、无工具执行、无自动同步；只读输入，全部写入发生在本人显式请求内 |
| 消息四重闸门 / 决策 D3 恒否 / Claim 审核闸门 | README 铁律 | ✅ 全部不动 | 新模块不触碰 `REPLY_SEND_ENABLED`、`messages`、`workflow`、`knowledge.approve_claim` |
| 冻结项：Neo4j / Qdrant / Temporal / OPA / 模型微调 / D3 | 蓝图第十五节 | ✅ 不引入 | 只用现有 PostgreSQL + FastAPI + 现有本体服务，外部连接以**可插拔只读 adapter 接口**预留，本次不落任何凭据、不引入中间件 |

---

## 3. 关键冲突的深入说明

### 3.1 C1：为什么不能用 owner(3)

上一轮建议「映射由本人批准 ⇒ 写入用 `writer='owner'`」。核对 `write_fact` 后发现这是错的：
`decide_write` 比较的是**值的信任级**，不是规则的信任级。若同步写入 tier=3，出现以下路径：

```
源库部门字段录错 → 本人手工改成正确值(tier=3) → 下一次同步仍带错误值且 tier=3
→ valid_from 更晚、信任级相等 → superseded：源库错误值顶掉本人修正，且无争议提示
```

改为 tier=2 后，同一路径变成 `contested`，进 `GET /ontology/conflicts` 由本人裁决 —— 这正是
§9.2「低信任永不覆盖高信任」设计要防的那条风险路径。因此 **契约批准规则、同步值按 agent 级写入**，
既是更准确的语义，也让既有护栏原生生效，无需新增 pin/override 机制。

### 3.2 G1：链路校验分母口径

`lineage.overview` 现有：

```sql
active_facts      = count(facts where status='active')
facts_with_memory = count(facts where status='active' AND claim_id IS NOT NULL AND EXISTS memory)
facts_to_memory   = facts_with_memory >= active_facts * 0.8
```

结构化事实没有 `claim_id`，按设计也不会生成「个人记忆」（记忆是个人经历，不是 HR 字段）。
若不改分母，每接入一批组织事实就把覆盖率往下压，`7/7` 全绿会无端变红，且误导为「记忆链路退化」。
修法：分母改为**参与记忆链路的活跃事实**（`claim_id IS NOT NULL`），分子不变、0.8 阈值不变、
不放宽任何口径；结构化事实另行由第 8 项校验覆盖。

### 3.3 G2：visibility 必须 fail-closed

词表：`owner`(默认，本人可见) / `organization`(组织内可见) / `restricted`(敏感)。
检索与事实列表用**白名单** `IN ('owner','organization')`，未知值、NULL 之外的任何值一律按不可召回处理。
`entity_profile` 同样过滤，避免按实体反查绕过检索闸门。本次不实现跨用户权限系统（超出个人分身范围），
只保证 restricted 事实不进入通用问答/画像/列表的默认视图。

### 3.4 合规擦除：本次不新增债务，但保留待办

`withdraw_fact` 是软删，`object_value` 明文仍在库中；个保法删除权需要物理擦除 tombstone 或
字段级加密 + 销毁密钥（见 `docs/fact-deletion-assessment.md`）。本方案通过 **C3 绩效默认不落库**
避开新增敏感副本；若后续决定把绩效落 L1，必须先实现擦除机制。该项继续**冻结待本人拍板**。

---

## 4. 数据模型（新增，全部落在现有 `ingest` schema）

| 表 | 作用 | 关键字段 |
|---|---|---|
| `ingest.structured_contracts` | 映射契约，版本化 | `name, version, status(draft/published/deprecated), source_ref, mapping, contract_hash, published_by/at, actor` |
| `ingest.structured_sync_runs` | 一次同步的不可变留痕 | `contract_id, version, batch_id, dry_run, cursor_before/after, counts, actor, status` |
| `ingest.structured_anomalies` | 异常处置闭环 | `sync_run_id, reason, pk, field, detail, status(open/resolved/dismissed), actor, note, verification, verified_at/detail` |
| `ingest.structured_uploads` | 文件解析后的行快照（表格 / MD / JSON） | `file_name, format, size_bytes, sha256, columns, row_count, rows, warnings, contract_id, uploaded_by` |

契约 `mapping` 结构（本人审核的对象，纯 JSON、可 diff、可回滚）：

```json
{
  "entity": {
    "entity_type": "Person",
    "pk_column": "employee_id",
    "name_column": "employee_name",
    "external_id_prefix": "hr:"
  },
  "fields": [
    {"column": "dept_name", "predicate": "belongs_to_department",
     "object_kind": "literal", "visibility": "organization",
     "valid_from_column": "effective_from", "required": true},
    {"column": "manager_id", "predicate": "reports_to",
     "object_kind": "entity_ref", "target_entity_type": "Person",
     "relation_type": "reports_to", "visibility": "organization", "required": false}
  ],
  "sensitivity": {"default": "organization", "restricted_columns": ["performance_score"]},
  "allowed_values": {"performance_rating": ["A", "B", "C", "D"]},
  "expect_columns": ["employee_id", "employee_name", "dept_name", "manager_id", "effective_from"]
}
```

**`expect_columns` 是 schema_drift 的判定依据**：源库列集合与之不符即整批拒绝，不静默丢列。

---

## 5. 接口与依赖方向

```
GET  /structured/contracts                 契约列表（含状态、版本、哈希）
GET  /structured/contracts/{id}            契约详情
POST /structured/contracts                 创建 draft（校验 mapping，不写事实）
POST /structured/contracts/{id}/publish   发布：draft→published，旧版 deprecated，单事务 + 审计
POST /structured/contracts/{id}/preview   dry-run：只算断言与异常，零知识层写入（默认）
POST /structured/contracts/{id}/sync      confirm=true 且契约 published 才写库
GET  /structured/sync-runs                同步批次留痕
GET  /structured/anomalies                异常列表（可按 status/reason 过滤）
POST /structured/anomalies/{id}/review    resolve / dismiss + 验证结论回填
GET  /structured/reasons                  原因码元数据（谁 / 在哪 / 怎么做 / 怎么验证）
GET  /structured/diagnostics              自检：契约闸门、visibility、链路校验口径
POST /structured/uploads                  上传表格 / MD / JSON：只解析并落快照，不写知识层
GET  /structured/uploads                  上传快照列表（文件名、格式、行数、指纹）
GET  /structured/uploads/{id}             快照详情（include_rows=true 才带行）
DELETE /structured/uploads/{id}           删除行副本（合规入口，不回溯删除已写入事实）
GET  /structured/formats                  支持格式与上限（页面与文档同一口径）
```

接入内核接收**行数据数组**（JSON）作为输入：不落任何外部库凭据、不引入中间件，
真实「直连库/湖仓只读视图」以 `RowProvider` 接口预留，属于后续 adapter，不改变本次内核逻辑。
这符合「私有化 + 只读 + 最小改动」，也让 100% 字段一致性可离线验证。

### 5.1 文件接入：只做「行的载体」，不做语义猜测

湖仓之外，最常见的现实是同事直接给你一份 `xlsx`、导出的 `csv`，或者一段 Markdown 表格。
它们与湖仓取数的区别只在**取数方式**，不在数据性质，因此不新开链路：

```
csv / tsv / xlsx / md / json
        │  解析（编码嗅探、表头规范化、日期与数字归一）
        ▼
ingest.structured_uploads（行快照，带 sha256）
        │  preview / sync 按 upload_id 取行
        ▼
映射契约 → 确定性断言 → write_fact（与湖仓来源完全同一条路径）
```

四条硬约束：

1. **零 LLM、零列名猜测（写入层）**。字段 → 谓词由本人**确认**后生效——一次性导入由
   auto-draft 从数据推导出建议（见下节），人只审一张表；但断言层始终由零 LLM 的
   纯函数执行，模型只产「候选谓词」建议，未被采纳的建议不会进入任何 mapping。
   「上传一个文件」依然不构成绕过 ADR-003 等价闸门的自动抽取通道：没有确认，不写库。
2. **超限拒绝，不截断；常规文件自动分批**。单文件 ≤100 MiB、≤20 万行（进程内存硬上限）、
   ≤512 列、≤600 万单元格（行 × 列）；超字节返回 413、超行/列/单元格返回 400，均提示拆分。
   列数上限只用于拦截「把非表格文件当表格解析」这类异常表头（正常组织数据导出常见
   100~300 列，早期 80 列的阈值会把正常宽表整体拒掉）；真正吃内存的是行 × 列，因此
   另设单元格总数闸门——宽表会自动折算成更小的行数上限，而不是靠列数隐式限制行数。
   超过 **5000 行**由服务端自动
   切批（`BATCH_ROWS`），逐批断言、逐批事务，**行序不变、全部行都写入**，不做任何隐式截断。
   分批的语义变化只有一条且更保守：原「整文件单事务」→「每批单事务 + 失败即停」，
   任一批抛错时该批整体回滚、后续批次不执行，返回 `partial/failed_batch/batches_committed`，
   已提交批次有效（不会留下半批数据）。
   **跨批一致性必须补回来**：`multi_manager`（单值维度多值）与 `cycle_detected`（汇报成环）
   原本在「批内」判定，分批会切断判定范围，因此拆分前先用 `cross_batch_anomalies()`
   对整份文件做一次线性预检，命中的 (主键, 谓词) 在**每一批**里都被过滤掉。
3. **不静默丢弃**。编码回退（UTF-8 → GB18030）、空表头补名、重名列加后缀、多表格取最大并告警、
   多余列告警、嵌套值序列化告警，全部写进 `warnings` 并在控制台展示；列集合仍要与
   `expect_columns` 完全一致，否则照旧 `schema_drift` 整批拒绝。
4. **快照可删除**。行副本确实会留存敏感列，因此提供 `DELETE /structured/uploads/{id}`；
   删除只清副本，不回溯删除已写入事实（事实靠 `source_ref` 自证来源）。

### 一次性导入路径（B 档决策，2026-11-12）

第一性原理拆解后：契约 14 个字段里 **11 个是数据本身就能算出的事实**（主键 = 唯一非空列、
名称列、实体类型、expect_columns、required、object_kind、时间列、敏感列…），**2 个是语义
命名**（列 → 谓词、关系名，可由列名词典预填），只有 **1 个必须人拍板**——这批数据是否入库。
因此一次性导入不再强制「先建并发布契约」：

```
上传 / 粘贴 → POST /structured/suggest（纯推导，不写库）
    │  主键/名称列/类型/必填/时间列/敏感列：规则判定，置信 1.0
    │  常见组织列名：词典命中（department/reports_to/job_title/level/status/hired_at…），置信 0.9
    │  未命中列：问一次模型要候选谓词（25s 超时，失败降级为留空），置信 0.6
    ▼
第二步 · 人审一张表（勾选入库列、改谓词、定可见性）——这一步同时完成「审规则 + 审值」
    ▼
POST /structured/adhoc/preview → 确认 → POST /structured/adhoc/sync（confirm=true）
    │  mapping 由当次请求携带，服务端落一条 ephemeral 契约（status='ephemeral'）作追溯锚点
    ▼
与湖仓来源完全相同的断言 / 幂等 / 可见性 / 分批 / 异常判定 → write_fact
```

- **没有第二条写路径**。adhoc 与 contracts 流程共用 `_prepare / preview / sync`，
  `sync_runs` 里的留痕、链路校验第 8 项、异常复核对两种来源一视同仁。
- **ephemeral 契约按 mapping 哈希幂等**：同一套规则反复导入只落一条记录；具体来源由
  facts.source_ref 的 `file_name / file_sha256 / upload_id` 精确承担。它不可发布、不作为
  后续同步的闸门，只做追溯；需要复用规则（定时同步）时在控制台点「存为可复用契约」，
  走正常的 draft → publish。
- **模型只出现在建议层**。未命中的列由模型给 snake_case 候选，输出经过消毒
  （非法标识符、与已占用谓词冲突的一律不采纳）；模型不可用时整条链路照常工作——
  留空、默认不入库，由人在表里补一行。这维持了「写入可确定性复现」的既有约束。
- **敏感列按完整词段判定**。裸子串会把 manager_id（含 age）判成敏感而静默丢掉汇报
  关系——这类误判比漏判更难发现，英文按词段、中文按子串，绩效/薪资/证件类列默认
  restricted 不落库，与「绩效默认不落库」的合规决策一致。

原「映射契约」流程保留，入口折叠在控制台「复用已有契约」下：湖仓来源、定时同步、
固定表结构反复导入仍然走「契约发布 → preview → sync」，那才是契约的真实使用场景。

放宽字节与自动分批都必须配内存保护，否则单进程会被大文件拖垮：

- **逐行流式解析**：csv/tsv 用生成器喂断言准备，xlsx 用 `iter_rows` 流式取表头后消费；
  命中硬上限立即中止，不先把整表建成 list。
- **分块读取上传体**：接口按 1 MiB 分块并在超限时立刻返回 413，不把上限之外的字节读进内存。
- **响应只回样本**：断言样本 20 条、异常样本 100 条（另给 `*_total`）；异常明细落库单批上限
  500 条，总数仍记在 `counts.anomalies_total`，避免十万级审计噪声。

**为什么存快照而不是让浏览器回传全量行**：万行级别经前端往返既慢又容易与「粘贴 rows」混用；
快照还让「同一份文件重试」只靠 `upload_id` 即可复现同一批断言。文件指纹 `sha256` 与文件名一并
写入 `facts.source_ref`（`file_name / file_format / file_sha256 / upload_id`），
六要素仍齐备，链路第 8 项不会因接入方式变化而变红。

**行来源二选一**：`rows` 与 `upload_id` 同时出现直接 400，避免两份数据拼成一批。

---

## 6. 异常闭环：谁 / 在哪 / 怎么做 / 怎么验证

沿用 `NEEDS_REVIEW` 那套「原因码 → 说明 → 建议动作 → 验证」结构，独立成结构化原因码
（对象不同，不复用 `pipeline_reviews` 表以免与文档任务串台）：

| 原因码 | 触发 | 谁 / 在哪 | 怎么做 | 怎么验证 |
|---|---|---|---|---|
| `schema_drift` | 列集合 ≠ `expect_columns` | 本人 / 结构化数据源 → 契约 | 修订契约并发新版本 | 新契约 preview 零 schema_drift |
| `missing_required` | 必填列为空 | 本人 / 异常列表 | 回源修正或剔除该行 | 下次同步该 pk 无此异常 |
| `entity_unresolved` | 工号/主管锚点找不到实体 | 本人 / 异常列表 | 补主数据或调整锚点 | 实体归一后该行产出断言 |
| `multi_manager` | 单值维度同批次出现多个值（调岗 / 换主管） | 本人 / 异常列表 | 按生效日期分批同步，或回源核对后显式放行 | 该主键该谓词仍只有一条 active，历史段无空洞 |
| `cycle_detected` | 汇报链成环 | 本人 / 异常列表 | 回源修正 | 环检测通过 |
| `enum_invalid` | 值不在 `allowed_values` | 本人 / 异常列表 | 回源修正或更新词表 | 该值不再出现 |
| `restricted_skipped` | 命中 `restricted_columns` | 系统自动 | 按 C3 不落库，仅留计数 | `sync_runs.counts.restricted_skipped` |

**验证结论回填**：`resolve` 后由下一次 `preview/sync` 自动把该异常从 `open` 回填为
`recovered`（不再出现）或 `still_failing`（仍出现），写进 `verification / verified_detail`，
与文档蒸馏的 `verify_retry` 同构，闭环不悬空。

---

## 7. 验收口径

1. **字段一致性 100%**：抽样 100 行逐字段比对源数据，任一不一致整批回滚（`db.transaction()`），不允许「≥95%」。
2. **闸门**：`draft` 契约调用 sync / preview 恒 400；未带 `confirm=true` 自动回落到 preview；preview 零知识层写入（前后 fact / entity / relation 计数不变，仅记一条 dry-run 批次留痕供审计）。
3. **时间旅行**：`entity_profile(id, asof=调岗前)` 返回旧部门，`asof=现在` 返回新部门，取代链连续无空洞。
4. **护栏**：本人手工修正的 fact（tier=3）不被同步覆盖，同步值落 `contested`；`multi_manager` 在写库前被拦截。
5. **可见性**：`restricted` 事实不出现在 `/twin/chat` 检索、`/knowledge/facts` 默认列表、`entity_profile`。
6. **链路**：`link_checks` 保持 7 项原判定不变，新增第 8 项 `structured_facts_traced` 100%；`facts_to_memory` 分母修正后不因结构化事实变红。
7. **审计**：`audit.access_logs` 中契约发布、每次真实同步、每次异常处置均有记录，actor 由服务端注入。
8. **回归**：`pytest` 全绿，既有铁律（外发四重闸门、D3 恒否、Claim 审核）零改动。

文件接入追加 5 条：

9. **格式等价**：同一份数据分别以 csv / xlsx / md 表格 / json 上传，解析出的 `columns` 与 `rows` 必须完全一致（数字不出现 `1001` 与 `1001.0` 两种形态，日期统一 `YYYY-MM-DD`）。
10. **超限拒绝**：>20 万行（内存硬上限）/ >512 列 / >600 万单元格（行 × 列）/ >100 MiB / 未知扩展名 / 空文件 / 只有表头，全部返回明确错误码，且**不产生任何快照或断言**。
11. **自动分批无损**：12000 行文件预览返回 `batch_count=3`、`stats.rows=12000`、
    `assertions_total=36000`（按 3 字段映射），逐批行数之和等于文件行数；
    同步后每批一条 `structured_sync_runs` 留痕，`批次行数之和 == 文件行数`。
12. **跨批一致性**：同一主键在同一单值维度上跨批出现两个值 → `multi_manager` 拦截，
    且该 (主键, 谓词) 在每一批都不产生断言；汇报链跨批成环 → `cycle_detected`，关系边全部丢弃。
13. **分批失败语义**：第 N 批写入抛错 → 该批回滚、后续批次不执行，返回
    `partial=true / failed_batch=N / batches_committed=N-1`，前 N-1 批保持有效。
14. **不静默丢弃**：多表格、重名列、空表头、嵌套 JSON 值必须在 `warnings` 中出现；列集合与 `expect_columns` 不一致时仍走 `schema_drift` 整批拒绝。
15. **可追溯**：文件来源事实的 `source_ref` 必须同时含 `file_name / file_format / file_sha256 / upload_id` 与原有六要素；重复上传同一文件幂等为 `duplicate`，同名改内容则因 `row_hash` 不同而产生新事实。
16. **可删除**：`DELETE /structured/uploads/{id}` 后快照不可再被 preview/sync 引用（400），已写入事实不受影响；删除动作有审计记录。

---

## 8. 明确不做（冻结 / 待本人拍板）

- 绩效明细落库与跨用户权限体系（C3；需先定合规擦除口径）
- 物理擦除 / tombstone / 字段级加密（合规项，待拍板）
- 直连生产 HR 库或湖仓的 adapter、凭据托管、CDC / binlog 增量（接口预留，本次只做只读行输入内核）
- 文件接入的 xlsx 多工作表批量导入、PDF / Word 表格识别（当前取首个工作表并告警，避免「悄悄少一半数据」）
- 定时自动同步（与「未经确认自动执行 0」冲突）
- Text-to-SQL 自然语言查询（涉及 policy 白名单与只读视图，需单独评审）
- 蓝图第十五节全部冻结项（Neo4j / Qdrant / Temporal / OPA / 模型微调 / D3 自动执行）

---

## 9. 风险登记

| 风险 | 等级 | 缓解 |
|---|---|---|
| 源库错误值污染分身事实 | 中 | tier=2 + 本人修正 tier=3 → contested；`row_hash` + 批次留痕可整批追溯 |
| 敏感字段被通用问答召回 | 高 | G2 visibility 白名单 fail-closed + C3 默认不落库 |
| 契约被误改导致批量错写 | 中 | 版本化 + 发布闸门 + `contract_hash` + preview 默认 + 同事务审计 |
| 链路校验假绿/假红 | 中 | 第 8 项用真实覆盖率；分母修正公开在 `link_details`，不放宽阈值 |
| Schema 漂移静默丢列 | 中 | `expect_columns` 整批拒绝，不做「能映射多少算多少」 |
| 单值维度静默取代 | 中 | `multi_manager` / `cycle_detected` 接入层预检 |
| 同名实体分裂（同一人两个画像） | 高 → 已闭环 | 文档蒸馏建的实体无 `external_ids`，结构化路径原先只按锚点查，导致某某/某某丙各建两档。现 `_ensure_entity` 增加名字归一：锚点查不到时按「类型 + 规范化名称」认领已有实体，把新锚点与别名并入（`_attach_external_id`），**只加标识、不自动合并两个既有档案**，避免真重名被误并；存量重复用 `status='merged' + merged_into` 标记合并，事实与关系整体迁移，可追溯 |
| 自动推导把事件列当实体锚点 | 中 → 已闭环 | HR 调动单样例曾以 `action_code` 为主键、`action_cn_name`（值"调动"）为名称列。现主键分强/弱词表并排除事件类列（action/event/操作…），名称列按「精确 name > 强特征词 > 人名值启发」判定；布尔/编码/日期/数字/空列与名称类谓词解耦；谓词冲突按「列名更直接者赢」裁决；推导结果在前端「确认实体」卡由本人核对后才写入 |
