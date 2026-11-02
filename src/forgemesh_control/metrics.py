"""Real host metrics from /proc, with no GPU or psutil assumptions."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgemesh_control.config import ControlSettings


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _cpu_times() -> tuple[int, int]:
    line = _read("/proc/stat").splitlines()[0] if _read("/proc/stat") else ""
    fields = [int(value) for value in line.split()[1:] if value.isdigit()]
    if len(fields) < 4:
        return 0, 0
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    total = sum(fields)
    return total, idle


def _meminfo() -> tuple[int, int]:
    total = available = 0
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split()
        if not value:
            continue
        amount = int(value[0]) * 1024
        if key == "MemTotal":
            total = amount
        elif key == "MemAvailable":
            available = amount
    return total, max(0, total - available)


def _net_bytes() -> tuple[int, int]:
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        rx += int(fields[0])
        tx += int(fields[8])
    return rx, tx


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _gpu_info() -> dict[str, Any]:
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            result = subprocess.run(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                name, _, memory = result.stdout.strip().splitlines()[0].partition(",")
                return {"detected": True, "vendor": "nvidia", "name": name.strip(),
                        "memory": memory.strip()}
        except (OSError, subprocess.SubprocessError):
            pass
    render = Path("/dev/dri/renderD128")
    if render.exists():
        return {"detected": True, "vendor": "unknown", "name": "DRM render node",
                "memory": ""}
    return {"detected": False, "vendor": "", "name": "", "memory": ""}


class MetricsService:
    """Samples /proc and keeps a bounded in-memory history for the graphs."""

    def __init__(self, settings: ControlSettings, history: int = 180) -> None:
        self.settings = settings
        self.history: deque[dict[str, Any]] = deque(maxlen=history)
        self._last_cpu = _cpu_times()
        self._last_net = _net_bytes()
        self._last_time = time.monotonic()
        self._last_cpu_percent = 0.0
        self._last_rx_kbps = 0.0
        self._last_tx_kbps = 0.0
        self._gpu: dict[str, Any] | None = None
        self.boot_time = self._boot_time()

    @staticmethod
    def _boot_time() -> datetime | None:
        stat = _read("/proc/stat")
        for line in stat.splitlines():
            if line.startswith("btime "):
                try:
                    return datetime.fromtimestamp(int(line.split()[1]), tz=UTC)
                except ValueError:
                    return None
        return None

    def sample(self) -> dict[str, Any]:
        now = time.monotonic()
        total, idle = _cpu_times()
        prev_total, prev_idle = self._last_cpu
        delta_total = max(0, total - prev_total)
        delta_idle = max(0, idle - prev_idle)
        if delta_total > 0:
            self._last_cpu_percent = round(100.0 * (delta_total - delta_idle) / delta_total, 1)
        self._last_cpu = (total, idle)

        mem_total, mem_used = _meminfo()
        disk = shutil.disk_usage(self.settings.workspaces)

        rx, tx = _net_bytes()
        elapsed = max(0.001, now - self._last_time)
        rx_kbps = max(0.0, (rx - self._last_net[0]) / elapsed / 1024)
        tx_kbps = max(0.0, (tx - self._last_net[1]) / elapsed / 1024)
        if rx >= self._last_net[0] and tx >= self._last_net[1]:
            self._last_rx_kbps = round(rx_kbps, 1)
            self._last_tx_kbps = round(tx_kbps, 1)
        self._last_net = (rx, tx)
        self._last_time = now

        if self._gpu is None:
            self._gpu = _gpu_info()

        ram_percent = round(100.0 * mem_used / mem_total, 1) if mem_total else 0.0
        disk_percent = round(100.0 * disk.used / disk.total, 1) if disk.total else 0.0
        sample = {
            "timestamp": datetime.now(UTC).isoformat(),
            "cpu_percent": self._last_cpu_percent,
            "ram_percent": ram_percent,
            "ram_used_bytes": mem_used,
            "ram_total_bytes": mem_total,
            "disk_percent": disk_percent,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "disk_free_bytes": disk.free,
            "rx_kbps": self._last_rx_kbps,
            "tx_kbps": self._last_tx_kbps,
            "cpu_count": os.cpu_count() or 1,
        }
        self.history.append(sample)
        return sample

    def snapshot(self) -> dict[str, Any]:
        sample = self.sample()
        uptime = time.monotonic()
        return {
            **sample,
            "history": list(self.history),
            "host": {
                "hostname": socket.gethostname(),
                "os": f"{platform.system()} {platform.release()}",
                "distribution": _distribution(),
                "arch": platform.machine(),
                "cpu_model": _cpu_model(),
                "cpu_count": os.cpu_count() or 1,
                "python": platform.python_version(),
                "gpu": self._gpu or {},
                "boot_time": self.boot_time.isoformat() if self.boot_time else None,
            },
            "process": {
                "pid": os.getpid(),
                "uptime_seconds": int(uptime),
            },
        }


def _distribution() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.platform()
