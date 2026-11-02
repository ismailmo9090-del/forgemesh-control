"""Audit log persistence."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forgemesh_browser.storage.models import AuditLogRow


async def add(
    session: AsyncSession,
    action: str,
    *,
    actor: str = "cli",
    details: dict | None = None,
) -> AuditLogRow:
    row = AuditLogRow(
        actor=actor, action=action, details_json=json.dumps(details or {}, default=str)
    )
    session.add(row)
    await session.flush()
    return row


async def list_recent(session: AsyncSession, limit: int = 100) -> list[AuditLogRow]:
    result = await session.execute(
        select(AuditLogRow).order_by(AuditLogRow.timestamp.desc()).limit(limit)
    )
    return list(result.scalars().all())
