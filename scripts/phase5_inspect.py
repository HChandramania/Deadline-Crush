"""Read-only, sanitized inspection of the local disposable product fixture."""
import asyncio
import json
import os
from pathlib import Path
import re
import ssl

import asyncpg


async def run():
    metadata = json.loads(Path('tmp/phase5-local.json').read_text())
    schema = metadata['schema']
    if not re.fullmatch(r'phase1_[a-f0-9]{32}', schema):
        raise ValueError('Not a disposable schema')
    connection = await asyncpg.connect(os.environ['DATABASE_URL_UNPOOLED'], ssl=ssl.create_default_context(), timeout=15)
    try:
        async with connection.transaction(readonly=True):
            await connection.execute(f'SET LOCAL search_path TO "{schema}",pg_catalog')
            profiles = await connection.fetch('SELECT id,role FROM users ORDER BY created_at LIMIT 10')
            versions = await connection.fetch('SELECT version_no,lifecycle,validation_status FROM submission_versions ORDER BY version_no LIMIT 20')
            jobs = await connection.fetch('SELECT status,attempts FROM validation_jobs ORDER BY created_at LIMIT 20')
            organizer_audits = await connection.fetchval("SELECT count(*) FROM audit_events WHERE event_type='ORGANIZER_PROVISIONED'")
            print(json.dumps({'schema': schema, 'profiles': [dict(r) for r in profiles],
                              'versions': [dict(r) for r in versions], 'jobs': [dict(r) for r in jobs],
                              'organizer_provisioning_audits': organizer_audits}, default=str))
    finally:
        await connection.close()


if __name__ == '__main__':
    try: asyncio.run(run())
    except Exception as exc: raise SystemExit('Fixture inspection failed: '+type(exc).__name__) from None
