"""Integration test fixtures -- requires Postgres on localhost:5433.

The whole module is skipped at collection time if Postgres is
unreachable, so a developer running ``make test-unit`` without a
running compose stack still gets clean output.

Schema is applied once per pytest session via Alembic against a
dedicated ``fx_engine_test`` database (created on demand from the
default ``postgres`` DB if absent). Per-test cleanup truncates the
three tables -- TRUNCATE is the right tool here because we need the
prior transaction's effects to be visible to the next test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ADMIN_URL = "postgresql+asyncpg://fx:devpass@localhost:5433/postgres"
POSTGRES_TEST_URL = "postgresql+asyncpg://fx:devpass@localhost:5433/fx_engine_test"


def _alembic_config(async_url: str) -> AlembicConfig:
    config = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", async_url)
    return config


def _is_postgres_reachable() -> bool:
    """Sync probe used to skip the module if compose isn't up."""

    async def _check() -> bool:
        try:
            engine = create_async_engine(_ADMIN_URL, connect_args={"timeout": 2})
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()
            return True
        except (OperationalError, OSError, ConnectionRefusedError):
            return False
        except Exception:
            return False

    try:
        return asyncio.run(_check())
    except Exception:
        return False


# Skip the entire module if Postgres is unreachable.
pytestmark = pytest.mark.skipif(
    not _is_postgres_reachable(),
    reason="Postgres on localhost:5433 not reachable; start docker-compose to run these tests",
)


async def _ensure_test_database() -> None:
    """Create fx_engine_test if it doesn't already exist."""
    engine = create_async_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'fx_engine_test'")
            )
            if result.scalar() is None:
                await conn.execute(text("CREATE DATABASE fx_engine_test"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    asyncio.run(_ensure_test_database())
    command.upgrade(_alembic_config(POSTGRES_TEST_URL), "head")
    yield POSTGRES_TEST_URL


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(postgres_url)
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            # CASCADE chains through ledger_entries -> executions -> quotes
            # -> customers/balances. Listed leaf-first for readability.
            await conn.execute(
                text(
                    "TRUNCATE ledger_entries, executions, quotes, "
                    "balances, customers, rates RESTART IDENTITY CASCADE"
                )
            )
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s
