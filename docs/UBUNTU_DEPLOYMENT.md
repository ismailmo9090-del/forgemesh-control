# Ubuntu Deployment

ForgeMesh Browser is a single-user, per-machine daemon. It installs into a
project virtualenv, needs no `sudo`, and stores everything under your user's
XDG data directory. This page covers the two supported modes (desktop and
headless server), systemd units, upgrades and troubleshooting.

Verified on: Ubuntu 26.04.1 LTS, Python 3.14.4, Google Chrome 156.0.8078.79,
Intel Core i5-8365U (8 cores, 15 289 MB RAM).

## 0. Prerequisites

```bash
sudo apt update && sudo apt install -y python3 python3-venv curl   # only if missing
curl -LsSf https://astral.sh/uv/install.sh | sh                    # optional, faster installs
```

`scripts/setup_browser.sh --check` reports what the runtime will use. On this
machine it selects the system browser:

```
System browser: /usr/bin/google-chrome (Google Chrome 156.0.8078.79)
```

Playwright's own Chromium bundle is only downloaded when you ask for it
(`--playwright`), because a system Chrome/Chromium is enough and shares your
existing updates.

## 1. Install

```bash
cd ~/projects/termeinal\ browser        # wherever this tree lives (space in the name is fine)
scripts/install.sh                 # runtime + dev tools + browser check + smoke test
```

If you have the project under version control, `git clone <repo> && cd <repo>`
first; nothing in the install path depends on git.

What it does, in order: platform check (Ubuntu/Debian family), Python ≥ 3.11
check, reuse or create `.venv` (uv if present, else `venv`), editable install
with `[dev]` extras, create `~/.local/share/forgemesh-browser/{profiles,downloads,
screenshots,assets,results,logs,runtime}` and `~/.config/forgemesh-browser` with
mode `0700`, run `setup_browser.sh`, validate `import forgemesh_browser` and the
`fmb` entry point, then a headless smoke test in a throwaway `FMB_DATA_DIR` on
port `8799` that asserts `fmb --json status` reports a running daemon and
browser, stops all sessions and shuts down.

Flags: `--no-dev` (skip pytest), `--no-smoke` (skip the launch test),
`--playwright`, `--python PATH`. It never calls `sudo`; if Chromium needs system
libraries it prints the exact command for you to run.

```bash
source .venv/bin/activate     # or call .venv/bin/fmb directly
fmb --version
fmb status
```

## 2. Mode A — Desktop (Ubuntu with a GNOME session)

Use this when you need to log in to providers by hand, watch automation, or work
interactively.

```bash
scripts/start.sh dev &         # FMB_HEADLESS=false, human-readable logs
fmb profile create work --name "Work"
fmb account login chatgpt --profile work    # visible window; you complete login
fmb account complete chatgpt --profile work
fmb --profile work open https://chatgpt.com/
fmb --json --profile work extract
```

Notes:
- The default is still headless (`headless: true`); `dev` mode and
  `--headed`/`start --headed` opt into a visible window per profile.
- `fmb start` and any browser command auto-spawn the detached daemon
  (`python -m forgemesh_browser.daemon`, logs to `logs/daemon.log`, PID in
  `runtime/daemon.json`) and wait up to 25 s for `/health`.
- Stop it with `fmb shutdown`; `scripts/uninstall.sh` removes only the venv and
  build artifacts, keeping profiles and data.

## 3. Mode B — Headless server / SSH

```bash
scripts/install.sh --no-dev --no-smoke    # smoke test needs a working browser; run it once manually
scripts/start.sh prod --background
fmb --json status
```

Everything except manual login works headless. A visible window needs an X
display, and a *human* must complete any login, CAPTCHA or MFA — so on a server
you have three honest options:

1. **SSH X forwarding** (recommended). From a machine with an X server:
   ```bash
   ssh -X user@server
   cd "<project dir>" && .venv/bin/fmb account login chatgpt --profile work
   ```
   The Chromium window appears on your workstation, you log in there, and the
   session is stored on the server. Then `fmb account complete chatgpt
   --profile work`, and all later runs use headless sessions.
   Needs `xauth` and `X11Forwarding yes` in `/etc/ssh/sshd_config` (that line is
   a config change on your server — review and apply it yourself).
