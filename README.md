# Personal Digital Twin Platform

基于多源个人知识、个人本体和认知模型，具备**知识问答、个人风格回复和工作流决策**能力的私有化数字分身平台。

> **产品定位已升级**：V1.2 起目标为「**企业数字员工智能体平台**」——企业知识底座 + 岗位认知配置 + 能力包 + 权限策略，支持多岗位/多角色/多组织。当前代码即 V1.1 基线，不推倒重来。

> **蓝图与实施状态**：
> - V1.2 蓝图原文：[`docs/blueprint-v1.2.txt`](docs/blueprint-v1.2.txt)
> - **V1.2 开发计划（重规划，14 周 5 阶段）**：[`docs/development-plan-v1.2.md`](docs/development-plan-v1.2.md)
> - V1.0 原文：[`docs/blueprint-v1.0.txt`](docs/blueprint-v1.0.txt)；逐节现状：[`docs/blueprint-v1.1-implementation.md`](docs/blueprint-v1.1-implementation.md)
>
> 当前最大的三个缺口是 **evaluation（零实现）、pgvector（未启用，本机内嵌 PG 无 vector 扩展）、真 worker（仍是 eager）**，即 V1.2 阶段 0 的全部内容。蓝图里列为 MVP 但当前未达标的项另有 RAGFlow，列在本文「与蓝图的差距」。

## 快速启动

```bash
cp .env.example .env          # 修改密码等配置
make up                       # 启动 PostgreSQL(pgvector)/Redis/MinIO/API/Worker/Web
# 需要 GPU 时：make up-ai     # 追加 vLLM 本地推理
make seed                     # 写入示例数据（演示审批闭环）
make test                     # 后端冒烟测试
```

- Web 控制台：http://localhost:8200 （八个工作区：数字分身/知识生产/知识库/本体/认知/决策/消息/链路观测）
- API 文档：http://localhost:8100/docs
- **注意**：以上是 Docker 编排形态；当前本机实跑的是下一节的「本地无 Docker」形态（8101 + 内嵌 PostgreSQL 5434 + eager 后台线程 + 原文内联 DB，无 Redis/Celery Worker/MinIO/vLLM）。`LLM_BASE_URL` 实际指向企业 OneAPI 网关，并非完全本地推理。
- RAGFlow 1.0 按官方 compose 固定 v1.0.0 部署后，把地址和 Key 填入 `.env`（`RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY`），系统经 HTTP API Adapter 对接，**不触碰其内部数据库**。
- **偏差提示**：compose 用的是 `pgvector/pgvector:pg16` 镜像，但当前 `db/init` 的 DDL 把 `embedding` 建成 `TEXT`、从未执行 `CREATE EXTENSION vector`，也没有 HNSW/IVFFlat 索引（见下文 A1）。镜像带扩展 ≠ 已启用扩展。

## 本地无 Docker 运行（当前环境）

> Docker 编排仍使用 8100(API)/8200(Web)；本机直跑使用以下端口。

内嵌 PostgreSQL 16.4 使用 **5434**（5433 会被 WSL 端口转发占用），API/控制台使用 **8101**：

```bash
# 1) 数据库（数据目录 .runtime/pgdata，勿删；首次崩溃恢复可能需数分钟）
.runtime/pgsql/bin/postgres.exe -D .runtime/pgdata -p 5434 -h 127.0.0.1

# 2) API + 控制台（LLM 配置从 .env 注入，不写死在命令行）
cd backend && set -a && . ../.env && set +a && \
  CELERY_TASK_ALWAYS_EAGER=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8101 --no-proxy-headers
```

- 控制台：http://127.0.0.1:8101/ 　API 文档：http://127.0.0.1:8101/docs
- 数据库 DSN 默认值已写在 `backend/app/config.py`；`celery` 本地用 eager 模式，蒸馏在后台线程执行。

### 一键常驻启动（推荐）

会话内启动的后台进程会随会话清理退出，导致页面报「Failed to fetch」/ 8101 打不开。

**静默后台启动（推荐，无任何命令行窗口）**：双击项目根目录的 **`start-silent.bat`**。
它用 `pythonw.exe` 拉起 `start_hidden.py` 守护进程，**不弹出 cmd 黑框**，窗口瞬间消失后服务在后台常驻：

- 自动检测并拉起内嵌 PostgreSQL 5434（`CREATE_NO_WINDOW`，无控制台）
- 守护 API 8101：uvicorn 异常退出后约 10 秒自动重启
- 日志：`.runtime/uvicorn.out.log`、`.runtime/uvicorn.err.log`、`.runtime/pg-stderr.log`
- 停止：双击 **`stop-services.bat`**（优雅停 PG，失败则强制结束）

带窗口模式（调试用）：双击 **`start-services.bat`**，脚本会检测并拉起 PostgreSQL
（用 `pg_ctl -w` 等待崩溃恢复完成，最多 5 分钟）、加载 `.env`、启动 8101 的 API + 控制台。
保持那个命令行窗口打开即为服务运行中；页面每 10 秒刷新「● 服务在线 / ● 服务已断开」。

> 强杀 PostgreSQL 后首次启动需做 fsync 崩溃恢复（约 1–3 分钟），期间数据库报
> `the database system is starting up` 属正常，等待即可，不要删除 `.runtime/pgdata`。

## 目录结构

```
personal-digital-twin/
├── docker-compose.yml        # 三安全域编排（数据域/AI域/应用域）
├── db/init/                  # PostgreSQL 初始化 DDL（9 个 Schema）
├── backend/app/
│   ├── api/v1/               # sources/documents/knowledge/cognition/ontology/twin/workflow/lineage
│   ├── modules/
│   │   ├── dws_source/       # DWS CLI：文档/消息/日历/会议四类只读接入
│   │   ├── ingestion/        # MinIO 原始存储 + 幂等去重
│   │   ├── document_parser/  # 解析与切片（含证据定位器）
│   │   ├── distillation/     # Claim/Evidence 抽取 + 实体消歧 + Prompt 版本化
│   │   ├── retrieval/        # 混合检索：中文词法召回 + 可选向量重排 + 事实/记忆/RAGFlow
│   │   ├── twin/             # LangGraph Orchestrator + 三张任务图
│   │   ├── policy/           # 工具白名单 + D0~D3 决策权限
│   │   ├── audit/            # 不可变决策快照（哈希防篡改）+ 访问日志
│   │   └── llm/              # OpenAI 兼容适配器（当前 .env 指向企业 OneAPI 网关）+ 幂等键
│   └── workers/              # Celery 蒸馏状态机（14 状态，可重试）
├── web/index.html            # MVP 控制台
└── docs/                     # 蓝图原文与实施状态 / 开发计划 / ADR / 知识与本体规范 / 决策授权规范 / 评估文档
    ├── blueprint-v1.2.txt                 # 蓝图 V1.2 原文（企业数字员工智能体平台，逐字归档）
    ├── development-plan-v1.2.md           # V1.2 开发计划：5 阶段 / 14 周 / G0~G4 门禁 / 7 项决策
    ├── blueprint-v1.0.txt                 # 蓝图 V1.0 原文（逐字归档，不修改）
    ├── blueprint-v1.1-implementation.md   # 逐节实施状态 + 偏差 + 冻结判据
    ├── ADR-architecture-decisions.md
    ├── knowledge-ontology-spec.md
    ├── decision-authorization-spec.md
    ├── structured-ingestion-assessment.md
    ├── needs-review-sop.md
    └── optimization-review-2026-11-11.md
```

