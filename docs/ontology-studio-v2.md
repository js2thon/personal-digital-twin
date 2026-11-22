# Ontology Studio V2

入口：`/ontology-studio/`（Docker Web）或对应静态站点路径。

## 能力

- **本体关系设计**：读取 RDF/OWL KG Session，展示 Object Types、Properties、Link Types；点击对象查看 URI/CURIE、实例数、属性、出入向关系。
- **数据血缘**：读取自动建模任务，展示来源、结构画像、语义草稿、审核、发布契约、指标的流水线阶段；显示任务实际返回的指标依赖。
- **行为与指标**：预留 Actions / Functions 目录区；不在后端目录 API 尚未提供时伪造可执行动作。读取现有租户指标注册表。
- **质量与治理**：展示任务质量问题、审核候选、证据、置信度和版本元数据；安全边界由服务端鉴权和策略执行。
- **跨视图联动**：对象详情可跳转血缘和指标视图；左侧本体会话和自动建模任务可切换。

## 数据接口

- `GET /api/v1/kg/sessions`
- `GET /api/v1/kg/{session_id}`
- `GET /api/v1/automodel/jobs`
- `GET /api/v1/automodel/jobs/{job_id}?full=true`
- `GET /api/v1/automodel/jobs/{job_id}/metrics`
- `GET /api/v1/ontology/metrics`

页面使用与 Ontology App Studio 相同的 API Base / API Key 会话设置（`ontoStudioApiBase` / `ontoStudioKey`）。

## 设计边界

1. RDF 会话与自动建模任务是不同数据域；页面不会把 KG Session 当作语义实例库，也不会假设两者已自动投影。
2. 只有 API 实际返回的证据、置信度、接口、值类型、版本和指标才会显示。缺失字段明确显示“暂无数据”。
3. Actions / Functions 目前展示能力目录占位；执行动作必须以后端动作注册表、参数校验、权限策略和审计接口为准。
4. 字段级实例血缘当前取决于后端是否提供对应 instance lineage API；本页不推断字段来源。
5. 前端为只读治理视图；审批、发布、写库仍走现有后端 API 和人工确认流程。

## 校验

`node web/ontology-studio/validate.mjs` 检查内嵌 JavaScript 语法、DOM 引用和关键 API 集成。
