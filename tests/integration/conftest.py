"""Integration test fixtures -- requires Postgres on localhost:5433.

The whole module is skipped at collection time if Postgres is
unreachable, so a developer running ``make test-unit`` without a
running compose stack still gets clean output.

Schema is applied once per pytest session via Alembic against a
dedicated ``fx_engine_test`` database (created on demand from the
default ``postgres`` DB if absent). Per-test cleanup truncates all
relevant tables -- TRUNCATE is the right tool here because we need
the prior transaction's effects to be visible to the next test.

Note on platform-specific test-runner behaviour
================================================

Both the integration and unit make targets pass
``-p no:unraisableexception`` to pytest. The reason is
Windows-specific.

On Windows, the asyncio proactor event loop's socket cleanup, the
asyncpg / aiosqlite connection pool's ``__del__`` paths, and the
underlying TCP / sqlite-file socket teardowns all emit
``ResourceWarning`` from garbage-collected finalizers rather than
from explicit close hooks. The project's strict
``filterwarnings = ["error"]`` config (see pyproject.toml) would
otherwise escalate those warnings into test failures via pytest's
unraisable-exception hook -- failures attributed to whichever test
is currently executing when the GC happens to fire, which is
non-deterministic.

On Linux and macOS the same cleanup paths are synchronous and the
warnings are not emitted, so the override is a no-op there. CI
environments running Ubuntu (the canonical case) still catch the
same class of bug through the synchronous cleanup paths.

A note on scope: the override was initially scoped to integration
only on the reasoning that the strict-warnings regime had caught a
real leak in step 2 (an unclosed ``sqlite3.connect`` in a unit
test). Reviewing during step 7, the leak was caught by the same
``unraisableexception`` hook we are now disabling -- it was a
``__del__``-emitted warning, not a warning raised inside test
code. Holding the unit tier to a stricter standard than integration
on this specific class of warning was therefore inconsistent. The
scope was widened to both tiers. ``filterwarnings = ["error"]``
remains in effect for warnings raised inside test code, where it
catches everything except GC-finalizer noise.
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
