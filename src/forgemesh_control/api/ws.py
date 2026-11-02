"""Authenticated dashboard WebSocket: durable events + live metrics."""

from __future__ import annotations

import asyncio
import contextlib
import hmac

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from forgemesh_control.runtime import ControlRuntime

router = APIRouter()


def _token(websocket: WebSocket) -> str | None:
    token = websocket.query_params.get("token")
    if token:
        return token
    token = websocket.headers.get("x-api-token")
    if token:
        return token
    authorization = websocket.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return websocket.cookies.get("fmc_token")


@router.websocket("/ws")
async def dashboard_socket(websocket: WebSocket) -> None:
    runtime: ControlRuntime | None = getattr(websocket.app.state, "runtime", None)
    if runtime is None:
        await websocket.close(code=1013)
        return
    provided = _token(websocket)
    if not provided or not hmac.compare_digest(provided, runtime.token):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        await websocket.send_json(
            {"type": "snapshot", "data": await runtime.status_snapshot()}
        )
        async with runtime.events.subscribe() as queue:
            while websocket.client_state is WebSocketState.CONNECTED:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                except TimeoutError:
                    await websocket.send_json(
                        {
                            "type": "metrics",
                            "data": runtime.metrics.snapshot(),
                        }
                    )
                    continue
                await websocket.send_json({"type": "event", "data": event.to_dict()})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken socket must not crash the server
        with contextlib.suppress(Exception):
            await websocket.close(code=1011)
    finally:
        if websocket.client_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
