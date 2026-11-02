#!/bin/sh
# dpkg postinst for the ForgeMesh Client Manager .deb.
#
# The Electron shell ships the Python source, but Python packages are not
# vendored into the AppImage/deb (they are wheels tied to the interpreter and
# architecture). This creates a private virtual environment once, owned by
# root, and installs the runtime subset the local bridge imports.
#
# The app prefers /opt/forgemesh-client/venv/bin/python and falls back to
# /opt/forgemesh/venv (the control-server package) or an explicit FMCM_PYTHON.
set -e

VENV=/opt/forgemesh-client/venv

if [ "$1" = "configure" ]; then
    if [ ! -x "$VENV/bin/python" ]; then
        python3 -m venv "$VENV"
    fi
    "$VENV/bin/pip" install --disable-pip-version-check --no-input \
        --requirement "/opt/ForgeMesh Client Manager/resources/client-requirements.txt" \
        || {
            echo "ForgeMesh Client Manager: could not install Python dependencies." >&2
            echo "Connect to the internet and run:" >&2
            echo "  sudo $VENV/bin/pip install -r '/opt/ForgeMesh Client Manager/resources/client-requirements.txt'" >&2
            exit 1
        }
    # The agent CLIs installed later write shims into ~/.local/bin; the desktop
    # session's PATH usually contains it, but the systemd user session may not.
    chmod 0755 "$VENV/bin/python" 2>/dev/null || true
fi

exit 0
