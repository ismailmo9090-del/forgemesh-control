#!/usr/bin/env bash
# ForgeMesh Control Center: install, update or launch with one command.
#
# Installed as /usr/bin/forgemesh by the .deb. The same file is the bootstrap
# used on a fresh machine, so it must not depend on being inside a checkout.
set -euo pipefail

PROG="forgemesh"
REPO="ismailmo9090-del/forgemesh-control"
PORT="${FMC_PORT:-8787}"
BASE="http://127.0.0.1:${PORT}"
DATA="${FMC_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/forgemesh-control}"
BOOTSTRAP="curl -fsSL https://raw.githubusercontent.com/${REPO}/main/scripts/forgemesh.sh | bash"

ACTION="all"
REINSTALL=0
DRY_RUN=0

if [ -t 1 ]; then
    C_RED=$'\033[1;31m'; C_YEL=$'\033[1;33m'; C_CYA=$'\033[1;36m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_YEL=""; C_CYA=""; C_OFF=""
fi

log() { printf '%s[forgemesh]%s %s\n' "$C_CYA" "$C_OFF" "$*"; }
warn() { printf '%s[forgemesh]%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
fail() { printf '%s[forgemesh]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    cat <<EOF
ForgeMesh Control Center: one command to install, update and launch.

Usage:
  $PROG                 install or update to the latest release, then launch
  $PROG --run           launch the installed version (no network, no changes)
  $PROG --install       install or update only
  $PROG --worker        set up a worker laptop: install/update, print join steps
  $PROG --reinstall     reinstall the latest release even if it is current
  $PROG --dry-run       show what would happen, change nothing
  $PROG --help          show this help

Fresh machine:
  $BOOTSTRAP

Before the first install, that curl line is the command. Afterwards the package
provides '$PROG', so the same update-and-run flow needs no curl.

It uses sudo only for apt, keeps data in ~/.local/share/forgemesh-*, and never
touches credentials. The dashboard opens with a fresh session cookie.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --run) ACTION="run" ;;
        --install) ACTION="install" ;;
        --worker) ACTION="worker" ;;
        --reinstall) REINSTALL=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1 (try: $PROG --help)" ;;
    esac
    shift
done

need() { command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"; }

# --------------------------------------------------------------- launch only
if [ "$ACTION" = "run" ]; then
    command -v forgemesh-control >/dev/null 2>&1 || fail \
        "ForgeMesh is not installed yet. Run: $BOOTSTRAP"
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Would launch forgemesh-control"
        exit 0
    fi
    log "Starting ForgeMesh Control Center"
    forgemesh-control || fail "Could not start it. See ${DATA}/logs/launcher.log"
    printf '\nDashboard: %s\nControl token: %s\n' "$BASE" "${DATA}/runtime/control_token"
    exit 0
fi

need curl
need python3
need dpkg
need dpkg-deb

SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || fail "sudo is required to install the package."
    SUDO=(sudo)
fi

installed_version() {
    dpkg-query -W -f='${Status}\t${Version}' forgemesh-control 2>/dev/null \
        | awk -F'\t' '$1 == "install ok installed" {print $2; exit}'
}

health_version() {
    curl -fsS --max-time 2 "${BASE}/health" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null \
        || true
}

fetch_latest() {
    local json
    json="$(curl -fsSL --retry 2 --max-time 25 \
        "https://api.github.com/repos/${REPO}/releases/latest")" || return 1
    printf '%s' "$json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
version = str(data.get("tag_name") or "").lstrip("v")
url = ""
for asset in data.get("assets") or []:
    if str(asset.get("name") or "").endswith("_all.deb"):
        url = str(asset.get("browser_download_url") or "")
        break
if not version or not url:
    raise SystemExit(1)
print(version)
print(url)
'
}

restart_runtime() {
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    if systemctl --user is-active --quiet forgemesh-control.service 2>/dev/null; then
        log "Restarting forgemesh-control.service"
        systemctl --user restart forgemesh-control.service \
            || warn "Could not restart the systemd unit; the launcher will try again."
    else
        log "Stopping the running control server"
        pkill -f -- "-m forgemesh_control.main serve" 2>/dev/null || true
    fi
    if systemctl --user is-active --quiet forgemesh-browser.service 2>/dev/null; then
        log "Restarting forgemesh-browser.service"
        systemctl --user restart forgemesh-browser.service \
            || warn "Could not restart the browser unit."
    else
        if command -v fmb >/dev/null 2>&1; then
            fmb shutdown >/dev/null 2>&1 || true
        fi
        pkill -f -- "-m forgemesh_browser.daemon" 2>/dev/null || true
    fi
    i=0
    while [ "$i" -lt 30 ]; do
        if ! curl -fsS --max-time 1 "${BASE}/health" >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1))
        sleep 0.5
    done
    warn "The old server still answers on ${BASE}; stop it before launching."
}

