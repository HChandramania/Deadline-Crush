"""Bounded R2 control plane. No participant bytes or SDK errors leave this adapter."""
import asyncio
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

from botocore.exceptions import ClientError
from submissions import ObjectEvidence, StoredPart, SubmissionError, fingerprint


def etag_bytes(value):
    if not re.fullmatch(r'"[a-fA-F0-9]{32}"', value):
        raise SubmissionError('INVALID_ETAG')
    return bytes.fromhex(value[1:-1])


def object_etag(manifest):
    # R2's documented multipart identity, NOT a full-file SHA-256.
    digest = hashlib.md5(b''.join(etag_bytes(p.etag) for p in manifest), usedforsecurity=False).hexdigest()
    return f'"{digest}-{len(manifest)}"'


class R2Storage:
    def __init__(self, client, bucket, concurrency=5):
        self.client, self.bucket = client, bucket
        self.executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='r2-control')
        self.slots = asyncio.Semaphore(concurrency)

    async def call(self, method, **kwargs):
        # Acquire before submitting: executor queue cannot grow with request count.
        await self.slots.acquire()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, lambda: getattr(self.client, method)(**kwargs))
        except BaseException:
            self.slots.release()
            raise
        def finished(value):
            # The worker, not a cancelled HTTP task, owns the concurrency slot.
            self.slots.release()
            if not value.cancelled():
                value.exception()  # Consume failures after HTTP cancellation.
        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code in ('NoSuchUpload', '404', 'NoSuchKey', 'NotFound'):
                raise SubmissionError('STORAGE_NOT_FOUND', 404) from None
            raise SubmissionError('STORAGE_UNAVAILABLE', 503) from None
        except Exception:
            raise SubmissionError('STORAGE_UNAVAILABLE', 503) from None

    async def close(self):
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)

    def params(self, upload):
        return {'Bucket': self.bucket, 'Key': upload.object_key}

    def metadata(self, upload):
        return {'session-id': str(upload.session_id), 'version-id': str(upload.version_id),
                'authorization': fingerprint({'key': upload.object_key, 'size': upload.expected_size,
                    'parts': upload.expected_parts, 'part_size': upload.part_size})}

    async def create(self, upload, content_type):
        result = await self.call('create_multipart_upload', **self.params(upload),
                                 ContentType=content_type, Metadata=self.metadata(upload))
        return result['UploadId']

    async def sign(self, upload, numbers, seconds):
        return [await self.call('generate_presigned_url', ClientMethod='upload_part',
            Params={**self.params(upload), 'UploadId': upload.r2_upload_id, 'PartNumber': n},
            ExpiresIn=seconds, HttpMethod='PUT') for n in numbers]

    async def parts(self, upload):
        result, marker = [], 0
        # Authorized maximum is 13 parts; tolerate pagination but never unbounded.
        for _ in range(14):
            page = await self.call('list_parts', **self.params(upload), UploadId=upload.r2_upload_id,
                                   PartNumberMarker=marker, MaxParts=100)
            for part in page.get('Parts', []):
                etag_bytes(part['ETag'])
                result.append(StoredPart(part['PartNumber'], part['ETag'], part['Size']))
            if len(result) > upload.expected_parts:
                raise SubmissionError('PARTS_MISMATCH')
            if not page.get('IsTruncated'):
                numbers = [p.part_number for p in result]
                if numbers != sorted(set(numbers)) or any(n<1 or n>upload.expected_parts for n in numbers):
                    raise SubmissionError('PARTS_MISMATCH')
                return tuple(result)
            next_marker = int(page.get('NextPartNumberMarker', 0))
            if next_marker <= marker:
                break
            marker = next_marker
        raise SubmissionError('PARTS_MISMATCH')

    async def head(self, upload, manifest):
        try:
            value = await self.call('head_object', **self.params(upload))
        except SubmissionError as exc:
            if exc.code == 'STORAGE_NOT_FOUND':
                return None
            raise
        if (value.get('ContentLength') != upload.expected_size or
                value.get('Metadata') != self.metadata(upload) or
                value.get('ETag') != object_etag(manifest)):
            raise SubmissionError('STORAGE_EVIDENCE_MISMATCH')
        return ObjectEvidence(upload.session_id, upload.object_key, upload.expected_size,
                              fingerprint([p.model_dump() for p in manifest]))

    async def completed_object(self, upload, manifest):
        evidence = await self.head(upload, manifest)
        if evidence:
            return evidence
        try:
            observed = await self.parts(upload)
        except SubmissionError as exc:
            if exc.code == 'STORAGE_NOT_FOUND':
                # Another fenced/idempotent executor may have completed between
                # the first HEAD and ListParts. Independently verify the object.
                evidence = await self.head(upload, manifest)
                if evidence:
                    return evidence
            raise
        expected = tuple(StoredPart(p.part_number, p.etag,
            min(upload.part_size, upload.expected_size-(p.part_number-1)*upload.part_size)) for p in manifest)
        if len(manifest) != upload.expected_parts or observed != expected:
            raise SubmissionError('PARTS_MISMATCH')
        try:
            await self.call('complete_multipart_upload', **self.params(upload), UploadId=upload.r2_upload_id,
                MultipartUpload={'Parts': [{'PartNumber': p.part_number, 'ETag': p.etag} for p in manifest]})
        except SubmissionError:
            # Includes response loss and NoSuchUpload. Neither proves success.
            evidence = await self.head(upload, manifest)
            if evidence:
                return evidence
            raise
        return await self.head(upload, manifest)

    async def abort(self, key, upload_id):
        try:
            await self.call('abort_multipart_upload', Bucket=self.bucket, Key=key, UploadId=upload_id)
        except SubmissionError as exc:
            if exc.code != 'STORAGE_NOT_FOUND':
                raise

    async def sweep_key(self, key):
        # Exact DB-authorized key only. No bucket-wide scan or object deletion.
        key_marker = upload_marker = None
        for _ in range(10):
            params = {'Bucket': self.bucket, 'Prefix': key, 'MaxUploads': 100}
            if key_marker:
                params.update(KeyMarker=key_marker, UploadIdMarker=upload_marker)
            page = await self.call('list_multipart_uploads', **params)
            for item in page.get('Uploads', []):
                if item['Key'] == key:
                    await self.abort(key, item['UploadId'])
            if not page.get('IsTruncated'):
                return
            next_key, next_upload = page.get('NextKeyMarker'), page.get('NextUploadIdMarker')
            if (next_key, next_upload) == (key_marker, upload_marker):
                break
            key_marker, upload_marker = next_key, next_upload
        raise SubmissionError('CLEANUP_PAGE_LIMIT', 503)
