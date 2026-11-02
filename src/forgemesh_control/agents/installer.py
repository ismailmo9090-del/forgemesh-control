"""Detection and installation of coding CLIs on the central server only.

This module never installs anything on mesh workers. It only touches the
machine running the ForgeMesh Control Center.

Installation is treated as an observable process, not a single boolean:

1. detect OS + architecture,
2. download the *official* installer over HTTPS and check its contents,
3. execute the downloaded script with ``bash`` (no ``curl | bash``),
4. re-detect the real executable and run ``--version``,
5. hand off to the registry for functional verification and authentication.

Every stage is reported through a callback so the setup wizard can show the
actual line the installer printed. Nothing is marked successful merely because
a process exited with zero; the executable must exist and report a version.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from forgemesh_control.agents.base import agent_env, first_version, stream_process
from forgemesh_control.config import ControlSettings

StageCallback = Callable[[str, str, str, dict[str, Any]], Awaitable[None] | None]

INSTALL_TIMEOUT_SECONDS = 900
DOWNLOAD_TIMEOUT_SECONDS = 120


@dataclass(slots=True, frozen=True)
class AgentSource:
    kind: str
    display_name: str
    binary_name: str
    script_url: str
    script_args: tuple[str, ...]
    script_marker: str
    npm_package: str
    known_paths: tuple[str, ...]
    docs_url: str


OFFICIAL_SOURCES: dict[str, AgentSource] = {
    "opencode": AgentSource(
        kind="opencode",
        display_name="OpenCode CLI",
        binary_name="opencode",
        script_url="https://opencode.ai/install",
        script_args=("--no-modify-path",),
        script_marker="opencode",
        npm_package="opencode-ai",
        known_paths=(
            "~/.opencode/bin/opencode",
            "~/.local/bin/opencode",
            "~/npm-global/bin/opencode",
            "~/.npm-global/bin/opencode",
            "/usr/local/bin/opencode",
        ),
        docs_url="https://opencode.ai/docs",
    ),
    "qoder": AgentSource(
        kind="qoder",
        display_name="Qoder CLI",
        binary_name="qodercli",
        script_url="https://qoder.com/install",
        script_args=("--skip-path",),
        script_marker="qoder",
        npm_package="@qoder-ai/qodercli",
        known_paths=(
            "~/.local/bin/qodercli",
            "~/npm-global/bin/qodercli",
            "~/.npm-global/bin/qodercli",
            "/usr/local/bin/qodercli",
        ),
        docs_url="https://docs.qoder.com/cli/installation",
    ),
}


@dataclass(slots=True)
class PlatformInfo:
    os_name: str
    arch: str
    supported: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "os": self.os_name,
            "arch": self.arch,
            "supported": self.supported,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Detection:
    kind: str
    installed: bool
    path: str = ""
    version: str = ""
    error: str | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "installed": self.installed,
            "path": self.path,
            "version": self.version,
            "error": self.error,
            "candidates": self.candidates,
        }


@dataclass(slots=True)
class InstallResult:
    kind: str
    ok: bool
    stage: str
    path: str = ""
    version: str = ""
    source: str = ""
    log_path: str = ""
    error: str | None = None
    log_tail: str = ""
    duration_ms: int = 0


def detect_platform(kind: str | None = None) -> PlatformInfo:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "unknown")
    if system not in {"linux", "darwin"}:
        return PlatformInfo(
            system,
            arch,
            False,
            "Central agent installation is implemented for Linux and macOS only; "
            "worker-side installation is handled by the Mesh Client team.",
        )
    if arch not in {"amd64", "arm64"}:
        return PlatformInfo(system, arch, False, f"Unsupported CPU architecture: {machine}")
    if kind == "qoder" and system == "windows" and arch == "arm64":
        return PlatformInfo(system, arch, False, "Qoder CLI does not publish Windows arm64 builds.")
    return PlatformInfo(system, arch, True, f"{system}/{arch}")


class AgentInstaller:
    def __init__(
        self,
        settings: ControlSettings,
        *,
        on_stage: StageCallback | None = None,
    ) -> None:
        self.settings = settings
        self.on_stage = on_stage
        self._locks: dict[str, asyncio.Lock] = {
            kind: asyncio.Lock() for kind in OFFICIAL_SOURCES
        }

    # ------------------------------------------------------------------ notify
    async def _stage(
        self, kind: str, stage: str, message: str, **data: Any
    ) -> None:
        if self.on_stage is None:
            return
        result = self.on_stage(kind, stage, message, data)
        if asyncio.iscoroutine(result):
            await result

    # ---------------------------------------------------------------- detection
    def _candidates(self, kind: str) -> list[Path]:
        source = OFFICIAL_SOURCES[kind]
        configured = {
            "opencode": self.settings.opencode_bin,
            "qoder": self.settings.qoder_bin,
        }.get(kind)
        candidates: list[Path] = []
        if configured:
            candidates.append(Path(configured).expanduser())
        found = shutil.which(source.binary_name, path=agent_env().get("PATH"))
        if found:
            candidates.append(Path(found))
        for raw in source.known_paths:
            candidates.append(Path(raw).expanduser())
        if kind == "qoder":
            # Versioned payload installed by ``qodercli install``. Newest first.
            versioned = sorted(
                Path("~/.qoder/bin/qodercli").expanduser().glob("qodercli-*"),
                reverse=True,
            )
            candidates.extend(versioned)

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            unique.append(resolved)
        return unique

    def _is_gui_binary(self, kind: str, path: Path) -> bool:
        if kind != "qoder":
            return False
        text = str(path)
        # ``/opt/Qoder/qoder`` and ``/usr/bin/qoder`` are the desktop app, not
        # the CLI. Installing the CLI must never overwrite them.
        return path.name == "qoder" or "/opt/qoder" in text.lower()

    async def _probe_binary(self, path: Path, timeout_seconds: int = 25) -> tuple[str, str]:
        if not path.is_file():
            return "", "not a file"
        if not (path.stat().st_mode & stat.S_IXUSR):
            return "", "file is not executable"
        code, lines, error = await stream_process(
            [str(path), "--version"],
            cwd=Path.home(),
            env=agent_env(),
            timeout_seconds=timeout_seconds,
        )
        text = "\n".join(lines).strip()
        if error:
            return "", error
        if code != 0:
            return "", f"--version exited with {code}: {text[:300]}"
        version = first_version(text)
        if not version:
            return "", f"--version returned no version: {text[:300]}"
        return version, ""

    async def detect(self, kind: str) -> Detection:
        source = OFFICIAL_SOURCES[kind]
        report = Detection(kind=kind, installed=False)
        for path in self._candidates(kind):
            entry = {"path": str(path), "exists": path.is_file()}
            report.candidates.append(entry)
            if self._is_gui_binary(kind, path):
                entry["skipped"] = "desktop Qoder application, not the CLI"
                continue
            if not path.is_file():
                continue
            version, error = await self._probe_binary(path)
            entry["version"] = version
            if error:
                entry["error"] = error
                report.error = report.error or error
                continue
            report.installed = True
            report.path = str(path)
            report.version = version
            report.error = None
            return report
        if report.error is None:
            report.error = f"{source.display_name} executable was not found."
        return report

    # -------------------------------------------------------------- installation
    async def install(self, kind: str, *, force: bool = False) -> InstallResult:
        source = OFFICIAL_SOURCES[kind]
        async with self._locks[kind]:
            return await self._install_locked(source, force=force)

    async def _install_locked(self, source: AgentSource, *, force: bool) -> InstallResult:
        started = time.monotonic()
        platform_info = detect_platform(source.kind)
        if not platform_info.supported:
            return InstallResult(
                kind=source.kind,
                ok=False,
                stage="failed",
                error=platform_info.detail,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        await self._stage(
            source.kind, "detected", f"Platform detected: {platform_info.detail}"
        )
        if not force:
            existing = await self.detect(source.kind)
            if existing.installed:
                await self._stage(
                    source.kind,
                    "already_installed",
                    f"Already installed at {existing.path} (version {existing.version})",
                    path=existing.path,
                    version=existing.version,
                )
                return InstallResult(
                    kind=source.kind,
                    ok=True,
                    stage="already_installed",
                    path=existing.path,
                    version=existing.version,
                    source="existing",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

        log_path = self._log_path(source.kind)
        log_lines: list[str] = []

        def log(message: str) -> None:
            stamp = datetime.now(UTC).strftime("%H:%M:%S")
            log_lines.append(f"[{stamp}] {message}")

        temp_dir = Path(tempfile.mkdtemp(prefix=f"forgemesh-{source.kind}-"))
        script_path = temp_dir / f"{source.kind}-install.sh"
        try:
            await self._stage(
                source.kind, "downloading", f"Downloading {source.script_url}"
            )
            log(f"Downloading official installer: {source.script_url}")
            download_error = await self._download_script(source, script_path, log)
            if download_error:
                return await self._fail(
                    source, "downloading", download_error, log_lines, log_path, started
                )

            await self._stage(
                source.kind,
                "installing",
                "Running the official installer (no sudo, no shell profile changes)",
            )
            log(f"Executing downloaded script with bash: {script_path}")
            code, lines, error = await stream_process(
                ["bash", str(script_path), *source.script_args],
                cwd=temp_dir,
                env=agent_env({"FORGEMESH_MANAGED_INSTALL": "1"}),
                timeout_seconds=INSTALL_TIMEOUT_SECONDS,
                on_line=log,
            )
            await self._stage(
                source.kind,
                "installing",
                "\n".join(lines[-12:]) if lines else "Installer produced no output yet",
            )
            source_used = "official_script"
            if error or code != 0:
                log(f"Official script failed (exit {code}): {error or 'see log'}")
                npm_error = await self._npm_install(source, log)
                if npm_error is None:
                    source_used = "npm_fallback"
                else:
                    log(f"npm fallback also failed: {npm_error}")
                    tail = "\n".join(log_lines[-25:])
                    return await self._fail(
                        source,
                        "installing",
                        error or f"Official installer exited with {code}. {npm_error}",
                        log_lines,
                        log_path,
                        started,
                        tail=tail,
                    )

            await self._stage(
                source.kind, "configuring", "Checking executable path and permissions"
            )
            detection = await self.detect(source.kind)
            if not detection.installed:
                tail = "\n".join(log_lines[-25:])
                return await self._fail(
                    source,
                    "configuring",
                    detection.error or "Installer finished but no executable was found.",
                    log_lines,
                    log_path,
                    started,
                    tail=tail,
                )

            await self._stage(
                source.kind,
                "verifying",
                f"Executable found at {detection.path}; verifying --version",
                path=detection.path,
                version=detection.version,
            )
            log(f"Detected executable: {detection.path} (version {detection.version})")
            await self._stage(
                source.kind,
                "ready",
                f"{source.display_name} {detection.version} installed at {detection.path}",
                path=detection.path,
                version=detection.version,
            )
            self._write_log(log_path, log_lines)
            return InstallResult(
                kind=source.kind,
                ok=True,
                stage="ready",
                path=detection.path,
                version=detection.version,
                source=source_used,
                log_path=str(log_path),
                log_tail="\n".join(log_lines[-25:]),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _download_script(
        self, source: AgentSource, destination: Path, log: Callable[[str], None]
    ) -> str | None:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS),
                headers={"User-Agent": "ForgeMesh-Control/0.1 (central installer)"},
            ) as client:
                response = await client.get(source.script_url)
            if response.status_code != 200:
                return f"Installer download returned HTTP {response.status_code}"
            body = response.content
        except (httpx.HTTPError, OSError) as exc:
            return f"Could not download the official installer: {exc}"

        if len(body) < 500:
            return f"Installer download is suspiciously small ({len(body)} bytes)"
        text = body.decode("utf-8", errors="replace")
        lowered = text.lower()
        if lowered.lstrip().startswith(("<!doctype html", "<html")):
            return "Installer URL returned HTML instead of a shell script"
        if not text.lstrip().startswith("#!"):
            return "Downloaded installer has no shebang; refusing to execute it"
        if source.script_marker.lower() not in lowered:
            return (
                f"Downloaded installer does not mention {source.display_name}; "
                "refusing to execute an unverified script"
            )
        destination.write_bytes(body)
        destination.chmod(0o700)
        log(f"Downloaded {len(body)} bytes; script markers verified")
        return None

    async def _npm_install(
        self, source: AgentSource, log: Callable[[str], None]
    ) -> str | None:
        npm = shutil.which("npm", path=agent_env().get("PATH"))
        if not npm:
            return "npm is not available for a fallback installation"
        prefix = Path.home() / ".local"
        argv = [npm, "install", "-g", "--prefix", str(prefix), source.npm_package]
        log(f"Trying official npm package fallback: {' '.join(argv)}")
        code, lines, error = await stream_process(
            argv,
            cwd=Path.home(),
            env=agent_env(),
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            on_line=log,
        )
        if error or code != 0:
            tail = "\n".join(lines[-10:])
            return f"npm exited with {code}: {error or tail}"
        return None

    async def _fail(
        self,
        source: AgentSource,
        stage: str,
        message: str,
        log_lines: list[str],
        log_path: Path,
        started: float,
        *,
        tail: str = "",
    ) -> InstallResult:
        log_lines.append(f"[FAILED] {message}")
        self._write_log(log_path, log_lines)
        await self._stage(
            source.kind,
            "failed",
            message,
            error=message,
            log_path=str(log_path),
        )
        return InstallResult(
            kind=source.kind,
            ok=False,
            stage="failed",
            log_path=str(log_path),
            error=message,
            log_tail=tail or "\n".join(log_lines[-25:]),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------------- update
    async def update(self, kind: str, *, approve: bool) -> InstallResult:
        """Update only after explicit approval and only with a verified command."""
        source = OFFICIAL_SOURCES[kind]
        if not approve:
            return InstallResult(
                kind=kind,
                ok=False,
                stage="failed",
                error=(
                    "Update policy requires explicit user approval. "
                    "Confirm the update in Agent Management first."
                ),
            )
        detection = await self.detect(kind)
        if not detection.installed:
            return InstallResult(
                kind=kind,
                ok=False,
                stage="failed",
                error="Cannot update an agent that is not installed.",
            )
        argv = (
            [detection.path, "upgrade"]
            if kind == "opencode"
            else [detection.path, "update"]
        )
        started = time.monotonic()
        log_path = self._log_path(f"{kind}-update")
        log_lines: list[str] = []
        await self._stage(kind, "installing", f"Running {' '.join(argv)}")
        code, lines, error = await stream_process(
            argv,
            cwd=Path.home(),
            env=agent_env(),
            timeout_seconds=INSTALL_TIMEOUT_SECONDS,
            on_line=log_lines.append,
        )
        if error or code != 0:
            message = error or f"Update command exited with {code}"
            self._write_log(log_path, log_lines)
            return await self._fail(source, "failed", message, log_lines, log_path, started)
        after = await self.detect(kind)
        self._write_log(log_path, log_lines)
        await self._stage(
            kind,
            "ready",
            f"Update finished; now at version {after.version or 'unknown'}",
            path=after.path,
            version=after.version,
        )
        return InstallResult(
            kind=kind,
            ok=after.installed,
            stage="ready" if after.installed else "failed",
            path=after.path,
            version=after.version,
            source="update",
            log_path=str(log_path),
            log_tail="\n".join(log_lines[-25:]),
            error=None if after.installed else "Update command succeeded but detection failed",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # -------------------------------------------------------------------- files
    def _log_path(self, name: str) -> Path:
        directory = self.settings.logs_dir / "agents"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return directory / f"{name}-{stamp}.log"

    def _write_log(self, path: Path, lines: list[str]) -> None:
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        except OSError:
            pass
