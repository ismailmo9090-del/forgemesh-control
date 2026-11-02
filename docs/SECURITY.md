# Security Model

ForgeMesh drives real, logged-in browser sessions on your machine and exposes a
local HTTP API. The design assumption is that **the machine, the profiles and
the API token are the secret**, and that everything the browser sees — page
content, downloaded files, model output — is untrusted input.

## Threat surface

| Surface | Exposure | Primary control |
| --- | --- | --- |
| Local API | Any process owned by your user; remote hosts if you enable it | Token auth + localhost-only origin check |
| Browser profiles | Cookies and sessions of real accounts | Per-profile `0700` dirs, no shared user-data-dir, `max_contexts` |
| File output | Screenshots, downloads, assets, results | Containment to `data_dir`, `0600` files, SHA-256 manifest |
| Uploads | Arbitrary user-supplied paths | Symlink rejection, regular-file check, size cap, type allowlist, image verification |
| Page content | XSS-style prompt injection into the AI layer | Untrusted-content system prompt, never executed |
| Logs / journal | Secrets leaking into SQLite, events or CLI output | Redaction at every publish point |
| Providers | Quota evasion, ToS breach | No rotation, no CAPTCHA/MFA handling, no auto paid fallback |

## 1. Credentials: profiles by default, an encrypted vault on request

- **Default: no credential store.** Website authentication happens inside the
  browser profile, by you, in a visible window
  (`fmb account login chatgpt --profile work`). If you never run
  `fmb identity password`, the tool stores no password at all.
- **Opt-in vault.** `fmb identity password you@gmail.com` stores one encrypted
  blob at `data/secrets/vault.json` (`0600`, parent dir `0700`). It is
  AES-256-GCM; the 32-byte key comes from `config_dir/vault.key`
  (`0600`, created on first use) or from an scrypt-stretched
  `FMB_VAULT_PASSPHRASE` (`n=2^14, r=8, p=1`, per-vault random salt).
- The ciphertext contains the email→password map, so email addresses are not
  readable in the file either. Writes are atomic (`O_EXCL` temp file → `replace`)
  and serialised with `flock`, so the CLI and the daemon cannot corrupt each
  other. The key file and vault path are refused if they are symlinks.
- The password is decrypted only inside the daemon's prefill call
  (`accounts/login_runner.py`). It is never returned by the API, written to the
  task journal, published as an event, put in an audit row or printed. The audit
  entry records only `{email, provider}` for a password change.
- Worker agents and the ForgeMesh API never receive a password. They address an
  identity by email; only `login`/`login-all` cause a local prefill.
- Losing the key or passphrase loses the stored passwords. There is no recovery
  backdoor, no plaintext export and no password hint.
- Password fields render as `has_value: true` in `inspect`; the DOM snapshot
  never reads `value` from them, so nothing sensitive reaches the CLI, the
  journal or the event bus.
- The only other persisted secret is the random API token
  (`data/runtime/api_token`, mode `0600`, created with `O_CREAT|O_TRUNC, 0o600`).
- Provider API keys are read from settings/environment only; nothing writes them
  into the database, and `GET /settings` reports `openai_configured: true`
  instead of the value.

## 2. API authentication and origin

Every router except `/health`, `/` and `/docs` is mounted with
`[Depends(require_local), Depends(require_token)]` (`api/server.py`).

- `require_token` accepts `X-API-Token` or `Authorization: Bearer …` and compares
  with `hmac.compare_digest` — no timing oracle, no "first bytes" shortcut.
- `require_local` checks the socket peer against `127.0.0.1`, `::1`, `localhost`
  (and `testclient` for the test harness). `api_remote` defaults to `false`, so
  binding to `0.0.0.0` still cannot serve non-local peers until you flip it
  deliberately.
- The WebSocket handshake takes `?token=…` (browsers cannot set headers there),
  performs the same constant-time comparison, and closes with `4401` on a bad
  token and `4403` for a remote peer.
- The token file is not world-readable; a second process owned by another user
  cannot obtain it. On a shared machine, keep `data_dir` in your own home.

To expose the API beyond localhost, set `FMB_API_REMOTE=true` and put it behind
TLS yourself (reverse proxy with client auth). Do not run it on a public
interface with only the token as protection — the token is a single static
secret with no scope, expiry or rate limiting.

## 3. Path containment

`security/path_validation.py` is the only place path decisions are made:

| Function | Guarantee |
| --- | --- |
| `sanitize_filename` | takes the basename (so `../../x` → `x`), replaces control chars and `<>:"/\|?*`, rejects `.`/`..`, prefixes Windows reserved names, caps at 180 chars |
| `is_within` | resolved child must be under the resolved root |
| `safe_join` | joins untrusted parts under a base and raises `SecurityError` on escape |
| `validate_user_file` | rejects symlinks, requires a regular file, enforces `upload_max_bytes` |
| `ensure_output_path` | relative output paths are confined to the allowed base; traversal raises `SecurityError`; the filename is sanitized |

Asset storage additionally refuses to register any file that resolves outside
`data_dir`, and `GET /media/{asset_id}` re-checks containment before streaming a
`FileResponse`. `profile_path()` checks `is_symlink()` **before** `resolve()`,
because resolving first is what turns a planted symlink into an escape.

Tests cover each of these (`tests/unit/test_path_validation.py`,
`test_profiles.py::test_profile_layout_rejects_symlink`, `test_media.py`).

## 4. File and directory permissions

`ensure_private_dir` creates `0700` directories; `secure_file` sets `0600`;
`secure_tree` tightens a tree while skipping (not following) symlinked entries.
Applied to: the whole `data_dir` tree, profile directories, the API token,
`state.json` (written via temp-file + `os.replace`), daemon info, downloaded and
generated assets, screenshots, AI outputs, provider captures, and `audit.jsonl`.
The install script pre-creates the same layout so nothing is briefly
group-readable.

