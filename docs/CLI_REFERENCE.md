# CLI Reference

The entry point is `fmb` (installed in `.venv/bin/fmb`). Every command supports
`--help`.

## Global options

| Option | Meaning |
| --- | --- |
| `--json`, `-j` | Machine-readable JSON on stdout. Errors are still written as errors. |
| `--verbose`, `-v` | More diagnostic output. |
| `--quiet`, `-q` | Suppress informational messages. |
| `--profile`, `-p` | Profile for this command (default: active profile). |
| `--data-dir PATH` | Override the data directory (also via `FMB_DATA_DIR`). |
| `--version`, `-V` | Print the version. |

Global options go before the subcommand: `fmb --json -p work open https://example.com`.

The CLI starts the daemon automatically when a command needs it. `fmb stop`
never auto-starts the daemon. All commands are local-only unless
`FMB_API_REMOTE=true` is set deliberately.

## Profiles

```bash
fmb profile create work --name "Work" --description "Authorized work accounts"
fmb profile list
fmb profile current
fmb profile activate work
fmb profile inspect work          # path, permissions, isolation, accounts
fmb profile rename work "Office"
fmb profile delete old --yes      # irreversible; confirmation is required
```

IDs are lowercase letters, digits, `-` and `_` (max 64). The directory is
created with mode `0700` under `data/profiles/<id>`.

## Browser lifecycle and navigation

```bash
fmb start                  # headless by default
fmb start --headed         # visible window (manual login/debugging)
fmb stop                   # stop the selected profile
fmb stop --all
fmb status                 # daemon + profiles + running sessions
fmb open https://example.com
fmb open example.com --headed
fmb tabs
fmb new-tab https://example.org
fmb activate <tab-id>
fmb close-tab [--tab <tab-id>]
fmb reload | fmb back | fmb forward
fmb console --limit 200    # console messages and page errors
```

Accepted URLs are `http` and `https`. A bare host is completed with `https`,
except `localhost`/`127.0.0.1` which use `http`.

## Inspection and extraction

```bash
fmb inspect                       # interactive elements (default)
fmb inspect --all --limit 500     # include headings, paragraphs, images
fmb extract                       # JSON: text, headings, links, images, forms
fmb extract --output page.json
```

`inspect` assigns stable `data-fmb-ref` attributes and prints them. References
are valid until the page navigates or the DOM changes. Input values are never
printed: only `has_value: true/false` is exposed.

## Target grammar

Automation commands accept any of these targets, in priority order:

| Form | Example |
| --- | --- |
| DOM reference | `ref=e12` |
| Accessible role + name | `role=button[name="Sign in"]`, `role=textbox[name="Email"]` |
| Label | `label=Email address` |
| Placeholder | `placeholder=Search` |
| Test ID | `testid=submit` |
| CSS | `css=#email`, `#email`, `.primary` |
| XPath | `xpath=//button[@type="submit"]`, `//button` |
| Visible text | `text=Sign in`, `Sign in` |

Resolution fails with `element_not_found` when nothing matches and with
`ambiguous_target` when more than one element matches. The CLI escapes
non-ASCII text using HTML entities for `label=`, `placeholder=` and `text=`
forms automatically.

## Automation

```bash
fmb click "Sign in" --count 1
fmb fill "label=Email address" "user@example.com"
fmb type "#search" "query"          # key-by-key
fmb press Enter [--target "#search"]
fmb select "#pick" Beta
fmb check "#agree"                  # or --uncheck
fmb hover ".menu"
fmb scroll down --amount 1.5        # down, up, top, bottom
fmb wait 2
fmb wait-for --selector "#result" --state visible
fmb wait-for --text "Done"
```

Interactive actions support `--tab <id>`. Typed text is never echoed into the
event log; only the character count is recorded.

### Workflows

```bash
fmb workflow login.yaml --name "daily check"
```

```yaml
name: login
steps:
  - action: navigate
    text: https://example.com/login
  - action: fill
    target: label=Email
    text: user@example.com
  - action: fill
    target: label=Password
    text: ""                 # still do not put real passwords in files
  - action: click
    target: role=button[name="Sign in"]
  - action: wait_for_selector
    target: "#dashboard"
  - action: inspect
```

Allowed actions: `navigate`, `click`, `fill`, `type`, `press`, `select`,
`scroll`, `wait`, `wait_for_selector`, `wait_for_text`, `hover`, `check`,
`inspect`. Unknown actions are rejected before execution. A failing step stops
the workflow and reports the failed step and completed steps. There is no
script-evaluation action.

## Files and assets

```bash
fmb upload ./design.png [--selector "#file"]
fmb download --timeout 60 --output ./downloads/
fmb assets --kind download --limit 20
fmb asset <asset-id> --output ./restored.bin
```

Downloads are detected through Playwright's download event, moved into
`data/assets/<project>/<provider|general>/downloads/`, and recorded with MIME
type, size and SHA-256 in SQLite plus `assets/manifest.jsonl`.

`download` waits for the next download triggered by the page, then copies it to
`--output`; if the path ends with `/` or is an existing directory, the original
filename is kept. Uploads are limited to images, documents and archives defined
in `forgemesh_browser/media/metadata.py`, and oversized or corrupt images are
rejected before reaching the page.

## Vision

```bash
fmb screenshot                          # viewport
fmb screenshot --full-page -o ./page.png
fmb screenshot --selector "#chart" -o ./chart.png
fmb locate --image ./reference.png [--screenshot ./page.png]
fmb locate --image ./reference.png -o annotated.png
fmb ocr ./page.png --lang eng -o page.txt
fmb compare ./before.png ./after.png -o diff.png
```

