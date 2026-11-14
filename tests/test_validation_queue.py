"""Real Neon independent connections; storage/identity controlled in these tests."""
import asyncio
import json
import os
import sys
from uuid import UUID,uuid4
import pytest
from deadline_crush_spikes import db
from validation_jobs import Queue
from validation_worker import process_once
from validation_pipeline import ProcessingFailure
from test_phase2_database import environment,admit,activate,complete

pytestmark=pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'),reason='isolated Neon configuration required')


async def finalized(client,pool,schema,team):
    value=(await admit(client,team,links=[])).json()
    await activate(pool,schema,value)
    assert (await complete(client,team,value)).status_code==200
    return value


def result(status='ACCEPTED'):
    return {'status':status,'findings':[] if status=='ACCEPTED' else ['ZIP_CORRUPT']}


def test_dispatch_concurrent_claim_heartbeat_atomic_result():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            await finalized(client,pool,schema,team)
            q=Queue(pool,schema)
            assert await q.claim(uuid4()) is None
            assert sum(await asyncio.gather(q.dispatch(),q.dispatch()))==1
            claims=await asyncio.gather(*(q.claim(uuid4()) for _ in range(5)))
            job=next(j for j in claims if j)
            assert sum(j is not None for j in claims)==1
            assert await q.heartbeat(job)
            assert await q.complete(job,result('NEEDS_FIX'))
            assert not await q.complete(job,result())
            assert not await q.heartbeat(job)
            current=(await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()
            assert current['validation_status']=='NEEDS_FIX' and current['validation_errors']==['ZIP_CORRUPT']
            metrics=await q.metrics()
            assert metrics['needs_fix']==1 and metrics['stale_rejections']==1
    asyncio.run(check())


def test_skip_locked_and_fencing_after_expiry():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            values=[await finalized(client,pool,schema,team) for _ in range(2)]
            q=Queue(pool,schema); await q.dispatch()
            async with db.transaction(pool,schema) as c:
                await c.fetchrow('SELECT id FROM validation_jobs WHERE version_id=$1 FOR UPDATE',UUID(values[0]['version_id']))
                job=await q.claim(uuid4())
                assert job['version_id']==UUID(values[1]['version_id'])
            async with db.transaction(pool,schema) as c:
                await c.execute("UPDATE validation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=$1",job['id'])
            newer=None
            for _ in range(2):
                candidate=await q.claim(uuid4())
                if candidate['id']==job['id']: newer=candidate
            assert newer and newer['lease_token']==job['lease_token']+1
            assert await q.complete(newer,result())
            assert not await q.complete(job,result('NEEDS_FIX'))
            assert not await q.retry(job,'STORAGE_UNAVAILABLE')
            assert (await q.metrics())['recovered']==1
            current=(await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()
            assert current['selected_version_id']==values[1]['version_id'] and current['validation_status']=='ACCEPTED'
    asyncio.run(check())


def test_retry_exhaustion_retains_checking():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            await finalized(client,pool,schema,team)
            q=Queue(pool,schema,max_attempts=2); await q.dispatch()
            for attempt in (1,2):
                job=await q.claim(uuid4()); assert job['attempts']==attempt
                assert await q.retry(job,'STORAGE_UNAVAILABLE')
                async with db.transaction(pool,schema) as c:
                    row=await c.fetchrow('SELECT * FROM validation_jobs')
                    assert row['available_at']>row['created_at']
                    await c.execute('UPDATE validation_jobs SET available_at=clock_timestamp()')
            assert await q.claim(uuid4()) is None
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM validation_jobs')=='FAILED'
                assert await c.fetchval('SELECT validation_status FROM submission_versions')=='CHECKING'
                assert await c.fetchval('SELECT count(*) FROM validation_artifacts')==0
    asyncio.run(check())


def test_atomic_artifact_failure_rolls_back_terminal_write():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            await finalized(client,pool,schema,team)
            q=Queue(pool,schema); await q.dispatch(); job=await q.claim(uuid4())
            key=f"previews/{job['version_id']}/{job['id']}/{job['lease_token']}/preview.png"
            assert await q.stage(job,'PDF_PREVIEW',key)
            import asyncpg
            with pytest.raises(asyncpg.UniqueViolationError):
                await q.complete(job,result(),[('PDF_PREVIEW',key),('PDF_PREVIEW',key)])
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM validation_jobs')=='RUNNING'
                assert await c.fetchval('SELECT validation_status FROM submission_versions')=='CHECKING'
            assert await q.complete(job,result(),[('PDF_PREVIEW',key)])
            assert await q.abandoned_previews()==[]
    asyncio.run(check())


def test_worker_coroutine_interruption_durable_recovery():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            await finalized(client,pool,schema,team)
            q=Queue(pool,schema); await q.dispatch()
            entered=asyncio.Event()
            class Pipeline:
                async def process(self,source,job):
                    entered.set(); await asyncio.Event().wait()
            task=asyncio.create_task(process_once(q,Pipeline(),uuid4()))
            await asyncio.wait_for(entered.wait(),10); task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM validation_jobs')=='RUNNING'
                await c.execute("UPDATE validation_jobs SET lease_until=clock_timestamp()-interval '1 second'")
            recovered=await q.claim(uuid4())
            assert recovered['lease_token']==2
            assert await q.complete(recovered,result())
    asyncio.run(check())


def test_real_multiprocess_stale_worker_cannot_overwrite():
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            await finalized(client,pool,schema,team)
            q=Queue(pool,schema); await q.dispatch()
            code='''import asyncio,json,sys
from uuid import uuid4
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from validation_jobs import Queue
async def main():
 p=await db.create_pool(Settings.load()); q=Queue(p,sys.argv[1]); j=await q.claim(uuid4())
 print('CLAIMED',flush=True)
 await asyncio.to_thread(sys.stdin.readline)
 print(json.dumps({'committed':await q.complete(j,{'status':'NEEDS_FIX','findings':['stale']})}),flush=True)
 await db.close_pool(p)
asyncio.run(main())'''
            child=await asyncio.create_subprocess_exec(sys.executable,'-c',code,schema,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:
                assert (await asyncio.wait_for(child.stdout.readline(),15)).strip()==b'CLAIMED'
                async with db.transaction(pool,schema) as c:
                    await c.execute("UPDATE validation_jobs SET lease_until=clock_timestamp()-interval '1 second'")
                newer=await q.claim(uuid4()); assert newer['lease_token']==2
                assert await q.complete(newer,result())
                child.stdin.write(b'continue\n'); await child.stdin.drain()
                assert json.loads(await asyncio.wait_for(child.stdout.readline(),15))=={'committed':False}
                assert await child.wait()==0
                async with db.transaction(pool,schema) as c:
                    assert await c.fetchval('SELECT validation_status FROM submission_versions')=='ACCEPTED'
            finally:
                if child.returncode is None:
                    child.kill(); await child.wait()
            # Actual OS process termination after a durable claim. Distinct
            # from coroutine cancellation and from the stalled stale writer.
            await finalized(client,pool,schema,team); await q.dispatch()
            killed=await asyncio.create_subprocess_exec(sys.executable,'-c',code,schema,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            try:
                assert (await asyncio.wait_for(killed.stdout.readline(),15)).strip()==b'CLAIMED'
                killed.kill(); await killed.wait()
                async with db.transaction(pool,schema) as c:
                    await c.execute("UPDATE validation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE status='RUNNING'")
                recovered=await q.claim(uuid4())
                assert recovered['lease_token']==2 and await q.complete(recovered,result())
                assert await q.claim(uuid4()) is None
            finally:
                if killed.returncode is None: killed.kill(); await killed.wait()
    asyncio.run(check())
