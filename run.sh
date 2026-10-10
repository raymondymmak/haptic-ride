#!/usr/bin/env bash
# One command for the demo laptop: fake bridge + game engine + overlay.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1

PY="${PYTHON:-python3}"
WS_PORT="${WS_PORT:-8765}"
HTTP_PORT="${HTTP_PORT:-8641}"

# Prefer a venv. Some machines (Debian/Ubuntu) ship Python without ensurepip,
# so fall back to a local folder on PYTHONPATH. Always invoke "$PY" — `python`
# is not on PATH everywhere.
if [[ ! -x .venv/bin/python ]]; then
  rm -rf .venv
  if ! "$PY" -m venv .venv >/dev/null 2>&1 || [[ ! -x .venv/bin/python ]]; then
    rm -rf .venv
    mkdir -p .vendor
    "$PY" -m pip install -q -r requirements.txt --target .vendor
    export PYTHONPATH="${PWD}/.vendor${PYTHONPATH:+:$PYTHONPATH}"
  fi
fi
if [[ -x .venv/bin/python ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PY=".venv/bin/python"
  "$PY" -m pip install -q -r requirements.txt
fi

BRIDGE_PID=""
GAME_PID=""
cleanup() {
  if [[ -n "$BRIDGE_PID" ]]; then kill "$BRIDGE_PID" 2>/dev/null || true; fi
  if [[ -n "$GAME_PID" ]]; then kill "$GAME_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

"$PY" bridge/fake_bridge.py --host 0.0.0.0 --port "$WS_PORT" --http-port "$HTTP_PORT" "$@" &
BRIDGE_PID=$!
sleep 0.5
if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
  echo "bridge failed to start" >&2
  exit 1
fi
"$PY" engine/game.py --url "ws://127.0.0.1:${WS_PORT}" &
GAME_PID=$!

echo ""
echo "  Haptic Ride   http://127.0.0.1:${HTTP_PORT}"
echo "  A/D shoulder check    ← / → turn    Ctrl-C stops"
echo ""

wait -n "$BRIDGE_PID" "$GAME_PID" || true
