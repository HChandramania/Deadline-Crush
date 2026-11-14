"""Consume a live SDK JWT on stdin; verify in FastAPI against real branch JWKS.

Only application rows in a fresh disposable schema are written. Managed auth
tables are not queried or modified directly. No JWT or email is printed.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path[:0]=[str(Path(p).resolve()) for p in ['apps/api','packages/shared/src','tests']]
import httpx
from main import app, lifespan
from phase1_support import foundation


async def run(token):
    async with foundation() as (schema,pool,admin):
        original=os.environ.get('APP_SCHEMA')
        os.environ['APP_SCHEMA']=schema
        try:
            async with lifespan(app):
                claims=await app.state.verifier.verify(token)
                assert claims['iss']==app.state.verifier.issuer
                assert claims['aud']==app.state.verifier.audience
                assert isinstance(claims['sub'],str)
                assert claims.get('exp')>claims.get('iat')
                print('Live managed JWT signature/issuer/audience/expiry/opaque subject: PASS')
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                    assert (await client.get('/api/v1/users/me')).status_code==401
                    first=await client.get('/api/v1/users/me',headers={'Authorization':'Bearer '+token})
                    assert first.status_code==200 and first.json()['role']=='MEMBER'
                    again=await client.get('/api/v1/users/me',headers={'Authorization':'Bearer '+token})
                    assert again.json()['id']==first.json()['id']
                    teams=await client.get('/api/v1/teams',headers={'Authorization':'Bearer '+token})
                    assert teams.status_code==200 and teams.json()=={'teams':[]}
                    assert (await client.get('/api/v1/organizer/metrics',headers={'Authorization':'Bearer '+token})).status_code==403
                print('Live SDK identity -> FastAPI -> isolated Neon profile/team authorization: PASS')
        finally:
            if original is None: os.environ.pop('APP_SCHEMA',None)
            else: os.environ['APP_SCHEMA']=original
    print('Isolated application schema cleanup: PASS')


if __name__=='__main__':
    try: asyncio.run(run(json.load(sys.stdin)['token']))
    except Exception as error:
        print('Live identity verification failed:',type(error).__name__)
        raise SystemExit(1) from None
