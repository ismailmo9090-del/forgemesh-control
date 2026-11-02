"""Local Ollama adapter (text + vision models)."""

from __future__ import annotations

from typing import Any

import httpx

from forgemesh_browser.ai.base import AIProvider, AIRequest, AIResponse
from forgemesh_browser.core.exceptions import AIProviderError
from forgemesh_browser.settings import Settings


class OllamaProvider(AIProvider):
    provider_id = "ollama"
    supports_vision = True
    is_local = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.host = settings.ollama_host.rstrip("/")
        self.default_model = settings.ollama_model

    async def complete(self, request: AIRequest) -> AIResponse:
        model = request.model or self.default_model
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        user: dict[str, Any] = {"role": "user", "content": request.prompt}
        if request.images:
            user["images"] = [image.data_base64 for image in request.images]
        messages.append(user)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        if request.output_format == "json":
            payload["format"] = "json"
        try:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                response = await client.post(f"{self.host}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise AIProviderError(
                f"Ollama returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AIProviderError(
                f"Ollama is not reachable at {self.host}: {exc}. "
                "Is `ollama serve` running?"
            ) from exc
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise AIProviderError("Ollama response did not contain message.content")
        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
        }
        return AIResponse(text=content.strip(), provider=self.provider_id, model=model, usage=usage)

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.host}/api/tags")
                response.raise_for_status()
                models = [model["name"] for model in response.json().get("models", [])]
            return {
                "provider": self.provider_id,
                "available": True,
                "host": self.host,
                "models": models,
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "provider": self.provider_id,
                "available": False,
                "host": self.host,
                "error": str(exc),
            }

    async def list_models(self) -> list[str]:
        health = await self.health()
        return list(health.get("models") or [])
