"""Domain exceptions with stable error codes for CLI/API responses."""

from __future__ import annotations

from datetime import datetime


class ForgeMeshError(Exception):
    code = "forgemesh_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ConfigError(ForgeMeshError):
    code = "config_error"
    http_status = 500


class ProfileError(ForgeMeshError):
    code = "profile_error"


class ProfileNotFound(ProfileError):
    code = "profile_not_found"
    http_status = 404


class ProfileInUse(ProfileError):
    code = "profile_in_use"
    http_status = 409


class BrowserError(ForgeMeshError):
    code = "browser_error"
    http_status = 500


class BrowserNotRunning(BrowserError):
    code = "browser_not_running"
    http_status = 409


class BrowserLimitError(BrowserError):
    code = "browser_limit"
    http_status = 429


class NavigationError(BrowserError):
    code = "navigation_error"
    http_status = 502


class AutomationError(ForgeMeshError):
    code = "automation_error"
    http_status = 422


class ElementNotFound(AutomationError):
    code = "element_not_found"
    http_status = 404


class AmbiguousTarget(AutomationError):
    code = "ambiguous_target"
    http_status = 409


class AccountError(ForgeMeshError):
    code = "account_error"


class NotAuthenticated(AccountError):
    code = "not_authenticated"
    http_status = 401


class NeedsHuman(AccountError):
    """A login step needs a human (CAPTCHA, MFA/OTP, device verification).

    ForgeMesh never bypasses these checks. The workflow stops here and reports
    what the person has to finish in the browser window.
    """

    code = "needs_human"
    http_status = 428


class IdentityError(AccountError):
    code = "identity_error"


class IdentityNotFound(IdentityError):
    code = "identity_not_found"
    http_status = 404


class AdapterError(AccountError):
    code = "adapter_error"
    http_status = 502


class RateLimited(AccountError):
    code = "rate_limited"
    http_status = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after: datetime | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.retry_after = retry_after


class AIProviderError(ForgeMeshError):
    code = "ai_provider_error"
    http_status = 502


class AIDependencyError(ForgeMeshError):
    code = "ai_dependency_missing"
    http_status = 503


class SecurityError(ForgeMeshError):
    code = "security_error"
    http_status = 403


class VaultError(SecurityError):
    code = "vault_error"


class CredentialMissing(VaultError):
    code = "credential_missing"
    http_status = 409


class MediaError(ForgeMeshError):
    code = "media_error"


class DownloadTimeout(MediaError):
    code = "download_timeout"
    http_status = 504


class DaemonError(ForgeMeshError):
    code = "daemon_error"
    http_status = 503
