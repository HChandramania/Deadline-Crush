"""R2 streaming, child supervision and attempt-specific preview staging."""
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from botocore.exceptions import BotoCoreError, ClientError
from r2_uploads import object_etag
from submissions import UploadAuthorization, PartInput, fingerprint


class ProcessingFailure(Exception):
    pass


def download(client,bucket,source,path):
    size = source['expected_size']
    if not 0<size<=104857600:
        raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH')
    upload = UploadAuthorization(source['session_id'],source['id'],source['object_key'],
        source['r2_upload_id'],size,source['part_size'],source['expected_parts'])
    metadata = {'session-id':str(upload.session_id),'version-id':str(upload.version_id),
        'authorization':fingerprint({'key':upload.object_key,'size':size,'parts':upload.expected_parts,'part_size':upload.part_size})}
    manifest = [PartInput(**p) for p in json.loads(source['manifest'])]
    expected_etag = object_etag(manifest)
    body = None
    try:
        response = client.get_object(Bucket=bucket,Key=source['object_key'],IfMatch=expected_etag)
        body = response['Body']
        if (response.get('ContentLength')!=size or response.get('Metadata')!=metadata
                or response.get('ETag')!=expected_etag):
            raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH')
        digest,total,started = hashlib.sha256(),0,time.monotonic()
        part_hash,part_bytes,part_index = hashlib.md5(usedforsecurity=False),0,0
        with path.open('xb') as output:
            while chunk := body.read(65536):
                total+=len(chunk)
                if total>size or time.monotonic()-started>120:
                    raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH' if total>size else 'STORAGE_UNAVAILABLE')
                output.write(chunk)
                digest.update(chunk)
                remaining = memoryview(chunk)
                while remaining:
                    length = min(len(remaining),upload.part_size-part_bytes)
                    part_hash.update(remaining[:length])
                    remaining = remaining[length:]
                    part_bytes+=length
                    if part_bytes==upload.part_size:
                        if part_index>=len(manifest) or '"'+part_hash.hexdigest()+'"'!=manifest[part_index].etag:
                            raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH')
                        part_index+=1
                        part_hash,part_bytes = hashlib.md5(usedforsecurity=False),0
        if part_bytes:
            if part_index>=len(manifest) or '"'+part_hash.hexdigest()+'"'!=manifest[part_index].etag:
                raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH')
            part_index+=1
        if total!=size or part_index!=len(manifest) or (source['verified_sha256'] and digest.hexdigest()!=source['verified_sha256']):
            raise ProcessingFailure('SOURCE_IDENTITY_MISMATCH')
        return digest.hexdigest()
    except (ClientError,BotoCoreError,OSError):
        raise ProcessingFailure('STORAGE_UNAVAILABLE') from None
    finally:
        if body is not None:
            body.close()


async def blocking(function,*args,**kwargs):
    # A cancelled coroutine must not remove its temporary file while the SDK
    # thread is still writing it. Keep the owning task alive until it finishes.
    task = asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            if not cancelled:
                raise
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError()
    return task.result()


async def render(path,output,links,timeout=60):
    if sys.platform!='linux':
        raise ProcessingFailure('PROCESSING_UNAVAILABLE')
    link_path = output/'links.json'
    link_path.write_text(json.dumps(links),encoding='utf-8')
    child = Path(__file__).with_name('validation_child.py')
    # No credentials, PYTHONPATH injection, user site packages or SDK config.
    process = await asyncio.create_subprocess_exec(sys.executable,'-I',str(child),str(path),str(output),str(link_path),
        env={'PATH':os.defpath,'LANG':'C.UTF-8'},stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.wait(),timeout)
    except (TimeoutError,asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode!=0:
        raise ProcessingFailure('RENDERER_CRASH')
    result_path = output/'result.json'
    if not result_path.is_file() or result_path.stat().st_size>65536:
        raise ProcessingFailure('RENDERER_CRASH')
    return json.loads(result_path.read_text(encoding='utf-8'))


class Pipeline:
    def __init__(self,client,bucket,queue,concurrency=1):
        self.client,self.bucket = client,bucket
        self.queue = queue
        self.slots = asyncio.Semaphore(concurrency)

    async def process(self,source,job):
        async with self.slots:
            with TemporaryDirectory(prefix='deadline-validation-') as root:
                output = Path(root)
                path = output/'source'
                digest = await blocking(download,self.client,self.bucket,source,path)
                try:
                    result = await render(path,output,json.loads(source['links']))
                except TimeoutError:
                    # Time exhaustion is not proof of an actionable content defect.
                    raise ProcessingFailure('PROCESSING_UNAVAILABLE') from None
                if result.get('transient_error'):
                    raise ProcessingFailure(result['transient_error'])
                artifacts = []
                name = result.pop('preview',None)
                if name:
                    if name not in ('preview.html','preview.png'):
                        raise ProcessingFailure('RENDERER_CRASH')
                    data = (output/name).read_bytes()
                    if len(data)>262144:
                        raise ProcessingFailure('RENDERER_CRASH')
                    key = f"previews/{source['id']}/{job['id']}/{job['lease_token']}/{name}"
                    content_type = result.pop('content_type')
                    kind = result.pop('artifact_type')
                    if not await self.queue.stage(job,kind,key):
                        raise ProcessingFailure('PROCESSING_UNAVAILABLE')
                    await blocking(self.client.put_object,Bucket=self.bucket,Key=key,Body=data,
                        ContentType=content_type,CacheControl='private, no-store',
                        ContentDisposition='attachment',Metadata={'version-id':str(source['id']),
                            'job-id':str(job['id']),'generation':str(job['lease_token'])})
                    artifacts.append((kind,key))
                result.update({'source_sha256':digest,'job_id':str(job['id']),
                    'version_id':str(source['id']),'generation':job['lease_token']})
                return result,artifacts
