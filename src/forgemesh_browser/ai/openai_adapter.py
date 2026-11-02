"""OpenAI-compatible adapter. Only used when explicitly configured and selected."""

from __future__ import annotations

from typing import Any

import httpx

from forgemesh_browser.ai.base import AIProvider, AIRequest, AIResponse
from forgemesh_browser.core.exceptions import AIProviderError
from forgemesh_browser.settings import Settings


class OpenAICompatibleProvider(AIProvider):
    provider_id = "openai"
    supports_vision = True
    is_local = False

    def __init__(self, settings: Settings) -> None:
        if not settings.openai_base_url or not settings.openai_api_key:
            raise AIProviderError(
                "OpenAI-compatible provider requires FMB_OPENAI_BASE_URL and "
                "FMB_OPENAI_API_KEY"
            )
        self.base_url = settings.openai_base_url.rstrip("/")
        self.api_key = settings.openai_api_key
        self.default_model = settings.openai_model
        self.timeout = settings.ai_timeout_seconds

    async def complete(self, request: AIRequest) -> AIResponse:
        model = request.model or self.default_model
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        if request.images:
            content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
            for image in request.images:
                content.append(
                    {"type": "image_url", "image_url": {"url": image.data_url}}
                )
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.output_format == "json":
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise AIProviderError(
                f"AI endpoint returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AIProviderError(f"AI endpoint request failed: {exc}") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError("AI endpoint returned an unexpected response shape") from exc
        return AIResponse(
            text=str(content).strip(),
            provider=self.provider_id,
            model=model,
            usage=data.get("usage") or {},
        )

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                response.raise_for_status()
            return {"provider": self.provider_id, "available": True, "host": self.base_url}
        except httpx.HTTPError as exc:
            return {
                "provider": self.provider_id,
                "available": False,
                "host": self.base_url,
                "error": str(exc),
            }
