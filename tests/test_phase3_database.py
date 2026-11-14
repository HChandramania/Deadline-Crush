"""Real Neon authorization/transaction tests with controlled storage doubles."""
import asyncio
import os
from types import SimpleNamespace
from uuid import UUID, uuid4
from datetime import timedelta

import pytest
from main import app
from deadline_crush_spikes import db
from submissions import storage_verifier
from storage_recovery import process_once
from test_phase2_database import environment, admit, complete, ControlledStorage
from test_submission_captain import add_member, transfer

pytestmark=pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'),reason='requires isolated Neon')


class MultipartStorage(ControlledStorage):
    def __init__(self): self.creates=0; self.crash=False; self.swept=[]
    async def create(self, upload, content_type):
        self.creates+=1
        if self.crash: raise RuntimeError('crash after external create')
        return 'actual-server-upload-id'
    async def parts(self, upload): return ()
    async def sign(self, upload, numbers, seconds): return ['https://private.example/bearer' for _ in numbers]
    async def sweep_key(self, key): self.swept.append(key)


async def mutate(client,team,value,path,body=None):
    return await client.post('/api/v1/uploads/'+value['upload_session_id']+path,
        params={'team_id':str(team)},headers={'Idempotency-Key':str(uuid4())},**({'json':body} if body is not None else {}))


def test_initialize_sign_resume_retries_and_captain_transfer():
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            adapter=MultipartStorage(); app.dependency_overrides[storage_verifier]=lambda:adapter
            other=await add_member(client,pool,schema,team,subject)
            value=(await admit(client,team)).json()
            responses=await asyncio.gather(*(mutate(client,team,value,'/initialize') for _ in range(3)))
            assert all(r.status_code in (200,202) for r in responses)
            assert adapter.creates==1
            assert (await mutate(client,team,value,'/parts/sign',{'part_numbers':[1]})).status_code==200
            assert (await mutate(client,team,value,'/parts/sign',{'part_numbers':[2]})).status_code==409
            key=str(uuid4())
            base='/api/v1/uploads/'+value['upload_session_id']+'/parts/sign'
            for numbers,expected in [([1],200),([1],200),([2],409)]:
                response=await client.post(base,params={'team_id':str(team)},headers={'Idempotency-Key':key},json={'part_numbers':numbers})
                assert response.status_code==expected
            assert response.json()['error']['code']=='IDEMPOTENCY_CONFLICT'
            state=(await mutate(client,team,value,'/resume')).json()
            assert state['missing_parts']==[1] and state['recoverable']
            subject[0]='other'
            for path,body in [('/initialize',None),('/resume',None),('/parts/sign',{'part_numbers':[1]})]:
                assert (await mutate(client,team,value,path,body)).status_code==403
            async with db.transaction(pool,schema) as c: await transfer(c,team,other)
            assert (await mutate(client,team,value,'/parts/sign',{'part_numbers':[1]})).status_code==200
            subject[0]='member'
            assert (await mutate(client,team,value,'/initialize')).status_code==403
            subject[0]='outsider'
            assert (await mutate(client,team,value,'/initialize')).status_code==404
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM storage_operations')==1
    asyncio.run(check())


def test_ambiguous_create_never_recreates():
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            adapter=MultipartStorage(); adapter.crash=True
            app.dependency_overrides[storage_verifier]=lambda:adapter
            value=(await admit(client,team)).json()
            assert (await mutate(client,team,value,'/initialize')).status_code==503
            assert (await mutate(client,team,value,'/initialize')).status_code==202
            assert adapter.creates==1
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT create_state FROM storage_operations')=='STARTED'
                assert await c.fetchval('SELECT r2_upload_id FROM upload_sessions') is None
    asyncio.run(check())


def test_internal_recovery_survives_captain_change_and_duplicates():
    async def check():
        async with environment() as (client,pool,schema,team,actor,adapter,subject):
            from test_phase2_database import activate
            value=(await admit(client,team)).json(); await activate(pool,schema,value)
            adapter.pending=True
            assert (await complete(client,team,value)).status_code==202
            other=await add_member(client,pool,schema,team,subject)
            async with db.transaction(pool,schema) as c: await transfer(c,team,other)
            assert (await complete(client,team,value)).status_code==403
            adapter.pending=False
            request=SimpleNamespace(app=app)
            await asyncio.gather(process_once(request,adapter),process_once(request,adapter))
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM completion_intents')=='FINALIZED'
                for table in ['validation_jobs','outbox']:
                    assert await c.fetchval(f'SELECT count(*) FROM {table}')==1
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='VERSION_FINALIZED'")==1
    asyncio.run(check())


def test_sign_rechecks_captain_after_parts_observation():
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            adapter=MultipartStorage(); app.dependency_overrides[storage_verifier]=lambda:adapter
            other=await add_member(client,pool,schema,team,subject)
            value=(await admit(client,team)).json()
            assert (await mutate(client,team,value,'/initialize')).status_code==200
            async def revoke(upload):
                async with db.transaction(pool,schema) as c: await transfer(c,team,other)
                return ()
            adapter.parts=revoke
            assert (await mutate(client,team,value,'/parts/sign',{'part_numbers':[1]})).status_code==403
    asyncio.run(check())


