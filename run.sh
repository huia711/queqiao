#!/usr/bin/env bash
# Start the self-hosted translator. Override settings with environment variables, e.g.
#   QUEQIAO_PORT=8686 MAGPIE_BASE_URL=http://127.0.0.1:3425/v1 QUEQIAO_DEFAULT_MODEL=workbuddy/glm-5.3-flashx ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境并安装依赖…"
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
fi

HOST="${QUEQIAO_HOST:-127.0.0.1}"
PORT="${QUEQIAO_PORT:-8686}"
echo "鹊桥 Queqiao: http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn app.main:asgi --host "$HOST" --port "$PORT" --log-level warning
