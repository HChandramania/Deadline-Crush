"""Explicit opt-in live checks; ephemeral schemas/keys, no production data."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
import httpx
import pytest
from botocore.exceptions import ClientError
from deadline_crush_spikes import db, jobs
from deadline_crush_spikes.settings import Settings
from live_support import isolated_pool
from storage import create_client

pytestmark = pytest.mark.skipif(os.environ.get('PHASE03_LIVE') != '1', reason='explicit Phase 0.3 live runner required')


def test_commit_rollback_timestamp_bounds_and_concurrent_claim():
    async def check():
        settings = Settings.load()
        async with isolated_pool(settings) as pool:
            assert pool.pool.get_max_size() == settings.DB_POOL_MAX
            event, owner, other = uuid4(), uuid4(), uuid4()
            async with pool.acquire() as connection:
                assert await connection.fetchval('SELECT 1') == 1
                assert await connection.fetchval('SELECT $1::text', "a'; --") == "a'; --"
                before = await connection.fetchval('SELECT clock_timestamp()')
            await jobs.enqueue(pool, event)  # Autocommit survives another connection.
            async with pool.acquire() as connection:
                assert await connection.fetchval('SELECT count(*) FROM phase0_jobs WHERE id=$1', event) == 1
                try:
                    async with connection.transaction():
                        await connection.execute('UPDATE phase0_jobs SET attempts=99 WHERE id=$1', event)
                        raise ValueError('rollback')
                except ValueError:
                    pass
                assert await connection.fetchval('SELECT attempts FROM phase0_jobs WHERE id=$1', event) == 0
                assert await connection.fetchval('SELECT clock_timestamp()') >= before
            claims = await asyncio.gather(jobs.claim(pool, owner), jobs.claim(pool, other))
            assert sum(claim is not None for claim in claims) == 1
            # Release and reconnect a test-owned client, without terminating shared server sessions.
            async with pool.acquire() as connection:
                connection.terminate()
            async with pool.acquire() as connection:
                assert await connection.fetchval('SELECT 1') == 1
        assert pool.closed
    asyncio.run(check())


def test_r2_multipart_identity_abort_invalid_signature_cleanup():
    settings = Settings.load()
    client = create_client(settings)
    if client is None:
        pytest.fail('R2 credentials unavailable', pytrace=False)
    key = 'phase0/' + str(uuid4())
    args = {'Bucket': settings.R2_BUCKET, 'Key': key}
    upload = None
    completed = False
    evidence = {}

    def record(name, result):
        evidence[name] = result['ResponseMetadata']['HTTPStatusCode']
        return result

    def missing_object():
        try:
            client.head_object(**args)
        except ClientError as exc:
            return exc.response['ResponseMetadata']['HTTPStatusCode'] == 404
        return False

    try:
        nonce = uuid4().hex
        chunks = [b'A' * (8 * 1024 * 1024), b'phase03 final part']
        digest = hashlib.sha256(b''.join(chunks)).hexdigest()
        created = record('create', client.create_multipart_upload(**args, Metadata={'probe': nonce, 'sha256': digest}))
        upload = created['UploadId']
        manifest = []
        with httpx.Client(timeout=60) as direct:
            for number, chunk in enumerate(chunks, 1):
                url = client.generate_presigned_url('upload_part', Params={**args, 'UploadId': upload, 'PartNumber': number}, ExpiresIn=120)
                response = direct.put(url, content=chunk)
                evidence['upload_part_' + str(number)] = response.status_code
                if response.status_code != 200:
                    pytest.fail('Direct R2 UploadPart rejected', pytrace=False)
                manifest.append({'PartNumber': number, 'ETag': response.headers['etag']})
            # Tamper only the signature; do not log or expose the bearer URL.
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query))
            signature = query['X-Amz-Signature']
            query['X-Amz-Signature'] = ('0' if signature[0] != '0' else '1') + signature[1:]
            bad = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ''))
            rejected = direct.put(bad, content=b'invalid-signature')
            evidence['invalid_signature'] = rejected.status_code
            if rejected.status_code != 403:
                pytest.fail('R2 did not reject invalid signature with 403', pytrace=False)
            expired_url = client.generate_presigned_url('upload_part', Params={**args, 'UploadId': upload, 'PartNumber': 3}, ExpiresIn=1)
            time.sleep(3)
            expired = direct.put(expired_url, content=b'expired')
            evidence['expired_signature'] = expired.status_code
            if expired.status_code != 403:
                pytest.fail('R2 did not reject expired signature with 403', pytrace=False)
        listed = record('list', client.list_parts(**args, UploadId=upload))
        assert [{'PartNumber': p['PartNumber'], 'ETag': p['ETag']} for p in listed['Parts']] == manifest
        result = record('complete', client.complete_multipart_upload(**args, UploadId=upload, MultipartUpload={'Parts': manifest}))
        completed = True
        metadata = record('head', client.head_object(**args))
        assert metadata['ContentLength'] == sum(map(len, chunks))
        assert metadata['ETag'] == result['ETag']
        assert metadata['Metadata']['probe'] == nonce
        assert metadata['Metadata']['sha256'] == digest
        downloaded = record('get', client.get_object(**args))
        with downloaded['Body'] as stream:
            assert hashlib.sha256(stream.read()).hexdigest() == digest
        evidence['verified_size_bytes'] = metadata['ContentLength']
        record('delete', client.delete_object(**args))
        completed = False
        upload = None
        assert missing_object()
        evidence['head_after_delete'] = 404
        upload = record('create_for_abort', client.create_multipart_upload(**args))['UploadId']
        record('abort', client.abort_multipart_upload(**args, UploadId=upload))
        try:
            client.list_parts(**args, UploadId=upload)
        except ClientError as exc:
            evidence['list_after_abort'] = exc.response['ResponseMetadata']['HTTPStatusCode']
            assert exc.response['Error']['Code'] == 'NoSuchUpload'
        else:
            pytest.fail('Aborted upload still exists', pytrace=False)
        upload = None
        assert missing_object()
        evidence['head_after_abort'] = 404
    except ClientError as exc:
        pytest.fail('R2 API failure: ' + exc.response['Error']['Code'], pytrace=False)
    except httpx.HTTPError:
        pytest.fail('R2 HTTP transport failure (URL omitted)', pytrace=False)
    finally:
        try:
            if completed:
                client.delete_object(**args)
            elif upload:
                try:
                    client.abort_multipart_upload(**args, UploadId=upload)
                except ClientError as exc:
                    if exc.response['Error']['Code'] != 'NoSuchUpload':
                        raise
        finally:
            client.close()
            Path('docs/phase03-r2-results.json').write_text(json.dumps(evidence, indent=2) + '\n')
