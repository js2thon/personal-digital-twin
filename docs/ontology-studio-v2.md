# Ontology Studio V2

入口：`/ontology-studio/`（Docker Web）或对应静态站点路径；现有 Ontology App Studio 顶栏增加入口。

## 能力

- **本体关系设计**：RDF/OWL KG Session 视图展示 Object Types、Properties、Link Types；点击对象查看 URI/CURIE、实例数、属性、出入向关系。
- **自动建模 Schema 图**：通过 `studio/schema-graph` 展示 Object Type、Property 节点与 `hasProperty` / Link Type 边；点击对象查看主键、来源表、值类型和关联关系。
- **端到端数据血缘**：通过 `/pipeline` 读取数据源、数据画像、本体草案、人工审核、映射契约、契约校验、物化、指标、发布和图谱写入阶段及其状态。
- **映射与数据概览**：读取自动建模 Schema 的 entity_types / relation_types / attributes、来源表、主键与 Pipeline 阶段摘要。
- **Actions**：读取 `studio/actions` 中真实的对象操作审计记录；不在工作台中绕过权限策略执行写操作。
- **Functions / Metrics**：展示自动建模任务的指标定义与依赖血缘，并读取租户指标注册表。
- **质量与治理**：读取 `studio/quality` 的实体/关系统计、孤立实体、悬空引用、非法关系类型、自环、重复边和契约问题；显示任务版本与审核信息。
- **证据与置信度**：仅展示服务端审核队列实际返回的证据、决策和分数；未返回时明确显示暂无数据。
- **跨视图联动**：对象详情可跳转血缘和指标视图；左侧会话与自动建模任务可切换。

## 数据接口

### RDF / OWL Session
- `GET /api/v1/kg/sessions`
- `GET /api/v1/kg/{session_id}`

### 自动建模任务工作台
- `GET /api/v1/automodel/jobs`
- `GET /api/v1/automodel/jobs/{job_id}?full=true`
- `GET /api/v1/automodel/jobs/{job_id}/studio/overview`
- `GET /api/v1/automodel/jobs/{job_id}/studio/schema-graph?props=true`
- `GET /api/v1/automodel/jobs/{job_id}/studio/quality`
- `GET /api/v1/automodel/jobs/{job_id}/studio/actions`
- `GET /api/v1/automodel/jobs/{job_id}/pipeline`
- `GET /api/v1/automodel/jobs/{job_id}/versions`
- `GET /api/v1/automodel/jobs/{job_id}/metrics`

### 指标注册表
- `GET /api/v1/ontology/metrics`

页面使用与 Ontology App Studio 相同的 API Base / API Key 会话设置（`ontoStudioApiBase` / `ontoStudioKey`）。

## 设计边界

1. RDF Session 与 Automodel Job 是不同数据域；页面不会把 KG Session 当作语义实例库，也不会假设两者已自动投影。
2. 自动建模 schema 的关系图使用 `from/to` 边结构；RDF Session 图使用 KG API 返回的对象与关系结构。
3. Actions 面板展示服务端操作日志；写操作仍须通过现有后端 Action API、权限策略与再次写入流程。
4. Functions 当前以指标定义/计算语义呈现；尚未提供独立的函数目录 API 时，不虚构函数清单。
5. 字段级实例血缘当前取决于后端持久化的 source_ref / fact lineage 能力；页面不根据字段名猜测来源。
6. 页面为只读治理视图；审批、发布、写库仍走现有后端 API 和人工确认流程。
7. 权限、租户隔离和行级访问由服务端执行；前端只展示 API 已授权返回的数据。

## 校验

`node web/ontology-studio/validate.mjs` 检查内嵌 JavaScript 语法、DOM 引用和关键 API 集成。
