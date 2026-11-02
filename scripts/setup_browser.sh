#!/usr/bin/env bash
# Ensure a usable Chromium engine exists for ForgeMesh Browser.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FORCE_PLAYWRIGHT=0
CHECK_ONLY=0

usage() {
    cat <<'EOF'
Usage: scripts/setup_browser.sh [options]

  --check         Report the detected browser and exit.
  --playwright    Download and install Playwright's Chromium build even when a
                  system Chrome/Chromium is already available.
  -h, --help      Show this help.

The default configuration uses the system Google Chrome when present. System
libraries for Chromium are installed only when this script runs as root; when
not, the exact `playwright install-deps` command is printed for you to run with
sudo. The script never invokes sudo itself.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --playwright) FORCE_PLAYWRIGHT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '\033[1;36m[browser]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[browser]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[browser]\033[0m %s\n' "$*" >&2; exit 1; }

find_system_browser() {
    for candidate in google-chrome google-chrome-stable chromium chromium-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then
            echo "$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

SYSTEM_BROWSER="$(find_system_browser || true)"
if [[ -n "$SYSTEM_BROWSER" ]]; then
    log "System browser: $SYSTEM_BROWSER"
    "$SYSTEM_BROWSER" --version 2>/dev/null || true
    if [[ $FORCE_PLAYWRIGHT -eq 0 ]]; then
        log "Using the system browser (set browser_channel: chrome or auto)."
        exit 0
    fi
else
    warn "No system Chrome/Chromium found."
fi

[[ $CHECK_ONLY -eq 1 ]] && exit 0

VENV_PY="$ROOT/.venv/bin/python"
PLAYWRIGHT="$ROOT/.venv/bin/playwright"
[[ -x "$VENV_PY" ]] || fail "Virtual environment missing. Run scripts/install.sh first."
[[ -x "$PLAYWRIGHT" ]] || fail "Playwright CLI missing. Run scripts/install.sh first."

log "Installing Playwright Chromium (this downloads the browser build)"
"$PLAYWRIGHT" install chromium

if [[ "$(id -u)" -eq 0 ]]; then
    log "Installing Chromium system libraries"
    "$PLAYWRIGHT" install-deps chromium
else
    warn "If Chromium fails to launch for missing libraries, run:"
    warn "  sudo $PLAYWRIGHT install-deps chromium"
fi

log "Verifying the Playwright installation"
"$VENV_PY" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<title>fmb</title><h1>ok</h1>")
    assert page.title() == "fmb"
    browser.close()
print("playwright chromium launch test passed")
PY
