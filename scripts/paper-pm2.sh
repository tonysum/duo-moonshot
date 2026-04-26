#!/usr/bin/env bash
# PM2 启动：raw-surge paper（与 moonshot.paper start 一致）
#   PAPER_PORT     — 监听端口，默认 8100
#   PAPER_CONFIG   — 可选，非空时传给 --config（R24 raw-surge 参数 JSON）
#   未设 PAPER_CONFIG 时，与直接 CLI 相同：走 MOONSHOT_R24_RAW_SURGE_PARAMS / 自动发现 config/r24_raw_surge_params.json
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${PAPER_PORT:-8100}"
CONFIG="${PAPER_CONFIG:-}"
if [ -n "$CONFIG" ]; then
  exec .venv/bin/python -m moonshot.paper start --port "$PORT" --config "$CONFIG"
else
  exec .venv/bin/python -m moonshot.paper start --port "$PORT"
fi
