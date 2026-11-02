"""Task journal persistence."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import TaskRow


async def create(
    session: AsyncSession,
    task_id: str,
    kind: str,
    params: dict,
    *,
    profile_id: str | None = None,
    provider_id: str | None = None,
) -> TaskRow:
    row = TaskRow(
        id=task_id,
        kind=kind,
        params_json=json.dumps(params, default=str),
        profile_id=profile_id,
        provider_id=provider_id,
    )
    session.add(row)
    await session.flush()
    return row


async def get(session: AsyncSession, task_id: str) -> TaskRow | None:
    return await session.get(TaskRow, task_id)


async def list_recent(session: AsyncSession, limit: int = 50) -> list[TaskRow]:
    result = await session.execute(
        select(TaskRow).order_by(TaskRow.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
