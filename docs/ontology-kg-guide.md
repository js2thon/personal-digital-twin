# 本体中心（导入 → 设计图 → 灌数据 → SPARQL / 语义问答）

> 入口：控制台左侧「本体中心」，或直达 `http://127.0.0.1:8101/?view=ontology`
> 代码：后端 `backend/app/modules/ontology/kg/`、`backend/app/api/v1/kg.py`；前端 `web/index.html`（`kgo-` 前缀样式 + `kg*` 函数族）

原来的本体工作台是「四页签 + 手动建模」的结构，页面自己不知道本体长什么样，全靠人填。
现在整页换成**导入驱动**：导入一份本体文件，页面立刻按本体内容画出设计图，再灌数据、
跑 SPARQL、用自然语言提问。旧页签（图/模式/规则/校验）连同其模板全部下线。

---

## 一、页面布局，一条主线

| 区块 | 作用 | 关键交互 |
| --- | --- | --- |
| 顶部会话条 | 当前本体、来源清单、6 格统计（三元组/类/对象属性/数据属性/个体/关系） | 下拉切会话、导出 `.ttl`、删除会话 |
| 本体设计图 | 按 `subClassOf` 层级自动分层布局，实线 = 对象属性（带标签+箭头），虚线 = 继承 | 拖节点/拖个体改位置、滚轮缩放、拖空白平移、双击复位、点个体跳本体建模、导出 SVG |
| 类详情 | 点类才出现，紧贴在画布正下方（不再占右侧一列） | 点关系跳转、查看数据属性与约束 |
| SPARQL 控制台 | 查询执行 + 结果表 + 示例集（29 条，点一下即跑） | `Ctrl/⌘ + Enter` 执行、复制查询、下载 `.rq` |
| AI 设计助手（右上浮层） | **同一个对话框干两件事**：说需求 → 设计/改本体；问数据 → 语义问答 | 多轮对话、上传需求文档、模式三键（自动/改本体/问数据）、应用/并入、拖动、收起 |

主区是**单列**：画布独占整行，宽度直接取决于内容区宽度，不再被侧栏挤扁；类详情排在画布下方，
SPARQL 控制台再往下。AI 设计助手不再是栅格占位，而是 `position: fixed` 的**右上浮层**（`#kgoAiCard`），
可拖动、可收成一个小胶囊（`#kgoAiFab`），所以画布从头到尾都是满宽的。屏宽 ≤1180px 时浮层自动变成
底部抽屉、拖动禁用，免得挤占本来就窄的屏。

> **原来的「语义问答」卡片已经整块删除。** 问答能力原样搬进 AI 设计助手的同一个对话框：
> 问题发给助手，答案（含结果表、执行的 SPARQL）以一条助手气泡回来，页面上不再有第二个问答入口。

没有本体时页面只剩一个拖拽区：拖文件进去就是导入本体；已经有本体时拖进去会自动识别为
**数据**文件（按内容判断，不看文件名）。也支持直接粘贴文本导入。右上角助手在空态下也在——
先让 AI 设计一份，再逐步灌数据，这条路同样成立。

### 设计图怎么来的

不是画出来的示意图，是从图里读出来的：

1. 类 = 有 `rdf:type owl:Class`（或 `rdfs:Class`）的资源，外加出现在 `rdfs:domain`/`rdfs:range`
   里的资源（有些本体不显式声明 `owl:Class`）；
2. 布局按 `rdfs:subClassOf` 的深度分列，左→右铺开；没有层级的本体退化成近似正方形网格；
3. 连线两类：`kind: "object"` 走对象属性的 domain→range，`kind: "subclass"` 走继承，前者带
   `rdfs:label` 作边标签；
4. 节点上挂 `实例数`，超过 140 个类会截断并在页面提示（`model.truncated`）。

点节点右侧出详情：中文/英文标签、CURIE、原始 URI、注释、父类与子类、数据属性表（含值类型）、
出向/入向关系（可点着跳）、`owl:oneOf` 封闭枚举、`owl:Restriction` 约束（`min/max/exactly/
someValuesOnly/hasValue` 六种按后端的真实字段渲染）、实例数。

### 画布上的手势

