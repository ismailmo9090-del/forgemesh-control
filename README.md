# ForgeMesh Browser

Terminal-first, AI-ready browser automation runtime for Ubuntu. It drives a real
Chrome engine through Playwright, keeps one **isolated profile per identity**,
survives manual logins across restarts, exposes everything through the `fmb`
CLI and a token-authenticated local HTTP + WebSocket API, and records every
action in a SQLite journal plus an append-only audit log. It is built to be a
core component of a multi-agent system, not a thin Playwright wrapper.

Version 0.1.4 · Python ≥ 3.11 · Ubuntu 26.04 tested (22.04+ expected) ·
no root required to install or run.

## Status at a glance

| Area | State |
| --- | --- |
| CLI (`fmb`: 44 direct commands plus the `profile`, `account` and `identity` groups) | complete and exercised end-to-end against a local site |
| Isolated Chrome-like profiles, persistent logins | complete; system Chrome runs as a normal process with Playwright attached over CDP, so Google/IdP OAuth accepts it |
| Encrypted credential vault + prefill logins per identity × provider | complete; validated end-to-end against a local login site, 2FA/CAPTCHA stop for a human |
| Automation (click/fill/type/select/scroll/wait/inspect/extract/workflows) | complete |
| Screenshots, OCR, template locate, image compare | complete (OCR needs system `tesseract`) |
| Upload/download with SHA-256 asset manifest | complete |
| FastAPI + WebSocket local API with token auth | complete |
| AI layer (Ollama local-first, OpenAI-compatible opt-in) | implemented and unit-tested; **not verified against a live model on this machine** (Ollama not installed here) |
| Provider adapters (ChatGPT, Claude, Kimi, DeepSeek, Grok, Perplexity, Gemini, Sarvam Indus) | implemented; DeepSeek validated end-to-end (manual Google login + live prompt), others not yet validated live |
| Tests | 153 headless-unit/integration tests, all passing; real-Chrome suite marked `browser` |
| systemd packaging | unit file and docs provided; not auto-installed |

## Requirements

- Ubuntu 22.04 or newer (`install.sh` warns on other distributions).
- Python 3.11+.
- A Chrome engine: Google Chrome, Chromium, or Playwright's own build. This
  project uses `/usr/bin/google-chrome` (Chrome 156) when present;
  `scripts/setup_browser.sh` reports and can install an alternative.
- Optional: `tesseract-ocr` for `fmb ocr`, `ollama` for the AI commands,
  `curl` only for the API doc examples (`wget` and the CLI work without it).
- Node.js 18+ and npm only to build the control dashboard from source; the
  `.deb` already embeds it.

## Install

```bash
cd "<project dir>"                   # quote the path if it contains a space
bash scripts/install.sh
```

That creates/reuses `.venv`, installs the package in editable mode with dev
extras, prepares `~/.local/share/forgemesh-browser` (mode `0700` subdirs),
checks the browser and runs a headless launch smoke test. It never calls
`sudo`; missing system libraries are reported with the exact command for you to
run.

| Flag | Effect |
| --- | --- |
| `--no-dev` | runtime dependencies only (skips pytest/ruff) |
| `--no-smoke` | skip the browser launch smoke test |
| `--playwright` | download Playwright Chromium even if a system Chrome exists |
| `--python PATH` | interpreter to build the venv with |

Useful follow-ups: `bash scripts/setup_browser.sh --check`,
`bash scripts/uninstall.sh` (keeps profiles; `--purge-data` deletes them after
one confirmation). Optional global command:
`ln -s "$PWD/.venv/bin/fmb" ~/.local/bin/fmb`.

### Debian package (`.deb`)

```bash
(cd frontend && npm ci && npm run build)   # prebuilt dashboard is embedded
bash packaging/build-deb.sh                # writes dist/forgemesh-control_<version>_all.deb
sudo apt install ./dist/forgemesh-control_0.1.4_all.deb
```

The package installs the application to `/opt/forgemesh/app`, creates a private
virtual environment in `/opt/forgemesh/venv` (internet is required for that
step), and adds `fmb`, `fmc` and a **ForgeMesh Control Center** launcher that
starts the server and opens the dashboard. Data stays in each user's
`~/.local/share/forgemesh-*` directories. Optional user services live in
`/usr/lib/systemd/user/` (`systemctl --user enable --now forgemesh-control`).

