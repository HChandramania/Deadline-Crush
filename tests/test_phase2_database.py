"""Real isolated Neon transactions with controlled storage evidence only."""
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from main import app
from identity import verified_identity
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from deadline_crush_spikes.migrations import apply
from phase1_support import foundation
from submissions import ObjectEvidence, StoredPart, fingerprint, storage_verifier
from test_submissions import PAYLOAD

pytestmark = pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'), reason='live isolated Neon schema requires database URLs')


class ControlledStorage:
    """No R2 calls. Test-only observations, independently set from request claims."""
    pending = False
    bad_parts = False
    bad_object = False
    fault = False

    async def parts(self, upload):
        return tuple(StoredPart(n, 'wrong' if self.bad_parts else f'etag-{n}',
            min(upload.part_size, upload.expected_size-(n-1)*upload.part_size))
            for n in range(1, upload.expected_parts+1))

    async def completed_object(self, upload, manifest):
        if self.fault:
            raise RuntimeError('injected crash before finalization')
        if self.pending:
            return None
        return ObjectEvidence(upload.session_id, upload.object_key,
            1 if self.bad_object else upload.expected_size,
            fingerprint([p.model_dump() for p in manifest]))


@asynccontextmanager
async def environment(offset=86400):
    async with foundation() as (schema, pool, admin):
        previous = (getattr(app.state, 'pool', None), getattr(app.state, 'settings', None))
        app.state.pool = pool
        app.state.settings = replace(Settings.load(), APP_SCHEMA=schema)
        subject = ['member']
        async def claims(): return {'iss': 'https://phase2.example', 'sub': subject[0]}
        storage = ControlledStorage()
        app.dependency_overrides[verified_identity] = claims
        app.dependency_overrides[storage_verifier] = lambda: storage
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                actor = UUID((await client.get('/api/v1/users/me')).json()['id'])
                async with db.transaction(pool, schema) as c:
                    hack = await c.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('phase2',clock_timestamp()+interval '1 day') RETURNING id")
                created = await client.post('/api/v1/teams', json={'hackathon_id': str(hack), 'name': 'phase2'}, headers={'Idempotency-Key': str(uuid4())})
                assert created.status_code == 201
                team = UUID(created.json()['id'])
                if offset != 86400:
                    # Anchor short boundary windows after onboarding network I/O.
                    async with db.transaction(pool,schema) as c:
                        await c.execute("UPDATE hackathons SET deadline_at=clock_timestamp()+$2*interval '1 second' WHERE id=$1",hack,offset)
                yield client, pool, schema, team, actor, storage, subject
        finally:
            app.dependency_overrides.clear()
            app.state.pool, app.state.settings = previous


async def admit(client, team, key=None, **changes):
    return await client.post('/api/v1/submissions/versions', params={'team_id': str(team)},
        json={**PAYLOAD, **changes}, headers={'Idempotency-Key': str(key or uuid4())})


async def activate(pool, schema, value):
    """Future storage adapter's pre-deadline activation; not a participant API."""
    async with db.transaction(pool, schema) as c:
        await c.execute("UPDATE submission_versions SET lifecycle='UPLOADING' WHERE id=$1", UUID(value['version_id']))
        await c.execute("UPDATE upload_sessions SET status='ACTIVE',r2_upload_id=$2 WHERE id=$1", UUID(value['upload_session_id']), str(uuid4()))


async def complete(client, team, value, key=None, etag='etag-1'):
    return await client.post('/api/v1/uploads/'+value['upload_session_id']+'/complete', params={'team_id': str(team)},
        json={'parts': [{'part_number': 1, 'etag': etag}]}, headers={'Idempotency-Key': str(key or uuid4())})