`locate` performs multiscale normalized cross-correlation (NumPy FFT) and
returns ranked boxes with confidence; `-o` copies the annotated PNG (the box
overlay the API writes under `data/results/`) to your path. `ocr` requires the
system `tesseract` binary; when missing, the command fails with the exact apt
command to install it. `compare` reports whether two images are pixel-identical
or, when they differ, the changed-pixel ratio and bounding box; `-o` writes the
difference image. Different-sized inputs are reported and exit non-zero. None
of these commands uses the AI layer.

## AI

```bash
fmb ai-providers
fmb ai-health
fmb ai-models [--provider ollama]
fmb ai "Summarize this page" --page
fmb ai --image ./reference.png "Find the matching button"
fmb ai --image ./reference.png --output ./results/
fmb ai --model local "Summarize this"
fmb ai --provider openai --model gpt-4o-mini "..."   # only if configured
fmb ai "Return JSON" --format json
```

- Default provider is `ollama`; it must be running locally.
- `--page` includes a DOM/text extract of the active page and adds a system
  prompt that treats page content as untrusted data.
- `--model local` maps to Ollama's default model.
- `--output` writes to a file, or into a directory as
  `ai-<timestamp>.<txt|json>`.
- The OpenAI-compatible provider is available only when
  `FMB_OPENAI_BASE_URL` and `FMB_OPENAI_API_KEY` are set. No command falls back
  to it automatically or silently spends money.
- Images must be supported types and at most 4 per request (8 MB each).

## Accounts and browser providers

```bash
fmb providers
fmb account login chatgpt --profile work    # also: gemini, sarvam, claude, kimi, grok, perplexity
fmb account complete chatgpt --profile work   # verify and record the session
fmb account list
fmb account status
fmb account inspect chatgpt --profile work
fmb account check [chatgpt]                   # live session check
fmb account ask chatgpt "Summarize this" --file ./input.pdf --timeout 180
fmb account capture chatgpt --output reply.png
```

`account login` opens the official site in a visible browser and polls the
daemon until the session is detected. Passwords, CAPTCHA and MFA are handled by
you in the browser; the tool never asks for them in the terminal. Polling works
without a TTY, so `--wait` is safe in scripts and background jobs; use
`--login-timeout <seconds>` (default 300) to bound the wait. With `--no-wait`,
run `account complete` yourself when done. Headless sessions can be reused after
a manual login, but the provider may still challenge expired sessions.

Chrome is launched as a normal OS process and Playwright attaches over CDP
(`browser_stealth: true`, the default). This keeps `navigator.webdriver` and the
User-Agent free of automation markers, which is required for "Sign in with
Google" flows to reach the password step instead of returning "This browser or
app may not be secure".

`account ask` automates the provider's normal chat UI and stores the answer. It
does not rotate accounts, bypass limits or switch to a paid API. If the site
reports a usage restriction, the account is marked `rate_limited` using only the
provider's own retry text.

## Identities and the encrypted vault

A **profile** isolates cookies. An **identity** binds one email address to
exactly one profile, so several emails never overwrite each other's sessions.
The vault stores the password encrypted; `identity login` fills the provider's
own form and then waits for the session.

```bash
fmb identity add you@gmail.com --label personal     # derives profile 'you'
fmb identity add you@outlook.com --profile-id you-outlook
fmb identity list
fmb identity password you@gmail.com                 # hidden prompt, confirmation
fmb identity password you@gmail.com --stdin < secret.txt
fmb identity password you@gmail.com --provider gemini   # per-site override
fmb identity password you@gmail.com --clear         # delete the stored secret
fmb identity login you@gmail.com --provider gemini --headed
fmb identity login-all                              # all identities × all providers
fmb identity login-all --email you@gmail.com --provider gemini,deepseek
fmb identity status
fmb identity remove you@gmail.com --yes [--delete-profile]
```

Status values per cell:

| Status | Meaning | What to do |
| --- | --- | --- |
| `logged_in` | Prefill worked and the session is live | nothing |
| `already_logged_in` | A valid session was found before any form was filled | nothing |
| `needs_human` | CAPTCHA, OTP/2FA, device approval or an unknown form | finish it in the visible window, then `fmb identity status` |
| `needs_credential` | No stored password, or the site rejected it | `fmb identity password <email> [--provider <id>]` |
| `blocked` | The identity provider refused the automated browser | use the site's own email/phone form or the official API |
| `error` | Browser/adapter failure, layout change | re-run `fmb account check` and inspect the window |

`--headed` is the default; `--headless` only works when no human step is needed.
`--timeout` bounds each cell (default 180 s). The runner checks the normal app
URL first, so a second run reports `already_logged_in` without submitting the
credentials again.

Vault defaults: a random 256-bit key in
`~/.config/forgemesh-browser/vault.key` (0600), ciphertext in
`~/.local/share/forgemesh-browser/secrets/vault.json` (0600, dir 0700). Set
`FMB_VAULT_PASSPHRASE` for the CLI *and* the daemon before the first password to
use scrypt instead. If neither key nor passphrase is available, the vault cannot
be decrypted and no password is exposed.

CAPTCHA and MFA are never solved or replayed. The runner stops that cell and
never retries a rejected password.

## Daemon and observability

```bash
fmb serve [--host 127.0.0.1] [--port 8765]   # foreground, systemd-friendly
fmb shutdown
fmb logs --lines 100 [--follow]
fmb tasks [--limit 25]
fmb task <task-id>
fmb events [--limit 50]
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Command failed (domain or daemon error); message and code are printed |
| 2 | Invalid CLI usage (Typer) |

With `--json`, successful results are JSON. Failures still use exit code 1 and
print an error; scripts should check the exit code rather than parse a
success-shaped object.

## API

The REST and WebSocket interface is documented in [API_REFERENCE.md](API_REFERENCE.md).
