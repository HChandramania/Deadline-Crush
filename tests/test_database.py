"""Real PostgreSQL tests; run only against a disposable Phase 0 database."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4
import pytest
from deadline_crush_spikes import db, jobs
from deadline_crush_spikes.settings import Settings
from live_support import isolated_pool

pytestmark = pytest.mark.skipif(not os.environ.get('DATABASE_URL'), reason='DATABASE_URL unavailable')


def test_postgres_transactions_and_pool():
    async def check():
        async with isolated_pool(Settings.load()) as pool:
            assert (await db.probe(pool))['status'] == 'passed'
            assert (await db.probe(pool))['status'] == 'passed'
    asyncio.run(check())


def test_postgres_claim_retry_recovery_fencing():
    async def check():
        event, owner, other = uuid4(), uuid4(), uuid4()
        async with isolated_pool(Settings.load()) as pool:
            await jobs.enqueue(pool, event)
            await jobs.enqueue(pool, event)
            async with pool.acquire() as locked:
                async with locked.transaction():
                    await locked.fetchrow('SELECT id FROM phase0_jobs WHERE id=$1 FOR UPDATE', event)
                    assert await jobs.claim(pool, other) is None
            first = await jobs.claim(pool, owner)
            assert first['id'] == event
            assert await jobs.claim(pool, other) is None
            assert not await jobs.finish(pool, first, other)
            assert await jobs.finish(pool, first, owner, retry=True)
            second = await jobs.claim(pool, other)
            assert second['lease_token'] > first['lease_token']
            assert not await jobs.finish(pool, first, owner)
            async with pool.acquire() as conn:
                await conn.execute("UPDATE phase0_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1", event)
            third = await jobs.claim(pool, owner)
            assert third['attempts'] == 3
            assert not await jobs.finish(pool, second, other)
            assert await jobs.finish(pool, third, owner)
            assert await jobs.claim(pool, other) is None
    asyncio.run(check())
