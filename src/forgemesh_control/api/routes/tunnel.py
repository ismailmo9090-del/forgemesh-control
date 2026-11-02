"""Public-access tunnel routes (ngrok) for the control dashboard."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, SecretStr

from forgemesh_control.api.deps import get_runtime
from forgemesh_control.tunnel import TunnelService

router = APIRouter(tags=["tunnel"])


def get_tunnel(request: Request) -> TunnelService:
    return get_runtime(request).tunnel


class AuthtokenBody(BaseModel):
    token: SecretStr


@router.get("/api/tunnel")
async def tunnel_status(request: Request) -> dict[str, Any]:
    return await get_tunnel(request).status()


@router.post("/api/tunnel/authtoken")
async def save_authtoken(body: AuthtokenBody, request: Request) -> dict[str, Any]:
    # The secret goes straight to ngrok's own config command and is never
    # echoed by the route, stored in the control DB or written to the audit log.
    return await get_tunnel(request).configure_authtoken(body.token.get_secret_value())


@router.post("/api/tunnel/start")
async def start_tunnel(request: Request) -> dict[str, Any]:
    return await get_tunnel(request).start()


@router.post("/api/tunnel/stop")
async def stop_tunnel(request: Request) -> dict[str, Any]:
    return await get_tunnel(request).stop()


@router.post("/api/tunnel/install")
async def install_tunnel(request: Request) -> dict[str, Any]:
    return await get_tunnel(request).install()
