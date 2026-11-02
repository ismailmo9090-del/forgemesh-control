"""Mesh worker subsystem: pairing, authenticated transport, liveness and tasks.

Workers run on other machines by design, so the ``/api/mesh/*`` endpoints and
the ``/ws/worker`` socket authenticate with the worker token only and do NOT
use ``require_local``; the dashboard-facing ``/api/workers/*`` routes stay
loopback+control-token. The protocol is OS-agnostic; the Windows worker path
is explicitly untested and flagged in capabilities as ``windows_untested``.

Honesty rules enforced here: a worker is only ``online`` after an observed
heartbeat or live socket, inventory values are stored exactly as the worker
reported them (unknown stays unknown), and task claims only happen for tasks
that exist, belong to that worker and are ``queued``/``assigned`` while the
worker is online and enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from datetime import timedelta
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.models import (
    AgentRow,
    ModelRow,
    PairingCodeRow,
    TaskEventRow,
    TaskRow,
    WorkerRow,
    as_utc,
    iso,
    utcnow,
)
from forgemesh_control.security import (
    hash_token,
    new_pairing_code,
    new_worker_token,
    token_matches,
)
from forgemesh_control.storage import ControlDatabase

logger = logging.getLogger("forgemesh.control.mesh")

MESH_PROTOCOL_VERSION = 1
MAX_INVENTORY_BYTES = 256 * 1024
MAX_TASK_OUTPUT_CHARS = 200_000
MIN_DEVICE_KEY_LENGTH = 8

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

_MODEL_AVAILABILITY = {"unknown", "available", "unavailable"}
_CLAIMABLE_STATUSES = ("queued", "assigned")
_FINAL_STATUSES = {"succeeded", "failed"}

_OS_FAMILIES = {"linux", "windows", "darwin", "freebsd", "unknown"}


class MeshError(RuntimeError):
    """A mesh operation refused with a machine-readable code."""

    def __init__(self, message: str, *, code: str = "invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def bounded_text(value: Any, max_length: int, *, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = _CONTROL_CHARS.sub(" ", value).strip()
    return cleaned[:max_length] or fallback


def _bounded_int(value: Any, *, lo: int = 0, hi: int = 100) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(lo, min(hi, value))


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class MeshService:
    """Owns worker trust (pairing/tokens), the WS transport and task handoff."""

    def __init__(
        self,
        settings: ControlSettings,
        db: ControlDatabase,
        events: EventStore,
        audit: AuditService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.audit = audit
        # worker_id -> live websocket. A reconnect replaces the previous socket.
        self._sockets: dict[str, WebSocket] = {}
        self._sweep_task: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------------ pairing
    async def create_pairing_code(self, label: str = "") -> dict[str, Any]:
        code = new_pairing_code()
        now = utcnow()
        expires_at = now + timedelta(seconds=self.settings.pairing_code_ttl_seconds)
        safe_label = bounded_text(label, 120)
        async with self.db.session_scope() as session:
            session.add(PairingCodeRow(code=code, expires_at=expires_at, label=safe_label))
        await self.events.publish(
            "worker.pairing_code_created",
            label=safe_label,
            expires_at=iso(expires_at),
        )
        await self.audit.record("mesh.pairing_code_created", label=safe_label)
        return {
            "code": code,
            "expires_at": iso(expires_at),
            "label": safe_label,
            "worker_join_url": "/api/mesh/register",
            "ttl_seconds": self.settings.pairing_code_ttl_seconds,
        }

    async def _redeem_pairing_code(self, session: Any, code: str) -> PairingCodeRow:
        row = await session.get(PairingCodeRow, code)
        if row is None:
            raise MeshError("Unknown pairing code.", code="pairing_invalid")
        if row.used_at is not None:
            raise MeshError("Pairing code was already used.", code="pairing_used")
        if as_utc(row.expires_at) is None or as_utc(row.expires_at) <= utcnow():
            raise MeshError("Pairing code expired.", code="pairing_expired")
        row.used_at = utcnow()
        return row

    # ------------------------------------------------------------ registration
    async def register(
        self, payload: dict[str, Any], *, worker_token: str | None = None
    ) -> dict[str, Any]:
        """Pair a worker with a code, or re-register an already-paired worker.

        The bearer token is returned exactly once per pairing; only its
        SHA-256 hash is stored.
        """
        device_key = bounded_text(payload.get("device_key"), 120)
        if len(device_key) < MIN_DEVICE_KEY_LENGTH:
            raise MeshError(
                f"device_key must be at least {MIN_DEVICE_KEY_LENGTH} characters.",
                code="invalid",
            )
        fields = self._normalize_worker_fields(payload)
        agents = self._normalize_agents(payload.get("agents"))
        models = self._normalize_models(payload.get("models"))
        self._check_inventory_size(payload.get("inventory"))
        now = utcnow()

        async with self.db.session_scope() as session:
            issued_token: str | None = None
            repaired = False
            reauthorized = False

            worker: WorkerRow | None = None
            if worker_token:
                worker = await self._find_by_token(session, worker_token)
                if worker.device_key != device_key:
                    raise MeshError(
                        "Worker token does not match this device_key.",
                        code="forbidden",
                    )
                reauthorized = True
            else:
                code = bounded_text(payload.get("pairing_code"), 32).upper()
                if not code:
                    raise MeshError(
                        "pairing_code is required for a first registration.",
                        code="unauthorized",
                    )
                try:
                    await self._redeem_pairing_code(session, code)
                except MeshError as exc:
                    await self.audit.record(
                        "mesh.pairing_failed", reason=exc.code, device_key=device_key
                    )
                    raise
                worker = (
                    await session.execute(
                        select(WorkerRow).where(WorkerRow.device_key == device_key)
                    )
                ).scalar_one_or_none()
                if worker is not None and worker.revoked_at is not None:
                    # A fresh pairing code is a deliberate operator re-approval.
                    repaired = True
                    worker.revoked_at = None
                    worker.enabled = True
                if worker is None:
                    issued_token = new_worker_token()
                    worker = WorkerRow(
                        id=f"wrk_{uuid.uuid4().hex[:10]}",
                        device_key=device_key,
                        name=fields["name"] or "worker",
                        paired_at=now,
                    )
                    worker.token_hash = hash_token(issued_token)
                    session.add(worker)
                else:
                    # Re-pairing rotates the token: the old one stops working.
                    issued_token = new_worker_token()
                    worker.token_hash = hash_token(issued_token)

            worker.name = fields["name"] or worker.name or "worker"
            worker.os_family = fields["os_family"]
            worker.os_version = fields["os_version"]
            worker.arch = fields["arch"]
            worker.inventory = dict(payload.get("inventory") or {}) if isinstance(
                payload.get("inventory"), dict
            ) else {}
            worker.capabilities = {
                "cpu_count": _bounded_int(fields.get("cpu_count"), lo=0, hi=4096),
                "protocol_version": fields["protocol_version"],
                "agents": [item["kind"] for item in agents if item["installed"]],
                "windows_untested": fields["os_family"] == "windows",
            }
            if worker_token is None:
                # Registration is not proof of liveness; heartbeats are.
                worker.status = "offline"
            worker_id = worker.id
            await self._upsert_agents(session, worker_id, agents)
            await self._upsert_models(session, worker_id, models)

        if not reauthorized:
            await self.events.publish(
                "worker.repaired" if repaired else "worker.paired",
                worker_id=worker_id,
                name=fields["name"],
            )
        await self.audit.record(
            "mesh.worker_registered",
            worker_id=worker_id,
            reauthorized=reauthorized,
            repaired=repaired,
            token_issued=issued_token is not None,
        )
        return {
            "worker_id": worker_id,
            "token": issued_token,
            "token_note": (
                "Store this token now; it is shown exactly once and only its "
                "hash is kept server-side."
                if issued_token is not None
                else "Existing token remains valid; no new token issued."
            ),
        }

    def _normalize_worker_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        os_family = bounded_text(payload.get("os_family"), 20).lower() or "unknown"
        if os_family not in _OS_FAMILIES:
            os_family = "unknown"
        raw_protocol = payload.get("protocol_version")
        protocol = raw_protocol if isinstance(raw_protocol, int) and not isinstance(
            raw_protocol, bool
        ) else 0
        return {
            "name": bounded_text(payload.get("name"), 120),
            "os_family": os_family,
            "os_version": bounded_text(payload.get("os_version"), 120),
            "arch": bounded_text(payload.get("arch"), 40),
            "cpu_count": payload.get("cpu_count"),
            "protocol_version": protocol,
        }

    def _check_inventory_size(self, inventory: Any) -> None:
        if inventory is not None and not isinstance(inventory, (dict, list)):
            raise MeshError("inventory must be a JSON object.", code="invalid")
        if inventory is not None:
            size = len(json.dumps(inventory, default=str))
            if size > MAX_INVENTORY_BYTES:
                raise MeshError(
                    f"inventory exceeds {MAX_INVENTORY_BYTES} bytes ({size}).",
                    code="inventory_too_large",
                )

    def _normalize_agents(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for raw in value[:50]:
            entry = _json_object(raw)
            kind = bounded_text(entry.get("kind"), 40).lower()
            if not kind:
                continue
            items.append(
                {
                    "kind": kind,
                    "path": bounded_text(entry.get("path"), 400),
                    "version": bounded_text(entry.get("version"), 80),
                    "installed": entry.get("installed") is True,
                    # "usable" must be earned by a real functional check; the
                    # server refuses to infer it from mere detection.
                    "usable": entry.get("usable") is True,
                    "capabilities": _json_object(entry.get("capabilities")),
                }
            )
        return items

    def _normalize_models(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for raw in value[:200]:
            entry = _json_object(raw)
            provider = bounded_text(entry.get("provider"), 60).lower()
            model_id = bounded_text(entry.get("model_id"), 160)
            if not provider or not model_id:
                continue
            availability = bounded_text(entry.get("availability"), 30).lower() or "unknown"
            if availability not in _MODEL_AVAILABILITY:
                availability = "unknown"
            items.append(
                {
                    "provider": provider,
                    "model_id": model_id,
                    "display_name": bounded_text(entry.get("display_name"), 160),
                    "context_window": _bounded_int(
                        entry.get("context_window"), lo=0, hi=100_000_000
                    ),
                    "capabilities": _json_object(entry.get("capabilities")),
                    "availability": availability,
                }
            )
        return items

    async def _upsert_agents(
        self, session: Any, worker_id: str, agents: list[dict[str, Any]]
    ) -> None:
        existing = (
            await session.execute(select(AgentRow).where(AgentRow.worker_id == worker_id))
        ).scalars().all()
        by_kind = {row.kind: row for row in existing}
        for item in agents:
            row = by_kind.get(item["kind"])
            if row is None:
                session.add(
                    AgentRow(
                        worker_id=worker_id,
                        kind=item["kind"],
                        path=item["path"],
                        version=item["version"],
                        installed=item["installed"],
                        usable=item["usable"],
                        capabilities=item["capabilities"],
                        detected_at=utcnow(),
                    )
                )
            else:
                row.path = item["path"]
                row.version = item["version"]
                row.installed = item["installed"]
                row.usable = item["usable"]
                row.capabilities = item["capabilities"]
                row.detected_at = utcnow()
        stale = {item["kind"] for item in agents}
        for row in existing:
            if row.kind not in stale:
                await session.delete(row)

    async def _upsert_models(
        self, session: Any, worker_id: str, models: list[dict[str, Any]]
    ) -> None:
        existing = (
            await session.execute(select(ModelRow).where(ModelRow.worker_id == worker_id))
        ).scalars().all()
        by_key = {(row.provider, row.model_id): row for row in existing}
        now = utcnow()
        seen: set[tuple[str, str]] = set()
        for item in models:
            key = (item["provider"], item["model_id"])
            seen.add(key)
            row = by_key.get(key)
            if row is None:
                session.add(
                    ModelRow(
                        worker_id=worker_id,
                        provider=item["provider"],
                        model_id=item["model_id"],
                        display_name=item["display_name"],
                        context_window=item["context_window"],
                        capabilities=item["capabilities"],
                        availability=item["availability"],
                        source="worker_reported",
                        last_seen_at=now,
                    )
                )
            else:
                row.display_name = item["display_name"] or row.display_name
                row.context_window = item["context_window"]
                row.capabilities = item["capabilities"]
                row.availability = item["availability"]
                row.last_seen_at = now
        for row in existing:
            if (row.provider, row.model_id) not in seen:
                await session.delete(row)

    # ---------------------------------------------------------- authentication
    async def _find_by_token(self, session: Any, token: str) -> WorkerRow:
        candidate_hash = hash_token(token)
        row = (
            await session.execute(select(WorkerRow).where(WorkerRow.token_hash == candidate_hash))
        ).scalar_one_or_none()
        # Re-check with a constant-time compare; the hash lookup is only an
        # index shortcut, not the authorization decision.
        if row is None or not token_matches(token, row.token_hash):
            raise MeshError("Invalid worker token.", code="unauthorized")
        if row.revoked_at is not None:
            raise MeshError("This worker has been revoked.", code="worker_revoked")
        if not row.enabled:
            raise MeshError("This worker is disabled.", code="worker_disabled")
        return row

    async def authenticate(self, token: str | None) -> WorkerRow:
        if not token:
            raise MeshError("Missing worker token (X-Worker-Token header).", code="unauthorized")
        async with self.db.session_scope() as session:
            return await self._find_by_token(session, token)

    async def get_worker(self, worker_id: str, *, require_active: bool = False) -> WorkerRow:
        async with self.db.session_scope() as session:
            row = await session.get(WorkerRow, worker_id)
            if row is None:
                raise MeshError("Unknown worker.", code="not_found")
            if require_active:
                if row.revoked_at is not None:
                    raise MeshError("This worker has been revoked.", code="worker_revoked")
                if not row.enabled:
                    raise MeshError("This worker is disabled.", code="worker_disabled")
            return row

    # ----------------------------------------------------------- liveness
    def is_live(self, worker_id: str) -> bool:
        return worker_id in self._sockets

    async def heartbeat(self, worker_id: str) -> dict[str, Any]:
        now = utcnow()
        async with self.db.session_scope() as session:
            row = await self.get_worker_for_update(session, worker_id)
            was_online = row.status == "online"
            row.last_heartbeat_at = now
            row.status = "online"
        if not was_online:
            await self.events.publish("worker.online", worker_id=worker_id, source="heartbeat")
        pending = await self.count_claimable(worker_id)
        return {
            "server_time": iso(now),
            "worker_id": worker_id,
            "tasks_pending": pending,
            "offline_after_seconds": self.settings.worker_offline_after_seconds,
        }

    async def get_worker_for_update(self, session: Any, worker_id: str) -> WorkerRow:
        row = await session.get(WorkerRow, worker_id)
        if row is None:
            raise MeshError("Unknown worker.", code="not_found")
        if row.revoked_at is not None:
            raise MeshError("This worker has been revoked.", code="worker_revoked")
        if not row.enabled:
            raise MeshError("This worker is disabled.", code="worker_disabled")
        return row

    async def mark_online(self, worker_id: str, *, source: str) -> None:
        now = utcnow()
        async with self.db.session_scope() as session:
            row = await session.get(WorkerRow, worker_id)
            if row is None or row.revoked_at is not None or not row.enabled:
                return
            was_online = row.status == "online"
            row.last_heartbeat_at = now
            row.status = "online"
        if not was_online:
            await self.events.publish("worker.online", worker_id=worker_id, source=source)

    async def sweep_once(self) -> list[str]:
        """Mark workers offline whose last heartbeat is older than the window."""
        cutoff = utcnow() - timedelta(seconds=self.settings.worker_offline_after_seconds)
        went_offline: list[tuple[str, str]] = []
        async with self.db.session_scope() as session:
            rows = (
                await session.execute(
                    select(WorkerRow).where(WorkerRow.status == "online")
                )
            ).scalars().all()
            for row in rows:
                last = as_utc(row.last_heartbeat_at)
                if last is None or last < cutoff:
                    if row.id in self._sockets:
                        # A live socket is renewed liveness even if a heartbeat
                        # write raced; it will be swept once it drops.
                        continue
                    row.status = "offline"
                    went_offline.append((row.id, row.name))
        for worker_id, name in went_offline:
            await self.events.publish(
                "worker.offline",
                important=True,
                worker_id=worker_id,
                name=name,
                reason="no_heartbeat_within_window",
                window_seconds=self.settings.worker_offline_after_seconds,
            )
        if went_offline:
            await self.audit.record(
                "mesh.workers_swept_offline", worker_ids=[item[0] for item in went_offline]
            )
        return [worker_id for worker_id, _name in went_offline]

    def start(self) -> None:
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweep_task
            self._sweep_task = None
        for worker_id, socket in list(self._sockets.items()):
            with contextlib.suppress(Exception):
                await socket.close(code=1012)
            self._sockets.pop(worker_id, None)

    async def _sweep_loop(self) -> None:
        interval = max(5, self.settings.worker_heartbeat_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep_once()
            except Exception:  # noqa: BLE001 - the sweeper must outlive one bad pass
                logger.exception("Mesh heartbeat sweep failed")

    # ------------------------------------------------------------- inventory
    async def update_inventory(self, worker_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        fields = self._normalize_worker_fields(payload)
        agents = self._normalize_agents(payload.get("agents"))
        models = self._normalize_models(payload.get("models"))
        self._check_inventory_size(payload.get("inventory"))
        async with self.db.session_scope() as session:
            row = await self.get_worker_for_update(session, worker_id)
            if fields["name"]:
                row.name = fields["name"]
            if payload.get("os_family") is not None:
                row.os_family = fields["os_family"]
                row.os_version = fields["os_version"]
                row.arch = fields["arch"]
            if isinstance(payload.get("inventory"), dict):
                row.inventory = dict(payload["inventory"])
            row.capabilities = {
                **_json_object(row.capabilities),
                "agents": [item["kind"] for item in agents if item["installed"]],
                "windows_untested": row.os_family == "windows",
            }
            await self._upsert_agents(session, worker_id, agents)
            await self._upsert_models(session, worker_id, models)
        await self.events.publish(
            "worker.inventory_updated",
            important=False,
            worker_id=worker_id,
            agents=[item["kind"] for item in agents],
            models=len(models),
        )
        return {"worker_id": worker_id, "agents": len(agents), "models": len(models)}

    # ----------------------------------------------------------- revocation
    async def revoke(self, worker_id: str) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(WorkerRow, worker_id)
            if row is None:
                raise MeshError("Unknown worker.", code="not_found")
            already = row.revoked_at is not None
            row.revoked_at = utcnow()
            row.enabled = False
            row.status = "offline"
        await self.drop_socket(worker_id, reason="revoked")
        if not already:
            await self.events.publish("worker.revoked", important=True, worker_id=worker_id)
        await self.audit.record("mesh.worker_revoked", worker_id=worker_id, repeat=already)
        return {"worker_id": worker_id, "revoked": True, "already_revoked": already}

    async def set_enabled(self, worker_id: str, enabled: bool) -> dict[str, Any]:
        async with self.db.session_scope() as session:
            row = await session.get(WorkerRow, worker_id)
            if row is None:
                raise MeshError("Unknown worker.", code="not_found")
            if enabled and row.revoked_at is not None:
                raise MeshError(
                    "A revoked worker cannot be re-enabled; issue a new pairing code instead.",
                    code="forbidden",
                )
            row.enabled = enabled
            if not enabled:
                row.status = "offline"
        if not enabled:
            await self.drop_socket(worker_id, reason="disabled")
        await self.events.publish(
            "worker.enabled_changed",
            important=True,
            worker_id=worker_id,
            enabled=enabled,
        )
        await self.audit.record("mesh.worker_enabled_changed", worker_id=worker_id, enabled=enabled)
        return {"worker_id": worker_id, "enabled": enabled}

    # ------------------------------------------------------------ websocket
    async def attach(self, worker_id: str, websocket: WebSocket) -> None:
        previous = self._sockets.get(worker_id)
        self._sockets[worker_id] = websocket
        if previous is not None and previous is not websocket:
            # Reconnect replaces the old socket, per the registry contract.
            with contextlib.suppress(Exception):
                await previous.close(code=1000)

    def detach(self, worker_id: str, websocket: WebSocket) -> None:
        if self._sockets.get(worker_id) is websocket:
            self._sockets.pop(worker_id, None)

    async def drop_socket(self, worker_id: str, *, reason: str) -> bool:
        socket = self._sockets.pop(worker_id, None)
        if socket is None:
            return False
        try:
            await socket.send_json({"type": "error", "code": reason, "message": reason})
        except Exception:  # noqa: BLE001 - a dying socket may already be closed
            pass
        with contextlib.suppress(Exception):
            await socket.close(code=1008)
        return True

    async def push(self, worker_id: str, message: dict[str, Any]) -> bool:
        socket = self._sockets.get(worker_id)
        if socket is None:
            return False
        try:
            await socket.send_json(message)
            return True
        except Exception:  # noqa: BLE001 - transport failure, not a data failure
            self.detach(worker_id, socket)
            return False

    async def handle_worker_socket(self, websocket: WebSocket) -> None:
        token = websocket.query_params.get("worker_token") or websocket.headers.get(
            "x-worker-token"
        )
        try:
            worker = await self.authenticate(token)
        except MeshError:
            await websocket.close(code=1008)
            return
        worker_id = worker.id
        await websocket.accept()
        await self.attach(worker_id, websocket)
        await self.mark_online(worker_id, source="websocket")
        try:
            await websocket.send_json(
                {
                    "type": "hello",
                    "protocol_version": MESH_PROTOCOL_VERSION,
                    "worker_id": worker_id,
                    "heartbeat_seconds": self.settings.worker_heartbeat_seconds,
                    "offline_after_seconds": self.settings.worker_offline_after_seconds,
                    "max_concurrent_tasks": self.settings.worker_max_tasks,
                }
            )
            while True:
                message = await websocket.receive_json()
                reply, close_after = await self._handle_ws_message(worker_id, message)
                if reply is not None:
                    await websocket.send_json(reply)
                if close_after:
                    await websocket.close(code=1002)
                    return
        except (WebSocketDisconnect, asyncio.IncompleteReadError):
            pass
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "malformed_frame",
                        "message": "Frames must be JSON objects with a string 'type'.",
                    }
                )
                await websocket.close(code=1002)
        except Exception:  # noqa: BLE001 - a broken worker socket must not crash the server
            logger.exception("Mesh worker websocket failed for %s", worker_id)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
        finally:
            self.detach(worker_id, websocket)

    async def _handle_ws_message(
        self, worker_id: str, message: Any
    ) -> tuple[dict[str, Any] | None, bool]:
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return (
                {
                    "type": "error",
                    "code": "malformed_frame",
                    "message": "Expected a JSON object with a string 'type'.",
                },
                False,
            )
        kind = message["type"]
        try:
            if kind == "hello":
                client_version = message.get("protocol_version")
                if client_version != MESH_PROTOCOL_VERSION:
                    return (
                        {
                            "type": "error",
                            "code": "protocol_mismatch",
                            "message": (
                                f"Server speaks mesh protocol {MESH_PROTOCOL_VERSION}, "
                                f"worker reported {client_version!r}."
                            ),
                        },
                        True,
                    )
                return ({"type": "hello_ack", "worker_id": worker_id}, False)
            if kind == "heartbeat":
                data = await self.heartbeat(worker_id)
                return ({"type": "heartbeat_ack", "data": data}, False)
            if kind == "claim":
                claimed = await self.claim_next_task(worker_id)
                return ({"type": "assignment", "task": claimed}, False)
            if kind == "task_progress":
                await self.report_progress(
                    worker_id,
                    str(message.get("task_id") or ""),
                    message.get("percent"),
                    message.get("message"),
                )
                return (None, False)
            if kind == "task_result":
                await self.report_result(
                    worker_id,
                    str(message.get("task_id") or ""),
                    status=str(message.get("status") or ""),
                    output=message.get("output"),
                    error=message.get("error"),
                )
                return (None, False)
            if kind == "inventory_update":
                await self.update_inventory(worker_id, dict(message))
                return ({"type": "inventory_ack"}, False)
            return (
                {
                    "type": "error",
                    "code": "unknown_message",
                    "message": f"Unsupported worker message {kind!r}.",
                },
                False,
            )
        except MeshError as exc:
            return ({"type": "error", "code": exc.code, "message": exc.message}, False)

    # ----------------------------------------------------------------- tasks
    async def _live_or_online(self, row: WorkerRow) -> bool:
        return row.status == "online" or self.is_live(row.id)

    async def count_claimable(self, worker_id: str) -> int:
        async with self.db.session_scope() as session:
            row = await session.get(WorkerRow, worker_id)
            if row is None or not row.enabled or row.revoked_at is not None:
                return 0
            if not await self._live_or_online(row):
                return 0
            running = (
                await session.execute(
                    select(TaskRow.id).where(TaskRow.worker_id == worker_id,
                                             TaskRow.status == "running")
                )
            ).scalars().all()
            capacity = max(0, self.settings.worker_max_tasks - len(running))
            if capacity == 0:
                return 0
            claimable = (
                await session.execute(
                    select(TaskRow.id).where(
                        TaskRow.worker_id == worker_id,
                        TaskRow.status.in_(_CLAIMABLE_STATUSES),
                    ).limit(100)
                )
            ).scalars().all()
            return min(len(claimable), capacity)

    async def list_claimable(self, worker_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.session_scope() as session:
            worker = await session.get(WorkerRow, worker_id)
            if worker is None or not worker.enabled or worker.revoked_at is not None:
                raise MeshError("Worker is not active.", code="worker_disabled")
            if not await self._live_or_online(worker):
                return []
            rows = (
                await session.execute(
                    select(TaskRow)
                    .where(
                        TaskRow.worker_id == worker_id,
                        TaskRow.status.in_(_CLAIMABLE_STATUSES),
                    )
                    .order_by(TaskRow.priority, TaskRow.created_at)
                    .limit(limit)
                )
            ).scalars().all()
        return [self._task_payload(row) for row in rows]

    async def claim_next_task(self, worker_id: str) -> dict[str, Any] | None:
        candidates = await self.list_claimable(worker_id, limit=1)
        if not candidates:
            return None
        return await self.claim_task(worker_id, candidates[0]["id"])

    async def claim_task(self, worker_id: str, task_id: str) -> dict[str, Any]:
        now = utcnow()
        async with self.db.session_scope() as session:
            worker = await self.get_worker_for_update(session, worker_id)
            if not await self._live_or_online(worker):
                raise MeshError(
                    "Worker is offline; no task is handed out.", code="worker_offline"
                )
            running_tasks = (
                await session.execute(
                    select(TaskRow.id).where(
                        TaskRow.worker_id == worker_id, TaskRow.status == "running"
                    )
                )
            ).scalars().all()
            if len(running_tasks) >= self.settings.worker_max_tasks:
                raise MeshError(
                    f"Worker already runs {len(running_tasks)} tasks "
                    f"(limit {self.settings.worker_max_tasks}).",
                    code="capacity",
                )
            task = await session.get(TaskRow, task_id)
            if task is None:
                raise MeshError("Unknown task.", code="not_found")
            if task.worker_id != worker_id:
                raise MeshError("Task is not assigned to this worker.", code="forbidden")
            if task.status not in _CLAIMABLE_STATUSES:
                raise MeshError(
                    f"Task status {task.status!r} is not claimable.", code="conflict"
                )
            task.status = "running"
            task.started_at = now
            task.attempts += 1
            task.progress = 0
            task.error = None
            worker.current_task_id = task_id
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    kind="claimed",
                    message=f"Claimed by worker {worker_id}.",
                    data={"worker_id": worker_id},
                )
            )
            payload = self._task_payload(task)
        await self.events.publish(
            "task.running", important=False, task_id=task_id, worker_id=worker_id
        )
        return payload

    async def report_progress(
        self, worker_id: str, task_id: str, percent: Any, message: Any
    ) -> dict[str, Any]:
        note = bounded_text(message, 500)
        value = _bounded_int(percent)
        async with self.db.session_scope() as session:
            await self.get_worker_for_update(session, worker_id)
            task = await session.get(TaskRow, task_id)
            if task is None:
                raise MeshError("Unknown task.", code="not_found")
            if task.worker_id != worker_id:
                raise MeshError("Task does not belong to this worker.", code="forbidden")
            if task.status != "running":
                raise MeshError(
                    f"Task status {task.status!r} does not accept progress.", code="conflict"
                )
            if value is not None:
                task.progress = value
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    kind="progress",
                    message=note,
                    data={"percent": value, "source": "worker"},
                )
            )
        await self.events.publish(
            "task.progress", important=False, task_id=task_id, percent=value, message=note
        )
        return {"task_id": task_id, "progress": value}

    async def report_result(
        self,
        worker_id: str,
        task_id: str,
        *,
        status: str,
        output: Any,
        error: Any,
    ) -> dict[str, Any]:
        if status not in _FINAL_STATUSES:
            raise MeshError(
                f"Result status must be one of {sorted(_FINAL_STATUSES)}.", code="invalid"
            )
        now = utcnow()
        text = bounded_text(output, MAX_TASK_OUTPUT_CHARS, fallback="")
        failure = bounded_text(error, 4000, fallback="") or None
        async with self.db.session_scope() as session:
            worker = await self.get_worker_for_update(session, worker_id)
            task = await session.get(TaskRow, task_id)
            if task is None:
                raise MeshError("Unknown task.", code="not_found")
            if task.worker_id != worker_id:
                raise MeshError("Task does not belong to this worker.", code="forbidden")
            if task.status != "running":
                raise MeshError(
                    f"Task status {task.status!r} does not accept a result.", code="conflict"
                )
            # The worker protocol says "succeeded"; the rest of the control plane
            # (scheduler gates, dependency checks, dashboard) uses "completed".
            task.status = "completed" if status == "succeeded" else status
            task.output = text
            task.error = failure if status == "failed" else (failure or None)
            task.ended_at = now
            task.progress = 100 if status == "succeeded" else task.progress
            if worker.current_task_id == task_id:
                worker.current_task_id = None
            session.add(
                TaskEventRow(
                    task_id=task_id,
                    kind="result",
                    message=status,
                    data={"worker_id": worker_id, "error": failure},
                )
            )
        persisted = "completed" if status == "succeeded" else status
        await self.events.publish(
            f"task.{persisted}",
            task_id=task_id,
            worker_id=worker_id,
            error=failure,
        )
        await self.audit.record(
            "mesh.task_result", actor=worker_id, task_id=task_id, status=persisted
        )
        return {"task_id": task_id, "status": persisted}

    @staticmethod
    def _task_payload(task: TaskRow) -> dict[str, Any]:
        return {
            "id": task.id,
            "project_id": task.project_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "agent_kind": task.agent_kind,
            "model_id": task.model_id,
            "prompt": task.prompt,
            "priority": task.priority,
            "progress": task.progress,
            "attempts": task.attempts,
            "created_at": iso(task.created_at),
        }

    # -------------------------------------------------------------- snapshot
    async def snapshot(self) -> dict[str, Any]:
        now = utcnow()
        async with self.db.session_scope() as session:
            workers = (
                await session.execute(select(WorkerRow).order_by(WorkerRow.name))
            ).scalars().all()
            agents = (await session.execute(select(AgentRow))).scalars().all()
            model_rows = (await session.execute(select(ModelRow))).scalars().all()
            active_codes = (
                await session.execute(
                    select(PairingCodeRow).where(
                        PairingCodeRow.used_at.is_(None),
                        PairingCodeRow.expires_at > now,
                    )
                )
            ).scalars().all()
        agents_by_worker: dict[str, list[dict[str, Any]]] = {}
        for agent in agents:
            agents_by_worker.setdefault(agent.worker_id, []).append(
                {
                    "kind": agent.kind,
                    "path": agent.path,
                    "version": agent.version,
                    "installed": agent.installed,
                    "usable": agent.usable,
                    "detected_at": iso(agent.detected_at),
                }
            )
        models_by_worker: dict[str, int] = {}
        for model in model_rows:
            if model.worker_id:
                models_by_worker[model.worker_id] = models_by_worker.get(model.worker_id, 0) + 1
        items = []
        for row in workers:
            last = as_utc(row.last_heartbeat_at)
            age = (now - last).total_seconds() if last is not None else None
            items.append(
                {
                    "id": row.id,
                    "name": row.name,
                    "os_family": row.os_family,
                    "os_version": row.os_version,
                    "arch": row.arch,
                    "status": row.status,
                    "enabled": row.enabled,
                    "revoked": row.revoked_at is not None,
                    "connected": self.is_live(row.id),
                    "paired_at": iso(row.paired_at),
                    "revoked_at": iso(row.revoked_at),
                    "last_heartbeat_at": iso(row.last_heartbeat_at),
                    "heartbeat_age_seconds": round(age, 1) if age is not None else None,
                    "inventory": row.inventory or {},
                    "capabilities": row.capabilities or {},
                    "current_task_id": row.current_task_id,
                    "agents": agents_by_worker.get(row.id, []),
                    "model_count": models_by_worker.get(row.id, 0),
                }
            )
        return {
            "workers": items,
            "counts": {
                "workers": len(items),
                "online": sum(1 for item in items if item["status"] == "online"),
                "revoked": sum(1 for item in items if item["revoked"]),
            },
            "pairing": {
                "active_codes": [
                    {"label": code.label, "expires_at": iso(code.expires_at)}
                    for code in active_codes
                ],
                "ttl_seconds": self.settings.pairing_code_ttl_seconds,
            },
            "protocol_version": MESH_PROTOCOL_VERSION,
        }
