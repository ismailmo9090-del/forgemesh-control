"""Application settings.

Resolution order (later wins): bundled config/settings.yaml, user
~/.config/forgemesh-browser/settings.yaml, environment variables (FMB_*),
explicit overrides passed to :func:`load_settings`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_CONFIG = PACKAGE_ROOT / "config" / "settings.yaml"

#: Channels Playwright can launch, plus the two aliases this project accepts.
BROWSER_CHANNELS = {
    "",
    "auto",
    "none",
    "chrome",
    "chromium",
    "bundled",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
    "msedge-canary",
}
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
_LOG_LEVELS = {"NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "forgemesh-browser"


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "forgemesh-browser"


class Settings(BaseSettings):
    """Runtime configuration. All fields can be set via ``FMB_<FIELD>``."""

    model_config = SettingsConfigDict(
        env_prefix="FMB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "forgemesh-browser"
    data_dir: Path = Field(default_factory=default_data_dir)
    config_dir: Path = Field(default_factory=default_config_dir)

    default_profile: str = "primary"
    headless: bool = True
    viewport_width: int = Field(default=1440, ge=320, le=7680)
    viewport_height: int = Field(default=900, ge=240, le=4320)
    # Six concurrent profiles cover one seat per built-in provider. Each
    # headless Chrome costs roughly 300-500 MB, so 6 stays inside the budget of
    # a normal 8 GB machine while still letting a full council run at once.
    max_contexts: int = Field(default=6, ge=1, le=16)
    navigation_timeout_ms: int = Field(default=45_000, ge=1_000)
    action_timeout_ms: int = Field(default=15_000, ge=500)
    download_max_bytes: int = Field(default=200 * 1024 * 1024, ge=1024)
    upload_max_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    browser_channel: str | None = "chrome"
    locale: str = "en-US"
    # Hide Playwright's automation markers so identity providers (Google, etc.)
    # accept the browser for interactive OAuth. Does not bypass CAPTCHA/MFA.
    browser_stealth: bool = True

    # Encrypted credential vault. Passwords are only read by the daemon at
    # prefill time; they are never sent to worker agents or written to logs.
    vault_passphrase: SecretStr | None = None
    identity_login_timeout_seconds: int = Field(default=180, ge=10)
    identity_login_delay_seconds: float = Field(default=1.5, ge=0)

    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8765, ge=1, le=65535)
    api_remote: bool = False

    ai_provider: str = "ollama"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1"
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: int = Field(default=120, ge=5)

    ocr_lang: str = "eng"
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("data_dir", "config_dir", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if isinstance(value, (str, os.PathLike)):
            return Path(os.path.expandvars(str(value))).expanduser()
        return value

    @field_validator("browser_channel", mode="before")
    @classmethod
    def _valid_channel(cls, value: Any) -> str | None:
        if value is None:
            return None
        channel = str(value).strip().lower()
        if channel not in BROWSER_CHANNELS:
            raise ValueError(
                f"Unknown browser channel {value!r}. Use one of: "
                + ", ".join(sorted(item for item in BROWSER_CHANNELS if item))
            )
        return channel

    @field_validator("locale")
    @classmethod
    def _valid_locale(cls, value: str) -> str:
        locale = value.strip()
        if not _LOCALE_RE.fullmatch(locale):
            raise ValueError(
                f"Invalid locale {value!r}; expected a BCP-47 tag such as en-US."
            )
        return locale

    @field_validator("log_level", mode="before")
    @classmethod
    def _valid_log_level(cls, value: Any) -> str:
        level = str(value).strip().upper()
        if level not in _LOG_LEVELS:
            raise ValueError(f"Invalid log level {value!r}.")
        return level

    @property
    def profiles_dir(self) -> Path:
        return self.data_dir / "profiles"

    @property
    def downloads_dir(self) -> Path:
        return self.data_dir / "downloads"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "forgemesh.db"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def vault_path(self) -> Path:
        return self.secrets_dir / "vault.json"

    @property
    def vault_key_path(self) -> Path:
        return self.config_dir / "vault.key"

    @property
    def token_path(self) -> Path:
        return self.runtime_dir / "api_token"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def daemon_info_path(self) -> Path:
        return self.runtime_dir / "daemon.json"

    @property
    def daemon_log_path(self) -> Path:
        return self.logs_dir / "daemon.log"

    @property
    def user_settings_path(self) -> Path:
        return self.config_dir / "settings.yaml"

    def ensure_dirs(self) -> None:
        from forgemesh_browser.security.permissions import ensure_private_dir

        for directory in (
            self.data_dir,
            self.config_dir,
            self.profiles_dir,
            self.downloads_dir,
            self.screenshots_dir,
            self.assets_dir,
            self.results_dir,
            self.logs_dir,
            self.runtime_dir,
            self.secrets_dir,
        ):
            ensure_private_dir(directory)


_SETTING_KEYS = set(Settings.model_fields.keys())


def _yaml_values(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - depends on user file
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Settings file {path} must contain a mapping")
    return {k: v for k, v in data.items() if k in _SETTING_KEYS}


def load_settings(**overrides: Any) -> Settings:
    """Load settings from YAML files, environment and explicit overrides."""

    values: dict[str, Any] = {}
    values.update(_yaml_values(PROJECT_CONFIG))
    values.update(_yaml_values(default_config_dir() / "settings.yaml"))
    env_config = os.environ.get("FMB_CONFIG")
    if env_config:
        values.update(_yaml_values(Path(env_config).expanduser()))

    # Environment variables must win over YAML.
    for key in list(values):
        if f"FMB_{key.upper()}" in os.environ:
            values.pop(key)
    values.update(overrides)

    settings = Settings(**values)
    settings.ensure_dirs()
    return settings
