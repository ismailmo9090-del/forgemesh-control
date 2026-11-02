"""Account registry models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AccountInfo(BaseModel):
    id: int
    profile_id: str
    provider_id: str
    provider_name: str
    label: str
    status: str
    last_checked_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    retry_after: datetime | None = None


class ProviderAvailability(BaseModel):
    provider_id: str
    provider_name: str
    accounts: list[AccountInfo] = Field(default_factory=list)
    available: int = 0
    needs_reauth: int = 0
    rate_limited: int = 0
    errors: int = 0


class AccountStatusSummary(BaseModel):
    total: int = 0
    available: int = 0
    needs_reauth: int = 0
    rate_limited: int = 0
    errors: int = 0
    unknown: int = 0
    providers: list[ProviderAvailability] = Field(default_factory=list)


class IdentityInfo(BaseModel):
    """One email address plus the profile that stores its sessions.

    ``has_password`` is a boolean only: the vault never exposes the password
    itself through the API, the CLI or the task records.
    """

    email: str
    label: str = ""
    profile_id: str
    has_password: bool = False
    password_providers: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    last_login_at: datetime | None = None
    accounts: list[AccountInfo] = Field(default_factory=list)


class IdentityLoginResult(BaseModel):
    email: str
    profile_id: str
    provider_id: str
    provider_name: str = ""
    #: already_logged_in | logged_in | needs_human | needs_credential |
    #: blocked | error
    status: str = "unknown"
    detail: str = ""
    url: str = ""
    filled: list[str] = Field(default_factory=list)
    headless: bool = False


class IdentityLoginSummary(BaseModel):
    total: int = 0
    already_logged_in: int = 0
    logged_in: int = 0
    needs_human: int = 0
    needs_credential: int = 0
    blocked: int = 0
    errors: int = 0
    results: list[IdentityLoginResult] = Field(default_factory=list)
