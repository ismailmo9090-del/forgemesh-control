# Profile System

A profile is a Chrome-style user-data directory that owns all browser state for
one identity: cookies, localStorage, IndexedDB, cache, extensions state and
download history. Profiles are how ForgeMesh keeps "work" and "personal"
logins, or one account per AI provider, separated without running several
browser processes by hand.

## Disk layout

```
~/.local/share/forgemesh-browser/        # data_dir (mode 0700)
├── profiles/
│   ├── primary/                         # one Chromium user-data-dir (0700)
│   │   ├── Default/                     # created lazily on first launch
│   │   ├── Local Storage/
│   │   ├── IndexedDB/
│   │   ├── Cookies
│   │   └── ...
│   └── work/
├── downloads/  screenshots/  assets/  results/  logs/   # 0700
├── runtime/
│   ├── api_token                        # 0600
│   └── daemon.json                      # pid, host, port, started_at
├── forgemesh.db                         # SQLite: profiles, accounts, assets, tasks, audit
└── state.json                           # 0600 — which profile is selected
```

Override the root with `--data-dir`, `FMB_DATA_DIR`, `XDG_DATA_HOME` or
`data_dir` in `settings.yaml`.

## Identity rules

Profile IDs match `^[a-z0-9][a-z0-9_-]{0,63}$`, are lowercased and trimmed on
input, and `profiles`, `runtime`, `.` and `..` are reserved so a profile can
never shadow an internal directory. A display name is separate and freely
renameable (`fmb profile rename work "Office"`); if omitted it is derived from
the ID (`dev-box` → `Dev Box`).

Paths are computed by `profiles/storage.py:profile_path()`, which refuses a
symlinked profile directory *before* resolving, then verifies containment inside
`data/profiles`. That ordering matters: resolving first would let a planted
symlink escape to any path the daemon can write to.

## Creation and deletion

```bash
fmb profile create work --name "Work" --description "Authorized work accounts"
fmb profile inspect work
fmb profile delete old --yes
```

`create` validates the ID, makes the `0700` directory, inserts the SQLite row,
audits the action and — when no profile was selected yet — marks it selected.
`ensure_default()` creates `primary` on first daemon start.

`delete` requires explicit confirmation, refuses while the profile's browser
context is running (`profile_in_use`, stop it first), removes the row and then
`shutil.rmtree`s the directory. If the deleted profile was selected, selection
moves to the first remaining profile. Deletion is irreversible: it removes
logged-in sessions, so re-authentication is required for a new profile.

## Isolation guarantees

`inspect_isolation()` produces the report shown by `fmb profile inspect`:

| Field | Meaning |
| --- | --- |
| `within_root` | path stays under `data/profiles` |
| `is_symlink` | directory is a symlink (must be `false`) |
| `mode` | octal permissions, expected `0o700` |
| `private` | owner-readable **and** no group/other bits (`mode & 0o077 == 0`) |
| `browser_dirs` | which Chrome state markers exist (`Default`, `Local Storage`, `IndexedDB`, `Cache`, `Cookies`, `Network`, `Local State`) |
| `shared_with_other_profiles` | other profile directories that resolve into this one (must be empty) |

Enforcement points:

- One Playwright `launch_persistent_context` per profile; contexts never share a
  user-data-dir, so cookies and storage are physically separate.
- `max_contexts` (default 6) caps concurrent profiles; exceeding it raises
  `browser_limit` (429) instead of silently launching more Chromium processes.
- Files written from a profile (screenshots, downloads, captures) land in the
  data directory with mode `0600`.
- The daemon runs as your own user; there is no setuid helper and no shared
  system profile.

## Persistence and reuse

State is written to disk by Chromium as usual and survives daemon restarts, so
a session authenticated once is available to later headless runs. That is the
whole point of `account login` → `account complete`: a human logs in in a visible
window, and subsequent automation reuses the stored cookies.

Practical consequences:

- Cookies are only as private as the file permissions; keep `data_dir` on your
  own encrypted home and never chmod it world-readable.
- A session ends when the provider expires it. `fmb account check` re-detects
  the login state and records `needs_reauth` rather than pretending to work.
- Copying a profile directory to another machine moves the logged-in sessions
  with it. Treat `data/profiles/<id>` as secret material.

## Selection semantics

Two different flags exist and are easy to confuse:

- `active` — the row is enabled (soft-state in SQLite).
- `selected` — this profile is the default target for commands that don't pass
  `--profile` or a `profile` field. It is persisted in `state.json` via an
  atomic temp-file + `os.replace` write with mode `0600`.

`fmb profile activate work` changes selection only; it starts no browser. Every
API endpoint resolves `profile or runtime.profiles.current()`, so agents should
either pass `profile` explicitly or check `/status → selected_profile`.

## Multi-agent usage pattern

Give each agent (or each project) its own profile rather than sharing one:

```bash
fmb profile create agent_research --description "Research agent, provider: perplexity"
fmb --profile agent_research open https://example.com
fmb --json --profile agent_research extract
```

Benefits: per-agent rate limits stay attributable, a stale login only affects
one agent, and `fmb assets --profile agent_research` gives a clean audit trail.
When `max_contexts` is the binding constraint, raise it in `settings.yaml` —
each additional context costs roughly a Chrome process tree (see
[BENCHMARKS.md](BENCHMARKS.md)).
