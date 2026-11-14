"""Actual R2+Neon submission pipeline; identity is controlled unless JWT supplied.

Disposable application schema and version-scoped opaque R2 keys only. Never log
responses/URLs. Cleanup deletes only keys allocated by this test's admissions.
"""
import asyncio
import os
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from main import app
from r2_uploads import R2Storage
from storage import create_client
from storage_recovery import process_once
from submissions import storage_verifier
from test_phase2_database import environment, admit, complete
from test_phase3_database import mutate

pytestmark=pytest.mark.skipif(not all(os.environ.get(n) for n in
    ('DATABASE_URL_UNPOOLED','R2_ACCOUNT_ID','R2_BUCKET','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY')),
    reason='live Neon and R2 configuration required')


@pytest.mark.parametrize('lost_response',[False,True])
def test_actual_r2_to_atomic_neon_finalization(lost_response):
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            settings=Settings.load(); sdk=create_client(settings)
            adapter=R2Storage(sdk,settings.R2_BUCKET); keys=[]; uploads=[]
            app.dependency_overrides[storage_verifier]=lambda:adapter
            original_call=adapter.call
            auth_http=None
            previous_verifier=getattr(app.state,'verifier',None)
            try:
                token=os.environ.get('PHASE3_ACCESS_JWT')
                if token:
                    # Optional existing managed session; no synthetic signup.
                    from identity import TokenVerifier, verified_identity
                    auth_http=httpx.AsyncClient(timeout=5,follow_redirects=False)
                    app.state.verifier=TokenVerifier(settings.NEON_AUTH_BASE_URL,settings.NEON_AUTH_JWKS_URL,auth_http)
                    app.dependency_overrides.pop(verified_identity)
                    client.headers['Authorization']='Bearer '+token
                    profile=await client.get('/api/v1/users/me')
                    assert profile.status_code==200,'Managed JWT verification failed'
                    actor=UUID(profile.json()['id'])
                    async with db.transaction(pool,schema) as c:
                        hack=await c.fetchval('SELECT hackathon_id FROM teams WHERE id=$1',team)
                    from uuid import uuid4
                    created=await client.post('/api/v1/teams',json={'hackathon_id':str(hack),'name':'managed-jwt-phase3'},
                        headers={'Idempotency-Key':str(uuid4())})
                    assert created.status_code==201
                    team=UUID(created.json()['id'])
                # 8 MiB + final short part establishes the actual part-size rule.
                size=8388608+17
                value=(await admit(client,team,size_bytes=size)).json()
                assert (await mutate(client,team,value,'/initialize')).status_code==200
                async with db.transaction(pool,schema) as c:
                    row=await c.fetchrow('SELECT object_key,r2_upload_id FROM upload_sessions WHERE id=$1',UUID(value['upload_session_id']))
                    keys.append(row['object_key']); uploads.append(row['r2_upload_id'])
                signed=await mutate(client,team,value,'/parts/sign',{'part_numbers':[1,2]})
                assert signed.status_code==200
                parts=[]
                async with httpx.AsyncClient(timeout=60) as direct:
                    for n,part in enumerate(signed.json()['parts'],1):
                        try:
                            put=await direct.put(part['url'],content=b'x'*(8388608 if n==1 else 17))
                        except httpx.HTTPError:
                            pytest.fail('Direct R2 transport failure; bearer URL omitted',pytrace=False)
                        assert put.status_code==200, 'R2 UploadPart rejected; response omitted'
                        parts.append({'part_number':n,'etag':put.headers['etag']})
                resume=(await mutate(client,team,value,'/resume')).json()
                assert resume['missing_parts']==[]
                assert [p['size_bytes'] for p in resume['completed_parts']]==[8388608,17]
                if lost_response:
                    async def crash_after_complete(method,**kwargs):
                        result=await original_call(method,**kwargs)
                        if method=='complete_multipart_upload':
                            # A process death bypasses request exception recovery.
                            raise SystemExit('injected crash after successful R2 completion')
                        return result
                    adapter.call=crash_after_complete
                    # Admit via the actual Phase 2 protocol, then execute the
                    # external operation separately to inject process-death boundary.
                    from submissions import admit_intent, CompletionInput, finalize
                    from uuid import uuid4
                    request=SimpleNamespace(app=app)
                    await admit_intent(request,actor,team,UUID(value['upload_session_id']),uuid4(),CompletionInput(parts=parts),adapter)
                    with pytest.raises(SystemExit):
                        await finalize(request,actor,team,UUID(value['upload_session_id']),adapter)
                    adapter.call=original_call
                    async with db.transaction(pool,schema) as c:
                        assert await c.fetchval('SELECT count(*) FROM validation_jobs')==0
                        assert await c.fetchval('SELECT status FROM completion_intents')=='ADMITTED'
                    # UploadId is gone; independent HEAD is now required.
                    await asyncio.gather(process_once(request,adapter),process_once(request,adapter))
                else:
                    response=await mutate(client,team,value,'/complete',{'parts':parts})
                    assert response.status_code==200
                # Replayed HTTP finalization does no storage mutation and produces
                # one logical job/outbox/audit regardless of execution count.
                for _ in range(2):
                    response=await mutate(client,team,value,'/complete',{'parts':parts})
                    assert response.status_code==200
                current=(await client.get('/api/v1/submissions/current',params={'team_id':str(team)})).json()
                assert current['selected_version_id']==value['version_id'] and current['version_no']==1
                async with db.transaction(pool,schema) as c:
                    assert await c.fetchval('SELECT status FROM completion_intents')=='FINALIZED'
                    for table in ['validation_jobs','outbox']:
                        assert await c.fetchval(f'SELECT count(*) FROM {table}')==1
                    assert await c.fetchval("SELECT count(*) FROM audit_events WHERE event_type='VERSION_FINALIZED'")==1
                head=await original_call('head_object',Bucket=settings.R2_BUCKET,Key=keys[0])
                assert head['ContentLength']==size
            finally:
                adapter.call=original_call
                for key,upload_id in zip(keys,uploads):
                    await adapter.abort(key,upload_id)
                    await original_call('delete_object',Bucket=settings.R2_BUCKET,Key=key)
                    await adapter.sweep_key(key)
                await adapter.close(); sdk.close()
                app.state.verifier=previous_verifier
                if auth_http is not None: await auth_http.aclose()
    asyncio.run(check())


