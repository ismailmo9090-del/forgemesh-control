"""Worker endpoints: dashboard management (loopback+token) and mesh API (token).

The dashboard half (``/api/workers/*``) is protected by the existing
``require_local`` + ``require_token`` dependencies. The mesh half
(``/api/mesh/*``, ``/ws/worker``) authenticates with the worker token only —
workers live on other machines, so ``require_local`` must never apply to them.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field

from forgemesh_control.api.deps import get_runtime, require_local, require_token
from forgemesh_control.mesh import MeshError, MeshService, bounded_text
from forgemesh_control.models import WorkerRow, iso

router = APIRouter(tags=["mesh"])

DASHBOARD = [Depends(require_local), Depends(require_token)]

_MESH_STATUS = {
    "unauthorized": 401,
    "pairing_invalid": 401,
    "pairing_expired": 401,
    "pairing_used": 401,
    "forbidden": 403,
    "worker_revoked": 403,
    "worker_disabled": 403,
    "worker_offline": 409,
    "not_found": 404,
    "conflict": 409,
    "capacity": 409,
    "inventory_too_large": 413,
    "invalid": 400,
}

W = TypeVar("W", bound=Callable[..., Awaitable[Any]])


def _to_http(exc: MeshError) -> HTTPException:
    return HTTPException(
        status_code=_MESH_STATUS.get(exc.code, 400),
        detail={"code": exc.code, "message": exc.message},
    )


def mesh_errors(func: W) -> W:
    """Translate service-level MeshError into HTTP status codes."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except MeshError as exc:
            raise _to_http(exc) from exc

    return cast(W, wrapper)


def get_mesh(request: Request) -> MeshService:
    runtime = get_runtime(request)
    mesh = getattr(runtime, "mesh", None)
    if mesh is None:  # pragma: no cover - runtime wiring guarantees this
        raise HTTPException(status_code=503, detail="Mesh service is not ready")
    return mesh


async def require_worker(request: Request) -> WorkerRow:
    """Worker-token authentication; deliberately not loopback-gated."""
    token = request.headers.get("x-worker-token") or request.query_params.get("worker_token")
    try:
        return await get_mesh(request).authenticate(token)
    except MeshError as exc:
        raise _to_http(exc) from exc


# ------------------------------------------------------------------ dashboard
class PairingBody(BaseModel):
    label: str = ""


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/api/workers/pairing", dependencies=DASHBOARD)
async def create_pairing(body: PairingBody, request: Request) -> dict[str, Any]:
    return await get_mesh(request).create_pairing_code(bounded_text(body.label, 120))


@router.get("/api/workers", dependencies=DASHBOARD)
async def list_workers(request: Request) -> dict[str, Any]:
    return await get_mesh(request).snapshot()


@router.post("/api/workers/{worker_id}/revoke", dependencies=DASHBOARD)
@mesh_errors
async def revoke_worker(worker_id: str, request: Request) -> dict[str, Any]:
    return await get_mesh(request).revoke(worker_id)


@router.post("/api/workers/{worker_id}/enabled", dependencies=DASHBOARD)
@mesh_errors
async def set_worker_enabled(
    worker_id: str, body: EnabledBody, request: Request
) -> dict[str, Any]:
    return await get_mesh(request).set_enabled(worker_id, body.enabled)


# ------------------------------------------------------------------- mesh API
class WorkerAgentBody(BaseModel):
    kind: str
    path: str = ""
    version: str = ""
    installed: bool = False
    usable: bool = False
    capabilities: dict[str, Any] = Field(default_factory=dict)


class WorkerModelBody(BaseModel):
    provider: str
    model_id: str
    display_name: str = ""
    context_window: int | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    availability: str = "unknown"