| 操作 | 效果 |
| --- | --- |
| 拖空白 | 平移整图（不改节点坐标） |
| 拖类节点 | 按 `1/scale` 换算成图坐标移动，松手写进 `localStorage["pdt.kg.pos.<sid>"]` |
| 拖个体药丸 | 钉住绝对坐标，写进 `localStorage["pdt.kg.pos.<sid>.ind"]`，此后不再跟随所属类 |
| 双击节点 / 药丸 | 复位这一个：类回自动布局位，个体回所属类下方；`重排` 一次清掉全部覆盖 |
| 滚轮 / `＋` `－` / `适应` | 缩放整图，工具栏中间实时显示百分比（`100%`） |
| `展开个体` / `收起个体` | 个体数 ≤ 60 时默认自动展开，可手动盖开关；超过 60 默认收起，避免糊成一片 |
| 点类节点 | 右侧出详情（拖过一点点不会误触，位移 > 4px 才算拖） |
| 点个体药丸 | 跳到「本体建模」，见下节 |

坐标是**两套独立存取**：类位置和个体位置分开落盘，所以挪动类不会把钉住的个体再拽走。

### 点个体 → 本体建模业务对象

设计图上每个个体药丸点了之后：

1. 按它的 `rdf:type` 找到本体类，取类名的 local part 当业务对象名（如 `employee:Employee` → `Employee`）；
2. 建模草稿里已有同名（或同 label）业务对象 → **只选中并滚动到可见**，不重复建；
3. 没有 → 用这个类的 `rdfs:label`、注释、`data_props` 新建一个业务对象，属性按
   `xsd:` 口径归一到标准类型（`xsd:string → string`、`xsd:dateTime → datetime`、
   `xsd:decimal → number`、`xsd:nonNegativeInteger → integer`…），再生成对应属性节点；
4. 视图切换走 `gotoView("modelling")`，**不刷新整页**，设计图的视角和拖拽结果都保留。

同名判断是幂等的：同一个个体连点两次，第二次只是重新选中，不会堆出第二个对象。

### AI 设计助手：一个对话框，两种活

右上浮层解决的是「手上还没有本体文件」的起点问题，也顺手接管了问答。三条入口：

| 入口 | 走的路 |
| --- | --- |
| 写需求 / 说修改 → `发送`（自动模式判断走哪条） | `POST /kg/design/draft` 或 `POST /kg/{sid}/ask` |
| `上传需求文档`（docx/xlsx/pptx/pdf/txt/md/csv/json/html） | `POST /kg/design/doc`（multipart）→ 抽正文 → 同一套生成流程 |
| 模式条手动锁定（`改本体` / `问数据`） | 跳过自动判断，强制走对应链路 |

**路由靠「三条件全中」才认问句**：有 `sid` + 图里个体数 > 0 + 句子里出现问句特征词（`？吗 哪些 谁
多少 统计 平均 排名…`）。少一条就当设计需求 ———— 这样「给合同加个金额字段吗」不会被误判成查询，
而在空本体上问「有哪些员工」也不会跑去执行一条必然为空的 SPARQL，只会告诉你还没数据。

不过「少一条就当设计需求」有个坑：当前会话**有本体但一个个体都没灌**时，一句 `技术部有哪些员工？`
会被静默当成设计需求去生成草稿——用户以为在提问，实际产出的是新本体。所以自动档加了一道守卫：
只要句子形态像问句（`？` 结尾，或以 `哪些/谁/多少/怎么/是否/有没有/列出/统计/平均/排名…` 开头），
且当前会话个体数为 0，就先回一条解释气泡（「这句像在问数据，但当前会话还没有个体（7 类 / 0 个体）…」），
并把原句留在输入框里 —— 要么先去 `灌入数据…`，要么切「改本体」再发一次，`KGO.ai.turtle` 不动、不出草稿。
会话下拉的选项也改标 `N 类 · N 个体 / 无个体`（原来只标三元组数），选之前就能看出这份能不能问答。


生成（改本体）刻意是**两段式**，不让人被迫接受一份没看清的本体：

1. `draft`：只生成 + 出行摘要，**不落库**。模型被要求直接写 Turtle（不写 JSON 套 Turtle，
   免得转义坏掉），并在文件头用 `# name:` / `# summary:` / `# assume:` 三行注释交代本体名、
   设计思路、它替你做的假设；
