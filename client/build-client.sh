#!/usr/bin/env bash
# Build the ForgeMesh Client Manager desktop app for Linux.
#
#   client/build-client.sh [--skip-typecheck]
#
# Stages:
#   1. install dashboard dependencies and build client/frontend/dist
#   2. typecheck the renderer (unless --skip-typecheck)
#   3. package AppImage + .deb with electron-builder
#   4. write SHA-256 checksums for the artifacts in client/desktop/release/
#
# The .deb post-install script creates /opt/forgemesh-client/venv and installs
# client/requirements-client.txt; the Electron shell prefers that interpreter,
# then /opt/forgemesh/venv (control-server package), then FMCM_PYTHON.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="$ROOT/client"
FRONTEND="$CLIENT/frontend"
DESKTOP="$CLIENT/desktop"
RELEASE="$DESKTOP/release"
TYPECHECK=1

for arg in "$@"; do
    case "$arg" in
        --skip-typecheck) TYPECHECK=0 ;;
        -h|--help)
            sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

log() { printf '\033[1;36m[client]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[client]\033[0m %s\n' "$*" >&2; exit 1; }

command -v npm >/dev/null 2>&1 || fail "npm is required (Node.js 18+)."
command -v node >/dev/null 2>&1 || fail "node is required (Node.js 18+)."

install_deps() {
    local dir="$1"
    if [[ -f "$dir/package-lock.json" ]]; then
        (cd "$dir" && npm ci --no-audit --no-fund)
    else
        (cd "$dir" && npm install --no-audit --no-fund)
    fi
}

log "Installing dashboard dependencies"
install_deps "$FRONTEND"

if [[ "$TYPECHECK" -eq 1 ]]; then
    log "Typechecking the renderer"
    (cd "$FRONTEND" && npm run typecheck)
fi

log "Building the dashboard"
(cd "$FRONTEND" && npm run build)
[[ -f "$FRONTEND/dist/index.html" ]] || fail "frontend build did not produce dist/index.html"

log "Installing desktop dependencies"
install_deps "$DESKTOP"

log "Packaging AppImage and .deb"
(cd "$DESKTOP" && npm run dist)

shopt -s nullglob
artifacts=("$RELEASE"/*.AppImage "$RELEASE"/*.deb)
[[ "${#artifacts[@]}" -gt 0 ]] || fail "electron-builder produced no Linux artifacts in $RELEASE"

log "Writing SHA-256 checksums"
(
    cd "$RELEASE"
    : > SHA256SUMS
    for file in *.AppImage *.deb; do
        [[ -e "$file" ]] || continue
        sha256sum "$file" >> SHA256SUMS
    done
    cat SHA256SUMS
)

log "Done. The .deb installs its Python runtime; the AppImage needs FMCM_PYTHON."
