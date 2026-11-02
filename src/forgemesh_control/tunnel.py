"""ngrok tunnel for the control center.

Workers can reach the server from another network over the public HTTPS URL
this module publishes. Only the official ngrok CLI is used:

* ``ngrok config add-authtoken`` stores the credential in a private config
  file (mode 0600). ForgeMesh never stores, logs or returns the token.
* ``ngrok http <port>`` starts the tunnel. The public URL is read from
  ngrok's own local API, never guessed.

The free ngrok plan assigns a new hostname on every start, so the reported URL
is always the live one; when the process exits the URL is dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_control.config import ControlSettings

#: Official stable Linux builds linked from https://ngrok.com/download.
NGROK_DOWNLOADS = {
    "x86_64": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip",
    "amd64": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.zip",
    "aarch64": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.zip",
    "arm64": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.zip",
    "armv7l": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm.zip",
    "armv6l": "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm.zip",
}

#: Tokens are redacted from every CLI line we keep or return.
TOKEN_PATTERN = re.compile(r"\b2[A-Za-z0-9_]{30,}\b")
VERSION_PATTERN = re.compile(r"(\d+\.\d+\.\d+)")
TOKEN_INPUT_PATTERN = re.compile(r"[A-Za-z0-9._\-]{20,200}")

START_TIMEOUT_SECONDS = 30.0
MAX_DOWNLOAD_BYTES = 120 * 1024 * 1024

#: One-time setup for the other laptop; the package then provides ``fmw``.
WORKER_INSTALL_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/ismailmo9090-del/"
    "forgemesh-control/main/scripts/forgemesh.sh | bash -s -- --worker"
)

logger = logging.getLogger(__name__)


class TunnelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "tunnel_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    @property
    def status_code(self) -> int:
        if self.code in {"not_installed", "needs_authtoken", "already_running"}:
            return 409
        if self.code in {"unsupported", "invalid"}:
            return 400
        return 502


def _redact(text: str) -> str:
    return TOKEN_PATTERN.sub("[redacted]", text or "")


class TunnelService:
    def __init__(self, settings: ControlSettings, events: Any = None, audit: Any = None) -> None:
        self.settings = settings
        self.events = events
        self.audit = audit
        self._proc: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None
        self._api_port: int | None = None
        self._public_url: str | None = None
        self._last_error: str | None = None
        self._started_at: str | None = None
        self._version_cache: tuple[float, str] | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ paths
    @property
    def tunnel_dir(self) -> Path:
        return self.settings.data_dir / "tunnel"

    @property
    def config_path(self) -> Path:
        return self.tunnel_dir / "ngrok.yml"

    @property
    def runtime_config_path(self) -> Path:
        return self.tunnel_dir / "runtime.yml"

    @property
    def state_path(self) -> Path:
        return self.tunnel_dir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.settings.logs_dir / "ngrok.log"

    @property
    def default_config_path(self) -> Path:
        return Path.home() / ".config" / "ngrok" / "ngrok.yml"

    # -------------------------------------------------------------- discovery
    def resolve_binary(self) -> str:
        configured = self.settings.ngrok_bin
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        found = shutil.which("ngrok")
        if found:
            return found
        for candidate in (
            Path.home() / ".local" / "bin" / "ngrok",
            Path("/snap/bin/ngrok"),
            Path("/usr/local/bin/ngrok"),
            Path("/usr/bin/ngrok"),
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return ""

    async def _version(self, binary: str) -> str:
        now = time.monotonic()
        if self._version_cache and now - self._version_cache[0] < 60:
            return self._version_cache[1]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        text = f"{result.stdout}\n{result.stderr}"
        match = VERSION_PATTERN.search(text)
        version = match.group(1) if match else text.strip().splitlines()[0][:60]
        self._version_cache = (now, version)
        return version

    def _config_with_token(self) -> Path | None:
        if self.config_path.is_file():
            with contextlib.suppress(OSError):
                if TOKEN_PATTERN.search(self.config_path.read_text(encoding="utf-8")):
                    return self.config_path
        if self.default_config_path.is_file():
            with contextlib.suppress(OSError):
                if TOKEN_PATTERN.search(self.default_config_path.read_text(encoding="utf-8")):
                    return self.default_config_path
        return None

    def _authtoken_source(self) -> str:
        path = self._config_with_token()
        if path is None:
            return ""
        return "forgemesh" if path == self.config_path else "ngrok"

    # ---------------------------------------------------------------- process
    def _process_alive(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        if self._pid is None:
            return False
        try:
            os.kill(self._pid, 0)
        except OSError:
            return False
        return True

    def _adopt_state(self) -> None:
        """Recover a tunnel started by a previous server process."""
        if self._pid is not None or self._proc is not None:
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        pid = state.get("pid")
        api_port = state.get("api_port")
        if not isinstance(pid, int) or not isinstance(api_port, int):
            return
        try:
            os.kill(pid, 0)
        except OSError:
            self.state_path.unlink(missing_ok=True)
            return
        # Guard against a recycled PID: only adopt a real ngrok http agent
        # pointed at this server's port.
        with contextlib.suppress(OSError):
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            joined = b" ".join(cmdline).decode("utf-8", "replace")
            if "ngrok" not in joined or str(self.settings.port) not in joined:
                return
        self._pid = pid
        self._api_port = api_port
        self._started_at = state.get("started_at")
        self._public_url = state.get("public_url")

    async def reconcile(self) -> None:
        self._adopt_state()

    async def _local_api_url(self) -> str | None:
        if not self._api_port:
            return None
        url = f"http://127.0.0.1:{self._api_port}/api/tunnels"
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return self._public_url_from(payload)

    @staticmethod
    def _public_url_from(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        fallback = None
        for tunnel in payload.get("tunnels") or []:
            if not isinstance(tunnel, dict):
                continue
            url = tunnel.get("public_url")
            if not isinstance(url, str):
                continue
            if url.startswith("https://"):
                return url
            if url.startswith("http://") and fallback is None:
                fallback = url
        return fallback

    def _log_tail(self, limit: int = 1200) -> str:
        if not self.log_path.is_file():
            return ""
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 64_000))
                raw = handle.read().decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [line for line in raw.splitlines() if line.strip()]
        formatted: list[str] = []
        for line in lines[-8:]:
            try:
                event = json.loads(line)
            except ValueError:
                formatted.append(line)
                continue
            if not isinstance(event, dict):
                formatted.append(line)
                continue
            level = event.get("lvl") or event.get("level") or ""
            parts = [
                f"[{level}]" if level else "",
                str(event.get("msg") or ""),
                str(event.get("err") or ""),
            ]
            formatted.append(" ".join(part for part in parts if part).strip() or line)
        return _redact("\n".join(formatted))[-limit:]

    def _write_state(self) -> None:
        ensure_private_dir(self.tunnel_dir)
        payload = {
            "pid": self._pid,
            "api_port": self._api_port,
            "public_url": self._public_url,
            "started_at": self._started_at,
        }
        self.state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        secure_file(self.state_path)

    def _clear_state(self) -> None:
        self.state_path.unlink(missing_ok=True)

    def _stop_process(self) -> None:
        pid = self._pid
        proc = self._proc
        if proc is not None and proc.poll() is None:
            pid = pid or proc.pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
        elif pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            for _ in range(50):
                if not self._process_alive():
                    break
                time.sleep(0.2)
            if self._process_alive():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
        self._proc = None
        self._pid = None
        self._api_port = None
        self._public_url = None
        self._started_at = None

    # ----------------------------------------------------------------- status
    async def status(self) -> dict[str, Any]:
        self._adopt_state()
        binary = self.resolve_binary()
        version = await self._version(binary) if binary else ""
        alive = self._process_alive()

        if alive:
            live_url = await self._local_api_url()
            if live_url:
                self._public_url = live_url
            elif self._public_url is None:
                # The agent is running but has not registered a URL yet.
                pass
        elif self._pid is not None or self._proc is not None:
            tail = self._log_tail()
            if tail:
                self._last_error = f"ngrok exited.\n{tail}"
            self._stop_process()
            self._clear_state()

        token_source = self._authtoken_source()
        if not binary:
            state = "not_installed"
        elif not token_source:
            state = "needs_authtoken"
        elif alive:
            state = "running" if self._public_url else "starting"
        elif self._last_error:
            state = "error"
        else:
            state = "stopped"

        public_url = self._public_url if alive else None
        port = self.settings.port
        machine = platform.machine().lower()
        return {
            "installed": bool(binary),
            "path": binary,
            "version": version,
            "state": state,
            "running": alive,
            "public_url": public_url,
            "worker_server_url": public_url or "",
            "local_url": f"http://127.0.0.1:{port}",
            "port": port,
            "authtoken_saved": bool(token_source),
            "authtoken_source": token_source,
            "log_path": str(self.log_path) if self.log_path.is_file() else "",
            "last_error": _redact(self._last_error or ""),
            "started_at": self._started_at if alive else None,
            "join_steps": self._join_steps(public_url),
            "worker_install_command": WORKER_INSTALL_COMMAND,
            "free_plan_note": (
                "The free ngrok plan assigns a new public URL every time the tunnel "
                "starts, so create a new pairing code after each restart."
            ),
            "install_supported": platform.system() == "Linux" and machine in NGROK_DOWNLOADS,
            "install_url": "https://ngrok.com/download",
            "install_commands": ["sudo snap install ngrok"],
        }

    def _join_steps(self, public_url: str | None) -> list[str]:
        if not public_url:
            return []
        return [
            f'fmw register --server {public_url} --code <PAIRING-CODE> --name "Worker 1"',
            "fmw run",
        ]

    # -------------------------------------------------------------- authtoken
    async def configure_authtoken(self, token: str) -> dict[str, Any]:
        token = (token or "").strip()
        if not TOKEN_INPUT_PATTERN.fullmatch(token):
            raise TunnelError(
                "That does not look like an ngrok authtoken. Copy it from "
                "https://dashboard.ngrok.com/get-started/your-authtoken",
                code="invalid",
            )
        binary = self.resolve_binary()
        if not binary:
            raise TunnelError("ngrok is not installed on this server.", code="not_installed")

        ensure_private_dir(self.tunnel_dir)
        async with self._lock:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    [binary, "config", "add-authtoken", token, "--config", str(self.config_path)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TunnelError(
                    f"Could not run ngrok config add-authtoken: {_redact(str(exc))}",
                    code="start_failed",
                ) from exc
            if result.returncode != 0:
                detail = _redact((result.stderr or result.stdout or "").strip())
                raise TunnelError(
                    f"ngrok rejected the authtoken (exit {result.returncode}): "
                    f"{detail or 'no output'}",
                    code="start_failed",
                )
            if not self.config_path.is_file():
                raise TunnelError(
                    "ngrok reported success but wrote no config file.", code="start_failed"
                )
            secure_file(self.config_path)
            try:
                self.config_path.chmod(0o600)
            except OSError:
                pass
            if not TOKEN_PATTERN.search(self.config_path.read_text(encoding="utf-8")):
                raise TunnelError(
                    "ngrok config does not contain an authtoken after saving.",
                    code="start_failed",
                )
            check = await asyncio.to_thread(
                subprocess.run,
                [binary, "config", "check", "--config", str(self.config_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if check.returncode != 0:
                detail = _redact((check.stderr or check.stdout or "").strip())
                raise TunnelError(
                    f"ngrok config check failed: {detail or 'no output'}",
                    code="start_failed",
                )
            self._last_error = None
        await self._record("tunnel.authtoken_saved")
        status = await self.status()
        status["saved"] = True
        status["restart_required"] = status["running"]
        return status

    # ------------------------------------------------------------------- start
    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def start(self) -> dict[str, Any]:
        binary = self.resolve_binary()
        if not binary:
            raise TunnelError(
                "ngrok is not installed on this server. Open the tunnel card and use "
                "'Download ngrok' or install it from https://ngrok.com/download.",
                code="not_installed",
            )
        self._adopt_state()
        if self._process_alive():
            return await self.status()
        config = self._config_with_token()
        if config is None:
            raise TunnelError(
                "No ngrok authtoken is saved. Paste your token in the tunnel card first.",
                code="needs_authtoken",
            )

        async with self._lock:
            if self._process_alive():
                return await self.status()
            ensure_private_dir(self.tunnel_dir)
            ensure_private_dir(self.settings.logs_dir)
            api_port = self._free_port()
            self.runtime_config_path.write_text(
                f'version: "3"\nagent:\n    web_addr: 127.0.0.1:{api_port}\n',
                encoding="utf-8",
            )
            secure_file(self.runtime_config_path)

            args = [
                binary,
                "http",
                str(self.settings.port),
                "--log=stdout",
                "--log-format=json",
                "--log-level=info",
                "--config",
                str(config),
                "--config",
                str(self.runtime_config_path),
            ]
            self._last_error = None
            with self.log_path.open("ab") as log:
                secure_file(self.log_path)
                self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            self._pid = self._proc.pid
            self._api_port = api_port
            self._started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._write_state()

            deadline = time.monotonic() + START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    tail = self._log_tail()
                    self._last_error = (
                        "ngrok exited before publishing a URL."
                        + (f"\n{tail}" if tail else "")
                    )
                    self._stop_process()
                    self._clear_state()
                    await self._record("tunnel.failed", error=self._last_error)
                    raise TunnelError(self._last_error, code="start_failed")
                url = await self._local_api_url()
                if url:
                    self._public_url = url
                    self._write_state()
                    await self._record("tunnel.started", public_url=url)
                    return await self.status()
                await asyncio.sleep(0.4)

            tail = self._log_tail()
            self._last_error = (
                f"ngrok did not publish a URL within {int(START_TIMEOUT_SECONDS)}s."
                + (f"\n{tail}" if tail else "")
            )
            self._stop_process()
            self._clear_state()
            await self._record("tunnel.failed", error=self._last_error)
            raise TunnelError(self._last_error, code="start_failed")

    async def maybe_start_on_boot(self) -> dict[str, Any] | None:
        """Publish this server again when the operator already opted in.

        The token must come from the tunnel card's own config. A token in
        ``~/.config/ngrok`` may belong to unrelated work, so it never starts
        this server automatically.
        """
        status = await self.status()
        if status["running"] or not status["installed"]:
            return None
        if status["authtoken_source"] != "forgemesh":
            return None
        if status["state"] not in {"stopped", "error"}:
            return None
        try:
            return await self.start()
        except TunnelError as exc:
            # start() already recorded the real failure; the card polls it.
            logger.warning("Automatic tunnel start failed: %s", exc.message)
            return await self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            was_running = self._process_alive()
            self._stop_process()
            self._clear_state()
            self._public_url = None
            self._last_error = None
        if was_running:
            await self._record("tunnel.stopped")
        return await self.status()

    # ----------------------------------------------------------------- install
    async def install(self) -> dict[str, Any]:
        binary = self.resolve_binary()
        if binary:
            return await self.status()
        if platform.system() != "Linux":
            raise TunnelError(
                "Automatic download is only offered on Linux. Install ngrok from "
                "https://ngrok.com/download and reopen this card.",
                code="unsupported",
            )
        url = NGROK_DOWNLOADS.get(platform.machine().lower())
        if not url:
            raise TunnelError(
                f"No official ngrok build is published for {platform.machine()!r}. "
                "Download it manually from https://ngrok.com/download.",
                code="unsupported",
            )
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=180.0) as client:
                response = await client.get(url)
            if response.status_code != 200:
                raise TunnelError(
                    f"ngrok download returned HTTP {response.status_code}.", code="install_failed"
                )
            content = response.content
        except httpx.HTTPError as exc:
            raise TunnelError(
                f"Could not download ngrok: {_redact(str(exc))}", code="install_failed"
            ) from exc
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise TunnelError("ngrok download was unexpectedly large.", code="install_failed")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                member = next(
                    (
                        name
                        for name in archive.namelist()
                        if name.rsplit("/", 1)[-1] == "ngrok"
                    ),
                    None,
                )
                if member is None:
                    raise TunnelError(
                        "The ngrok archive did not contain an ngrok binary.",
                        code="install_failed",
                    )
                payload = archive.read(member)
        except (zipfile.BadZipFile, OSError) as exc:
            raise TunnelError(
                "The ngrok download was not a valid zip archive.", code="install_failed"
            ) from exc
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise TunnelError("ngrok binary was unexpectedly large.", code="install_failed")
        if not payload.startswith(b"\x7fELF"):
            raise TunnelError(
                "The downloaded file is not a Linux executable; refusing to install it.",
                code="install_failed",
            )

        target_dir = Path.home() / ".local" / "bin"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "ngrok"
        temp = target_dir / f".ngrok.tmp-{os.getpid()}"
        try:
            temp.write_bytes(payload)
            temp.chmod(0o755)
            os.replace(temp, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise TunnelError(
                f"Could not write {target}: {_redact(str(exc))}", code="install_failed"
            ) from exc

        self._version_cache = None
        version = await self._version(str(target))
        if not version:
            with contextlib.suppress(OSError):
                target.unlink()
            raise TunnelError(
                "The downloaded ngrok binary did not run on this machine.",
                code="install_failed",
            )
        await self._record("tunnel.installed", path=str(target), version=version)
        status = await self.status()
        status["installed_path"] = str(target)
        return status

    # ------------------------------------------------------------------ events
    async def _record(self, event_type: str, **fields: Any) -> None:
        if self.events is not None:
            with contextlib.suppress(Exception):
                await self.events.publish(event_type, important=False, **fields)
        if self.audit is not None:
            with contextlib.suppress(Exception):
                await self.audit.record(event_type, **fields)