2. 解析器三重门槛：能被 rdflib 解析 + 三元组 > 0 + 至少 1 个 `owl:Class`，三者缺一算失败；
3. 失败时把 rdflib 的报错原文回灌给模型重试**一次**；两次都失败就如实报错并提示改措辞，
   **不拿模板凑一份假本体**；
4. 摘要里的类清单、每类属性数、关系数都从**真实图**上算（`build_model`），不采信模型的自述；
5. `apply`：人确认后 `POST /kg/design/apply {turtle, name[, sid]}`，不带 `sid` = 新建会话，
   带 `sid` = 并入当前本体（`merged: true`，幂等，同一份并两次三元组数不变）。

**对话式怎么做的**：出过一版之后，每一轮都把上一版 Turtle（`base_turtle`，上限 16000 字符）
连同最近 8 轮「人话」历史回给模型，提示里明确「只动这次说到的地方，其余原样保留」。所以
「给合同加个金额字段」是在上一版上改，不是推翻重来；后端再用 `compare(old, new)` 从两张图上
算差集（`delta`），把「加了什么 / 改了什么」翻译成一句人话（`reply`）回显在气泡里 —— 差分是
从图算的，不是让模型自述。历史只回带人话，不回带整份文档正文，避免提示词爆掉。

**只有最新一版能应用**：每渲染一版新草稿，消息流里旧气泡上的动作按钮会被摘掉，免得对着上一版
点「应用」。动作包括：应用到（新）设计图 / 并入当前本体 / 下载 Turtle / 复制。

生成成功时返回的是 `graph.serialize("turtle")` 而不是模型原文本——顺带补上 `owl:Ontology`
声明和 `rdfs:label/comment`，否则页面顶部标题栏会是空的；前缀也由 rdflib 绑定后原样写出，
所以 CURIE 是 `ex:Sample` 而不是退化成整条 URI 或 `onto:Sample`。

文档链路为速度关掉了版式模型：`parse_bytes(..., fast=True)` 让 pdf/office 先走
pypdf / python-docx / openpyxl / python-pptx，读不出东西才退回 docling。实测一份 3 页 docx
从 60s+ 降到 3.2s（助手只要正文，不需要版式）。抽出来的正文长度会在气泡里告诉你（「从文档里
读到 N 字正文」），但**不再回填输入框**——正文太长，塞回文本框只会挡住对话。

**浮层的两个手势**：拖标题栏任意处移动（`pointer` 事件 + `setPointerCapture`，落在视口内，
松手写 `localStorage["pdt.kgo.ai.pos"]`，重进页面还在原地）；点「收起」变右上角小胶囊，点胶囊
复原，折叠态存 `localStorage["pdt.kgo.ai.fold"]`。头部按钮不会被拖拽吃掉（`e.target.closest("button")`）。
位置只是 `position: fixed`，但面板是 `#v-ontology` 的后代，切到别的视图时祖先 `display:none`，
整棵子树不渲染，不会串页。

---

## 二、接口清单

