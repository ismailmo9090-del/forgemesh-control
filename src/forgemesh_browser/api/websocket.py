"""Live event stream over WebSocket."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from forgemesh_browser.api.deps import is_local_host

router = APIRouter(tags=["events"])


@router.websocket("/ws")
async def websocket_events(websocket: WebSocket, token: str | None = None) -> None:
    runtime = websocket.app.state.runtime
    if not token or not hmac.compare_digest(token, runtime.api_token):
        await websocket.close(code=4401, reason="invalid token")
        return
    if not runtime.settings.api_remote and not is_local_host(
        websocket.client.host if websocket.client else None
    ):
        await websocket.close(code=4403, reason="remote access disabled")
        return

    await websocket.accept()
    try:
        await websocket.send_json(
            {
                "type": "stream.ready",
                "data": {
                    "started_at": runtime.started_at.isoformat()
                    if runtime.started_at
                    else None,
                    "history": runtime.events.history(50),
                },
            }
        )
        async with runtime.events.subscribe() as queue:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    await websocket.send_json({"type": "ping", "data": {}})
                    continue
                await websocket.send_json(event.to_dict())
    except (WebSocketDisconnect, RuntimeError):
        return
