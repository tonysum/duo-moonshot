#!/usr/bin/env bash
# PM2 启动脚本：从环境变量读端口与策略，restart 后生效
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${PAPER_PORT:-8100}"
STRAT="${PAPER_STRATEGY:-daily}"
exec .venv/bin/python -m moonshot.paper start --port "$PORT" --strategy "$STRAT"