## 已实现的核心机制

| 机制 | 位置 |
|---|---|
| 蒸馏状态机 PENDING→COMPLETED + RETRY_WAIT/FAILED/NEEDS_REVIEW/REVIEW_DISMISSED（复核闭环见 SOP） | `workers/pipeline_tasks.py`、`modules/distillation/review.py` |
| 幂等键 hash(doc+content_hash+pipeline+model+prompt) | `modules/llm/client.py` |
| Claim→人工审核→Fact（不自动转） | `api/v1/knowledge.py` |
| 认知 Profile 版本快照（可回滚/比较/审查） | `api/v1/cognition.py` |
| 三张 LangGraph 图（问答/回复/决策） | `modules/twin/graphs/` |
| 问答证据不足拒答 + 引用验证 | `graphs/qa.py` |
| 回复不擅自承诺（承诺检查器） | `graphs/reply.py` |
| 决策 MVP 自动执行恒为否（D2 固定） | `policy/engine.py` + `graphs/decision.py` |
| 未授权动作无法执行（执行前最终校验） | `api/v1/workflow.py` |
| 决策快照哈希防篡改 | `modules/audit/service.py` |
| 钉钉文档（DWS CLI）全类型导入蒸馏 | `modules/dws_source/client.py` + `api/v1/documents.py` |
| 文档解析适配层（MinerU / docling / opendataloader-pdf / 内置兜底） | `modules/document_parser/adapters.py` |
| 候选知识批量审核（全选 / 转事实 / 拒绝） | `api/v1/knowledge.py` |
| 蒸馏任务显示文档名 + 级联删除 | `api/v1/documents.py` |
| Astryx 设计系统（明暗主题 / 无障碍 / 令牌化） | `web/index.html` + `docs/astryx-design-system.md` |

## 文档解析与钉钉文档蒸馏

`GET /api/v1/documents/parsers` 查看引擎可用性。所有引擎缺失时自动降级，不阻塞流水线：

| 文档类型 | 读取方式 | 首选引擎 |
|---|---|---|
| 钉钉在线文档 `adoc` | `dws doc read` | Markdown 直读 |
| 钉钉表格 `axls` | `dws sheet range read` | 结构化转 Markdown |
| `pdf` | `dws drive download` + 解析 | MinerU → opendataloader-pdf → docling → pypdf |
| `docx/xlsx/pptx` | `dws drive download` + 解析 | docling → python-docx/openpyxl/python-pptx |
| 本地上传任意文件 | 直接解析 | 同上，纯文本直读 |

可选增强安装：`pip install -e "backend[parsers]"`（docling 依赖 torch 较大；
opendataloader-pdf 还需 Java 11+；MinerU 提供 CLI 即可）。

控制台「知识生产」页会自动列出你的钉钉文档，按类型标注解析引擎，一键蒸馏；
「知识库」支持一键全选后批量转为事实/拒绝：**批量转为事实点击即生效，无二次确认**；批量拒绝因不可恢复，仍需 8 秒内连点两次确认。任务列表可显示文档名并级联删除。

## 钉钉四类数据源与五大中心关联链路

`modules/dws_source/streams.py` 统一封装四类**只读**数据源，全部走同一条蒸馏流水线：

| 数据源 | DWS 命令 | kind | 蒸馏正文 |
|---|---|---|---|
| 钉钉文档 | `drive search` / `doc read` / `sheet read` | `document` | Markdown / 解析适配层 |
| 企业 IM消息 | `chat +chat-list` / `+chat-messages` / `+at-me` | `chat` | 会话消息时间线（含「@我」汇总） |
| 日历日程 | `calendar +agenda` / `+attendee-list` | `calendar` | 标题 / 时间 / 地点 / 状态 / 参会人 |
| 会议听记 | `minutes +list-all` / `+search` / `+detail` | `minutes` | 会议摘要 / 关键词 / 行动项 |

接口：

- `GET /api/v1/documents/dws/streams?kind=...&query=...&days=30` —— 列出可蒸馏条目
- `POST /api/v1/documents/dws/distill-status` —— `{"kind":"document","items":[{node_id,name,doc_url}]}`，按条目返回 `completed / active / none`
- `POST /api/v1/documents/dws/stream-import` —— `{"kind":"minutes","item":{...}}`，登记数据源后进入统一蒸馏

列表操作列按蒸馏状态自动切换：`COMPLETED` → 置灰的「**已蒸馏**」（不可点击、不可重复提交）；
任务进行中 → 置灰的「蒸馏中…」；`FAILED / NEEDS_REVIEW / REVIEW_DISMISSED` 仍可重新蒸馏
（`NEEDS_REVIEW` 请优先在「知识生产流水线 → 复核」中处置，重蒸只是其中一种动作）。
状态由 `modules/dws_source/distill_status.py` 从本地库比对得出，优先用 `external_ref`（节点 ID），
其次 `origin_url`，最后用规范化标题兜底（覆盖早期导入但缺少来源元数据的文档），按数据源种类隔离避免误判。
页面每 5 秒轻量刷新一次状态，蒸馏提交与任务删除后也会立即同步，不重新抓取 DWS 列表。
- `GET /api/v1/lineage/overview` —— 各中心计数 + 链路校验 + 来源分布
- `GET /api/v1/lineage/trace/{document_id}` —— 文档 → 任务 → 切片 → 证据 → 主张 → 事实 → 关系 → 决策

已补齐的链路断点（括号内为此前状态）：

```
数据源(source.data_sources / documents.metadata.source_kind，原来为空)
  → 蒸馏任务(pipeline_runs/steps) → 文档切片(semantic_chunks，原来恒空，检索不到原文)
  → 证据/候选知识(evidence_spans/claims) --人工审核--> 事实(facts)
       ├→ 个人记忆(memory_items，带真实 source_claim_ids)
       ├→ 认知候选(profile_proposals，带 evidence_claim_ids) → 画像版本 → 注入问答/回复/决策三张图（原来恒为空）
       └→ 本体关系(ontology.relations 由 candidate 激活，原来前端看不到)
  → 决策(decision_records / decision_evidence / audit.decision_snapshots，原来只返回内存 uuid 不落库)
  → 人工审批(workflow.approval_tasks) → 执行审计
```

控制台「知识生产」页可切换四类数据源并一键蒸馏，任务表可打开「链路」追踪；知识/本体/认知/决策各中心均带来源标签与证据下钻；数字分身首页展示链路总览与校验结果。

铁律不变：除「消息中心」经本人逐条确认后的外发外，所有 `dws` 调用只读；审核前 Claim 不得成为 Fact；决策默认必须人工审批，MVP 自动执行恒为否。

## 企业 IM消息接管（回复草稿与逐条确认闸门）

控制台新增「消息中心」：只读收件 → 数字分身起草 → 本人逐条确认 → 受控外发。

| 阶段 | DWS 命令 | 说明 |
|---|---|---|
| 收件 | `chat +at-me` / `+unread-chats` / `+chat-messages` / `+chat-list --types group,p2p` | 只读；覆盖群聊与单聊。@我 消息、未读会话、最近有活动的单聊；单聊里对方的有效消息一律起草（含无疑问词），群聊仍需提问/请求词。按 `messageId` 幂等去重 |
| 起草 | 无（内部 LLM） | 复用 `communication` LangGraph：检索个人事实/文档 + 认知画像 + 承诺检查，输出草稿与待确认事项 |
| 预览 | `chat +messages-reply --dry-run` | 返回 `willSend=false`，绝不外发 |
| 外发 | `chat +messages-reply -y` | **仅**在本人确认且显式开启总开关后才会执行，带稳定幂等键 |

