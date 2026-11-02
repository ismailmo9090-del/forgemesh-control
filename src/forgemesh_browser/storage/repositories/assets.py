"""Asset manifest persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import AssetRow


async def insert(session: AsyncSession, asset: AssetRow) -> AssetRow:
    session.add(asset)
    await session.flush()
    return asset


async def get(session: AsyncSession, asset_id: str) -> AssetRow | None:
    return await session.get(AssetRow, asset_id)


async def list_all(
    session: AsyncSession,
    *,
    profile_id: str | None = None,
    kind: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
) -> list[AssetRow]:
    query = select(AssetRow).order_by(AssetRow.created_at.desc()).limit(limit)
    if profile_id:
        query = query.where(AssetRow.profile_id == profile_id)
    if kind:
        query = query.where(AssetRow.kind == kind)
    if project_id:
        query = query.where(AssetRow.project_id == project_id)
    result = await session.execute(query)
    return list(result.scalars().all())
