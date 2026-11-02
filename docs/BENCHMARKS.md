# Benchmarks

Every number here was measured on the development machine by
`scripts/benchmark.py`; nothing is estimated or copied from upstream claims. Raw
JSON: `data/results/benchmark.json`.

## Machine under test

| Field | Value |
| --- | --- |
| OS | Ubuntu 26.04.1 LTS (kernel 7.0.0-30-generic) |
| CPU | Intel Core i5-8365U @ 1.60 GHz, 8 logical cores |
| RAM | 15 289 MB total |
| Python | 3.14.4 (project `.venv`) |
| Browser | Google Chrome 156.0.8078.79 (system binary, headless) |
| Run | 2026-10-31 15:58 UTC, `--sessions 2`, temp data dir |

## Methodology

- The runtime and browser are driven **in-process** (`Runtime(settings)` with a
  throwaway `data_dir`, `headless=True`, `browser_channel="auto"`), so the
  numbers include profile creation, SQLite init and Playwright launch but not
  daemon HTTP overhead or CLI startup.
- The page is served by a local `ThreadingHTTPServer` on `127.0.0.1:0` —
  ~1.7 KB of HTML with an interactive button, a labelled input and 120
  paragraphs, giving extraction real text to walk (6 651 chars captured). No
  network access is involved, so these are lower bounds versus the public web.
- Memory and CPU come from `/proc`: the process tree is found by matching each
  profile's `--user-data-dir` path in `cmdline`, then walking children via
  `PPID`. Summed `VmRSS` is reported; CPU is the delta of `utime+stime` ticks
  over the measurement window, converted with `SC_CLK_TCK` and reported both as
  % of one core and % of the whole 8-core machine.
- Idle CPU is sampled over a deliberate 1.5 s quiet window after launch; active
  CPU spans page load + screenshot + extraction on one profile.
- The temporary data directory is deleted afterwards unless `--keep` is passed.

## Results

| Measurement | Value | Notes |
| --- | --- | --- |
| Runtime start (SQLite init, default profile, event bus) | **20.9 ms** | one-off |
| Browser start — first profile | **543.4 ms** | persistent context launch, 9-process tree |
| Idle RAM, one profile | **927.1 MB** | 9 Chromium processes |
| Idle CPU, one profile | **9.2 %** of one core (1.1 % of machine) | settling work after launch, not steady-state 0 |
| Page load (local HTTP) | **67.6 ms** | includes navigate + settle |
| Screenshot (viewport, PNG) | **75.9 ms** | written under `screenshots/`, registered as an asset |
| DOM extraction | **5.9 ms** | 6 651 chars of text + headings/links/forms |
| Active RAM | **966.5 MB** | after load + screenshot + extract |
| Active CPU | **57.6 %** of one core (7.2 % of machine) | burst across those three actions |
| 2 more profiles opened concurrently | **494.4 ms** | `asyncio.gather`, 3 live profiles in total |
| Total RAM with 3 live profiles | **2 872.1 MB** | primary + 2 benchmark profiles |
| `stop_all()` | **149.2 ms** | 0 profiles left running afterwards |

Splitting the memory by owner (measured separately, `/proc/<pid>/status` of the
Python process): the daemon itself costs **83 MB** after start and **86 MB**
with one browser context attached — i.e. **~96 % of the footprint is Chromium,
not ForgeMesh**.

## Interpretation

- **Plan around ~1 GB RSS per active profile.** `2 872 MB / 3 profiles ≈ 957 MB`
  average, and `2 872 − 927 = 1 945 MB` for the two extra profiles ⇒ ≈972 MB each
  marginal. That is headless Chrome's own cost; it does not shrink for small
  pages.
- **`max_contexts` is the memory valve.** Default 6 (one seat per built-in
  council provider) ⇒ ~6 GB worst case. On a 4 GB VPS set it to 1; on a 16 GB
  desktop 6 is comfortable. Profiles are stopped
  individually (`fmb stop --profile x`), and stop is cheap (149 ms), so the
  pattern that works is start → work → stop rather than keeping everything open.
- **Launch, not throughput, is the latency budget.** A profile costs 543 ms to
  start and pages load in tens of milliseconds, so a 20-step workflow on one
  already-open profile is dominated by the site's own JavaScript, not by
  ForgeMesh. Reuse sessions.
- **CPU is bursty.** 57.6 % of one core during page load/screenshot/extract,
  ~9 % while settling, effectively idle when a page is static. `CPUQuota=400%`
  in the systemd template leaves the machine responsive without throttling a
  single profile.
- **The CLI adds seconds, not milliseconds.** Every command attaches to (or
  spawns) the daemon over localhost HTTP; a cold daemon spawn is bounded by the
  25 s readiness timeout, a warm one by `browser_start_ms` plus HTTP.
- **AI cost is external.** `ai_timeout_seconds` defaults to 120 s; a local
  Ollama model on this CPU class is seconds-to-minutes per call and dwarfs every
  number above. Nothing here is GPU-accelerated.

## Reproducing

```bash
.venv/bin/python scripts/benchmark.py                          # human summary
.venv/bin/python scripts/benchmark.py --sessions 3 --json
.venv/bin/python scripts/benchmark.py --output data/results/benchmark.json
.venv/bin/python scripts/benchmark.py --data-dir ./bench-data --keep   # inspect artefacts
```

Exit code is 1 when the run fails, and a failure is reported as
`{"success": false, "error": "..."}` rather than as a partial "pass". `--sessions`
larger than `max_contexts` is capped and annotated with `sessions_note`.

For a quick sanity check without measurement, use the smoke path:

```bash
time (FMB_DATA_DIR=$(mktemp -d) .venv/bin/fmb start && .venv/bin/fmb status)
```

Other automated evidence of correctness (not performance):

```bash
.venv/bin/python -m pytest -q                    # 102 tests
.venv/bin/python -m pytest -q -m browser         # real Chrome end-to-end subset
```

## Limitations of these numbers

- One machine, one run, one page. No percentiles; run-to-run spread on a laptop
  with a background desktop session is easily ±10–20 % for the millisecond
  figures. Treat them as orders of magnitude.
- Localhost HTTP and a 1.7 KB page: real sites add network, TLS, third-party
  scripts and much larger DOMs, so loads and extractions in the wild are slower
  and memory per profile grows with page complexity.
- `--user-data-dir` matching counts only processes whose command line names a
  benchmark profile, so a Chrome you already had open is **not** included — and
  correspondingly, the totals exclude any other browser workload on the machine.
- Full-page screenshots, `--all` DOM inspection, downloads of large files, OCR
  and vision matching were not benchmarked here. OCR/`tesseract` is absent on
  this machine, so OCR time is unmeasured (the feature is tested only for its
  dependency error).
- `concurrent_open_ms` measured 3 profiles launched in parallel on 8 logical
  cores; on 1–2 vCPU expect it to scale roughly linearly per profile.
- No headless-vs-headed comparison was run, and no Playwright-bundled Chromium
  was used (the system Chrome is the default channel here).
