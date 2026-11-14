"""Dedicated bounded completion/cleanup loop; no validation processing here."""
import argparse
import asyncio
import signal
from dataclasses import replace
from types import SimpleNamespace

from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from domain import transaction
from submissions import finalize, upload_row
from r2_uploads import R2Storage
from storage import create_client


async def process_once(request, verifier):
    async with transaction(request) as c:
        # Backfill operation scheduling for already-active Phase 2 sessions too.
        await c.execute('''INSERT INTO storage_operations(upload_session_id,create_state)
            SELECT s.id,'BOUND' FROM upload_sessions s JOIN completion_intents i ON i.upload_session_id=s.id
            WHERE i.status IN ('ADMITTED','RECOVERY_REQUIRED','STORAGE_COMPLETED')
             AND NOT EXISTS(SELECT 1 FROM storage_operations o WHERE o.upload_session_id=s.id)
            LIMIT 100
            ON CONFLICT DO NOTHING''')
        claim = await c.fetchrow('''WITH candidate AS (
            SELECT o.upload_session_id FROM storage_operations o
            JOIN upload_sessions s ON s.id=o.upload_session_id
            LEFT JOIN completion_intents i ON i.upload_session_id=s.id
            WHERE o.available_at<=clock_timestamp()
             AND (o.recovery_until IS NULL OR o.recovery_until<clock_timestamp())
             AND (i.status IN ('ADMITTED','RECOVERY_REQUIRED','STORAGE_COMPLETED')
                  OR (i.id IS NULL AND s.expires_at<clock_timestamp()-interval '1 hour'))
            ORDER BY o.available_at FOR UPDATE OF o SKIP LOCKED LIMIT 1)
            UPDATE storage_operations o SET recovery_token=recovery_token+1,
              recovery_until=clock_timestamp()+interval '180 seconds',attempts=attempts+1
            FROM candidate WHERE o.upload_session_id=candidate.upload_session_id RETURNING o.*''')
        if not claim:
            return False
        session, token = claim['upload_session_id'], claim['recovery_token']
        team = await c.fetchval('''SELECT v.team_id FROM upload_sessions s
            JOIN submission_versions v ON v.id=s.version_id WHERE s.id=$1''', session)
    error = None
    try:
        async with transaction(request) as c:
            pending = await c.fetchval('SELECT 1 FROM completion_intents WHERE upload_session_id=$1', session)
        if pending:
            result = await finalize(request, None, team, session, verifier, recovery=token)
            if result is None:
                error = 'RECOVERY_PENDING'
        else:
            # Commit a terminal state BEFORE abort I/O. No future activation/sign
            # or completion admission can race the sweep. Never delete objects.
            async with transaction(request) as c:
                await c.fetchrow('SELECT id FROM teams WHERE id=$1 FOR UPDATE', team)
                row = await upload_row(c, team, session)
                safe = await c.fetchval('''SELECT 1 FROM storage_operations o
                    WHERE o.upload_session_id=$1 AND o.recovery_token=$2 AND o.recovery_until>clock_timestamp()
                     AND NOT EXISTS(SELECT 1 FROM completion_intents i WHERE i.upload_session_id=$1)
                     AND $3::timestamptz<clock_timestamp()-interval '1 hour' FOR UPDATE''',session,token,row['expires_at'])
                if not safe or row['status'] not in ('INITIATING','ACTIVE','ABORTED','EXPIRED'):
                    return True
                if row['status'] in ('INITIATING','ACTIVE'):
                    await c.execute("UPDATE upload_sessions SET status='EXPIRED' WHERE id=$1",session)
                    await c.execute("UPDATE submission_versions SET lifecycle='EXPIRED' WHERE id=$1",row['version_id'])
                await c.execute("UPDATE storage_operations SET create_state='ABANDONED' WHERE upload_session_id=$1 AND create_state='STARTED'",session)
                key = row['object_key']
            await verifier.sweep_key(key)
    except Exception:
        # Persist only a constant safe category. Includes transient Neon/R2 errors.
        error = 'RECOVERY_DEPENDENCY_FAILED'
    async with transaction(request) as c:
        await c.execute('''UPDATE storage_operations SET recovery_until=NULL,last_error=$3,
            available_at=clock_timestamp()+make_interval(secs=>CASE WHEN $3::text IS NULL THEN 3600
              ELSE least(3600, power(2,least(attempts,10))::integer) END)
            WHERE upload_session_id=$1 AND recovery_token=$2''',session,token,error)
    return True


async def run(once=False):
    settings=Settings.load()
    pool=await db.create_pool(replace(settings,DB_POOL_MIN=settings.WORKER_DB_POOL_MIN,DB_POOL_MAX=settings.WORKER_DB_POOL_MAX))
    client=create_client(settings)
    if pool is None or client is None:
        await db.close_pool(pool)
        raise RuntimeError('Recovery configuration missing')
    verifier=R2Storage(client,settings.R2_BUCKET,concurrency=2)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool,settings=settings)))
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        while not stop.is_set():
            try:
                worked=await process_once(request,verifier)
            except Exception:
                print('Recovery dependency unavailable',flush=True)
                worked=False
            if once:
                return
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(),timeout=2)
                except TimeoutError:
                    pass
    finally:
        await verifier.close()
        client.close()
        await db.close_pool(pool)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    try:
        if args.check:
            Settings.load()
            print('Storage recovery imports/configuration: PASS')
        else:
            asyncio.run(run(args.once))
    except Exception:
        raise SystemExit('Storage recovery unavailable; check private configuration') from None
