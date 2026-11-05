#!/usr/bin/env bash
# 启动 ACP Room（Linux / macOS）
#   ./start.sh            默认 8770
#   ./start.sh 9000       换端口
#   NO_BROWSER=1 ./start.sh
#
# Windows 用 start.ps1，功能一样。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8770}"

# 优先仓库内的 .venv；兼容「仓库被放在另一个项目里、venv 在上级目录」的旧布局；
# 都没有就退回系统 python。
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "../.venv/bin/python" ]; then
  PY="../.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

if [ -z "${PY:-}" ]; then
  echo "找不到 Python。先装 3.10+，再跑：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

export PYTHONIOENCODING=utf-8
export PORT

echo "ACP Room -> http://127.0.0.1:$PORT"

if [ "${NO_BROWSER:-0}" != "1" ]; then
  (
    for _ in $(seq 1 60); do
      if curl -sf "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
        command -v xdg-open >/dev/null 2>&1 && xdg-open "http://127.0.0.1:$PORT" && exit 0
        command -v open     >/dev/null 2>&1 && open     "http://127.0.0.1:$PORT" && exit 0
        exit 0
      fi
      sleep 0.5
    done
  ) &
fi

exec "$PY" server.py --port "$PORT"
