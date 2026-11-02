"""Connector for the existing ForgeMesh Browser service.

The browser runtime is a separate product. This module only speaks its HTTP API
(with the local token) and its CLI; it never imports Playwright, profiles or
provider adapters, and never stores cookies or passwords.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from forgemesh_control.config import ControlSettings

logger = logging.getLogger("forgemesh.control.browser")


class BrowserUnavailable(RuntimeError):
    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class BrowserConnector:
    def __init__(self, settings: ControlSettings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ plumbing
    def _token(self) -> str | None:
        path = self.settings.resolved_browser_token_path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None

    def _headers(self) -> dict[str, str]:
        token = self._token()
        if not token:
            raise BrowserUnavailable(
                "ForgeMesh Browser token not found. Start the browser service first: "
                "fmb serve"
            )
        return {"X-API-Token": token}

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.browser_api_url,
                timeout=self.settings.browser_timeout_seconds,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        client = await self.client()
        try:
            response = await client.request(
                method, path, headers=self._headers(), **kwargs
            )
        except httpx.RequestError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise BrowserUnavailable(
                f"Cannot reach ForgeMesh Browser at {self.settings.browser_api_url}. "
                "Start it with `fmb serve`.",
                detail=str(exc),
            ) from exc
        if response.status_code >= 400:
            self.last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            message = response.text[:300]
            try:
                body = response.json()
                message = (
                    body.get("error", {}).get("message")
                    or body.get("detail")
                    or message
                )
            except ValueError:
                pass
            raise BrowserUnavailable(
                f"ForgeMesh Browser rejected {method} {path}: {message}"
            )
        self.last_error = None
        if response.status_code == 204:
            return {}
        return response.json()

    # ---------------------------------------------------------------------- API
    async def health_check(self) -> dict[str, Any]:
        try:
            health = await self._request("GET", "/health")
            status = await self._request("GET", "/status")
        except BrowserUnavailable as exc:
            return {
                "connected": False,
                "error": exc.message,
                "url": self.settings.browser_api_url,
                "token_present": self._token() is not None,
            }
        return {
            "connected": True,
            "error": None,
            "url": self.settings.browser_api_url,
            "token_present": True,
            "service": health,
            "status": status,
        }

    async def providers(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/providers")
        return data.get("providers", data if isinstance(data, list) else [])

    async def list_profiles(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/profiles")
        return data if isinstance(data, list) else data.get("profiles", [])

    async def get_profile_status(self, profile_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/profiles/{profile_id}")

    async def open_profile(self, profile_id: str, *, headless: bool = True) -> dict[str, Any]:
        return await self._request(
            "POST", "/browser/start", json={"profile": profile_id, "headless": headless}
        )

    async def close_profile(self, profile_id: str) -> dict[str, Any]:
        return await self._request("POST", "/browser/stop", json={"profile": profile_id})

    async def create_profile(
        self, profile_id: str, *, name: str | None = None, description: str = ""
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/profiles",
            json={"id": profile_id, "name": name, "description": description},
        )

    async def rename_profile(self, profile_id: str, name: str) -> dict[str, Any]:
        return await self._request("PATCH", f"/profiles/{profile_id}", json={"name": name})

    async def delete_profile(self, profile_id: str) -> dict[str, Any]:
        """Removes the persistent profile - and with it every signed-in session
        stored inside it. Callers must have obtained an explicit confirmation."""
        return await self._request("DELETE", f"/profiles/{profile_id}", params={"confirm": True})

    async def navigate(self, profile_id: str, url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/browser/navigate", json={"profile": profile_id, "url": url}
        )

    async def begin_login(
        self, provider_id: str, profile_id: str, *, headless: bool = False
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/accounts/{provider_id}/login/begin",
            json={"profile": profile_id, "headless": headless},
        )

    async def complete_login(self, provider_id: str, profile_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/accounts/{provider_id}/login/complete", json={"profile": profile_id}
        )

    async def accounts(self, *, provider: str | None = None) -> list[dict[str, Any]]:
        params = {"provider": provider} if provider else None
        data = await self._request("GET", "/accounts", params=params)
        return data.get("accounts", [])

    async def get_session_status(self) -> dict[str, Any]:
        return await self._request("GET", "/accounts/status")

    async def ask(
        self,
        provider_id: str,
        prompt: str,
        *,
        profile_id: str | None = None,
        files: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt, "files": files or []}
        if profile_id:
            payload["profile"] = profile_id
        if timeout_ms:
            payload["timeout_ms"] = timeout_ms
        return await self._request(
            "POST", f"/accounts/{provider_id}/ask", json=payload
        )

    async def screenshot(self, provider_id: str, profile_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/accounts/{provider_id}/capture",
            json={"profile": profile_id},
        )

    async def check_account(self, provider_id: str, profile_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"/accounts/{provider_id}/check", json={"profile": profile_id}
        )

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._request("GET", "/tasks", params={"limit": limit})
        return data.get("tasks", [])

    async def events(self, limit: int = 50) -> list[dict[str, Any]]:
        data = await self._request("GET", "/events/history", params={"limit": limit})
        return data.get("events", [])

    async def resolve_profile(self, provider_id: str) -> str | None:
        """Pick an existing signed-in profile for a provider, if any."""
        rows = await self.accounts(provider=provider_id)
        available = [row for row in rows if row.get("status") == "available"]
        for row in available or rows:
            profile_id = row.get("profile_id")
            if profile_id:
                return str(profile_id)
        return None

    async def cli_version(self) -> dict[str, Any]:
        cli = self.settings.resolved_browser_cli
        if cli is None:
            return {"available": False, "path": None, "version": None}
        try:
            process = await asyncio.create_subprocess_exec(
                str(cli),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except (OSError, TimeoutError) as exc:
            return {"available": False, "path": str(cli), "version": None, "error": str(exc)}
        return {
            "available": process.returncode == 0,
            "path": str(cli),
            "version": stdout.decode(errors="replace").strip(),
        }


async def probe_browser(settings: ControlSettings) -> dict[str, Any]:
    connector = BrowserConnector(settings)
    try:
        return await connector.health_check()
    finally:
        await connector.close()


def default_browser_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "forgemesh-browser"
