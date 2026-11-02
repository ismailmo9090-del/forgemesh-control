"""Profile persistence queries."""

from __future__ import annotations

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import AccountRow, ProfileRow, utcnow


async def create(
    session: AsyncSession, profile_id: str, name: str, description: str = ""
) -> ProfileRow:
    row = ProfileRow(id=profile_id, name=name, description=description)
    session.add(row)
    await session.flush()
    return row


async def get(session: AsyncSession, profile_id: str) -> ProfileRow | None:
    return await session.get(ProfileRow, profile_id)


async def list_all(session: AsyncSession) -> list[ProfileRow]:
    result = await session.execute(select(ProfileRow).order_by(ProfileRow.created_at))
    return list(result.scalars().all())


async def rename(session: AsyncSession, profile_id: str, name: str) -> None:
    row = await session.get(ProfileRow, profile_id)
    if row is not None:
        row.name = name


async def set_active(session: AsyncSession, profile_id: str, active: bool) -> None:
    row = await session.get(ProfileRow, profile_id)
    if row is not None:
        row.active = active


async def touch(session: AsyncSession, profile_id: str) -> None:
    row = await session.get(ProfileRow, profile_id)
    if row is not None:
        row.last_used_at = utcnow()


async def count_accounts(session: AsyncSession, profile_id: str) -> int:
    result = await session.execute(
        select(func.count(AccountRow.id)).where(AccountRow.profile_id == profile_id)
    )
    return int(result.scalar_one())


async def delete(session: AsyncSession, profile_id: str) -> bool:
    await session.execute(
        sa_delete(AccountRow).where(AccountRow.profile_id == profile_id)
    )
    result = await session.execute(
        sa_delete(ProfileRow).where(ProfileRow.id == profile_id)
    )
    return bool(result.rowcount)
