# Ontology Studio V2 后端接入说明

本目录由 `ontology_studio_v2.py` **严格抽取**，去掉 Streamlit / PyVis 页面层，保留：

| 能力 | 位置 |
|------|------|
| `Ontology` / `AutoOntologyBuilder` | `core.py` |
| 主键推断、自动外键、手动关系 | `core.py` |
| 质量报告、最短路径 | `core.py` |
| Schema / 实例图 JSON | `schema_graph_payload` / `instance_graph_payload` |
| Action CRUD、导出 JSON/OWL | `core.py` |
| LLM 工具与问答 | `core.py` |
| 进程内会话 | `service.py` |
| FastAPI 路由 | `app/api/v1/studio_v2.py` |

## 1. 拷贝路径

```
backend/app/modules/ontology/studio_v2/
  __init__.py
  core.py
  service.py
  INTEGRATION.md

backend/app/api/v1/studio_v2.py
```

## 2. 注册路由

在 API 聚合处（常见为 `backend/app/api/v1/__init__.py` 或 `router.py`）：

```python
from app.api.v1.studio_v2 import router as studio_v2_router

api_router.include_router(studio_v2_router)  # → /api/v1/studio-v2/...
```

确认 `app` 包路径与现有 `backend/app` 一致。

## 3. 依赖

已有项目一般已具备；否则：

```
pandas networkx rdflib openpyxl
openai   # 仅问答需要
```

**不需要** streamlit、pyvis。

## 4. API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/studio-v2/health` | 健康检查 |
| GET | `/studio-v2/sessions` | 会话列表 |
| GET | `/studio-v2/sessions/{id}` | 会话摘要 |
| DELETE | `/studio-v2/sessions/{id}` | 删除会话 |
| POST | `/studio-v2/build/upload` | 多文件 CSV/Excel 建模 |
| POST | `/studio-v2/build/records` | JSON 行记录建模 |
| POST | `/studio-v2/infer-primary-keys` | 仅推断主键 |
| GET | `/studio-v2/sessions/{id}/summary` | 摘要 |
| GET | `/studio-v2/sessions/{id}/schema-graph` | Schema 图 JSON |
| GET | `/studio-v2/sessions/{id}/instance-graph` | 实例图 JSON |
| GET | `/studio-v2/sessions/{id}/quality` | 质量报告 |
| POST | `/studio-v2/sessions/{id}/path` | 最短路径 |
| GET | `/studio-v2/sessions/{id}/objects/{type}` | 实例列表 |
| POST/PATCH/DELETE | `/studio-v2/sessions/{id}/objects` | Action |
| GET | `/studio-v2/sessions/{id}/actions` | Action 日志 |
| POST | `/studio-v2/sessions/{id}/tools` | 执行本体工具 |
| POST | `/studio-v2/sessions/{id}/ask` | 降级问答 |
| GET | `/studio-v2/sessions/{id}/export.json` | 导出 JSON |
| GET | `/studio-v2/sessions/{id}/export.owl` | 导出 OWL |

### 建模示例（multipart）

```bash
curl -X POST http://127.0.0.1:8100/api/v1/studio-v2/build/upload \
  -F "files=@员工.csv" -F "files=@部门.csv" \
  -F 'primary_keys_json={"员工":"工号","部门":"部门编号"}' \
  -F 'manual_links_json=[]' \
  -F "name=HR样例"
```

### 实例图示例

```
GET /api/v1/studio-v2/sessions/{id}/instance-graph?types=员工,部门&max_nodes=300&search=&hops=1
```

## 5. 与前端「本体建模」的关系

- 本模块**只提供后端**；`automodel.html` 中删除「本体与审核 / 映射与发布 / 实例与关系」后，新「本体建模」页应调用上述 API。
- 图谱返回 **nodes/edges JSON**，前端用现有 React Flow / SVG / vis 渲染即可，风格与工作台其余页签统一。
- 旧 mapping/publish/instance 专用接口可逐步下线，避免双轨。

## 6. 与原 Streamlit 文件的对应

| Streamlit | 本模块 |
|-----------|--------|
| `Ontology` | `core.Ontology` |
| `AutoOntologyBuilder` | `core.AutoOntologyBuilder` |
| `create_schema_graph` (HTML) | `schema_graph_payload` (JSON) |
| `create_instance_graph` (HTML) | `instance_graph_payload` (JSON) |
| `quality_report` / `find_shortest_path` | 同名 |
| `execute_ontology_tool` / `ask_llm*` | 同名 |
| `st.session_state.ontology_v2` | `StudioV2Store` 会话 |

算法与数据结构未改写，仅去掉 UI 与 PyVis。


### 上游数据与画布联动说明

- Schema Graph 的对象节点返回 `source`（来源表、来源类型、行数、字段列表）；属性节点返回 `source`（来源表/列、是否已映射）；关系边返回 `source_binding`（起点表列与终点表列）。
- `compare-dataset` 只生成结构/字段类型/记录数差异，不修改当前会话、本体或 MappingContract。差异必须经过人工审核，再进入后续数据重载和映射发布流程。
- 画布坐标通过 `/layout` 回写到当前 Studio V2 会话。当前 `StudioV2Store` 是进程内存储，进程重启后会话与布局都会丢失；生产环境需要迁移到数据库/持久化仓储后才能承诺长期保存。
