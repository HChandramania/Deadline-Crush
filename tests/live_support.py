"""Isolated Phase 0.3 test adapter; production SQL is unchanged."""
from contextlib import asynccontextmanager
import re
import ssl
from uuid import uuid4
import asyncpg
from deadline_crush_spikes import db


class Connection:
    def __init__(self, connection, pool):
        self.connection, self.pool = connection, pool

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def sql(self, sql):
        return sql.replace('phase0_jobs', f'"{self.pool.schema}".phase0_jobs')

    async def execute(self, sql, *args):
        return await self.connection.execute(self.sql(sql), *args)

    async def fetchval(self, sql, *args):
        return await self.connection.fetchval(self.sql(sql), *args)

    async def fetchrow(self, sql, *args):
        if self.pool.disconnect and 'WITH candidate AS' in sql:
            self.pool.disconnect = False
            self.connection.terminate()  # Disconnect only this test-owned client.
        return await self.connection.fetchrow(self.sql(sql), *args)


class Pool:
    def __init__(self, pool, schema, disconnect=False):
        if not re.fullmatch(r'phase03_[a-f0-9]{32}', schema):
            raise ValueError('Invalid disposable schema')
        self.pool, self.schema, self.disconnect = pool, schema, disconnect
        self.closed = False

    @asynccontextmanager
    async def acquire(self, **kwargs):
        async with self.pool.acquire(**kwargs) as connection:
            yield Connection(connection, self)

    async def close(self):
        await self.pool.close()
        self.closed = True

    def terminate(self):
        self.pool.terminate()


@asynccontextmanager
async def isolated_pool(settings):
    import os
    from pathlib import Path
    schema = 'phase03_' + uuid4().hex
    direct = os.environ.get('DATABASE_URL_UNPOOLED')
    if not direct:
        raise RuntimeError('DATABASE_URL_UNPOOLED is required for isolated tests')
    admin = await asyncpg.connect(direct, ssl=ssl.create_default_context(), timeout=15)
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(Path('infra/phase0.sql').read_text().replace('phase0_jobs', f'"{schema}".phase0_jobs').replace(
            f'INDEX IF NOT EXISTS "{schema}".phase0_jobs_available', 'INDEX IF NOT EXISTS phase0_jobs_available'))
        pool = Pool(await db.create_pool(settings), schema)
        yield pool
    finally:
        if pool is not None:
            await db.close_pool(pool)
        if admin.is_closed():
            # Long/injected outages may close the idle direct admin connection.
            # Reconnect only to drop this fixture's exact recorded UUID schema.
            admin = await asyncpg.connect(direct, ssl=ssl.create_default_context(), timeout=15)
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
