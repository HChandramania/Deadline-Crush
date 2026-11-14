import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import asyncpg
import pytest
from deadline_crush_spikes import db
from deadline_crush_spikes.migrations import apply
from phase1_support import foundation

pytestmark=pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'),reason='live isolated Neon schema requires database URLs')


def test_immutable_versions_manifest_selection_and_deadline():
    async def check():
        async with foundation() as (schema,pool,admin):
            async with db.transaction(pool,schema) as c:
                user=await c.fetchval("INSERT INTO users(auth_issuer,auth_subject) VALUES('fixture','one') RETURNING id")
                hack=await c.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('fixture',clock_timestamp()+interval '1 day') RETURNING id")
                team=await c.fetchval("INSERT INTO teams(hackathon_id,name,captain_id,idempotency_key,request_hash) VALUES($1,'fixture',$2,$3,repeat('0',64)) RETURNING id",hack,user,uuid4())
                await c.execute("INSERT INTO team_members(team_id,user_id,role) VALUES($1,$2,'CAPTAIN')",team,user)
                version=await c.fetchval("""INSERT INTO submission_versions(team_id,version_no,idempotency_key,request_hash,title,object_key,expected_size,admitted_at)
                    VALUES($1,1,$2,repeat('0',64),'immutable','unique-key',5,clock_timestamp()) RETURNING id""",team,uuid4())
                upload=await c.fetchval("""INSERT INTO upload_sessions(version_id,object_key,status,part_size,expected_parts,expires_at)
                    VALUES($1,'unique-key','ACTIVE',8388608,1,clock_timestamp()) RETURNING id""",version)
                await c.execute("""INSERT INTO completion_intents(upload_session_id,idempotency_key,manifest,manifest_sha256,admitted_at,status)
                    VALUES($1,$2,'[]',repeat('0',64),clock_timestamp(),'ADMITTED')""",upload,uuid4())
                for sql,args in [
                    ('UPDATE submission_versions SET title=\'changed\' WHERE id=$1',(version,)),
                    ('DELETE FROM submission_versions WHERE id=$1',(version,)),
                    ("UPDATE completion_intents SET manifest='[1]' WHERE upload_session_id=$1",(upload,)),
                    ("UPDATE hackathons SET deadline_at=clock_timestamp() WHERE id=$1",(hack,))]:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with c.transaction(): await c.execute(sql,*args)
                await c.execute("UPDATE submission_versions SET lifecycle='UPLOADING' WHERE id=$1",version)
                await c.execute("UPDATE submission_versions SET lifecycle='COMPLETING' WHERE id=$1",version)
                await c.execute("UPDATE submission_versions SET lifecycle='FINALIZED',finalized_at=clock_timestamp() WHERE id=$1",version)
                await c.execute('UPDATE teams SET selected_version_id=$1,selected_version_no=1 WHERE id=$2',version,team)
            with pytest.raises(asyncpg.CheckViolationError):
                async with db.transaction(pool,schema) as c:
                    await c.execute('UPDATE teams SET selected_version_no=2 WHERE id=$1',team)
            with pytest.raises(asyncpg.CheckViolationError):
                async with db.transaction(pool,schema) as c:
                    await c.execute("UPDATE submission_versions SET lifecycle='UPLOADING' WHERE id=$1",version)
            async with db.transaction(pool,schema) as c:
                await c.execute("UPDATE submission_versions SET validation_status='NEEDS_FIX' WHERE id=$1",version)
                assert await c.fetchval('SELECT selected_version_no FROM teams WHERE id=$1',team)==1
    asyncio.run(check())


def test_migration_checksum_refuses_changed_history():
    async def check():
        async with foundation() as (schema,pool,admin):
            with TemporaryDirectory() as directory:
                Path(directory,'001_foundation.sql').write_text('-- changed history')
                with pytest.raises(ValueError,match='checksum'):
                    await apply(admin,schema,Path(directory))
    asyncio.run(check())
