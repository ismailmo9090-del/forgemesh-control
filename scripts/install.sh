#!/usr/bin/env bash
# Install ForgeMesh Browser into ./.venv (user-space, no sudo required).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
WITH_DEV=1
WITH_SMOKE=1
WITH_PLAYWRIGHT=0

usage() {
    cat <<'EOF'
Usage: scripts/install.sh [options]

  --no-dev          Install runtime dependencies only (skip pytest/ruff).
  --no-smoke        Skip the browser launch smoke test.
  --playwright      Download Playwright Chromium even if system Chrome exists.
  --python PATH     Python interpreter to use (default: python3).
  -h, --help        Show this help.

The script never uses sudo. System packages required for Chromium are reported
with the exact command to run yourself when they are missing.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-dev) WITH_DEV=0 ;;
        --no-smoke) WITH_SMOKE=0 ;;
        --playwright) WITH_PLAYWRIGHT=1 ;;
        --python) PYTHON="${2:?--python requires a path}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '\033[1;36m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# --- platform checks ---------------------------------------------------------
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian|linuxmint|pop) ;;
        *) warn "Tested on Ubuntu; detected ${PRETTY_NAME:-unknown}. Continuing." ;;
    esac
    if [[ -n "${VERSION_ID:-}" ]] && [[ "${ID:-}" == "ubuntu" ]]; then
        if [[ "$(printf '%s\n' "22.04" "$VERSION_ID" | sort -V | head -n1)" != "22.04" ]]; then
            warn "Ubuntu ${VERSION_ID} is older than the tested 22.04 baseline."
        fi
    fi
else
    warn "Cannot read /etc/os-release; continuing on an unknown distribution."
fi

command -v "$PYTHON" >/dev/null 2>&1 || fail "Python interpreter not found: $PYTHON"
"$PYTHON" - <<'PY' || fail "ForgeMesh Browser requires Python 3.11 or newer."
import sys
if sys.version_info < (3, 11):
    print(f"Python {sys.version.split()[0]} is too old", file=sys.stderr)
    raise SystemExit(1)
print(f"Python {sys.version.split()[0]} detected")
PY

# --- virtual environment -----------------------------------------------------
# uv is usually in ~/.local/bin or ~/.cargo/bin, which a non-login shell (cron,
# CI, `bash scripts/install.sh` from a stripped PATH) may not export.
find_uv() {
    if command -v uv >/dev/null 2>&1; then
        echo uv
    elif [[ -x "$HOME/.local/bin/uv" ]]; then
        echo "$HOME/.local/bin/uv"
    elif [[ -x "$HOME/.cargo/bin/uv" ]]; then
        echo "$HOME/.cargo/bin/uv"
    fi
}
UV="$(find_uv)"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    log "Creating virtual environment at .venv"
    if [[ -n "$UV" ]]; then
        "$UV" venv "$ROOT/.venv" --python "$PYTHON"
    else
        "$PYTHON" -m venv "$ROOT/.venv"
    fi
else
    log "Reusing existing virtual environment"
fi
VENV_PY="$ROOT/.venv/bin/python"

EXTRAS=""
[[ $WITH_DEV -eq 1 ]] && EXTRAS="[dev]"

log "Installing project dependencies${EXTRAS:+ and dev tools}"
if [[ -n "$UV" ]]; then
    "$UV" pip install --python "$VENV_PY" -e "$ROOT$EXTRAS"
elif "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    "$VENV_PY" -m pip install --upgrade pip
    "$VENV_PY" -m pip install -e "$ROOT$EXTRAS"
elif "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    log "Bootstrapping pip into .venv"
    "$VENV_PY" -m ensurepip --upgrade
    "$VENV_PY" -m pip install -e "$ROOT$EXTRAS"
else
    fail "No installer available: uv was not found and $VENV_PY has neither pip nor ensurepip.
Install uv (https://docs.astral.sh/uv/) or recreate .venv with: rm -rf .venv && python3 -m venv .venv"
fi

# --- data and config directories --------------------------------------------
DATA_DIR="${FMB_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/forgemesh-browser}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/forgemesh-browser"
log "Preparing private data directories under $DATA_DIR"
for sub in "" profiles downloads screenshots assets results logs runtime; do
    mkdir -p "$DATA_DIR/${sub}"
    chmod 700 "$DATA_DIR/${sub}"
done
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

# --- browser ----------------------------------------------------------------
log "Checking browser availability"
"$ROOT/scripts/setup_browser.sh" $([[ $WITH_PLAYWRIGHT -eq 1 ]] && echo --playwright)

# --- dashboard --------------------------------------------------------------
# frontend/dist is a build artifact and is not committed, so a fresh clone has
# no dashboard until this runs. The control server serves it from that path.
if [[ -f "$ROOT/frontend/dist/index.html" ]]; then
    log "Using the existing dashboard build in frontend/dist"
elif command -v npm >/dev/null 2>&1; then
    log "Building the control dashboard (npm)"
    if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
        (cd "$ROOT/frontend" && npm ci)
    fi
    (cd "$ROOT/frontend" && npm run build)
else
    warn "Node.js/npm not found; the dashboard will be unavailable (fmc still serves its API)."
    warn "Install Node.js 18+ and run: (cd frontend && npm install && npm run build)"
    warn "Or install the packaged build: sudo apt install ./forgemesh-control_<version>_all.deb"
fi

# --- validation -------------------------------------------------------------
log "Validating the installation"
"$VENV_PY" -c "import forgemesh_browser; print('forgemesh_browser', forgemesh_browser.__version__)"
"$ROOT/.venv/bin/fmb" --version >/dev/null
log "CLI entry point: $ROOT/.venv/bin/fmb"

if [[ $WITH_SMOKE -eq 1 ]]; then
    log "Running browser smoke test (headless)"
    SMOKE_DIR="$(mktemp -d -t fmb-smoke-XXXXXX)"
    SMOKE_PORT="${FMB_SMOKE_PORT:-8799}"
    smoke_cleanup() {
        FMB_DATA_DIR="$SMOKE_DIR" FMB_API_PORT="$SMOKE_PORT" \
            "$ROOT/.venv/bin/fmb" shutdown >/dev/null 2>&1 || true
        rm -rf "$SMOKE_DIR"
    }
    trap smoke_cleanup EXIT
    export FMB_DATA_DIR="$SMOKE_DIR"
    export FMB_API_PORT="$SMOKE_PORT"
    export FMB_HEADLESS=true
    if ! "$ROOT/.venv/bin/fmb" start >/dev/null; then
        fail "Browser smoke test failed to start. Check Chrome/Chromium installation."
    fi
    status_json="$("$ROOT/.venv/bin/fmb" --json status)"
    "$VENV_PY" - "$status_json" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
assert data["running"] is True, "no browser session reported"
assert data["daemon"]["running"] is True, "daemon not running"
print("browser smoke test passed")
PY
    "$ROOT/.venv/bin/fmb" stop --all >/dev/null
    "$ROOT/.venv/bin/fmb" shutdown >/dev/null || true
    trap - EXIT
    smoke_cleanup
    unset FMB_DATA_DIR FMB_API_PORT FMB_HEADLESS
fi

cat <<EOF

ForgeMesh Browser is installed.

  Activate the CLI:   source .venv/bin/activate
  Or call it directly: $ROOT/.venv/bin/fmb --help
  Start the daemon:   scripts/start.sh prod
  Development mode:   scripts/start.sh dev

Profiles and data live in: $DATA_DIR
EOF