def test_actual_ambiguous_initialization_and_orphan_abort():
    async def check():
        async with environment() as (client,pool,schema,team,actor,_,subject):
            settings=Settings.load(); sdk=create_client(settings)
            adapter=R2Storage(sdk,settings.R2_BUCKET); original_create=adapter.create
            created=[]
            async def ambiguous(upload, content_type):
                upload_id=await original_create(upload,content_type)
                created.append((upload.object_key,upload_id))
                raise TimeoutError('injected lost create response')
            adapter.create=ambiguous
            app.dependency_overrides[storage_verifier]=lambda:adapter
            try:
                value=(await admit(client,team)).json()
                assert (await mutate(client,team,value,'/initialize')).status_code==503
                assert (await mutate(client,team,value,'/initialize')).status_code==202
                assert len(created)==1
                async with db.transaction(pool,schema) as c:
                    assert await c.fetchval('SELECT create_state FROM storage_operations')=='STARTED'
                    assert await c.fetchval('SELECT r2_upload_id FROM upload_sessions') is None
                key,upload_id=created[0]
                # Exercise actual R2 ListMultipartUploads and exact-key abort.
                # Cleanup eligibility/retention is independently tested in Neon.
                await adapter.sweep_key(key)
                from submissions import SubmissionError
                with pytest.raises(SubmissionError) as failure:
                    await adapter.call('list_parts',Bucket=settings.R2_BUCKET,Key=key,UploadId=upload_id)
                assert failure.value.code=='STORAGE_NOT_FOUND'
            finally:
                for key,upload_id in created: await adapter.abort(key,upload_id)
                await adapter.close(); sdk.close()
    asyncio.run(check())


