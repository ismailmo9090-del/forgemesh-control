# ForgeMesh Client Manager (Linux)

The desktop app that runs on a **worker / output laptop**. It turns the machine
into a mesh worker, installs and signs in to the coding agents locally, and
shows the review, approval and delivery screens against a ForgeMesh control
server.

It is not the control server. The server runs `fmc` on another machine (or
127.0.0.1 in a single-laptop demo); this app pairs with it using a single-use
code, keeps a worker token, and supervises the worker loop.

## Architecture

```
Electron main (client/desktop/main.js)
  ├─ starts  python -m forgemesh_control.client_manager --handshake --port 0
  │            → local FastAPI bridge on 127.0.0.1:<random>, random bearer token
  ├─ loads   client/frontend/dist (React renderer, IPC only, no network access)
  └─ proxies renderer → bridge over IPC → HTTP on loopback
                 │
                 └─ spawns  python -m forgemesh_control.client_worker
                              → mesh protocol v1 WebSocket to the control server,
                                newline-delimited JSON events back to the bridge
```

* The bridge only listens on `127.0.0.1` and rejects requests without the
  handshake bearer token.
* The worker child receives `FMW_SERVER`, `FMW_WORKER_TOKEN` and
  `FMW_DEVICE_KEY` through its environment, never argv, so the token cannot be
  read from `ps`.
* The renderer cannot open network connections: every request goes through
  `ipcMain` and the preload only exposes the documented bridge calls.

## Run from a checkout

```bash
# one-time
(cd client/frontend && npm install && npm run build)
(cd client/desktop && npm install)

# run (uses ../../.venv/python from the repo root, or FMCM_PYTHON)
cd client/desktop && npm start
```

Evidence capture for a UI report:

```bash
cd client/desktop && npm run start:capture   # writes captures/*.png
```

## Package

```bash
bash client/build-client.sh          # typecheck + build + AppImage/deb + SHA256SUMS
```

Artifacts land in `client/desktop/release/`. Generated names use the version in
`client/desktop/package.json`, which is kept in step with `pyproject.toml`.

The `.deb` post-install script creates `/opt/forgemesh-client/venv` and installs
`client/requirements-client.txt` (the bridge's runtime subset, not the browser
runtime). The Electron shell picks the first working interpreter from:

1. `FMCM_PYTHON`
2. a bundled `resources/backend/venv/bin/python` (not produced today)
3. `/opt/forgemesh-client/venv/bin/python` (this package)
4. `/opt/forgemesh/venv/bin/python` (control-server package on the same laptop)
5. the checkout `.venv`, then `/usr/bin/python3`

`apt install ./client/desktop/release/forgemesh-client-manager_<version>_amd64.deb`
also installs `python3-venv`, `python3-pip`, `curl`, `ca-certificates` and
`libnotify4`. The first install needs internet for the wheel download; upgrades
re-run the same pip command.

The **AppImage has no install hook**, so it does not create that runtime. Run it
with `FMCM_PYTHON` pointing at an interpreter that already has
`client/requirements-client.txt` installed, or use the `.deb` on worker laptops.

## Agents

The roster is data-driven and intentionally limited to **OpenCode** (`opencode`)
and **Qoder** (`qodercli`), matching `OFFICIAL_SOURCES` in the control server.
Installation mirrors `agents/installer.py`:

1. detect OS/architecture and report whether the platform is supported;
2. download the official installer over HTTPS and inspect it before running;
3. execute the saved script with `bash` (never a `curl | bash` pipe), honouring
   `--no-modify-path` / `--skip-path`;
4. re-detect the real binary in the documented locations and run `--version`;
5. fall back to the npm package only when the script route fails, and report
   which route was used;
6. verify functionally before marking the agent installed.

Sign-in opens a PTY hosted by the bridge (`agent_auth.py`), so password entry,
MFA, CAPTCHA and provider consent stay with the human in the GUI. The app never
types credentials for you, never bypasses a challenge, and never asks you to
paste a password into a terminal command. Sign-in failures are reported with
the exact line the CLI printed; a challenge stops the flow and asks the human.

The client never calls the server's `/api/agents/*` routes: those install
agents **on the server host**. Worker-side installation is this app's job.

## Security posture

| Concern | Handling |
| --- | --- |
| Worker token at rest | Secret Service via `secret-tool`; falls back to a `0600` file with a visible backend label in Settings |
| Token in transit | HTTPS/WSS required for non-loopback servers; plain HTTP is refused unless the user explicitly enables the hotspot switch **and** acknowledges it |
| Token in logs | `[redacted]` before events or worker logs are stored |
| Local API | Loopback-only, random port, random bearer token from the startup handshake |
| Artifacts | Absolute-path check, workspace containment check, SHA-256 recomputation, sanitised name, `0600` copy |
| Server responses | Treated as untrusted input; rendered as text, never executed |
| Destructive actions | Stop/takeover/update/copy require an explicit confirmation flag |

## Operator mode

Approval, review, delivery and artifact screens use the dashboard API. Those
routes need the control token and a loopback connection to the server, so the
usual setup is an SSH tunnel (`ssh -L 8787:127.0.0.1:8787 user@server`). When
this laptop *is* the server, Settings → Operator → "Use local server token"
reads the token the server wrote to
`~/.local/share/forgemesh-control/runtime/control_token`.

Without operator mode the app still pairs, supervises the worker and manages
agents locally; the review screens show the `operator_token_required` state.

## Systemd conflict

`forgemesh-worker.service` also opens a worker connection. The mesh allows one
connection per device, so starting the GUI worker while the unit runs offers a
takeover (stop the unit, let the GUI own the loop). The app never silently
stops a service you started.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_client_manager.py -q
(cd client/frontend && npm run typecheck)
```

The client tests cover the transport guard, insecure-HTTP propagation to the
worker child, secret storage permissions and redaction, operator-token
requirements, and artifact path/checksum/copy hardening. They do not require a
server.

## Known limits

* Sign-in is human-gated by design; an unattended flow stops at the first
  challenge.
* The bridge is a local trust boundary: it isolates other users, not malware
  already running as you. Install the app from a source you trust.
* Wayland compositors without GPU compositing may take longer to show the main
  window; the splash reports each startup stage.
* The AppImage does not install Python dependencies; without `FMCM_PYTHON` it
  falls back to `/usr/bin/python3` and the bridge will not start. Prefer the
  `.deb` on worker laptops.
* A failed first install (offline) leaves `/opt/forgemesh-client/venv`
  incomplete; re-run `sudo apt install --reinstall ./forgemesh-client-manager_*.deb`.