### One command (install, update or launch)

On a fresh machine:

```bash
curl -fsSL https://raw.githubusercontent.com/ismailmo9090-del/forgemesh-control/main/scripts/forgemesh.sh | bash
```

The package also installs the same script as `forgemesh`, so later runs need no curl:

```bash
forgemesh              # install or update to the latest release, then launch
forgemesh --run        # launch the installed version (no network, no changes)
forgemesh --install    # install or update only
forgemesh --worker     # worker laptop: install fmw, print the join steps, start nothing
forgemesh --reinstall  # reinstall even when the version is current
forgemesh --dry-run    # show the plan, change nothing
```

It compares the installed package with the latest GitHub release, installs only
when the package is missing or older, restarts a control server that is running
an outdated version, then opens the dashboard. It uses `sudo` only for `apt` and
never touches credentials.

## Run

Daemon-managed, one command each (the CLI starts the daemon on demand):

```bash
fmb status                                        # daemon, profiles, sessions
fmb profile create work --name "Work"
fmb -p work open https://example.com
fmb -p work inspect                               # stable refs: ref=e12, role=…, css=…
fmb -p work click "Sign in"
fmb -p work fill "label=Email" "user@example.com"
fmb -p work wait-for --text "Inbox"
fmb -p work extract --output page.json
fmb -p work screenshot --full-page -o ./page.png
fmb -p work upload ./design.png --selector "input[type=file]"
fmb -p work download --timeout 60 --output ./downloads/
fmb locate --image ./button.png --screenshot ./page.png -o annotated.png
fmb compare ./before.png ./after.png -o diff.png
fmb tasks ; fmb -p work stop ; fmb shutdown
```

Long-lived daemon (systemd-friendly):

```bash
scripts/start.sh prod            # headless, JSON logs, foreground
scripts/start.sh prod --background
scripts/start.sh dev             # headed-capable, human logs
.venv/bin/fmb serve              # the same daemon call, directly
```

Manual login into an AI platform (the browser is visible; you type the
password, solve the CAPTCHA and complete MFA yourself — the tool never asks for
them in a terminal):

```bash
fmb account login chatgpt --profile work     # uses scripts/start.sh dev or --headed
fmb account complete chatgpt --profile work  # verify and record the session
fmb account status
```

### Identities, encrypted vault and prefill logins

A **profile** isolates cookies; an **identity** is the human email that owns one
profile. Register an email once, store its password in the local encrypted
vault, and `fmb` fills the site's own email/password form for every provider:

```bash
fmb identity add you@gmail.com --label personal         # creates profile 'you'
fmb identity password you@gmail.com                     # hidden prompt, AES-256-GCM vault
fmb identity password you@gmail.com --provider gemini   # optional per-site override
fmb identity login you@gmail.com --provider gemini      # prefill, then pause for 2FA
fmb identity login-all                                  # identity × provider matrix
fmb identity status                                     # sessions + vault metadata
```

What the runner does and does not do:

- It only types into the site's own form. "Continue with Google/Microsoft/Apple"
  buttons are clicked; they are never imitated.
- CAPTCHA, OTP/2FA, device approval and "Verify it's you" stop that cell with
  `needs_human`. The browser window stays open so you can finish it; the batch
  moves on to the next cell.
- A rejected password becomes `needs_credential`. The runner never tries a
  different password (no lockout roulette) and never retries the same one.
- One email = one profile. Two emails in one profile would overwrite each
  other's cookies, so the mapping is enforced when the identity is created.
- The password is decrypted in the daemon only for the length of the prefill
  call. It is never returned by the API, written to the task journal, emitted as
  an event or printed by the CLI.
- Existing sessions are checked on the app URL first, so a second run reports
  `already_logged_in` instead of submitting credentials again.

Key management: by default a random 256-bit key is created at
`~/.config/forgemesh-browser/vault.key` (mode `0600`). To require a master
passphrase instead, export `FMB_VAULT_PASSPHRASE` for **both** the CLI and the
daemon *before* the first `fmb identity password`. Losing the key loses the
stored passwords; there is deliberately no recovery backdoor.

