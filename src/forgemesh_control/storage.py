"""Async SQLite storage for the control center."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forgemesh_control.models import Base

SCHEMA_VERSION = 2

#: SQLite has no alembic here: `create_all` only adds missing *tables*, so a
#: column introduced after a database already exists has to be added by hand.
#: Every entry is idempotent - existing databases get the ALTER, fresh ones are
#: created with the column already present.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "council_participants": (
        ("account_id", "TEXT NOT NULL DEFAULT ''"),
        ("account_label", "TEXT NOT NULL DEFAULT ''"),
        ("council_role", "TEXT NOT NULL DEFAULT ''"),
    ),
    "council_messages": (
        ("account_id", "TEXT NOT NULL DEFAULT ''"),
        ("profile_id", "TEXT NOT NULL DEFAULT ''"),
    ),
    "proposals": (
        ("account_id", "TEXT NOT NULL DEFAULT ''"),
        ("account_label", "TEXT NOT NULL DEFAULT ''"),
    ),
    "agent_accounts": (
        ("status", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("config_dir", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class ControlDatabase:
    """One SQLite file, WAL mode, foreign keys enforced, typed sessions."""

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
        # PRAGMA foreign_keys is per SQLite *connection*, not per database. The
        # init() call only touches one pooled connection, so every new
        # connection needs the pragma applied here or ON DELETE CASCADE and
        # foreign-key checks silently do nothing on other connections.
        event.listen(self._engine.sync_engine, "connect", _enable_sqlite_foreign_keys)

    async def init(self) -> None:
        if self._initialized:
            return
        async with self._engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.run_sync(Base.metadata.create_all)
            await self._apply_added_columns(conn)
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

    async def _apply_added_columns(self, conn: Any) -> None:
        """Add columns introduced after a database already exists."""
        for table, columns in _ADDED_COLUMNS.items():
            present = {
                str(row[1])
                for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
            }
            if not present:
                continue
            for name, ddl in columns:
                if name not in present:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

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

    async def schema_version(self) -> str:
        async with self.session_scope() as session:
            row = await session.execute(
                text("SELECT value FROM schema_meta WHERE key='version'")
            )
            found = row.scalar_one_or_none()
            return str(found) if found is not None else "unknown"

    async def close(self) -> None:
        await self._engine.dispose()
