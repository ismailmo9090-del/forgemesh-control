# Local API Reference

The daemon serves a JSON HTTP API plus a WebSocket event stream on
`http://127.0.0.1:8765` by default (`api_host`/`api_port` in settings, or
`--host`/`--port` on `fmb serve`). Interactive OpenAPI docs are at `/docs`.

Base URL used in every example:

```bash
export FMB=http://127.0.0.1:8765
export TOKEN=$(cat ~/.local/share/forgemesh-browser/runtime/api_token)
```

The examples use `curl` (`sudo apt install curl` if it is missing). The same
requests work with `wget --header="X-API-Token: $TOKEN"` plus
`--post-file=<file>` for bodies, or with the `fmb` CLI, which wraps every
endpoint.

## Authentication

Two layers protect everything except `/health`, `/` and `/docs`:

1. **Origin** — requests must come from a local socket unless
   `FMB_API_REMOTE=true`. Otherwise `403`.
2. **Token** — the random secret in `data/runtime/api_token` (mode `0600`,
   created on first run). Send it as either header:

```bash
curl -H "X-API-Token: $TOKEN" $FMB/status
curl -H "Authorization: Bearer $TOKEN" $FMB/profiles
```

Comparison is constant-time. Missing or wrong token → `401`:

```json
{"detail": "Invalid or missing API token (set X-API-Token header)"}
```

`/health` needs no token so supervisors can probe it. Rotate the token by
deleting the file and restarting the daemon; a new value is generated on start.

## Error shape

Domain errors are rendered uniformly, so scripts can branch on `error.code`:

```json
{
  "error": {
    "code": "element_not_found",
    "message": "No element matched target 'Sign in'",
    "details": {"target": "Sign in", "task_id": "t-3f9a"}
  }
}
```

