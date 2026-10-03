"""
Async SQLAlchemy engine + session factory.

Spec section 36 chose PostgreSQL (+pgvector later, Phase 8) specifically so
document state and vectors live in one transactional database. This module
only wires up the engine/session -- schema lives in models.py, actual
tables are created via Alembic migrations, never via create_all() in
application code, so schema history stays auditable.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def build_engine(settings: Settings):
    return create_async_engine(settings.database_url, pool_pre_ping=True, future=True)


def build_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session
