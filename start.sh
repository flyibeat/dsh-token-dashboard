#!/bin/sh
# DeepSeek Harness 词元用量看板 —— 一键启动（macOS / Linux）
# 关闭终端即停止服务。
set -e
cd "$(dirname "$0")"

# 解释器优先级：DSH_PYTHON → python3 → python → DSH 托管的运行时。
# 不写死用户名/路径：DSH 托管的运行时由 DSH_HOME 推出来。
PY="${DSH_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3
  elif command -v python >/dev/null 2>&1; then PY=python
  fi
fi
if [ -z "$PY" ]; then
  D="${DSH_HOME:-$HOME/.dsh}"
  PY=$(ls -d "$D"/dsh-runtimes/*/dependencies/python/bin/python 2>/dev/null | head -n 1 || true)
fi
if [ -z "$PY" ]; then
  echo "找不到 Python。请安装 Python 3.10+，或设 DSH_PYTHON 指向解释器。" >&2
  exit 1
fi

echo "使用解释器：$PY"
exec "$PY" usage_server.py --open "$@"