2. **Login once locally, then move the profile.** On your desktop:
   `fmb account login …` + `fmb account complete …`, stop the browser, then copy
   exactly one profile directory over an encrypted channel:
   ```bash
   tar -C ~/.local/share/forgemesh-browser/profiles -czf - work \
     | ssh user@server 'mkdir -p ~/.local/share/forgemesh-browser/profiles \
                        && tar -C ~/.local/share/forgemesh-browser/profiles -xzf -'
   ssh user@server 'chmod -R 700 ~/.local/share/forgemesh-browser/profiles/work'
   ```
   That tarball contains live session cookies: keep it on an encrypted disk,
   delete it after the copy (`shred -u` if you care), and never commit it.
3. **`Xvfb` only if nobody needs to see the page.** `xvfb-run -a fmb account
   login …` gives Chromium a display, but you cannot read a CAPTCHA on a virtual
   screen, so this does not solve first-time authentication. Don't use it to
   pretend a challenge was "handled".

Server hygiene:
- Keep `api_host = 127.0.0.1`. `daemon.py` refuses to bind a non-loopback host
  unless `FMB_API_REMOTE=true`; if you must reach the API from another machine,
  use an SSH tunnel (`ssh -L 8765:127.0.0.1:8765 user@server`) instead of
  exposing the port.
- `max_contexts: 6` by default, so every council seat can open at once. Each
  active profile is a Chromium process tree (~1 GB); on an 8 GB / 2 vCPU VPS,
  lower it to one or two and raise deliberately (see
  [BENCHMARKS.md](BENCHMARKS.md)).
- OCR needs `sudo apt install -y tesseract-ocr`; without it `fmb ocr` fails with
  that command rather than returning empty text.

## 4. systemd

The daemon is foreground-friendly (`fmb serve`, clean SIGTERM,
`timeout_graceful_shutdown=10`, lifespan closes every browser context and the
SQLite handle), so it fits a **user** service — which is the right scope because
profiles, the API token and `fmb` belong to one login.

