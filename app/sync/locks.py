"""
Job locks: at most one sync per source, and one embedding run, at a time.

Spec section 58: the scheduler must not launch a second synchronization
while another is running, and must use a database lock if more than one
application instance can exist. Postgres session-level advisory locks do
exactly that: they're held by one connection, are visible to every
instance sharing the database, and are released by Postgres automatically
if the holding process dies -- so a crash can never leave a stale lock
behind (unlike a "running" flag in a table).

Same two-implementation shape as the repositories in state.py: the
Postgres version for production, an in-process one for tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("knowledge_agent.sync.locks")

LIBRARY_SYNC_LOCK = "sync:document_library"
PAGE_SYNC_LOCK = "sync:site_pages"
EMBEDDING_LOCK = "embeddings"


def advisory_lock_key(name: str) -> int:
    """Stable signed 64-bit key for a lock name (advisory locks take a bigint)."""
    return int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)


class LockManager(Protocol):
    def hold(self, name: str, wait: bool = False):
        """
        Async context manager yielding True if the lock was acquired (and is
        held until the block exits), False if another holder has it. With
        wait=True it blocks until acquired and always yields True.
        """
        ...

    async def is_held(self, name: str) -> bool:
        """Whether anyone (any instance) currently holds the lock. Advisory only -- racy by nature."""
        ...


class PostgresAdvisoryLockManager:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    @asynccontextmanager
    async def hold(self, name: str, wait: bool = False) -> AsyncIterator[bool]:
        key = advisory_lock_key(name)
        # A dedicated connection: a session-level advisory lock belongs to the
        # connection that took it, so it must stay checked out while held.
        async with self._engine.connect() as conn:
            if wait:
                await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                acquired = True
            else:
                acquired = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
            # End the implicit transaction so the connection doesn't sit
            # "idle in transaction" for the whole job; the lock outlives it.
            await conn.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    try:
                        await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                        await conn.commit()
                    except Exception:
                        # Closing the connection below still frees the lock
                        # once the pool discards it; log rather than mask the
                        # job's own outcome.
                        logger.exception("advisory_unlock_failed lock=%s", name)
                        await conn.invalidate()

    async def is_held(self, name: str) -> bool:
        async with self.hold(name) as acquired:
            return not acquired


class InMemoryLockManager:
    """Test double -- same semantics within one process."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, name: str) -> asyncio.Lock:
        return self._locks.setdefault(name, asyncio.Lock())

    @asynccontextmanager
    async def hold(self, name: str, wait: bool = False) -> AsyncIterator[bool]:
        lock = self._lock(name)
        if not wait and lock.locked():
            yield False
            return
        async with lock:
            yield True

    async def is_held(self, name: str) -> bool:
        return self._lock(name).locked()
