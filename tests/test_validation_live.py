"""Actual R2 source + actual Neon ledger + actual limited Linux parser."""
import asyncio
import io
import json
import os
import sys
import zipfile
from uuid import UUID,uuid4
import pytest
from main import app
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from storage import create_client
from r2_uploads import R2Storage
from submissions import storage_verifier
from validation_jobs import Queue
from validation_pipeline import Pipeline
from validation_worker import process_once
from test_phase2_database import environment,admit,complete
from test_phase3_database import mutate

pytestmark=pytest.mark.skipif(sys.platform!='linux' or not all(os.environ.get(n) for n in
    ('DATABASE_URL_UNPOOLED','R2_ACCOUNT_ID','R2_BUCKET','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY')),
    reason='Linux and real isolated Neon/R2 required')


@pytest.mark.parametrize('kind',['valid_zip','invalid_zip','pdf'])
def test_real_finalized_artifact_to_result_and_private_preview(kind):
    async def check():
        async with environment() as (client,pool,schema,team,*_):
            buffer=io.BytesIO()
            if kind=='pdf':
                import pymupdf
                with pymupdf.open() as d:
                    d.new_page().insert_text((30,30),'Phase 4 live fixture'); data=d.tobytes()
            else:
                with zipfile.ZipFile(buffer,'w') as z:
                    z.writestr('../unsafe' if kind=='invalid_zip' else 'README.md','Live Phase 4 fixture')
                data=buffer.getvalue()
            settings=Settings.load(); sdk=create_client(settings); storage=R2Storage(sdk,settings.R2_BUCKET)
            keys=[]; upload=None
            app.dependency_overrides[storage_verifier]=lambda:storage
            try:
                value=(await admit(client,team,size_bytes=len(data),links=[],
                    filename='project.pdf' if kind=='pdf' else 'project.zip',
                    content_type='application/pdf' if kind=='pdf' else 'application/zip')).json()
                assert (await mutate(client,team,value,'/initialize')).status_code==200
                async with db.transaction(pool,schema) as c:
                    upload=await c.fetchrow('SELECT * FROM upload_sessions WHERE version_id=$1',UUID(value['version_id']))
                keys.append(upload['object_key'])
                part=await storage.call('upload_part',Bucket=settings.R2_BUCKET,Key=upload['object_key'],
                    UploadId=upload['r2_upload_id'],PartNumber=1,Body=data)
                assert (await complete(client,team,value,etag=part['ETag'])).status_code==200
                queue=Queue(pool,schema); assert await queue.dispatch()==1
                pipeline=Pipeline(sdk,settings.R2_BUCKET,queue)
                if kind=='valid_zip':
                    # Real standalone consumer processes, sharing only the
                    # durable database queue. No test processing hook.
                    children=[await asyncio.create_subprocess_exec(sys.executable,'apps/consumer/validation_main.py',
                        '--once',env={**os.environ,'APP_SCHEMA':schema},
                        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL) for _ in range(2)]
                    assert all(code==0 for code in await asyncio.gather(*(c.wait() for c in children)))
                else:
                    assert await process_once(queue,pipeline,uuid4())
                assert not await process_once(queue,pipeline,uuid4())
                if kind=='valid_zip':
                    idle=await asyncio.create_subprocess_exec(sys.executable,'apps/consumer/validation_main.py',
                        '--poll-seconds','0.2',env={**os.environ,'APP_SCHEMA':schema},
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
                    try:
                        ready=json.loads(await asyncio.wait_for(idle.stdout.readline(),15))
                        assert ready['event']=='validation_ready'
                        idle.terminate()
                        assert await asyncio.wait_for(idle.wait(),20)==0
                    finally:
                        if idle.returncode is None: idle.kill(); await idle.wait()
                async with db.transaction(pool,schema) as c:
                    job=await c.fetchrow('SELECT * FROM validation_jobs')
                    artifacts=await c.fetch('SELECT * FROM validation_artifacts')
                    keys.extend(a['object_key'] for a in artifacts)
                    keys.extend(r['object_key'] for r in await c.fetch('SELECT object_key FROM validation_preview_intents'))
                    assert job['status']=='SUCCEEDED'
                    result=json.loads(job['result'])
                    assert result['status']==('NEEDS_FIX' if kind=='invalid_zip' else 'ACCEPTED')
                    import hashlib
                    assert result['source_sha256']==hashlib.sha256(data).hexdigest()
                    assert await c.fetchval('SELECT validation_status FROM submission_versions')==result['status']
                    assert len(artifacts)==(0 if kind=='invalid_zip' else 1)
                for artifact in artifacts:
                    head=await storage.call('head_object',Bucket=settings.R2_BUCKET,Key=artifact['object_key'])
                    assert 0<head['ContentLength']<=262144
                    assert head['Metadata']['generation']=='1'
                    import httpx
                    async with httpx.AsyncClient() as anonymous:
                        response=await anonymous.get(sdk.meta.endpoint_url+'/'+settings.R2_BUCKET+'/'+artifact['object_key'])
                        assert response.status_code in (400,403)
                current=(await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()
                assert current['selected_version_id']==value['version_id'] and current['validation_status']==result['status']
            finally:
                # Exact fixture-created keys only; never enumerate the bucket.
                async with db.transaction(pool,schema) as c:
                    keys.extend(r['object_key'] for r in await c.fetch('SELECT object_key FROM validation_preview_intents'))
                for key in set(keys):
                    await storage.call('delete_object',Bucket=settings.R2_BUCKET,Key=key)
                if upload: await storage.abort(upload['object_key'],upload['r2_upload_id'])
                await storage.close(); sdk.close()
    asyncio.run(check())
