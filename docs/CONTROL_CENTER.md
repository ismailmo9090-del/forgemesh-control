# ForgeMesh Control Center

The central-server half of this repository: a FastAPI service (`forgemesh_control`,
port **8787**) with a React dashboard and an Electron shell, that manages AI
provider accounts, the two central coding CLIs, the AI council, projects,
workspaces, workers and delivery. It talks to ForgeMesh Browser **only** over its
HTTP/WS API (`fmb serve`, port 8765) and to the CLIs only through their own
documented commands. It never drives a browser itself and never stores a
credential of its own.

Run it with `fmc serve`, or install the desktop shell with
`scripts/install_desktop.sh`, which writes `~/.local/bin/forgemesh-control-center`
and a `.desktop` entry that start Electron against this checkout. The dashboard
is loopback-only and every mutating route needs the control token from
`<data-dir>/runtime/control_token` (mode `0600`), sent as `X-API-Token` or the
`fmc_token` cookie.

## 1. UI reference image

[`reference/forgemesh-ui-reference.png`](reference/forgemesh-ui-reference.png)
is the single composite reference required by the brief: 20 screens on one
board, one light theme (white/off-white surfaces, charcoal type, indigo accent,
green/amber/red status).

**It is an AI-generated design reference, not a screenshot of this app.** The
screens that exist today are the ones listed as verified in section 3; the rest
of the board shows the intended visual language for screens that are still
behind the older dashboard layout.

## 2. What is real here

| Surface | Backend it actually calls |
| --- | --- |
| Settings → AI Accounts (provider cards, add, login, verify, active, rename, logout, remove) | `/api/accounts/*` → `AccountManager` → ForgeMesh Browser HTTP API (isolated profile per account) |
| GUI login window | Browser runtime opens the provider's official login page in that account's own profile; the dashboard only records the outcome |
| Settings → Coding Agents (detect, verify, install, update, reinstall, models, accounts, logs) | `/api/agents/*` → `CentralAgentRegistry` → the CLI's own commands |
| Embedded sign-in terminal | `/api/agents/{kind}/auth/*` → the CLI's documented `login` command hosted in a POSIX PTY |
| AI Council (members, meetings, rounds, decision) | `/api/council/*` → the browser runtime's provider sessions, account-aware |
| Projects, tasks, workspaces, delivery, workers | `/api/projects`, `/api/tasks`, `/api/workspaces`, `/api/delivery`, `/api/workers` |

Nothing on these pages is simulated: a row is green only because a real command
returned, and an unavailable runtime is reported as unavailable.

## 3. First-run results measured on this machine (2026-11-01)

Host: `mohd-ismail-Latitude-5400`, Ubuntu 26.04.1 LTS x86-64, Python 3.14.4 in
`.venv`, Chrome-based ForgeMesh Browser runtime reachable on 8765.

A throwaway instance was started against an empty data directory
(`FMC_DATA_DIR=/tmp/fmc-wizard-demo/data`, port 8791) so the numbers below are a
true first run, not the day-to-day database:

| Wizard step | Result as reported by the server |
| --- | --- |
| 1 Welcome / 6 Configure central server | done - the dashboard authenticated with the freshly generated token |
| 2 Check system compatibility | done - 2 coding agents checked on linux |
| 3 Install required components | done - ForgeMesh Browser runtime reachable, so provider logins can open a real window |
| 4 Install OpenCode CLI | installed, version 1.19.5, functional check passed in 6.1 s (`opencode/beam-free`, real prompt returned the verify token) |
| 5 Install Qoder CLI | installed, version 1.1.81, authenticated, functional check passed in 16.8 s (real prompt returned the verify token) |
| 7 Connect AI provider accounts | open, optional - 0 accounts registered in the throwaway database |
| 8 Authenticate coding agents | open - OpenCode reported `unauthenticated`; installing a CLI is not a login |
| 9 Verify connections | done - 2 of 2 agents answered an availability check, 0 provider sessions verified |
| 10 Finish setup | shows the still-open steps by name instead of claiming completion |

Real interactions beyond the wizard, all observed rather than asserted:

- The hosted OpenCode sign-in terminal rendered the CLI's own provider picker
  (`Add credential → Select provider`), keyboard buttons moved the real
  selection, and cancelling produced
  `status=cancelled, exit_code=-15, detail="[ForgeMesh sent SIGTERM to
  /home/mohd-ismail/.opencode/bin/opencode providers login]"` (session
  `ath_abb1fc353ab4`). No credential was typed and no provider was selected.
