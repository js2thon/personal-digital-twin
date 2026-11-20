# 全链路逻辑梳理与优化评审（2026-11-11）

> **状态更新（2026-11-12）**：评审清单已**全部清零**。第一批 H1–H4 / M1–M6 / M8–M12 / L1/L2/L4/L5/L6/L9/L11；第三批 M7 + L3/L7/L8/L10/L12/L13/L14/L15。回归：pytest **22 passed**、接口与浏览器遍历 8 菜单无 JS 报错、证据去重 797→88 且引用零孤儿、`/docs` 与 API 在伪造 XFF 下按预期放行（本机身份不被代理头覆盖）。
>
> **状态更新（2026-11-11 晚）**：高优先级 H1–H4、中优先级 M1–M6/M8–M12、低优先级 L1/L2/L4/L5/L6/L9/L11 已全部修复并通过回归（pytest 19 passed + 接口实测 + 浏览器遍历无 JS 报错）。剩余低优先级 L3/L7/L8/L10/L12–L15 择机处理；M7（反代信任边界）在纯本机部署形态下暂不阻塞，暴露到网络前必须先修。

对照 `docs/ADR-architecture-decisions.md`、`docs/knowledge-ontology-spec.md`、`docs/decision-authorization-spec.md` 与 README，逐模块通读 `backend/app` 后的结论。

## 一、总体判断：架构骨架与铁律均成立

| 铁律 | 验证结果 |
|---|---|
| 消息外发四重闸门（approved + confirm + 总开关 + live） | ✅ 无绕过口，含原子抢占、稳定幂等键、失败回滚、全链路审计 |
| 决策 D3 自动执行恒为否 | ✅ 配置默认 False + policy 双重短路 + 执行前恒 403 |
| QA 证据不足拒答 | ✅ 两轮补充检索后仍不足即拒答，引用编号越界剔除正确 |
| Claim 未经审核不得成 Fact | ✅ 全部 claim 恒为 candidate 等人工 approve |
| 审计快照与决策同事务落库 | ✅ `db.transaction()` ContextVar 复用实现正确 |

当前数据规模：42 文档 / 41 任务 / 271 切片 / 797 Claim / 341 事实 / 93 实体 / 526 记忆 / 516 认知候选 / 11 决策。`link_checks` 仅剩「决策落库」未打通（approvals=0，属演示数据问题，非代码缺陷）。pytest 19 passed。

## 二、高优先级（建议立即修）

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| H1 | `api/v1/documents.py:41-53` | 蒸馏入口按幂等键查到已有 run 即返回去重，**不检查状态不派发**：FAILED/NEEDS_REVIEW 的任务在前端亮起「重新蒸馏」，点击后实际什么都不做 | 查到已有 run 时：非 COMPLETED/活动态 → 重置 PENDING+retry_count=0 并重新派发；仅 COMPLETED 才 deduplicated |
| H2 | `modules/distillation/service.py:79-93` | Claim 幂等用 `subject::text = :s` 逐字节比较，依赖 JSON key 顺序，LLM 输出顺序稍变即判不重复 → 重蒸馏产生成倍重复 Claim/Evidence | 改 `c.subject = CAST(:s AS jsonb)`（jsonb 等值对 key 序不敏感），一行修死 |
| H3 | `modules/ontology/service.py:269-287` | 无主体（subject_id=NULL）事实写入**完全绕过信任护栏**：低信任观察可静默作废 owner 确认过的事实，不留 contested | 无主体分支也走 `decide_write` 决策矩阵；低信任异值 → 插 contested，绝不 UPDATE 既有行 |
| H4 | `workers/pipeline_tasks.py:223-230` | `reprocess` 对**执行中**的 run 也重置 PENDING → eager 线程与 worker 并发双蒸，切片 DELETE+INSERT 竞态 | 仅 FAILED/NEEDS_REVIEW/COMPLETED/CANCELLED 才重置；活动态返回 already_running |

