# 对象驱动本体设计（Object-Driven Ontology Design）

> 状态：Draft → 落地中（P0/P1）  
> 适用范围：`web/ontology-studio` + `/api/v1/ontology/objects*`  
> 相关：`ontology-studio-v2.md`、`palantir-style-business-object-modeling.md`、`knowledge-ontology-spec.md`

## 1. 目标

将本体工作台从「关系 / 映射中心」升级为「**业务对象中心**」：

- 业务人员以 **Object** 理解世界（医院、科室、报警事件…）
- 每个对象显式拥有：**属性 → 类型 → 函数 → 行动**
- 关系与数据源映射仍保留，但作为对象上的边与绑定，而非唯一入口

## 2. 元模型

```
ObjectType
├── identity[]          # 业务键 / 主键属性名
├── label, description, domain
├── properties{}        # name → Property
├── relations{}         # name → Relation (from this object)
├── functions{}         # name → Function (derived / aggregate)
├── actions{}           # name → Action (副作用 + 策略)
└── quality             # 绑定完整度、未映射字段数等摘要
```

### Property
- `label`, `value_type`, `role`（identity / timestamp / measure / enum / text…）
- `source?`：`{ table, column }` 物理绑定
- `required`, `enum?`

### Relation
- `label`, `target`（ObjectType 名）, `cardinality`
- `inverse?`, `semantic?`
- 可选 source 绑定（FK / 关联表）

### Function
- `kind`: `aggregate` | `derived` | `metric`
- `op` / `expr`, `on`（依赖属性）, `output_type`

### Action
- `label`, `params[]`
- `effects[]`: create_object / write_relation / notify / start_workflow
- `policy`: open | approval_required | owner_only

## 3. API

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/v1/ontology/objects` | 对象目录（可 `?demo=1` 强制示例域） |
| GET | `/api/v1/ontology/objects/{id}` | 对象完整定义 |
| GET | `/api/v1/ontology/objects/{id}/graph` | 四列画布专用展开结构 |

无已发布 schema 时回落 **demo 域**（Hospital / Department / Alarm），保证前端可独立演示。

## 4. UI

新增 Tab：**⓪ 对象设计**

- 左：对象目录
- 中：四列 SVG 画布（Property → Type → Function → Action）
- 右：Inspector

保留原有：① 关系设计 ② Pipeline/映射 ③ 行为与指标 ④ 治理

## 5. 阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| P0 | 对象目录 API + 只读详情 | 本提交 |
| P1 | 四列画布 `object-graph.js` + 对象设计 Tab | 本提交 |
| P2 | 属性源绑定写回 mapping 草稿 | 后续 |
| P3 | Function 预览接指标引擎 | 后续 |
| P4 | Action 接 workflow | 后续 |

## 6. 与现有契约关系

- `schema.entity_types` / `attributes` / `relation_types` → 聚合为 ObjectType
- automodel `contract.mappings` → Property.source
- 指标注册表 → Function（kind=metric）
- 不删除关系视图与 mapping-graph

