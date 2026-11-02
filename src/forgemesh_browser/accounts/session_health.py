"""Session status classification and provider availability summaries."""

from __future__ import annotations

from datetime import datetime

from forgemesh_browser.accounts.models import (
    AccountInfo,
    AccountStatusSummary,
    ProviderAvailability,
)
from forgemesh_browser.storage.models import AccountRow, as_utc

STATUS_UNKNOWN = "unknown"
STATUS_AVAILABLE = "available"
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ERROR = "error"
STATUS_LOGIN_PENDING = "login_pending"

KNOWN_STATUSES = {
    STATUS_UNKNOWN,
    STATUS_AVAILABLE,
    STATUS_NEEDS_REAUTH,
    STATUS_RATE_LIMITED,
    STATUS_ERROR,
    STATUS_LOGIN_PENDING,
}


def account_info(row: AccountRow, provider_name: str | None = None) -> AccountInfo:
    return AccountInfo(
        id=row.id,
        profile_id=row.profile_id,
        provider_id=row.provider_id,
        provider_name=provider_name or row.provider_id,
        label=row.label,
        status=row.status,
        last_checked_at=as_utc(row.last_checked_at),
        last_ok_at=as_utc(row.last_ok_at),
        last_error=row.last_error,
        retry_after=as_utc(row.retry_after),
    )


def summarize(accounts: list[AccountInfo]) -> AccountStatusSummary:
    summary = AccountStatusSummary(total=len(accounts))
    by_provider: dict[str, ProviderAvailability] = {}
    for account in accounts:
        bucket = by_provider.setdefault(
            account.provider_id,
            ProviderAvailability(
                provider_id=account.provider_id, provider_name=account.provider_name
            ),
        )
        bucket.accounts.append(account)
        if account.status == STATUS_AVAILABLE:
            summary.available += 1
            bucket.available += 1
        elif account.status == STATUS_NEEDS_REAUTH:
            summary.needs_reauth += 1
            bucket.needs_reauth += 1
        elif account.status == STATUS_RATE_LIMITED:
            summary.rate_limited += 1
            bucket.rate_limited += 1
        elif account.status == STATUS_ERROR:
            summary.errors += 1
            bucket.errors += 1
        else:
            summary.unknown += 1
    summary.providers = sorted(by_provider.values(), key=lambda item: item.provider_id)
    return summary


def next_retry(account: AccountInfo) -> datetime | None:
    return account.retry_after
