from contextlib import asynccontextmanager
from dataclasses import replace
import os
import ssl
from uuid import uuid4

import asyncpg
from deadline_crush_spikes import db
from deadline_crush_spikes.migrations import apply
from deadline_crush_spikes.settings import Settings


@asynccontextmanager
async def foundation():
    schema='phase1_'+uuid4().hex
    admin=await asyncpg.connect(os.environ['DATABASE_URL_UNPOOLED'],ssl=ssl.create_default_context(),timeout=15)
    pool=None
    auth_before=await admin.fetch("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='neon_auth' ORDER BY table_name,ordinal_position")
    try:
        await apply(admin,schema)
        await apply(admin,schema)
        pool=await db.create_pool(Settings.load())
        yield schema,pool,admin
    finally:
        await db.close_pool(pool)
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        auth_after=await admin.fetch("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='neon_auth' ORDER BY table_name,ordinal_position")
        assert auth_before==auth_after, 'Managed Auth structure changed'
        await admin.close()
