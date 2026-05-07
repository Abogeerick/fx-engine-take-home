"""Database engine, session factory, and a session-scope helper.

The composition root constructs an engine and a session factory and
hands them to dependents; repositories take an ``AsyncSession``
parameter and do not own session or transaction lifecycle. The
execute path in step 3 will manage its own transaction explicitly
(SELECT ... FOR UPDATE inside a single transaction), so the helper
here is for the simpler read paths only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create an async engine for either Postgres or SQLite.

    The URL must use an async driver (``postgresql+asyncpg`` or
    ``sqlite+aiosqlite``); other drivers fail at connect time.
    """
    return create_async_engine(database_url, echo=echo, future=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session, commit on clean exit, rollback on exception."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
