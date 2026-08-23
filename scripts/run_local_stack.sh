#!/usr/bin/env bash
# Native (no Docker) stand-in for the EC2 box:
#   - always-on poly-sharp-finder (monitor)
#   - sharp pipeline now, then every 30 minutes
#
# Usage (from trading-bot/):
#   ./scripts/run_local_stack.sh
#   POLYMAKER_SHARP_INTERVAL_SEC=120 ./scripts/run_local_stack.sh   # 2 min loop for testing
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Create trading-bot/.env first (copy .env.example)." >&2
  exit 1
fi

mkdir -p journal logs data-aggregation/output poly-sharp-finder/signals poly-sharp-finder/intents
[[ -f poly-sharp-finder/watch_list.json ]] || echo '[]' > poly-sharp-finder/watch_list.json
[[ -f state.db ]] || : > state.db

export POLYMAKER_LIVE="${POLYMAKER_LIVE:-0}"
export POLYMAKER_SHARP_INTERVAL_SEC="${POLYMAKER_SHARP_INTERVAL_SEC:-1800}"

echo "Local stack: monitor + sharp loop every ${POLYMAKER_SHARP_INTERVAL_SEC}s (POLYMAKER_LIVE=${POLYMAKER_LIVE})"
echo "Ctrl-C stops both."

uv run python scripts/run_monitor.py &
MONITOR_PID=$!
cleanup() {
  echo "stopping monitor ($MONITOR_PID)"
  kill "$MONITOR_PID" 2>/dev/null || true
  wait "$MONITOR_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uv run python scripts/run_sharp_loop.py
