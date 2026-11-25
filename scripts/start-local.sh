#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$ROOT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install -e "$ROOT_DIR/backend"

if [ ! -f "$ROOT_DIR/.env" ]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  echo "已生成 .env，请确认 DATABASE_URL 和模型服务配置后重新运行。"
  exit 1
fi

echo "启动 Personal Digital Twin：http://127.0.0.1:${API_PORT:-8100}"
cd "$ROOT_DIR/backend"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT:-8100}"
