import asyncio
import os
from uuid import uuid4
from dataclasses import replace
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
from main import app
from identity import verified_identity
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from phase1_support import foundation

pytestmark=pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'),reason='live isolated Neon schema requires database URLs')


def test_foundation_schema_constraints_rollback_and_isolation():
    async def check():
        async with foundation() as (schema,pool,admin):
            async with db.transaction(pool,schema) as c:
                names=await c.fetch('SELECT table_name FROM information_schema.tables WHERE table_schema=$1',schema)
                assert len(names)==15  # Domain tables, storage/preview intents and migration ledger.
                assert await c.fetchval("SELECT count(*) FROM schema_migrations")==5
                assert not await c.fetchval("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema=$1 AND column_name='password_hash')",schema)
                user=await c.fetchval("INSERT INTO users(auth_issuer,auth_subject) VALUES('issuer','opaque') RETURNING id")
                for sql,args,error in [
                    ("INSERT INTO users(auth_issuer,auth_subject) VALUES('issuer','opaque')",(),asyncpg.UniqueViolationError),
                    ("INSERT INTO users(auth_issuer,auth_subject,role) VALUES('issuer','other','ADMIN')",(),asyncpg.CheckViolationError),
                    ("INSERT INTO team_members(team_id,user_id) VALUES($1,$2)",(uuid4(),user),asyncpg.ForeignKeyViolationError),
                    ("INSERT INTO hackathons(name,deadline_at,grace_seconds) VALUES('x',clock_timestamp(),5)",(),asyncpg.CheckViolationError)]:
                    with pytest.raises(error):
                        async with c.transaction(): await c.execute(sql,*args)
            with pytest.raises(ValueError):
                async with db.transaction(pool,schema) as c:
                    await c.execute("INSERT INTO users(auth_issuer,auth_subject) VALUES('issuer','rollback')")
                    raise ValueError('rollback')
            async with db.transaction(pool,schema) as c:
                assert await c.fetchval('SELECT count(*) FROM users')==1
            async with pool.acquire() as c:
                assert await c.fetchval('SELECT current_schema()')!=schema
                assert await c.fetchval('SELECT 1')==1
    asyncio.run(check())


def test_profile_concurrency_and_team_authorization():
    async def check():
        old_settings=getattr(app.state,'settings',None); old_pool=getattr(app.state,'pool',None)
        async with foundation() as (schema,pool,admin):
            app.state.settings=replace(Settings.load(),APP_SCHEMA=schema); app.state.pool=pool
            # Only authentication is mocked; actual ASGI routes and real Neon SQL run.
            subject='participant'
            async def claims(): return {'iss':'https://test.example','sub':subject,'email':'unverified@example.com','role':'ORGANIZER'}
            app.dependency_overrides[verified_identity]=claims
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    profiles=await asyncio.gather(*(client.get('/api/v1/users/me') for _ in range(8)))
                    assert all(r.status_code==200 for r in profiles)
                    assert len({r.json()['id'] for r in profiles})==1
                    own=profiles[0].json()['id']; assert profiles[0].json()['role']=='MEMBER'
                    assert (await client.get('/api/v1/organizer/metrics')).status_code==403
                    async with db.transaction(pool,schema) as c:
                        hack=await c.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('fixture',clock_timestamp()+interval '1 day') RETURNING id")
                    payload={'name':'Team A','hackathon_id':str(hack)}; headers={'Idempotency-Key':str(uuid4())}
                    responses=await asyncio.gather(*(client.post('/api/v1/teams',json=payload,headers=headers) for _ in range(4)))
                    assert sorted(r.status_code for r in responses)==[200,200,200,201]
                    team=responses[0].json(); assert team['captain_id']==own
                    assert (await client.get('/api/v1/teams/'+team['id'])).status_code==200
                    assert (await client.get('/api/v1/teams/'+team['id']+'/members')).json()['members'][0]['role']=='CAPTAIN'
                    assert len((await client.get('/api/v1/teams')).json()['teams'])==1
                    assert (await client.post('/api/v1/teams',json={**payload,'name':'changed'},headers=headers)).status_code==409
                    assert (await client.post('/api/v1/teams',json={**payload,'role':'ORGANIZER'},headers=headers)).status_code==422
                    assert (await client.get('/api/v1/teams/not-a-uuid')).status_code==422
                    subject='other-participant'
                    other=(await client.get('/api/v1/users/me')).json()['id']
                    assert (await client.get('/api/v1/teams')).json()=={'teams':[]}
                    assert (await client.get('/api/v1/teams/'+team['id'])).status_code==404
                    assert (await client.get('/api/v1/teams/'+team['id']+'/members')).status_code==404
                    from uuid import UUID
                    async with db.transaction(pool,schema) as c:
                        await c.execute('INSERT INTO team_members(team_id,user_id) VALUES($1,$2)',UUID(team['id']),UUID(other))
                    assert (await client.get('/api/v1/teams/'+team['id'])).status_code==200
                    async with db.transaction(pool,schema) as c:
                        await c.execute('DELETE FROM team_members WHERE team_id=$1 AND user_id=$2',UUID(team['id']),UUID(other))
                        await c.execute("UPDATE users SET role='ORGANIZER' WHERE id=$1",UUID(other))
                    assert (await client.get('/api/v1/teams/'+team['id'])).status_code==404
                    assert (await client.get('/api/v1/organizer/metrics')).status_code==200
                    async with db.transaction(pool,schema) as c:
                        assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='TEAM_CREATED'")==1
                        with pytest.raises(asyncpg.CheckViolationError):
                            async with c.transaction(): await c.execute("UPDATE audit_events SET event_type='changed'")
            finally:
                app.dependency_overrides.clear(); app.state.settings=old_settings; app.state.pool=old_pool
    asyncio.run(check())
