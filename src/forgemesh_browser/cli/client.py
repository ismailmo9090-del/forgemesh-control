"""CLI ↔ daemon client. Automatically starts the local daemon when needed."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from forgemesh_browser.core.exceptions import DaemonError, ForgeMeshError
from forgemesh_browser.security.secrets import read_or_create_token
from forgemesh_browser.settings import Settings
from forgemesh_browser.utils.logging import tail_file

STARTUP_TIMEOUT = 25.0


class APIError(ForgeMeshError):
    code = "api_error"
    http_status = 400

    def __init__(self, status: int, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message, details=details)
        self.http_status = status
        self.code = code


def base_url(settings: Settings) -> str:
    host = settings.api_host
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{settings.api_port}"


def health(settings: Settings, *, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{base_url(settings)}/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if payload.get("service") == "forgemesh-browser":
                return payload
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return None


def ensure_daemon(settings: Settings) -> None:
    if health(settings):
        return
    settings.ensure_dirs()
    log_path = settings.daemon_log_path
    command = [sys.executable, "-m", "forgemesh_browser.daemon"]
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            cwd=str(Path.cwd()),
            env=os.environ.copy(),
        )
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if health(settings):
            return
        if process.poll() is not None:
            raise DaemonError(
                "ForgeMesh daemon failed to start.\n" + (tail_file(log_path, 25) or "No log output.")
            )
        time.sleep(0.25)
    raise DaemonError(
        f"Daemon did not become ready within {STARTUP_TIMEOUT:.0f}s. "
        f"See {log_path}\n" + (tail_file(log_path, 25) or "")
    )


class ServiceClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.token = read_or_create_token(settings.token_path)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ServiceClient:
        self._client = httpx.AsyncClient(
            base_url=base_url(self.settings),
            headers={"X-API-Token": self.token},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        timeout: float | None = None,
    ) -> Any:
        assert self._client is not None
        try:
            response = await self._client.request(
                method,
                path,
                json=payload,
                params=params,
                files=files,
                data=data,
                timeout=timeout,
            )
        except httpx.RequestError as exc:
            raise DaemonError(
                f"Cannot reach the local daemon at {base_url(self.settings)}: {exc}. "
                "Try `fmb logs`."
            ) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
                error = body.get("error") or {}
                raise APIError(
                    response.status_code,
                    error.get("code", "api_error"),
                    error.get("message") or body.get("detail") or response.text[:500],
                    error.get("details"),
                )
            except json.JSONDecodeError:
                raise APIError(
                    response.status_code, "api_error", response.text[:500]
                ) from None
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, payload: dict | None = None, **kwargs: Any) -> Any:
        return await self._request("POST", path, payload=payload, **kwargs)

    async def patch(self, path: str, payload: dict | None = None, **kwargs: Any) -> Any:
        return await self._request("PATCH", path, payload=payload, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self._request("DELETE", path, **kwargs)

    async def upload(
        self,
        path: str,
        file_path: Path,
        *,
        profile: str | None,
        selector: str | None,
        tab_id: str | None = None,
        timeout: float = 180.0,
    ) -> Any:
        assert self._client is not None
        with file_path.open("rb") as handle:
            return await self._request(
                "POST",
                path,
                files={"file": (file_path.name, handle, "application/octet-stream")},
                data={
                    "profile": profile or "",
                    "selector": selector or "",
                    "tab_id": tab_id or "",
                },
                timeout=timeout,
            )

    async def get_bytes(self, path: str) -> bytes:
        assert self._client is not None
        response = await self._client.get(path)
        if response.status_code >= 400:
            raise APIError(
                response.status_code,
                "media_error",
                response.text[:300],
            )
        return response.content


def run_with_client(
    ctx,
    operation: Callable[[ServiceClient], Any],
    *,
    require_running: bool = False,
) -> Any:
    """Start the daemon if needed, execute an async operation, handle errors.

    With ``require_running`` the daemon is never auto-started (used by commands
    such as ``fmb stop`` where spawning a daemon would be surprising).
    """

    from forgemesh_browser.cli.output import Output, get_context

    cli = get_context(ctx)
    out = Output(cli)

    async def runner():
        if require_running and health(cli.settings) is None:
            raise DaemonError(
                "The local daemon is not running. Start it with `fmb start` "
                "(or any browser command)."
            )
        ensure_daemon(cli.settings)
        async with ServiceClient(cli.settings) as client:
            result = operation(client)
            if asyncio.iscoroutine(result):
                return await result
            return result

    try:
        return asyncio.run(runner())
    except ForgeMeshError as exc:
        out.error(exc.message, code=exc.code, details=exc.details)
        raise SystemExit(1) from None
    except (OSError, ValueError) as exc:
        out.error(str(exc))
        raise SystemExit(1) from None


def daemon_pid(settings: Settings) -> int | None:
    path = settings.daemon_info_path
    if not path.is_file():
        return None
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        pid = int(info.get("pid"))
    except (ValueError, OSError, TypeError):
        return None
    if not Path(f"/proc/{pid}").exists():
        return None
    return pid


def _cmdline_is_daemon(args: list[str]) -> bool:
    """Recognise both supported daemon launch forms.

    The CLI auto-starts `python -m forgemesh_browser.daemon`; scripts and the
    systemd unit run the installed entry point as `fmb serve`.
    """

    joined = " ".join(args)
    if "forgemesh_browser.daemon" in joined:
        return True
    runs_fmb = any(Path(arg).name in {"fmb", "fmb.exe"} for arg in args)
    return runs_fmb and "serve" in args


def _is_daemon_process(pid: int) -> bool:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return _cmdline_is_daemon(
        [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
    )


def shutdown_daemon(settings: Settings) -> dict[str, Any]:
    if not health(settings):
        return {"stopped": False, "message": "Daemon is not running"}
    pid = daemon_pid(settings)
    if pid is None or not _is_daemon_process(pid):
        raise DaemonError(
            "Daemon is responding but its PID file is missing or stale; refusing to kill an unknown process."
        )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not health(settings):
            return {"stopped": True, "pid": pid}
        time.sleep(0.25)
    raise DaemonError(
        f"Daemon (pid {pid}) did not shut down within 15s. Check `fmb logs`."
    )
