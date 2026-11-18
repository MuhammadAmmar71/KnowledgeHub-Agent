import asyncio

import pytest

from app.sync.locks import LIBRARY_SYNC_LOCK, PAGE_SYNC_LOCK, InMemoryLockManager, advisory_lock_key


def test_advisory_lock_key_is_stable_signed_64_bit_and_distinct():
    key = advisory_lock_key(LIBRARY_SYNC_LOCK)
    assert key == advisory_lock_key(LIBRARY_SYNC_LOCK)
    assert -(2 ** 63) <= key < 2 ** 63
    assert key != advisory_lock_key(PAGE_SYNC_LOCK)


@pytest.mark.asyncio
async def test_second_try_acquire_fails_while_held_and_succeeds_after():
    locks = InMemoryLockManager()
    async with locks.hold("x") as first:
        assert first is True
        assert await locks.is_held("x")
        async with locks.hold("x") as second:
            assert second is False
        async with locks.hold("other") as unrelated:
            assert unrelated is True
    assert not await locks.is_held("x")
    async with locks.hold("x") as again:
        assert again is True


@pytest.mark.asyncio
async def test_wait_blocks_until_released():
    locks = InMemoryLockManager()
    order: list[str] = []

    async def waiter():
        async with locks.hold("x", wait=True) as acquired:
            order.append(f"waiter:{acquired}")

    async with locks.hold("x"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        order.append("holder-done")
    await task
    assert order == ["holder-done", "waiter:True"]
