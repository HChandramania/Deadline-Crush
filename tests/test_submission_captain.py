"""Captain authorization uses real, locked PostgreSQL role state."""
import asyncio
import os
from uuid import UUID, uuid4

from fastapi import Request
import pytest

from deadline_crush_spikes import db
from identity import verified_identity
from main import app
from test_phase2_database import environment, admit, activate, complete
from test_submissions import PAYLOAD

pytestmark = pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'), reason='live isolated Neon schema requires database URLs')


async def add_member(client,pool,schema,team,subject):
    subject[0]='other'
    user=UUID((await client.get('/api/v1/users/me')).json()['id'])
    async with db.transaction(pool,schema) as c:
        await c.execute('INSERT INTO team_members(team_id,user_id) VALUES($1,$2)',team,user)
    subject[0]='member'
    return user


async def transfer(c,team,new_captain):
    # Test/admin-only operation. No public role-transfer route is introduced.
    await c.fetchval('SELECT id FROM teams WHERE id=$1 FOR UPDATE',team)
    await c.execute("UPDATE team_members SET role='MEMBER' WHERE team_id=$1 AND role='CAPTAIN'",team)
    await c.execute('UPDATE teams SET captain_id=$2 WHERE id=$1',team,new_captain)
    await c.execute("UPDATE team_members SET role='CAPTAIN' WHERE team_id=$1 AND user_id=$2",team,new_captain)


def test_captain_transfer_rechecks_idempotent_admission_and_finalization():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            other=await add_member(client,pool,schema,team,subject)
            key=uuid4(); value=(await admit(client,team,key)).json()
            await activate(pool,schema,value)
            assert (await complete(client,team,value)).status_code==200
            subject[0]='other'
            assert (await admit(client,team,key,title='member edit')).status_code==403
            assert (await complete(client,team,value)).status_code==403
            for path in ['/submissions/current','/submissions/versions']:
                assert (await client.get('/api/v1'+path,params={'team_id':str(team)})).status_code==200
            async with db.transaction(pool,schema) as c: await transfer(c,team,other)
            # New captain can resume the same immutable finalized resource.
            assert (await complete(client,team,value)).status_code==200
            created=await admit(client,team,key,title='captain edit')
            assert created.status_code==201 and created.json()['version_no']==2
            subject[0]='member'
            assert (await admit(client,team,key)).status_code==403
            assert (await complete(client,team,value)).status_code==403
            assert (await client.get('/api/v1/submissions/versions',params={'team_id':str(team)})).status_code==200
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==1
                assert await c.fetchval('SELECT next_version_no FROM teams WHERE id=$1',team)==3
    asyncio.run(check())


def test_concurrent_captain_and_member_requests_ignore_claimed_roles_and_other_team_captaincy():
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            await add_member(client,pool,schema,team,subject)
            subject[0]='other'
            async with db.transaction(pool,schema) as c:
                hack=await c.fetchval('SELECT hackathon_id FROM teams WHERE id=$1',team)
            other_team=await client.post('/api/v1/teams',json={'name':'other','hackathon_id':str(hack)},headers={'Idempotency-Key':str(uuid4())})
            assert other_team.status_code==201
            async def claims(request: Request):
                # Forged token roles are deliberately ignored by domain mapping.
                return {'iss':'https://phase2.example','sub':request.headers.get('x-test-subject','member'),'role':'CAPTAIN'}
            app.dependency_overrides[verified_identity]=claims
            async def send(subject_name):
                return await client.post('/api/v1/submissions/versions',params={'team_id':str(team)},json=PAYLOAD,
                    headers={'Idempotency-Key':str(uuid4()),'x-test-subject':subject_name})
            results=await asyncio.gather(*(send(name) for name in ['member','other']*4))
            assert [r.status_code for r in results]==[201,403]*4
            assert all(results[n].json()['error']['code']=='CAPTAIN_REQUIRED' for n in [1,3,5,7])
            assert (await send('outsider')).status_code==404
            response=await client.post('/api/v1/submissions/versions',params={'team_id':str(uuid4())},json=PAYLOAD,
                headers={'Idempotency-Key':str(uuid4()),'x-test-subject':'member'})
            assert response.status_code==404
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM submission_versions')==4
                assert await c.fetchval('SELECT next_version_no FROM teams WHERE id=$1',team)==5
                assert await c.fetchval("SELECT count(*) FROM audit_events WHERE detail->>'reason'='CAPTAIN_REQUIRED'")==4
    asyncio.run(check())


@pytest.mark.parametrize('phase',['parts','object'])
def test_captain_revocation_during_storage_observation_prevents_mutation(phase):
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            other=await add_member(client,pool,schema,team,subject)
            value=(await admit(client,team)).json(); await activate(pool,schema,value)
            entered,released=asyncio.Event(),asyncio.Event()
            original=storage.parts if phase=='parts' else storage.completed_object
            async def paused(*args):
                entered.set()
                await released.wait()
                return await original(*args)
            if phase=='parts': storage.parts=paused
            else: storage.completed_object=paused
            task=asyncio.create_task(complete(client,team,value))
            await asyncio.wait_for(entered.wait(),15)
            try:
                async with db.transaction(pool,schema) as c: await transfer(c,team,other)
            finally:
                released.set()
            response=await task
            assert response.status_code==403 and response.json()['error']['code']=='CAPTAIN_REQUIRED'
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM validation_jobs')==0
                assert await c.fetchval('SELECT count(*) FROM completion_intents')==(0 if phase=='parts' else 1)
                assert await c.fetchval('SELECT lifecycle FROM submission_versions WHERE id=$1',UUID(value['version_id']))==('UPLOADING' if phase=='parts' else 'COMPLETING')
            subject[0]='other'
            assert (await complete(client,team,value)).status_code==200
    asyncio.run(check())


def test_captain_transfer_committed_while_admission_waits_is_observed(monkeypatch):
    import submissions
    async def check():
        async with environment() as (client,pool,schema,team,actor,storage,subject):
            other=await add_member(client,pool,schema,team,subject)
            entered=asyncio.Event(); original=submissions.lock_team
            async def observed(*args,**kwargs):
                entered.set()
                return await original(*args,**kwargs)
            monkeypatch.setattr(submissions,'lock_team',observed)
            async with db.transaction(pool,schema) as c:
                await transfer(c,team,other)
                task=asyncio.create_task(admit(client,team))
                await asyncio.wait_for(entered.wait(),15)
                assert not task.done()
            response=await task
            assert response.status_code==403
            subject[0]='other'
            assert (await admit(client,team)).status_code==201
    asyncio.run(check())