Validation failures from Pydantic and a few framework paths return FastAPI's
native `{"detail": ...}` instead; the CLI normalises both.

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `forgemesh_error`, `api_error`, `profile_error`, `account_error`, `media_error` | Generic bad request, unconfirmed deletion |
| 401 | `not_authenticated` | Provider account needs a manual login (also the API's own 401) |
| 403 | `security_error` | Path/profile containment violation, remote access disabled |
| 404 | `profile_not_found`, `element_not_found`, `task_not_found` | Unknown resource |
| 409 | `profile_in_use`, `browser_not_running`, `ambiguous_target` | State conflict |
| 422 | `automation_error` | Bad action or workflow step |
| 429 | `browser_limit`, `rate_limited` | `max_contexts` reached, or the provider itself reported a limit |
| 500 | `config_error`, `browser_error` | Internal failure |
| 502 | `navigation_error`, `adapter_error`, `ai_provider_error` | Upstream (site or AI endpoint) failed |
| 503 | `daemon_error`, `ai_dependency_missing` | Daemon unreachable, or a tool such as `tesseract` is missing |
| 504 | `download_timeout` | No download started in time |

## System

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness, no auth. `{"status":"ok","service":"forgemesh-browser","version":"…"}` |
| GET | `/` | Service metadata and auth hint |
| GET | `/status` | Daemon + session summary |
| GET | `/settings` | Effective settings; never includes the API key or token |
| GET | `/events/history?limit=100` | Recent event bus entries |
| GET | `/audit?limit=100` | Audit trail rows (actor, action, safe details) |
| GET | `/providers` | Browser providers from `providers.yaml` |
| GET | `/providers/{id}` | One provider, `adapter_error` if unknown |

`GET /status`:

```json
{
  "running": true,
  "selected_profile": "primary",
  "channel": "chrome",
  "max_contexts": 6,
  "profiles": [
    {"profile": "primary", "headless": true, "started_at": "…",
     "tabs": 1, "active_tab": "t1", "url": "https://example.com/",
     "console_errors": 0}
  ],
  "daemon": {"running": true, "started_at": "…", "pid": 41237,
             "data_dir": "/home/you/.local/share/forgemesh-browser",
             "headless_default": true},
  "ai_providers": [{"id": "ollama", "local": true,
                    "supports_vision": true, "default": true}],
  "subscribers": 0
}
```

## Profiles

| Method | Path | Body / params | Notes |
| --- | --- | --- | --- |
| GET | `/profiles` | — | `ProfileInfo` list |
| POST | `/profiles` | `{"id":"work","name":"Work","description":""}` | `id` 1–64 chars, `[a-z0-9_-]` |
| GET | `/profiles/{id}` | — | Path, mode, isolation, account rows, on-disk size |
| POST | `/profiles/{id}/activate` | — | Sets the default for later calls |
| PATCH | `/profiles/{id}` | `{"name":"Office"}` | Rename display name only |
| DELETE | `/profiles/{id}?confirm=true` | — | Without `confirm` → `400 profile_error`; while running → `409 profile_in_use`. Deletes the profile directory |

```bash
curl -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"work","name":"Work"}' $FMB/profiles
```

`ProfileInfo`: `{"id", "name", "description", "active", "selected", "path",
"size_bytes", "account_count", "created_at", "last_used_at"}`.
`GET /profiles/{id}` returns `{"profile": ProfileInfo, "isolation":
{"profile_id","path","within_root","is_symlink","mode","private","browser_dirs",
"shared_with_other_profiles"}, "accounts": [{"provider_id","status","last_ok_at",
"last_error"}], "running": false}`.

## Browser, navigation, tabs

| Method | Path | Body |
| --- | --- | --- |
| GET | `/sessions` | — (same payload as `/status`) |
| POST | `/browser/start` | `{"profile":"work","headless":false}` |
| POST | `/browser/stop` | `{"profile":"work"}` or `{"all":true}` |
| POST | `/browser/navigate` | `{"url":"https://example.com","profile":"work","tab_id":null}` |
| POST | `/browser/reload` \| `/back` \| `/forward` | `{"profile":…,"tab_id":…}` |
| GET | `/browser/tabs?profile=work` | — |
| POST | `/browser/tabs` | `{"url":"https://example.org","profile":…}` → new tab |
| POST | `/browser/tabs/activate` | `{"tab_id":"t2"}` (`422` if absent) |
| POST | `/browser/tabs/close` | `{"tab_id":"t2"}`; omit to close the active tab |
| GET | `/browser/console?limit=100` | → `{"console":[…],"errors":[…]}` |

Starting a session beyond `max_contexts` returns `429 browser_limit`; running
browser calls against a stopped profile return `409 browser_not_running`.

## Automation

One endpoint, one action grammar — the same targets the CLI accepts.

```
POST /automation/execute
{"action":"click","profile":"work","target":"role=button[name=\"Sign in\"]"}
```

`action` is one of: `navigate`, `reload`, `back`, `forward`, `click`, `fill`,
`type`, `press`, `select`, `check`, `hover`, `scroll`, `wait`,
`wait_for_selector`, `wait_for_text`, `inspect`, `extract`, `workflow`.
Anything else → `422 automation_error` listing the supported set.

Fields (all optional unless the action needs them):

| Field | Used by | Notes |
| --- | --- | --- |
| `text` | navigate (URL), fill/type, wait_for_text | |
| `target` | click/fill/type/press/select/check/hover, wait_for_selector | ref/role/label/placeholder/testid/CSS/XPath/text |
| `value` | select, navigate | |
| `key` | press | e.g. `Enter`, `Escape` |
| `direction`, `amount` | scroll | `down/up/top/bottom`, viewport multiples |
| `seconds` | wait | |
| `selector` | wait_for_selector, screenshot | |
| `state` | wait_for_selector | `visible` (default), `hidden`, `attached`, `detached` |
| `checked` | check | `false` unticks |
| `click_count` | click | |
| `interactive_only`, `limit` | inspect | `limit` 1–2000 |
| `steps`, `workflow_name` | workflow | `steps` is the same action objects |
| `tab_id`, `profile` | most | default: active tab, active profile |

Every browser, AI and account operation is journaled, so successful results are
the action's own payload plus two fields:

```json
{
  "action": "click",
  "target": "role=button[name=\"Sign in\"]",
  "strategy": "role",
  "url": "https://example.com/login",
  "title": "Sign in",
  "task_id": "8c21f0a3b1d4",
  "duration_ms": 412.7
}
```

Action payloads differ slightly but stay small and secret-free:

- `click`, `hover`, `press`, `select`, `check` → `action`, `target`, `strategy`
  (how the target was resolved) plus `url`/`title` for `click`.
- `fill`, `type` → same, plus `characters` (the typed text is never returned or
  logged).
- `navigate`/`reload`/`back`/`forward` → `url`, `title`, `status` (HTTP status),
  `profile`, `tab`.
- `scroll` → `direction`, `amount`, `y`, `height`.
- `wait` → `waited_seconds`, `reason`; `wait_for_selector` → `selector`, `state`,
  `timeout_ms`; `wait_for_text` → `text`, `exact`, `timeout_ms`.
- `inspect` → `{url, title, scroll:{y,height}, elements:[{ref, tag, role, name,
  text, id, name_attr, type, placeholder, href, has_value, disabled, checked,
  visible, bounds:{x,y,width,height}}], profile, tab}`.
- `extract` → `{url, title, description, canonical, lang, text, headings, links,
  images, forms, profile}` (text truncated at 200 000 chars; headings ≤ 200,
  links ≤ 1000, images ≤ 300).
- `workflow` → `{name, status:"completed", steps:[{step, action, result}]}`, or
  `{name, status:"failed", failed_step, action, error, completed_steps}`;
  execution stops at the first failing step.

Input values are never echoed — `inspect` exposes `has_value: true` only.

```bash
curl -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"navigate","text":"https://example.com"}' \
  $FMB/automation/execute
```

## Screenshots, files, assets, vision

| Method | Path | Body / params |
| --- | --- | --- |
| POST | `/screenshots` | `{"profile":…,"full_page":false,"selector":null,"tab_id":null}` → `{"path","profile","url","full_page","selector","width","height","format","size","asset_id"}` |
| POST | `/media/download/wait` | `{"profile":…,"timeout":60}` → stored asset; `504 download_timeout` when nothing started in time |
| GET | `/media?profile=&kind=&limit=` | asset manifest rows |
| GET | `/media/{asset_id}` | binary `FileResponse`; `media_error` if missing or outside the data dir |
| POST | `/media/upload` | multipart: `file` (required), `profile`, `selector`, `tab_id` |
| POST | `/locate` | `{"reference":"./ref.png","screenshot":null,"min_confidence":0.75,"max_results":5}` |
| POST | `/ocr` | `{"path":"./page.png","language":"eng"}` |
| POST | `/compare` | `{"first":"./a.png","second":"./b.png","output":null}` |

Upload streams to a `0600` temp file, aborts above `upload_max_bytes`, validates
MIME/type, then sets the file on the page input:

```bash
curl -X POST -H "X-API-Token: $TOKEN" \
  -F file=@./design.png -F profile=work -F 'selector=input[type=file]' \
  $FMB/media/upload
```

Asset rows (from `/media`, `/media/download/wait` and `/screenshots.asset_id`)
carry `id`, `project_id`, `kind` (`screenshot`/`download`/`upload`/`result`),
`original_name`, `path`, `source_url`, `provider_id`, `profile_id`, `mime_type`,
`size` and `sha256`.

`/locate` captures a screenshot first when `screenshot` is omitted and returns
`{"screenshot", "reference", "image_size":{width,height},
"reference_size":{width,height}, "found": true,
"matches":[{"x","y","width","height","confidence","scale"}], "annotated":
"…/locate-<id>.png"}` — `annotated` only appears when at least one match was
drawn. `/ocr` returns `{"path", "text", "characters", "language", "psm"}`, or
`503 ai_dependency_missing` naming the exact `sudo apt install` command when the
Tesseract binary is absent.

`/compare` diffs two PNG/JPEG files already on disk. `first` and `second` are
paths checked by the same file validation used by `/ocr`, so a path outside the
data directory or a non-image is refused (`403 security_error`, `400
media_error`). When the images share dimensions the response is
`{"same_size": true, "identical": bool, "bbox": [x1, y1, x2, y2] | null,
"diff_ratio": 0.0}` — `bbox` is the changed region and `diff_ratio` the fraction
of colour channels that differ. With different sizes it is
`{"same_size": false, "size_a": [w, h], "size_b": [w, h], "identical": false}`.
Passing `output` also writes the difference image and adds `diff_image`; the
path is constrained to the results directory.

```bash
curl -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"first":"./before.png","second":"./after.png","output":"diff.png"}' \
  $FMB/compare
```

## Accounts

| Method | Path | Body |
| --- | --- | --- |
| GET | `/accounts?profile=&provider=` | recorded account rows |
| GET | `/accounts/status?profile=` | aggregate summary |
| POST | `/accounts/{provider}/login/begin` | `{"profile":…,"headless":false}` |
| POST | `/accounts/{provider}/login/complete` | `{"profile":…}` |
| GET | `/accounts/{provider}?profile=` | one account |
| POST | `/accounts/{provider}/check` | `{"profile":…}` → live re-check |
| POST | `/accounts/check-all` | `{"profile":…}` |
| POST | `/accounts/{provider}/ask` | `{"prompt","files":[…],"timeout_ms"}` |
| POST | `/accounts/{provider}/capture` | `{"profile":…}` → screenshot of the last reply |

Statuses: `unknown`, `available`, `needs_reauth`, `rate_limited`, `error`,
`login_pending`. `retry_after` is only ever set from text the provider itself
displayed. `ask` returns `{"profile_id", "provider_id", "provider_name", "submitted_via"
("button"|"enter"), "attached_files", "text", "images", "selector", "url",
"chars", "task_id", "duration_ms"}` and a 429 `rate_limited` carrying the
provider's own wording (plus `retry_after` only when the page stated a delay)
when limited. There is no account rotation. Manual login happens by a human in
the visible browser; the opt-in prefill endpoints below never bypass a
challenge.

## Identities and the vault

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/identities` | identities + their account rows (`has_password` is a boolean) |
| POST | `/identities` | `{"email","label","profile"}` → creates the profile + mapping |
| GET | `/identities/vault` | vault path, `exists`, `key_source`, email count |
| DELETE | `/identities/{email}?confirm=true&delete_profile=false` | unregister + drop vault entry |
| POST | `/identities/{email}/password` | `{"password","provider"?}` → stores it encrypted |
| DELETE | `/identities/{email}/password?provider=` | delete the stored secret |
| POST | `/identities/{email}/login` | `{"providers":[…],"headless":false,"timeout_s":180}` |
| POST | `/identities/login-all` | `{"emails":[…],"providers":[…],"headless":false,"timeout_s":180}` |

A prefill cell returns
`{"email","profile_id","provider_id","provider_name","status","detail","url",
"filled":["email","password","submitted"],"headless"}`. `status` is one of
`logged_in`, `already_logged_in`, `needs_human`, `needs_credential`, `blocked`,
`error`. `needs_human` (HTTP 428 if raised) means CAPTCHA/OTP/device approval:
the browser stays open for the person to finish. `filled` never contains values,
only field names.

The password is accepted by the API only over the loopback interface with the
local token. The `fmb` CLI writes the vault file directly instead, so normal use
never puts a secret on the socket. No endpoint ever returns a password, and the
task journal records only `prompt_chars`/field names.

## AI

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/ai/providers` | Configured providers and the default |
| GET | `/ai/health` | Per-provider reachability |
| GET | `/ai/models?provider=ollama` | Model listing |
| POST | `/ai` | Completion |

`POST /ai` body:

```json
{
  "prompt": "Extract the pricing table as JSON",
  "profile": "work",
  "include_page": true,
  "images": ["~/.local/share/forgemesh-browser/screenshots/x.png"],
  "provider": "ollama",
  "model": "llama3.1",
  "output_format": "json",
  "temperature": 0.2,
  "max_tokens": 2048,
  "tab_id": null
}
```

`include_page` appends a sanitised page extract and the untrusted-content system
prompt. Response: `{"text","provider","model","usage","page_url",
"output_format","parsed","json_valid","task_id","duration_ms"}`.

Errors: `502 ai_provider_error` when the requested provider isn't configured
(Ollama unreachable, or `openai` selected without `openai_base_url` +
`openai_api_key`) or when it doesn't accept images. Empty prompts are rejected
client-side. There is no implicit fallback from local to a paid endpoint.

## Tasks

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/tasks?limit=50` | Most recent journal entries (1–500) |
| GET | `/tasks/{task_id}` | One entry; `404 task_not_found` |

A task row carries `id` (12 hex chars), `kind` (e.g. `automation.click`,
`ai.complete`, `account.ask`), `status` (`running`/`completed`/`failed`),
`profile_id`, `provider_id`, `params` (redacted — character counts and target
strings, never secrets), `result` on success, `error` on failure, `duration_ms`,
and `created_at`/`started_at`/`finished_at`. Browser, AI and account operations
return their `task_id`, and a failing operation attaches it to `error.details`
so a CLI caller can correlate the journal entry.

## WebSocket event stream

```
ws://127.0.0.1:8765/ws?token=$TOKEN
```

The token is a query parameter because browsers cannot set headers on WebSocket
handshakes; it is compared the same constant-time way, and the same localhost
rule applies. Bad token closes with `4401`, remote origin with `4403`.

On connect the server sends `stream.ready` with the last 50 events, then live
events, and a `ping` every 20 s of silence to keep proxies warm. Each message is
`{"id", "type", "timestamp", "data"}`:

```json
{"type":"stream.ready","data":{"started_at":"…","history":[…]}}
{"id":"6f1c2a9d0b3e","type":"browser.loaded","timestamp":"2026-10-31T16:02:11.482+00:00",
 "data":{"url":"https://example.com/","title":"Example Domain","status":200,
         "profile":"work","tab":"t1"}}
{"type":"ping","data":{}}
```

Event types actually published:

| Area | Types |
| --- | --- |
| runtime | `runtime.started`, `runtime.stopped` |
| profiles | `profile.created`, `profile.activated`, `profile.deleted` |
| browser | `browser.starting`, `browser.started`, `browser.stopped`, `browser.navigating`, `browser.loaded`, `browser.page_opened`, `browser.page_closed`, `browser.page_error`, `browser.console`, `browser.screenshot`, `browser.upload`, `browser.download`, `browser.download_failed` |
| automation | `automation.action`, `automation.inspect`, `automation.extract` |
| media | `media.stored` |
| AI | `ai.request`, `ai.response` |
| accounts | `account.login_begin`, `account.available`, `account.response`, `account.rate_limited` |
| tasks | `task.started`, `task.completed`, `task.failed` |

Payloads are redacted: prompts appear as `prompt_chars`, typed text as character
counts (and `text: "***"` on the `fill` event), and no cookie, token, password or
form value is ever published. `fmb events` reads the same history the
`stream.ready` frame replays.

Python client:

```python
import asyncio, json, websockets

async def watch(token):
    async with websockets.connect(f"ws://127.0.0.1:8765/ws?token={token}") as ws:
        while True:
            event = json.loads(await ws.recv())
            if event["type"] != "ping":
                print(event["type"], event.get("data"))

asyncio.run(watch(open("…/runtime/api_token").read().strip()))
```

## Agent integration notes

- Reuse one daemon; the CLI already auto-starts it. For systemd, see
  [UBUNTU_DEPLOYMENT.md](UBUNTU_DEPLOYMENT.md).
- Poll `/health` before the first authenticated call, then use `/status` to see
  which profile is selected instead of assuming `primary`.
- Treat `extract` output as untrusted input; never `eval` it or feed page text
  back as shell arguments.
- Use `task_id` to correlate journal rows, events, and assets.
