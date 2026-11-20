# 蒸馏任务 `NEEDS_REVIEW` 处置规范（2026-11-12）

> 一句话结论：`NEEDS_REVIEW` 不是「失败」，而是**流水线跑完了但产物不合格，需要本人做一次判断**。
> 本文档回答：这个状态怎么来的、接下来谁操作、在哪里操作、怎么做、做完怎么验证。

## 1. 这个状态是什么

流水线状态机：

```
PENDING → INGESTING → PARSING → CHUNKING → EXTRACTING → VALIDATING
        → ENTITY_RESOLUTION → KNOWLEDGE_REVIEW → PUBLISHING → COMPLETED

异常分支：RETRY_WAIT（自动重试） / FAILED（技术故障） / CANCELLED
         / NEEDS_REVIEW（产物不合格，等人判断）
         / REVIEW_DISMISSED（本人已判定无需处理，终态）
```

`NEEDS_REVIEW` 与 `FAILED` 的区别：**FAILED 是系统故障，NEEDS_REVIEW 是内容/模型结果需要人确认**，
所以前者可以自动重试，后者必须有一次显式的人工结论，不能靠定时重试蒙混过去。

### 产生原因（`error_info.reason`）

| 原因码 | 中文 | 含义 | 重蒸是否有效 | 建议动作 |
|---|---|---|---|---|
| `no_chunks` | 原文无有效切片 | 空文件、扫描件未 OCR、内容被清洗为空 | ❌ 重蒸结果一样 | 检查/重传源文件 → 判定无需处理 |
| `no_claims` | 模型未抽出主张 | 切片正常、模型正常返回 `[]`（多为通讯录、流程图、模板等无「本人」知识的文档） | ⚠️ 视情况 | 本人确认内容 → 判定无需处理；或换模型后重蒸 |
| `invalid_json` | 模型输出非 JSON | 模型返回解释文字/代码块，无法解析 | ✅ | 换模型或调 `EXTRACT_SYSTEM` → 重新蒸馏 |
| `llm_unavailable` | LLM 端点不可用 | 网络错误、超时、网关 5xx | ✅ | 修端点 → 重新蒸馏 |
| `unknown` | 未分类原因 | 缺少可识别 reason | ✅ | 看原始诊断后决定 |

**历史修正**：旧版 `chat_json` 在 JSON 解析失败时静默返回 `[]`，导致 `invalid_json` 一律被记成
`no_claims`，两类完全不同的故障无法区分；同时只要**任意一个切片**抛异常就把整批成功抽取的
主张全部丢弃。现在按切片统计成功/失败，端点故障与格式错误分别上报，部分失败但仍有产出时
任务继续 `COMPLETED`，只在 `error_info.warning=partial_extract` 与步骤日志里留痕。

## 2. 谁来操作

**只有本人（owner）**。身份由服务端 `auth.current_actor` 从 TCP 对端 / `X-Twin-API-Key`
判定后注入，**不接受请求体自报 reviewer**，每次处置都把 actor 写进
`ingest.pipeline_reviews` 与 `audit.access_logs`。

系统不自动替人做这个决定：不存在「定时扫描 → 自动关闭」或「自动重试到消失」的路径。

## 3. 在哪里操作

控制台：**上传文档 → 蒸馏任务状态 → 该行「复核」按钮**（侧栏「蒸馏任务」会亮出待复核数量角标，
列表右上角可切「仅待复核」筛选）。

对应接口：

| 用途 | 接口 |
|---|---|
| 待复核汇总（角标 / 原因分布） | `GET /api/v1/documents/runs/needs-review` |
| 复核详情（诊断 + 产物 + 步骤日志 + 历史与验证结论） | `GET /api/v1/documents/runs/{run_id}/review` |
| 提交处置 | `POST /api/v1/documents/runs/{run_id}/review`，body `{action: retry\|dismiss, note?}` |
| 任务列表（含 `error_info`、原因标签、产物计数） | `GET /api/v1/documents/runs/recent?limit=500` |

复核弹窗里能看到：原因解释、处置人/位置/建议动作、切片数/主张数/候选数/步骤数、
原始诊断 JSON、按原因分流的处理步骤、流水线步骤日志、历史处置与验证记录。