安全契约（代码层面不可绕过）：

1. 扫描、起草、预览均为只读或 dry-run；草稿落库状态恒为 `pending`。
2. 必须 `POST /messages/replies/{id}/review` 显式 `approve`（可同时编辑定稿）才变成 `approved`。
3. 发送接口同时校验 `status=approved` + `confirm=true`；缺一即 403。
4. 服务端总开关 `REPLY_SEND_ENABLED` 默认 **false**：此时即使请求 `live=true` 也只返回 dry-run 预览，不调用任何写命令。
5. 每次尝试/预览/发送写入 `audit.access_logs`；发送结果落 `send_result` 快照。

开启真实外发：在项目根 `.env` 增加 `REPLY_SEND_ENABLED=true` 并重启服务。开启后每条仍需本人在控制台编辑定稿 → 点「确认草稿」→ 点「确认并真实发送」→ 二次弹窗确认。CLI 自身安全等级为 `effect=write / confirmation=user_required`，仅在用户已明确确认时才追加 `-y`。

状态机：`pending → approved → sent`（或 `rejected` / `failed`）；`sent` 不可重复发送。

API：`GET /messages/config`、`POST /messages/scan`、`GET /messages/scan/status`、
`GET /messages/replies?status=`、`POST /messages/replies/{id}/review`、
`POST /messages/replies/{id}/send`。

## MVP 验收场景（项目审批闭环）

1. 上传历史对话/文档 → 蒸馏出 Claim/Evidence → 审核转 Fact
2. 认知候选 → 本人确认 → 发布新 Profile Version
3. `POST /api/v1/twin/decision` 发起审批分析 → 返回建议+证据+风险
4. 生成人工审批任务 → `/workflow/approvals/{id}/approve`
5. 记录真实决策与建议差异 → `/cognition/feedback` 反馈进入对应处理队列

## 整体优化（2026-11-11）

针对「架构骨架正确但实际能力打折」的评审结论做了系统性修复：

| 问题 | 修复 |
|---|---|
| 检索是假的（事实/记忆取最新 6 条，中文整句 ILIKE 恒 0 命中） | `retrieval/lexical.py` 中文短语 + 2-gram + 英文词加权召回，三路都按相关性打分；`qa.py` 补上真实的「不足→扩大补充检索」与「引用编号越界剔除」分支；无关问题现在会正确拒答 |
| 本地库无 pgvector（`embedding` 实为 TEXT） | 新增可选 `embedding_vec TEXT` 列存 JSON 向量，蒸馏时批量写入（失败自动降级）；检索时若有向量则叠加余弦重排，否则词法检索独立工作 |
| 写操作跨 5 个独立事务 | `db/session.py` 用 ContextVar 实现 `with db.transaction():`，所有 `fetch_*/execute` 自动复用同一连接；审核发布、决策落库、审批、蒸馏 PUBLISHING 全部原子化，嵌套事务安全 |
| 去重跨来源串台、`publish_claim` 不幂等 | 去重改为「同一数据源内 external_id → 标题+hash → hash」三级，metadata 只补缺不覆盖；`external_id` 真正写入；同一主张重复蒸馏复用已有 Claim；不再产生 source=target 的假关系 |
| 级联删除留悬空引用 | 补清 memory_items / profile_proposals / decision_evidence，并把证据链断裂的决策标记为 `evidence_invalidated`（保留不可变快照） |
| 单聊没接管 | `chat-list --types group,p2p` 覆盖 56 个单聊；消息中心新增「未读单聊 / 最近活动单聊」路径，无疑问词也起草；单聊外发省略 `--group` 由 message-id 定位；前端显示「单聊/群聊」标签 |
| 全链路无身份边界 | `app/auth.py` + 中间件：回环视为本人，非回环必须 `X-Twin-API-Key`（未配置则一律 401）；CORS 从 `*` 收紧；`reviewer/approver_id/actor` 改由服务端注入；`policy.check_tool("send_message")` 接入外发路径 |
| 假接口 / 假绿 | `/sources/{id}/sync` 真实刷新 DWS 并把结果写 Outbox（未接入类型明确报错），`GET /sources/outbox` 可查；`/cognition/feedback` 返回可追踪的 `ticket_id` + `GET /cognition/feedbacks`；`lineage.link_checks` 改按真实覆盖率计算并附 `link_details` |
| 历史数据缺口 | `POST /knowledge/maintenance/backfill`（幂等）清自环关系、回填历史事实的记忆与认知候选、基于事实文本生成真实实体关系；`POST /knowledge/maintenance/rechunk` 从已存原文为缺切片的历史文档重建切片（纯本地、不调 LLM、不动审核状态） |
| 状态重启即丢 | `workflow.twin_sessions` / `workflow.scan_state` 落库，服务启动时幂等建表；DWS 列表加 120 秒 TTL 缓存（`refresh=true` 可强制重抓） |
| 运维脆弱 | `start-services.bat` 选择真正装了 uvicorn 的 Python、8101 占用时健康探测后无限等待接管（不再 10 分钟放弃）、设置 NO_PROXY |

安全闸门**未做任何放宽**：`REPLY_SEND_ENABLED` 仍默认 false、消息仍须逐条确认、决策自动执行恒为否、审核前 Claim 不得成为 Fact。

## 全功能版本（2026-11-11 第二批）

MVP 验收通过后，按 `docs/optimization-review-2026-11-11.md` 的评审清单完成系统性修复与能力补齐：

| 类别 | 内容 |
|---|---|
| 蒸馏幂等修复 | H1 失败/待复核任务重新导入会重置 PENDING 并重新派发（此前被去重后永远无法重蒸）；H2 Claim 幂等改 jsonb 语义等值（不再依赖 LLM 输出 key 顺序）；H4 仅终态可重置，执行中任务返回 already_running 防并发双蒸 |
| 信任护栏补洞 | H3 无主体事实写入同样走信任护栏：同值幂等/提升信任级，异值低信任落 contested 等 owner 裁决，绝不静默作废已确认事实 |
| 审计强化 | M1 决策快照升级 HMAC-SHA256（密钥取 api_key），防持库者改快照后重算哈希；历史裸 SHA256 快照自动回退校验 |
| 消歧健壮化 | M2 实体消歧：LLM 失败不再静默新建重复实体，match_id 先验证存在性，并有确定性名称匹配兜底 |
| 证据引用修复 | M3 决策 fact 证据 reference_id 取 fact_id（此前恒 NULL，文档删除后失效检测失明） |
| 起草质量 | M4 回复图把检索到的事实（主体 · 谓词 = 值）并入起草上下文 |
| trace 准确性 | M5 QA 降级分支 trace 改为 degraded + 实际行为描述 |
| 裁决正确性 | M6 争议裁决 accept 对回溯型争议按 backdated 语义处理，不再制造 asof 空洞区间 |
| 性能 | M8 切片单事务批量写入（db.executemany）；M10 启动幂等补索引（runs.document_id / documents.external_id / chunks.document_id / claims.status / facts.subject_predicate）+ pg_trgm GIN 索引（entities/chunks/memory）+ 蒸馏状态索引 5s TTL 缓存；M9 detect_mentions 实体表 30s TTL 缓存 |
| 数据完整性 | M11 无 MinIO 内联原文超 20 万字符时写 _inline_truncated 标记 |
| 本体发布 | M12 publish 强制 draft 状态 + 字段删除兼容性检查 + 旧版本置 deprecated（单事务） |
| 低优先级清理 | L1 软超时重试超限落 FAILED；L2 reject_claim 0 行返回 400；L4 retrieval_scope API 参数生效；L5 /twin/reply 落链路快照+审计；L6 DWS 导出/下载临时目录用后即清；L9 分页 limit 统一钳制（含 approvals 补 LIMIT）；L11 get_engine 双检加锁 |