前缀 `/api/v1`。全部在 `backend/app/api/v1/kg.py`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/kg/status` | rdflib 版本、支持的格式、落盘目录是否可写、会话数、LLM 是否可用 |
| GET | `/kg/sessions` | 会话列表（含每个会话的统计与来源） |
| POST | `/kg/import` | 上传本体文件（multipart） |
| POST | `/kg/import/text` | 粘贴文本导入本体 |
| GET | `/kg/{sid}` | 会话详情 + 完整设计模型 |
| GET | `/kg/{sid}/export.ttl` | 导出整张图为 Turtle |
| POST | `/kg/{sid}/data` | 上传数据文件（multipart）灌入当前图 |
| POST | `/kg/{sid}/data/text` | 粘贴文本灌数据 |
| POST | `/kg/{sid}/sparql` | 执行只读查询（`{query, limit}`） |
| GET | `/kg/{sid}/examples` | 示例集（带 `runnable` 标记） |
| POST | `/kg/{sid}/examples/{eid}` | 跑指定编号的示例 |
| POST | `/kg/{sid}/ask` | 自然语言问答（`{question, use_llm}`） |
| POST | `/kg/design/draft` | 需求文本 → 本体草稿 + 设计摘要（`{requirements, sid?, name?}`，不落库） |
| POST | `/kg/design/doc` | 上传需求文档 → 抽正文 → 同上报文（multipart：`file`、`name?`、`sid?`） |
| POST | `/kg/design/apply` | 草稿落库（`{turtle, name?, sid?}`；带 `sid` 为并入现有会话） |
| DELETE | `/kg/{sid}` | 删除会话（内存 + 落盘） |

数据落盘在 `backend/.runtime/kg/<sid>/{graph.ttl, meta.json}`，服务重启后按 id 惰性载回，
内存里最多留 20 个会话（LRU 只清内存、不删盘）。

### 导入支持

按扩展名先猜，失败再依次重试：`turtle` / `xml`(RDF/XML) / `json-ld` / `nt` / `n3`。
解析成功但零三元组也算失败、继续试下一种。多个文件可一次拖入，逐个试，失败的在页面
列出文件名和错误原因（`failed[]`），成功的照常合并。

---

## 三、示例查询集（29 条，全部实跑通过）

19 条对齐 `docs/samples/employee-ontology.ttl`（员工本体），10 条只用 RDF/RDFS/OWL 标准词汇、
任何本体都能跑。页面左侧列表点一下即执行，`下载 .rq` 可整包导出；导入的不是员工本体时，
员工那 19 条会标成「需对应本体」并置灰，但仍然可以点开看查询长什么样。

### 员工本体套件（e01–e19）

| 编号 | 分类 | 主题 | 考的是什么 |
| --- | --- | --- | --- |
| e01 | 实体刻画 | 员工档案（单行宽表） | 一个实体的多个 `OPTIONAL` 字段拼宽表 |
| e02 | 归属定位 | 人 → 部门 → 职位 → 公司 | 多跳 `OPTIONAL` 链 |
| e03 | 多跳 | 员工 → 上级 → 职位 → 职级 | 三跳路径 |
| e04 | 反向查询 | 直接下属名单 | 反向使用对象属性 |
| e05 | 传递闭包 | 整条汇报链 | `reportsTo+` |
| e06 | 集合查询 | 部门在编名单 | 按标签过滤 + 成员列举 |
| e07 | 缺失检测 | 没有记录上级的人 | `FILTER NOT EXISTS` |
| e08 | 聚合 | 部门人数 / 平均司龄 / 最早入职 | `GROUP BY` + `AVG` + `MIN` + `YEAR` |
| e09 | 聚合 | 性别分布 | 分组计数 |
| e10 | 聚合 | 在职率 | 子查询算占比 |
| e11 | 一致性 | 跨部门汇报 | 两个 `belongsToDepartment` 比对 |
| e12 | 一致性 | 汇报链成环（含自环） | `reportsTo+` 自指检测 |
| e13 | 一致性 | 工号重复 | `GROUP BY … HAVING (COUNT > 1)` |
| e14 | 一致性 | 必填归属缺失 | 多个 `FILTER NOT EXISTS` |
| e15 | 约束校验 | 封闭词表越界 | 对着 `owl:oneOf` 列表校验取值 |
| e16 | 结构缺口 | 空缺职位 | 反连接 |
| e17 | 结构缺口 | 职位归属部门（只能靠员工反推） | 经实例反推模式层关系 |
| e18 | 类层级 | `foaf:Person` 继承链 | `rdfs:subClassOf+` 跨词表 |
| e19 | 传递闭包 | 部门层级树 | `parentDepartment+` |

### 通用套件（g01–g10，任何本体都能跑）

类清单（含中文名与实例数）、每类实例数、子类关系、对象属性 domain→range、数据属性清单、
空类体检、谓词使用频次 TOP30、无 `rdf:type` 的个体、跨两层继承、三元组总数。

### 为什么「可直接跑」

示例里的 `PREFIX` 写得全，但执行前服务端还会**补齐图里有、查询里没声明的 PREFIX**
（`query.with_prefixes`），所以同一份 `.rq` 粘到页面、Jena、GraphDB 都不用先改前缀。
反过来说：示例自身也带着完整前缀声明，脱离本平台单独执行同样成立。

---

## 四、语义问答怎么工作

> 入口在 AI 设计助手浮层里（模式切「问数据」，或在自动模式下直接问），页面上没有独立的问答卡片。

```
问题 ──► LLM（喂本体摘要 schema_brief）──► SPARQL ──► rdflib 执行 ──► 结果回译成人话
                    │ 失败/不可用
                    └──► 关键词模板兜底 ──► SPARQL ──► 执行 ──► 人话