Local API (`~/.local/share/forgemesh-browser/runtime/api_token` is the secret):

```bash
export FMB=http://127.0.0.1:8765
export TOKEN=$(cat ~/.local/share/forgemesh-browser/runtime/api_token)
curl -H "X-API-Token: $TOKEN" $FMB/status
curl -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"navigate","text":"https://example.com"}' $FMB/automation/execute
```

Machine-readable output everywhere: `fmb --json status`, `fmb --json tasks`.
Interactive API docs are served at `$FMB/docs`.

## Tests

```bash
.venv/bin/python -m pytest -q            # 153 passed (no browser needed)
.venv/bin/python -m pytest -q -m browser # 6 passed (real Chrome, local mock site)
```

The suite runs against a mock site served from `tests/conftest.py`
(`ThreadingHTTPServer` on `127.0.0.1`, ephemeral port) so no external network or
provider account is touched. Layout: `tests/unit/` (settings and secret
handling, selectors, profiles, providers, media, vision, AI router, event/task
journal, automation journal redaction), `tests/integration/test_api.py` (token
auth, localhost policy, profile CRUD, `/compare`), `tests/browser/`
(navigate→inspect→click→extract→screenshot→download on a live Chromium).
Path-traversal and no-secret-leak coverage lives in
`tests/unit/test_path_validation.py` and `tests/unit/test_settings_security.py`.

## Resource usage (measured on this machine)

`.venv/bin/python scripts/benchmark.py` (full write-up in
[`docs/BENCHMARKS.md`](docs/BENCHMARKS.md)), i5-8365U / 15 GB / Chrome 156:

| Metric | Value |
| --- | --- |
| Runtime start | 20.9 ms |
| Browser start (persistent context) | 543.4 ms, 9 processes |
| Idle RSS, one profile | 927 MB · 9.2 % of one core |
| Local page load / screenshot / DOM extract | 67.6 ms / 75.9 ms / 5.9 ms |
| Two extra profiles opened concurrently | 494.4 ms |
| Three live profiles | 2 872 MB total (≈96 % is Chromium, ~85 MB is ForgeMesh) |
| `stop_all` | 149.2 ms |

`max_contexts` (default 6, one seat per council provider) is the memory valve.
Each extra headless profile costs roughly 1 GB, so lower it on small machines.

## Security posture

- API bound to `127.0.0.1` and origin-checked; binding elsewhere is refused
  unless `FMB_API_REMOTE=true`. Token compared in constant time; `/health` is
  the only public route. WebSocket uses `?token=` with `4401`/`4403` closes.
- Passwords are opt-in and encrypted: the vault is one AES-256-GCM blob keyed
  either by a `0600` key file or an scrypt-stretched `FMB_VAULT_PASSPHRASE`.
  Nothing plaintext reaches SQLite, the journal, the audit log, events, task
  records or API responses. If you never run `fmb identity password`, the tool
  stores no password at all. Typed automation values are still reduced to
  `text_chars`/`value_chars` before they reach the journal.
- All user-supplied paths go through containment checks (`0600` files, `0700`
  dirs, symlink refused, traversal blocked); outputs are pinned to the results
  or data directory.
- Page content is treated as untrusted: no `eval`, no executing commands found
  in text, workflow steps are validated against an action whitelist, and only
  `http`/`https` URLs are accepted.
- No CAPTCHA/MFA bypass, no account rotation to evade provider limits, and no
  silent failover to a paid API — the OpenAI-compatible adapter exists only when
  you configure a key.
- Destructive operations (`profile delete`, `shutdown`, `uninstall --purge-data`)
  require explicit confirmation.

Details and threat model: [`docs/SECURITY.md`](docs/SECURITY.md).

## ForgeMesh Control Center

The same repository also ships the central server that sits above this runtime:
`forgemesh_control` (`fmc`, port 8787) with a light-theme React dashboard and an
Electron shell. It manages AI provider accounts (one isolated browser profile
per account), the OpenCode and Qoder CLIs on the central server, the
account-aware AI council, projects, workspaces, workers and delivery - and it
drives the browser **only** through this package's HTTP/WS API.

