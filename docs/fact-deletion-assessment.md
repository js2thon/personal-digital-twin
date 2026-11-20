# 「已发布事实」能否删除 —— 可行性评估（2026-11-12）

结论先行：**数据库层可以删，工程与审计层不建议开放单条物理删除**。推荐用「撤销发布 / 作废（`withdrawn`）」的软删除替代，效果对用户等价（列表与检索立刻消失），但保住可追溯性与审计完整性。

> **实施状态（2026-11-12）**：已按第 3/4 节落地「撤销发布」软删除，**未开放单条物理删除**。
> 新增 `POST /api/v1/knowledge/facts/{id}/withdraw` 与 `GET /knowledge/facts?status=withdrawn`；
> 控制台事实表支持撤销、争议、已撤销三态视图；级联硬删的 fact 型悬空引用已修复；
> 第 5 节三项缺口已补齐。详见文末第 6 节。

## 1. 为什么技术上删得掉

- `knowledge.facts` 只有一条出边外键 `claim_id → knowledge.claims(id)`，**没有任何表通过外键引用 facts.id**（`db/init/02_knowledge_ontology.sql` 36-48）。
- 下游引用全部是软引用：`decision.decision_evidence.reference_id`、`decision_records.evidence_refs`、审计快照 JSON 里都只存 fact_id，无 FK、无 `ON DELETE CASCADE/RESTRICT`。
- `status VARCHAR(30)` 无 CHECK 约束，版本号无唯一约束。
- 现有 `DELETE /documents/runs/{id}?cascade=true` 里已有 `DELETE FROM knowledge.facts WHERE claim_id IN (...)` 的批量删除先例。
- 前端「已发布事实」表格目前**没有删除按钮，也没有删除 API**；唯一 fact 级操作是「本体争议」里对 contested 事实的驳回 / 采纳。

## 2. 为什么不能删：10 条实际风险

| # | 风险 | 依据 |
|---|---|---|
| 1 | 决策证据 `reference_id` 悬空，数据库不报错、不级联 | `api/v1/twin.py` 写 fact_id；`db/init/03_...sql` 无 FK |
| 2 | 现有级联清理按 `claim_id + chunk_id` 匹配，删不掉 fact 型证据 | `api/v1/documents.py` 的 `ref_arr` |
| 3 | HMAC 不可变快照只存 fact_id + quote，删行后历史决策「当时依据哪条事实」无法复现 | `modules/audit/service.py`、`decision-authorization-spec.md` 不可变快照要求 |
| 4 | 实体画像双时间线断裂，asof 回看出现空洞 | `modules/ontology/service.py` `_series` / `_pick_current` / `entity_profile` |
| 5 | `superseded_by` 变悬空指针；`MAX(version)+1` 会复用已删版本号 | 同上 `_close` / `_next_version` |
| 6 | 同一 Claim 可被再次 approve 重复发布（幂等判断只看 active fact） | `api/v1/knowledge.py` `_approve_in_tx` |
| 7 | 列表、事实召回、链路计数、实体 fact_count 同时少一行 | `knowledge.py` / `retrieval/service.py` / `lineage.py` / `ontology.py` |
| 8 | 链路校验 `claims_to_facts` 变红（该校验用 EXISTS 查全表，删行后 approved Claim 失去对应 Fact） | `lineage.py` |
| 9 | 证据充分性下降导致 QA 拒答 / needs_info；memory、认知候选、relations 变孤儿 | `graphs/qa.py`、`graphs/reply.py`、`knowledge.py` |
| 10 | 与「PG 为唯一权威源」「证据可追溯率 100%」「不覆盖旧版本」的设计原则相抵触 | ADR-001 / ADR-004、`knowledge-ontology-spec.md` 6/25/39 |

> 说明：规范里**没有**一条明文写「facts 表不可删除」；上述第 10 条是由权威源、可追溯、不可变快照等要求结合代码推导出的工程结论。

## 3. 推荐方案：撤销发布（软删除）

复用已存在的 `withdrawn` 状态（`ontology/service.py` 的 `FACT_WITHDRAWN`、`resolve_conflict` reject 分支、`knowledge-ontology-spec.md` 状态词表），动作定义为：

```
UPDATE knowledge.facts
   SET status='withdrawn', contested=FALSE,
       valid_to=COALESCE(valid_to, NOW()),
       source_ref = source_ref || {"withdrawn_by": actor, "reason": ..., "at": ...}
 WHERE id=:id AND status='active'
+ audit.log_access(actor, "knowledge.fact.withdraw", "knowledge.fact", fact_id, ...)
```

