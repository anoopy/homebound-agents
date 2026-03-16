#!/bin/bash
# Homebound tmux session management.
#
# Usage:
#   homeboundctl.sh start [--config PATH]      # Start orchestrator
#   homeboundctl.sh start-dry [--config PATH]  # Start in dry-run mode
#   homeboundctl.sh stop [--config PATH]       # Stop orchestrator only
#   homeboundctl.sh stop-all [--config PATH]   # Stop everything
#   homeboundctl.sh status [--config PATH]     # Show tmux windows
#   homeboundctl.sh attach [--config PATH]     # Attach to session
#   homeboundctl.sh logs [--config PATH]       # Tail log file
#   homeboundctl.sh health [--config PATH]     # Full health report

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Prefer venv Python (has PyYAML) over system Python
if [ -x "$REPO_DIR/venv/bin/python3" ]; then
  PY="$REPO_DIR/venv/bin/python3"
else
  PY="python3"
fi

# Inherit user's full PATH from their login shell (non-interactive shells lack it).
# Use tail -1 to discard any noisy init-script output before our echo.
_login_path="$("${SHELL:-/bin/bash}" -lc 'echo "$PATH"' 2>/dev/null | tail -1)" || true
[ -n "$_login_path" ] && export PATH="$_login_path"

# Source .env for tokens (SLACK_BOT_TOKEN, ANTHROPIC_API_KEY) and any PATH overrides.
if [ -f "$REPO_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$REPO_DIR/.env"
    set +a
fi

# Resolve homebound CLI to an absolute path (may be in venv or on PATH)
if [ -x "$REPO_DIR/venv/bin/homebound" ]; then
  HOMEBOUND_BIN="$REPO_DIR/venv/bin/homebound"
elif command -v homebound &>/dev/null; then
  HOMEBOUND_BIN="$(command -v homebound)"
else
  echo "Error: 'homebound' not found. Install with: pip install -e ." >&2
  exit 1
fi

# Build tmux -e flags to pass tokens and PATH into new sessions/windows.
# PATH must be passed explicitly because tmux's default shell may not inherit
# the login shell PATH (e.g., /opt/homebrew/bin for Homebrew on Apple Silicon).
TMUX_ENV_FLAGS=(-e "PATH=$PATH")
[ -n "${SLACK_BOT_TOKEN:-}" ] && TMUX_ENV_FLAGS+=(-e "SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN")
[ -n "${SLACK_CHANNEL_ID:-}" ] && TMUX_ENV_FLAGS+=(-e "SLACK_CHANNEL_ID=$SLACK_CHANNEL_ID")
[ -n "${ANTHROPIC_API_KEY:-}" ] && TMUX_ENV_FLAGS+=(-e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY")

# ---------------------------------------------------------------------------
# Parse --config flag
# ---------------------------------------------------------------------------
CONFIG_FILE=""
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_FILE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
      shift 2
      ;;
    *)
      REMAINING_ARGS+=("$1")
      shift
      ;;
  esac
done
CMD="${REMAINING_ARGS[0]:-help}"

# ---------------------------------------------------------------------------
# Validate --config file exists
# ---------------------------------------------------------------------------
if [ -n "$CONFIG_FILE" ] && [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: config file not found: $CONFIG_FILE" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Derive session name from config (or use defaults)
# ---------------------------------------------------------------------------
if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ]; then
  SESSION=$($PY - "$CONFIG_FILE" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('orchestrator', {}).get('name', 'homebound'))
PYEOF
  ) 2>/dev/null || SESSION="homebound"
  PROJECT_DIR=$($PY - "$CONFIG_FILE" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('tracker', {}).get('project_dir', '.'))
PYEOF
  ) 2>/dev/null || PROJECT_DIR="."
  PROJECT_DIR="$(cd "$PROJECT_DIR" 2>/dev/null && pwd || echo "$PROJECT_DIR")"
  CONFIG_FLAG="--config $CONFIG_FILE"
else
  SESSION="homebound"
  PROJECT_DIR="$(pwd)"
  CONFIG_FLAG=""
fi

