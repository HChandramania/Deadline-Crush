"""Synchronous boto3 control plane; upload bytes never enter FastAPI."""
import boto3
from botocore.config import Config


def create_client(settings):
    if not all((settings.R2_ACCOUNT_ID, settings.R2_BUCKET,
                settings.R2_ACCESS_KEY_ID, settings.R2_SECRET_ACCESS_KEY)):
        return None
    return boto3.client('s3', endpoint_url=f'https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=settings.R2_ACCESS_KEY_ID, aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name='auto', config=Config(signature_version='s3v4', connect_timeout=5,
            read_timeout=20, max_pool_connections=5, retries={'total_max_attempts': 1, 'mode': 'standard'},
            request_checksum_calculation='when_required', response_checksum_validation='when_required'))


def operation(client, bucket, body):
    if client is None:
        raise RuntimeError('R2 not configured')
    params = {'Bucket': bucket, 'Key': body.key}
    name = body.operation
    if name == 'create':
        return {'upload_id': client.create_multipart_upload(**params)['UploadId']}
    if name == 'sign':
        return {'url': client.generate_presigned_url('upload_part',
            Params={**params, 'UploadId': body.upload_id, 'PartNumber': 1}, ExpiresIn=120)}
    if name == 'list':
        parts = []
        for page in client.get_paginator('list_parts').paginate(**params, UploadId=body.upload_id):
            parts.extend({'part_number': p['PartNumber'], 'etag': p['ETag'], 'size': p['Size']} for p in page.get('Parts', []))
        return {'parts': parts}
    if name == 'complete':
        client.complete_multipart_upload(**params, UploadId=body.upload_id,
            MultipartUpload={'Parts': [{'PartNumber': p.part_number, 'ETag': p.etag} for p in body.manifest]})
    elif name == 'abort':
        client.abort_multipart_upload(**params, UploadId=body.upload_id)
    elif name == 'head':
        value = client.head_object(**params)
        return {'size': value['ContentLength'], 'etag': value['ETag']}
    elif name == 'delete':
        client.delete_object(**params)
    else:
        raise ValueError('Unknown operation')
    return {'status': 'ok'}