```

- **喂给模型的不是整份本体**，是 `model.schema_brief()` 压出来的结构摘要（类、属性、
  取值范围、几条示例查询），避免长本体把上下文撑爆；
- **两轮重试**：第一轮报错后把错误信息塞回去再要一次，仍失败才降级模板；
- **兜底模板**不看模型，靠关键词匹配类标签 + 属性名拼查询，所以离线也能用；
- 气泡里的徽标区分引擎：`模型生成` / `模板兜底`，并附「看这条 SPARQL（耗时 N 秒）」和
  「实际执行的查询（已自动补齐 PREFIX）」折叠 —— 答案永远来自图数据，模型只负责写查询；
- 答案里的 URI 一律走**收敛后的前缀表**（见下）显示成 `employee:emp001` 这种 CURIE。

### 前缀收敛（这轮修掉的一个真问题）

rdflib 把图落盘成 Turtle 再读回来，会给原来的短前缀改名叫 `ns1`/`ns2`。之前查询结果
单元格直接读 `graph.namespaces()`，页面上就露出 `ns1:emp001`。现在统一由
`model._Names` 收敛出一份前缀表（只保留图里真正出现过的命名空间、一个命名空间只留一个
可读名、匿名前缀另起短名），再由 `kg.py::_pfx_of(sess)` 传给 `query.run()`，
`_curie` / `with_prefixes` 共用同一份映射。结果是查询结果、设计图、提示词三处前缀永远一致。

---

## 五、安全边界

- 只允许 `SELECT / ASK / CONSTRUCT / DESCRIBE`；
- `SERVICE / LOAD / INSERT / DELETE / DROP / CLEAR / CREATE / ADD / MOVE / COPY / WITH / USING`
  一律拒绝——图在内存里，一次 `LOAD` 就能把内网地址当数据源读出去；
- 拦截前先剥掉注释和字符串字面量，避免 `"DELETE"` 这种正常内容被误杀（也避免用它绕过）；
- 结果行数上限 500（接口可调到 2000），CONSTRUCT 预览 20000 字符，超出给截断标记。

---

## 六、本轮验证记录（实机）

后端 8101 + 浏览器实测，一次跑完：

```
导入 docs/samples/employee-ontology.ttl   → 230 三元组 / 6 类 / 7 对象属性 / 15 数据属性 / 14 个体
灌入 docs/samples/employee-demo-data.ttl  → +51 三元组 / +8 个体
示例集                                    → 29 条全部 runnable，实跑 29/29 通过，0 失败
查询结果 CURIE                            → employee:emp001 / employee:emp002 / employee:emp003
问答「技术部有哪些员工？」                → 模型生成，共 3 条：张三、赵六、王五
问答「谁的工号是 E003？」                 → 模型生成，共 2 条：周七、王五（演示数据里故意造的重复工号）
导出 export.ttl                           → 9023 字符，可再导入
浏览器自检                                → 节点 6、示例 29、e01 一行、e02 两行、详情 750 字符、控制台错误 0
```

### 画布拖拽 + 个体跳建模（会话 1779c6c0d4，6 类 / 14 个体，实机断言）

```
拖类节点        → 屏幕位移 60/40px ÷ scale 0.35 = 图坐标 172,114，落盘 pdt.kg.pos.<sid> 一致
拖个体药丸      → 位移 200,0（屏幕 70px ÷ 0.35），KGO.ipin 钉住，刷新后不再跟类走
展开/收起个体   → 14 → 0 → 14，按钮文案与计数同步
点类节点        → 拖动阈值 4px 未误触发，详情 739 字符正常渲染
缩放百分比      → 工具栏 35%（由 kgFit 按画布尺寸算出）
点个体 Company  → 命中已有对象：selKind=object、Company/公司、建模 tab=active、视图 shown、侧栏选中
点个体 Resigned → 新建对象：EmploymentStatus → label「雇佣状态」（该类无 data_props，实体属性 0 条）
点个体 emp002   → 新建 Employee → label「员工」、10 条属性、10 个属性节点
                  属性声明 address:string;birthDate:date;…（xsd: 前缀已归一，无一残留）
