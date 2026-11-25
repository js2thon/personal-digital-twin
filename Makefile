# Personal Digital Twin - 本地原生开发命令
.PHONY: install start test lint

install:
	cd backend && python -m pip install -e ".[dev]"

start:
	cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8100

test:
	cd backend && python -m pytest tests -q

lint:
	cd backend && python -m compileall -q app
