"""Provider-independent AI interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgemesh_browser.ai.multimodal import ImagePayload


@dataclass(slots=True)
class AIRequest:
    prompt: str
    images: list[ImagePayload] = field(default_factory=list)
    system: str | None = None
    model: str | None = None
    provider: str | None = None
    output_format: str = "text"
    temperature: float = 0.2
    max_tokens: int = 2048


@dataclass(slots=True)
class AIResponse:
    text: str
    provider: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


class AIProvider(ABC):
    provider_id: str = "base"
    supports_vision: bool = False
    is_local: bool = True

    @abstractmethod
    async def complete(self, request: AIRequest) -> AIResponse:  # pragma: no cover
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        return []


def image_paths(images: list[ImagePayload]) -> list[Path]:
    return [image.path for image in images]
