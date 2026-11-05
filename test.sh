#!/usr/bin/env bash
# 跑测试（Linux / macOS）。Windows 用 test.ps1，功能一样。
#   ./test.sh                   全量
#   ./test.sh tests/test_policy.py -v
set -euo pipefail
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x "../.venv/bin/python" ]; then
  PY="../.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

echo "pytest <- $PY"
export PYTHONIOENCODING=utf-8
exec "$PY" -m pytest "$@"
