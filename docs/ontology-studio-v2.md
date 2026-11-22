# Ontology Studio V2 — Enterprise Governance Upgrade

入口：`/ontology-studio/`（Docker Web）或对应静态站点路径；现有 Ontology App Studio 顶栏增加入口。

## 交付能力

### 1. 本体可视化
- Schema 图展示 Object Types、Properties、Link Types；节点详情展示主键、来源表、属性和出入向关系。
- Schema / 实例图切换；实例图支持关键词筛选、选中实例、邻居 2-hop 展开和节点聚焦。
- 画布缩放、适配；实例图通过服务端 `studio/instance-graph` 限制返回规模，默认最多 1,000 节点（API 上限 3,000）。
- RDF/OWL Session 与 Automodel Job 保持独立数据域；RDF Session 不会自动获得 Automodel 实例图。

### 2. 双向字段级数据血缘
- 正向：实例 external_id → 本体属性/关系事实 → source_ref → 源表/源字段/来源行标识。
- 反向：源表 + 源字段 → 受影响的本体实例、属性/关系和值。
- 物化阶段在结构化映射事实的 `source_ref` 中持久化 `table`、`column`、`source_row_key`、`contract_version`。
- 指标血缘沿用指标引擎的 ontology → atomic → derived → composite DAG；工作台并列展示指标定义和数据字段血缘。
- 仅对新物化且实际带有 source_ref 的事实提供字段级追踪。旧快照、手工 Action 创建/修改事实或缺失 source_ref 的数据会显示“未持久化”，不按字段名猜测。

### 3. 本体质量与治理

- 确定性质量评分（0–100）：基于契约错误、悬空引用、重复边、非法关系、自环、未映射属性和待审核项的规则扣分。
- 报告包含评分说明、覆盖率、问题代码、严重级别、计数和明细；该分数不是模型置信度，也不替代业务验收。
- 发布门禁汇总阻断问题和待人工审核候选。最终发布仍由既有发布 API 再次验证。
- 版本管理支持版本列表和两版本差异（类、属性、关系、映射、指标）。
- 操作审计显示任务已有 Actions 记录。
- 权限摘要只显示服务端返回的身份上下文、租户隔离执行状态和写入边界；当前 API 未提供角色/权限矩阵，因此不推断用户角色或可执行权限。

### 4. Discovery 与 Production 摄取边界（P0.1）
- Automodel connectors 当前对 CSV/TSV、Excel、JSON、Parquet 和数据库执行有界抽样；其 TableSet 用于数据画像、本体草案、映射建议和预览，不代表全量数据。
- Automodel materializer 的结果显式标记为 `discovery_sample`，旧 `PgWriter` 已禁用，拒绝把该路径产物写入正式实体/事实表；即使调用方伪造 `data_scope=production` 也不能绕过。
- 当前正式摄取入口是独立的 `ontology.mapping_execution`：仅支持已审核 Mapping Set 对上传表格数据执行，单次最多 500 行，支持 append/upsert、稳定源记录 ID、tenant/mapping/source 唯一性、事务和 ingestion-run/字段级血缘；调用方需分批提交。
- 该执行器不会隐式删除源端缺失行；数据库连接器到生产全量/增量摄取的完整链路、自动分页/水位和删除同步仍未完成。Discovery 采样行数、正式摄取行数及其运行状态必须分别展示；不得将 Discovery 结果计数解释为生产物化结果。
- 当前 Automodel 旧 PgWriter 被安全关闭；后续若要恢复统一的生产物化能力，应由受控 IngestionRun/执行服务提供不可伪造的授权上下文，而不是增加可由调用方传入的布尔标记。

## 新增 / 复用接口

### 图与实例
- `GET /api/v1/automodel/jobs/{job_id}/studio/schema-graph?props=true`
- `POST /api/v1/automodel/jobs/{job_id}/studio/instance-graph`（JSON: `focus`, `hops`, `max_nodes`）
- `GET /api/v1/automodel/jobs/{job_id}/studio/neighbors?entity=...`

### 血缘
- `GET /api/v1/automodel/jobs/{job_id}/studio/provenance?entity=...&predicate=...`
- `GET /api/v1/automodel/jobs/{job_id}/studio/provenance/source?table=...&column=...`
- `GET /api/v1/automodel/jobs/{job_id}/metrics`（指标 DAG）

### 治理与版本
- `GET /api/v1/automodel/jobs/{job_id}/studio/quality`
- `GET /api/v1/automodel/jobs/{job_id}/studio/governance`
- `GET /api/v1/automodel/jobs/{job_id}/versions`
- `GET /api/v1/automodel/versions/diff?a=...&b=...`
- `GET /api/v1/automodel/jobs/{job_id}/studio/actions`

## 关键治理规则

1. API 通过现有 `_job(job_id, request)` 按当前 principal 的 tenant scope 读取任务；新接口不接受客户端 tenant_id。
2. 前端治理页面只读，不开放未经审批的发布、回滚或实例写入。
3. 质量评分是可解释的确定性规则评分；不能被解释为 LLM 置信度、数据正确率或合规认证。
4. `release_gate.ready` 是工作台摘要，不是发布授权凭证；发布 API 必须再次执行后端校验。
5. 当前未暴露角色权限矩阵、审批人矩阵或细粒度字段 ACL，界面明确显示缺口。
6. source_ref 中只保存数据源标识、表字段、契约版本和源行主键标识，不保存原始整行数据。
7. 数据库源如果未物化/持久化字段血缘，不能从 Schema 映射推断实际运行时字段来源。

## 验证

- `node web/ontology-studio/validate.mjs`：检查内嵌 JavaScript 语法、DOM 引用、核心接口路径和工作台控件。
- `cd backend && python -m pytest tests/test_automodel.py -q`：自动建模、指标、问答和本次新增的质量/双向血缘回归测试。
- 仍需在部署环境完成真实 API、浏览器 E2E、租户隔离和私有数据库连接验证。