INSTALLED="$(installed_version || true)"
LATEST=""
ASSET=""

if OUT="$(fetch_latest 2>/dev/null)"; then
    LATEST="$(printf '%s\n' "$OUT" | sed -n '1p')"
    ASSET="$(printf '%s\n' "$OUT" | sed -n '2p')"
else
    if [ -n "$INSTALLED" ]; then
        warn "Could not reach GitHub; using the installed version ${INSTALLED}."
        LATEST="$INSTALLED"
    else
        fail "Could not reach GitHub and ForgeMesh is not installed. Check the network and retry."
    fi
fi

NEED_INSTALL=0
if [ -z "$INSTALLED" ]; then
    NEED_INSTALL=1
elif [ "$REINSTALL" -eq 1 ]; then
    NEED_INSTALL=1
elif [ -n "$LATEST" ] && dpkg --compare-versions "$INSTALLED" lt "$LATEST"; then
    NEED_INSTALL=1
fi

RESTART=0
if [ "$NEED_INSTALL" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Would install ForgeMesh ${LATEST:-latest} (installed: ${INSTALLED:-none})"
        RESTART=1
    else
        [ -n "$ASSET" ] || fail "The latest release has no .deb asset."
        TMP_DEB="$(mktemp /tmp/forgemesh-control.XXXXXX.deb)"
        trap 'rm -f "$TMP_DEB"' EXIT
        log "Downloading ForgeMesh ${LATEST}"
        curl -fL --retry 3 --max-time 300 -o "$TMP_DEB" "$ASSET" \
            || fail "Download failed: $ASSET"
        dpkg-deb --info "$TMP_DEB" >/dev/null 2>&1 \
            || fail "The downloaded file is not a valid .deb."
        log "Installing ForgeMesh ${LATEST} (sudo may ask for your password)"
        APT_ARGS=(-y)
        [ "$REINSTALL" -eq 1 ] && APT_ARGS+=(--reinstall)
        "${SUDO[@]}" apt-get install "${APT_ARGS[@]}" "$TMP_DEB" \
            || fail "apt could not install the package. Check the output above."
        INSTALLED="$(installed_version || true)"
        [ -n "$INSTALLED" ] || fail "The package did not register with dpkg."
        RESTART=1
    fi
elif [ -n "$INSTALLED" ]; then
    log "ForgeMesh ${INSTALLED} is already the latest release."
fi

if [ "$ACTION" = "worker" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Would set up this laptop as a Mesh Client worker and print the join steps"
        exit 0
    fi
    command -v fmw >/dev/null 2>&1 \
        || fail "fmw is missing; the package may be incomplete."
    cat <<EOF

This laptop is set up as a Mesh Client worker (ForgeMesh ${INSTALLED:-unknown}).
Nothing runs and nothing is exposed here.

On the server laptop: Dashboard -> Worker Nodes
  1. Start the public ngrok tunnel and copy the https://... URL.
  2. Create a pairing code.

Then run here:
  fmw register --server https://<public-url> --code <PAIRING-CODE> --name "Worker 1"
  fmw run

Install and sign in to OpenCode/Qoder on this laptop yourself; ForgeMesh never
does that for you. Keep it running after logout:
  systemctl --user enable --now forgemesh-worker
EOF
    exit 0
fi

if [ "$RESTART" -eq 1 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        log "Would restart the control server and browser daemon"
    else
        restart_runtime
    fi
elif [ -n "$INSTALLED" ]; then
    RUNNING="$(health_version || true)"
    if [ -n "$RUNNING" ] && [ "$RUNNING" != "$INSTALLED" ]; then
        log "Running server is ${RUNNING}; restarting it for ${INSTALLED}"
        if [ "$DRY_RUN" -eq 1 ]; then
            log "Would restart the control server and browser daemon"
        else
            restart_runtime
        fi
    fi
fi

if [ "$ACTION" = "install" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        exit 0
    fi
    log "Done. Start it with: $PROG --run"
    exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
    log "Would launch forgemesh-control"
    exit 0
fi

command -v forgemesh-control >/dev/null 2>&1 \
    || fail "forgemesh-control is missing; the package may be incomplete."

log "Starting ForgeMesh Control Center"
forgemesh-control || fail "Could not start it. See ${DATA}/logs/launcher.log"
printf '\nDashboard: %s\nControl token: %s\n' "$BASE" "${DATA}/runtime/control_token"
