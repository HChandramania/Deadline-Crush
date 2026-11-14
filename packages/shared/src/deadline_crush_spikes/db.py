"""Bounded per-process pool; Neon transaction pooling uses no session state."""
import asyncio
import ssl
from contextlib import asynccontextmanager
from .migrations import schema_name


@asynccontextmanager
async def transaction(pool, schema='deadline_crush'):
    schema = schema_name(schema)
    if pool is None:
        raise RuntimeError('Database not configured')
    async with pool.acquire(timeout=10) as connection:
        async with connection.transaction():
            # Transaction-local state is safe with Neon's transaction pooler.
            await connection.execute(f'SET LOCAL search_path TO "{schema}", pg_catalog')
            yield connection


async def create_pool(settings):
    import asyncpg
    if not settings.DATABASE_URL:
        return None
    return await asyncpg.create_pool(settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN, max_size=settings.DB_POOL_MAX,
        ssl=ssl.create_default_context(), timeout=10, command_timeout=10,
        statement_cache_size=0, max_inactive_connection_lifetime=60)


async def close_pool(pool):
    if pool is not None:
        try:
            await asyncio.wait_for(pool.close(), timeout=15)
        except TimeoutError:
            pool.terminate()


async def probe(pool):
    if pool is None:
        raise RuntimeError('Database not configured')
    async with pool.acquire(timeout=10) as conn:
        return await probe_connection(conn)


async def probe_connection(conn):
    import asyncpg
    assert await conn.fetchval('SELECT 1') == 1
    async with conn.transaction():
        await conn.execute('CREATE TEMP TABLE phase0_probe (id int PRIMARY KEY, value text) ON COMMIT DROP')
        value = "quote'; --"
        assert await conn.fetchval('INSERT INTO phase0_probe VALUES ($1,$2) RETURNING value', 1, value) == value
        await conn.fetchrow('SELECT * FROM phase0_probe WHERE id=$1 FOR UPDATE', 1)
        try:
            async with conn.transaction():
                await conn.execute('INSERT INTO phase0_probe VALUES ($1,$2)', 1, 'duplicate')
        except asyncpg.UniqueViolationError:
            pass
        else:
            raise AssertionError('unique constraint not enforced')
        try:
            async with conn.transaction():
                await conn.execute('INSERT INTO phase0_probe VALUES ($1,$2)', 2, 'rollback')
                raise ValueError('injected rollback')
        except ValueError:
            pass
        assert await conn.fetchval('SELECT count(*) FROM phase0_probe') == 1
        stamp = await conn.fetchval('SELECT clock_timestamp()')
    assert await conn.fetchval('SELECT 1') == 1
    return {'status': 'passed', 'server_time': stamp.isoformat()}