def test_concurrent_admissions_retries_conflicts_and_immutability():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            responses = await asyncio.gather(*(admit(client,team) for _ in range(8)))
            assert all(r.status_code == 201 for r in responses)
            assert sorted(r.json()['version_no'] for r in responses) == list(range(1,9))
            key = uuid4()
            retries = await asyncio.gather(*(admit(client,team,key) for _ in range(5)))
            assert sorted(r.status_code for r in retries) == [200,200,200,200,201]
            assert len({r.json()['version_id'] for r in retries}) == 1
            assert (await admit(client,team,key,title='conflict')).json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'
            value = responses[0].json()
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT next_version_no FROM teams WHERE id=$1',team) == 10
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='VERSION_ADMITTED'") == 9
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='ADMISSION_REJECTED'") == 1
                for sql in ["UPDATE submission_versions SET title='mutated' WHERE id=$1",
                            "UPDATE submission_versions SET filename='mutated' WHERE id=$1",
                            'DELETE FROM submission_versions WHERE id=$1']:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with c.transaction(): await c.execute(sql,UUID(value['version_id']))
                for column,value2 in [('expected_parts',2),('part_size',5242880)]:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with c.transaction():
                            await c.execute(f'UPDATE upload_sessions SET {column}=$2 WHERE id=$1', UUID(value['upload_session_id']),value2)
            history = (await client.get('/api/v1/submissions/versions',params={'team_id':str(team)})).json()
            assert [v['version_no'] for v in history['versions']] == list(range(9,0,-1))
    asyncio.run(check())


@pytest.mark.parametrize('cutoff_name', ['deadline','grace'])
@pytest.mark.parametrize('microseconds,allowed', [(-1,True),(0,True),(1,False)])
def test_exact_database_boundary_policy(cutoff_name,microseconds,allowed):
    async def check():
        async with environment() as (_,pool,schema,*rest):
            async with db.transaction(pool,schema) as c:
                cutoff = await c.fetchval("SELECT clock_timestamp()+CASE WHEN $1::text='grace' THEN interval '600 seconds' ELSE interval '0 seconds' END",cutoff_name)
                assert await c.fetchval('SELECT submission_time_eligible($1,$2)',cutoff+timedelta(microseconds=microseconds),cutoff) is allowed
    asyncio.run(check())


def test_deadline_rejection_audit_and_size_limit():
    async def check():
        async with environment(-1) as (client,pool,schema,team,*rest):
            result = await admit(client,team)
            assert result.status_code == 409 and result.json()['error']['code'] == 'DEADLINE_CLOSED'
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM late_attempts') == 1
                assert await c.fetchval('SELECT count(*) FROM submission_versions') == 0
                assert await c.fetchval('SELECT next_version_no FROM teams WHERE id=$1',team) == 1
        async with environment() as (client,pool,schema,team,*rest):
            assert (await admit(client,team,size_bytes=104857601)).status_code == 413
    asyncio.run(check())


def test_lock_wait_and_old_transaction_timestamp_cannot_admit_late():
    async def check():
        async with environment() as (client,pool,schema,team,*rest):
            async with db.transaction(pool,schema) as locked:
                await locked.fetchval('SELECT id FROM teams WHERE id=$1 FOR UPDATE',team)
                hack = await locked.fetchval('SELECT hackathon_id FROM teams WHERE id=$1',team)
                await locked.execute("UPDATE hackathons SET deadline_at=clock_timestamp()+interval '1 second' WHERE id=$1",hack)
            async with db.transaction(pool,schema) as locked:
                await locked.fetchval('SELECT id FROM teams WHERE id=$1 FOR UPDATE',team)
                task = asyncio.create_task(admit(client,team))
                await locked.execute('SELECT pg_sleep(1.3)')
            response = await task
            assert response.json()['error']['code'] == 'DEADLINE_CLOSED'
            async with db.transaction(pool,schema) as c:
                start = await c.fetchval('SELECT now()')
                await c.execute('SELECT pg_sleep(0.02)')
                row = await c.fetchrow('SELECT now() AS old,clock_timestamp() AS fresh')
                assert row['old'] == start < row['fresh']
                assert not await c.fetchval('SELECT submission_time_eligible(clock_timestamp(),$1)',start)
    asyncio.run(check())


