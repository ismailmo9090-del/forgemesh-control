#!/usr/bin/env bash
# Remove the local installation. Browser profiles are preserved by default.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PURGE_DATA=0
ASSUME_YES=0

usage() {
    cat <<'EOF'
Usage: scripts/uninstall.sh [options]

  --purge-data   Also delete profiles, sessions, downloads and settings.
                 This is irreversible and requires confirmation.
  --yes          Skip the interactive confirmation prompt.
  -h, --help     Show this help.

By default this removes only the virtual environment and generated caches.
It never removes system packages, Chrome, or unrelated files.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge-data) PURGE_DATA=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

DATA_DIR="${FMB_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/forgemesh-browser}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/forgemesh-browser"

echo "ForgeMesh Browser uninstall"
echo "  project: $ROOT"
echo "  data:    $DATA_DIR"
if [[ $PURGE_DATA -eq 1 ]]; then
    echo "  profiles and downloads will be DELETED (--purge-data)."
else
    echo "  profiles and downloads will be kept."
fi

if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "Continue? [y/N] " answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) echo "Cancelled."; exit 1 ;;
    esac
fi

if [[ -x "$ROOT/.venv/bin/fmb" ]] && [[ -f "$DATA_DIR/runtime/daemon.json" ]]; then
    echo "Stopping the local daemon"
    "$ROOT/.venv/bin/fmb" shutdown >/dev/null 2>&1 || true
fi

echo "Removing the virtual environment and build caches"
rm -rf "$ROOT/.venv" "$ROOT/.pytest_cache" "$ROOT/.ruff_cache" \
    "$ROOT/build" "$ROOT/dist" "$ROOT/src/forgemesh_browser.egg-info"
find "$ROOT/src" -maxdepth 3 -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

if [[ $PURGE_DATA -eq 1 ]]; then
    if [[ $ASSUME_YES -eq 0 ]]; then
        read -r -p "Type DELETE to erase all profiles and downloads: " confirm
        [[ "$confirm" == "DELETE" ]] || { echo "Data preserved."; exit 0; }
    fi
    echo "Deleting data and configuration directories"
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
fi

echo "Uninstall complete. The system Chrome/Chromium installation was not modified."
