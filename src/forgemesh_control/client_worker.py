"""Worker loop used by the ForgeMesh Client Manager desktop app.

This module is deliberately thin. It does not re-implement mesh protocol v1:
it imports the frame names, reconnect ladder and task executor from
:mod:`forgemesh_control.worker_client`, then republishes what really happened as
newline-delimited JSON on stdout so the local Client Manager daemon can show
the same state to the human.

The token is read from the environment (never argv) so it cannot be seen in
``ps``. The daemon keeps the only copy in its Secret Service/file store.

Exit codes and close-code mapping are reported as named states, not inferred:
``1008`` means the token was refused, ``1002`` means the server sent
``protocol_mismatch``/``malformed_frame``, ``1013`` means the mesh is
unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from typing import Any

import websockets

from forgemesh_control.agent_auth import redact_output
from forgemesh_control.mesh import MESH_PROTOCOL_VERSION
from forgemesh_control.worker_client import (
    RECONNECT_DELAYS,
    _guard_scheme,
    _ws_url,
    collect_inventory,
    execute_task,
)

MAX_EMITTED_OUTPUT = 190_000


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insecure_from_env() -> bool:
    """The daemon sets this only after the user acknowledged the plain-HTTP switch."""
    return os.environ.get("FMW_INSECURE_HTTP") == "1"


def emit(event: str, **fields: Any) -> None:
    """One JSON object per line. stdout is the daemon's event channel."""
    payload = {"event": event, "at": _now(), **fields}
    sys.stdout.write(json.dumps(payload, default=str, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class WorkerLoop:
    def __init__(self, *, server: str, token: str, device_key: str, timeout: int) -> None:
        self.server = server
        self.token = token
        self.device_key = device_key
        self.timeout = timeout
        self.stop_requested = False
        self._heartbeat: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._running: set[asyncio.Task[None]] = set()
        self._max_tasks = 1
        self._ws: Any = None

    # ------------------------------------------------------------------ output
    def _on_frame(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "task_progress":
            emit(
                "task_progress",
                task_id=message.get("task_id"),
                percent=message.get("percent"),
                message=message.get("message") or "",
            )
        elif kind == "task_result":
            output = str(message.get("output") or "")
            # Local display only. Credential-shaped strings are redacted before
            # they can reach the daemon's ring buffer.
            emit(
                "task_result",
                task_id=message.get("task_id"),
                status=message.get("status"),
                output_chars=len(output),
                output=redact_output(output[:MAX_EMITTED_OUTPUT]),
                error=redact_output(str(message.get("error") or "")) or None,
            )
        elif kind == "claim":
            emit("claim_sent")

    async def _send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("socket is not open")
        self._on_frame(message)
        async with self._send_lock:
            await self._ws.send(json.dumps(message))

    # --------------------------------------------------------------- heartbeat
    async def _heartbeater(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                if self._ws is not None:
                    await self._send({"type": "heartbeat"})
                    emit("heartbeat_sent")
        except asyncio.CancelledError:
            raise

    async def _request_claim(self) -> None:
        if len(self._running) < self._max_tasks:
            await self._send({"type": "claim"})

    # ------------------------------------------------------------------ session
    async def session(self) -> None:
        url = f"{_ws_url(self.server)}/ws/worker?worker_token={self.token}"
        async with websockets.connect(url, open_timeout=30, ping_interval=20) as ws:
            self._ws = ws
            emit("socket_open", url_scheme=url.split(":", 1)[0])
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    emit("worker_error", code="malformed_frame", message="server sent non-JSON")
                    continue
                kind = message.get("type")
                if kind == "hello":
                    server_protocol = message.get("protocol_version")
                    self._max_tasks = int(message.get("max_concurrent_tasks") or 1)
                    emit(
                        "hello",
                        worker_id=message.get("worker_id"),
                        protocol_version=server_protocol,
                        local_protocol_version=MESH_PROTOCOL_VERSION,
                        protocol_match=server_protocol == MESH_PROTOCOL_VERSION,
                        heartbeat_seconds=message.get("heartbeat_seconds"),
                        offline_after_seconds=message.get("offline_after_seconds"),
                        max_concurrent_tasks=self._max_tasks,
                    )
                    await self._send(
                        {
                            "type": "hello",
                            "protocol_version": MESH_PROTOCOL_VERSION,
                            "device_key": self.device_key,
                        }
                    )
                    interval = float(message.get("heartbeat_seconds") or 15)
                    self._heartbeat = asyncio.create_task(self._heartbeater(interval))
                    emit("connected", worker_id=message.get("worker_id"))
                    await self._request_claim()
                elif kind == "hello_ack":
                    emit("hello_ack", worker_id=message.get("worker_id"))
                elif kind == "heartbeat_ack":
                    data = message.get("data") or {}
                    emit(
                        "heartbeat_ack",
                        tasks_pending=data.get("tasks_pending", 0),
                        worker_status=data.get("status"),
                    )
                    if int(data.get("tasks_pending") or 0) > 0:
                        await self._request_claim()
                elif kind == "assignment":
                    task = message.get("task") or {}
                    task_id = str(task.get("id") or "")
                    if not task_id:
                        # The server sends {"task": null} when nothing is
                        # claimable. It is not an assignment.
                        continue
                    emit(
                        "task_assigned",
                        task_id=task_id,
                        title=task.get("title"),
                        project_id=task.get("project_id"),
                        agent_kind=task.get("agent_kind"),
                        model_id=task.get("model_id"),
                    )
                    run = asyncio.create_task(self._run_task(task))
                    self._running.add(run)
                    run.add_done_callback(self._running.discard)
                    await self._request_claim()
                elif kind == "inventory_ack":
                    emit("inventory_ack", updated=message.get("updated"))
                elif kind == "error":
                    code = message.get("code") or "unknown_message"
                    emit("worker_error", code=code, message=message.get("message") or "")
                    if code == "protocol_mismatch":
                        raise SystemExit(0)
            emit("socket_closed")

    async def _run_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        try:
            await execute_task(self._send, task, self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report the real crash to the parent
            emit("task_result", task_id=task_id, status="failed", output="", error=f"{type(exc).__name__}: {exc}")

    # --------------------------------------------------------------------- run
    async def run(self) -> int:
        base = _guard_scheme(self.server, insecure_http=insecure_from_env())
        attempt = 0
        while not self.stop_requested:
            emit("connecting", server=base)
            try:
                await self.session()
                attempt = 0
            except SystemExit:
                return 0
            except websockets.exceptions.InvalidStatus as exc:
                code = getattr(exc.response, "status_code", None)
                emit("worker_error", code="handshake_rejected", message=f"HTTP {code}")
                if code in (401, 403):
                    emit("revoked", message="The server refused this worker token.")
                    return 0
            except websockets.exceptions.ConnectionClosed as exc:
                code = exc.code
                reason = exc.reason or ""
                if code == 1008:
                    emit("revoked", message="The server closed the socket: bad or missing worker token.")
                    return 0
                if code == 1002:
                    emit("protocol_mismatch", message=reason or "The server reported a protocol error.")
                    return 0
                if code == 1013:
                    emit("mesh_unavailable", message=reason or "The server says the mesh is unavailable.")
                else:
                    emit("disconnected", message=f"WebSocket closed ({code}) {reason}".strip())
            except (OSError, websockets.exceptions.WebSocketException) as exc:
                emit("disconnected", message=str(exc))
            finally:
                if self._heartbeat is not None:
                    self._heartbeat.cancel()
                    self._heartbeat = None
                self._ws = None
                for task in list(self._running):
                    task.cancel()
                self._running.clear()
            if self.stop_requested:
                break
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            emit("reconnecting", attempt=attempt, delay_seconds=delay)
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline and not self.stop_requested:
                await asyncio.sleep(0.2)
        emit("stopped")
        return 0


async def _self_check() -> dict[str, Any]:
    inventory = await collect_inventory()
    return {
        "os_family": inventory.get("os_family"),
        "arch": inventory.get("arch"),
        "protocol_version": inventory.get("protocol_version"),
        "agents": [
            {"kind": item.get("kind"), "installed": item.get("installed")}
            for item in inventory.get("agents", [])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ForgeMesh Client Manager worker loop")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=int(os.environ.get("FMCM_TASK_TIMEOUT", "1800")),
    )
    args = parser.parse_args()

    if args.self_check:
        print(json.dumps(asyncio.run(_self_check()), indent=2))
        return

    server = os.environ.get("FMW_SERVER", "").strip()
    token = os.environ.get("FMW_WORKER_TOKEN", "").strip()
    device_key = os.environ.get("FMW_DEVICE_KEY", "").strip()
    if not server or not token or not device_key:
        emit("worker_error", code="missing_config", message="server, token or device key missing")
        raise SystemExit(2)

    loop = WorkerLoop(
        server=server,
        token=token,
        device_key=device_key,
        timeout=max(30, args.task_timeout),
    )

    def _stop(_signum: int, _frame: Any) -> None:
        loop.stop_requested = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    raise SystemExit(asyncio.run(loop.run()))


if __name__ == "__main__":
    main()