def test_finalization_selection_jobs_and_duplicate_outcomes():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            values = [(await admit(client,team)).json() for _ in range(3)]
            for value in values: await activate(pool,schema,value)
            # Independent requests finalize out of version order.
            responses = await asyncio.gather(complete(client,team,values[1]),complete(client,team,values[0]))
            assert all(r.status_code==200 for r in responses)
            async with db.transaction(pool,schema) as c:
                await c.execute("UPDATE submission_versions SET validation_status='ACCEPTED' WHERE id=$1",UUID(values[0]['version_id']))
                await c.execute("UPDATE submission_versions SET validation_status='NEEDS_FIX' WHERE id=$1",UUID(values[1]['version_id']))
            retries = await asyncio.gather(*(complete(client,team,values[1]) for _ in range(4)))
            assert all(r.status_code==200 for r in retries)
            current = (await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()
            assert current['version_no']==2 and current['validation_status']=='NEEDS_FIX'
            assert (await complete(client,team,values[1],etag='changed')).json()['error']['code']=='IDEMPOTENCY_CONFLICT'
            async with db.transaction(pool,schema) as c:
                for table in ['validation_jobs','outbox']:
                    assert await c.fetchval(f'SELECT count(*) FROM {table}')==2
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='VERSION_FINALIZED'")==2
                assert await c.fetchval('SELECT selected_version_no FROM teams WHERE id=$1',team)==2
                with pytest.raises(asyncpg.CheckViolationError):
                    async with c.transaction():
                        await c.execute("UPDATE completion_intents SET manifest='[]'")
    asyncio.run(check())


@pytest.mark.parametrize('mode', ['pending','missing_parts','wrong_object','crash'])
def test_storage_evidence_and_crash_recovery(mode):
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            value = (await admit(client,team)).json()
            await activate(pool,schema,value)
            storage.pending = mode=='pending'
            storage.bad_parts = mode=='missing_parts'
            storage.bad_object = mode=='wrong_object'
            storage.fault = mode=='crash'
            response = await complete(client,team,value)
            assert response.status_code == {'pending':202,'missing_parts':409,'wrong_object':409,'crash':503}[mode]
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==0
                assert await c.fetchval('SELECT count(*) FROM outbox')==0
                assert await c.fetchval('SELECT count(*) FROM completion_intents')==(0 if mode=='missing_parts' else 1)
            storage.pending=storage.bad_parts=storage.bad_object=storage.fault=False
            assert (await complete(client,team,value)).status_code==200
    asyncio.run(check())


def test_database_commit_failure_rolls_back_finalization_and_retries():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            value=(await admit(client,team)).json(); await activate(pool,schema,value)
            async with db.transaction(pool,schema) as c:
                await c.execute("""CREATE FUNCTION injected_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'injected commit failure'; END $$;
                    CREATE CONSTRAINT TRIGGER injected_failure AFTER INSERT ON validation_jobs
                    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION injected_failure();""")
            assert (await complete(client,team,value)).status_code==503
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT lifecycle FROM submission_versions WHERE id=$1',UUID(value['version_id']))=='COMPLETING'
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==0
                assert await c.fetchval('SELECT count(*) FROM outbox')==0
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='VERSION_FINALIZED'")==0
                await c.execute('DROP TRIGGER injected_failure ON validation_jobs')
            assert (await complete(client,team,value)).status_code==200
            # Crash/response loss after commit: identical retry returns durable result.
            assert (await complete(client,team,value)).status_code==200
    asyncio.run(check())


def test_member_access_cross_team_organizer_and_principal_key_scope():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            key=uuid4(); value=(await admit(client,team,key)).json()
            subject[0]='outsider'
            outsider=UUID((await client.get('/api/v1/users/me')).json()['id'])
            assert (await admit(client,team,key)).status_code==404
            for path in ['/submissions/current','/submissions/versions']:
                assert (await client.get('/api/v1'+path,params={'team_id':str(team)})).status_code==404
            assert (await complete(client,team,value)).status_code==404
            async with db.transaction(pool,schema) as c:
                await c.execute("UPDATE users SET role='ORGANIZER' WHERE id=$1",outsider)
            assert (await admit(client,team)).status_code==404
            async with db.transaction(pool,schema) as c:
                await c.execute('INSERT INTO team_members(team_id,user_id) VALUES($1,$2)',team,outsider)
            response=await admit(client,team,key)
            assert response.status_code==403 and response.json()['error']['code']=='CAPTAIN_REQUIRED'
            for path in ['/submissions/current','/submissions/versions']:
                assert (await client.get('/api/v1'+path,params={'team_id':str(team)})).status_code==200
            assert (await complete(client,team,value)).status_code==403
            subject[0]='member'
            await activate(pool,schema,value)
            assert (await complete(client,team,value)).status_code==200
            subject[0]='outsider'
            async with db.transaction(pool,schema) as c:
                await c.execute('DELETE FROM team_members WHERE team_id=$1 AND user_id=$2',team,outsider)
            assert (await complete(client,team,value)).status_code==404
    asyncio.run(check())


@pytest.mark.parametrize('recoverable', [True, False])
def test_grace_cutoff_and_durable_intent_recovery(recoverable):
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            # Isolated fixture models a candidate durably admitted before D.
            # No production deadline is modified and no service clock is mocked.
            async with db.transaction(pool,schema) as c:
                hack=await c.fetchval('SELECT hackathon_id FROM teams WHERE id=$1',team)
                deadline=await c.fetchval("UPDATE hackathons SET deadline_at=clock_timestamp()+$2*interval '1 second' WHERE id=$1 RETURNING deadline_at",hack,-596 if recoverable else -601)
                grace=deadline+timedelta(seconds=600)
                version,session=uuid4(),uuid4()
                await c.execute('''INSERT INTO submission_versions(id,team_id,version_no,idempotency_key,request_hash,
                    title,object_key,expected_size,admitted_at,admitted_by,deadline_at,grace_until,lifecycle)
                    VALUES($1,$2,1,$3,$4,'fixture',$5,7340032,$6,$7,$8,$9,'UPLOADING')''',
                    version,team,uuid4(),fingerprint(PAYLOAD),str(version),deadline-timedelta(seconds=1),actor,deadline,grace)
                await c.execute('''INSERT INTO upload_sessions(id,version_id,object_key,status,r2_upload_id,part_size,expected_parts,expires_at)
                    VALUES($1,$2,$3,'ACTIVE','authorized-before-deadline',8388608,1,$4)''',session,version,str(version),grace)
                await c.execute('UPDATE teams SET next_version_no=2 WHERE id=$1',team)
            value={'version_id':str(version),'upload_session_id':str(session)}
            storage.pending=True
            response=await complete(client,team,value)
            if not recoverable:
                assert response.json()['error']['code']=='GRACE_EXPIRED'
                async with db.transaction(pool,schema) as c:
                    assert await c.fetchval('SELECT count(*) FROM late_attempts')==1
                    assert await c.fetchval('SELECT count(*) FROM completion_intents')==0
                return
            assert response.status_code==202
            async with db.transaction(pool,schema) as c:
                await c.execute('SELECT pg_sleep(GREATEST(0,EXTRACT(epoch FROM $1::timestamptz-clock_timestamp()))+0.05)',grace)
            storage.pending=False
            assert (await complete(client,team,value)).status_code==200
            assert (await complete(client,team,value,etag='changed')).status_code==409
            assert (await admit(client,team)).json()['error']['code']=='DEADLINE_CLOSED'
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==1
                assert await c.fetchval('SELECT finalized_at FROM submission_versions WHERE id=$1',version)>grace
    asyncio.run(check())


def test_durable_admission_retry_after_deadline():
    async def check():
        async with environment(6) as (client,pool,schema,team,*rest):
            key=uuid4(); original=await admit(client,team,key)
            assert original.status_code==201
            async with db.transaction(pool,schema) as c:
                await c.execute("SELECT pg_sleep(GREATEST(0,EXTRACT(epoch FROM deadline_at-clock_timestamp()))+0.02) FROM hackathons")
            retry=await admit(client,team,key)
            assert retry.status_code==200 and retry.json()==original.json()
            assert (await admit(client,team)).json()['error']['code']=='DEADLINE_CLOSED'
    asyncio.run(check())


def test_frozen_session_prevents_new_or_expanded_work_during_grace():
    async def check():
        async with environment(8) as (client,pool,schema,team,*rest):
            first=(await admit(client,team)).json(); second=(await admit(client,team)).json()
            await activate(pool,schema,first)
            async with db.transaction(pool,schema) as c:
                await c.execute("SELECT pg_sleep(GREATEST(0,EXTRACT(epoch FROM deadline_at-clock_timestamp()))+0.02) FROM hackathons")
                for sql,args in [
                    ("UPDATE upload_sessions SET r2_upload_id='new' WHERE id=$1",(UUID(second['upload_session_id']),)),
                    ("UPDATE upload_sessions SET expected_parts=2 WHERE id=$1",(UUID(first['upload_session_id']),)),
                    ("UPDATE upload_sessions SET r2_upload_id='replaced' WHERE id=$1",(UUID(first['upload_session_id']),)),
                    ("UPDATE submission_versions SET description='edit' WHERE id=$1",(UUID(first['version_id']),)),
                    ("UPDATE submission_versions SET lifecycle='FINALIZED',finalized_at=clock_timestamp() WHERE id=$1",(UUID(first['version_id']),))]:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with c.transaction(): await c.execute(sql,*args)
            extra=await client.post('/api/v1/uploads/'+first['upload_session_id']+'/complete',params={'team_id':str(team)},
                headers={'Idempotency-Key':str(uuid4())},json={'parts':[{'part_number':1,'etag':'etag-1'},{'part_number':2,'etag':'etag-2'}]})
            assert extra.json()['error']['code']=='PARTS_MISMATCH'
            assert (await complete(client,team,first)).status_code==200
            assert (await admit(client,team)).json()['error']['code']=='DEADLINE_CLOSED'
    asyncio.run(check())


def test_multiple_teams_hackathons_and_storage_default_are_isolated():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            key=uuid4(); first=(await admit(client,team,key)).json()
            async with db.transaction(pool,schema) as c:
                hack=await c.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('other',clock_timestamp()+interval '1 day') RETURNING id")
            other=(await client.post('/api/v1/teams',json={'name':'other','hackathon_id':str(hack)},headers={'Idempotency-Key':str(uuid4())})).json()['id']
            second=await admit(client,UUID(other),key)
            assert second.status_code==201 and second.json()['version_no']==1
            assert (await complete(client,UUID(other),first)).status_code==404
            await activate(pool,schema,first)
            app.dependency_overrides.pop(storage_verifier)
            response=await complete(client,team,first)
            assert response.status_code==503 and response.json()['error']['code']=='STORAGE_NOT_CONFIGURED'
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM completion_intents')==0
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='COMPLETION_REJECTED'")>=1
            assert (await client.post('/api/v1/submissions/versions',json=PAYLOAD,headers={'Idempotency-Key':str(uuid4())})).status_code==422
    asyncio.run(check())


def test_forward_migration_preserves_phase1_rows():
    async def check():
        async with foundation() as (_,pool,admin):
            schema='phase1_'+uuid4().hex
            try:
                with TemporaryDirectory() as directory:
                    for name in ['001_foundation.sql','002_integrity.sql']:
                        Path(directory,name).write_bytes(Path('migrations',name).read_bytes())
                    await apply(admin,schema,Path(directory))
                async with db.transaction(pool,schema) as c:
                    user=await c.fetchval("INSERT INTO users(auth_issuer,auth_subject) VALUES('migration','existing') RETURNING id")
                    hack=await c.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('existing',clock_timestamp()+interval '1 day') RETURNING id")
                    team=await c.fetchval("INSERT INTO teams(hackathon_id,name,captain_id,idempotency_key,request_hash) VALUES($1,'existing',$2,$3,$4) RETURNING id",hack,user,uuid4(),'0'*64)
                    await c.execute("INSERT INTO team_members(team_id,user_id,role) VALUES($1,$2,'CAPTAIN')",team,user)
                    version=await c.fetchval("""INSERT INTO submission_versions(team_id,version_no,idempotency_key,request_hash,title,object_key,expected_size,admitted_at)
                        VALUES($1,1,$2,$3,'existing','existing',5,clock_timestamp()) RETURNING id""",team,uuid4(),'0'*64)
                await apply(admin,schema); await apply(admin,schema)
                async with db.transaction(pool,schema) as c:
                    assert await c.fetchval('SELECT title FROM submission_versions WHERE id=$1',version)=='existing'
                    assert await c.fetchval('SELECT count(*) FROM team_members')==1
                    assert await c.fetchval('SELECT count(*) FROM schema_migrations')==5
            finally:
                await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    asyncio.run(check())


@pytest.mark.parametrize('skew_days', [-365,365])
def test_application_clock_skew_does_not_control_admission(monkeypatch,skew_days):
    class SkewedDateTime(datetime):
        @classmethod
        def now(cls,tz=None):
            return datetime.now(tz)+timedelta(days=skew_days)
    monkeypatch.setattr('submissions.datetime',SkewedDateTime)
    async def check():
        async with environment() as (client,pool,schema,team,*rest):
            result=await admit(client,team)
            assert result.status_code==201
            async with db.transaction(pool,schema) as c:
                recorded=await c.fetchval('SELECT admitted_at FROM submission_versions WHERE id=$1',UUID(result.json()['version_id']))
                current=await c.fetchval('SELECT clock_timestamp()')
                assert timedelta(0)<=current-recorded<timedelta(minutes=1)
    asyncio.run(check())


def test_legacy_finalized_row_without_policy_proof_is_not_acknowledged_or_selected():
    async def check():
        async with environment() as (client,pool,schema,team,*rest):
            version,session=uuid4(),uuid4()
            manifest=[{'part_number':1,'etag':'etag-1'}]
            async with db.transaction(pool,schema) as c:
                await c.execute("""INSERT INTO submission_versions(id,team_id,version_no,idempotency_key,request_hash,title,
                    object_key,expected_size,admitted_at,lifecycle,finalized_at)
                    VALUES($1,$2,99,$3,$4,'legacy',$5,7340032,clock_timestamp(),'FINALIZED',clock_timestamp())""",
                    version,team,uuid4(),'0'*64,str(version))
                await c.execute("""INSERT INTO upload_sessions(id,version_id,object_key,status,part_size,expected_parts,expires_at)
                    VALUES($1,$2,$3,'COMPLETED',8388608,1,clock_timestamp())""",session,version,str(version))
                await c.execute("""INSERT INTO completion_intents(upload_session_id,idempotency_key,manifest,manifest_sha256,admitted_at,status)
                    VALUES($1,$2,$3::jsonb,$4,clock_timestamp(),'FINALIZED')""",session,uuid4(),json.dumps(manifest),fingerprint(manifest))
            response=await complete(client,team,{'upload_session_id':str(session)})
            assert response.status_code==409 and response.json()['error']['code']=='INELIGIBLE_VERSION'
            assert (await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()['selected_version_id'] is None
    asyncio.run(check())
