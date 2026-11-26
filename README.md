# Personal Digital Twin

面向企业场景的私有化数字员工与本体平台。通过接入企业文档和业务数据，构建可追溯的知识与本体模型，为智能问答、业务分析和受控工作流提供统一语义基础。

## 核心能力
- 多源知识接入、知识蒸馏与治理
- 本体建模、数据映射、质量验证与版本管理
- 基于本体的可追溯智能问答
- 数字员工消息回复草稿和工作流决策辅助
- 权限策略、人工确认和操作审计

## 技术栈
- 后端：Python 3.11+、FastAPI、SQLAlchemy
- 智能编排：LangGraph、LLM API 适配
- 数据库：PostgreSQL 16（当前版本基础 DDL 和运行时 SQL 使用 PostgreSQL 方言）
- 可选服务：Redis（异步任务）、MinIO（对象存储）
- 前端：原生 Web 控制台
- 本体：RDF / Turtle、RDFLib、SPARQL

## 本地快速启动（无 Docker）

项目采用本机 Python 进程运行，不需要 Docker、Docker Compose 或 Nginx。当前版本仍要求本机安装并启动 PostgreSQL 16，并启用项目需要的 pgvector 扩展。由于初始化 DDL、schema、触发器和部分查询使用 PostgreSQL 专有能力，当前不能直接用 SQLite 替代。

### 1. 准备环境
- Python 3.11+
- PostgreSQL 16 + pgvector 扩展
- Node.js 20+（仅前端静态校验需要）

创建数据库和用户后，复制 .env.example 为 .env，并将 DATABASE_URL 配置为本机 PostgreSQL 地址。

### 2. 安装并启动
Linux / macOS：
```bash
bash scripts/start-local.sh
```

Windows PowerShell：
```powershell
.\scripts\start-local.ps1
```

脚本创建本地 Python 虚拟环境、安装后端依赖并启动 API。首次启动时应用会初始化基础数据库结构。

### 3. 访问
- Web 控制台：http://127.0.0.1:8100/
- API 文档：http://127.0.0.1:8100/docs
- 本体应用工作台：http://127.0.0.1:8100/ontology-app-studio/

按 Ctrl+C 停止服务。

## 可选组件
默认本地单机模式不启动 Celery Worker、Redis、MinIO 或独立模型推理服务。LLM 和 Embedding 通过 OpenAI 兼容 API 配置，可连接本机模型服务或企业内网模型网关。需要异步任务队列或独立对象存储时，再按需配置 Redis / MinIO。

## 项目文档
- 前端统一设计规范： [DESIGN.md](DESIGN.md)
- 企业数字员工平台 V1.2 蓝图：docs/blueprint-v1.2.txt
- V1.2 开发计划：docs/development-plan-v1.2.md
- 本体知识图谱使用指南：docs/ontology-kg-guide.md

## 安全原则
默认采用本机私有化部署思路。正式部署前请检查访问控制、密钥管理和数据库备份策略。
