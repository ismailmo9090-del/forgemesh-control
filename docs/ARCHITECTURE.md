# Architecture

ForgeMesh Browser is a local, terminal-first browser runtime. The CLI and the
HTTP API are two clients of the same `Runtime` composition root; neither talks
to Playwright directly.

```text
                       +-----------------------------+
   fmb CLI (Typer) --->|  local daemon (FastAPI)      |
                       |  uvicorn on 127.0.0.1:8765   |
   ForgeMesh agents -->|  X-API-Token authentication  |
                       +--------------+--------------+
                                      |
                              +-------v--------+
                              |    Runtime     |
                              +-------+--------+
        +----------------+------------+-------------+----------------+
        |                |            |             |                |
  ProfileManager    BrowserManager  Automation    Media/Asset     AI Router
  (SQLite + disks)  (persistent     Controller    Store + Vision  (Ollama /
                     contexts)                                   OpenAI-compat.)
        |                |            |             |
        +----------------+------------+-------------+
                                      |
                              Playwright / Chrome
```

## Process model

| Process | Responsibility |
| --- | --- |
| `fmb` CLI | Parses arguments, renders output, starts the daemon on demand, calls the local API. |
| daemon (`python -m forgemesh_browser.daemon`) | Owns the runtime, browser contexts, SQLite engine, task journal and WebSocket stream. |
| Chrome/Chromium | One persistent process per running profile, launched by Playwright. |

The daemon is lazy: read-only commands such as `fmb status` fall back to the
database and never launch a browser. A browser is started only when an
operation needs a page. `max_contexts` (default 6) bounds concurrent profiles.

The CLI spawns the daemon detached (`python -m forgemesh_browser.daemon`) with
`start_new_session=True`, waits for `/health`, and then uses the token from
`data/runtime/api_token`. `fmb shutdown` verifies `/proc/<pid>/cmdline` before
signalling so a recycled PID is never killed.

## Modules

| Package | Purpose |
| --- | --- |
| `cli/` | Typer application, commands, rich/JSON output, daemon client, interactive prompts. |
| `core/` | `Runtime`, `BrowserManager`, `SessionManager`, `TaskManager`, `EventBus`, exceptions. |
| `profiles/` | Profile records, disk layout, isolation checks and selected-profile state. |
| `automation/` | Target resolution, interactions, navigation, waits, DOM snapshot/extraction, workflows. |
| `media/` | Upload validation, download handling, image metadata, asset manifest and SHA-256 storage. |
| `vision/` | Screenshots, template location, image comparison, OCR. |
| `ai/` | Provider-independent router, Ollama adapter, OpenAI-compatible adapter, prompt policy, image loading. |
| `accounts/` | Provider account registry, manual login flow, session checks, availability state, identity registry (`identities.py`) and the prefill login matrix (`login_runner.py`). |
| `providers/` | Browser-UI adapters for ChatGPT, Claude, Kimi, DeepSeek, Grok, Perplexity, Gemini and Sarvam Indus, including login-form selectors and prefill. |
| `api/` | FastAPI factory, dependency checks, REST routes and WebSocket stream. |
| `security/` | Token creation, file permissions, path containment, audit log, redaction, AES-256-GCM credential vault (`vault.py`). |
| `storage/` | Async SQLAlchemy engine, models and repositories. |

## Request flow

A navigation request:

1. CLI authenticates with the daemon token and calls `POST /browser/navigate`.
2. The route resolves the profile and calls `SessionManager.open`.
3. `ProfileManager.require` validates the profile exists.
4. `BrowserManager.start` creates a Playwright persistent context in
   `data/profiles/<id>` (cookies, localStorage, IndexedDB, cache live there).
5. `BrowserManager.goto` normalises the URL, navigates, captures the title and
   publishes `browser.navigating` / `browser.loaded` events.
6. The CLI renders the result or emits JSON.

Automation requests go through `POST /automation/execute`, which journals the
action in the task table before dispatching it. Failures are journaled with the
error and are re-raised; the API maps them to stable error codes.

## Configuration

Resolution order (later wins):

1. `config/settings.yaml` shipped with the package.
2. `~/.config/forgemesh-browser/settings.yaml` (or `XDG_CONFIG_HOME`).
3. `FMB_CONFIG=/path/to/settings.yaml`.
4. `FMB_<FIELD>` environment variables.
5. Explicit overrides (`--data-dir`, `fmb serve --host/--port`).

`Settings.ensure_dirs()` creates the data tree with mode `0700`. The database is
SQLite in WAL mode; schema creation is versioned through `schema_meta`. Browser
profile data is never deleted during an upgrade.

## Events and tasks

`EventBus` is an in-process ring buffer (500 events) with async subscribers. It
feeds `fmb events` and the `/ws` WebSocket. Events never contain cookies,
passwords or typed form values: `fill` publishes only `characters` and
`text: "***"`.

`TaskManager` writes every API/CLI operation to the `tasks` table with status,
parameters, result, error and duration. Task IDs are returned to clients and
included in error details, so an agent can correlate failures.

## Error model

All domain failures derive from `ForgeMeshError` with a stable `code` and HTTP
status. The API returns:

```json
{"error": {"code": "element_not_found", "message": "...", "details": {...}}}
```

Representative codes: `profile_not_found` (404), `profile_in_use` (409),
`browser_limit` (429), `element_not_found` (404), `ambiguous_target` (409),
`adapter_error` (502), `rate_limited` (429), `ai_provider_error` (502),
`security_error` (403), `download_timeout` (504).

## Design decisions

- **Persistent contexts, not shared user data.** Every profile gets its own
  `launch_persistent_context` directory, which is the only reliable way to
  isolate cookies and storage without copying authentication databases.
- **One daemon owns the browsers.** This keeps sessions alive between CLI
  invocations and gives agents a single authenticated control plane.
- **Strict target resolution.** `automation/selectors.py` tries accessible
  roles, labels and stable selectors, and fails on zero or multiple matches.
  It never clicks a different element to make a script "succeed".
- **Local-first AI.** Ollama is the default and only implicit provider. An
  OpenAI-compatible endpoint is registered only when both base URL and key are
  configured, and requests never fall back to it automatically.
- **Manifest + database.** Assets are recorded in SQLite and appended to
  `assets/manifest.jsonl`, so files remain discoverable even if the database is
  rebuilt.
- **Browser UI adapters are isolated.** Every provider's selectors live in its
  own module. A layout change raises `AdapterError` instead of returning a
  wrong answer.

## Phase status

| Phase | Status |
| --- | --- |
| 1. Core browser, CLI, install | complete |
| 2. Profiles, persistent sessions, manual login | complete |
| 3. Automation engine and workflows | complete |
| 4. Media engine and assets | complete |
| 5. AI/multimodal layer | complete for Ollama + OpenAI-compatible; OCR needs the system Tesseract package |
| 6. Account registry and availability | complete; live logins require the user |
| 7. Daemon, WebSocket, systemd packaging | complete; `scripts/forgemesh-browser.service` is a template you install by hand (see `UBUNTU_DEPLOYMENT.md`), not auto-installed |

Known gaps are listed in [UBUNTU_DEPLOYMENT.md](UBUNTU_DEPLOYMENT.md#known-limitations)
and the README.