async def frozen_fixture(pool,schema,team,actor,deadline_offset,active=True):
    """Historical admission fixture, never a bypass in application endpoints."""
    from submissions import fingerprint
    async with db.transaction(pool,schema) as c:
        hack=await c.fetchval('SELECT hackathon_id FROM teams WHERE id=$1',team)
        deadline=await c.fetchval("UPDATE hackathons SET deadline_at=clock_timestamp()+$2*interval '1 second' WHERE id=$1 RETURNING deadline_at",hack,deadline_offset)
        version,session=uuid4(),uuid4(); grace=deadline+timedelta(seconds=600)
        key=f'submissions/{hack}/{team}/{version}/source'
        await c.execute('''INSERT INTO submission_versions(id,team_id,version_no,idempotency_key,request_hash,
            title,object_key,expected_size,admitted_at,admitted_by,deadline_at,grace_until,lifecycle)
            VALUES($1,$2,1,$3,$4,'historical',$5,7340032,$6,$7,$8,$9,$10)''',
            version,team,uuid4(),fingerprint({'fixture':True}),key,deadline-timedelta(seconds=1),actor,deadline,grace,
            'UPLOADING' if active else 'CREATED')
        await c.execute('''INSERT INTO upload_sessions(id,version_id,object_key,status,r2_upload_id,part_size,expected_parts,expires_at)
            VALUES($1,$2,$3,$4,$5,8388608,1,$6)''',session,version,key,'ACTIVE' if active else 'INITIATING',
            'historical-upload' if active else None,grace)
        await c.execute('UPDATE teams SET next_version_no=2 WHERE id=$1',team)
    return {'version_id':str(version),'upload_session_id':str(session)}


@pytest.mark.parametrize('offset',[-1,-601])
def test_grace_authorized_signing_and_no_late_initialization(offset):
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            adapter=MultipartStorage(); app.dependency_overrides[storage_verifier]=lambda:adapter
            value=await frozen_fixture(pool,schema,team,actor,offset)
            response=await mutate(client,team,value,'/parts/sign',{'part_numbers':[1]})
            assert response.status_code==(200 if offset==-1 else 409)
            assert (await mutate(client,team,value,'/parts/sign',{'part_numbers':[2]})).status_code==409
            assert (await admit(client,team)).status_code==409
            assert adapter.creates==0
        async with environment() as (client,pool,schema,team,actor,_,subject):
            app.dependency_overrides[storage_verifier]=lambda:adapter
            value=await frozen_fixture(pool,schema,team,actor,offset,active=False)
            response=await mutate(client,team,value,'/initialize')
            assert response.json()['error']['code']=='DEADLINE_CLOSED'
            assert adapter.creates==0
    asyncio.run(check())


def test_expired_orphan_cleanup_is_exact_and_repeated():
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            adapter=MultipartStorage(); value=await frozen_fixture(pool,schema,team,actor,-5000,active=False)
            async with db.transaction(pool,schema) as c:
                await c.execute('INSERT INTO storage_operations(upload_session_id) VALUES($1)',UUID(value['upload_session_id']))
                key=await c.fetchval('SELECT object_key FROM upload_sessions')
            assert await process_once(SimpleNamespace(app=app),adapter)
            assert adapter.swept==[key]
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM upload_sessions')=='EXPIRED'
                assert await c.fetchval('SELECT create_state FROM storage_operations')=='ABANDONED'
                await c.execute('UPDATE storage_operations SET available_at=clock_timestamp()')
            await process_once(SimpleNamespace(app=app),adapter)
            assert adapter.swept==[key,key]  # catches late results of in-flight creates
    asyncio.run(check())


def test_post_grace_recovery_backoff_and_fencing_never_sweeps_intent():
    async def check():
        from submissions import finalize, SubmissionError, canonical, fingerprint
        async with environment() as (client,pool,schema,team,actor,adapter,subject):
            value=await frozen_fixture(pool,schema,team,actor,-5000)
            parts=[{'part_number':1,'etag':'etag-1'}]
            async with db.transaction(pool,schema) as c:
                # Committed historical intent that qualified before grace cutoff.
                grace=await c.fetchval('SELECT grace_until FROM submission_versions')
                await c.execute('''INSERT INTO completion_intents(upload_session_id,idempotency_key,manifest,manifest_sha256,admitted_at,status,admitted_by)
                    VALUES($1,$2,$3::jsonb,$4,$5,'ADMITTED',$6)''',UUID(value['upload_session_id']),uuid4(),canonical(parts),fingerprint(parts),grace-timedelta(seconds=1),actor)
                await c.execute("UPDATE upload_sessions SET status='COMPLETING'")
                await c.execute("UPDATE submission_versions SET lifecycle='COMPLETING'")
            request=SimpleNamespace(app=app); adapter.fault=True
            await process_once(request,adapter)
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT attempts FROM storage_operations')==1
                assert await c.fetchval('SELECT available_at>clock_timestamp() FROM storage_operations')
                assert await c.fetchval('SELECT status FROM completion_intents')=='ADMITTED'
                await c.execute('UPDATE storage_operations SET available_at=clock_timestamp()')
            adapter.fault=False
            await process_once(request,adapter)
            with pytest.raises(SubmissionError):
                await finalize(request,None,team,UUID(value['upload_session_id']),adapter,recovery=1)
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT status FROM completion_intents')=='FINALIZED'
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==1
    asyncio.run(check())
