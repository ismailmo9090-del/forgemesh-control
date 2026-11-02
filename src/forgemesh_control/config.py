"""Validated configuration for the ForgeMesh Control Center.

Every field can be overridden with ``FMC_<FIELD>`` (for example
``FMC_PORT=9100``). The control center is loopback-only unless remote access is
explicitly enabled, mirroring the browser runtime's policy.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_FRONTEND_DIST = PACKAGE_ROOT / "frontend" / "dist"


def _xdg(kind: str, fallback: str) -> Path:
    base = os.environ.get(f"XDG_{kind.upper()}_HOME")
    if base:
        return Path(base)
    return Path.home() / fallback


def default_data_dir() -> Path:
    return _xdg("data", ".local/share") / "forgemesh-control"


def default_config_dir() -> Path:
    return _xdg("config", ".config") / "forgemesh-control"


def default_browser_data_dir() -> Path:
    return _xdg("data", ".local/share") / "forgemesh-browser"


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ControlSettings(BaseSettings):
    """Runtime configuration for the control server and worker API."""

    model_config = SettingsConfigDict(
        env_prefix="FMC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "forgemesh-control"
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    allow_remote: bool = False

    data_dir: Path = Field(default_factory=default_data_dir)
    config_dir: Path = Field(default_factory=default_config_dir)
    workspace_root: Path | None = None
    frontend_dist: Path = DEFAULT_FRONTEND_DIST

    # Existing ForgeMesh Browser service. Never rebuilt here.
    browser_api_url: str = "http://127.0.0.1:8765"
    browser_token_path: Path | None = None
    browser_cli: str | None = None
    browser_timeout_seconds: int = Field(default=180, ge=5)

    # Local AI fallback (Ollama). Disabled unless the user turns it on.
    offline_ai_enabled: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1"
    offline_ai_timeout_seconds: int = Field(default=120, ge=5)

    # Council limits: bounded concurrency, never open every profile at once.
    council_max_parallel: int = Field(default=3, ge=1, le=6)
    council_timeout_seconds: int = Field(default=240, ge=20)
    council_rounds: int = Field(default=10, ge=1, le=10)
    council_round_timeout_seconds: int = Field(default=300, ge=20)

    # Coding agents. Empty means "probe PATH and known install locations".
    opencode_bin: str | None = None
    qoder_bin: str | None = None
    agent_timeout_seconds: int = Field(default=3600, ge=30)
    agent_max_concurrency: int = Field(default=2, ge=1, le=16)
    auto_install_agents: bool = True
    command_timeout_seconds: int = Field(default=900, ge=10)
    auto_queue_tasks: bool = False

    # Ordered control brain: Qoder first, OpenCode as the fallback. It writes
    # the meeting brief and the final polish text; ForgeMesh still performs the
    # browser automation itself.
    brain_enabled: bool = True
    brain_timeout_seconds: int = Field(default=150, ge=20)

    # Documents and uploads.
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    max_archive_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_archive_entries: int = Field(default=500, ge=1)
    max_extracted_chars: int = Field(default=2_000_000, ge=10_000)

    # Worker transport.
    worker_heartbeat_seconds: int = Field(default=15, ge=5)
    worker_offline_after_seconds: int = Field(default=60, ge=10)
    pairing_code_ttl_seconds: int = Field(default=600, ge=30)
    worker_max_tasks: int = Field(default=2, ge=1, le=32)

    log_level: str = "INFO"

    @field_validator("data_dir", "config_dir", "workspace_root", "frontend_dist", mode="before")
    @classmethod
    def _expand(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, os.PathLike)):
            return Path(os.path.expandvars(str(value))).expanduser()
        return value

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("host must not be empty")
        return value.strip()

    @field_validator("browser_api_url", "ollama_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"URL must start with http(s)://: {value!r}")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _local_only(self) -> ControlSettings:
        if not _is_loopback(self.host) and not self.allow_remote:
            raise ValueError(
                f"Refusing to bind to non-loopback host {self.host!r}. "
                "Set FMC_ALLOW_REMOTE=true only when the dashboard is behind TLS."
            )
        return self

    # ------------------------------------------------------------------ paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "control.db"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def token_path(self) -> Path:
        return self.runtime_dir / "control_token"

    @property
    def server_info_path(self) -> Path:
        return self.runtime_dir / "server.json"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

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
    def workspaces(self) -> Path:
        return self.workspace_root or (self.data_dir / "workspaces")

    @property
    def agent_config_root(self) -> Path:
        """Parent of the isolated CLI config roots ForgeMesh creates.

        Only ``qodercli --config-dir`` accounts live here; each subdirectory is
        one signed-in Qoder account, exactly as the CLI documents.
        """
        return self.data_dir / "agent-config"

    @property
    def resolved_browser_token_path(self) -> Path:
        if self.browser_token_path:
            return self.browser_token_path
        return default_browser_data_dir() / "runtime" / "api_token"

    @property
    def resolved_browser_cli(self) -> Path | None:
        if self.browser_cli:
            candidate = Path(self.browser_cli).expanduser()
            return candidate if candidate.exists() else None
        for candidate in (
            PROJECT_ROOT / ".venv" / "bin" / "fmb",
            Path("/usr/local/bin/fmb"),
            Path.home() / ".local/bin/fmb",
        ):
            if candidate.exists():
                return candidate
        return None

    @property
    def vault_passphrase(self) -> SecretStr | None:
        # Reuse the browser vault's passphrase setting so a single exported
        # value keeps both encrypted stores usable.
        value = os.environ.get("FMB_VAULT_PASSPHRASE") or os.environ.get(
            "FMC_VAULT_PASSPHRASE"
        )
        return SecretStr(value) if value else None

    def ensure_dirs(self) -> None:
        from forgemesh_browser.security.permissions import ensure_private_dir

        for directory in (
            self.data_dir,
            self.config_dir,
            self.runtime_dir,
            self.logs_dir,
            self.uploads_dir,
            self.artifacts_dir,
            self.reports_dir,
            self.secrets_dir,
            self.workspaces,
            self.agent_config_root,
        ):
            ensure_private_dir(directory)


def load_control_settings(**overrides: Any) -> ControlSettings:
    return ControlSettings(**overrides)