A template ships at `scripts/forgemesh-browser.service`:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/forgemesh-browser.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now forgemesh-browser.service
systemctl --user status forgemesh-browser.service
journalctl --user -u forgemesh-browser.service -f
fmb status            # the CLI now attaches to the supervised daemon
```

`loginctl enable-linger $USER` keeps it running after you log out (it starts it
at boot too). Without lingering, the daemon dies with your session.

Key directives and why:

| Directive | Reason |
| --- | --- |
| `ExecStart=%h/projects/terminal-browser/.venv/bin/fmb serve` | absolute path; systemd has no login shell, so no venv activation and no `~`. If your project directory contains a space (e.g. `~/projects/termeinal browser`), don't fight systemd's quoting — make a space-free symlink and use that: `ln -s ~/projects/termeinal\ browser ~/.local/share/forgemesh-src`, then `ExecStart=%h/.local/share/forgemesh-src/.venv/bin/fmb serve` |
| `Restart=on-failure`, `RestartSec=5` | a crash restarts; **not** `always` — `fmb serve` deliberately exits if another daemon already answers `/health`, and `Restart=always` would turn that guard into a restart loop |
| `MemoryMax=4G`, `MemoryHigh=3G` | one Chromium tree measured ~1 GB; this bounds a runaway context instead of the whole box |
| `CPUQuota=400%` | leaves headroom for the rest of the system on an 8-core laptop |
| `TimeoutStopSec=30` | generous enough for graceful browser shutdown |
| `LimitNOFILE=65536` | Chromium wants many fds |
| `NoNewPrivileges=true` | nothing here needs privilege escalation |
| `UMask=0077` | anything the daemon creates is private by default |

Deliberately **not** used: `ProtectHome`/`ReadOnlyPaths` (the daemon must write
its own data dir), `PrivateTmp` (Chromium relies on `/tmp` for shared memory and
singleton locks), `ProtectSystem=strict` (the editable install lives in your
home). If you need stronger isolation, run ForgeMesh in a dedicated container or
VM and mount only its data directory.

For a system-wide unit (multi-user hosts): set `User=`, `Group=`, and an explicit
`Environment=XDG_DATA_HOME=/home/<user>/.local/share`; do not run the daemon as
root — Chromium drops its own sandbox under root and you would own every
provider session as root-owned state.

## 5. Configuration in deployment

Resolution order (later wins): bundled `config/settings.yaml` →
`~/.config/forgemesh-browser/settings.yaml` → `$FMB_CONFIG` file → `FMB_*`
environment → explicit CLI overrides.

In `~/.config/forgemesh-browser/settings.yaml`:

```yaml
headless: true
max_contexts: 6
api_host: 127.0.0.1
api_port: 8765
ai_provider: ollama
ollama_host: http://127.0.0.1:11434
ollama_model: llama3.1
ocr_lang: eng
log_level: INFO
log_json: true
```

In a unit file, prefer environment overrides so secrets stay out of the YAML
under your home:

```ini
Environment=FMB_HEADLESS=true
Environment=FMB_API_PORT=8765
Environment=FMB_AI_PROVIDER=ollama
# only if you knowingly accept remote, third-party calls:
# EnvironmentFile=%h/.config/forgemesh-browser/openai.env   # chmod 600 this file
```

`EnvironmentFile` with mode `0600` is the safe way to pass
`FMB_OPENAI_API_KEY`. Nothing in ForgeMesh reads a key from a command-line
argument, and `/settings` only ever reports `openai_configured: true/false`.

If you use the identity vault with a master passphrase, the daemon must see the
same value as the CLI. Put it in a `0600` environment file rather than in the
unit file or `settings.yaml`:

```ini
EnvironmentFile=%h/.config/forgemesh-browser/vault.env   # chmod 600
# contents: FMB_VAULT_PASSPHRASE=...
```

Without a passphrase the daemon reads the random key file at
`~/.config/forgemesh-browser/vault.key` (mode `0600`), which needs no unit-file
configuration. If neither is available, prefill logins return
`needs_credential`; nothing is decrypted and no secret is exposed.

## 6. Upgrade

Profiles, accounts, assets, tasks and audit rows live in `data_dir`, never in the
virtualenv — so an upgrade does not touch logins:

```bash
cd "<project dir>"
git fetch && git pull           # or replace the tree
systemctl --user stop forgemesh-browser.service
scripts/install.sh              # reuses .venv, reinstalls editable, re-runs smoke test
scripts/benchmark.py            # optional: compare against data/results/benchmark.json
pytest -q                       # optional: 102 tests, browser subset needs Chrome
systemctl --user start forgemesh-browser.service
fmb status && fmb account check-all
```

Database schema is created by `Database.init()` on start; if you want to inspect
it first, `sqlite3 ~/.local/share/forgemesh-browser/forgemesh.db '.schema'`.
Back up before a risky upgrade — a single tar of `data_dir` is the whole state,
and it contains live session cookies, so encrypt it (`gpg -c` prompts for a
passphrase):

```bash
systemctl --user stop forgemesh-browser.service
tar -C ~/.local/share -cf - forgemesh-browser | gzip | gpg -c -o ~/fmb-backup-$(date +%F).tar.gz.gpg
chmod 600 ~/fmb-backup-*.tar.gz.gpg
```

## 7. Uninstall

```bash
scripts/uninstall.sh                 # confirms, stops daemon, removes .venv/caches/build output
scripts/uninstall.sh --purge-data    # additionally deletes profiles + assets (second "DELETE" prompt)
```

It never removes system packages, Chrome, or your `~/.config` settings unless you
ask. `--purge-data` destroys logged-in sessions irreversibly.

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Daemon did not become ready within 25s` | Read `fmb logs -n 200`. Usually a port clash: `ss -ltnp \| grep 8765`, then set `FMB_API_PORT`. |
| `Cannot reach the local daemon at …` | Stale `runtime/daemon.json`. Confirm the PID really is a daemon (`fmb shutdown` refuses to kill an unknown PID), otherwise remove the file and retry. |
| `Browser not found` / launch failure | `scripts/setup_browser.sh` (or `--playwright` to download a bundle). Chromium also needs its system libs; the script prints the apt command. |
| `Missing X server` / `Display` errors | A headed command (`--headed`, `account login`) without a display. Use `ssh -X`, or run the login on a desktop and copy the profile (Mode B). |
| `401 Invalid or missing API token` | Wrong header, or another user's token. `TOKEN=$(cat ~/.local/share/forgemesh-browser/runtime/api_token)`; the file must stay `0600`. |
| `403 Remote API access is disabled` | You are reaching the daemon from a non-loopback peer. Use an SSH tunnel, or set `FMB_API_REMOTE=true` knowingly. |
| `429 browser_limit` | `max_contexts` reached; stop a profile (`fmb stop --profile x`) or raise the cap and budget the RAM. |
| `ai_provider_error: Ollama is not reachable` | `ollama serve` isn't running, or `FMB_OLLAMA_HOST` is wrong. `fmb ai-health` shows per-provider status. The tool will **not** fall back to a paid API. |
| `ai_dependency_missing` on `fmb ocr` | `sudo apt install -y tesseract-ocr` (+ `tesseract-ocr-urd` etc. for other `--lang`). |
| `adapter_error: composer not found` | The provider changed its UI. Re-check selectors in `providers/<id>/adapter.py` (see [PROVIDER_ADAPTERS.md](PROVIDER_ADAPTERS.md)); until a real `fmb account ask` succeeds, the provider is not working. |
| `rate_limited` | The site said so. `fmb account status` shows `retry_after` from its own wording. Wait; do not switch accounts. |
| Sessions lost after reboot | Nothing to fix — Chromium state is on disk. If it happens, check `data_dir` isn't on a tmpfs or ephemeral container layer. |
| Disk growth | `du -sh ~/.local/share/forgemesh-browser/*` — profiles (cache) dominate. `fmb profile delete` and prune `assets/` you no longer need. |

