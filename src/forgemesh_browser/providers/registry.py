"""Provider registry loaded from bundled/user providers.yaml."""

from __future__ import annotations

import yaml
from pydantic import BaseModel

from forgemesh_browser.core.exceptions import AdapterError
from forgemesh_browser.providers.base import GenericChatProvider
from forgemesh_browser.settings import PROJECT_CONFIG, Settings


class ProviderInfo(BaseModel):
    id: str
    name: str
    url: str
    enabled: bool = True
    login_supported: bool = True


def _builtin_adapters() -> dict[str, GenericChatProvider]:
    from forgemesh_browser.providers.chatgpt.adapter import Adapter as ChatGPT
    from forgemesh_browser.providers.claude.adapter import Adapter as Claude
    from forgemesh_browser.providers.deepseek.adapter import Adapter as DeepSeek
    from forgemesh_browser.providers.gemini.adapter import Adapter as Gemini
    from forgemesh_browser.providers.grok.adapter import Adapter as Grok
    from forgemesh_browser.providers.kimi.adapter import Adapter as Kimi
    from forgemesh_browser.providers.perplexity.adapter import Adapter as Perplexity
    from forgemesh_browser.providers.sarvam.adapter import Adapter as Sarvam

    adapters = [
        ChatGPT(),
        Claude(),
        Kimi(),
        DeepSeek(),
        Grok(),
        Perplexity(),
        Gemini(),
        Sarvam(),
    ]
    return {adapter.provider_id: adapter for adapter in adapters}


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._adapters = _builtin_adapters()
        self._config = self._load_config()

    def _load_config(self) -> dict:
        config: dict = {}
        for path in (
            PROJECT_CONFIG.parent / "providers.yaml",
            self.settings.config_dir / "providers.yaml",
        ):
            if path.is_file():
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                except yaml.YAMLError as exc:
                    raise AdapterError(f"Invalid provider config {path}: {exc}") from exc
                if isinstance(data, dict):
                    config.update(data.get("providers") or {})
        return config

    def _enabled(self, provider_id: str) -> bool:
        entry = self._config.get(provider_id) or {}
        return bool(entry.get("enabled", True))

    def list(self) -> list[ProviderInfo]:
        infos = []
        for provider_id, adapter in self._adapters.items():
            entry = self._config.get(provider_id) or {}
            infos.append(
                ProviderInfo(
                    id=provider_id,
                    name=entry.get("name") or adapter.display_name,
                    url=entry.get("url") or adapter.home_url,
                    enabled=self._enabled(provider_id),
                    login_supported=True,
                )
            )
        return infos

    def get(self, provider_id: str) -> GenericChatProvider:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise AdapterError(
                f"Unknown provider '{provider_id}'. Available: "
                + ", ".join(self._adapters)
            )
        if not self._enabled(provider_id):
            raise AdapterError(
                f"Provider '{provider_id}' is disabled in config/providers.yaml"
            )
        return adapter

    def ids(self) -> list[str]:
        return list(self._adapters)

    def name(self, provider_id: str) -> str:
        for info in self.list():
            if info.id == provider_id:
                return info.name
        return provider_id
