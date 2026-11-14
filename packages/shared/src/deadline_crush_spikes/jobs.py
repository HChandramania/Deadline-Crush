"""Disposable PostgreSQL transport spike. No submission/validation workflow."""
from uuid import UUID

CLAIM = '''WITH candidate AS (
 SELECT id FROM phase0_jobs
 WHERE (status='PENDING' AND available_at <= clock_timestamp())
 OR (status='RUNNING' AND lease_until < clock_timestamp())
 ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE phase0_jobs j SET status='RUNNING', attempts=attempts+1,
 lease_owner=$1, lease_token=lease_token+1,
 lease_until=clock_timestamp()+interval '30 seconds'
FROM candidate WHERE j.id=candidate.id RETURNING j.*'''


async def enqueue(pool, event_id: UUID):
    async with pool.acquire(timeout=10) as connection:
        await connection.execute('INSERT INTO phase0_jobs(id) VALUES ($1) ON CONFLICT DO NOTHING', event_id)


async def claim(pool, owner: UUID):
    async with pool.acquire(timeout=10) as connection:
        async with connection.transaction():
            return await connection.fetchrow(CLAIM, owner)


async def finish(pool, job, owner: UUID, retry=False):
    async with pool.acquire(timeout=10) as connection:
        result = await connection.execute('''UPDATE phase0_jobs
            SET status=$4, lease_until=NULL, available_at=clock_timestamp(), lease_owner=NULL
            WHERE id=$1 AND lease_owner=$2 AND lease_token=$3 AND status='RUNNING'
              AND lease_until > clock_timestamp()''', job['id'], owner, job['lease_token'],
            'PENDING' if retry else 'SUCCEEDED')
        return result == 'UPDATE 1'


async def process_once(pool, owner: UUID):
    job = await claim(pool, owner)
    if job is None:
        return False
    await finish(pool, job, owner, retry=job['attempts'] == 1)
    return True