安全闸门未放宽：REPLY_SEND_ENABLED 默认 false、消息逐条确认、D3 恒否、审核闸门不变。
蓝图冻结项（Neo4j / Qdrant / Temporal / OPA / 模型微调 / D3）继续冻结，接口已预留。

## 全功能版本（2026-11-12 第三批：剩余清单清零）

| 类别 | 内容 |
|---|---|
| 反代身份边界 | M7 默认不采信 `X-Forwarded-For`（显式 `TRUST_PROXY_HEADERS=true` 才解析，且从右往左取首个非可信地址）；`/docs`、`/openapi.json` 纳入身份闸门；`testclient` 判定改为测试侧显式开关。**关键**：uvicorn 默认会自行改写 client.host，必须在启动参数加 `--no-proxy-headers`，否则应用层的不信任策略会被架空 |
| 并发安全 | L3 认知审批在单事务内用 `pg_advisory_xact_lock` 串行化版本分配 + `UPDATE ... RETURNING` 原子抢占，杜绝 UNIQUE(version) 500 与画像更新丢失 |
| 数据接入 | L7 在线表格按工作表元数据动态计算读取区域（可配 `DWS_SHEET_MAX_ROWS/COLS`），不再固定 A1:Z200，截断留提示；L8 上传加 25 MiB 上限，超限 413 |
| 内存治理 | L10 会话兜底改 200 会话 ×50 轮有界 LRU；DWS 列表缓存 128 条上限 + 过期清理 |
| 存储去重 | L12 同文档相同证据复用一条 evidence_spans；新增 `POST /knowledge/maintenance/dedup-evidence`（默认 dry-run）合并存量，实测 797 → 88 条，claim_evidence 引用零孤儿 |
| 策略透明 | L13 工具白名单标注真实执行点（`wired`/`implicit`/`declared_only`），新增只读 `GET /workflow/policy/tools` 覆盖度自检 |
| 凭据 | L14 移除 MinIO 默认口令，未显式配置凭据直接降级为内联 DB 存储 |
| 阈值生效 | L15 `CLAIM_REVIEW_THRESHOLD` 低置信度 Claim 落库 `review_priority='low_confidence'` 并在审核队列前排，前端显示「优先复核」；存量幂等回填 |

启动脚本同步：`start-silent.bat` / `start_hidden.py` 静默守护（无窗口、uvicorn 退出自动重启），`stop-services.bat` 一键停止；启动参数统一带 `--no-proxy-headers`。

## 全功能版本（2026-11-12 第四批：已发布事实撤销发布）

按 `docs/fact-deletion-assessment.md` 结论落地「撤销发布」软删除 —— 单条物理删除**不开放**：

| 类别 | 内容 |
|---|---|
| 新端点 | `POST /api/v1/knowledge/facts/{fact_id}/withdraw` `{reason?}`：仅 active 可撤销，单事务内 `active → withdrawn` + `valid_to=NOW()` + `source_ref` 留痕（`withdrawn_by/reason/at`）+ 审计日志；重复撤销 400 |
| 查询 | `GET /api/v1/knowledge/facts?status=active\|contested\|superseded\|withdrawn`（白名单外回落 active）；控制台事实表下拉切换三种视图 |
| 可见效果 | 事实列表、检索召回、问答证据只查 active，撤销后立即消失；决策证据 `reference_id`、审计快照、`superseded_by` 仍可解析，链路校验不变红，版本号不复用 |
| 留痕 | `entity_profile` 增加 `withdrawn` 桶与 `counts.withdrawn`；`ontology/diagnostics` 报告已撤销条数；画像「空维度」表增加已撤销列 |
| 前端 | 事实表新增「撤销发布」按钮（danger + 8 秒两步确认 + 非阻塞提示，无原生弹窗）；文档删除确认改为两步确认并在文案中明确「会删除已发布事实及下游引用」 |
| 级联硬删 | `delete_run(cascade)` 删 facts 前先收集 fact_id 纳入 `ref_arr` 清理 `decision_evidence`，并 `UPDATE ... SET superseded_by=NULL` 解掉悬空版本链指针；返回新增 `facts` / `dangling_refs_cleared` 计数 |

安全闸门未放宽：撤销是软状态变更、不删行，Claim 审核闸门、消息外发闸门、D3 恒否均不变。

## 交互收口（2026-11-12 第五批：审核即时生效 + 去原生弹窗）

| 类别 | 内容 |
|---|---|
| 批量转事实 | `batchReview('approve')` 去掉 8 秒两步确认，勾选后点一次直接调用 `/knowledge/claims/batch-review`；保留防连点（按钮禁用 + 处理中）、结果提示条（新发布 / 重复佐证 / 转争议 / 跳过）、页码归位与列表、徽章刷新 |
| 批量拒绝 | 丢弃候选不可恢复，仍保留 8 秒两步确认（仅按钮文案变化，无弹窗） |
| 全量去弹窗 | 决策审批、本体争议裁决、认知候选生成/确认、文件上传、链路追踪、链路运行记录删除、链路详情加载等路径的原生 `alert/confirm` 全部改为非阻塞提示条或按钮两步确认；页面已无 `alert/confirm/prompt` 调用 |
| 未改动 | 消息外发的「逐条确认草稿 → 确认并真实发送」人工确认闸门、D3 恒否、policy 工具白名单等安全铁律一律不动 |

### 链路校验收口（7/7 全绿，2026-11-12）

`GET /api/v1/lineage/overview` 的 `link_checks` 此前长期是 5/7：`decisions_persisted` 与
`decisions_with_snapshot` 用「全库决策数」做分母，被一条 `scripts/seed_demo.py` 造的孤立演示决策
（既无 `decision_evidence` 又无 `audit.decision_snapshots`）永久拖成 false。

| 改动 | 说明 |
|---|---|
| `api/v1/lineage.py` | 新增 `_decision_link_checks`：把孤立演示记录显式分类为 `orphan_decisions`，`linked_decisions` 参与覆盖率判定，两者都放进 `link_details` 公开；真实决策只要缺一条证据或快照，对应项仍报 false —— **不靠放宽口径刷绿** |
| `scripts/seed_demo.py` | 演示决策同步写一条标注 `demo_seed`、不伪造 `reference_id` 的证据行，以及用 `audit.save_decision_snapshot` 生成的如实 HMAC 快照；重新播种不会再制造孤儿 |
| 数据 | 不删、不改、不补写任何历史记录；现存 1 条孤立演示决策原样保留并可见 |

实测：11 条真实决策 100% 有证据与快照，`link_checks` 7 项全为 true，
`link_details.orphan_decisions=1 / linked_decisions=11`。

