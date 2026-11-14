"""Adapter unit tests: SDK doubles, never reported as real R2 evidence."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from r2_uploads import R2Storage, object_etag
from submissions import UploadAuthorization, PartInput, SubmissionError, StoredPart
from uploads import SignInput
from fastapi.testclient import TestClient
from main import app


def upload():
    return UploadAuthorization(uuid4(),uuid4(),'disposable/source','opaque-upload',8388611,8388608,2)


def manifest():
    return (PartInput(part_number=1,etag='"'+ 'ab'*16 +'"'),PartInput(part_number=2,etag='"'+'cd'*16+'"'))


def missing():
    return ClientError({'Error':{'Code':'NoSuchKey'}},'HeadObject')


@pytest.mark.parametrize('value',[[],[True],['1'],[0],[-1],[14],[1,1]])
def test_sign_input_rejects_untrusted_ranges(value):
    with pytest.raises(ValidationError): SignInput(part_numbers=value)


@pytest.mark.parametrize('value',['etag','"bad"','a'*32,'"'+'z'*32+'"','"'+'a'*31+'"'])
def test_invalid_r2_etag_rejected(value):
    with pytest.raises(SubmissionError): object_etag((PartInput(part_number=1,etag=value),))


def test_sign_binds_every_capability_and_uses_put():
    async def check():
        client=Mock(); client.generate_presigned_url.return_value='secret-url'
        adapter=R2Storage(client,'private'); u=upload()
        try:
            assert await adapter.sign(u,[1,2],30)==['secret-url','secret-url']
            for n,call in enumerate(client.generate_presigned_url.call_args_list,1):
                assert call.kwargs=={'ClientMethod':'upload_part','Params':{'Bucket':'private','Key':u.object_key,
                    'UploadId':u.r2_upload_id,'PartNumber':n},'ExpiresIn':30,'HttpMethod':'PUT'}
        finally: await adapter.close()
    asyncio.run(check())


def test_list_parts_pagination_and_exact_sizes():
    async def check():
        client=Mock(); u=upload(); m=manifest()
        client.list_parts.side_effect=[{'Parts':[{'PartNumber':1,'ETag':m[0].etag,'Size':8388608}],
            'IsTruncated':True,'NextPartNumberMarker':1},
            {'Parts':[{'PartNumber':2,'ETag':m[1].etag,'Size':3}],'IsTruncated':False}]
        adapter=R2Storage(client,'private')
        try:
            assert await adapter.parts(u)==(StoredPart(1,m[0].etag,8388608),StoredPart(2,m[1].etag,3))
            assert client.list_parts.call_args.kwargs['PartNumberMarker']==1
        finally: await adapter.close()
    asyncio.run(check())


@pytest.mark.parametrize('mode',['duplicate','out_of_range','loop','too_many'])
def test_list_parts_fails_closed(mode):
    async def check():
        client=Mock(); p={'PartNumber':1,'ETag':manifest()[0].etag,'Size':1}
        page={'Parts':[p,p] if mode=='duplicate' else [{**p,'PartNumber':3}] if mode=='out_of_range' else [p]*3 if mode=='too_many' else [p],
              'IsTruncated':mode=='loop','NextPartNumberMarker':0}
        client.list_parts.return_value=page
        adapter=R2Storage(client,'private')
        try:
            with pytest.raises(SubmissionError): await adapter.parts(upload())
        finally: await adapter.close()
    asyncio.run(check())


@pytest.mark.parametrize('field',['size','metadata','etag','session'])
def test_head_rejects_wrong_object_identity(field):
    async def check():
        client=Mock(); adapter=R2Storage(client,'private'); u=upload(); m=manifest()
        head={'ContentLength':u.expected_size,'Metadata':adapter.metadata(u),'ETag':object_etag(m)}
        if field=='size': head['ContentLength']=1
        if field=='metadata': head['Metadata']={}
        if field=='etag': head['ETag']='"replaced"'
        if field=='session': head['Metadata']={**head['Metadata'],'session-id':str(uuid4())}
        client.head_object.return_value=head
        try:
            with pytest.raises(SubmissionError,match='Storage evidence mismatch'): await adapter.head(u,m)
        finally: await adapter.close()
    asyncio.run(check())


@pytest.mark.parametrize('lost',[False,True])
def test_exact_completion_then_independent_head(lost):
    async def check():
        client=Mock(); adapter=R2Storage(client,'private'); u=upload(); m=manifest()
        client.head_object.side_effect=[missing(),{'ContentLength':u.expected_size,'Metadata':adapter.metadata(u),'ETag':object_etag(m)}]
        client.list_parts.return_value={'Parts':[{'PartNumber':p.part_number,'ETag':p.etag,
            'Size':8388608 if p.part_number==1 else 3} for p in m]}
        if lost: client.complete_multipart_upload.side_effect=TimeoutError('secret URL must not escape')
        try:
            evidence=await adapter.completed_object(u,m)
            assert evidence.size_bytes==u.expected_size
            assert client.complete_multipart_upload.call_args.kwargs['MultipartUpload']=={'Parts':[
                {'PartNumber':p.part_number,'ETag':p.etag} for p in m]}
        finally: await adapter.close()
    asyncio.run(check())


def test_no_such_upload_without_object_never_finalizes():
    async def check():
        client=Mock(); client.head_object.side_effect=missing()
        client.list_parts.side_effect=ClientError({'Error':{'Code':'NoSuchUpload'}},'ListParts')
        adapter=R2Storage(client,'private')
        try:
            with pytest.raises(SubmissionError): await adapter.completed_object(upload(),manifest())
            client.complete_multipart_upload.assert_not_called()
        finally: await adapter.close()
    asyncio.run(check())


@pytest.mark.parametrize('field',['etag','size','missing'])
def test_changed_parts_never_complete(field):
    async def check():
        client=Mock(); client.head_object.side_effect=missing(); m=manifest()
        client.list_parts.return_value={'Parts':[] if field=='missing' else [
            {'PartNumber':p.part_number,'ETag':m[1].etag if field=='etag' else p.etag,
             'Size':1 if field=='size' else 8388608 if p.part_number==1 else 3} for p in m]}
        adapter=R2Storage(client,'private')
        try:
            with pytest.raises(SubmissionError): await adapter.completed_object(upload(),m)
            client.complete_multipart_upload.assert_not_called()
        finally: await adapter.close()
    asyncio.run(check())


def test_cleanup_exact_key_only_never_deletes_objects():
    async def check():
        client=Mock(); client.list_multipart_uploads.return_value={'Uploads':[
            {'Key':'expected/source','UploadId':'orphan'},{'Key':'expected/source-other','UploadId':'other'}]}
        adapter=R2Storage(client,'private')
        try:
            await adapter.sweep_key('expected/source')
            client.abort_multipart_upload.assert_called_once_with(Bucket='private',Key='expected/source',UploadId='orphan')
            client.delete_object.assert_not_called()
        finally: await adapter.close()
    asyncio.run(check())


def test_create_sets_server_metadata():
    async def check():
        client=Mock(); client.create_multipart_upload.return_value={'UploadId':'created'}
        adapter=R2Storage(client,'private'); u=upload()
        try:
            assert await adapter.create(u,'application/zip')=='created'
            assert client.create_multipart_upload.call_args.kwargs['Metadata']==adapter.metadata(u)
        finally: await adapter.close()
    asyncio.run(check())


@pytest.mark.parametrize('method,path,body',[
    ('post','/initialize',None),('post','/resume',None),
    ('post','/parts/sign',{'part_numbers':[1]}),('get','',None)])
def test_upload_routes_require_managed_identity(method,path,body):
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.request(method,'/api/v1/uploads/'+str(uuid4())+path,
            params={'team_id':str(uuid4())},headers={'Idempotency-Key':str(uuid4())},
            **({'json':body} if body else {}))
        assert response.status_code==401


def test_sdk_failure_is_redacted():
    async def check():
        client=Mock(); client.head_object.side_effect=RuntimeError('secret presigned URL')
        adapter=R2Storage(client,'private')
        try:
            with pytest.raises(SubmissionError) as failure: await adapter.head(upload(),manifest())
            assert failure.value.code=='STORAGE_UNAVAILABLE'
            assert 'secret' not in str(failure.value)
        finally: await adapter.close()
    asyncio.run(check())


def test_lifespan_does_not_leave_a_closed_storage_adapter(monkeypatch):
    from unittest.mock import AsyncMock
    sdk=Mock()
    monkeypatch.setattr('main.db.create_pool',AsyncMock(return_value=None))
    monkeypatch.setattr('main.storage.create_client',lambda settings:sdk)
    with TestClient(app,raise_server_exceptions=False):
        assert isinstance(app.state.submission_verifier,R2Storage)
    assert app.state.submission_verifier is None
    sdk.close.assert_called_once()


def test_cancelled_requests_do_not_release_running_sdk_slots():
    import threading
    async def check():
        started=threading.Event(); release=threading.Event(); calls=[]
        def blocked(**kwargs):
            calls.append(True); started.set(); release.wait(timeout=2)
            return {'ok':True}
        client=Mock(); client.head_object.side_effect=blocked
        adapter=R2Storage(client,'private',concurrency=1)
        first=asyncio.create_task(adapter.call('head_object'))
        second=None
        try:
            assert await asyncio.to_thread(started.wait,1)
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(first,timeout=0.2)
            second=asyncio.create_task(adapter.call('head_object'))
            await asyncio.sleep(0.02)
            assert len(calls)==1 and not second.done()
            release.set()
            assert await second=={'ok':True}
        finally:
            release.set()
            if second and not second.done(): await second
            await adapter.close()
    asyncio.run(check())