- Qoder's **Logout** button refuses instead of pretending: *"Qoder CLI has no
  logout command. An isolated account can be used, made active or removed from
  ForgeMesh, but its stored session stays in its own config directory until
  Qoder itself expires it."*
- Two real human-completed Qoder device-flow logins in an earlier session
  (`ath_820416e99b09`, `ath_a8b2b9dea217`, both exit 0, CLI answered *"Login
  successful!"*) left the `default` account signed in and active.
- A DeepSeek browser account went through create → verify (which failed
  honestly with the runtime's own reason) → remove, and its browser profile was
  purged with it.

### Defect found and fixed during this run

`qodercli` 1.1.81 exits **42** with
`sdk_worker_removed: SDK worker sessions are no longer supported` when it
inherits the SDK variables (`QODER_AGENT_SDK_*`, `QODER_SDK_AUTH_PAYLOAD_FILE`,
`QODER_CONFIG_DIR`, …) of a parent agent session that launched the control
server. Every Qoder run from such a server failed.
`agent_env()` in `src/forgemesh_control/agents/base.py` now strips those
variables for every child process (CLI runs, hosted logins, installer, worker
client). Verified end-to-end after the fix: the same functional check returned
`{"verified": true, "detail": "Real prompt returned FORGEMESH_VERIFY_OK"}` and
the agent moved from `limited` to `available` with `last_error: null`.

### Later the same evening, on the day-to-day instance

The live server (default data directory, port 8787) was restarted to pick up the
fix and re-verified against the real CLIs:

| Agent | Result |
| --- | --- |
| Qoder CLI 1.1.81 | `available` - functional check passed in 9.1 s, real prompt returned the verify token, `auth_status: authenticated` |
| OpenCode CLI 1.19.5 | `limited` - functional check reported `Timed out after 240s` |

The OpenCode timeout is **not** a ForgeMesh condition: the identical
`~/.opencode/bin/opencode run --format json -m opencode/<free-model> "…"`
command answered in 5.9 s at 21:35 and hung with no output at all from 21:50
onward, for three different free models, while `https://opencode.ai/zen/v1/models`
and general internet access were returning `200`/`204` in the same minute. The
process sat in `ep_poll` with no child. The dashboard shows the CLI's own
timeout rather than a pass, which is the behaviour the brief asks for; the card
goes green as soon as the CLI answers again (`Verify installation`).

### The OpenCode timeout explained, and a third defect

Later the same night the cause was measured rather than inferred. With
`opencode providers list` reporting **0 credentials**, the documented
`opencode run --print-logs --log-level ERROR` flags put the provider's own
answer on the pipe:

```
level=ERROR … error.error="AI_APICallError: Free usage limit reached.
Free models work in OpenCode (https://opencode.ai); subscribe to OpenCode Go…"
```

and the same free model, minutes earlier, had replied `FORGEMESH_FREE_OK` to a
real prompt with `cost: 0` and no credential stored. So: **OpenCode does not
need a sign-up to work** - what it needs is free-tier headroom, and the CLI
reports that only on the log stream, never as a JSON event, then waits
indefinitely. Three consequences were fixed:

1. **Sign-in is no longer demanded of a CLI that works without one.**
   `auth_support()` now carries `required_for_basic_use` (OpenCode `false`,
   Qoder `true`, base default `true`), the registry stores it as
   `metadata.auth_required_for_use`, and `setup_status()` splits
   `auth_needed` from `auth_optional`. The wizard step reads *"OpenCode CLI
   answers real prompts with no credential stored, so signing in there is
   optional."* and both the Coding Agents card and the AI Agents & Models card
   show a neutral `sign-in optional` chip instead of a warning.
2. **A throttled check no longer sits for four minutes.** The functional check
   uses the documented `--print-logs --log-level ERROR` flags, keeps the CLI's
   own error line as the reported reason (never a guessed cause, never its log
   files), and stops the run as soon as that line arrives - `stream_process`
   gained an `early_exit` predicate. A throttled OpenCode check now finishes in
   seconds and says *"Free usage limit reached…"*, which is the CLI's sentence,
   not ForgeMesh's.
3. **Aborting a run actually needs a process-group kill.** `process.wait()`
   returns only once the output pipe is closed, and an aborted CLI leaves a
   helper holding it, so a terminate looked like it had no effect (a 30 s
   `sleep` in the place of the helper decided the timing, not the terminate).
   Children are now started in their own session and the abort signals the whole
   group: terminate, then force after 1.5 s. The same fix bounds the hard
   timeout path.

Measured on the live instance right after those changes: OpenCode 1.19.5
`available`, real prompt returned the verify token in **3.6 s** with
`auth_status: unauthenticated`; Qoder 1.1.81 `available` in **6.3 s** with
`auth_status: authenticated`; `setup/status` reported `state: ready`,
`auth_needed: []`, `auth_optional: ["opencode"]`, `missing_capabilities: []`.

### Second defect found by looking at the running pages

`frontend/index.html` still carried `<meta name="color-scheme" content="dark">`
from the previous theme. Chromium then painted every *unstyled* form control
with its dark palette - the worker-pairing input rendered as a charcoal box with
charcoal text, unreadable, while the rest of the page stayed white. The meta tag
is now `content="light only"` (plus a matching `theme-color`), and
`frontend/src/styles.css` gives bare `input / select / textarea` an explicit
light background so a page that forgets to style a control cannot regress to the
user-agent palette.

Re-checked after rebuilding: every navigation page (Overview, Projects, Problem
Statements, AI Council, Worker Nodes, AI Agents & Models, Task Queue, Browser
Automation, Terminal & Logs, Settings) was walked in the running app and each
one reported no dark `input`/`select`/`textarea`. Screenshots of the Overview,
Settings, AI Accounts, Coding Agents, the first-run wizard and the hosted
sign-in terminal were taken from the live app and are light throughout.

### The same defect inside a real task run

Running a real approved task per agent through the dashboard's own endpoints
showed the timeout was not limited to the availability check:

| Agent | Task | Result |
| --- | --- | --- |
| Qoder CLI 1.1.81 | `tsk_085a133778` | `completed` - `exit=0 ok=True duration=18041ms`, committed, artifact `FM_QODER_PROBE` written in the workspace |
| OpenCode CLI 1.19.5 | `tsk_3f3624458a` | stuck at `running`, progress 10, no log line after the workspace was created |

The OpenCode task had no model selected, so `submit_task` sent no `-m`. Measured
directly: `opencode run --format json --dir <new workspace> "<prompt>"` with no
`-m` chooses its own default, which here is `deepseek-v4.1-flash-free` - the
throttled one - and it emits `AI_APICallError: Free usage limit reached` only
under `--print-logs`, retries silently and never exits. The scheduler would have
sat on it for the full `agent_timeout_seconds` (3600). Two fixes:

* `OpenCodeAgent.submit_task` now falls back to the same free-model preference
  the availability check uses when the task selects no model, and the adapter
  reports the model that really ran on `AgentRunResult.model`, which the
  scheduler writes to the task log as `Model used: ...`.
* the run shares `--print-logs --log-level ERROR`, the refusal scan and the
  early stop with the availability check, so a throttled or refused run reports
  the CLI's own sentence instead of a timeout.

Re-run after the fix (`tsk_304e7bc960`): answered in 12.0 s and failed honestly.
The CLI's event stream said `permission requested: external_directory (...);
auto-rejecting` and `The user rejected permission to use this specific tool
call.` - OpenCode asks for permission to touch anything outside `--dir`, and with
no terminal to answer in it rejects itself. `--auto` would override that, but the
vendor labels it `(dangerous!)` and ForgeMesh never auto-approves a write, so it
is not used; the run's text stream was empty, so the task used to read `failed`
with no reason at all. `submit_task` now also reads `state.error` off a failed
`tool` part, so the dashboard shows the CLI's own refusal as the task error.

### A fourth defect: one long event line killed the run

The next attempt (`tsk_d36b9d7442`) answered in 18 s and failed with
`ValueError: Separator is found, but chunk is longer than limit`. That is not the
CLI's fault: OpenCode writes one JSON event per line and a single tool result was
bigger than asyncio's 64 KiB `StreamReader` limit, so `readline()` raised inside
the pump and ForgeMesh threw away an otherwise healthy run. `stream_process`
now starts the subprocess with `limit=STREAM_LINE_LIMIT_BYTES` (32 MiB), and
`test_stream_process_reads_a_line_beyond_the_default_buffer` feeds a 200 000-byte
single line through it.

The same run also showed the boundary working as intended: the model tried
`ls`/`find` across `~/.local/share/forgemesh-control/` - other projects'
workspaces included - and OpenCode answered
`permission requested: external_directory (...); auto-rejecting`. The agent is
confined to its own workspace by the CLI itself, not by a ForgeMesh guess.

### Both agents, one real approved task each

After the buffer fix, the same probe (`/api/projects` -> blueprint and UI spec ->
three approvals -> `/api/tasks` -> `/api/tasks/{id}/start`) completed on both
CLIs with real files on disk:

| Agent | Task | Result |
| --- | --- | --- |
| Qoder CLI 1.1.81 | `tsk_085a133778` | `completed` - `exit=0 ok=True duration=18041ms`, commit `b86fae6e`, artifact `FM_QODER_PROBE` |
| OpenCode CLI 1.19.5 | `tsk_6f262deecb` | `completed` - `exit=0 ok=True duration=12319ms`, commit `c6b4abeb`, artifact `FM_OPENCODE_PROBE`, task log line `Model used: opencode/beam-free` |

Both marker files were read back from the workspace before the probe projects
were deleted with `confirm=true` and their workspace directories removed, so no
test data is left in the dashboard or under
`~/.local/share/forgemesh-control/workspaces/`.

### Council: three separate failures, three fixes

A live meeting showed ChatGPT returning `500 Internal Server Error` on every
round and DeepSeek answering with text that was not parseable as JSON. They had
different causes:

- **ChatGPT 500 - dead Playwright context.** `BrowserManager` kept `_profiles`
  entries after a Chrome window/context died, so `_page_for()` called
  `new_page()` on a closed context and the `TargetClosedError` escaped as a 500.
  The manager now checks `browser.is_connected()` (plus a `context.pages`
  fallback), listens for `context.close` / `browser.disconnected` to drop the
  profile, terminates the orphaned process, and rebuilds the profile exactly
  once when a tab is requested. `fmb account ask` after closing the context
  manually now answers instead of raising.
- **ChatGPT - stale selectors.** Even with a healthy context, chatgpt.com's
  current rollout has no `#prompt-textarea`, no `data-testid='send-button'` and
  no `data-message-author-role`; the adapter waited 180 s for a message element
  that no longer exists. The adapter now also matches
  `[contenteditable='true'][role='textbox']`, `button[aria-label='Send']` /
  `['Stop']` and
  `[data-markdown-text-style='assistant-message']`, keeping the old selectors
  first for older rollouts. A live ask returned 2 454 chars, 12/12 JSON items,
  submitted via the send button.
- **DeepSeek - truncated JSON.** `collect_response` treated two seconds of
  unchanged text as "finished", but a streaming JSON answer can pause that long
  mid-object. If the stable text looks like an unfinished JSON document, the
  collector now waits up to 20 s more for it to close. A live ask returned
  1 450 chars and parsed as 12/12 items. When a reply is still unparseable the
  council error now includes the first characters of the reply instead of only
  saying "not parseable".
- **Blueprint panel copy.** "No blueprint has been created from this meeting in
  this session" was static text; the panel now explains whether a project is
  missing or the decision simply has not been saved yet.

### Why the council did not review its own proposals

A later meeting (`mtg_c96976df8a`, three rounds) finished as
"completed" with four proposals and **zero critiques**, so the decision was
selected as an unreviewed draft. The cause was the round planner: when DeepSeek
failed the statements contract, the engine compared proposals (2) against
`required` (3 seats) and spent round 2 on a divergence retry instead. Round 3
was then the final round, so it became a synthesis - the critique round never
existed.

- **Reserved rounds.** In a normal meeting the planner now keeps two rounds in
  reserve (critique + synthesis) before it may spend a round on divergence.
  A three-round meeting therefore always runs statements -> critique ->
  synthesis; a four-round meeting may still diverge in round 2. Hackathon mode
  keeps its old budget because collecting statements is its goal.
  `test_a_silent_provider_does_not_cost_the_critique_round` covers the exact
  failure.
- **DeepSeek's lone `- 4`.** DeepSeek's statements answers were stored as
  `-\n4` / `-\n12`: while the real answer streams, the send button's icon
  carries `.ds-loading`, and a two-second lull in the markdown element was read
  as a finished answer. `.ds-loading` is now a stop selector, so the collector
  waits while DeepSeek is genuinely generating. A live ask then returned
  1 052 chars of JSON (2/2 statements) in 18.5 s.
- **Decision wording.** With several proposals but no critiques the decision
  used to say "X is the only proposal on the table"; it now reports how many
  proposals were submitted and that the first one is listed only because
  nothing was scored.

### Desktop shell, verified by launching it


`~/.local/bin/forgemesh-control-center` was started twice against the running
server. Both times the shell detected the healthy `/health` endpoint and entered
`attached` mode - it does not spawn or stop an external server, and
`shell:restart` refuses with *"This server is external; the shell does not own
its process."* With `FMC_DESKTOP_CAPTURE` set, it wrote a real PNG of the
dashboard window (`desktop/captures`-style capture path), which shows the
light-theme shell at full desktop resolution: icon rail, `FORGEMESH SERVER
ONLINE` pill, live CPU/RAM/Storage/Network sparklines, agent chips, council
provider tiles and the empty-state cards.

Splash-window capture does **not** work on this Wayland session - every stage
capture logged `capture failed for stageN-done: UnknownVizError`, while the main
window captured fine. That is a limitation of the capture aid, not of the app.

## 4. Test evidence

```
.venv/bin/python -m pytest -q -m "not integration"   # 271 passed
.venv/bin/python -m pytest -q -m integration         #  28 passed
.venv/bin/python -m ruff check src tests             # All checks passed!
npx --prefix frontend tsc -p frontend/tsconfig.json --noEmit && npm --prefix frontend run build
                                                      # clean, dist/assets/index-BaAFaq8W.js
```

- **Mocked (271)** - services and adapters with fake CLIs and a fake browser
  runtime. Each file's docstring states what is mocked. No provider response is
  invented in an integration test, and no mocked test is labelled as proof that
  a real login worked.
- **Integration (28)** - the real FastAPI app boots against a throwaway data
  directory with the browser runtime pointed at a closed port. These assert that
  the routes exist, that they demand the token, and that with no runtime they
  report *unknown / unreachable / failed* instead of simulating a council
  deliberation, an account session or a sign-in session.

## 5. Known limitations (not hidden by the UI)

- **One council seat per provider per meeting.** Multiple accounts of the same
  provider are managed and verifiable, but a meeting picks one of them.
- **OpenCode holds one credential per provider.** It has no documented way to
  keep two accounts for the same provider, so `multiple_accounts: false`,
  `account_isolation: none`, and switching means logging out first. The card
  says exactly this.
- **Qoder has no logout command.** Only removal of the ForgeMesh-side account.
- **Hosted sign-in needs a POSIX PTY** (Linux/macOS). Without one the panel
  falls back and says so. Windows support is **not claimed** - it has not been
  tested.
- **The Qoder device flow opens the system browser** because that is what
  `qodercli login` itself does; ForgeMesh does not intercept it.
- **The council needs `fmb serve` running.** A meeting with no reachable runtime
  fails with the actionable reason, and no proposal or decision is fabricated.
- **OpenCode's free models are the CLI's own service.** When that gateway stops
  answering, the card reports the CLI's timeout (see section 3) and the agent
  stays `limited` until a real prompt succeeds. ForgeMesh cannot substitute a
  response.
- **Desktop splash capture fails on Wayland** (`UnknownVizError`); the main
  window captures correctly, and the shell itself is unaffected.
- **Worker-laptop agent installation is out of scope** for this component; it is
  handled by another team member. This dashboard only installs on the central
  server, and says so on the page.
- **Screenshots of the dashboard are not part of the automated tests.** The
  visual checks in section 3 were taken by hand from the running app.

## 6. Security invariants the code is written to keep

- Cookies, tokens, passwords and one-time codes are never typed into, stored by
  or displayed in the dashboard; the browser profile keeps its own session and
  ForgeMesh records only whether it is signed in.
- PTY output is redacted before it is persisted, and an account row serialises
  without any cookie/token/secret field (covered by tests).
- Login, logout, account switching, destructive removal and agent execution are
  audited (`AuditService`) and emitted as events.
- Destructive and externally consequential actions require an explicit
  confirmation; unsafe git remotes and non-allowlisted commands are refused
  before execution.
- Filesystem access for agent runs is confined to the workspace roots; the
  control API rejects non-loopback clients unless `FMC_ALLOW_REMOTE=true` is set
  deliberately behind TLS.