连点两次        → 对象数不变、重新选中（幂等）
```

前端页面在 `?view=ontology` 下加载无报错，`kgTab()` 的深链接落点（sparql/ask/graph/rules 等）
全部映射到新结构，旧链接不会 404 到空白页。

### AI 设计助手（三端点实跑，2026-11-19）

```
POST /kg/design/draft  「合同管理」需求      → ok=True engine=llm name=合同管理本体 8.6s
                                             4 类（合同 5 / 付款计划 3 / 项目 2 / 供应商 3 属性）
                                             3 关系 / 13 数据属性 / 76 三元组 / 3 条假设
POST /kg/design/draft  「实验室样品管理」    → ok=True 4 类（样品 4 / 检测项目 3 / 人员 2 / 检测任务 2）
                                             3 关系 / 11 数据属性 / 71 三元组 / 8.9s
                                             前缀 ex:、owl:Ontology label「实验室样品检测本体」齐全
POST /kg/design/doc    设备运维需求.docx     → ok=True engine=python-docx chars=284 端到端 25.7s
                                             7 类 / 6 关系 / 25 数据属性 / 148 三元组
POST /kg/design/apply  不带 sid              → merged=False，新建会话，71 三元组
POST /kg/design/apply  带 sid（同 turtle）   → merged=True，sid 不变，三元组仍 71（幂等）
```

修掉的两个真问题：`_merge()` 只 `add(tri)` 会丢源文件的 `@prefix`，导致 CURIE 退化成整条 URI
（现在把 `graph.bind()` 一起搬过来）；模型不写 `owl:Ontology` 时页面标题栏是空的
（`_stamp()` 补齐声明与 `rdfs:label/comment`）。

前端 `kgAi*` 函数族与 HTML 结构做完 `node --check`（rc=0）与标签平衡自检
（div 255/255、section 10/10、aside 5/5、textarea 5/5、button 116/116）。

### 对话式 + 浮层 + 问答并入助手（会话 1779c6c0d4，6 类 / 14 个体，实机断言）

浏览器这一侧没法截图（当前模型不支持读图），所以用一页一次性自检页把 `/console/?view=ontology`
装进 iframe，在里面按验收点逐条跑，结果累积进 `document.title` 再读回来。iframe 视口 1500×900：

```
结构        noAskCard=true（页面上已无独立问答卡片）；SPARQL 卡与画布都在
浮层        position=fixed；默认位 1075,78（= 右距 22 + 滚动条 15）；388×454
收起/展开   card.hidden=true / fab.hidden=false / display=none  →  false / true / flex
模式条      auto/已灌入 14 个体   qa/已灌入 14 个体   design（三键 on 态跟随）
路由        kgAiIsQuestion('给合同加个金额字段')=false，('技术部有哪些员工？')=true
            kgAiRoute → qa / design（各按预期）
类详情      kgDetail('employee:Company') → hidden=false，185 字符，开头「公司 employee:Company」
问答        kgAiAskData('张三的工号、邮箱、…是什么？') → 气泡内 .kgo-ans「模型生成查询 共 4 条结果」
            结果表 4 行；折叠「实际执行的查询（已自动补齐 PREFIX）」「看这条 SPARQL（耗时 5 秒）」
            err=false（答案确实来自图数据）
拖动        头部合成 pointerdown/move/up → 1075,78 → 875,218（位移 -200,+140 精确）
            localStorage pdt.kgo.ai.pos = {"left":875,"top":218}
刷新保持    location.reload() 后位置仍为 875,218
```

后端两轮对话（同一会话）也各跑了一次：
`第 1 轮 → 2 类 / 2 关系 / 6 数据属性`；`第 2 轮「给合同加负责人」→ reply「改好了：「合同」加了
负责人。现在共 2 个类 / 2 条关系 / 7 个数据属性」，delta.added_attrs=[{合同:[负责人]}]`。


---

## 七、自己跑一遍

```bash
# 1. 起服务（已有守护进程的话跳过）
cd personal-digital-twin
start-services.bat

# 2. 页面里导入
#    打开 http://127.0.0.1:8101/?view=ontology
#    把 docs/samples/employee-ontology.ttl 拖进去  → 自动出设计图
#    再把 docs/samples/employee-demo-data.ttl 拖进去 → 统计里个体数从 14 变 22

