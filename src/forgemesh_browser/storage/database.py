"""Async SQLite database with lightweight schema versioning."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forgemesh_browser.storage.models import Base

SCHEMA_VERSION = 2


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path}",
            echo=False,
            future=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        self._initialized = False

    async def init(self) -> None:
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO schema_meta(key, value) VALUES('version', :v) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                ),
                {"v": str(SCHEMA_VERSION)},
            )
        self._initialized = True

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._sessionmaker

    def session(self) -> AsyncSession:
        return self._sessionmaker()

    @contextlib.asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        session = self._sessionmaker()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close(self) -> None:
        await self._engine.dispose()
