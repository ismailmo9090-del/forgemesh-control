"""Identity persistence queries."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import IdentityRow, utcnow


async def get(session: AsyncSession, email: str) -> IdentityRow | None:
    result = await session.execute(select(IdentityRow).where(IdentityRow.email == email))
    return result.scalar_one_or_none()


async def get_by_profile(session: AsyncSession, profile_id: str) -> IdentityRow | None:
    result = await session.execute(
        select(IdentityRow).where(IdentityRow.profile_id == profile_id)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession, email: str, profile_id: str, label: str = ""
) -> IdentityRow:
    row = IdentityRow(email=email, profile_id=profile_id, label=label)
    session.add(row)
    await session.flush()
    return row


async def list_all(session: AsyncSession) -> list[IdentityRow]:
    result = await session.execute(select(IdentityRow).order_by(IdentityRow.email))
    return list(result.scalars().all())


async def remove(session: AsyncSession, email: str) -> bool:
    result = await session.execute(sa_delete(IdentityRow).where(IdentityRow.email == email))
    return bool(result.rowcount)


async def touch_login(session: AsyncSession, email: str) -> None:
    row = await get(session, email)
    if row is not None:
        row.last_login_at = utcnow()
