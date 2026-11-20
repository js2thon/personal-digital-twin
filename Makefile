# Personal Digital Twin - 常用命令
.PHONY: up down seed test logs

up:            ## 启动基础底座（PostgreSQL/Redis/MinIO/API/Worker/Web）
	docker compose up -d --build

up-ai:         ## 启动含 vLLM 的完整 AI 栈（需要 GPU）
	docker compose --profile ai up -d --build

down:
	docker compose down

seed:          ## 写入示例数据（演示审批闭环）
	docker compose exec twin-api python scripts/seed_demo.py

test:          ## 后端冒烟测试
	cd backend && python -m pytest tests -q

logs:
	docker compose logs -f twin-api twin-worker