def test_actual_r2_cors_preflight_readonly():
    """Browser-compatible HTTP checks; no JavaScript/browser execution claim."""
    async def check():
        settings=Settings.load(); sdk=create_client(settings)
        adapter=R2Storage(sdk,settings.R2_BUCKET)
        from submissions import UploadAuthorization
        from uuid import uuid4
        # No object/upload mutation is necessary to inspect bucket CORS behavior.
        upload=UploadAuthorization(uuid4(),uuid4(),'phase0/'+str(uuid4()),'not-a-real-upload',1,8388608,1)
        upload_id=None; verification={}
        try:
            url=(await adapter.sign(upload,[1],30))[0]
            try:
                async with httpx.AsyncClient(timeout=15) as direct:
                    response=await direct.options(url,headers={'Origin':'http://localhost:5173',
                        'Access-Control-Request-Method':'PUT','Access-Control-Request-Headers':'content-type'})
            except httpx.HTTPError:
                pytest.fail('R2 preflight transport failed; URL omitted',pytrace=False)
            if response.status_code in (400,403) and 'access-control-allow-origin' not in response.headers:
                pytest.skip('Existing bucket does not permit localhost preflight; bucket configuration left unchanged')
            assert response.status_code in (200,204)
            assert response.headers.get('access-control-allow-origin')=='http://localhost:5173'
            assert 'PUT' in response.headers.get('access-control-allow-methods','')
            verification['options_put']=response.status_code
            upload=replace(upload,expected_size=17,r2_upload_id=None)
            upload_id=await adapter.create(upload,'application/octet-stream')
            upload=replace(upload,r2_upload_id=upload_id)
            url=(await adapter.sign(upload,[1],30))[0]
            async with httpx.AsyncClient(timeout=30) as direct:
                try:
                    put=await direct.put(url,content=b'cors-development!',headers={
                        'Origin':'http://localhost:5173','Content-Type':'application/octet-stream'})
                except httpx.HTTPError:
                    pytest.fail('R2 browser-compatible PUT failed; URL omitted',pytrace=False)
                assert put.status_code==200
                assert put.headers.get('access-control-allow-origin')=='http://localhost:5173'
                assert 'etag' in put.headers.get('access-control-expose-headers','').lower()
                verification['put']=put.status_code
                verification['put_etag_exposed']=True
                from submissions import PartInput
                evidence=await adapter.completed_object(upload,(PartInput(part_number=1,etag=put.headers['etag']),))
                assert evidence and evidence.size_bytes==17
                head_url=await adapter.call('generate_presigned_url',ClientMethod='head_object',
                    Params={'Bucket':settings.R2_BUCKET,'Key':upload.object_key},ExpiresIn=30,HttpMethod='HEAD')
                try:
                    preflight=await direct.options(head_url,headers={'Origin':'http://localhost:5173','Access-Control-Request-Method':'HEAD'})
                    head=await direct.head(head_url,headers={'Origin':'http://localhost:5173'})
                    denied=await direct.options(head_url,headers={'Origin':'https://unapproved-phase3.invalid','Access-Control-Request-Method':'HEAD'})
                except httpx.HTTPError:
                    pytest.fail('R2 HEAD/preflight transport failed; URL omitted',pytrace=False)
                assert preflight.status_code in (200,204)
                assert preflight.headers.get('access-control-allow-origin')=='http://localhost:5173'
                assert head.status_code==200 and int(head.headers['content-length'])==17
                assert head.headers.get('access-control-allow-origin')=='http://localhost:5173'
                assert 'etag' in head.headers.get('access-control-expose-headers','').lower() and head.headers.get('etag')
                assert denied.headers.get('access-control-allow-origin')!='https://unapproved-phase3.invalid'
                verification.update(options_head=preflight.status_code,head=head.status_code,
                    head_etag_exposed=True,unapproved_origin_denied=True)
                # No public settings are changed; this checks the S3 endpoint only.
                from urllib.parse import urlsplit,urlunsplit
                parsed=urlsplit(head_url)
                unsigned=await direct.head(urlunsplit((parsed.scheme,parsed.netloc,parsed.path,'','')))
                assert unsigned.status_code in (400,403) and not unsigned.headers.get('etag')
                verification['unsigned_s3_head']=unsigned.status_code
            report_path=Path('docs/phase3-cors-results.json')
            report=json.loads(report_path.read_text()) if report_path.exists() else {}
            report['http_verification']=verification
            report['actual_browser_javascript']='Not run; no connected browser surface'
            report_path.write_text(json.dumps(report,indent=2)+'\n')
        finally:
            if upload_id:
                await adapter.abort(upload.object_key,upload_id)
                await adapter.call('delete_object',Bucket=settings.R2_BUCKET,Key=upload.object_key)
            await adapter.close(); sdk.close()
    asyncio.run(check())