LOG_DIR="${PROJECT_DIR}/tmp/${SESSION}"
mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
case "$CMD" in
  start)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
        echo "Orchestrator already running in session '$SESSION'. Use 'attach' to connect or 'stop' to restart."
        exit 1
      fi
      echo "Re-attaching orchestrator to existing session '$SESSION' (orphaned children will be re-adopted)..."
      tmux new-window "${TMUX_ENV_FLAGS[@]}" -t "$SESSION" -n orchestrator -c "$PROJECT_DIR" \
        "$HOMEBOUND_BIN start ${CONFIG_FLAG}; echo '[Homebound exited. Press Enter to close.]'; read"
    else
      echo "Starting Homebound in tmux session '$SESSION'..."
      tmux new-session "${TMUX_ENV_FLAGS[@]}" -d -s "$SESSION" -n orchestrator -c "$PROJECT_DIR" \
        "$HOMEBOUND_BIN start ${CONFIG_FLAG}; echo '[Homebound exited. Press Enter to close.]'; read"
    fi
    echo "Homebound started. Use 'homeboundctl.sh attach${CONFIG_FILE:+ --config $CONFIG_FILE}' to connect."
    echo "Log: ${LOG_DIR}/homebound.log"
    ;;

  start-dry)
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
        echo "Homebound already running in session '$SESSION'."
        exit 1
      fi
      tmux new-window "${TMUX_ENV_FLAGS[@]}" -t "$SESSION" -n orchestrator -c "$PROJECT_DIR" \
        "$HOMEBOUND_BIN start --dry-run ${CONFIG_FLAG}; echo '[Homebound exited. Press Enter to close.]'; read"
    else
      tmux new-session "${TMUX_ENV_FLAGS[@]}" -d -s "$SESSION" -n orchestrator -c "$PROJECT_DIR" \
        "$HOMEBOUND_BIN start --dry-run ${CONFIG_FLAG}; echo '[Homebound exited. Press Enter to close.]'; read"
    fi
    echo "Homebound started (dry run)."
    ;;

  stop)
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Homebound not running (session '$SESSION')."
      exit 0
    fi
    if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
      echo "Stopping orchestrator in session '$SESSION' (child sessions will continue running)..."
      tmux send-keys -t "${SESSION}:orchestrator" C-c
      sleep 5
      if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
        tmux kill-window -t "${SESSION}:orchestrator" 2>/dev/null || true
      fi
    else
      echo "Orchestrator window not found in session '$SESSION'."
    fi
    remaining=$(tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep "^CLAUDE-" || true)
    if [ -n "$remaining" ]; then
      echo "Orchestrator stopped. Child sessions still running:"
      echo "$remaining" | sed 's/^/  /'
      echo "Use 'stop-all' to tear down everything."
    else
      tmux kill-session -t "$SESSION" 2>/dev/null || true
      echo "Homebound stopped (no child sessions remaining)."
    fi
    ;;

  stop-all)
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Homebound not running (session '$SESSION')."
      exit 0
    fi
    echo "Stopping everything in session '$SESSION'..."
    children=$(tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep "^CLAUDE-" || true)
    if [ -n "$children" ]; then
      for child in $children; do
        echo "  Sending /exit to ${child}..."
        tmux send-keys -t "${SESSION}:${child}" -l "/exit" 2>/dev/null || true
        tmux send-keys -t "${SESSION}:${child}" Enter 2>/dev/null || true
      done
      echo "  Waiting for child sessions to exit..."
      sleep 5
    fi
    if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
      tmux send-keys -t "${SESSION}:orchestrator" C-c 2>/dev/null || true
      sleep 3
    fi
    tmux kill-session -t "$SESSION" 2>/dev/null || true
    echo "Homebound fully stopped."
    ;;

  status)
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Homebound not running (session '$SESSION')."
      exit 0
    fi
    echo "Homebound session '$SESSION' windows:"
    tmux list-windows -t "$SESSION" -F "  #{window_index}: #{window_name} (#{window_activity_string})"
    ;;

  attach)
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "Homebound not running (session '$SESSION')."
      exit 1
    fi
    tmux attach -t "$SESSION"
    ;;

  logs)
    if [ -f "${LOG_DIR}/homebound.log" ]; then
      tail -f "${LOG_DIR}/homebound.log"
    else
      echo "No log file found at ${LOG_DIR}/homebound.log"
      exit 1
    fi
    ;;

  health)
    echo "=== Homebound Health Report ==="
    echo "Session: ${SESSION}"
    echo ""
    if tmux has-session -t "$SESSION" 2>/dev/null; then
      echo "tmux session:   RUNNING"
    else
      echo "tmux session:   NOT RUNNING"
      echo ""
      echo "Start with: homeboundctl.sh start${CONFIG_FILE:+ --config $CONFIG_FILE}"
      exit 0
    fi
    if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
      echo "Orchestrator:   RUNNING"
    else
      echo "Orchestrator:   NOT RUNNING (children may be orphaned)"
    fi
    children=$(tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep "^CLAUDE-" || true)
    if [ -n "$children" ]; then
      child_count=$(echo "$children" | wc -l | tr -d ' ')
      echo "Children:       ${child_count} active"
      echo "$children" | sed 's/^/                  /'
    else
      echo "Children:       none"
    fi
    LOG_FILE="${LOG_DIR}/homebound.log"
    if [ -f "$LOG_FILE" ]; then
      log_size=$(du -h "$LOG_FILE" | cut -f1)
      last_log=$(tail -1 "$LOG_FILE" 2>/dev/null | cut -d' ' -f1-2)
      echo "Log size:       ${log_size}"
      echo "Last log entry: ${last_log:-unknown}"
    else
      echo "Log:            no log file"
    fi
    echo ""
    ;;

  *)
    echo "Usage: homeboundctl.sh [--config <path>] <command>"
    echo ""
    echo "Commands:"
    echo "  start        Start orchestrator in tmux"
    echo "  start-dry    Start in dry-run mode"
    echo "  stop         Stop orchestrator only (children survive)"
    echo "  stop-all     Stop everything (orchestrator + all children)"
    echo "  status       Show tmux windows"
    echo "  health       Full system health report"
    echo "  attach       Attach to the tmux session"
    echo "  logs         Tail the log file"
    ;;
esac
