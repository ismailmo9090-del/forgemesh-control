"""Routes AI requests to explicitly configured providers. No automatic paid fallback."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from forgemesh_browser.ai.base import AIProvider, AIRequest, AIResponse
from forgemesh_browser.ai.multimodal import load_images
from forgemesh_browser.ai.ollama_adapter import OllamaProvider
from forgemesh_browser.ai.openai_adapter import OpenAICompatibleProvider
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import AIProviderError
from forgemesh_browser.settings import Settings

logger = logging.getLogger("forgemesh.ai")


class AIRouter:
    def __init__(self, settings: Settings, events: EventBus) -> None:
        self.settings = settings
        self.events = events
        self.providers: dict[str, AIProvider] = {"ollama": OllamaProvider(settings)}
        if settings.openai_base_url and settings.openai_api_key:
            try:
                self.providers["openai"] = OpenAICompatibleProvider(settings)
            except AIProviderError as exc:
                logger.warning("OpenAI-compatible provider disabled: %s", exc)

    def list_providers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": provider.provider_id,
                "local": provider.is_local,
                "supports_vision": provider.supports_vision,
                "default": provider.provider_id == self.settings.ai_provider,
            }
            for provider in self.providers.values()
        ]

    def _resolve(self, provider_id: str | None) -> AIProvider:
        selected = provider_id or self.settings.ai_provider
        provider = self.providers.get(selected)
        if provider is None:
            raise AIProviderError(
                f"AI provider '{selected}' is not configured. Available: "
                + ", ".join(self.providers)
            )
        return provider

    async def complete(
        self,
        prompt: str,
        *,
        images: list[str | Path] | None = None,
        provider: str | None = None,
        model: str | None = None,
        system: str | None = None,
        output_format: str = "text",
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> AIResponse:
        if not prompt.strip():
            raise AIProviderError("Prompt cannot be empty")
        chosen = self._resolve(provider)
        payloads = load_images(images or []) if images else []
        if payloads and not chosen.supports_vision:
            raise AIProviderError(
                f"Provider '{chosen.provider_id}' does not support image input"
            )
        request = AIRequest(
            prompt=prompt,
            images=payloads,
            system=system,
            model=model,
            provider=chosen.provider_id,
            output_format=output_format,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.events.publish(
            "ai.request",
            provider=chosen.provider_id,
            model=model or "default",
            prompt_chars=len(prompt),
            images=len(payloads),
        )
        response = await chosen.complete(request)
        self.events.publish(
            "ai.response",
            provider=response.provider,
            model=response.model,
            chars=len(response.text),
        )
        return response

    async def health(self) -> dict[str, Any]:
        results = {}
        for provider_id, provider in self.providers.items():
            results[provider_id] = await provider.health()
        return results

    async def models(self, provider: str | None = None) -> dict[str, Any]:
        if provider:
            return {
                provider: await self._resolve(provider).list_models()
            }
        return {
            provider_id: await provider.list_models()
            for provider_id, provider in self.providers.items()
        }
