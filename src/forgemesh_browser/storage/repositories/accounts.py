"""Account registry persistence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import AccountRow, utcnow


async def get(
    session: AsyncSession, profile_id: str, provider_id: str
) -> AccountRow | None:
    result = await session.execute(
        select(AccountRow).where(
            AccountRow.profile_id == profile_id,
            AccountRow.provider_id == provider_id,
        )
    )
    return result.scalar_one_or_none()


async def upsert(
    session: AsyncSession,
    profile_id: str,
    provider_id: str,
    label: str = "",
) -> AccountRow:
    row = await get(session, profile_id, provider_id)
    if row is None:
        row = AccountRow(
            profile_id=profile_id, provider_id=provider_id, label=label or provider_id
        )
        session.add(row)
        await session.flush()
    elif label:
        row.label = label
        row.updated_at = utcnow()
    return row


async def list_all(
    session: AsyncSession,
    *,
    profile_id: str | None = None,
    provider_id: str | None = None,
) -> list[AccountRow]:
    query = select(AccountRow).order_by(AccountRow.provider_id, AccountRow.profile_id)
    if profile_id:
        query = query.where(AccountRow.profile_id == profile_id)
    if provider_id:
        query = query.where(AccountRow.provider_id == provider_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_status(
    session: AsyncSession,
    account_id: int,
    status: str,
    *,
    error: str | None = None,
    retry_after: datetime | None = None,
    ok: bool = False,
) -> AccountRow | None:
    row = await session.get(AccountRow, account_id)
    if row is None:
        return None
    row.status = status
    row.last_checked_at = utcnow()
    row.updated_at = utcnow()
    if ok:
        row.last_ok_at = row.last_checked_at
        row.last_error = None
        row.last_error_at = None
        row.retry_after = None
    elif error:
        row.last_error = error
        row.last_error_at = row.last_checked_at
        row.retry_after = retry_after
    return row