## 4. 怎么操作

只有两个动作，都是点击即生效、无原生弹窗：

| 动作 | 做什么 | 状态变化 | 适用场景 |
|---|---|---|---|
| **重新蒸馏** `retry` | 先做一次 LLM 端点预检 → 按状态机重置 `PENDING` → 重新派发流水线 | `NEEDS_REVIEW` → `PENDING` → `COMPLETED` / 新的 `NEEDS_REVIEW` / `FAILED` | 端点已恢复、换过模型/Prompt、认为文档应能抽出知识 |
| **判定无需处理** `dismiss` | 本人确认该文档不需要进入知识库；**不删任何行** | `NEEDS_REVIEW` → `REVIEW_DISMISSED` | 空文件、通讯录/模板类无个人知识的内容、重复导入 |

约束：

- 只有 `NEEDS_REVIEW` / `REVIEW_DISMISSED` 两个状态可处置；其它状态返回 400，活动态并发双蒸被状态机挡住。
- `REVIEW_DISMISSED` **允许反悔**：之后仍可从复核台重新发起蒸馏，或回到「我的钉钉数据」重新提交；
  人工结论不是永久锁死。
- 预检不通**不阻断**操作（`no_chunks` 等场景本就不需要 LLM），但会明确告警并写进留痕。
- 所有处置都在单事务内完成「状态更新 + 复核记录 + 审计日志」，中途失败整体回滚。

## 5. 怎么验证

三层验证，避免「点了按钮但不知道好没好」：

1. **重蒸前预检**：`llm_preflight()` 做一次最小 JSON 调用，结果 `preflight.ok` 随接口返回并写入
   `verified_detail`；不通时前端直接给出警告。
2. **重蒸后自动回填**：流水线每次结束（`COMPLETED` / `NEEDS_REVIEW` / `FAILED`，含 worker 异常退出）
   都会把最近一条 `retry` 记录从 `pending` 回填为：
   - `recovered`（已恢复）：**状态 COMPLETED 且 `published > 0`** —— 真的产出了知识；
   - `still_failing`（仍失败）：其它任何情况，包含「不再报错但依旧 0 条主张」。
3. **人工复核**：重蒸完成后再次打开该行「复核记录」，对照
   「处置与验证记录」里的验证结论、产出主张数、以及诊断明细里的切片/主张计数。
   判定口径见 `review.verification_result()`。

原始诊断不会丢：重蒸会清空 `error_info`，因此复核动作会把 `original_reason` 写进
`error_info.last_review`，`classify()` 可回退读取，保证「当初为什么待复核」始终可追溯。

## 6. 留痕与审计

`ingest.pipeline_reviews` 每次处置一行：`action / reason / note / actor / before_status /
after_status / verification / verified_at / verified_detail / created_at`；同时写
`audit.access_logs` 的 `ingest.run.review.retry` / `ingest.run.review.dismiss`。

该表**故意不建到 `pipeline_runs` 的外键**：级联删除任务时保留本人处置记录，审计链不会因为删任务而断。

## 7. 当前库内现状（2026-11-12）

共 14 条 `NEEDS_REVIEW`：`no_claims` 10 条、`llm_unavailable` 3 条、`no_chunks` 1 条；
`COMPLETED` 28 条、`PENDING` 2 条。企业 LLM 网关当前预检 **可用**（`ok=true`），
因此 `llm_unavailable` 那 3 条具备直接重蒸的条件；`no_claims` 的 10 条建议先由本人确认文档内容
再决定关闭或换模型重蒸；`no_chunks` 那条需要先检查源文件。

**本次改动没有自动处置这 14 条中的任何一条** —— 判断权留给本人。

## 8. 不变的安全约束

- Claim 审核闸门不变：蒸馏只产出 `candidate`，仍需人工「转为事实」才成为 Fact。
- 消息外发 `confirm:true, live:true` 人工确认闸门、D3 恒否、工具白名单均未改动。
- 复核只改任务状态与留痕，不删除文档、切片、证据、事实或审计记录。