## 5. Untrusted web content

- `extract`/`inspect` return page text as data. The AI layer adds
  `PAGE_SYSTEM_PROMPT` and instructs the model that page content is untrusted,
  so embedded "instructions" are treated as text, not commands
  (`ai/prompt_manager.py`).
- Nothing executes page-derived strings. `workflow` steps come from a local YAML
  file, actions are validated against a fixed whitelist, and there is no
  `evaluate`/`exec`/shell action; an unknown action raises before anything runs.
- `normalize_url` accepts only `http`/`https` — no `file:`, `javascript:`,
  `data:` or custom schemes as navigation targets.
- Downloads are moved into the asset store (never left in a browser-chosen
  path), stored `0600`, and recorded with MIME type, size and SHA-256 so you can
  verify what actually arrived.
- Uploads are capped (`upload_max_bytes`), type-allowlisted (images, documents,
  archives — executables rejected), and images are decoded with
  `Image.verify()` first, so a corrupt or mislabelled file fails before it
  reaches the page.
- Do not upload to a site you are not authorised to use, and do not fetch
  private or protected resources without authorisation. The tool does not
  attempt to obtain them by bypassing access controls.

## 6. Provider limits and fair use

- Login state detection only *reads* whether you are signed in.
  `NotAuthenticated` tells you to log in manually.
- CAPTCHA, MFA, 2FA prompts and security challenges are **never** automated,
  solved, forwarded or circumvented. `account login` deliberately runs headed so
  a human can complete them. Prefill logins behave the same way: `identity
  login` fills the email/password fields, then reports `needs_human` when a
  CAPTCHA widget, one-time-code field or "verify it's you" text appears. It
  never submits an OTP, never approves a push and never retries a rejected
  password.
- The browser is launched as a regular Chrome process and Playwright attaches
  over CDP, so automation flags (`navigator.webdriver`, `HeadlessChrome` UA) do
  not make identity providers reject an otherwise human-driven sign-in. This is
  compatibility, not evasion: the user still types the password and completes
  MFA, and no CAPTCHA or security challenge is touched. `browser_stealth: false`
  disables the cosmetic patching used by the fallback launcher.
- Orphaned Chrome processes from a crashed daemon are reaped and stale profile
  locks cleared on startup, so a restart cannot inherit a half-dead session.
- `RateLimited` is raised only when the page contains provider-stated wording
  (`RESTRICTION_PHRASES`), and `retry_after` is parsed from that same text
  ("try again in 4 hours"). No reset time is ever inferred or invented.
- There is one account per (profile, provider) row and no rotation, no proxy
  switching, no fingerprint randomisation and no session-reset logic intended to
  evade quotas or account restrictions. Using more accounts to get around a
  limit is exactly what this tool refuses to do.
- No automatic escalation to a paid API. A missing/unreachable Ollama is an
  error (`ai_provider_error`), not a trigger to call `openai` — which is itself
  only registered when both `openai_base_url` and `openai_api_key` are set.

## 7. Destructive operations

Confirmation is required, and the act is audited:

```bash
fmb profile delete old --yes      # refuses while the profile is running
scripts/uninstall.sh              # interactive; --purge-data needs a second "DELETE"
```

`AuditLogger.record()` writes to SQLite **and** `logs/audit.jsonl` (`0600`) for
`profile.create/rename/activate/delete` and
`account.login_begin/login_complete/login_failed/ask/rate_limited`. Audit
failures are logged, never swallowed silently into a success. Details are
limited to ids, paths and states — no page content, no prompts.

## 8. Resource containment

- `max_contexts` (default 6) bounds concurrent Chromium user-data-dirs; exceeding
  it raises `browser_limit` (429) rather than forking more processes.
- Timeouts are enforced: navigation 45 s, actions 15 s, downloads `1–600 s`,
  AI `ai_timeout_seconds` (≥5 s), waits `0–600 s`.
- Payload bounds: 200 MB downloads, 100 MB uploads, 4 images per AI request at
  8 MB each (larger images are downscaled to 2048 px, never passed through raw),
  `limit` ≤ 2000 DOM elements, extract text ≤ 200 000 chars.
- The daemon is a single-user process; `scripts/benchmark.py` measures the real
  cost (~0.9 GB RSS per active profile on this machine — see
  [BENCHMARKS.md](BENCHMARKS.md)).

## 9. Failure handling

Nothing is ignored: `ForgeMeshError` subclasses carry a stable `code` and
`http_status`, the API renders them as `{"error":{code,message,details}}`, and
the task journal records `failed` with the message and `task_id` in `details`.
`uncaught` is not an option — the lifespan handler always stops the browser and
closes the database, and shutdown deletes the PID file only if it still names
this process.

## 10. Known limits

- No OS keyring integration: the API token and any `FMB_OPENAI_API_KEY` live in
  files/environment with `0600` protection, not in a hardware-backed store.
- No per-user API scopes, token expiry or rate limiting — it is a local,
  single-operator daemon by design.
- Chromium sandbox behaviour is the browser's own; ForgeMesh runs as your user
  and does not add privilege separation between profiles beyond file
  permissions.
- Encrypted-at-rest storage is inherited from the OS (home directory encryption,
  e.g. LUKS). The tool never claims to encrypt profiles itself.
- OCR depends on the system `tesseract` binary; missing it is a hard error, not
  a silent no-op.

## Reporting

Treat a leak of session cookies, the API token, or a way to make the daemon
execute arbitrary input as a security bug. Include the daemon log tail
(`fmb logs --lines 200`) **after** checking it for secrets, and redact tokens
before sharing anything publicly.
