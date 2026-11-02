#!/usr/bin/env python3
"""Measure ForgeMesh Browser resource usage on this machine.

Reports only measured values: startup, memory, CPU, page load, screenshot,
DOM extraction and concurrent sessions. Uses an in-process local HTTP server,
so no network access is required.

Usage:
    .venv/bin/python scripts/benchmark.py [--sessions 2] [--output PATH]

The browser runs headless with the configured channel (system Chrome by
default). The temporary data directory is removed unless --keep is given.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from forgemesh_browser.core.runtime import Runtime  # noqa: E402
from forgemesh_browser.settings import Settings  # noqa: E402

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Benchmark</title></head>
<body>
<h1>ForgeMesh benchmark page</h1>
<p id="status">ready</p>
<button id="go" onclick="document.getElementById('status').textContent='clicked'">Go</button>
<label for="q">Query</label><input id="q" name="q" placeholder="Search">
""" + "".join(
    f"<p>Paragraph {i} with representative text for extraction.</p>" for i in range(120)
) + """
</body></html>
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        return


def _start_site() -> tuple[ThreadingHTTPServer, str]:
    directory = Path(tempfile.mkdtemp(prefix="fmb-bench-site-"))
    (directory / "index.html").write_text(PAGE, encoding="utf-8")
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _proc_table() -> dict[int, dict[str, Any]]:
    table: dict[int, dict[str, Any]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        close = stat.rfind(")")
        fields = stat[close + 2 :].split()
        try:
            ppid = int(fields[1])
            cpu_ticks = int(fields[11]) + int(fields[12])
            rss_kb = 0
            for line in (entry / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
        except (OSError, IndexError, ValueError):
            continue
        table[pid] = {
            "ppid": ppid,
            "rss_kb": rss_kb,
            "cpu_ticks": cpu_ticks,
            "cmdline": cmdline,
        }
    return table


def _tree_pids(table: dict[int, dict[str, Any]], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, info in table.items():
        children.setdefault(info["ppid"], []).append(pid)
    selected = set(roots)
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child not in selected:
                selected.add(child)
                stack.append(child)
    return selected


def _browser_pids(profile_dirs: list[Path]) -> set[int]:
    table = _proc_table()
    needles = [str(path) for path in profile_dirs]
    roots = {
        pid
        for pid, info in table.items()
        if any(needle in info["cmdline"] for needle in needles)
    }
    return _tree_pids(table, roots)


def _measure(pids: set[int]) -> dict[str, float]:
    table = _proc_table()
    return {
        "rss_mb": round(
            sum(table[pid]["rss_kb"] for pid in pids if pid in table) / 1024, 1
        ),
        "cpu_ticks": float(
            sum(table[pid]["cpu_ticks"] for pid in pids if pid in table)
        ),
    }


def _cpu_percent(before: dict[str, float], after: dict[str, float], seconds: float) -> dict[str, float]:
    hz = os.sysconf("SC_CLK_TCK")
    cores = os.cpu_count() or 1
    used = max(0.0, after["cpu_ticks"] - before["cpu_ticks"]) / hz
    one_core = (used / seconds) * 100 if seconds > 0 else 0.0
    return {
        "one_core_percent": round(one_core, 1),
        "machine_percent": round(one_core / cores, 1),
    }


def _machine_info() -> dict[str, Any]:
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
                break
    except OSError:
        pass
    os_name = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    chrome = ""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        binary = shutil.which(name)
        if binary:
            try:
                chrome = subprocess.run(
                    [binary, "--version"], capture_output=True, text=True, timeout=10
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                chrome = binary
            break
    return {
        "os": os_name or platform.platform(),
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
        "cpu_cores": os.cpu_count(),
        "ram_mb": round(mem_kb / 1024, 0) if mem_kb else None,
        "python": platform.python_version(),
        "browser": chrome,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


async def run(sessions: int, keep: bool, data_dir: Path | None) -> dict[str, Any]:
    site, url = _start_site()
    workdir = data_dir or Path(tempfile.mkdtemp(prefix="fmb-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=workdir,
        config_dir=workdir / "config",
        headless=True,
        browser_channel="auto",
    )
    settings.ensure_dirs()
    runtime = Runtime(settings)
    results: dict[str, Any] = {"machine": _machine_info(), "sessions_requested": sessions}
    try:
        started = time.perf_counter()
        await runtime.start()
        results["runtime_start_ms"] = round((time.perf_counter() - started) * 1000, 1)

        started = time.perf_counter()
        await runtime.browser.start("primary", headless=True)
        results["browser_start_ms"] = round((time.perf_counter() - started) * 1000, 1)

        profile_dirs = [runtime.profiles.path("primary")]
        pids = _browser_pids(profile_dirs)
        results["browser_processes"] = len(pids)
        idle_before = _measure(pids)
        idle_started = time.perf_counter()
        await asyncio.sleep(1.5)
        idle_after = _measure(pids)
        results["idle_rss_mb"] = idle_after["rss_mb"]
        results["idle_cpu"] = _cpu_percent(
            idle_before, idle_after, time.perf_counter() - idle_started
        )

        load_started = time.perf_counter()
        cpu_before = _measure(pids)
        await runtime.sessions.open("primary", url)
        results["page_load_ms"] = round((time.perf_counter() - load_started) * 1000, 1)

        shot_started = time.perf_counter()
        await runtime.browser.screenshot("primary")
        results["screenshot_ms"] = round((time.perf_counter() - shot_started) * 1000, 1)

        extract_started = time.perf_counter()
        extracted = await runtime.automation.extract("primary")
        results["dom_extraction_ms"] = round(
            (time.perf_counter() - extract_started) * 1000, 1
        )
        results["extracted_text_chars"] = len(extracted.get("text") or "")

        results["active_rss_mb"] = _measure(_browser_pids(profile_dirs))["rss_mb"]
        results["active_cpu"] = _cpu_percent(
            cpu_before,
            _measure(_browser_pids(profile_dirs)),
            time.perf_counter() - load_started,
        )

        max_contexts = settings.max_contexts
        concurrency = max(1, min(sessions, max_contexts))
        if concurrency != sessions:
            results["sessions_note"] = (
                f"requested {sessions}, capped at max_contexts={max_contexts}"
            )
        names = [f"bench-{index}" for index in range(1, concurrency + 1)]
        for name in names:
            await runtime.profiles.create(name)

        async def open_profile(name: str) -> None:
            await runtime.sessions.open(name, url)

        concurrent_started = time.perf_counter()
        if names:
            await asyncio.gather(*(open_profile(name) for name in names))
        results["concurrent_open_ms"] = round(
            (time.perf_counter() - concurrent_started) * 1000, 1
        )
        all_dirs = [runtime.profiles.path(name) for name in ["primary", *names]]
        results["concurrent_sessions"] = concurrency
        results["concurrent_total_rss_mb"] = _measure(_browser_pids(all_dirs))["rss_mb"]

        stop_started = time.perf_counter()
        await runtime.browser.stop_all()
        results["stop_all_ms"] = round((time.perf_counter() - stop_started) * 1000, 1)
        results["running_after_stop"] = runtime.browser.running_profiles()
        results["success"] = True
    except Exception as exc:  # report, do not hide
        results["success"] = False
        results["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await runtime.stop()
        finally:
            site.shutdown()
            site.server_close()
            if not keep and data_dir is None:
                shutil.rmtree(workdir, ignore_errors=True)
    return results


def _print_summary(data: dict[str, Any]) -> None:
    machine = data["machine"]
    print("ForgeMesh Browser benchmark")
    print(f"  machine : {machine['os']} / {machine['cpu_model']} / "
          f"{machine['cpu_cores']} cores / {machine['ram_mb']} MB RAM")
    print(f"  python  : {machine['python']}    browser: {machine['browser'] or 'unknown'}")
    print(f"  time    : {machine['timestamp']}")
    if not data.get("success"):
        print(f"  FAILED  : {data.get('error')}")
        return
    print(f"  runtime start        : {data['runtime_start_ms']} ms")
    print(f"  browser start        : {data['browser_start_ms']} ms")
    print(f"  idle RAM             : {data['idle_rss_mb']} MB "
          f"({data['browser_processes']} processes)")
    print(f"  idle CPU             : {data['idle_cpu']['one_core_percent']}% of one core "
          f"({data['idle_cpu']['machine_percent']}% of machine)")
    print(f"  page load            : {data['page_load_ms']} ms")
    print(f"  screenshot           : {data['screenshot_ms']} ms")
    print(f"  DOM extraction       : {data['dom_extraction_ms']} ms "
          f"({data['extracted_text_chars']} chars)")
    print(f"  active RAM           : {data['active_rss_mb']} MB")
    print(f"  active CPU           : {data['active_cpu']['one_core_percent']}% of one core "
          f"({data['active_cpu']['machine_percent']}% of machine)")
    print(f"  concurrent sessions  : {data['concurrent_sessions']} opened in "
          f"{data['concurrent_open_ms']} ms, total {data['concurrent_total_rss_mb']} MB")
    print(f"  stop all             : {data['stop_all_ms']} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=2, help="concurrent sessions to test")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    parser.add_argument("--data-dir", type=Path, help="use this data directory")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data directory")
    parser.add_argument("--json", action="store_true", help="print only JSON")
    args = parser.parse_args()

    data = asyncio.run(run(args.sessions, args.keep, args.data_dir))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
        args.output.chmod(0o600)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_summary(data)
        if args.output:
            print(f"\nJSON written to {args.output}")
    return 0 if data.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
