"""Local-only control plane for the ForgeMesh Client Manager desktop app.

The Electron shell owns the window. This process owns everything that must be
true: pairing, the worker token, the supervised worker loop, local CLI
detection, the install wizard and the PTY-hosted sign-in. It binds
``127.0.0.1`` on an ephemeral port and accepts only a random bearer token that
is handed to the Electron main process over stdout at startup.

Why a second local service instead of fetching the mesh API from the renderer:

* the renderer is sandboxed and never sees a credential;
* the worker token over plain HTTP/WS is guarded in exactly one place;
* long operations (installer, PTY) need to outlive a page reload;
* the output laptop's dashboard routes are optional ``operator mode`` — the
  worker token alone is never sent to them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from forgemesh_browser.security.permissions import ensure_private_dir, secure_file
from forgemesh_control.agent_auth import (
    HAS_PTY,
    KEY_SEQUENCES,
    AgentAuthError,
    TerminalSession,
    redact_output,
)
from forgemesh_control.agents.base import agent_env
from forgemesh_control.agents.installer import (
    OFFICIAL_SOURCES,
    AgentInstaller,
    detect_platform,
)
from forgemesh_control.agents.opencode import VERIFY_PROMPT, VERIFY_TOKEN, OpenCodeAgent
from forgemesh_control.agents.qoder import QoderAgent
from forgemesh_control.config import ControlSettings
from forgemesh_control.mesh import MESH_PROTOCOL_VERSION
from forgemesh_control.security import is_loopback
from forgemesh_control.worker_client import collect_inventory

VERSION = "0.1.4"
APP_NAME = "ForgeMesh Client Manager"
MAX_EVENTS = 500
MAX_WORKER_LOG_LINES = 800
MAX_JOB_LOG_LINES = 1200
CHALLENGE_RE = re.compile(
    r"(?i)(captcha|recaptcha|hcaptcha|one[- ]time|otp|mfa|two[- ]factor|2fa|"
    r"verification code|device code|browser may not be secure|not a secure browser)"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _xdg(kind: str, fallback: str) -> Path:
    base = os.environ.get(f"XDG_{kind.upper()}_HOME")
    return Path(base) if base else Path.home() / fallback


@dataclass(slots=True)
class ClientPaths:
    config: Path
    data: Path

    @classmethod
    def create(cls) -> ClientPaths:
        paths = cls(
            config=_xdg("config", ".config") / "forgemesh-client-manager",
            data=_xdg("data", ".local/share") / "forgemesh-client-manager",
        )
        for directory in (
            paths.config,
            paths.config / "agent-config",
            paths.data,
            paths.data / "logs",
            paths.data / "logs" / "agents",
            paths.data / "logs" / "tasks",
            paths.data / "artifacts",
            paths.data / "workspaces",
            paths.data / "reviews",
        ):
            ensure_private_dir(directory)
        return paths

    @property
    def config_file(self) -> Path:
        return self.config / "config.json"

    @property
    def token_file(self) -> Path:
        return self.config / "worker-token"

    @property
    def control_token_file(self) -> Path:
        return self.config / "operator-control-token"

    @property
    def reviews_file(self) -> Path:
        return self.data / "reviews" / "reviews.json"


class ClientError(RuntimeError):
    def __init__(self, message: str, *, code: str = "client_error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# --------------------------------------------------------------------- secrets
def _secret_tool() -> str | None:
    return shutil.which("secret-tool")


def _secret_lookup() -> str | None:
    tool = _secret_tool()
    if not tool:
        return None
    try:
        result = subprocess.run(
            [tool, "lookup", "service", "forgemesh-client-manager", "key", "worker-token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _secret_store(token: str) -> bool:
    tool = _secret_tool()
    if not tool:
        return False
    try:
        result = subprocess.run(
            [
                tool,
                "store",
                "--label",
                "ForgeMesh Client Manager worker token",
                "service",
                "forgemesh-client-manager",
                "key",
                "worker-token",
            ],
            input=token,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _secret_delete() -> None:
    tool = _secret_tool()
    if not tool:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            [tool, "clear", "service", "forgemesh-client-manager", "key", "worker-token"],
            capture_output=True,
            timeout=10,
        )


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:4].upper() if token else ""


class ConfigStore:
    """JSON config at 0600 plus a token that is never written into it."""

    def __init__(self, paths: ClientPaths) -> None:
        self.paths = paths
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        try:
            data = json.loads(self.paths.config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        hostname = platform.node() or "unknown-host"
        device_key = str(data.get("device_key") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", device_key):
            device_key = uuid.uuid4().hex + uuid.uuid4().hex[:32]
        merged = {
            "device_key": device_key,
            "worker_name": str(data.get("worker_name") or hostname),
            "server_url": str(data.get("server_url") or ""),
            "worker_id": str(data.get("worker_id") or ""),
            "insecure_http": bool(data.get("insecure_http") or False),
            "insecure_acknowledged": bool(data.get("insecure_acknowledged") or False),
            "token_backend": str(data.get("token_backend") or "none"),
            "operator_token_source": str(data.get("operator_token_source") or ""),
            "usable": data.get("usable") or {},
            "usable_paths": data.get("usable_paths") or {},
            "created_at": str(data.get("created_at") or _now()),
            "updated_at": _now(),
        }
        self.data = merged
        self._write()
        return merged

    def _write(self) -> None:
        self.data["updated_at"] = _now()
        tmp = self.paths.config_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.paths.config_file)
        secure_file(self.paths.config_file)

    async def save(self, **updates: Any) -> dict[str, Any]:
        async with self._lock:
            self.data.update(updates)
            self._write()
            return dict(self.data)

    @property
    def configured(self) -> bool:
        return bool(self.data.get("server_url"))

    # ------------------------------------------------------------------ token
    def _read_token(self) -> str:
        if self.paths.token_file.is_file():
            try:
                return self.paths.token_file.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return _secret_lookup() or ""

    async def set_token(self, token: str) -> str:
        """Store in Secret Service, or a 0600 file with a visible warning."""
        backend = "secret_service"
        if not _secret_store(token):
            backend = "file"
            self.paths.token_file.write_text(token, encoding="utf-8")
            os.chmod(self.paths.token_file, 0o600)
            secure_file(self.paths.token_file)
        else:
            with contextlib.suppress(OSError):
                self.paths.token_file.unlink(missing_ok=True)
        await self.save(token_backend=backend)
        return backend

    def token(self) -> str:
        return self._read_token()

    def has_token(self) -> bool:
        return bool(self.token())

    def clear_token(self) -> None:
        _secret_delete()
        with contextlib.suppress(OSError):
            self.paths.token_file.unlink(missing_ok=True)
        self.data["token_backend"] = "none"
        self._write()

    # ------------------------------------------------------- operator token
    def operator_token(self) -> str:
        if self.paths.control_token_file.is_file():
            try:
                return self.paths.control_token_file.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""

    def set_operator_token(self, token: str, source: str) -> None:
        self.paths.control_token_file.write_text(token, encoding="utf-8")
        os.chmod(self.paths.control_token_file, 0o600)
        secure_file(self.paths.control_token_file)
        self.data["operator_token_source"] = source
        self._write()

    def clear_operator_token(self) -> None:
        with contextlib.suppress(OSError):
            self.paths.control_token_file.unlink(missing_ok=True)
        self.data["operator_token_source"] = ""
        self._write()

    # --------------------------------------------------------------- redaction
    def secrets_for_redaction(self) -> list[str]:
        values = [self.token()]
        operator = self.operator_token()
        if operator:
            values.append(operator)
        return [value for value in values if value]


# ------------------------------------------------------------------ transport
def guard_scheme(server: str, *, insecure_http: bool) -> str:
    """Mirror worker_client._guard_scheme before any token can be sent."""
    base = (server or "").rstrip("/")
    from urllib.parse import urlparse

    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClientError(f"Server URL must be http(s)://host[:port], got {server!r}", code="invalid_url")
    if parsed.username or parsed.password:
        raise ClientError("Server URL must not contain credentials.", code="invalid_url")
    if parsed.scheme == "http" and not is_loopback(parsed.hostname) and not insecure_http:
        raise ClientError(
            "Refusing to send the worker token over plain HTTP to a non-loopback host. "
            "Use https://, an SSH tunnel to 127.0.0.1, or explicitly enable the "
            "hotspot-only insecure HTTP switch.",
            code="insecure_transport_warning",
            status=400,
        )
    return base


def ws_url(base: str) -> str:
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base)
    scheme = {"http": "ws", "https": "wss"}[parsed.scheme]
    return urlunparse(parsed._replace(scheme=scheme))


def parse_server_detail(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "http_error", response.text[:500]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("code") or "http_error"), str(detail.get("message") or response.text[:500])
    if isinstance(detail, str):
        return "http_error", detail
    if isinstance(payload, dict) and payload.get("code"):
        return str(payload["code"]), str(payload.get("message") or "")
    return "http_error", str(payload)[:500]


MESH_MESSAGES = {
    "pairing_invalid": "The server does not recognise this pairing code. Create a fresh code on the server dashboard.",
    "pairing_used": "This pairing code was already used. Pairing codes are single-use; create a fresh one.",
    "pairing_expired": "This pairing code expired (codes live for 10 minutes). Create a fresh one and try again.",
    "protocol_mismatch": "The server and this client do not speak the same mesh protocol version.",
    "unauthorized": "The server refused the worker token.",
    "worker_revoked": "This worker was revoked on the server. Pair again with a fresh code.",
    "worker_disabled": "This worker is disabled on the server. Enable it from the device registry first.",
    "capacity": "The server is at capacity and refused the request.",
}


# --------------------------------------------------------------------- agents
@dataclass(slots=True)
class AgentState:
    kind: str
    display_name: str
    binary: str
    installed: bool = False
    path: str = ""
    version: str = ""
    availability: str = "unknown"
    auth_status: str = "unknown"
    auth_detail: str = ""
    models: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    checked_at: str = ""
    script_url: str = ""
    npm_package: str = ""
    docs_url: str = ""
    known_paths: list[str] = field(default_factory=list)
    platform: dict[str, Any] = field(default_factory=dict)
    usable_verified_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "binary": self.binary,
            "installed": self.installed,
            "path": self.path,
            "version": self.version,
            "availability": self.availability,
            "auth_status": self.auth_status,
            "auth_detail": self.auth_detail,
            "models": self.models,
            "error": self.error,
            "checked_at": self.checked_at,
            "script_url": self.script_url,
            "npm_package": self.npm_package,
            "docs_url": self.docs_url,
            "known_paths": self.known_paths,
            "platform": self.platform,
            "usable_verified_at": self.usable_verified_at,
        }


def _adapter(kind: str, path: str) -> Any:
    if kind == "opencode":
        return OpenCodeAgent(Path(path), timeout_seconds=120)
    if kind == "qoder":
        return QoderAgent(Path(path), timeout_seconds=120)
    return None


def _roster() -> dict[str, AgentState]:
    roster: dict[str, AgentState] = {}
    for kind, source in OFFICIAL_SOURCES.items():
        roster[kind] = AgentState(
            kind=kind,
            display_name=source.display_name,
            binary=source.binary_name,
            script_url=source.script_url,
            npm_package=source.npm_package,
            docs_url=source.docs_url,
            known_paths=list(source.known_paths),
        )
    return roster


# --------------------------------------------------------------- service core
class ClientService:
    def __init__(self, paths: ClientPaths) -> None:
        self.paths = paths
        self.started_at = _now()
        self.config = ConfigStore(paths)
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.worker_logs: deque[dict[str, Any]] = deque(maxlen=MAX_WORKER_LOG_LINES)
        self.worker_log_path = paths.data / "logs" / "worker.log"
        self.tasks: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, AgentState] = _roster()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.auth_sessions: dict[str, TerminalSession] = {}
        self.auth_by_kind: dict[str, str] = {}
        self.auth_history: deque[dict[str, Any]] = deque(maxlen=40)
        self.auth_exit_tasks: set[asyncio.Task[Any]] = set()
        self.sockets: set[WebSocket] = set()
        self.worker_process: asyncio.subprocess.Process | None = None
        self.worker_reader: asyncio.Task[None] | None = None
        self.worker_stderr: asyncio.Task[None] | None = None
        self.worker_status = "stopped"
        self.worker_owner = "none"
        self.worker_pid: int | None = None
        self.worker_started_at = ""
        self.worker_hello: dict[str, Any] = {}
        self.worker_reconnect: dict[str, Any] | None = None
        self.worker_error = ""
        self.last_heartbeat_at = ""
        self.last_hello_at = ""
        self.tasks_pending = 0
        self.connection_latency_ms: int | None = None
        self.connection_checked_at = ""
        self.connection_error = ""
        self.server_me: dict[str, Any] = {}
        self.server_status: dict[str, Any] = {}
        self.protocol_probe: dict[str, Any] = {}
        self.operator = {
            "available": False,
            "error": "",
            "last_refresh": "",
            "projects": [],
            "tasks": [],
            "workers": [],
            "events": [],
            "documents": [],
            "counts": {},
        }
        self.unit_active = False
        self.unit_checked_at = 0.0
        self._cpu_sample: tuple[float, float] | None = None
        self._cpu_percent: float | None = None
        self._poller: asyncio.Task[None] | None = None
        self._closing = False
        self.agent_lock = asyncio.Lock()
        self.agent_locks = {kind: asyncio.Lock() for kind in OFFICIAL_SOURCES}
        self.settings = ControlSettings(
            data_dir=self.paths.data,
            config_dir=self.paths.config,
        )
        self.installer = AgentInstaller(self.settings, on_stage=self._install_stage)

    # ------------------------------------------------------------------ events
    def _redact(self, text: str) -> str:
        value = str(text or "")
        for secret in self.config.secrets_for_redaction():
            if secret:
                value = value.replace(secret, "[redacted]")
        return redact_output(value)[:12000]

    def log_worker(self, line: str, *, level: str = "info") -> None:
        clean = self._redact(line).rstrip()
        if not clean:
            return
        entry = {"at": _now(), "level": level, "text": clean}
        self.worker_logs.append(entry)
        try:
            with self.worker_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{entry['at']}] {level}: {clean}\n")
        except OSError:
            pass

    def emit(self, event_kind: str, message: str, *, level: str = "info", **fields: Any) -> dict[str, Any]:
        entry = {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "at": _now(),
            "kind": event_kind,
            "level": level,
            "message": self._redact(message),
            "fields": {key: value for key, value in fields.items()},
        }
        self.events.append(entry)
        payload = {"type": "event", "event": entry}
        for client_socket in list(self.sockets):
            asyncio.create_task(self._safe_send(client_socket, payload))
        return entry

    async def _safe_send(self, socket: WebSocket, payload: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await socket.send_json(payload)

    # -------------------------------------------------------------- connection
    def connection_snapshot(self) -> dict[str, Any]:
        paired = self.config.configured and self.config.has_token()
        state = "unpaired"
        if paired:
            if self.worker_status == "connected":
                state = "connected"
            elif self.worker_status == "reconnecting":
                state = "reconnecting"
            elif self.worker_status in {"protocol_mismatch", "revoked"}:
                state = self.worker_status
            elif self.worker_status == "failed":
                state = "offline"
            elif self.worker_status in {"connecting", "starting"}:
                state = "connecting"
            elif self.unit_active and self.server_me.get("status") == "online":
                state = "connected"
            else:
                state = "offline"
        worker_token = self.config.token()
        return {
            "state": state,
            "paired": paired,
            "server_url": self.config.data.get("server_url", ""),
            "worker_id": self.config.data.get("worker_id", ""),
            "worker_name": self.config.data.get("worker_name", ""),
            "token_fingerprint": token_fingerprint(worker_token),
            "token_backend": self.config.data.get("token_backend", "none"),
            "token_warning": (
                "The worker token is stored in a 0600 file because Secret Service "
                "(secret-tool) is unavailable. Install libsecret-tools for the "
                "recommended store."
                if self.config.data.get("token_backend") == "file"
                else ""
            ),
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_hello_at": self.last_hello_at,
            "latency_ms": self.connection_latency_ms,
            "checked_at": self.connection_checked_at,
            "error": self.connection_error,
            "protocol_version": MESH_PROTOCOL_VERSION,
            "protocol_match": self.worker_hello.get("protocol_match"),
            "insecure_http": bool(self.config.data.get("insecure_http")),
            "insecure_warning": (
                "Plain HTTP is enabled for a non-loopback server. The worker token "
                "travels in clear text on this network."
                if self.config.data.get("insecure_http")
                else ""
            ),
            "operator_mode": bool(self.config.operator_token()),
            "operator_token_source": self.config.data.get("operator_token_source", ""),
            "me": self.server_me,
            "server_status": {
                "version": (self.server_status or {}).get("version", ""),
                "service": (self.server_status or {}).get("service", ""),
            },
        }

    async def _probe_http(self) -> None:
        base = self.config.data.get("server_url", "")
        token = self.config.token()
        if not base or not token:
            return
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{base}/api/mesh/me", headers={"X-Worker-Token": token}
                )
            self.connection_latency_ms = int((time.monotonic() - started) * 1000)
            self.connection_checked_at = _now()
            if response.status_code == 200:
                self.server_me = response.json()
                self.connection_error = ""
                last = self.server_me.get("last_heartbeat_at") or ""
                if last:
                    self.last_heartbeat_at = last
            elif response.status_code in (401, 403):
                code, message = parse_server_detail(response)
                self.connection_error = MESH_MESSAGES.get(code, message)
                if code in {"worker_revoked", "worker_disabled", "unauthorized"}:
                    self.worker_status = "revoked" if code != "worker_disabled" else "offline"
            else:
                code, message = parse_server_detail(response)
                self.connection_error = message or f"HTTP {response.status_code}"
        except (httpx.HTTPError, OSError) as exc:
            self.connection_latency_ms = None
            self.connection_checked_at = _now()
            self.connection_error = f"Server unreachable: {exc}"

    async def connection_test(self) -> dict[str, Any]:
        base = self.config.data.get("server_url", "")
        if not base:
            raise ClientError("Pair this device before testing the connection.", code="unpaired")
        insecure = bool(self.config.data.get("insecure_http"))
        base = guard_scheme(base, insecure_http=insecure)
        token = self.config.token()
        result: dict[str, Any] = {
            "server_url": base,
            "tested_at": _now(),
            "tls": "loopback" if is_loopback(httpx.URL(base).host) else ("https" if base.startswith("https") else "insecure_http"),
            "reachable": False,
            "latency_ms": None,
            "authenticated": False,
            "worker_id": "",
            "worker_status": "",
            "protocol_version": MESH_PROTOCOL_VERSION,
            "server_protocol_version": None,
            "protocol_match": None,
            "last_heartbeat_at": "",
            "heartbeat_age_seconds": None,
            "server_version": "",
            "device_to_device": (
                "Same machine: loopback traffic cannot be blocked by hotspot client isolation."
                if is_loopback(httpx.URL(base).host)
                else "Separate devices: a hotspot with AP/client isolation will break the mesh "
                "even when HTTP works. Use the same LAN without isolation, or an SSH tunnel."
            ),
            "error": "",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base}/api/mesh/me", headers={"X-Worker-Token": token})
            result["reachable"] = True
            result["latency_ms"] = int((time.monotonic() - started) * 1000)
            if response.status_code == 200:
                me = response.json()
                self.server_me = me
                result["authenticated"] = True
                result["worker_id"] = me.get("worker_id", "")
                result["worker_status"] = me.get("status", "")
                result["last_heartbeat_at"] = me.get("last_heartbeat_at") or ""
                if me.get("last_heartbeat_at"):
                    with contextlib.suppress(ValueError):
                        stamp = datetime.fromisoformat(
                            str(me["last_heartbeat_at"]).replace("Z", "+00:00")
                        )
                        result["heartbeat_age_seconds"] = int(
                            (datetime.now(UTC) - stamp).total_seconds()
                        )
            else:
                code, message = parse_server_detail(response)
                result["error"] = MESH_MESSAGES.get(code, message)
                result["code"] = code
        except (httpx.HTTPError, OSError) as exc:
            result["error"] = f"Server unreachable: {exc}"
            return result

        # /api/status is a dashboard route: only an operator-mode client may read it.
        operator_token = self.config.operator_token()
        if operator_token:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    status_response = await client.get(
                        f"{base}/api/status", headers={"X-API-Token": operator_token}
                    )
                if status_response.status_code == 200:
                    status_payload = status_response.json()
                    self.server_status = status_payload
                    result["server_version"] = status_payload.get("version", "")
            except (httpx.HTTPError, OSError):
                pass

        if self.worker_status == "connected" and self.worker_hello:
            result["server_protocol_version"] = self.worker_hello.get("protocol_version")
            result["protocol_match"] = bool(self.worker_hello.get("protocol_match"))
        else:
            probe = await self._probe_protocol(base, token)
            result["server_protocol_version"] = probe.get("protocol_version")
            result["protocol_match"] = probe.get("protocol_match")
            if probe.get("error"):
                result["error"] = probe["error"]

        self.connection_latency_ms = result["latency_ms"]
        self.connection_checked_at = result["tested_at"]
        self.connection_error = result["error"]
        self.connection_test_cache = result
        self.emit(
            "connection.tested",
            f"Connection test: reachable={result['reachable']} authenticated={result['authenticated']}",
            latency_ms=result["latency_ms"],
        )
        return result

    connection_test_cache: dict[str, Any] = {}

    async def _probe_protocol(self, base: str, token: str) -> dict[str, Any]:
        """Open one short worker socket and read the server's hello.

        Never called while the supervised worker is connected: a second socket
        would replace the live one. It is only used to prove the protocol
        version before the first start.
        """
        import websockets

        url = f"{ws_url(base)}/ws/worker?worker_token={token}"
        try:
            async with websockets.connect(url, open_timeout=10) as socket:
                raw = await asyncio.wait_for(socket.recv(), timeout=10)
                hello = json.loads(raw)
                if hello.get("type") != "hello":
                    return {"error": f"Expected hello, got {hello.get('type')!r}"}
                protocol = hello.get("protocol_version")
                return {
                    "protocol_version": protocol,
                    "protocol_match": protocol == MESH_PROTOCOL_VERSION,
                    "heartbeat_seconds": hello.get("heartbeat_seconds"),
                    "offline_after_seconds": hello.get("offline_after_seconds"),
                }
        except Exception as exc:  # noqa: BLE001 - surfaced, never guessed
            return {"error": f"Protocol probe failed: {exc}"}

    # ------------------------------------------------------------------ pairing
    async def pair(
        self,
        *,
        server: str,
        code: str,
        name: str,
        insecure_http: bool,
        insecure_confirmation: str,
    ) -> dict[str, Any]:
        if insecure_http and not is_loopback(httpx.URL(server).host):
            if insecure_confirmation.strip() != "ALLOW PLAIN HTTP":
                raise ClientError(
                    "Enabling plain HTTP for a non-loopback host requires typing "
                    "ALLOW PLAIN HTTP. It sends the worker token in clear text.",
                    code="confirmation_required",
                )
            await self.config.save(insecure_http=True, insecure_acknowledged=True)
        base = guard_scheme(server, insecure_http=insecure_http or bool(self.config.data.get("insecure_http")))
        clean_code = code.strip().upper()
        if not clean_code:
            raise ClientError("Enter the pairing code shown on the server dashboard.", code="pairing_invalid")
        try:
            inventory = await collect_inventory(name)
        except Exception as exc:  # noqa: BLE001 - local detection failure is real
            raise ClientError(f"Local inventory failed: {exc}", code="inventory_failed") from exc
        inventory["device_key"] = self.config.data["device_key"]
        inventory["name"] = name.strip() or self.config.data["worker_name"]
        inventory["pairing_code"] = clean_code
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(f"{base}/api/mesh/register", json=inventory)
        except (httpx.HTTPError, OSError) as exc:
            raise ClientError(f"Could not reach {base}: {exc}", code="server_unreachable") from exc
        if response.status_code != 200:
            code_name, message = parse_server_detail(response)
            raise ClientError(
                MESH_MESSAGES.get(code_name, message), code=code_name, status=400
            )
        payload = response.json()
        token = str(payload.get("token") or "")
        worker_id = str(payload.get("worker_id") or "")
        if not token or not worker_id:
            raise ClientError(
                "The server accepted registration but returned no worker token. "
                "Pairing codes are single-use; create a fresh code.",
                code="pairing_no_token",
            )
        backend = await self.config.set_token(token)
        await self.config.save(
            server_url=base,
            worker_id=worker_id,
            worker_name=inventory["name"],
            insecure_http=bool(insecure_http or self.config.data.get("insecure_http")),
        )
        self.connection_latency_ms = int((time.monotonic() - started) * 1000)
        self.connection_checked_at = _now()
        self.connection_error = ""
        self.emit(
            "pairing.completed",
            f"Paired as {worker_id}. Token fingerprint {token_fingerprint(token)}.",
            worker_id=worker_id,
            token_backend=backend,
        )
        await self._probe_http()
        # Pairing starts the GUI-owned loop unless the systemd unit already owns it.
        try:
            await self.start_worker(takeover=False, auto=True)
        except ClientError as exc:
            self.emit("worker.start_refused", exc.message, level="warning", code=exc.code)
        return self.snapshot()

    async def unpair(self, *, confirm: str, revoke_remote: bool) -> dict[str, Any]:
        if confirm.strip() != "UNPAIR":
            raise ClientError(
                "Type UNPAIR to remove this device's worker token.", code="confirmation_required"
            )
        worker_id = self.config.data.get("worker_id", "")
        revoked = False
        if revoke_remote and worker_id and self.config.operator_token():
            try:
                await self.operator_request("POST", f"/api/workers/{worker_id}/revoke")
                revoked = True
            except ClientError as exc:
                self.emit("device.revoke_failed", exc.message, level="warning")
        await self.stop_worker(confirm=True)
        self.config.clear_token()
        await self.config.save(server_url="", worker_id="")
        self.worker_status = "stopped"
        self.worker_hello = {}
        self.server_me = {}
        self.emit(
            "device.unpaired",
            "Unpaired. The device key was kept; the worker token was wiped locally"
            + (" and revoked on the server." if revoked else ". Server-side revocation needs operator mode."),
            worker_id=worker_id,
            revoked=revoked,
        )
        return {"ok": True, "revoked": revoked, "state": self.snapshot()}

    # ------------------------------------------------------------- worker loop
    async def _unit_active_now(self) -> bool:
        if time.monotonic() - self.unit_checked_at < 8:
            return self.unit_active
        self.unit_checked_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "is-active",
                "forgemesh-worker",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(process.communicate(), timeout=5)
            self.unit_active = out.decode().strip() == "active"
        except (OSError, TimeoutError):
            self.unit_active = False
        return self.unit_active

    async def _take_over_unit(self) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "stop",
                "forgemesh-worker",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            if process.returncode == 0:
                self.emit("worker.unit_stopped", "Stopped the systemd worker unit so the GUI can own the loop.")
                self.unit_active = False
                return True
            raise ClientError(
                f"Could not stop forgemesh-worker.service: {out.decode(errors='replace')[:300]}",
                code="unit_stop_failed",
            )
        except (OSError, TimeoutError) as exc:
            raise ClientError(f"Could not stop the systemd unit: {exc}", code="unit_stop_failed") from exc

    def _python_command(self) -> list[str]:
        package_root = Path(__file__).resolve().parents[1]
        env_python = os.environ.get("FMCM_PYTHON", "")
        if env_python and Path(env_python).is_file():
            return [env_python, "-m", "forgemesh_control.client_worker"]
        repo = package_root.parent
        for candidate in (
            Path("/opt/forgemesh/venv/bin/python"),
            repo / ".venv" / "bin" / "python",
            Path(sys.executable),
        ):
            if candidate.is_file():
                return [str(candidate), "-m", "forgemesh_control.client_worker"]
        return [sys.executable, "-m", "forgemesh_control.client_worker"]

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        package_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(package_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env["FMW_SERVER"] = self.config.data.get("server_url", "")
        env["FMW_WORKER_TOKEN"] = self.config.token()
        env["FMW_DEVICE_KEY"] = self.config.data["device_key"]
        env["FMCM_TASK_TIMEOUT"] = "1800"
        # The daemon already validated the scheme before spawning; the child
        # needs the same opt-in or it refuses the connection it was asked to make.
        if self.config.data.get("insecure_http"):
            env["FMW_INSECURE_HTTP"] = "1"
        else:
            env.pop("FMW_INSECURE_HTTP", None)
        return env

    async def start_worker(self, *, takeover: bool, auto: bool = False) -> dict[str, Any]:
        if not self.config.configured or not self.config.has_token():
            raise ClientError("Pair this device first.", code="unpaired")
        if self.worker_process is not None and self.worker_process.returncode is None:
            return {"ok": True, "already": True, "state": self.snapshot()}
        unit = await self._unit_active_now()
        if unit:
            self.worker_owner = "systemd"
            if not takeover:
                raise ClientError(
                    "forgemesh-worker.service is already running and owns the single "
                    "worker connection for this device. Stop it first, or let the GUI "
                    "take over.",
                    code="unit_active",
                )
            await self._take_over_unit()
        guard_scheme(
            self.config.data.get("server_url", ""),
            insecure_http=bool(self.config.data.get("insecure_http")),
        )
        command = self._python_command()
        env = self._child_env()
        self.worker_status = "starting"
        self.worker_error = ""
        self.worker_owner = "gui"
        self.emit("worker.starting", "Starting the supervised worker loop.", auto=auto)
        try:
            self.worker_process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.paths.data),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self.worker_status = "failed"
            self.worker_error = str(exc)
            raise ClientError(f"Could not start the worker process: {exc}", code="spawn_failed") from exc
        self.worker_pid = self.worker_process.pid
        self.worker_started_at = _now()
        self.worker_reader = asyncio.create_task(self._read_worker(self.worker_process))
        self.worker_stderr = asyncio.create_task(self._read_worker_stderr(self.worker_process))
        self.worker_status = "connecting"
        return {"ok": True, "pid": self.worker_pid, "command": " ".join(command), "state": self.snapshot()}

    async def stop_worker(self, *, confirm: bool = False, stop_unit: bool = False) -> dict[str, Any]:
        if stop_unit and self.unit_active:
            if not confirm:
                raise ClientError("Stopping the systemd unit needs confirm=true.", code="confirmation_required")
            await self._take_over_unit()
        process = self.worker_process
        if process is not None and process.returncode is None:
            self.worker_status = "stopping"
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        self.worker_process = None
        self.worker_pid = None
        self.worker_status = "stopped"
        self.worker_owner = "none"
        self.worker_hello = {}
        self.worker_reconnect = None
        self.worker_started_at = ""
        self.emit("worker.stopped", "GUI-owned worker loop stopped.")
        return {"ok": True, "state": self.snapshot()}

    async def restart_worker(self, *, takeover: bool) -> dict[str, Any]:
        await self.stop_worker(confirm=True)
        return await self.start_worker(takeover=takeover)

    async def _read_worker(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                self.log_worker(line)
                continue
            await self._handle_worker_event(event)
        code = await process.wait()
        if self.worker_process is process:
            self.worker_process = None
            self.worker_pid = None
            if self.worker_status not in {"stopped", "revoked", "protocol_mismatch"}:
                self.worker_status = "failed" if code not in (0, None) else "stopped"
            self.emit(
                "worker.exited",
                f"Worker process exited with code {code}.",
                level="error" if code not in (0, None) else "info",
                exit_code=code,
            )

    async def _read_worker_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            raw = await process.stderr.readline()
            if not raw:
                break
            self.log_worker(raw.decode("utf-8", errors="replace").rstrip(), level="warn")

    async def _handle_worker_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "connecting":
            self.worker_status = "connecting"
            self.worker_reconnect = None
            self.emit("worker.connecting", "Opening the worker WebSocket…")
        elif kind == "socket_open":
            self.emit("worker.socket_open", "Worker WebSocket opened; waiting for hello.")
        elif kind == "hello":
            self.worker_hello = event
            self.last_hello_at = event.get("at") or _now()
            if not event.get("protocol_match"):
                self.worker_status = "protocol_mismatch"
                self.worker_error = (
                    f"Server protocol {event.get('protocol_version')} vs client "
                    f"{event.get('local_protocol_version')}."
                )
                self.emit("worker.protocol_mismatch", self.worker_error, level="error")
            else:
                self.emit(
                    "worker.hello",
                    f"Server hello received (protocol {event.get('protocol_version')}).",
                    worker_id=event.get("worker_id"),
                )
        elif kind == "connected":
            self.worker_status = "connected"
            self.worker_reconnect = None
            self.worker_error = ""
            self.last_heartbeat_at = event.get("at") or _now()
            self.emit(
                "worker.connected",
                f"Connected as {event.get('worker_id')}.",
                worker_id=event.get("worker_id"),
            )
        elif kind == "heartbeat_ack":
            self.last_heartbeat_at = event.get("at") or _now()
            self.tasks_pending = int(event.get("tasks_pending") or 0)
        elif kind == "reconnecting":
            self.worker_status = "reconnecting"
            self.worker_reconnect = {
                "attempt": event.get("attempt"),
                "delay_seconds": event.get("delay_seconds"),
                "at": event.get("at"),
            }
            self.emit(
                "worker.reconnecting",
                f"Reconnecting in {event.get('delay_seconds')}s (attempt {event.get('attempt')}).",
                level="warning",
                attempt=event.get("attempt"),
                delay_seconds=event.get("delay_seconds"),
            )
        elif kind == "disconnected":
            self.worker_status = "offline"
            self.emit("worker.disconnected", event.get("message") or "Worker disconnected.", level="warning")
        elif kind == "mesh_unavailable":
            self.worker_status = "offline"
            self.worker_error = event.get("message") or "Mesh unavailable"
            self.emit("worker.mesh_unavailable", self.worker_error, level="warning")
        elif kind == "revoked":
            self.worker_status = "revoked"
            self.worker_error = event.get("message") or "Worker token refused."
            self.emit("worker.revoked", self.worker_error, level="error")
        elif kind == "protocol_mismatch":
            self.worker_status = "protocol_mismatch"
            self.worker_error = event.get("message") or "Protocol mismatch."
            self.emit("worker.protocol_mismatch", self.worker_error, level="error")
        elif kind == "worker_error":
            self.worker_error = event.get("message") or event.get("code") or "Worker error"
            self.emit(
                "worker.error",
                f"{event.get('code')}: {self.worker_error}",
                level="error",
                code=event.get("code"),
            )
        elif kind == "task_assigned":
            task_id = str(event.get("task_id") or "")
            self.tasks[task_id] = {
                **self.tasks.get(task_id, {}),
                "id": task_id,
                "title": event.get("title") or "",
                "project_id": event.get("project_id") or "",
                "agent_kind": event.get("agent_kind") or "",
                "local_status": "running",
                "progress": 0,
                "message": "assigned",
                "started_at": event.get("at") or _now(),
                "updated_at": event.get("at") or _now(),
            }
            self.emit("task.assigned", f"Task {task_id} assigned to this laptop.", task_id=task_id)
        elif kind == "task_progress":
            task_id = str(event.get("task_id") or "")
            entry = self.tasks.setdefault(task_id, {"id": task_id})
            entry.update(
                {
                    "local_status": "running",
                    "progress": event.get("percent"),
                    "message": event.get("message") or "",
                    "updated_at": event.get("at") or _now(),
                }
            )
        elif kind == "task_result":
            task_id = str(event.get("task_id") or "")
            entry = self.tasks.setdefault(task_id, {"id": task_id})
            entry.update(
                {
                    "local_status": event.get("status"),
                    "progress": 100,
                    "error": event.get("error"),
                    "output": event.get("output") or "",
                    "output_chars": event.get("output_chars"),
                    "finished_at": event.get("at") or _now(),
                    "updated_at": event.get("at") or _now(),
                }
            )
            self.emit(
                "task.result",
                f"Task {task_id} reported {event.get('status')}.",
                level="info" if event.get("status") == "succeeded" else "error",
                task_id=task_id,
                status=event.get("status"),
            )
            asyncio.create_task(self.refresh_operator())
        elif kind == "stopped":
            self.worker_status = "stopped"
        else:
            self.log_worker(json.dumps(event, ensure_ascii=False))

    # -------------------------------------------------------------- snapshots
    def _merged_tasks(self) -> list[dict[str, Any]]:
        server_tasks = self.operator.get("tasks") or []
        by_id: dict[str, dict[str, Any]] = {}
        for task in server_tasks:
            if isinstance(task, dict) and task.get("id"):
                by_id[str(task["id"])] = dict(task)
        for task_id, local in self.tasks.items():
            merged = by_id.get(task_id, {"id": task_id})
            merged.update({key: value for key, value in local.items() if value not in (None, "")})
            by_id[task_id] = merged
        tasks = list(by_id.values())
        tasks.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return tasks

    def agent_inventory(self) -> list[dict[str, Any]]:
        return [self.agents[kind].to_dict() for kind in OFFICIAL_SOURCES]

    def _system_stats(self) -> dict[str, Any]:
        """Real local CPU/memory numbers, sampled from /proc.

        The renderer keeps a short history for the sparklines. Nothing here is
        invented: if /proc is unavailable the field is null and the UI says so.
        """
        cpu_percent: float | None = None
        try:
            fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
            values = [float(item) for item in fields]
            idle = values[3] + (values[4] if len(values) > 4 else 0.0)
            total = sum(values)
            if self._cpu_sample is not None:
                idle_delta = idle - self._cpu_sample[0]
                total_delta = total - self._cpu_sample[1]
                if total_delta > 0:
                    cpu_percent = round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100)), 1)
                    self._cpu_percent = cpu_percent
            self._cpu_sample = (idle, total)
        except (OSError, ValueError, IndexError):
            cpu_percent = self._cpu_percent
        mem_total = mem_available = None
        try:
            info: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, rest = line.partition(":")
                if key in {"MemTotal", "MemAvailable"}:
                    info[key] = int(rest.strip().split()[0]) * 1024
            mem_total = info.get("MemTotal")
            mem_available = info.get("MemAvailable")
        except (OSError, ValueError, IndexError):
            pass
        load = None
        try:
            load = [round(value, 2) for value in os.getloadavg()]
        except (OSError, AttributeError):
            pass
        mem_used = (mem_total - mem_available) if mem_total and mem_available else None
        return {
            "cpu_percent": cpu_percent,
            "mem_total_bytes": mem_total,
            "mem_used_bytes": mem_used,
            "mem_percent": (
                round(mem_used / mem_total * 100, 1) if mem_used is not None and mem_total else None
            ),
            "load": load,
            "cpu_count": os.cpu_count(),
            "sampled_at": _now(),
        }

    def snapshot(self) -> dict[str, Any]:
        worker = {
            "status": self.worker_status,
            "owner": "systemd" if self.unit_active else (self.worker_owner if self.worker_pid else "none"),
            "pid": self.worker_pid,
            "started_at": self.worker_started_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_hello_at": self.last_hello_at,
            "hello": self.worker_hello,
            "reconnect": self.worker_reconnect,
            "error": self.worker_error,
            "tasks_pending": self.tasks_pending,
            "unit_active": self.unit_active,
            "logs": list(self.worker_logs)[-200:],
        }
        return {
            "app": {
                "name": APP_NAME,
                "version": VERSION,
                "protocol_version": MESH_PROTOCOL_VERSION,
                "started_at": self.started_at,
                "platform": platform.platform(),
                "arch": platform.machine(),
                "pty": HAS_PTY,
                "python": platform.python_version(),
                "paths": {
                    "config": str(self.paths.config),
                    "data": str(self.paths.data),
                    "workspaces": str(self.paths.data / "workspaces"),
                    "artifacts": str(self.paths.data / "artifacts"),
                    "logs": str(self.paths.data / "logs"),
                },
                "device_key_fingerprint": hashlib.sha256(
                    self.config.data["device_key"].encode()
                ).hexdigest()[:8],
            },
            "connection": self.connection_snapshot(),
            "system": self._system_stats(),
            "worker": worker,
            "agents": self.agent_inventory(),
            "tasks": self._merged_tasks(),
            "events": list(self.events)[-200:],
            "auth_history": list(self.auth_history),
            "operator": {
                "available": bool(self.config.operator_token()),
                "error": self.operator.get("error", ""),
                "last_refresh": self.operator.get("last_refresh", ""),
                "counts": self.operator.get("counts", {}),
                "projects": self.operator.get("projects", []),
                "workers": self.operator.get("workers", []),
                "documents": self.operator.get("documents", []),
                "events": self.operator.get("events", []),
                "protocol_gaps": [
                    "A worker token cannot read /api/projects, /api/tasks, /api/delivery or "
                    "/api/workers. Those screens need optional operator mode with the server "
                    "control token; the worker token is never sent to them.",
                    "There is no artifact download endpoint. The Client Manager can only "
                    "verify/copy an artifact path that is readable on this machine.",
                    "There is no endpoint that converts free-text change feedback into a new "
                    "task. Review feedback is stored in the approval decision note and a "
                    "server-side review report is generated.",
                ],
            },
            "security": {
                "insecure_http": bool(self.config.data.get("insecure_http")),
                "secret_backend": self.config.data.get("token_backend"),
                "secret_tool_available": bool(_secret_tool()),
                "has_worker_token": self.config.has_token(),
                "has_operator_token": bool(self.config.operator_token()),
            },
        }

    # -------------------------------------------------------------- agent work
    async def detect_agent(self, kind: str) -> dict[str, Any]:
        if kind not in OFFICIAL_SOURCES:
            raise ClientError(f"Unknown agent {kind!r}.", code="unknown_agent", status=404)
        async with self.agent_locks[kind]:
            state = self.agents[kind]
            platform_info = detect_platform(kind)
            state.platform = platform_info.to_dict()
            try:
                detection = await self.installer.detect(kind)
            except Exception as exc:  # noqa: BLE001 - report the real failure
                state.checked_at = _now()
                state.error = f"Detection failed: {exc}"
                state.installed = False
                state.availability = "unknown"
                return state.to_dict()
            state.installed = detection.installed
            state.path = detection.path
            state.version = detection.version
            state.error = detection.error or ""
            state.checked_at = _now()
            state.models = []
            state.auth_status = "unknown"
            state.auth_detail = ""
            state.availability = "unknown"
            if detection.installed:
                state.availability = "detected"
                adapter = _adapter(kind, detection.path)
                if adapter is not None:
                    try:
                        status, detail = await asyncio.wait_for(adapter.check_auth(), timeout=45)
                        state.auth_status = status
                        state.auth_detail = self._redact(detail)
                        if status == "authenticated":
                            state.availability = "authenticated"
                    except Exception as exc:  # noqa: BLE001
                        state.auth_detail = f"Auth check failed: {exc}"
                    try:
                        models = await asyncio.wait_for(adapter.list_models(), timeout=50)
                        state.models = [
                            {
                                "provider": item.split("/", 1)[0] if "/" in item else kind,
                                "model_id": item.split("/", 1)[1] if "/" in item else item,
                                "display_name": item,
                                "availability": "unknown",
                            }
                            for item in models
                        ]
                    except Exception as exc:  # noqa: BLE001
                        state.models = []
                        state.error = f"Model listing failed: {exc}"
                usable_path = str(self.config.data.get("usable_paths", {}).get(kind) or "")
                verified_at = str(self.config.data.get("usable", {}).get(kind) or "")
                if usable_path and usable_path == detection.path and verified_at:
                    state.availability = "usable"
                    state.usable_verified_at = verified_at
            self.emit(
                "agent.detected",
                f"{state.display_name}: "
                + (
                    f"{state.version} at {state.path} ({state.availability})"
                    if state.installed
                    else "not installed"
                ),
                kind=kind,
            )
            return state.to_dict()

    async def detect_all(self) -> list[dict[str, Any]]:
        await asyncio.gather(*(self.detect_agent(kind) for kind in OFFICIAL_SOURCES), return_exceptions=True)
        return self.agent_inventory()

    def _new_job(self, kind: str, operation: str) -> dict[str, Any]:
        job = {
            "id": f"job_{uuid.uuid4().hex[:10]}",
            "kind": kind,
            "operation": operation,
            "status": "running",
            "stage": "starting",
            "message": "",
            "log": [],
            "result": None,
            "error": "",
            "started_at": _now(),
            "ended_at": "",
            "log_path": "",
        }
        self.jobs[job["id"]] = job
        return job

    def _job_log(self, job: dict[str, Any], message: str) -> None:
        clean = self._redact(message)
        job["log"].append({"at": _now(), "text": clean})
        if len(job["log"]) > MAX_JOB_LOG_LINES:
            del job["log"][: len(job["log"]) - MAX_JOB_LOG_LINES]

    async def _install_stage(self, kind: str, stage: str, message: str, data: dict[str, Any]) -> None:
        job = self._active_job(kind, {"install", "update"})
        if job is None:
            return
        job["stage"] = stage
        job["message"] = self._redact(message)
        self._job_log(job, f"[{stage}] {message}")
        if data.get("path"):
            job["result"] = {**(job.get("result") or {}), "path": data["path"]}
        if data.get("version"):
            job["result"] = {**(job.get("result") or {}), "version": data["version"]}
        if data.get("log_path"):
            job["log_path"] = str(data["log_path"])
        if stage == "failed":
            job["status"] = "failed"
            job["error"] = self._redact(data.get("error") or message)
            job["ended_at"] = _now()
        self.emit("agent.install.stage", job["message"], kind=kind, stage=stage, job_id=job["id"])

    def _active_job(self, kind: str, operations: set[str]) -> dict[str, Any] | None:
        for job in reversed(list(self.jobs.values())):
            if job["kind"] == kind and job["operation"] in operations and job["status"] == "running":
                return job
        return None

    async def install_agent(self, kind: str, *, force: bool, operation: str, approve: bool) -> dict[str, Any]:
        if kind not in OFFICIAL_SOURCES:
            raise ClientError(f"Unknown agent {kind!r}.", code="unknown_agent", status=404)
        async with self.agent_locks[kind]:
            if operation == "update":
                if not approve:
                    raise ClientError(
                        "Updating a CLI needs explicit approval. Confirm the update command first.",
                        code="approval_required",
                    )
                job = self._new_job(kind, "update")
                asyncio.create_task(self._run_update(job, kind))
            else:
                job = self._new_job(kind, "install")
                asyncio.create_task(self._run_install(job, kind, force=force))
        return self.job_view(job["id"], offset=0)

    async def _run_install(self, job: dict[str, Any], kind: str, *, force: bool) -> None:
        try:
            result = await self.installer.install(kind, force=force)
            job["result"] = {
                "ok": result.ok,
                "stage": result.stage,
                "path": result.path,
                "version": result.version,
                "source": result.source,
                "duration_ms": result.duration_ms,
                "log_path": result.log_path,
                "log_tail": self._redact(result.log_tail),
            }
            job["status"] = "succeeded" if result.ok else "failed"
            job["stage"] = result.stage
            job["error"] = self._redact(result.error or "")
            job["ended_at"] = _now()
            if result.source:
                self._job_log(job, f"Install route used: {result.source}")
            await self.detect_agent(kind)
            await self.report_inventory(reason=f"after {kind} install")
        except Exception as exc:  # noqa: BLE001 - the job records the real failure
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["ended_at"] = _now()
            self._job_log(job, f"[failed] {job['error']}")
        finally:
            self.emit(
                "agent.install.finished",
                f"{kind} {job['operation']} {job['status']}.",
                level="info" if job["status"] == "succeeded" else "error",
                kind=kind,
                job_id=job["id"],
            )

    async def _run_update(self, job: dict[str, Any], kind: str) -> None:
        try:
            result = await self.installer.update(kind, approve=True)
            job["result"] = {
                "ok": result.ok,
                "stage": result.stage,
                "path": result.path,
                "version": result.version,
                "source": result.source,
                "duration_ms": result.duration_ms,
                "log_path": result.log_path,
                "log_tail": self._redact(result.log_tail),
            }
            job["status"] = "succeeded" if result.ok else "failed"
            job["error"] = self._redact(result.error or "")
            job["stage"] = result.stage
            job["ended_at"] = _now()
            await self.detect_agent(kind)
            await self.report_inventory(reason=f"after {kind} update")
        except Exception as exc:  # noqa: BLE001
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["ended_at"] = _now()
            self._job_log(job, f"[failed] {job['error']}")
        finally:
            self.emit(
                "agent.update.finished",
                f"{kind} update {job['status']}.",
                level="info" if job["status"] == "succeeded" else "error",
                kind=kind,
                job_id=job["id"],
            )

    async def verify_agent(self, kind: str) -> dict[str, Any]:
        if kind not in OFFICIAL_SOURCES:
            raise ClientError(f"Unknown agent {kind!r}.", code="unknown_agent", status=404)
        state = self.agents[kind]
        if not state.installed or not state.path:
            raise ClientError(
                f"{state.display_name} is not installed, so there is nothing to verify.",
                code="not_installed",
            )
        job = self._new_job(kind, "verify")
        asyncio.create_task(self._run_verify(job, kind))
        return self.job_view(job["id"], offset=0)

    async def _run_verify(self, job: dict[str, Any], kind: str) -> None:
        state = self.agents[kind]
        job["stage"] = "verifying"
        self._job_log(job, f"Running a real functional prompt with {state.path}")
        self._job_log(job, f"Prompt: {VERIFY_PROMPT}")
        try:
            adapter = _adapter(kind, state.path)

            def on_line(line: str) -> None:
                self._job_log(job, line)

            result = await adapter.submit_task(
                VERIFY_PROMPT,
                cwd=self.paths.config / "agent-config",
                on_line=on_line,
                timeout_seconds=120,
            )
            ok = bool(result.ok and VERIFY_TOKEN in (result.text or ""))
            job["result"] = {
                "ok": ok,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "model": result.model,
                "token_seen": VERIFY_TOKEN in (result.text or ""),
                "output_chars": len(result.text or ""),
                "error": result.error,
            }
            job["status"] = "succeeded" if ok else "failed"
            job["error"] = "" if ok else (result.error or "The CLI answered but did not return the verify token.")
            if ok:
                usable = dict(self.config.data.get("usable") or {})
                usable_paths = dict(self.config.data.get("usable_paths") or {})
                usable[kind] = _now()
                usable_paths[kind] = state.path
                await self.config.save(usable=usable, usable_paths=usable_paths)
                state.availability = "usable"
                state.usable_verified_at = usable[kind]
            job["ended_at"] = _now()
            await self.detect_agent(kind)
            await self.report_inventory(reason=f"after {kind} functional verify")
        except Exception as exc:  # noqa: BLE001 - recorded, not hidden
            job["status"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["ended_at"] = _now()
            self._job_log(job, f"[failed] {job['error']}")
        finally:
            self.emit(
                "agent.verify.finished",
                f"{kind} functional verification {job['status']}.",
                level="info" if job["status"] == "succeeded" else "error",
                kind=kind,
                job_id=job["id"],
            )

    def job_view(self, job_id: str, *, offset: int) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise ClientError("No such job.", code="unknown_job", status=404)
        log = job["log"]
        start = max(0, min(int(offset), len(log)))
        return {**{key: value for key, value in job.items() if key != "log"}, "log": log[start:], "next_offset": len(log)}

    # ------------------------------------------------------------- inventory
    async def report_inventory(self, *, reason: str = "") -> dict[str, Any]:
        base = self.config.data.get("server_url", "")
        token = self.config.token()
        if not base or not token:
            return {"ok": False, "error": "not paired"}
        try:
            payload = await collect_inventory(self.config.data.get("worker_name", ""))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"inventory failed: {exc}"}
        payload["device_key"] = self.config.data["device_key"]
        payload["name"] = self.config.data.get("worker_name", "")
        agents: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        for kind, state in self.agents.items():
            agents.append(
                {
                    "kind": kind,
                    "path": state.path,
                    "version": state.version,
                    "installed": state.installed,
                    "usable": state.availability == "usable",
                    "capabilities": {
                        "detected_by": "client_manager",
                        "availability": state.availability,
                        "auth_status": state.auth_status,
                        "version_verified": bool(state.version),
                        "install_source": "official_script_or_npm",
                    },
                }
            )
            for model in state.models:
                models.append(
                    {
                        "provider": model.get("provider", kind),
                        "model_id": model.get("model_id", ""),
                        "display_name": model.get("display_name", ""),
                        "context_window": None,
                        "capabilities": {},
                        "availability": "unknown",
                    }
                )
        payload["agents"] = agents
        payload["models"] = models
        # The device key belongs to registration, not inventory updates.
        payload.pop("pairing_code", None)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{base}/api/mesh/inventory",
                    headers={"X-Worker-Token": token},
                    json=payload,
                )
            if response.status_code != 200:
                code, message = parse_server_detail(response)
                self.emit("inventory.failed", message, level="warning", code=code)
                return {"ok": False, "code": code, "error": message}
            result = response.json()
            self.emit("inventory.updated", f"Inventory reported to the server ({reason}).".strip())
            return {"ok": True, "result": result}
        except (httpx.HTTPError, OSError) as exc:
            self.emit("inventory.failed", f"Inventory upload failed: {exc}", level="warning")
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------ auth
    def _auth_session(self, kind: str, session_id: str | None) -> TerminalSession | None:
        target = session_id or self.auth_by_kind.get(kind)
        session = self.auth_sessions.get(target or "")
        if session is not None and session.kind == kind:
            return session
        return None

    def _auth_view(self, session: TerminalSession, offset: int) -> dict[str, Any]:
        view = session.output(offset)
        view["challenge"] = CHALLENGE_RE.search(view.get("text", "")) is not None
        view["command"] = self._redact(view.get("command", ""))
        return view

    def _history_view(self, summary: dict[str, Any]) -> dict[str, Any]:
        """A finished session keeps its captured tail readable after exit."""
        text = str(summary.get("tail") or "")
        return {
            "id": summary.get("id", ""),
            "kind": summary.get("kind", ""),
            "command": summary.get("command", ""),
            "mode": summary.get("mode", ""),
            "status": summary.get("status", "exited"),
            "exit_code": summary.get("exit_code"),
            "error": None,
            "text": text,
            "next_offset": len(text),
            "first_offset": 0,
            "total_chars": len(text),
            "truncated": False,
            "started_at": summary.get("started_at", ""),
            "ended_at": summary.get("ended_at", ""),
            "timeout_seconds": 0,
            "challenge": bool(summary.get("challenge")),
            "auth_status": summary.get("auth_status", "unknown"),
            "auth_detail": summary.get("auth_detail", ""),
            "outcome": summary.get("outcome", ""),
            "finished": True,
        }

    async def start_login(
        self,
        kind: str,
        *,
        provider: str = "",
        cols: int = 100,
        rows: int = 24,
    ) -> dict[str, Any]:
        if kind not in OFFICIAL_SOURCES:
            raise ClientError(f"Unknown agent {kind!r}.", code="unknown_agent", status=404)
        state = self.agents[kind]
        if not state.installed or not state.path:
            raise ClientError(
                f"{state.display_name} is not installed. Install it first.", code="not_installed"
            )
        running = self._auth_session(kind, None)
        if running is not None and running.status == "running":
            raise ClientError(
                "A sign-in session for this agent is already open. Finish or cancel it first.",
                code="session_running",
            )
        adapter = _adapter(kind, state.path)
        support = adapter.auth_support()
        if not support.get("interactive_login"):
            raise ClientError(
                support.get("limitation") or f"{kind} has no documented interactive login.",
                code="unsupported",
            )
        argv = [state.path, *adapter.login_argv(provider=provider)]
        if len(argv) < 2:
            raise ClientError("No documented login command is available.", code="unsupported")
        session = TerminalSession(
            kind=kind,
            argv=argv,
            cwd=self.paths.config / "agent-config",
            on_exit=self._on_auth_exit,
        )
        session.start(
            cols=max(20, min(int(cols), 400)),
            rows=max(5, min(int(rows), 200)),
            timeout_seconds=900,
        )
        self.auth_sessions[session.id] = session
        self.auth_by_kind[kind] = session.id
        self.emit(
            "agent.auth.started",
            f"{state.display_name} sign-in hosted in a {session.mode} terminal. "
            "The app will not type on your behalf.",
            kind=kind,
            session_id=session.id,
            command=self._redact(session.command),
            mode=session.mode,
        )
        return self._auth_view(session, 0)

    async def _on_auth_exit(self, session: TerminalSession) -> None:
        challenge = CHALLENGE_RE.search(session.tail or "")
        status = "unknown"
        detail = "The session ended before the CLI status could be read."
        try:
            state = self.agents[session.kind]
            adapter = _adapter(session.kind, state.path) if state.path else None
            if adapter is not None:
                if session.kind == "qoder":
                    qoder = await asyncio.wait_for(adapter.status(), timeout=45)
                    status = (
                        "authenticated"
                        if qoder.get("logged_in")
                        else ("unknown" if not qoder.get("ok") else "unauthenticated")
                    )
                    detail = str(qoder.get("detail") or "")
                else:
                    status, detail = await asyncio.wait_for(adapter.check_auth(), timeout=45)
        except Exception as exc:  # noqa: BLE001 - a failed re-check is still real
            detail = f"Status check failed: {exc}"
        outcome = (
            "authenticated"
            if status == "authenticated" and session.status == "exited" and session.exit_code == 0
            else ("cancelled" if session.status == "cancelled" else "still_signed_out")
        )
        summary = {
            "id": session.id,
            "kind": session.kind,
            "command": self._redact(session.command),
            "mode": session.mode,
            "status": session.status,
            "exit_code": session.exit_code,
            "auth_status": status,
            "auth_detail": self._redact(detail),
            "outcome": outcome,
            "challenge": bool(challenge),
            "challenge_text": challenge.group(0) if challenge else "",
            "tail": self._redact(session.tail[-1200:]),
            "started_at": session.started_at.isoformat(),
            "ended_at": session.ended_at.isoformat() if session.ended_at else "",
        }
        self.auth_history.appendleft(summary)
        self.auth_by_kind.pop(session.kind, None)
        self.emit(
            "agent.auth.finished",
            f"{session.kind} sign-in {outcome}; CLI status says {status}."
            + (" A human security challenge appeared and was left to the human." if challenge else ""),
            level="info" if outcome == "authenticated" else "warning",
            kind=session.kind,
            session_id=session.id,
            outcome=outcome,
            auth_status=status,
            challenge=bool(challenge),
        )
        await self.detect_agent(session.kind)
        await self.report_inventory(reason=f"after {session.kind} sign-in")

    async def auth_input(self, kind: str, session_id: str | None, text: str) -> dict[str, Any]:
        if not text:
            raise ClientError("Nothing to send.", code="empty_input")
        if len(text) > 4096:
            raise ClientError("Input is limited to 4096 characters.", code="invalid_input")
        session = self._require_auth(kind, session_id)
        try:
            session.write(text.encode("utf-8", errors="surrogatepass"))
        except AgentAuthError as exc:
            raise ClientError(exc.message, code=exc.code) from exc
        return self._auth_view(session, session.output(0)["next_offset"])

    async def auth_key(self, kind: str, session_id: str | None, key: str) -> dict[str, Any]:
        sequence = KEY_SEQUENCES.get((key or "").strip().lower())
        if sequence is None:
            raise ClientError(
                f"Unsupported key {key!r}. Allowed: {', '.join(sorted(KEY_SEQUENCES))}.",
                code="invalid_key",
            )
        session = self._require_auth(kind, session_id)
        session.write(sequence)
        if sequence == KEY_SEQUENCES["ctrl_c"]:
            session.interrupt()
        return self._auth_view(session, session.output(0)["next_offset"])

    async def auth_resize(self, kind: str, session_id: str | None, *, cols: int, rows: int) -> dict[str, Any]:
        session = self._require_auth(kind, session_id)
        session.resize(cols=max(20, min(cols, 400)), rows=max(5, min(rows, 200)))
        return self._auth_view(session, session.output(0)["next_offset"])

    async def auth_cancel(self, kind: str, session_id: str | None) -> dict[str, Any]:
        session = self._require_auth(kind, session_id)
        session.cancel()
        return self._auth_view(session, 0)

    def _require_auth(self, kind: str, session_id: str | None) -> TerminalSession:
        session = self._auth_session(kind, session_id)
        if session is None:
            raise ClientError("No open sign-in session for this agent.", code="unknown_session", status=404)
        if session.status != "running":
            raise ClientError(f"This sign-in session is already {session.status}.", code="session_closed")
        return session

    async def logout_agent(self, kind: str, provider: str) -> dict[str, Any]:
        state = self.agents[kind]
        if not state.installed or not state.path:
            raise ClientError(f"{state.display_name} is not installed.", code="not_installed")
        adapter = _adapter(kind, state.path)
        if not adapter.auth_support().get("logout"):
            raise ClientError(
                adapter.auth_support().get("limitation") or f"{kind} documents no logout command.",
                code="unsupported",
            )
        if kind == "opencode" and not provider.strip():
            raise ClientError(
                "OpenCode logs out one provider at a time. Enter the provider name.",
                code="provider_required",
            )
        result = await adapter.logout(provider)
        await self.detect_agent(kind)
        await self.report_inventory(reason=f"after {kind} logout")
        self.emit(
            "agent.logout",
            f"{state.display_name} logout requested; CLI result: {'ok' if result.get('ok') else 'failed'}.",
            level="info" if result.get("ok") else "warning",
            kind=kind,
        )
        return result

    def open_terminal(self, kind: str, *, provider: str = "") -> dict[str, Any]:
        state = self.agents[kind]
        if not state.installed or not state.path:
            raise ClientError(f"{state.display_name} is not installed.", code="not_installed")
        adapter = _adapter(kind, state.path)
        argv = [state.path, *adapter.login_argv(provider=provider)]
        terminal = None
        for candidate, prefix in (
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("konsole", ["-e"]),
            ("xterm", ["-e"]),
            ("kitty", []),
            ("alacritty", ["-e"]),
        ):
            found = shutil.which(candidate)
            if found:
                terminal = (found, prefix)
                break
        if terminal is None:
            raise ClientError(
                "No terminal emulator was found. Install x-terminal-emulator/gnome-terminal, "
                "or run the login command manually.",
                code="no_terminal",
            )
        found, prefix = terminal
        command = [found, *prefix, *argv]
        try:
            subprocess.Popen(
                command,
                cwd=str(self.paths.config / "agent-config"),
                env=agent_env(),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise ClientError(f"Could not open the terminal: {exc}", code="spawn_failed") from exc
        self.emit(
            "agent.auth.external_terminal",
            f"Opened the {kind} login in an external terminal. This app no longer sees its output.",
            kind=kind,
            command=" ".join(self._redact(part) for part in command),
        )
        return {"ok": True, "command": " ".join(command)}

    # ------------------------------------------------------------- operator API
    def _require_operator(self) -> str:
        token = self.config.operator_token()
        if not token:
            raise ClientError(
                "This server route needs the dashboard control token. Pairing gives this "
                "machine a worker token only; enable operator mode in Settings on the machine "
                "that runs the control server.",
                code="operator_token_required",
                status=409,
            )
        return token

    async def operator_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        token = self._require_operator()
        base = self.config.data.get("server_url", "")
        if not base:
            raise ClientError("Not paired.", code="unpaired")
        headers = {"X-API-Token": token}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method, f"{base}{path}", headers=headers, json=json_body, params=params
                )
        except (httpx.HTTPError, OSError) as exc:
            raise ClientError(f"Server unreachable: {exc}", code="server_unreachable", status=502) from exc
        if response.status_code >= 400:
            code, message = parse_server_detail(response)
            raise ClientError(
                MESH_MESSAGES.get(code, message or f"HTTP {response.status_code}"),
                code=code,
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            return {"text": response.text}

    async def enable_operator(self, *, token: str, use_local: bool) -> dict[str, Any]:
        base = self.config.data.get("server_url", "")
        if not base:
            raise ClientError("Pair this device before enabling operator mode.", code="unpaired")
        source = "manual"
        if use_local:
            candidate = _xdg("data", ".local/share") / "forgemesh-control" / "runtime" / "control_token"
            if not candidate.is_file():
                raise ClientError(
                    "No control token was found at "
                    f"{candidate}. This machine is not the control server, or the server "
                    "has not created its runtime token yet.",
                    code="control_token_missing",
                )
            token = candidate.read_text(encoding="utf-8").strip()
            source = "local_server_runtime"
        if not token:
            raise ClientError("A control token is required.", code="control_token_required")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{base}/api/status", headers={"X-API-Token": token})
        except (httpx.HTTPError, OSError) as exc:
            raise ClientError(f"Could not validate the control token: {exc}", code="server_unreachable") from exc
        if response.status_code != 200:
            raise ClientError(
                "The server rejected that control token. Dashboard routes also require a "
                "loopback connection; a remote server refuses operator mode by design.",
                code="control_token_invalid",
                status=400,
            )
        self.config.set_operator_token(token, source)
        self.server_status = response.json()
        self.emit(
            "operator.enabled",
            f"Operator mode enabled using the {source.replace('_', ' ')} control token. "
            "The worker token is still the only credential sent to /api/mesh/*.",
            source=source,
        )
        await self.refresh_operator()
        return {"ok": True, "source": source, "state": self.snapshot()}

    async def disable_operator(self) -> dict[str, Any]:
        self.config.clear_operator_token()
        self.operator.update(
            {"available": False, "error": "", "projects": [], "tasks": [], "workers": [], "events": [], "documents": [], "counts": {}}
        )
        self.emit("operator.disabled", "Operator mode disabled; the control token was removed from this machine.")
        return {"ok": True, "state": self.snapshot()}

    async def refresh_operator(self) -> None:
        if not self.config.operator_token():
            return
        results = await asyncio.gather(
            self.operator_request("GET", "/api/projects"),
            self.operator_request("GET", "/api/tasks", params={"limit": 200}),
            self.operator_request("GET", "/api/workers"),
            self.operator_request("GET", "/api/events/history", params={"limit": 200}),
            self.operator_request("GET", "/api/documents"),
            return_exceptions=True,
        )
        errors: list[str] = []
        projects = tasks = workers = events = documents = None
        for index, result in enumerate(results):
            if isinstance(result, Exception):
                errors.append(str(result))
                continue
            if index == 0:
                projects = result.get("projects", []) if isinstance(result, dict) else []
            elif index == 1:
                tasks = result.get("tasks", []) if isinstance(result, dict) else []
            elif index == 2:
                workers = result.get("workers", []) if isinstance(result, dict) else []
            elif index == 3:
                events = result.get("events", []) if isinstance(result, dict) else []
            elif index == 4:
                documents = result.get("documents", []) if isinstance(result, dict) else []
        if projects is not None:
            self.operator["projects"] = projects
        if tasks is not None:
            self.operator["tasks"] = tasks
        if workers is not None:
            self.operator["workers"] = workers
        if events is not None:
            self.operator["events"] = events
        if documents is not None:
            self.operator["documents"] = documents
        self.operator["available"] = True
        self.operator["error"] = "; ".join(errors[:3])
        self.operator["last_refresh"] = _now()
        self.operator["counts"] = {
            "projects": len(self.operator["projects"]),
            "tasks": len(self.operator["tasks"]),
            "workers": len(self.operator["workers"]),
            "documents": len(self.operator["documents"]),
        }

    async def project_detail(self, project_id: str) -> dict[str, Any]:
        payload = await self.operator_request("GET", f"/api/projects/{project_id}")
        payload["approval_states"] = self._approval_states(payload.get("approvals") or [])
        return payload

    @staticmethod
    def _approval_states(approvals: list[dict[str, Any]]) -> dict[str, Any]:
        stages = ("blueprint", "ui_spec", "implementation")
        states: dict[str, Any] = {}
        for stage in stages:
            rows = [row for row in approvals if row.get("stage") == stage]
            rows.sort(key=lambda row: str(row.get("requested_at") or ""), reverse=True)
            current = rows[0] if rows else None
            states[stage] = {
                "state": current.get("status") if current else "missing",
                "approval_id": current.get("id") if current else "",
                "summary": current.get("summary") if current else "",
                "requested_at": current.get("requested_at") if current else "",
                "note": current.get("note") if current else "",
                "history": rows,
                "ui_state": {
                    "pending": "Awaiting review",
                    "approved": "Approved",
                    "rejected": "Changes requested",
                    "missing": "Not requested",
                }.get(current.get("status") if current else "missing", "Unknown"),
            }
        return states

    async def review_task(self, task_id: str) -> dict[str, Any]:
        return await self.operator_request("GET", f"/api/tasks/{task_id}")

    # ---------------------------------------------------------------- lifecycle
    async def startup(self) -> None:
        self.emit("app.started", f"{APP_NAME} local bridge started.")
        asyncio.create_task(self._startup_detect())
        self._poller = asyncio.create_task(self._poll_loop())

    async def _startup_detect(self) -> None:
        await asyncio.sleep(0.2)
        await self.detect_all()
        if self.config.operator_token():
            await self.refresh_operator()
        if self.config.configured and self.config.has_token():
            await self._probe_http()
            # A desktop supervisor should restore the loop it owns. If a systemd
            # unit already owns the single connection, start_worker refuses and
            # the reason is shown rather than silently fighting for the token.
            try:
                await self.start_worker(takeover=False, auto=True)
            except ClientError as exc:
                self.emit(
                    "worker.start_refused",
                    exc.message,
                    level="info" if exc.code == "unit_active" else "warning",
                    code=exc.code,
                )

    async def _poll_loop(self) -> None:
        tick = 0
        while not self._closing:
            try:
                await asyncio.sleep(5)
                tick += 1
                if self.config.configured and self.config.has_token():
                    await self._probe_http()
                if self.config.operator_token() and tick % 2 == 0:
                    await self.refresh_operator()
                await self._unit_active_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the poller must keep running
                self.log_worker(f"poll error: {exc}", level="warn")

    async def shutdown(self) -> None:
        self._closing = True
        if self._poller is not None:
            self._poller.cancel()
        if self.worker_process is not None and self.worker_process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.worker_process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.worker_process.wait(), timeout=5)
        for session in list(self.auth_sessions.values()):
            if session.status == "running":
                with contextlib.suppress(Exception):
                    session.cancel()
        for client_socket in list(self.sockets):
            with contextlib.suppress(Exception):
                await client_socket.close()


# ------------------------------------------------------------------- app/models
class PairBody(BaseModel):
    server_url: str
    code: str
    name: str = ""
    insecure_http: bool = False
    insecure_confirmation: str = ""


class UnpairBody(BaseModel):
    confirm: str = ""
    revoke_remote: bool = False


class ConfigBody(BaseModel):
    worker_name: str | None = None
    insecure_http: bool | None = None
    insecure_confirmation: str = ""


class WorkerStartBody(BaseModel):
    takeover: bool = False


class WorkerStopBody(BaseModel):
    confirm: bool = False
    stop_unit: bool = False


class InstallBody(BaseModel):
    force: bool = False
    approve: bool = False


class LoginBody(BaseModel):
    provider: str = ""
    cols: int = Field(default=100, ge=20, le=400)
    rows: int = Field(default=24, ge=5, le=200)


class InputBody(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    session_id: str | None = None


class KeyBody(BaseModel):
    key: str
    session_id: str | None = None


class ResizeBody(BaseModel):
    cols: int = Field(ge=20, le=400)
    rows: int = Field(ge=5, le=200)
    session_id: str | None = None


class SessionRef(BaseModel):
    session_id: str | None = None


class LogoutBody(BaseModel):
    provider: str = ""


class OperatorBody(BaseModel):
    token: str = ""
    use_local: bool = False


class GateBody(BaseModel):
    summary: str = ""


class DecisionBody(BaseModel):
    approved: bool
    note: str = ""


class HumanActionBody(BaseModel):
    title: str
    description: str = ""
    why: str = ""
    feature: str = ""
    credential_name: str = ""
    blocking: bool = False


class ResolveBody(BaseModel):
    status: str
    note: str = ""


class DeliveryRunBody(BaseModel):
    project_id: str
    command: str
    task_id: str | None = None
    cwd: str | None = None


class ReportBody(BaseModel):
    project_id: str
    kind: str = Field(default="review", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ArtifactActionBody(BaseModel):
    project_id: str
    artifact_id: str
    confirm: bool = False


def create_app(service: ClientService, token: str) -> FastAPI:
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.token = token

    async def auth(
        authorization: str = Header(default=""),
        request: Request = None,  # type: ignore[assignment]
    ) -> None:
        # Loopback-only was already enforced by the bind address; the token is
        # the second lock so another local user cannot drive this app.
        if not authorization.startswith("Bearer ") or not secrets.compare_digest(
            authorization[7:], token
        ):
            raise HTTPException(status_code=401, detail="Invalid local bridge token")

    @app.exception_handler(ClientError)
    async def _client_error(_request: Request, exc: ClientError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "app": APP_NAME, "version": VERSION, "pid": os.getpid()}

    @app.get("/api/state")
    async def state(_: None = Depends(auth)) -> dict[str, Any]:
        return service.snapshot()

    @app.post("/api/pair")
    async def pair(body: PairBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.pair(
            server=body.server_url,
            code=body.code,
            name=body.name or service.config.data.get("worker_name", ""),
            insecure_http=body.insecure_http,
            insecure_confirmation=body.insecure_confirmation,
        )

    @app.post("/api/unpair")
    async def unpair(body: UnpairBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.unpair(confirm=body.confirm, revoke_remote=body.revoke_remote)

    @app.post("/api/config")
    async def config_update(body: ConfigBody, _: None = Depends(auth)) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        if body.worker_name is not None:
            updates["worker_name"] = body.worker_name.strip()[:120]
        if body.insecure_http is not None:
            if body.insecure_http and not service.config.data.get("insecure_acknowledged"):
                if body.insecure_confirmation.strip() != "ALLOW PLAIN HTTP":
                    raise ClientError(
                        "Type ALLOW PLAIN HTTP to enable plain HTTP to a non-loopback server.",
                        code="confirmation_required",
                    )
                updates["insecure_acknowledged"] = True
            updates["insecure_http"] = body.insecure_http
        await service.config.save(**updates)
        service.emit("config.updated", "Configuration updated.", fields=list(updates))
        return service.snapshot()

    @app.post("/api/connection/test")
    async def connection_test(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.connection_test()

    @app.post("/api/worker/start")
    async def worker_start(body: WorkerStartBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.start_worker(takeover=body.takeover)

    @app.post("/api/worker/stop")
    async def worker_stop(body: WorkerStopBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.stop_worker(confirm=body.confirm, stop_unit=body.stop_unit)

    @app.post("/api/worker/restart")
    async def worker_restart(body: WorkerStartBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.restart_worker(takeover=body.takeover)

    @app.get("/api/agents")
    async def agents(_: None = Depends(auth)) -> dict[str, Any]:
        return {"agents": service.agent_inventory(), "pty": HAS_PTY}

    @app.post("/api/agents/{kind}/detect")
    async def agent_detect(kind: str, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.detect_agent(kind)

    @app.post("/api/agents/{kind}/install")
    async def agent_install(kind: str, body: InstallBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.install_agent(kind, force=body.force, operation="install", approve=body.approve)

    @app.post("/api/agents/{kind}/update")
    async def agent_update(kind: str, body: InstallBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.install_agent(kind, force=False, operation="update", approve=body.approve)

    @app.post("/api/agents/{kind}/verify")
    async def agent_verify(kind: str, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.verify_agent(kind)

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, offset: int = 0, _: None = Depends(auth)) -> dict[str, Any]:
        return service.job_view(job_id, offset=offset)

    @app.post("/api/agents/{kind}/login")
    async def agent_login(kind: str, body: LoginBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.start_login(kind, provider=body.provider, cols=body.cols, rows=body.rows)

    @app.get("/api/agents/{kind}/auth/session")
    async def auth_session(
        kind: str, session_id: str | None = None, offset: int = 0, _: None = Depends(auth)
    ) -> dict[str, Any]:
        session = service._auth_session(kind, session_id)
        if session is None:
            history = next(
                (
                    row
                    for row in service.auth_history
                    if row.get("id") == session_id and row.get("kind") == kind
                ),
                None,
            )
            if history is None:
                raise ClientError("No such sign-in session.", code="unknown_session", status=404)
            return service._history_view(history)
        return service._auth_view(session, max(0, offset))

    @app.post("/api/agents/{kind}/auth/session/input")
    async def auth_input(kind: str, body: InputBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.auth_input(kind, body.session_id, body.text)

    @app.post("/api/agents/{kind}/auth/session/key")
    async def auth_key(kind: str, body: KeyBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.auth_key(kind, body.session_id, body.key)

    @app.post("/api/agents/{kind}/auth/session/resize")
    async def auth_resize(kind: str, body: ResizeBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.auth_resize(kind, body.session_id, cols=body.cols, rows=body.rows)

    @app.post("/api/agents/{kind}/auth/session/cancel")
    async def auth_cancel(kind: str, body: SessionRef, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.auth_cancel(kind, body.session_id)

    @app.post("/api/agents/{kind}/logout")
    async def agent_logout(kind: str, body: LogoutBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.logout_agent(kind, body.provider)

    @app.post("/api/agents/{kind}/open-terminal")
    async def open_terminal(kind: str, body: LogoutBody, _: None = Depends(auth)) -> dict[str, Any]:
        return service.open_terminal(kind, provider=body.provider)

    @app.post("/api/report/inventory")
    async def report_inventory(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.report_inventory(reason="requested by the human")

    @app.post("/api/operator/enable")
    async def operator_enable(body: OperatorBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.enable_operator(token=body.token, use_local=body.use_local)

    @app.post("/api/operator/disable")
    async def operator_disable(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.disable_operator()

    @app.get("/api/operator/projects")
    async def projects(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/projects")

    @app.get("/api/operator/projects/{project_id}")
    async def project(project_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.project_detail(project_id)

    @app.post("/api/operator/projects/{project_id}/gate")
    async def gate(project_id: str, body: GateBody, _: None = Depends(auth)) -> dict[str, Any]:
        # Writes pending approvals for stages that are not pending/approved.
        result = await service.operator_request(
            "POST", f"/api/projects/{project_id}/gate", json_body={"summary": body.summary}
        )
        service.emit(
            "approval.gate_opened",
            f"Gate opened for {project_id}; the server wrote pending rows only for stages "
            "that needed review.",
            project_id=project_id,
        )
        await service.refresh_operator()
        return result

    @app.post("/api/operator/projects/{project_id}/approvals/{approval_id}/decide")
    async def decide(
        project_id: str, approval_id: str, body: DecisionBody, _: None = Depends(auth)
    ) -> dict[str, Any]:
        result = await service.operator_request(
            "POST",
            f"/api/projects/{project_id}/approvals/{approval_id}/decide",
            json_body={"approved": body.approved, "note": body.note},
        )
        service.emit(
            "approval.decided",
            f"{'Approved' if body.approved else 'Requested changes on'} {project_id} "
            f"({approval_id}).",
            level="info" if body.approved else "warning",
            project_id=project_id,
            approval_id=approval_id,
        )
        await service.refresh_operator()
        return result

    @app.post("/api/operator/projects/{project_id}/human-actions")
    async def human_action(
        project_id: str, body: HumanActionBody, _: None = Depends(auth)
    ) -> dict[str, Any]:
        result = await service.operator_request(
            "POST", f"/api/projects/{project_id}/human-actions", json_body=body.model_dump()
        )
        await service.refresh_operator()
        return result

    @app.post("/api/operator/projects/{project_id}/human-actions/{action_id}/resolve")
    async def resolve_action(
        project_id: str, action_id: str, body: ResolveBody, _: None = Depends(auth)
    ) -> dict[str, Any]:
        result = await service.operator_request(
            "POST",
            f"/api/projects/{project_id}/human-actions/{action_id}/resolve",
            json_body={"status": body.status, "note": body.note},
        )
        await service.refresh_operator()
        return result

    @app.get("/api/operator/tasks")
    async def tasks(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/tasks", params={"limit": 300})

    @app.get("/api/operator/tasks/{task_id}")
    async def task_detail(task_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", f"/api/tasks/{task_id}")

    @app.get("/api/operator/workers")
    async def workers(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/workers")

    @app.get("/api/operator/events")
    async def events(limit: int = Query(default=200, ge=1, le=500), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/events/history", params={"limit": limit})

    @app.get("/api/operator/documents")
    async def documents(_: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/documents")

    @app.get("/api/operator/documents/{document_id}")
    async def document(document_id: str, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", f"/api/documents/{document_id}")

    @app.get("/api/operator/artifacts")
    async def artifacts(project_id: str = Query(min_length=1), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request(
            "GET", "/api/delivery/artifacts", params={"project_id": project_id}
        )

    @app.post("/api/operator/artifacts/collect")
    async def collect_artifacts(project_id: str = Query(min_length=1), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request(
            "POST", "/api/delivery/artifacts", json_body={"project_id": project_id}
        )

    @app.get("/api/operator/runs")
    async def runs(project_id: str | None = None, _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request(
            "GET", "/api/delivery/runs", params={"project_id": project_id} if project_id else None
        )

    @app.post("/api/operator/delivery/run")
    async def delivery_run(body: DeliveryRunBody, _: None = Depends(auth)) -> dict[str, Any]:
        result = await service.operator_request(
            "POST",
            "/api/delivery/run",
            json_body={
                "project_id": body.project_id,
                "command": body.command,
                "task_id": body.task_id,
                "cwd": body.cwd,
            },
            timeout=900,
        )
        service.emit(
            "delivery.run",
            f"Verification command finished with status {result.get('status')}.",
            project_id=body.project_id,
            command=body.command,
        )
        return result

    @app.post("/api/operator/delivery/report")
    async def delivery_report(body: ReportBody, _: None = Depends(auth)) -> dict[str, Any]:
        result = await service.operator_request(
            "POST", "/api/delivery/report", json_body={"project_id": body.project_id, "kind": body.kind}
        )
        service.emit(
            "delivery.report",
            f"The server generated a {body.kind} report for {body.project_id}.",
            project_id=body.project_id,
        )
        return result

    @app.get("/api/operator/workspaces/status")
    async def workspace_status(path: str = Query(min_length=1), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/workspaces/status", params={"path": path})

    @app.get("/api/operator/workspaces/diff")
    async def workspace_diff(path: str = Query(min_length=1), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/workspaces/diff", params={"path": path})

    @app.get("/api/operator/workspaces/log")
    async def workspace_log(path: str = Query(min_length=1), _: None = Depends(auth)) -> dict[str, Any]:
        return await service.operator_request("GET", "/api/workspaces/log", params={"path": path})

    @app.post("/api/operator/artifacts/verify")
    async def artifact_verify(body: ArtifactActionBody, _: None = Depends(auth)) -> dict[str, Any]:
        return await _artifact_local(service, body, copy=False)

    @app.post("/api/operator/artifacts/copy")
    async def artifact_copy(body: ArtifactActionBody, _: None = Depends(auth)) -> dict[str, Any]:
        if not body.confirm:
            raise ClientError(
                "Copying a delivered artifact needs explicit confirmation.", code="confirmation_required"
            )
        return await _artifact_local(service, body, copy=True)

    @app.get("/api/system/disk")
    async def disk(_: None = Depends(auth)) -> dict[str, Any]:
        usage = shutil.disk_usage(service.paths.data)
        return {
            "path": str(service.paths.data),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
        }

    @app.get("/api/system/update-check")
    async def update_check(_: None = Depends(auth)) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "ForgeMesh-Client-Manager"}) as client:
                response = await client.get(
                    "https://api.github.com/repos/ismailmo9090-del/forgemesh-control/releases/latest"
                )
            if response.status_code != 200:
                return {"ok": False, "error": f"GitHub returned HTTP {response.status_code}"}
            payload = response.json()
            tag = str(payload.get("tag_name") or "").lstrip("v")
            return {
                "ok": True,
                "current": VERSION,
                "latest": tag,
                "update_available": bool(tag and tag != VERSION),
                "url": payload.get("html_url", ""),
                "published_at": payload.get("published_at", ""),
            }
        except (httpx.HTTPError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    @app.websocket("/ws")
    async def websocket(websocket: WebSocket, token_query: str = Query(default="", alias="token")) -> None:
        if not secrets.compare_digest(token_query, token):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        service.sockets.add(websocket)
        try:
            await websocket.send_json({"type": "snapshot", "state": service.snapshot()})
            while True:
                # The client only listens; a ping keeps the socket honest.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            service.sockets.discard(websocket)

    @app.on_event("startup")
    async def on_startup() -> None:
        await service.startup()

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        await service.shutdown()

    return app


async def _artifact_local(service: ClientService, body: ArtifactActionBody, *, copy: bool) -> dict[str, Any]:
    project = await service.project_detail(body.project_id)
    artifacts = await service.operator_request(
        "GET", "/api/delivery/artifacts", params={"project_id": body.project_id}
    )
    target = next(
        (item for item in artifacts.get("artifacts", []) if item.get("id") == body.artifact_id),
        None,
    )
    if target is None:
        raise ClientError("No such artifact.", code="not_found", status=404)
    raw_path = Path(str(target.get("path") or ""))
    if not raw_path.is_absolute():
        raise ClientError("The server returned a non-absolute artifact path.", code="unsafe_path")
    workspace = Path(str(project.get("workspace_path") or "")).resolve()
    try:
        resolved = raw_path.resolve(strict=True)
    except OSError:
        raise ClientError(
            "That artifact path is not readable on this machine. The server exposes "
            "metadata only; there is no artifact download endpoint.",
            code="not_local",
            status=409,
        ) from None
    if not workspace or (resolved != workspace and workspace not in resolved.parents):
        raise ClientError("Artifact path escapes the project workspace.", code="unsafe_path")
    if not resolved.is_file():
        raise ClientError("Artifact path is not a file.", code="not_local", status=409)
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    matches = digest == str(target.get("sha256") or "")
    result = {
        "artifact": target,
        "path": str(resolved),
        "recomputed_sha256": digest,
        "matches": matches,
        "size": resolved.stat().st_size,
    }
    if not matches:
        service.emit(
            "artifact.checksum_mismatch",
            f"Checksum mismatch for {target.get('name')}: server {target.get('sha256')} vs local {digest}.",
            level="error",
            artifact_id=body.artifact_id,
        )
        return result
    if copy:
        destination = service.paths.data / "artifacts" / re.sub(r"[^A-Za-z0-9._-]", "_", str(target.get("name") or resolved.name))[:120]
        shutil.copy2(resolved, destination)
        os.chmod(destination, 0o600)
        result["copied_to"] = str(destination)
        service.emit(
            "artifact.copied",
            f"Verified and copied {target.get('name')} to the client artifact directory.",
            artifact_id=body.artifact_id,
        )
    else:
        service.emit("artifact.verified", f"Checksum verified for {target.get('name')}.", artifact_id=body.artifact_id)
    return result


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="ForgeMesh Client Manager local bridge")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--handshake", action="store_true", help="print one JSON handshake line")
    args = parser.parse_args()

    paths = ClientPaths.create()
    service = ClientService(paths)
    token = args.token or secrets.token_urlsafe(32)
    port = args.port or _free_port()
    app = create_app(service, token)
    if args.handshake:
        print(
            json.dumps(
                {
                    "app": APP_NAME,
                    "version": VERSION,
                    "host": "127.0.0.1",
                    "port": port,
                    "token": token,
                    "pid": os.getpid(),
                }
            ),
            flush=True,
        )
    log_path = paths.data / "logs" / "client-manager.log"
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    def _stop(_signum: int, _frame: Any) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(f"[{_now()}] starting local bridge on 127.0.0.1:{port}\n")
            server.run()
    finally:
        with contextlib.suppress(OSError):
            log_path.chmod(0o600)


if __name__ == "__main__":
    main()
