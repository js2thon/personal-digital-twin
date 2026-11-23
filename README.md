# Personal Digital Twin

面向企业场景的私有化数字员工与本体平台。通过接入企业文档和业务数据，构建可追溯的知识与本体模型，为智能问答、业务分析和受控工作流提供统一语义基础。

## 核心能力

- **多源知识接入**：支持文档及多类业务数据源接入、解析、去重与处理。
- **知识蒸馏与治理**：抽取事实和证据，支持人工审核、版本管理与来源追踪。
- **本体建模**：支持本体文件导入、结构解析、类与关系可视化，以及数据实例接入。
- **本体问答**：结合本体结构生成并执行只读 SPARQL，展示答案、查询过程与执行信息。
- **数字员工**：提供知识问答、消息回复草稿和工作流决策辅助。
- **安全与审计**：通过权限策略、人工确认和操作留痕约束高风险操作。

## 技术栈

- 后端：Python、FastAPI
- 智能编排：LangGraph、LLM API 适配
- 数据：PostgreSQL、Redis、MinIO（按部署配置启用）
- 前端：Web 控制台
- 本体与知识图谱：RDF / Turtle、RDFLib、SPARQL

## 快速启动

项目提供 Docker Compose 部署配置：

```bash
cp .env.example .env
# 按需配置数据库、模型服务及其他连接信息
make up
make seed
```

- Web 控制台：http://localhost:8200
- API 文档：http://localhost:8100/docs
- 本体应用工作台：http://localhost:8200/ontology-app-studio/

具体环境要求、配置项和本地运行方式请以项目文档及脚本为准。

## 项目文档

- [企业数字员工平台 V1.2 蓝图](docs/blueprint-v1.2.txt)
- [V1.2 开发计划](docs/development-plan-v1.2.md)
- [本体知识图谱使用指南](docs/ontology-kg-guide.md)
- [文档目录](docs/)

## 安全原则

默认采用私有化部署思路。知识审核、消息发送和工作流执行均设置相应的权限与确认约束；未经授权不自动执行外部操作。请在正式部署前检查环境配置、访问控制和密钥管理。