```bash
fmc serve                                 # dashboard on http://127.0.0.1:8787
bash scripts/install_desktop.sh           # launcher + .desktop entry for the Electron shell
```

First-run results measured on this machine, the mocked-versus-integration test
split and the full list of known limitations are documented in
[`docs/CONTROL_CENTER.md`](docs/CONTROL_CENTER.md).

### Mesh workers (other laptops)

Workers connect **outbound** to the control server; no ports need to open on
the worker laptops.

1. On the central server, allow LAN access (only behind a trusted network or a
   TLS reverse proxy):
   ```bash
   FMC_HOST=0.0.0.0 FMC_ALLOW_REMOTE=true fmc serve
   ```
2. Open **Workers → Create pairing code** in the dashboard. Codes are
   single-use and expire after 10 minutes.
3. On the worker laptop (install the `.deb` or run `scripts/install.sh` from a
   clone), pair and keep the session open:
   ```bash
   fmw register --server http://CENTRAL-IP:8787 --code ABC123XY --name "Worker 1" --insecure-http
   fmw run
   ```
   `--insecure-http` is accepted only for trusted LANs; use `https://` when the
   server sits behind TLS. The returned worker token is stored mode `0600` in
   `~/.config/forgemesh-worker/token` and shown once.
4. Install and log in to the coding CLIs (OpenCode/Qoder) on the worker
   itself - the control server never installs or authenticates them remotely.
5. The Workers page lists the node after its first real heartbeat. Revoking a
   worker from the dashboard invalidates its token immediately.
6. Keep it running after logout: `systemctl --user enable --now forgemesh-worker`.

### Public access over ngrok (no port forwarding)

If the worker is not on the same LAN, expose the control server through an
ngrok HTTPS tunnel instead of opening router ports:

1. Open **Worker Nodes → Public access · ngrok**.
2. Click **Download ngrok (official)** if it is not installed, then paste your
   authtoken from <https://dashboard.ngrok.com/get-started/your-authtoken> and
   save it. The token is written by ngrok's own `config add-authtoken` command
   into a private file (mode `0600`) and the dashboard never shows it again.
3. Click **Start public tunnel**. The card shows the live `https://…ngrok…`
   URL only after ngrok's local API reports it. A token saved through this card
   is treated as consent to publish: after a restart the server starts that
   tunnel again by itself, so the URL is back before you go looking for it.
   A token in ngrok's shared `~/.config/ngrok` config never triggers this;
   disable it entirely with `FMC_AUTO_START_TUNNEL=false`.