class RegisterBody(BaseModel):
    device_key: str
    pairing_code: str | None = None
    name: str = ""
    os_family: str = "unknown"
    os_version: str = ""
    arch: str = ""
    protocol_version: int = 0
    cpu_count: int | None = None
    inventory: dict[str, Any] = Field(default_factory=dict)
    agents: list[WorkerAgentBody] = Field(default_factory=list)
    models: list[WorkerModelBody] = Field(default_factory=list)


class InventoryBody(BaseModel):
    name: str = ""
    os_family: str | None = None
    os_version: str = ""
    arch: str = ""
    cpu_count: int | None = None
    inventory: dict[str, Any] = Field(default_factory=dict)
    agents: list[WorkerAgentBody] = Field(default_factory=list)
    models: list[WorkerModelBody] = Field(default_factory=list)


class ProgressBody(BaseModel):
    percent: int | None = Field(default=None, ge=0, le=100)
    message: str = ""


class ResultBody(BaseModel):
    status: str
    output: str = ""
    error: str | None = None


@router.post("/api/mesh/register")
@mesh_errors
async def register_worker(request: Request, body: RegisterBody) -> dict[str, Any]:
    token = request.headers.get("x-worker-token")
    return await get_mesh(request).register(body.model_dump(), worker_token=token or None)


@router.get("/api/mesh/me")
async def worker_self(worker: WorkerRow = Depends(require_worker)) -> dict[str, Any]:
    return {
        "worker_id": worker.id,
        "name": worker.name,
        "status": worker.status,
        "enabled": worker.enabled,
        "revoked": worker.revoked_at is not None,
        "os_family": worker.os_family,
        "os_version": worker.os_version,
        "arch": worker.arch,
        "inventory": worker.inventory or {},
        "capabilities": worker.capabilities or {},
        "last_heartbeat_at": iso(worker.last_heartbeat_at),
        "current_task_id": worker.current_task_id,
    }


@router.post("/api/mesh/heartbeat")
@mesh_errors
async def worker_heartbeat(request: Request) -> dict[str, Any]:
    worker = await require_worker(request)
    return await get_mesh(request).heartbeat(worker.id)


@router.post("/api/mesh/inventory")
@mesh_errors
async def worker_inventory(body: InventoryBody, request: Request) -> dict[str, Any]:
    worker = await require_worker(request)
    return await get_mesh(request).update_inventory(worker.id, body.model_dump())


@router.get("/api/mesh/tasks/pending")
@mesh_errors
async def worker_pending_tasks(request: Request) -> dict[str, Any]:
    worker = await require_worker(request)
    mesh = get_mesh(request)
    return {
        "tasks": await mesh.list_claimable(worker.id),
        "worker_max_tasks": mesh.settings.worker_max_tasks,
    }


@router.post("/api/mesh/tasks/{task_id}/claim")
@mesh_errors
async def worker_claim_task(task_id: str, request: Request) -> dict[str, Any]:
    worker = await require_worker(request)
    task = await get_mesh(request).claim_task(worker.id, bounded_text(task_id, 40))
    return {"task": task}


@router.post("/api/mesh/tasks/{task_id}/progress")
@mesh_errors
async def worker_task_progress(task_id: str, body: ProgressBody, request: Request) -> dict:
    worker = await require_worker(request)
    return await get_mesh(request).report_progress(
        worker.id, bounded_text(task_id, 40), body.percent, body.message
    )


@router.post("/api/mesh/tasks/{task_id}/result")
@mesh_errors
async def worker_task_result(task_id: str, body: ResultBody, request: Request) -> dict:
    worker = await require_worker(request)
    return await get_mesh(request).report_result(
        worker.id, bounded_text(task_id, 40), status=body.status, output=body.output, error=body.error
    )


@router.websocket("/ws/worker")
async def worker_socket(websocket: WebSocket) -> None:
    runtime = getattr(websocket.app.state, "runtime", None)
    mesh = getattr(runtime, "mesh", None) if runtime is not None else None
    if not isinstance(mesh, MeshService):
        await websocket.close(code=1013)
        return
    await mesh.handle_worker_socket(websocket)