## 三、中优先级

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| M1 | `modules/audit/service.py` | 快照「防篡改」是无密钥裸 SHA256，同库存储可改后重算 | 改 HMAC-SHA256（掺服务端密钥）或哈希链，成本极低 |
| M2 | `modules/distillation/service.py:45-73` | 实体消歧 LLM 解析失败静默走「新建实体」→ 重复实体；match_id 未验证存在性就挂别名 | 解析失败不建实体；写 alias 前先验证实体存在 |
| M3 | `api/v1/twin.py:141-147` | 决策 fact 证据 `reference_id` 恒 NULL（取 `d.get("id")` 而实际字段是 `fact_id`）→ 文档删除后 fact 证据失效检测失明 | `d.get("id") or d.get("fact_id")` |
| M4 | `modules/twin/graphs/reply.py:34-38` | 回复起草的 context 不含检索到的 facts，「基于个人知识作答」只靠文档/记忆 | facts 以「主体 · 谓词 = 值」并入 context |
| M5 | `modules/twin/graphs/qa.py:191` | QA 降级分支（LLM 不可用）实际返回证据摘要，trace 却标「blocked 拒答」 | 改为 degraded + 正确文案 |
| M6 | `modules/ontology/service.py:436-451` | 争议裁决 accept 时无条件关闭当前值，回溯型争议会制造 asof 空洞区间 | 复用 write_fact 的 backdated 判定 |
| M7 | `app/auth.py`、`main.py` | 身份闸门按 TCP 对端地址判本人：被反代后远程访问者全被视作 owner；`/docs`、`/openapi.json` 在闸门外 | 显式信任代理开关；/docs 纳入闸门；`testclient` 判定挪到测试 |
| M8 | `workers/pipeline_tasks.py:186-195` 等 | 蒸馏切片逐条 INSERT、无显式事务（慢 + 非原子） | `with db.transaction()` + 批量 INSERT |
| M9 | `modules/retrieval/service.py`、`lexical.py` | 每请求 facts×chunks×memory 三张全表 ILIKE 扫描 + 500 行实体内存匹配，万级数据后 P95 恶化 | 建 pg_trgm GIN 索引（entities/entities/chunks/memory）；detect_mentions 加 TTL 缓存 |
| M10 | `dws_source/distill_status.py` | 蒸馏状态比对每次全量 JOIN；`pipeline_runs` 缺 document_id 索引、`documents` 缺 external_id 索引 | 补两个索引 + 5~10s TTL 缓存 |
| M11 | `modules/ingestion/storage.py:145-147` | 无 MinIO 时原文内联静默截断 20 万字符，长文档蒸馏缺后半部且无告警 | 截断写 `_inline_truncated` 标记并在任务结果提示 |
| M12 | `api/v1/ontology.py:232-247` | `ontology/publish` 宣称的兼容性检查未实现，且允许多版本同时 published（假实现） | 事务内校验 draft + 旧版本置 deprecated |

## 四、低优先级（择机处理）

- L1 软超时重试路径不查 retry_count，可能卡 RETRY_WAIT
- L2 reject_claim 更新 0 行仍返回 rejected；L3 认知审批并发撞 UNIQUE(version) 500
- L4 `_intent` 无条件覆盖调用方 retrieval_scope，API 参数失效
- L5 `/twin/reply` 不落链路快照、不写审计（与 chat/decision 不对称）
- L6 DWS 下载临时目录从不清理；L7 read_sheet 硬编码 A1:Z200 截断
- L8 上传无大小上限；L9 一批分页 limit 未钳制（list_approvals 全表返回）
- L10 内存会话与 DWS 缓存无界增长；L11 get_engine 双检无锁
- L12 同一 chunk 的 N 条 Claim 复制 N 份全文进 evidence_spans
- L13 工具白名单多数条目无执行点（装饰性）；L14 config 默认口令 fallback
- L15 KNOWLEDGE_REVIEW 阶段的 0.7 阈值只统计不消费（假动作）

## 五、建议动手顺序

1. **H1 + H2**（各一行级改动，直接消除「失败任务无法重蒸」与「重复知识」）
2. **H4 → H3**（并发安全 + 信任护栏补洞）
3. **M3 / M4 / M5**（小改动大收益：决策证据引用、回复草稿质量、trace 准确性）
4. **M1 + M2 + M6**（审计强度、实体消歧健壮性、裁决正确性）
5. **M8 / M9 / M10**（索引 + 批量写，为数据量增长做准备）
6. 低优先级按需清理

安全闸门全程不放宽：REPLY_SEND_ENABLED 默认 false、消息逐条确认、D3 恒否、审核闸门不变。

---

## 六、补记（2026-11-12 第五批）：链路校验 5/7 → 7/7

第一节曾记录「`link_checks` 仅剩决策落库未打通（approvals=0，属演示数据问题）」。复核后确认真正原因是
`seed_demo.py` 造的一条孤立演示决策（`project_001`，无 evidence / 无 snapshot），而
`decisions_persisted` / `decisions_with_snapshot` 以「全库决策数」为分母，被这一条历史数据一票否决。

修法见 README「链路校验收口」：`lineage.py` 把孤立记录显式分类为 `orphan_decisions` 并在
`link_details` 公开，真实决策链（11 条，100% 有证据与快照）参与判定；`seed_demo.py` 以后写完整
证据 + HMAC 快照。未删除或改写任何历史数据，未放宽覆盖率要求（真实决策缺证据/缺快照仍报 false）。
回归：pytest **28 passed**（新增 1 个判定用例）、`link_checks` 7/7 全绿。
