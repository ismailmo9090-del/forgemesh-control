#!/usr/bin/env bash
# Start the ForgeMesh Browser daemon in development or production mode.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="prod"
BACKGROUND=0

usage() {
    cat <<'EOF'
Usage: scripts/start.sh [dev|prod] [options]

  dev         Headed-capable, human-friendly logs (FMB_HEADLESS=false).
  prod        Headless, JSON logs (default). Suitable for systemd.
  --background  Detach and write output to data/logs/start.log.
  -h, --help    Show this help.

Environment variables already set (FMB_HEADLESS, FMB_LOG_JSON, FMB_API_PORT,
FMB_DATA_DIR, ...) always win over the mode defaults.
EOF
}

for arg in "$@"; do
    case "$arg" in
        dev|development) MODE="dev" ;;
        prod|production) MODE="prod" ;;
        --background) BACKGROUND=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

VENV_FMB="$ROOT/.venv/bin/fmb"
[[ -x "$VENV_FMB" ]] || { echo "Run scripts/install.sh first." >&2; exit 1; }

if [[ "$MODE" == "dev" ]]; then
    export FMB_HEADLESS="${FMB_HEADLESS:-false}"
    export FMB_LOG_JSON="${FMB_LOG_JSON:-false}"
else
    export FMB_HEADLESS="${FMB_HEADLESS:-true}"
    export FMB_LOG_JSON="${FMB_LOG_JSON:-true}"
fi

if [[ $BACKGROUND -eq 1 ]]; then
    DATA_DIR="${FMB_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/forgemesh-browser}"
    mkdir -p "$DATA_DIR/logs" "$DATA_DIR/runtime"
    chmod 700 "$DATA_DIR/logs" "$DATA_DIR/runtime"
    LOG="$DATA_DIR/logs/start.log"
    echo "Starting ForgeMesh Browser daemon in the background ($MODE mode)"
    setsid nohup "$VENV_FMB" serve >>"$LOG" 2>&1 &
    PID=$!
    echo "$PID" >"$DATA_DIR/runtime/start.pid"
    chmod 600 "$DATA_DIR/runtime/start.pid"
    echo "  pid: $PID"
    echo "  log: $LOG"
    echo "Stop it with: $VENV_FMB shutdown"
    exit 0
fi

echo "Starting ForgeMesh Browser daemon ($MODE mode, foreground)"
exec "$VENV_FMB" serve
