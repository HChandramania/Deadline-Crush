import asyncio
import hashlib
import io
import json
import threading
from uuid import uuid4
from types import SimpleNamespace
import pytest
from validation_pipeline import download,ProcessingFailure,blocking
from submissions import UploadAuthorization,PartInput,fingerprint
from r2_uploads import R2Storage,object_etag


def source_and_response(data=b'valid fixture'):
    version,session=uuid4(),uuid4()
    source={'id':version,'session_id':session,'object_key':f'submissions/{version}/source',
        'r2_upload_id':'upload','expected_size':len(data),'part_size':8388608,'expected_parts':1,
        'verified_sha256':None}
    manifest=[{'part_number':1,'etag':'"'+hashlib.md5(data,usedforsecurity=False).hexdigest()+'"'}]
    source['manifest']=json.dumps(manifest)
    auth=UploadAuthorization(session,version,source['object_key'],'upload',len(data),8388608,1)
    adapter=R2Storage(None,'private'); metadata=adapter.metadata(auth); adapter.executor.shutdown()
    response={'Body':io.BytesIO(data),'ContentLength':len(data),'Metadata':metadata,
        'ETag':object_etag([PartInput(**p) for p in manifest])}
    return source,response


def test_streamed_get_exact_identity_and_body_closed(tmp_path):
    source,response=source_and_response()
    calls=[]
    def get(**kwargs): calls.append(kwargs); return response
    digest=download(SimpleNamespace(get_object=get),'private',source,tmp_path/'source')
    assert digest==hashlib.sha256(b'valid fixture').hexdigest()
    assert response['Body'].closed
    assert calls==[{'Bucket':'private','Key':source['object_key'],'IfMatch':response['ETag']}]


@pytest.mark.parametrize('field,value',[('ContentLength',1),('Metadata',{}),('ETag','changed')])
def test_retrieval_rejects_mismatched_evidence(tmp_path,field,value):
    source,response=source_and_response(); response[field]=value
    with pytest.raises(ProcessingFailure,match='IDENTITY'):
        download(SimpleNamespace(get_object=lambda **kw:response),'private',source,tmp_path/'source')
    assert response['Body'].closed


def test_bytes_must_match_frozen_manifest(tmp_path):
    source,response=source_and_response(); response['Body']=io.BytesIO(b'evil! fixture')
    with pytest.raises(ProcessingFailure,match='IDENTITY'):
        download(SimpleNamespace(get_object=lambda **kw:response),'private',source,tmp_path/'source')


def test_known_sha256_mismatch_rejected(tmp_path):
    source,response=source_and_response(); source['verified_sha256']='0'*64
    with pytest.raises(ProcessingFailure,match='IDENTITY'):
        download(SimpleNamespace(get_object=lambda **kw:response),'private',source,tmp_path/'source')


def test_short_and_excess_body_rejected(tmp_path):
    for i,data in enumerate((b'x',b'x'*100)):
        source,response=source_and_response(); response['Body']=io.BytesIO(data)
        with pytest.raises(ProcessingFailure,match='IDENTITY'):
            download(SimpleNamespace(get_object=lambda **kw:response),'private',source,tmp_path/str(i))
        assert response['Body'].closed


def test_multiple_part_boundaries_with_short_reads(tmp_path):
    data=b'a'*8388608+b'b'*100
    source,response=source_and_response(data)
    source['expected_parts']=2
    parts=[PartInput(part_number=i+1,etag='"'+hashlib.md5(p,usedforsecurity=False).hexdigest()+'"')
        for i,p in enumerate((data[:8388608],data[8388608:]))]
    source['manifest']=json.dumps([p.model_dump() for p in parts])
    source_auth=UploadAuthorization(source['session_id'],source['id'],source['object_key'],'upload',len(data),8388608,2)
    adapter=R2Storage(None,'private'); response['Metadata']=adapter.metadata(source_auth); adapter.executor.shutdown()
    response['ETag']=object_etag(parts)
    class ShortReads(io.BytesIO):
        def read(self,n): return super().read(min(n,40001))
    response['Body']=ShortReads(data)
    assert download(SimpleNamespace(get_object=lambda **kw:response),'private',source,tmp_path/'source')==hashlib.sha256(data).hexdigest()


def test_repeated_cancellation_waits_for_sdk_writer():
    async def check():
        entered,release,finished=threading.Event(),threading.Event(),threading.Event()
        def write(): entered.set(); release.wait(5); finished.set()
        task=asyncio.create_task(blocking(write))
        await asyncio.to_thread(entered.wait,5)
        task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished.is_set()
    asyncio.run(check())