# 3. 命令行等价操作
SID=$(curl -s -X POST http://127.0.0.1:8101/api/v1/kg/import \
        -F "files=@docs/samples/employee-ontology.ttl" -F "name=employee" \
      | python -c "import sys,json;print(json.load(sys.stdin)['session']['id'])")

curl -X POST "http://127.0.0.1:8101/api/v1/kg/$SID/data" \
     -F "files=@docs/samples/employee-demo-data.ttl"

curl -X POST "http://127.0.0.1:8101/api/v1/kg/$SID/sparql" -H "Content-Type: application/json" \
     -d '{"query":"SELECT ?e ?n WHERE { ?e a <http://example.org/ontology/employee#Employee> ; <http://example.org/ontology/employee#fullName> ?n }"}'
```

示例数据里刻意埋了三处「问题数据」——重复工号（E003）、跨部门汇报、空缺职位——用来验证
e11 / e13 / e16 这三条一致性查询真的能查出来，不是摆设。

---

## 八、已知限制

- 设计图最多画 140 个类，超出的不画（数据仍在，查询不受影响）；
- 类之间没有 `subClassOf` 时布局退化成网格，拓扑感会弱一些；
- `owl:Restriction` 只展示六种常见约束，`owl:unionOf` / `owl:intersectionOf` 里的复杂表达式
  不展开；
- 问答的模板兜底只认「类 + 属性」层面的问法，问到具体数值比较（如「司龄大于 5 年的」）
  基本要靠模型生成；
- 会话落盘是整图重写，导入超大图（十万三元组以上）时首次导入会有明显等待；
- 个体药丸最多画 400 个（超过 60 个默认收起，得手动点「展开个体」，否则整屏都是虚线小胶囊）；
- 拖拽坐标存在浏览器 `localStorage` 里，按会话 id 分开存；换浏览器或清缓存会回到自动布局；
- 助手浮层的位置/折叠态同样存在 `localStorage`（`pdt.kgo.ai.pos` / `pdt.kgo.ai.fold`）；
  窄屏（≤1180px）是底部抽屉，拖动被禁用，之前拖出来的内联坐标会被清掉；
- 自动路由是**规则判断**不是分类模型：一句话里既有问句特征又是在下需求（「合同金额要不要加个
  币种？」）会被判成问数据，这时手动点「改本体」锁定即可；
- 对话上下文只回带最近 8 轮 × 700 字的人话，更早的修改靠上一版 Turtle 承载；一次改动想跨十几轮
  追溯，得靠「下载 Turtle」留档；
- 点个体跳建模只覆盖「类 → 业务对象」这一层，个体本身的属性值不会灌成对象实例数据 ——
  那里需要的是记录级同步，不是模型级同步。

---

## 附：本体建模左侧对象栏 —— 「对象分层」

本体建模页（`?view=modelling`）左侧对象栏有两个页签，第一个原来的「阶段分层」已改名为
**对象分层**：这一栏不再按建模阶段（数据源 / 实体 / 属性 / 函数）平铺所有节点，只回答
「我有哪些业务对象、每个对象身上挂了几个属性」。

| 页签 | 列什么 |
| --- | --- |
| 对象分层（默认） | 只列 `kind === "object"` 的节点：图标 + 对象名 + `N 属性`；按属性数从多到少排，同数按名称排 |
| 类型分组 | 原有行为不变：按 `MD_KINDS` 分组，带 `modNodeBadge` 徽标和「类型 · 副行」 |

口径与实现要点：

- 属性数取的是画布属性段的归组结果（`mdAttrBlocks()` 的 `kids.length`），和画布上看到的
  属性段完全一致；没挂属性的对象照样列出来，显示 `0 属性`，不会被藏掉；
- 孤儿属性块（`obj === null`）不计入任何对象，避免虚高；
- 侧栏搜索框仍生效，并且除了对象名/描述，还会匹配它名下属性的名字与描述——搜「工号」
  能定位到「员工」；
- 单击选中、双击定位到画布、选中态高亮都与「类型分组」共用同一套 `modSidePick` /
  `modSideFocus`；顶部统计条与标题右侧的节点数不受页签切换影响。

