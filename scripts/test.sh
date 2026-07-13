#!/usr/bin/env bash
#
# One-command verification pipeline for Kibitz.
#
#   scripts/test.sh          # Python test suite only
#   scripts/test.sh --live   # also spins up a scratch live server and smoke-checks it
#
# Exits nonzero on any failure; prints a clear pass/fail summary at the end.
set -uo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
PY="$REPO_ROOT/.venv/bin/python"

LIVE=0
for arg in "$@"; do
  case "$arg" in
    --live) LIVE=1 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '\033[31m✗\033[0m %s\n' "$1"; }

PYTEST_STATUS=0
LIVE_STATUS=0

bold "Running Python test suite..."
if "$PY" -m pytest; then
  ok "pytest passed"
else
  PYTEST_STATUS=$?
  fail "pytest failed (exit $PYTEST_STATUS)"
fi

if [ "$LIVE" -eq 1 ]; then
  bold "Running live scratch-server smoke check..."

  # Production runs on 8765 — never touch it. Use a distinct scratch port.
  LIVE_PORT=8899
  SRC_DATA="$HOME/Library/Application Support/Tintin AI Chess Analysis/data"
  SCRATCH_DIR="$(mktemp -d)"
  SCRATCH_DATA="$SCRATCH_DIR/data"
  SERVER_LOG="$SCRATCH_DIR/server.log"
  SERVER_PID=""

  cleanup_live() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
      kill "$SERVER_PID" 2>/dev/null
      wait "$SERVER_PID" 2>/dev/null
    fi
    rm -rf "$SCRATCH_DIR"
  }
  trap cleanup_live EXIT

  if [ -d "$SRC_DATA" ]; then
    cp -R "$SRC_DATA" "$SCRATCH_DATA"
    ok "Copied scratch data dir from: $SRC_DATA"
  else
    mkdir -p "$SCRATCH_DATA"
    echo "  (no existing data dir found at $SRC_DATA; starting with an empty scratch dir)"
  fi

  CHESS_APP_MODE=1 CHESS_WEB_OPEN=0 CHESS_WEB_PORT="$LIVE_PORT" CHESS_DATA_DIR="$SCRATCH_DATA" \
    "$PY" "$REPO_ROOT/scripts/run_web.py" --serve >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!

  UP=0
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$LIVE_PORT/" >/dev/null 2>&1; then
      UP=1
      break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      break # process died early
    fi
    sleep 0.5
  done

  if [ "$UP" -eq 1 ] && curl -fsS "http://127.0.0.1:$LIVE_PORT/api/history" >/dev/null 2>&1; then
    ok "Live scratch server responded on / and /api/history (port $LIVE_PORT)"
  else
    LIVE_STATUS=1
    fail "Live scratch server smoke check failed — see log below"
    echo "----- server log ($SERVER_LOG) -----"
    cat "$SERVER_LOG" 2>/dev/null
    echo "-------------------------------------"
  fi

  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null
  fi
  SERVER_PID=""
  trap - EXIT
  cleanup_live
fi

bold "Summary"
STATUS=0
if [ "$PYTEST_STATUS" -eq 0 ]; then ok "pytest: pass"; else fail "pytest: FAIL"; STATUS=1; fi
if [ "$LIVE" -eq 1 ]; then
  if [ "$LIVE_STATUS" -eq 0 ]; then ok "live smoke check: pass"; else fail "live smoke check: FAIL"; STATUS=1; fi
fi

exit "$STATUS"
