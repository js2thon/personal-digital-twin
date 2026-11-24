# 业务对象优先的本体建模

## 建模原则

本体建模以业务对象（Object Type）为中心，不以数据库表为中心。数据表是对象的一个或多个 backing datasource；源字段通过 MappingContract 映射到对象属性或 Link Type。已发布本体仍是对象、属性、关系语义的权威，MappingContract 是数据绑定与转换规则的权威。

## 对象角色

| 角色 | 含义 | 示例 |
|---|---|---|
| master | 静态主数据/核心业务主体 | Hospital、Department、Material、ServiceItem |
| event | 业务事件或明细记录 | DepartmentDailyOps、MaterialUsage、FinancialDaily |
| derived | 聚合/派生业务对象 | DepartmentMonthlySummary、HospitalDailySummary |
| reference | 字典、分类、枚举参照对象 | 医院等级、耗材分类 |

角色是业务语义，不由表名直接决定。系统可给出名称提示，但必须由业务人员确认。

## 业务对象最小定义

- 稳定对象标识：primary_key；复合标识暂需显式声明，不得伪装成单字段。
- 业务名称与定义：label、business_definition。
- 角色：business_role。
- 业务粒度：business_grain，尤其用于事件和派生对象。
- 事件时间：event_time，仅事件对象适用。
- 生命周期：lifecycle；来源系统：source_system。
- 属性按标识属性、业务属性、计算属性管理；对象间引用使用关系，不重复存储为普通字符串属性。

## 关系类型

| semantic_kind | 含义 |
|---|---|
| belongs_to | 归属 |
| contains | 包含/组成 |
| uses | 使用/消耗 |
| provides | 提供/开展 |
| associates | 一般业务关联 |
| derived_from | 派生来源/计算血缘 |

每条关系需声明源对象、目标对象、方向、基数；必要时定义 inverse_name。derived_from 表示数据计算来源，不能替代业务关联关系。

## 推荐建模顺序

1. 从业务文档/数据集识别候选业务对象及业务定义。
2. 识别对象身份、粒度、生命周期和对象角色。
3. 识别业务关系及其方向、基数和业务含义。
4. 为对象定义标识属性、业务属性、计算属性。
5. 绑定一个或多个数据源，生成 MappingContract 草稿。
6. 执行结构、覆盖度、一致性、可执行性和业务对象质量检查。
7. 预览、人工审核、版本化发布；未经批准不写生产本体或映射。

## API

- POST /api/v1/ontology/modelling/business-objects/analyze：从现有建模 spec 投影业务对象模型并给出角色建议。
- POST /api/v1/ontology/modelling/business-objects/validate：执行对象身份、关系端点、关系语义、基数和业务粒度检查。

以上端点为纯计算，不连接数据源、不写 Facts/Claims/图数据或生产 MappingContract。
