"""AI provider selection, explicit configuration and failure behaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forgemesh_browser.ai.base import AIProvider, AIRequest, AIResponse
from forgemesh_browser.ai.router import AIRouter
from forgemesh_browser.core.event_bus import EventBus
from forgemesh_browser.core.exceptions import AIProviderError, MediaError
from forgemesh_browser.settings import Settings


class NonVisionProvider(AIProvider):
    provider_id = "novision"
    supports_vision = False

    async def complete(self, request: AIRequest) -> AIResponse:  # pragma: no cover
        return AIResponse(text="unused", provider=self.provider_id, model="none")

    async def health(self) -> dict[str, Any]:  # pragma: no cover
        return {"provider": self.provider_id, "available": True}


def _router(settings: Settings) -> AIRouter:
    return AIRouter(settings, EventBus())


async def test_unknown_provider_is_rejected(settings: Settings) -> None:
    router = _router(settings)
    with pytest.raises(AIProviderError) as info:
        await router.complete("hello", provider="does-not-exist")
    assert "not configured" in str(info.value)


async def test_empty_prompt_is_rejected(settings: Settings) -> None:
    with pytest.raises(AIProviderError):
        await _router(settings).complete("   ")


async def test_unreachable_ollama_reports_error(settings: Settings) -> None:
    settings = settings.model_copy(update={"ollama_host": "http://127.0.0.1:1"})
    router = _router(settings)
    health = await router.health()
    assert health["ollama"]["available"] is False
    with pytest.raises(AIProviderError) as info:
        await router.complete("hello")
    assert "not reachable" in str(info.value)


async def test_no_implicit_paid_fallback(settings: Settings) -> None:
    router = _router(settings)
    assert set(router.providers) == {"ollama"}
    with pytest.raises(AIProviderError):
        await router.complete("hello", provider="openai")


def test_openai_registers_only_when_configured(settings: Settings) -> None:
    configured = settings.model_copy(
        update={"openai_base_url": "http://127.0.0.1:9", "openai_api_key": "test-key"}
    )
    router = _router(configured)
    assert "openai" in router.providers
    providers = {item["id"]: item for item in router.list_providers()}
    assert providers["openai"]["local"] is False
    assert providers["ollama"]["local"] is True


async def test_images_require_vision_capable_provider(settings: Settings, tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-image")
    router = _router(settings)
    router.providers["novision"] = NonVisionProvider()
    with pytest.raises(AIProviderError) as info:
        await router.complete("describe", images=[image], provider="novision")
    assert "does not support image input" in str(info.value)


async def test_missing_image_path_is_rejected(settings: Settings, tmp_path: Path) -> None:
    router = _router(settings)
    router.providers["novision"] = NonVisionProvider()
    with pytest.raises(MediaError):
        await router.complete(
            "describe", images=[tmp_path / "missing.png"], provider="novision"
        )


def test_provider_listing_flags_default(settings: Settings) -> None:
    listed = _router(settings).list_providers()
    assert listed[0]["id"] == "ollama"
    assert listed[0]["default"] is True
