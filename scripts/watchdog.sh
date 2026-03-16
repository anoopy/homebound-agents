#!/bin/bash
# Homebound watchdog — checks if orchestrator is running and restarts if not.
#
# Designed to be called by launchd/cron every 60s.
# Uses homeboundctl.sh start, which is idempotent (exits if already running).
#
# Usage:
#   scripts/watchdog.sh --config /path/to/homebound.yaml

set -euo pipefail

export HOME="${HOME:-$(dscl . -read /Users/"$(id -un)" NFSHomeDirectory | awk '{print $2}')}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Parse --config flag
CONFIG_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)
      CONFIG_FLAG="--config $2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

# Derive session name
if [ -n "$CONFIG_FLAG" ]; then
  CONFIG_FILE="${CONFIG_FLAG#--config }"
  SESSION=$(python3 - "$CONFIG_FILE" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('orchestrator', {}).get('name', 'homebound'))
PYEOF
  ) 2>/dev/null || SESSION="homebound"
  PROJECT_DIR=$(python3 - "$CONFIG_FILE" <<'PYEOF'
import sys, yaml
with open(sys.argv[1]) as f:
    cfg = yaml.safe_load(f) or {}
print(cfg.get('tracker', {}).get('project_dir', '.'))
PYEOF
  ) 2>/dev/null || PROJECT_DIR="."
else
  SESSION="homebound"
  PROJECT_DIR="$(pwd)"
fi

LOG_DIR="${PROJECT_DIR}/tmp/${SESSION}"
mkdir -p "$LOG_DIR"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*" >> "${LOG_DIR}/watchdog.log"
}

# Check if tmux session exists with an orchestrator window
if tmux has-session -t "$SESSION" 2>/dev/null; then
    if tmux list-windows -t "$SESSION" -F "#{window_name}" 2>/dev/null | grep -q "^orchestrator$"; then
        exit 0
    fi
fi

# Orchestrator not running — restart
log "Orchestrator not found in session '${SESSION}' — restarting"
set +e
# shellcheck disable=SC2086
"${SCRIPT_DIR}/homeboundctl.sh" ${CONFIG_FLAG} start >> "${LOG_DIR}/watchdog.log" 2>&1
rc=$?
set -e

if [ $rc -eq 0 ]; then
    log "Restart completed successfully"
else
    log "ERROR: restart failed (exit code: ${rc})"
fi
