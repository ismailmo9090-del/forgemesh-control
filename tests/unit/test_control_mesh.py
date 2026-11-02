"""Mesh subsystem tests: HTTP routes on a stub runtime + service-level flows.

The HTTP half mounts only the workers router on a small local app with a stub
``app.state.runtime`` (creating the app via ``create_app`` would shadow
``GET /api/workers`` with the pre-existing system route and boot unrelated
subsystems). The task lifecycle and sweep run against a real ControlDatabase
in the test loop. No second machine and no fabricated telemetry is involved.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from forgemesh_control.api.routes import workers as workers_routes
from forgemesh_control.audit import AuditService
from forgemesh_control.config import ControlSettings
from forgemesh_control.events import EventStore
from forgemesh_control.mesh import MeshError, MeshService
from forgemesh_control.models import (
    ProjectRow,
    TaskEventRow,
    TaskRow,
    WorkerRow,
    utcnow,
)
from forgemesh_control.security import hash_token
from forgemesh_control.storage import ControlDatabase

CONTROL_TOKEN = "test-control-token"
DASH_HEADERS = {"X-API-Token": CONTROL_TOKEN}


def _settings(tmp_path: Path, **overrides: Any) -> ControlSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "config_dir": tmp_path / "config",
        "auto_install_agents": False,
        "frontend_dist": tmp_path / "missing-dist",
        "worker_heartbeat_seconds": 5,
        "worker_offline_after_seconds": 30,
        "pairing_code_ttl_seconds": 120,
    }
    base.update(overrides)
    return ControlSettings(**base)


def _http_env(tmp_path: Path, **overrides: Any):
    settings = _settings(tmp_path, **overrides)
    db = ControlDatabase(settings.db_path)
    events = EventStore(db)
    audit = AuditService(db, settings.logs_dir)
    mesh = MeshService(settings, db, events, audit)
    runtime = SimpleNamespace(
        settings=settings, db=db, events=events, audit=audit, token=CONTROL_TOKEN, mesh=mesh
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await db.init()
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(workers_routes.router)
    return app, mesh


def _register_payload(code: str, device: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "device_key": device,
        "pairing_code": code,
        "name": "lab-box",
        "os_family": "linux",
        "os_version": "6.8.0",
        "arch": "x86_64",
        "cpu_count": 8,
        "protocol_version": 1,
        "inventory": {"hostname": "lab-box", "ram_total_bytes": 16_000_000_000},
        "agents": [
            {
                "kind": "opencode",
                "path": "/home/lab/.opencode/bin/opencode",
                "version": "1.19.5",
                "installed": True,
                "usable": False,
            }
        ],
        "models": [
            {"provider": "opencode", "model_id": "free/model-a", "availability": "unknown"}
        ],
    }
    payload.update(extra)
    return payload


@pytest.fixture
def mesh_http(tmp_path: Path):
    app, mesh = _http_env(tmp_path)
    with TestClient(app) as client:
        yield client, mesh


def _pair_and_register(client: TestClient, device: str | None = None) -> tuple[str, str]:
    device = device or f"dev-{uuid.uuid4().hex}"
    pairing = client.post("/api/workers/pairing", headers=DASH_HEADERS, json={"label": "unit"})
    assert pairing.status_code == 200
    code = pairing.json()["code"]
    registered = client.post("/api/mesh/register", json=_register_payload(code, device))
    assert registered.status_code == 200, registered.text
    body = registered.json()
    return body["worker_id"], body["token"]


# ------------------------------------------------------------------ pairing/API
def test_pairing_happy_path_token_once_and_reuse_refused(mesh_http) -> None:
    client, _mesh = mesh_http
    pairing = client.post("/api/workers/pairing", headers=DASH_HEADERS, json={"label": "laptop"})
    assert pairing.status_code == 200
    data = pairing.json()
    assert set(data) >= {"code", "expires_at", "label", "worker_join_url"}
    assert data["worker_join_url"] == "/api/mesh/register"

    device = f"dev-{uuid.uuid4().hex}"
    first = client.post("/api/mesh/register", json=_register_payload(data["code"], device))
    assert first.status_code == 200
    body = first.json()
    assert body["token"] and len(body["token"]) >= 32

    # The same code is single-use.
    again = client.post("/api/mesh/register", json=_register_payload(data["code"], device))
    assert again.status_code == 401
    assert again.json()["detail"]["code"] == "pairing_used"

    heartbeat = client.post("/api/mesh/heartbeat", headers={"X-Worker-Token": body["token"]})
    assert heartbeat.status_code == 200


def test_expired_pairing_code_fails_and_is_audited(mesh_http, monkeypatch) -> None:
    client, _mesh = mesh_http
    pairing = client.post("/api/workers/pairing", headers=DASH_HEADERS, json={"label": "ttl"})
    code = pairing.json()["code"]
    real_utcnow = utcnow
    monkeypatch.setattr("forgemesh_control.mesh.utcnow", lambda: real_utcnow() + timedelta(hours=2))
    result = client.post(
        "/api/mesh/register", json=_register_payload(code, f"dev-{uuid.uuid4().hex}")
    )
    monkeypatch.undo()
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "pairing_expired"
    audit_text = (_mesh.audit.path).read_text(encoding="utf-8") if _mesh.audit.path.exists() else ""
    assert "mesh.pairing_failed" in audit_text


def test_unknown_pairing_code_rejected(mesh_http) -> None:
    client, _mesh = mesh_http
    result = client.post(
        "/api/mesh/register", json=_register_payload("ZZZZZZZZ", f"dev-{uuid.uuid4().hex}")
    )
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "pairing_invalid"


def test_dashboard_routes_require_control_token(mesh_http) -> None:
    client, _mesh = mesh_http
    assert client.get("/api/workers").status_code == 401
    assert client.get("/api/workers", headers={"X-API-Token": "wrong"}).status_code == 401
    assert client.get("/api/workers", headers=DASH_HEADERS).status_code == 200
    # The mesh half must not require the dashboard token, and vice versa:
    assert client.post("/api/mesh/heartbeat").status_code == 401
    assert client.post(
        "/api/mesh/heartbeat", headers={"X-API-Token": CONTROL_TOKEN}
    ).status_code == 401


def test_heartbeat_marks_online_and_snapshot_shows_unknowns_as_unknown(mesh_http) -> None:
    client, _mesh = mesh_http
    worker_id, token = _pair_and_register(client)
    snapshot = client.get("/api/workers", headers=DASH_HEADERS).json()
    entry = next(w for w in snapshot["workers"] if w["id"] == worker_id)
    # Registration alone must never show the worker as online.
    assert entry["status"] == "offline"
    assert entry["connected"] is False
    assert entry["last_heartbeat_at"] is None

    ack = client.post("/api/mesh/heartbeat", headers={"X-Worker-Token": token})
    assert ack.status_code == 200
    assert isinstance(ack.json()["tasks_pending"], int)

    snapshot = client.get("/api/workers", headers=DASH_HEADERS).json()
    entry = next(w for w in snapshot["workers"] if w["id"] == worker_id)
    assert entry["status"] == "online"
    assert entry["last_heartbeat_at"] is not None
    assert entry["inventory"]["hostname"] == "lab-box"
    assert entry["agents"][0]["kind"] == "opencode"
    assert entry["agents"][0]["usable"] is False
    assert entry["model_count"] == 1


def test_inventory_update_persists_agents_and_models(mesh_http) -> None:
    client, _mesh = mesh_http
    worker_id, token = _pair_and_register(client)
    headers = {"X-Worker-Token": token}
    update = client.post(
        "/api/mesh/inventory",
        headers=headers,
        json={
            "inventory": {"ram_total_bytes": 32_000_000_000},
            "agents": [
                {"kind": "opencode", "path": "/x", "version": "2.0.0", "installed": True},
                {"kind": "qoder", "path": "/y", "version": "1.1.81", "installed": True},
            ],
            "models": [
                {"provider": "qoder", "model_id": "premium/x", "availability": "nonsense"}
            ],
        },
    )
    assert update.status_code == 200
    snapshot = client.get("/api/workers", headers=DASH_HEADERS).json()
    entry = next(w for w in snapshot["workers"] if w["id"] == worker_id)
    assert entry["inventory"]["ram_total_bytes"] == 32_000_000_000
    assert {a["kind"] for a in entry["agents"]} == {"opencode", "qoder"}
    assert entry["capabilities"]["agents"] == ["opencode", "qoder"]


def test_revoke_refuses_heartbeat_reenable_and_reconnect(mesh_http) -> None:
    client, _mesh = mesh_http
    worker_id, token = _pair_and_register(client)
    assert client.post("/api/mesh/heartbeat", headers={"X-Worker-Token": token}).status_code == 200

    revoked = client.post(f"/api/workers/{worker_id}/revoke", headers=DASH_HEADERS)
    assert revoked.status_code == 200

    refused = client.post("/api/mesh/heartbeat", headers={"X-Worker-Token": token})
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "worker_revoked"

    reenable = client.post(
        f"/api/workers/{worker_id}/enabled", headers=DASH_HEADERS, json={"enabled": True}
    )
    assert reenable.status_code == 403

    # Revocation cannot be undone by reconnecting the websocket.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/worker?worker_token={token}") as socket:
            socket.receive_json()

    snapshot = client.get("/api/workers", headers=DASH_HEADERS).json()
    entry = next(w for w in snapshot["workers"] if w["id"] == worker_id)
    assert entry["revoked"] is True and entry["status"] == "offline"


def test_disable_and_enable_toggle(mesh_http) -> None:
    client, _mesh = mesh_http
    worker_id, token = _pair_and_register(client)
    headers = {"X-Worker-Token": token}
    assert client.post("/api/mesh/heartbeat", headers=headers).status_code == 200

    off = client.post(
        f"/api/workers/{worker_id}/enabled", headers=DASH_HEADERS, json={"enabled": False}
    )
    assert off.status_code == 200
    refused = client.post("/api/mesh/heartbeat", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "worker_disabled"

    on = client.post(
        f"/api/workers/{worker_id}/enabled", headers=DASH_HEADERS, json={"enabled": True}
    )
    assert on.status_code == 200
    assert client.post("/api/mesh/heartbeat", headers=headers).status_code == 200


# ------------------------------------------------------------------- websocket
def test_ws_rejects_bad_and_missing_tokens(mesh_http) -> None:
    client, _mesh = mesh_http
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/worker?worker_token=not-a-real-token") as socket:
            socket.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/worker") as socket:
            socket.receive_json()


def test_ws_heartbeat_and_task_handshake_and_bad_frames(mesh_http) -> None:
    client, _mesh = mesh_http
    worker_id, token = _pair_and_register(client)
    with client.websocket_connect(f"/ws/worker?worker_token={token}") as socket:
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        assert hello["heartbeat_seconds"] == 5
        socket.send_json({"type": "hello", "protocol_version": 1, "device_key": "x"})
        assert socket.receive_json()["type"] == "hello_ack"
        socket.send_json({"type": "heartbeat"})
        ack = socket.receive_json()
        assert ack["type"] == "heartbeat_ack"
        assert ack["data"]["tasks_pending"] == 0
        socket.send_json({"type": "claim"})
        assignment = socket.receive_json()
        assert assignment == {"type": "assignment", "task": None}
        socket.send_json({"type": "nonsense"})
        error = socket.receive_json()
        assert error["code"] == "unknown_message"
        socket.send_text("this is not json {{{")
        error = socket.receive_json()
        assert error["code"] == "malformed_frame"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    snapshot = client.get("/api/workers", headers=DASH_HEADERS).json()
    entry = next(w for w in snapshot["workers"] if w["id"] == worker_id)
    assert entry["status"] == "online"  # observed heartbeat; liveness is heartbeat-based


def test_ws_protocol_mismatch_closes_without_crash(mesh_http) -> None:
    client, _mesh = mesh_http
    _worker_id, token = _pair_and_register(client)
    with client.websocket_connect(f"/ws/worker?worker_token={token}") as socket:
        socket.receive_json()  # server hello
        socket.send_json({"type": "hello", "protocol_version": 99})
        error = socket.receive_json()
        assert error["code"] == "protocol_mismatch"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
    # The server is still healthy afterwards.
    assert client.get("/api/workers", headers=DASH_HEADERS).status_code == 200


# ------------------------------------------------------------- task HTTP paths
def test_worker_endpoints_reject_dashboard_token_and_unknown_tasks(mesh_http) -> None:
    client, _mesh = mesh_http
    _worker_id, token = _pair_and_register(client)
    headers = {"X-Worker-Token": token}
    assert client.post("/api/mesh/heartbeat", headers=headers).status_code == 200
    pending = client.get("/api/mesh/tasks/pending", headers=headers)
    assert pending.status_code == 200
    # No tasks exist: the server must hand out nothing, not fabricate work.
    assert pending.json()["tasks"] == []
    assert client.post("/api/mesh/tasks/tsk_missing/claim", headers=headers).status_code == 404
    bad = client.post(
        "/api/mesh/tasks/tsk_missing/result", headers=headers, json={"status": "succeeded"}
    )
    assert bad.status_code == 404


# --------------------------------------------------------- service-level flows
@pytest.fixture
async def mesh_service(tmp_path: Path):
    settings = _settings(tmp_path)
    db = ControlDatabase(settings.db_path)
    await db.init()
    events = EventStore(db)
    audit = AuditService(db, settings.logs_dir)
    mesh = MeshService(settings, db, events, audit)
    yield mesh, db, settings, events
    await db.close()


async def _svc_pair(mesh: MeshService, device: str | None = None) -> tuple[str, str]:
    created = await mesh.create_pairing_code("svc")
    device = device or f"dev-{uuid.uuid4().hex}"
    result = await mesh.register(_register_payload(created["code"], device))
    return result["worker_id"], result["token"]


async def test_register_stores_only_the_token_hash(mesh_service) -> None:
    mesh, db, _settings, _events = mesh_service
    worker_id, token = await _svc_pair(mesh)
    row = await mesh.get_worker(worker_id)
    assert row.token_hash == hash_token(token)
    assert token not in row.token_hash
    authed = await mesh.authenticate(token)
    assert authed.id == worker_id
    with pytest.raises(MeshError):
        await mesh.authenticate(token + "x")
    history = await mesh.events.history(limit=50)
    assert any(item["type"] == "worker.paired" for item in history)


async def test_sweep_marks_stale_workers_offline(mesh_service) -> None:
    mesh, db, _settings, _events = mesh_service
    worker_id, token = await _svc_pair(mesh)
    assert (await mesh.get_worker(worker_id)).status == "offline"

    await mesh.heartbeat(worker_id)
    assert (await mesh.get_worker(worker_id)).status == "online"

    async with db.session_scope() as session:
        row = await session.get(WorkerRow, worker_id)
        assert row is not None
        row.last_heartbeat_at = utcnow() - timedelta(seconds=90)

    went_offline = await mesh.sweep_once()
    assert worker_id in went_offline
    assert (await mesh.get_worker(worker_id)).status == "offline"
    history = await mesh.events.history(limit=50)
    assert any(
        item["type"] == "worker.offline" and item["data"]["worker_id"] == worker_id
        for item in history
    )
    # A worker that never heartbeats again is not resurrected by the sweep.
    assert await mesh.sweep_once() == []
    del token


async def test_sweep_ignores_never_online_workers(mesh_service) -> None:
    mesh, _db, _settings, _events = mesh_service
    worker_id, _token = await _svc_pair(mesh)
    assert await mesh.sweep_once() == []
    assert (await mesh.get_worker(worker_id)).status == "offline"


async def test_task_claim_lifecycle_is_honest(mesh_service) -> None:
    mesh, db, _settings, _events = mesh_service
    worker_id, _token = await _svc_pair(mesh)
    other_worker_id, _other_token = await _svc_pair(mesh, device=f"dev-{uuid.uuid4().hex}")
    await mesh.heartbeat(worker_id)

    async with db.session_scope() as session:
        session.add(ProjectRow(id="prj_mesh_t", name="Mesh test project"))
        session.add(
            TaskRow(
                id="tsk_mesh_a",
                project_id="prj_mesh_t",
                title="Implement thing",
                status="queued",
                worker_id=worker_id,
                prompt="Say hello",
                agent_kind="opencode",
            )
        )
        session.add(
            TaskRow(
                id="tsk_mesh_b",
                project_id="prj_mesh_t",
                title="Other worker task",
                status="queued",
                worker_id=other_worker_id,
            )
        )

    claimable = await mesh.list_claimable(worker_id)
    assert [t["id"] for t in claimable] == ["tsk_mesh_a"]

    # A worker may never claim a task assigned to somebody else.
    await mesh.heartbeat(other_worker_id)
    with pytest.raises(MeshError) as wrong_worker:
        await mesh.claim_task(other_worker_id, "tsk_mesh_a")
    assert wrong_worker.value.code == "forbidden"

    task = await mesh.claim_task(worker_id, "tsk_mesh_a")
    assert task["status"] == "running"

    with pytest.raises(MeshError) as second:
        await mesh.claim_task(worker_id, "tsk_mesh_a")
    assert second.value.code == "conflict"

    await mesh.report_progress(worker_id, "tsk_mesh_a", 40, "halfway")
    async with db.session_scope() as session:
        row = await session.get(TaskRow, "tsk_mesh_a")
        assert row is not None and row.progress == 40

    done = await mesh.report_result(
        worker_id, "tsk_mesh_a", status="succeeded", output="all done", error=None
    )
    # Workers report "succeeded"; the control plane persists the canonical
    # "completed" that the scheduler's gates and dependency checks read.
    assert done["status"] == "completed"
    async with db.session_scope() as session:
        row = await session.get(TaskRow, "tsk_mesh_a")
        assert row is not None
        assert row.status == "completed"
        assert row.ended_at is not None
        events = (
            await session.execute(select(TaskEventRow).where(TaskEventRow.task_id == "tsk_mesh_a"))
        ).scalars().all()
        assert {event.kind for event in events} >= {"claimed", "progress", "result"}

    with pytest.raises(MeshError) as after:
        await mesh.report_result(worker_id, "tsk_mesh_a", status="failed", output="", error="x")
    assert after.value.code == "conflict"


async def test_claim_refused_when_worker_offline_and_capacity_capped(tmp_path: Path) -> None:
    settings = _settings(tmp_path, worker_max_tasks=1)
    db = ControlDatabase(settings.db_path)
    await db.init()
    mesh = MeshService(settings, db, EventStore(db), AuditService(db, settings.logs_dir))
    try:
        worker_id, _token = await _svc_pair(mesh)
        async with db.session_scope() as session:
            session.add(ProjectRow(id="prj_cap", name="cap"))
            for index in range(3):
                session.add(
                    TaskRow(
                        id=f"tsk_cap_{index}",
                        project_id="prj_cap",
                        title=f"task {index}",
                        status="queued",
                        worker_id=worker_id,
                    )
                )
        # Offline: nothing is handed out.
        assert await mesh.list_claimable(worker_id) == []
        with pytest.raises(MeshError) as offline:
            await mesh.claim_task(worker_id, "tsk_cap_0")
        assert offline.value.code == "worker_offline"

        await mesh.heartbeat(worker_id)
        await mesh.claim_task(worker_id, "tsk_cap_0")
        with pytest.raises(MeshError) as capacity:
            await mesh.claim_task(worker_id, "tsk_cap_1")
        assert capacity.value.code == "capacity"
        assert await mesh.count_claimable(worker_id) == 0
    finally:
        await db.close()


async def test_failed_result_records_real_error(mesh_service) -> None:
    mesh, db, _settings, _events = mesh_service
    worker_id, _token = await _svc_pair(mesh)
    await mesh.heartbeat(worker_id)
    async with db.session_scope() as session:
        session.add(ProjectRow(id="prj_fail", name="fail"))
        session.add(
            TaskRow(
                id="tsk_fail",
                project_id="prj_fail",
                title="boom",
                status="queued",
                worker_id=worker_id,
            )
        )
    await mesh.claim_task(worker_id, "tsk_fail")
    await mesh.report_result(
        worker_id, "tsk_fail", status="failed", output="", error="agent crashed: ENOENT"
    )
    async with db.session_scope() as session:
        row = await session.get(TaskRow, "tsk_fail")
        assert row is not None
        assert row.status == "failed"
        assert row.error == "agent crashed: ENOENT"


# ----------------------------------------------------------- socket registry
class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def send_json(self, message: dict[str, Any]) -> None:
        if self.close_code is not None:
            raise RuntimeError("socket closed")
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


async def test_reconnect_replaces_socket_and_revoke_drops_it(mesh_service) -> None:
    mesh, _db, _settings, _events = mesh_service
    worker_id, _token = await _svc_pair(mesh)

    first = _FakeSocket()
    second = _FakeSocket()
    await mesh.attach(worker_id, first)
    await mesh.attach(worker_id, second)
    assert first.close_code == 1000
    assert mesh.is_live(worker_id)

    # Revoke drops the live socket and revocation sticks.
    await mesh.revoke(worker_id)
    assert second.close_code == 1008
    assert not mesh.is_live(worker_id)
    with pytest.raises(MeshError) as auth:
        await mesh.authenticate(_token)
    assert auth.value.code == "worker_revoked"


async def test_push_reports_delivery_honestly(mesh_service) -> None:
    mesh, _db, _settings, _events = mesh_service
    worker_id, _token = await _svc_pair(mesh)
    assert await mesh.push(worker_id, {"type": "ping"}) is False
    socket = _FakeSocket()
    await mesh.attach(worker_id, socket)
    assert await mesh.push(worker_id, {"type": "ping"}) is True
    assert socket.sent == [{"type": "ping"}]


# ------------------------------------------------------------ worker client
def test_worker_client_guard_and_paths(tmp_path: Path, monkeypatch) -> None:
    from forgemesh_control import worker_client

    monkeypatch.setattr(worker_client, "CONFIG_DIR", tmp_path / "worker-config")
    key = worker_client._device_key()
    assert len(key) >= 16
    path = worker_client._write_private("token", "secret-token")
    assert path.stat().st_mode & 0o777 == 0o600
    assert worker_client._read_private("token") == "secret-token"

    # Loopback plain http is allowed; remote plain http must exit, not send.
    assert worker_client._guard_scheme("http://127.0.0.1:8787", insecure_http=False)
    import typer as _typer

    with pytest.raises(_typer.Exit):
        worker_client._guard_scheme("http://example.invalid:8787", insecure_http=False)
    # https to a remote host is fine.
    assert worker_client._guard_scheme("https://example.invalid", insecure_http=False)
    assert worker_client._ws_url("https://example.invalid") == "wss://example.invalid"


async def test_worker_client_inventory_is_detected_not_invented(tmp_path: Path, monkeypatch) -> None:
    import platform as platform_module

    from forgemesh_control import worker_client

    monkeypatch.setattr(worker_client, "CONFIG_DIR", tmp_path / "worker-config")
    monkeypatch.setattr(worker_client, "DATA_DIR", tmp_path / "worker-data")
    inventory = await worker_client.collect_inventory("unit-box")
    assert inventory["os_family"] in {"linux", "windows", "darwin", "freebsd", "unknown"}
    if platform_module.system() == "Linux":
        assert inventory["os_family"] == "linux"
        # /proc/meminfo exists here; the value must be real, never zero-filled.
        assert inventory["inventory"]["ram_total_bytes"] > 0
        assert inventory["inventory"]["disk"]["total_bytes"] > 0
    assert inventory["protocol_version"] == 1
    assert {agent["kind"] for agent in inventory["agents"]} == {"opencode", "qoder"}
    for agent in inventory["agents"]:
        assert agent["usable"] is False  # never claimed from detection alone
