"""Explicit application-schema administration; never touches neon_auth."""
import argparse
import asyncio
import os
import ssl
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0,str(Path('packages/shared/src').resolve()))
from deadline_crush_spikes import db
from deadline_crush_spikes.migrations import apply, schema_name


async def run(args):
    import asyncpg
    schema=schema_name(args.schema)
    connection=await asyncpg.connect(os.environ['DATABASE_URL_UNPOOLED'],ssl=ssl.create_default_context(),timeout=15)
    try:
        if args.action=='migrate':
            await apply(connection,schema)
            print('Application schema migrations applied/verified')
        else:
            async with connection.transaction():
                await connection.execute(f'SET LOCAL search_path TO "{schema}",pg_catalog')
                if args.action=='organizer':
                    user=await connection.fetchval("UPDATE users SET role='ORGANIZER' WHERE id=$1 RETURNING id",args.user_id)
                    if user is None: raise ValueError('Profile not found; sign in first')
                    await connection.execute("INSERT INTO audit_events(actor_id,event_type,entity_id) VALUES($1,'ORGANIZER_PROVISIONED',$1)",user)
                    print('Organizer provisioned by explicit database administrator operation')
                elif args.action=='fixture':
                    if not schema.startswith('phase1_'): raise ValueError('Fixtures require isolated phase1 schema')
                    if await connection.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE auth_issuer='https://fixture.invalid')"):
                        print('Fixtures already present; no changes')
                        return
                    hack=await connection.fetchval("INSERT INTO hackathons(name,deadline_at) VALUES('Development fixture',clock_timestamp()+interval '7 days') RETURNING id")
                    await connection.execute("INSERT INTO users(auth_issuer,auth_subject,role) VALUES('https://fixture.invalid','organizer','ORGANIZER')")
                    for number in range(2):
                        user=await connection.fetchval("INSERT INTO users(auth_issuer,auth_subject) VALUES('https://fixture.invalid',$1) RETURNING id",f'member-{number}')
                        team=await connection.fetchval("INSERT INTO teams(hackathon_id,name,captain_id,idempotency_key,request_hash) VALUES($1,$2,$3,gen_random_uuid(),repeat('0',64)) RETURNING id",hack,f'Team {number+1}',user)
                        await connection.execute("INSERT INTO team_members(team_id,user_id,role) VALUES($1,$2,'CAPTAIN')",team,user)
                        member=await connection.fetchval("INSERT INTO users(auth_issuer,auth_subject) VALUES('https://fixture.invalid',$1) RETURNING id",f'member-{number}-extra')
                        await connection.execute("INSERT INTO team_members(team_id,user_id) VALUES($1,$2)",team,member)
                        await connection.execute("INSERT INTO audit_events(team_id,actor_id,event_type,entity_id) VALUES($1,$2,'FIXTURE_TEAM_CREATED',$1)",team,user)
                    print('Isolated development fixtures created; no authentication accounts created')
    finally: await connection.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['migrate','organizer','fixture'])
    parser.add_argument('--schema',required=True)
    parser.add_argument('--user-id',type=UUID)
    args=parser.parse_args()
    if args.action=='organizer' and args.user_id is None: parser.error('--user-id is required')
    try: asyncio.run(run(args))
    except Exception as error:
        print('Administration failed:',type(error).__name__)
        raise SystemExit(1) from None
