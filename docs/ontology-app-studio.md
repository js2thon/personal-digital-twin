# 本体应用工作台（Ontology App Studio）

## 目标

把本体从“语义模型文件”推进为可使用的业务应用定义：读取本体类、属性、关系与实例，自动形成应用导航、指标卡片、结构关系图、属性分布图和实例明细。

## 页面入口

- Docker Web：`http://localhost:8200/ontology-app-studio/`
- 本机无 Docker：`http://127.0.0.1:8101/console/ontology-app-studio/`

## V1 页面流程

1. **选择或导入本体**：复用 `GET /api/v1/kg/sessions`、`POST /api/v1/kg/import`。
2. **AI 构建本体**：文本需求调用 `POST /api/v1/kg/design/draft`；需求文档调用 `POST /api/v1/kg/design/doc`。生成结果仅为草稿，必须人工确认后调用 `POST /api/v1/kg/design/apply`。
3. **接入实例数据**：复用 `POST /api/v1/kg/{sid}/data` 或 `POST /api/v1/kg/{sid}/data/text`。
4. **自动构建应用**：读取 `GET /api/v1/kg/{sid}` 的类、属性、关系与实例元数据，生成业务对象导航和图谱视图。
5. **自动生成图表**：对有实例的类及其数据属性，构造只读 SPARQL 聚合查询 `POST /api/v1/kg/{sid}/sparql`。低基数分类字段生成饼图，日期/时间字段生成折线图，数值字段生成分布柱状图，低基数分类字段生成饼图，高基数分类字段生成柱状图；图表不使用虚构数据。
6. **应用配置导出**：导出 JSON 配置，浏览器本地保存当前应用配置，便于后续迁移到服务端应用注册中心。

## 当前支持范围与边界

- 页面支持 RDF/Turtle、RDF/XML/OWL、JSON-LD、N-Triples、N3 本体和实例数据。
- CSV/XLSX 暂不直接由此页面导入 KG 会话；应先经过本体字段映射、质量验证、映射版本审批和受控入库 Pipeline。
- 图表聚合以本体显式声明的类/数据属性为依据；无实例、无可聚合属性或查询失败时显示空状态，不推断业务数据。
- 当前图表布局和应用配置保存在浏览器本地并可导出；尚未实现服务端多用户应用发布、权限策略、可视化编辑器和定时刷新。
- API 地址和 API Key 可在页面顶部配置并仅保存在当前浏览器会话的 sessionStorage，不写入应用配置文件。Docker 默认访问同主机的 8100 API 端口；本机无 Docker 模式默认使用当前 8101 地址。
- 关系图首版为本体 Schema 层图示，最多显示 18 个类；不是全量实例级网络图。

## CSV / XLSX 映射 Pipeline（V1.1）

数据接入面板复用既有后端接口，形成以下受控流程：

1. `POST /api/v1/ontology/modelling/dataset/inspect`：上传 CSV/XLSX，解析字段画像并生成语义 Schema Registry 候选映射；文件最大 10 MiB。
2. 用户逐字段显式选择目标实体类型和属性；未选择的字段不参与入库。候选分数仅为规则启发式排序，不是概率。
3. `POST /api/v1/ontology/modelling/mapping/validate`：校验源字段、目标属性、重复映射等结构约束。
4. `POST /api/v1/ontology/modelling/mapping/preview-file`：最多预览 20 行，执行值域转换、实例约束验证并展示字段血缘；不写库。
5. `POST /api/v1/ontology/modelling/mapping-sets`：保存不可变映射草稿版本。
6. `POST /api/v1/ontology/modelling/mapping-sets/{id}/approve`：用户显式审批；只有 `approved` 版本允许执行。
7. `POST /api/v1/ontology/modelling/dataset/execute`：按批准映射执行，单批最多 500 行，支持 append 幂等追加或指定稳定源主键的 upsert。

执行前端再次确认；后端仍强制检查租户归属、审批状态、映射结构、行数上限和事务边界。失败批次回滚并保留失败运行记录。

**重要边界**：当前 Pipeline 通过 `OntologyService.write_instance` 写入语义实例库；本体应用工作台现有图表从 RDF KG Session 的 SPARQL 图读取。两者尚无自动投影适配器，因此本次入库成功不会自动出现在当前 RDF Session 图表中。页面会明确提示此边界，不伪称图表已刷新。下一步需实现语义实例库 → RDF KG Session 的受控投影，或将图表查询统一到语义实例查询服务。

## 下一阶段

- 实现语义实例库到 RDF KG Session 的受控投影适配器，明确实体/属性 URI、稳定 ID、幂等键、版本绑定和回滚策略。
- 增加图表类型推断：时间序列、数值分布、Top-N、占比、交叉分析与明细钻取。
- 增加可配置筛选器、跨图联动、业务指标定义、图表 SQL/SPARQL 说明和证据/血缘下钻。
- 将浏览器本地应用配置升级为租户隔离的服务端应用注册中心，支持版本、发布、回滚和权限控制。