补充口径（同批）：知识审核的 `duplicate / reinforced` 分支**不新建 Fact**，只把 claim_id
追加到已有 fact 的 `source_ref.corroborating_claims`。旧 `claims_to_facts` 只认
`facts.claim_id` 直接关联，导致每产生一条重复佐证就把链路误报为断。现把这条真实关联
计入 `approved_with_fact`，并在 `link_details.approved_corroborated` 公开数量；
仍要求 100% 覆盖，少一条真实 fact 一样报 false。

### NEEDS_REVIEW 人工复核闭环（2026-11-12 第六批）

此前 `NEEDS_REVIEW` 只有一个徽章和一个「重新蒸馏」出口：看不到原因、没有处置入口、
没有责任人、没有验证，重蒸失败又回到同一状态。现补齐「谁 / 在哪 / 怎么做 / 怎么验证」，
完整规范见 `docs/needs-review-sop.md`。

| 层 | 改动 |
|---|---|
| 诊断 | `recent_runs` 返回 `error_info` + 原因中文标签 + 切片/主张/步骤计数；新增 `GET /documents/runs/needs-review` 汇总（角标 + 原因分布）；`chat_json(strict=True)` 区分 `invalid_json` 与 `no_claims`；按切片统计失败，**部分失败不再丢弃全部成功结果** |
| 处置 | 新增 `POST /documents/runs/{id}/review`（`retry` / `dismiss`），仅 `NEEDS_REVIEW` / `REVIEW_DISMISSED` 可处置；actor 由服务端注入；单事务完成状态更新 + `ingest.pipeline_reviews` 留痕 + 审计日志；`dismiss` 置终态 `REVIEW_DISMISSED`，不删任何数据且允许反悔 |
| 验证 | retry 前 LLM 端点预检；流水线每次结束自动把复核记录从 `pending` 回填为 `recovered`（COMPLETED 且 `published>0`）或 `still_failing`；worker 异常退出同样回填，闭环不悬空 |
| 控制台 | 侧栏「蒸馏任务」加待复核角标；列表可筛「仅待复核」，状态列显示中文原因；新增复核弹窗（诊断 / 步骤日志 / 历史处置与验证结论 / 两个处置动作），重蒸成功后仍可用「复核记录」回看结论 |
| 兼容 | `reclaim_run` 纳入 `REVIEW_DISMISSED`；`/documents/{id}/reprocess` 的 eager 分支改走同一状态机，不再绕过防并发双蒸；DDL 与状态注释同步 |

库内 14 条待复核任务（`no_claims` 10 / `llm_unavailable` 3 / `no_chunks` 1）**全部原样保留**，
判断权留给本人；Claim 审核闸门、外发人工确认闸门、D3 恒否均未改动。

## 结构化数据接入（2026-11-12 第七批：映射契约 · 确定性断言）

员工汇报关系、部门、岗位、绩效等存储在数据库 / 湖仓里的**结构化数据不走蒸馏**：
它们是权威源直读、零歧义，让 LLM「抽取」只会把 100% 准确的字段降级成推断，还会外发敏感人事数据。
完整方案评估（逐条核对 ADR / 授权规范 / 本体规范 / 铁律 / 冻结项）见
`docs/structured-ingestion-assessment.md`。

| 层 | 实现 |
|---|---|
| 旁路 | `source.data_sources(hr_structured)` → 映射契约（本人一次审核并发布）→ 确定性断言（纯函数、零 LLM、零 DB）→ `ontology.write_fact` 收口。不产生 documents / chunks / claims / evidence_spans |
| 映射契约 | `ingest.structured_contracts`：版本化 + `contract_hash`、`draft/published/deprecated`；发布单事务内旧版置 deprecated + 审计。**draft 契约调用 preview/sync 恒 400**（ADR-003 等价闸门，绝不用假 Claim 冒充审核） |
| 确定性断言 | `app/modules/structured/assertions.py`：工号做实体锚点（`properties.external_ids`），字段 → 谓词 / 实体 / 时间语义 / 敏感级别；`row_hash` + 不含批次与时钟的 `idempotency_key` 保证重复同步幂等 |
| 信任级 | 同步值按 `agent`(tier=2) 写入：契约批准的是**规则**不是**值**。本人手工修正(tier=3)不会被源库同步顶回，而是自然落 `contested` 等本人裁决 |
| 写入闸门 | 默认 preview（零知识层写入，只记 dry-run 批次留痕）；真实同步须 `confirm=true`，控制台 8 秒两步确认，不提供定时自动同步 |
| 双时间线 | 调岗 / 换主管走 `superseded` 取代链，`asof` 可回看历史部门；关系边 `ontology.relations` 同源只保留一条 active，换值关闭旧边而非覆盖 |
| 异常闭环 | `ingest.structured_anomalies` + 原因码元数据（谁 / 在哪 / 怎么做 / 怎么验证）：`schema_drift` 整批拒绝、`multi_manager` 与 `cycle_detected` 写库前拦截、`missing_required` / `enum_invalid` / `entity_unresolved` / `restricted_skipped`；处置后下一次批次自动回填 `recovered / still_failing` |
| 视角边界 | 组织关系 L1 落库；绩效明细 L0 只读联邦、默认 `restricted` 不落库；全员花名册不进。物理擦除 / tombstone 继续冻结待拍板 |
| 接口 | `GET/POST /structured/contracts`、`/publish`、`/preview`、`/sync`、`/sync-runs`、`/anomalies`、`/anomalies/{id}/review`、`/reasons`、`/diagnostics`、`/template`、`/uploads`、`/formats` |
| 控制台 | 蒸馏任务页新增「结构化数据源 · 映射契约」卡：载入示例 / 编辑契约 / 发布 / 预览 / 同步 / 异常复核 / 同步留痕与自检 / 文件上传；事实表「来源」列显示 `结构化 · 库表#主键` |
| 规则自动推导（B 档） | **一次性导入不再需要手写契约**。上传/粘贴后 `POST /structured/suggest` 从数据本身推导：主键 = 唯一非空列、名称列、实体类型、`expect_columns`、required、时间列全部规则判定；常见组织列名（部门/主管/岗位/职级/状态/入职）由内置词典预填谓词；未命中列问一次模型要 snake_case 候选（25s 超时、失败降级留空），**模型只产建议，写入仍是零 LLM 纯函数**。人只需审一张「列 → 谓词 / 对象 / 可见性」表格后确认写入；mapping 由当次请求携带，服务端落 `status='ephemeral'` 的规则快照（按 mapping 哈希幂等，同一套规则只落一条）作追溯锚点。敏感列按完整词段判定（英文词段 / 中文子串），绩效、薪资、证件、手机默认 restricted 不落库。adhoc 与契约流程共用同一条断言 / 幂等 / 分批 / 异常路径，没有第二条写路径；要复用规则（定时同步）时点「存为可复用契约」走 draft → publish |
| 文件接入 | 与湖仓同一条链路：`xlsx / csv / tsv / md 表格 / json` → `app/modules/structured/files.py` 解析 → `ingest.structured_uploads` 行快照 → 按 `upload_id` 预览/同步。解析零 LLM、不猜列名；单文件 ≤**100 MiB**、≤20 万行（进程内存硬上限）、≤**512 列**、≤600 万单元格（行 × 列），**超过 5000 行由服务端自动分批**（逐批断言、逐批事务、行序不变、全部行写入，绝不截断）；分批前用 `cross_batch_anomalies()` 对整份文件预检 `multi_manager / cycle_detected`，跨批冲突照样拦截；任一批失败则该批回滚、后续不执行，返回 `partial / failed_batch / batches_committed`。csv/xlsx 逐行流式消费、上传按 1 MiB 分块读取超限即 413；上限由 `/structured/formats` 下发，前端文案同源；多表格、重名列、嵌套值、多余列全部进 `warnings`；事实 `source_ref` 追加 `file_name / file_format / file_sha256 / upload_id`；快照可用 `DELETE /structured/uploads/{id}` 清除（合规） |
| 页面重命名与结构重排 | 「蒸馏任务」更名为「知识生产」，页首新增生产总览卡（三条入径：文档与消息 / 结构化接入 / 手工修正，各自闸门汇聚到唯一事实底座 `knowledge.facts`，附待复核 / 待审核主张 / 已发布事实三项统计）；上传文档与钉钉 DWS 合并为「① 文档与消息来源」，结构化卡按「契约 → 数据 → 闸门」分段，「蒸馏任务状态」改为「③ 生产流水线与复核」并修正口径（结构化不走文档流水线，留痕在「同步留痕」）；「知识中心」更名为「知识库」。仅改前端名称与结构，路由、接口与数据不变 |