Log locations: `logs/daemon.log` (auto-spawned daemon), `logs/start.log`
(`start.sh --background`), `logs/audit.jsonl` (destructive actions),
`journalctl --user -u forgemesh-browser.service` (systemd),
`fmb tasks` / `fmb events` (journal + event history).

## 9. Benchmark reference

Measured on this machine with `scripts/benchmark.py`
(full numbers in [BENCHMARKS.md](BENCHMARKS.md)):

- runtime start 20.9 ms; browser (profile) start 543 ms
- idle profile: 927 MB RSS across 9 processes, ~9.2 % of one core
- page load 68 ms; screenshot 76 ms; DOM extract 5.9 ms
- two concurrent profiles: opened in 494 ms, 2 872 MB RSS total
- clean `stop --all` in 149 ms

Budget roughly 1 GB RSS and a few percent of one core **per active profile**.
On 2 vCPU / 4 GB, keep `max_contexts: 1–2` and stop profiles when idle.

## 10. Known limitations

Deployment-scoped limits (product-wide limits are in the README):

- Manual login needs a display. Headless servers cannot complete a first-time
  login, CAPTCHA or MFA without X forwarding or moving a profile directory
  (§3). There is no screenshot-driven "login by proxy" mode, by design.
- The systemd unit is a template you install; nothing auto-enables it, and no
  system-wide (multi-user) unit ships. The daemon is single-user by design.
- The API has one static token shared by everything: no scopes, no expiry, no
  rate limiting, no TLS. Anything that reaches your localhost socket as your
  user can drive the browser — that is inherent to this design and the reason
  `api_remote` defaults to off.
- Non-loopback binding requires `FMB_API_REMOTE=true`; the daemon refuses
  otherwise rather than silently opening the port.
- `loginctl enable-linger` is needed for boot persistence; without it the daemon
  dies at logout and `fmb` respawns it on demand (adding ~0.5 s).
- OCR is not bundled: `tesseract-ocr` must be installed separately, per language
  pack too.
- Provider adapters are point-in-time selector sets. A site redesign can make an
  account `error`/`adapter` failure until the selectors are updated; there is no
  automatic selector discovery.
- Benchmarks come from one laptop-class machine and a local page; see
  [BENCHMARKS.md](BENCHMARKS.md#limitations-of-these-numbers).
- No Windows or macOS support (Linux paths, `/proc` scanning, systemd, X11).
- The daemon owns browsers as your user — no privilege separation between
  profiles beyond file permissions, and no protection against malware already
  running as you.