**为什么不复用 `superseded`**：它表示「被新值取代」，需要 `superseded_by` 指向替代事实，且会作为「曾经为真」的历史段进入时间线，还能被 `restore-superseded` 一键恢复；语义与「撤销发布」不符。

**软删除的直接收益**：
- 所有软引用仍可按 id 解析，审计快照可复现；
- `lineage.claims_to_facts` 用 `EXISTS` 不限 status，链路校验不会变红；
- `list_facts` / 事实召回只查 `status='active'`，作废后自动从「已发布事实」列表与检索中消失 —— 用户可见效果与删除一致；
- 画像时间线本就排除 withdrawn，符合「不再为真」。

## 4. 若实施，最小改动清单（已全部实施）

| 文件 | 改动 |
|---|---|
| `backend/app/modules/ontology/service.py` | 新增 `withdraw_fact(fact_id, actor, reason)`；`entity_profile` 增加 withdrawn 留痕桶；`diagnostics` 增加 withdrawn 计数 |
| `backend/app/api/v1/knowledge.py` | 新增 `POST /knowledge/facts/{fact_id}/withdraw`；`list_facts` 增加 `status` 参数（默认 active 兼容） |
| `web/index.html` | 「已发布事实」表增加「操作」列 +「撤销发布」按钮（danger 样式 + 二次确认），成功后刷新列表 |
| `docs/knowledge-ontology-spec.md` / `README.md` | 补 withdrawn 的撤销入口与留痕说明、登记新端点 |
| `backend/tests/test_smoke.py` | 断言 status / valid_to / source_ref、列表不再返回、链路校验仍为真 |
| `backend/app/api/v1/documents.py`（仅当保留级联硬删） | 把 fact_id 纳入 `ref_arr`，并清理 `superseded_by` 悬空指针 |

**真正物理删除只应保留在**：① 文档级联删除（需先修上面的悬空引用）；② 合规擦除类需求（当前代码无 tombstone 能力，需另建）。不建议对单条已发布事实开放。

## 5. 现状缺口（已全部补齐）

- ✅ `web/index.html` 删除确认：改为非弹窗两步确认，文案明确「候选知识、证据、切片与**已发布事实（含下游引用）**」，结果用提示条展示实际删除条数。
- ✅ DDL 注释同步为 `active/superseded/contested/withdrawn`（`db/init/02_knowledge_ontology.sql`）。
- ✅ `entity_profile` 新增 `withdrawn` 留痕桶与 `counts.withdrawn`；`diagnostics` 报告已撤销条数；画像空维度表显示已撤销数。

## 6. 实施记录（2026-11-12）

- 后端：`ontology/service.py` 新增 `_withdrawal_note` / `withdraw_fact` / `_timeline_rows`，状态、`source_ref` 留痕与审计日志同事务；`knowledge.py` 新增 `POST /knowledge/facts/{id}/withdraw` 与 `_fact_status_clause` 状态白名单；`documents.py` 级联删除先收集 fact_id 清理 `decision_evidence`、解 `superseded_by` 指针。
- 前端：事实表三态下拉 + 撤销发布按钮（8 秒两步确认、提示条、防连点、归位页码）；文档删除同样改为非弹窗两步确认。
- 回归：`pytest` 27 passed（新增 3 个用例）；接口与浏览器实测见工作日志。
- **未覆盖的残留**（不影响本次结论，需专用能力时另立需求）：
  1. `decision_records.evidence_refs` 的 JSON 里若含 fact_id，级联硬删不会改写该 JSON（只保留原样，供审计快照复现，不做静默篡改）；
  2. 合规擦除类「真删除 + tombstone」能力尚未建立，当前没有任何接口会物理删除单条事实；
  3. 撤销后若把对应 Claim 手动改回 candidate 再审核，会写入一条**新版本**事实（预期行为：撤销不等于禁止重发）。
  4. ~~控制台其它旧路径仍有个别原生 `alert/confirm`（决策审批、上传、链路记录删除等）~~ —— ✅ 已于 2026-11-12 第五批统一清理：全部改为非阻塞提示条或按钮两步确认，控制台已无 `alert/confirm/prompt` 调用；同时按用户要求把「批量转为事实」改为点击即生效（批量拒绝仍保留两步确认）。消息外发的人工确认闸门未动。