**同时补齐两处既有缺口（G1/G2）**：

- `facts.visibility` 此前「写入了但检索没消费」：`app/visibility.py` 统一白名单，
  问答召回 / 事实列表 / 实体画像三处共用 `owner|organization` 判定，`restricted` 与未知值 fail-closed。
- `lineage.link_checks` 的 `facts_to_memory` 分母改为「有 claim_id 的活跃事实」（结构化事实按设计不进个人记忆），
  分子与 0.8 阈值不变；新增第 8 项 `structured_facts_traced` 要求结构化事实 `source_ref` 六要素 100% 齐备。

验收：`pytest 80 passed`；端到端实测契约闸门 400、preview 零写入、同步 5 断言 / 1 关系 / 2 实体、
重复同步全 duplicate、调岗 1 superseded、`asof` 回看正确、`link_checks` **8/8 全绿**；
文件接入实测 csv / xlsx / md / json 解析列与行一致、超限拒绝、上传→预览→同步复用同一快照、清除后引用返回 400。
安全铁律（消息逐条确认、D3 恒否、Claim 审核闸门、外发总开关）一律未改动。

## 知识图谱（2026-11-12 第十六批）

图直接建在 PostgreSQL 上（Neo4j 仍冻结，见蓝图判据），只读、零写路径：

| 能力 | 实现 |
|---|---|
| 关系类型目录 | `modules/ontology/graph.py`：35 个已登记类型（label / 源目标类型约束 / 基数 / 家族）；**未登记类型不阻断、但在统计里单列**，用于暴露抽取噪声 |
| 类型约束检测 | 每条边按目录校验两端实体类型，违规边在图上画橙色虚线并在统计计数；首测即暴露 7 条（如 `本人 can_login_via 公司`、`某某 has_role 项目`） |
| 图查询 API | `GET /ontology/graph`（k≤3 跳子图，递归 CTE + path 数组环保护）、`/ontology/graph/stats`（孤立点 / 连通分量 / 度数前五）、`/ontology/graph/paths`（≤4 跳简单路径）、`/ontology/relation-types`（目录 + 用量） |
| 控制台可视化 | 本体页「知识图谱」卡：力导向布局（无第三方依赖）、节点按类型着色、度数决定半径、点击节点高亮相邻并联动实体画像、深度 1–3 / 状态 / 隐藏孤立点 / 全图切换 |
| 图谱质量 | 首测如实暴露：94 实体中 **79 个孤立（84%）**、81 个连通分量——关系抽取覆盖不足是下一步的本体工作重点 |

验收：`pytest 145 passed`（新增 19 例图谱用例）；四个端点在真库实测通过；
路径查询可回答「本人 →完成→ 项目」「本人 →汇报给→ 某某丙 →负责→ 项目」。

## 蓝图冻结项与实施状态

蓝图第十五节「最终工程决策」原文（逐字，见 `docs/blueprint-v1.0.txt` 第 1216–1233 行）：

| 技术或能力 | MVP | 后续 |
|---|---|---|
| RAGFlow 1.0 | MVP | 持续升级 |
| FastAPI / LangGraph / PostgreSQL + pgvector / MinIO / Celery + Redis / LinkML | MVP | — |
| Neo4j | 暂缓 | 复杂图查询 |
| Qdrant | 暂缓 | 独立向量服务 |
| Temporal | 暂缓 | 复杂长流程 |
| OPA | 暂缓 | 复杂授权策略 |
| 模型微调 | 暂缓 | 有高质量样本后评估 |
| 自动决策执行 | 禁止 | 分场景授权 |

**六项暂缓 + 一项禁止，当前全部未引入，与蓝图一致。**

> 表格读法：原文 MVP 列对前七行是**空单元格**（表示「属 MVP 范围」），此处按上下文补注为「MVP」以保证可读；
> 后六行原文明确写「暂缓」，最后一行写「禁止」。

口径更正：此前本文写「全部预留接口」，不准确。代码中**只有 D3 真预留**——`workflow.action_authorizations` 表、
`policy/engine.py` 的 D3 分支恒返回拒绝、`workflow.py` 执行前重校验；`neo4j / qdrant / temporal / opa`
在 `backend/app` 中**没有任何客户端或 adapter**，仅「架构允许后补」（ADR-001：图与向量均为可重建的派生服务）。

每项的解冻判据（量化触发条件 + 工程内容 + 回滚方式）见
[`docs/blueprint-v1.1-implementation.md` 附录 B](docs/blueprint-v1.1-implementation.md)。
**前置条件**：先建评估体系——没有基准指标，无法回答「该不该解冻」「解冻后是否变好」。

## 与蓝图的差距

功能面已覆盖蓝图第 1–9 周全部范围，并超出（结构化旁路、消息接管、链路观测）；第 10 周只完成决策/审批/审计，**「完整评估测试」未做**。仍有 7 处偏差，按严重度：

| # | 偏差 | 严重度 | 现状 |
|---|---|---|---|
| A1 | pgvector 未启用 | 高 | 蓝图把 `PostgreSQL + pgvector` 列为 MVP；当前 `semantic_chunks.embedding` 是 TEXT，281 切片仅 45 条有向量，无 HNSW/IVFFlat 索引，语义检索≈不可用 |
| A2 | evaluation 模块缺失 | 高 | 蓝图列为 **P0** 且给了题量与指标（QA 100–200 题等）；当前零实现，检索/Prompt 改动无法判断退化 |
| A3 | RAGFlow 未接入 | 中 | 只有 `config.ragflow_base_url / api_key` 两个配置项，无客户端；解析由 MinerU/docling 适配层替代 |
| A4 | Celery 未真异步 | 中 | 状态机与幂等齐备，但本机 `CELERY_TASK_ALWAYS_EAGER=1`，长任务仍占 API 进程；Redis 未运行 |
| A5 | LinkML 未使用 | 低 | 本体用 DDL + `ontology_versions.schema_content`(JSONB) 表达 |
| A6 | SSE / Checkpointer / interrupt 缺失 | 低 | 会话落 `workflow.twin_sessions`，审批走 `approval_tasks` + HTTP approve，语义等价 |
| A7 | 数据用途标签只有一维 | 低 | 蓝图要求 `privacy_level + allowed_purposes + external_sharing`；当前只有 `visibility`（owner/organization，fail-closed） |