4. Copy the public URL, create a pairing code, and copy the worker commands the
   card shows. On the worker laptop the first line installs `fmw` without
   starting a server there:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/ismailmo9090-del/forgemesh-control/main/scripts/forgemesh.sh | bash -s -- --worker
   fmw register --server https://YOUR-URL.ngrok-free.app --code ABC123XY --name "Worker 1"
   fmw run
   ```
   No `--insecure-http` is needed for the HTTPS URL, and the server can keep
   listening on `127.0.0.1` — the tunnel forwards to it locally. The worker's
   coding CLIs (OpenCode/Qoder) are installed and signed in on that laptop; this
   server never does that for it.

The free ngrok plan assigns a new hostname on every start and can show a
one-time browser warning page on the dashboard (click **Visit Site**; worker
WebSockets are unaffected). The URL is publicly reachable: the control token
still protects every route, so keep the URL private. A new pairing code is
needed after each tunnel restart.

## Documentation

| File | Covers |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | module map, data flow, design decisions, phase status |
| [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) | every command, global options, target grammar, workflows |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | REST + WebSocket reference with real payloads and error codes |
| [`docs/PROFILE_SYSTEM.md`](docs/PROFILE_SYSTEM.md) | profile layout, isolation guarantees, multi-agent patterns |
| [`docs/PROVIDER_ADAPTERS.md`](docs/PROVIDER_ADAPTERS.md) | adding a browser provider **and a new AI provider adapter** |
| [`docs/SECURITY.md`](docs/SECURITY.md) | threat model, redaction rules, containment, fair-use limits |
| [`docs/UBUNTU_DEPLOYMENT.md`](docs/UBUNTU_DEPLOYMENT.md) | desktop/headless install, systemd unit, upgrades, troubleshooting |
| [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md) | methodology, measured numbers, their limits |

## Known limitations

- AI responses were not verified against a live model here (Ollama is not
  installed on this machine); the transport, capability checks and error paths
  are covered by tests with a fake provider.
- The eight provider adapters encode current ChatGPT/Claude/Kimi/DeepSeek/Grok/
  Perplexity/Gemini/Sarvam Indus UI structure. DeepSeek is validated live; the
  rest are unverified in this environment and need selector updates when those
  sites change. The Sarvam Indus chat UI sits behind a login wall, so its
  post-login selectors are best-effort until first use.
- Prefill login is best-effort by design: sites that only offer SSO, change
  their form, or add an extra step come back as `needs_human` (or `error`) and
  need the manual `fmb account login` path. The runner never guesses selectors
  across frames or shadow DOM.
- CAPTCHA and 2FA are never automated, so a fully unattended batch leaves those
  cells pending on purpose. Disposable/temporary email domains and mass account
  creation are out of scope; the vault is for accounts you already own.
- OCR requires the system `tesseract` binary (absent here), so `fmb ocr` returns
  the install hint instead of text until it is installed.
- Profiles are Chromium-persistent-context isolated, not VM isolated; a
  compromised host can read them. Cookie exports are not implemented.
- One engine: no Firefox/WebKit backends. `extensions`, proxy rotation and
  stealth measures are deliberately out of scope.
- systemd units are templates you install; `install.sh` does not touch system
  services.
- Benchmarks are single runs on localhost pages; see the caveats in
  `docs/BENCHMARKS.md`.

Remaining work the spec anticipates, in order of value: live-model verification
of the AI layer, live-account validation of provider adapters, WebKit/Firefox
engines, a scheduled-task runner, and packaging as a wheel/PWA-free distribution.

## Adding a new AI provider adapter

Short version (full worked example with LM Studio in Part 2 of
[`docs/PROVIDER_ADAPTERS.md`](docs/PROVIDER_ADAPTERS.md)):

1. Add the settings fields you need in `src/forgemesh_browser/settings.py`
   (`base_url`, `api_key`, `model`) so they arrive from YAML/`FMB_*` env.
2. Create `src/forgemesh_browser/ai/<name>_adapter.py` implementing
   `AIProvider`: `id`, `is_local`, `supports_vision`, `available()`,
   `health()`, `models()`, `complete(AIRequest) -> AIResponse`. Raise only
   `AIProviderError` subtypes, keep `health()` non-raising, and never log prompts.
3. Register it in `AIRouter` (`src/forgemesh_browser/ai/router.py`) — always for
   local providers, only when configured for ones needing a key, so nothing
   silently becomes a paid path.
4. Reuse the existing surface: `fmb ai`, `fmb ai-providers`, `fmb ai-health`,
   `POST /ai` pick it up automatically.
5. Add a fake-transport test mirroring `tests/unit/test_ai_router.py`, including
   the vision-unsupported and timeout cases.

## Project layout

```
src/forgemesh_browser/   105 modules, ~9.5k lines
  core/                  settings, browser manager, events, tasks, exceptions
  profiles/              creation, isolation checks, storage, models
  automation/            interaction, selectors, waits, accessibility, workflows
  media/                 assets, uploads, downloads, image manager
  vision/                screenshots, template locate, compare, OCR
  ai/                    router, Ollama + OpenAI adapters, multimodal
  providers/ accounts/   site adapters, registry, session health
  api/                   FastAPI app, routers, WebSocket, deps
  cli/                   Typer app, output, client (auto-starts the daemon)
  security/ storage/     path validation, SQLAlchemy models
config/                  settings.yaml, providers.yaml
scripts/                 install/setup_browser/start/uninstall/benchmark + systemd unit
tests/                   unit + integration + real-browser
docs/                    see the table above
```

Data lives in `~/.local/share/forgemesh-browser/` (`FMB_DATA_DIR` overrides):
`profiles/`, `assets/`, `downloads/`, `screenshots/`, `results/`, `logs/`,
`runtime/` (pid, api_token, daemon.json) and `forgemesh.db`.
