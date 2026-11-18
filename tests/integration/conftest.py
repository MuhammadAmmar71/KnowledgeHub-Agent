"""
Integration tests against a real Postgres + pgvector (docker compose up -d db).

Uses a separate database (TEST_DATABASE_URL, default knowledge_agent_test on
the compose instance) -- created and migrated on first use, truncated before
every test -- never the development database. Skipped entirely when Postgres
isn't reachable, so the unit suite still runs anywhere.
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://knowledge_agent:knowledge_agent@localhost:5432/knowledge_agent_test",
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


async def _prepare_database() -> str | None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception as exc:  # noqa: BLE001
        return f"Postgres not reachable for integration tests: {type(exc).__name__}"
    finally:
        await admin.dispose()
    return None


def _migrate() -> None:
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT, env=env, check=True, capture_output=True,
    )


@pytest.fixture(scope="session")
def migrated_database_url() -> str:
    skip_reason = asyncio.run(_prepare_database())
    if skip_reason:
        pytest.skip(skip_reason)
    _migrate()
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_database_url):
    engine = create_async_engine(migrated_database_url)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE chat_messages, conversations, chunks, documents, sync_runs, sync_state CASCADE"))
    yield engine
    await engine.dispose()