蓝图中暂缓实现的两条任务链（Planning、Cognitive Reasoning）未实现；`/twin/tasks/{id}/stream`（SSE）缺。

## 第十七~十八批：图谱动态交互 + 图与事实底座对齐 + 本体工作台（2026-11-12）

**图谱动态化**：静态一次性布局 → 逐帧力导向模拟；节点拖拽（松手固定、双击解钉）、
滚轮/按钮缩放（0.25–5×）、空白拖拽平移、双击适应窗口、点击高亮相邻并联动画像；
ResizeObserver 自适应容器尺寸。

**图与事实底座对齐（根因修复）**：图此前是「第二套真源」——蒸馏时按
`object_value.name` 精确同名建边，结构化事实（`{"value": ...}`）从不产生边，
导致 84% 实体孤立、某某的 `reports_to/works_at/level` 在图上不可见。
新增 `modules/ontology/projection.py`（事实→关系确定性投影，纯函数）：

- 谓词映射（含 `responsible_for→is_owner_of` 等别名）；属性谓词
  （level/job_title/department-N）永不成为边；
- 值→实体解析：全名 → 别名 → 包含（实体名 ≥3 字且覆盖率 ≥50%，防「项目」吞掉
  「数据安全项目」），歧义不建边；
- 类型约束前置：`Person -has_role-> Project` 这类不再生成；
- `POST /ontology/relations/reconcile`（默认 dry-run）+ `/relations/drift`：
  投影边随事实增删；历史边只删「可证明是错的」（自环/类型不符/证据被拒），
  其余保留待复核；人工边（origin=manual）永不动；
- 审核通过 Claim 时即时投影（`ensure_fact_edge`），不等全量对账。

执行结果：+14 条（含 某某 reports_to→某某乙、works_at→公司）、−6 条错边
（某某丁 announced 某某丙、本人 can_login_via 公司 等），类型违规 7→1。

**本体工作台**（`modules/ontology/quality.py` + `workbench.py`，控制台本体页新卡）：

| 能力 | 内容 |
|---|---|
| 模式视图 | 实体类型环状图 + 允许关系类型的类型对连线（粗细=实际用量）；类型点选筛选；未定义/未登记类型单独标色 |
| 规则校验 | R1~R10（未登记类型/类型约束/单值冲突/自环/缺证据/悬空端点/重复/档案异常/属性值指实体/孤立实体），分级 error/warn/info，附样例 |
| 测试验证 | 规则引擎自测：内置正反例证明确「该报的报、不该报的不报」，页面常驻展示自测结论 |
| 编辑 | 实体改名/类型/别名（旧名自动保留为别名）；手工建边（页面与接口共用同一套校验：类型约束/自环/未登记/重复/单值冲突分别拦截 400/409）；删边软删留痕 |

测试 166 passed（+21：投影 10、规则 11）。**已知边界**：464 条活跃事实中
109 条的宾语是长文本（believes/plans_to 等），不解析成实体就不建边——
图反映的是「实体间关系」，要把这些句子变成边需要先建实体（建模决策，未做）。

## 第十九~二十批：V1.2 重规划 + 模式视图多布局（2026-11-12）

按《企业数字员工智能体平台蓝图 V1.2》重做开发计划（`docs/development-plan-v1.2.md`，
蓝图原文归档 `docs/blueprint-v1.2.txt`）；模式视图升级四种布局（环形/树状/分组/
力导向）+ 滚轮锚点缩放（20–400%，右下角百分比徽标）+ 拖空白平移 + 节点拖拽微调；
四布局静态对照页 `docs/layout-preview.html`。四布局几何用真实数据验证（无 NaN、
无空面板、力导向发散 bug 已修）。

## 第二十一批：图 ↔ 本体双向同步（2026-11-12）

此前只有「审核通过 → 建边」一个方向，存在四种对不上账。新增
`modules/ontology/sync.py`，四条规则全部收口：

| 规则 | 触发 | 行为 |
|---|---|---|
| R1 | 事实 active/变更 | 有且只有一条投影边（`upsert_fact_edge`；已存在的边补记 `fact_id`） |
| R2 | 事实撤销/失效/删除 | 投影边软停用，留痕谁停的、为什么 |
| R3 | 人工建边 | 自动补一条本体事实（`object_value` 用**实体 id** 不用名字），图上有的本体里也查得到 |
| R4 | 人工删边 | 连带撤销背后事实，否则下一轮对账必然复活 |

配套：`/ontology/sync`（dry-run 预览→显式落库）、`/ontology/sync/status`、
`/ontology/sync/orphans`（三类孤儿：事实已撤/实体停用/人工边缺事实）、
`/ontology/sync/fact/{id}`、`/ontology/sync/entity/{id}`；实体改名/改类型后
自动重投影其事实；控制台工作台新增「图谱 ↔ 本体」同步状态条（检查 → 对齐两步）。
对账口径修正：与全部活跃边比对，修掉「人工边被对账重复插边」的缺陷。
投影支持 `object_value.entity_id` 直接定位（改名/重名不再影响对账）。

验证：pytest 183 passed（+17：同步规则 14、投影 3）；
端到端实测 17/17（`scripts/verify_sync.py`）：建边→事实生成、对账不复活、
删边→事实撤销、撤销→边消失，全部通过。

## 第二十二批：CQ（Competency Questions）能力问题（2026-11-13）

按评估文档 `docs/cq-ontology-assessment.md` 落地第一阶段：**CQ 定义 + 确定性抽取 +
覆盖率与缺口**。CQ 不写查询语言，存「意图 + 主语类型 + 需要的关系/属性 + 答案形态」，
一套定义可翻译成事实查询 / 图路径 / 时间切片 / 聚合 / 存在判定五种只读执行计划。

- 表：`ontology.competency_questions`（含 `subject_side` 支持反向问题）、
  `ontology.cq_coverage_snapshots`（每次跑批留档看趋势）。评估文档里的
  `cq_requirements` 表折进了 `required_relations/required_attributes` 两个 JSONB
  字段（1:1 派生，单独建表只会多一张没人读的表）。
- 种子：内置 **16 条 CQ**（全部 `draft` 待业务方审核），按真实数据写：
  汇报对象、职级岗位、部门归属、负责项目、项目参与人、关系链路、时点角色、
  部门人数、观点偏好、制度约束、文档溯源、系统负责人、项目交付时间、技能、立项判定。
  `POST /ontology/cq/seed` 幂等写入，**已审核的不覆盖**。
- 确定性抽取 `analyze()`：问句名词→实体类型（按出现位置定主语，
  「ChatBI 系统的负责人」主语是 System 不是 Person）、动词→关系类型、
  修饰→属性；命不中的如实报「未命中」，不擅自造类型。
- 覆盖率 `coverage()`：逐条评估 → `answered(≥0.6)/partial/unanswerable` 三态 +
  六类缺口（缺实体/类型未声明/关系未登记/无关系实例/缺属性/缺证据），每个缺口
  带建议动作；**样例实测**用样例问句里的真实实体实际取一次值，
  区分「问题没用」和「数据只覆盖个别实例」。
- API：`GET/POST/PATCH /ontology/cq`、`/ontology/cq/seed`、`/ontology/cq/analyze`、
  `/ontology/cq/coverage?persist=`、`/ontology/cq/meta`。
- 控制台：本体工作台新增「**CQ 与缺口**」页签（列表 + 审核/编辑表单 +
  写入种子 + 跑覆盖率）；支持 `?view=ontology&tab=cq` 深链接直达。

首轮真实数据跑批：16 条 CQ = 9 条部分可答 + 7 条答不上（平均覆盖 4.2%）。
缺口分布：无关系实例 11、类型未声明 3（System/Document/Policy 有实体但
本体 v1.0.0 未声明）、缺证据 5、缺属性 2、关系未登记 1（`applies_to`，
即 CQ-011 用来驱动本体扩展的样例）。

测试：pytest 211 passed（+28：CQ 规则 26、主语判定 2）。

---

## 第二十三批：本体中心改版（导入驱动，2026-11-20）

**本体页面从「手动建模的四页签工作台」换成「导入驱动的一条主线」**：
导入本体文件 → 自动生成设计图 → 灌入数据 → SPARQL 控制台 / 语义问答。
旧页签（图 / 模式 / 规则 / 校验 / CQ）与其模板全部下线，页签改名为「本体中心」。

- 后端新模块 `backend/app/modules/ontology/kg/`：`store.py`（会话：内存 + 落盘
  `.runtime/kg/<sid>/`，LRU 20，重启惰性载回）、`model.py`（图 → 设计模型：类、层级、
  属性、`owl:oneOf`、`owl:Restriction`、个体数、前缀收敛）、`query.py`（只读 SPARQL
  执行 + 29 条示例集）、`qa.py`（LLM 生成 SPARQL + 模板兜底）。API 13 个端点挂在
  `backend/app/api/v1/kg.py`。
- 导入：按扩展名猜格式，失败再依次重试 Turtle / RDF/XML / JSON-LD / N-Triples / N3；
  多文件逐个试，失败项带原因回报。已有本体的会话里再拖文件 = 灌数据（按内容判断用途）。
- 设计图：`subClassOf` 深度分层 + 对象属性 domain→range 连线，实线带标签、虚线表继承，
  滚轮缩放 / 指针平移 / 点节点看详情（属性表、出向入向关系、约束、枚举、实例数）/
  导出 SVG（把 `--ax-*` 令牌解析成内联颜色，脱离页面也能看）。
- 示例集 29 条：19 条对齐员工本体（实体刻画、多跳、传递闭包、聚合、五类一致性校验、
  结构缺口、类层级），10 条只用 RDF/RDFS/OWL 标准词汇、任何本体都能跑。页面点一下即执行，
  可下载 `.rq`；不是员工本体时那 19 条标「需对应本体」。
- 问答：把 `schema_brief()` 压出来的结构摘要喂给模型出 SPARQL，执行失败带着报错重试一轮，
  仍失败降级关键词模板；答案、执行到的查询、每轮实际报错都在页面上可见，引擎用徽标区分。
- 修掉前缀丢失：图落盘再读回时 rdflib 会把短前缀改名成 `ns1`，查询结果单元格直接读
  `graph.namespaces()` 就在页面上露出 `ns1:emp001`。现在由 `model._Names` 收敛出一份
  前缀表，经 `kg.py::_pfx_of(sess)` 传给 `query.run()`，`_curie` 与 `with_prefixes`
  共用同一份映射——结果表格、设计图、提示词三处前缀永远一致。
- 旧入口兜底：后台巡检会走到已下线的 `#wbSchemaList` / `#wbPane-schema`，
  `drawSchemaList()` 与 `wbTab()` 都加了「元素不在就安静返回」的守卫。

首轮真实数据验证：导入 `docs/samples/employee-ontology.ttl` → 230 三元组 / 6 类 /
7 对象属性 / 15 数据属性 / 14 个体；灌入 `docs/samples/employee-demo-data.ttl` →
281 三元组 / 22 个体；示例集 **29/29 实跑通过**；问答「技术部有哪些员工？」模型生成，
答「共 3 条：张三、赵六、王五」；浏览器控制台错误 0。

说明文档：`docs/ontology-kg-guide.md`；示例文件：`docs/samples/`。

> 原「CQ 与缺口」页签随旧工作台下线，接口 `/ontology/cq*` 全部保留；
> `?view=ontology&tab=cq` 现在落到语义问答区。

---

## 第二十四批：设计助手对话化 + 浮层 + 问答并入（2026-11-20）

三条叠加改动，把本体设计页从「画布 + 侧栏 + 独立问答卡」改成「满宽画布 + 一个可拖可收的对话式助手」。

**1. 画布独占整行。** 主区从两列栅格改成单列：画布宽度直接等于内容区宽度，不再被侧栏挤扁；
原来右栏的类详情排到画布下方，SPARQL 控制台再往下。屏宽 ≤1180px 时才退回双列。

**2. 助手改多轮对话。** `POST /kg/design/draft` 增加 `history` 入参，把上一版 Turtle（截 16000
字符）+ 最近 8 轮（每轮截 700 字）一起回喂，所以「给合同加个金额字段」这种增量指令能落在上一版
草稿上，而不是重新从零设计。每轮回一句变更摘要，由后端 `compare()` 两次设计图的类 / 关系 /
数据属性集合算出 `delta`（新增、删除、旧→新改名），前端渲染成「改好了：『巡检单』加了 巡检结论，
现在共 2 个类 / 2 条关系 / 5 个数据属性」。只有最新一版草稿挂四个动作按钮（应用为新设计图 /
并入当前本体 / 下载 Turtle / 复制），旧气泡的按钮自动摘掉，避免误点历史版本。

**3. 助手变成右上浮层，且吞掉语义问答。** `#kgoAiCard` 是 `position: fixed`（`top:78px;
right:22px; width:388px`），头部可拖（pointer 事件 + `setPointerCapture`，放行头部按钮），
可收成胶囊 `#kgoAiFab`；位置落 `localStorage["pdt.kgo.ai.pos"]`、收起态落 `["pdt.kgo.ai.fold"]`，
刷新后原地复原。**独立的「语义问答」卡片整块删除**，问答并进同一个对话框：三档模式（自动 / 改本体 /
问数据），自动档按三条件判定（有会话 + 已灌个体 + 含问句特征词）才走问答，答案仍是带结果表、
执行到的 SPARQL、耗时的助手气泡；`?view=ontology&tab=ask|qa|chat|cq` 深链接统一落到「展开浮层
+ 切问数据」。屏宽 ≤1180px 浮层变底部抽屉、拖动禁用。

实机验证（iframe 自检页 + 标题回读，绕过截图不可用与 DOM 快照截断）：浮层 `position=fixed`、
几何 `1075,78,388,454`（`1500-388-22=1090`，差 15 = 滚动条，说明 `right:22px` 相对内容区生效）、
收起三态 `hidden/visible/display:none` 一致、拖动 `1075,78 → 875,218` 且刷新后不丢、路由
`给合同加个金额字段→design` / `技术部有哪些员工？→qa`、问答气泡 4 行结果 + 两个折叠 +
`err=false`、两轮对话第二轮 reply 为「改好了…共 2 个类 / 2 条关系 / 5 个数据属性」且点
「应用为新设计图」后新会话 `cls=2 / trip=38 / 画布 2 节点`。静态自检：`node --check`
（15566 行内联 JS）通过、7 类标签计数平衡、4 个后端文件 `py_compile` 通过、
旧问答 DOM/函数 `grep` 零命中。

细节见 `docs/ontology-kg-guide.md`（布局表、助手小节「一个对话框，两种活」、问答入口说明、
验证记录与已知限制）。

