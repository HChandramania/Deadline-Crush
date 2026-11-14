"""Opt-in real infrastructure checks. No credentials or URLs in test output."""
import os
import time
import uuid
import httpx
import pytest

URL = os.environ.get('PHASE0_URL')
TOKEN = os.environ.get('PROBE_TOKEN')
pytestmark = pytest.mark.skipif(not URL or not TOKEN, reason='remote URL/token unavailable')


@pytest.fixture
def client():
    with httpx.Client(base_url=URL, headers={'x-probe-token': TOKEN}, timeout=30) as value:
        yield value


def test_remote_database(client):
    assert client.get('/health').status_code == 200
    assert client.get('/health/dependencies').json()['postgres']['status'] == 'passed'


def test_remote_multipart(client):
    key = 'phase0/' + str(uuid.uuid4())
    uid = None
    completed = False
    def op(operation, **extra):
        response = client.post('/probe/r2', json={'operation': operation, 'key': key, 'upload_id': uid or '', **extra})
        assert response.status_code == 200, 'R2 control request failed'
        return response.json()
    try:
        uid = op('create')['upload_id']
        url = op('sign')['url']
        # Bytes go directly to R2, never to FastAPI. Avoid HTTP assertion introspection of URL.
        with httpx.Client(timeout=30) as storage:
            response = storage.put(url, content=b'phase0 direct chunk')
        if response.status_code != 200:
            pytest.fail('direct R2 PUT failed', pytrace=False)
        etag = response.headers['etag']
        assert any(p['etag'] == etag for p in op('list')['parts'])
        op('complete', manifest=[{'part_number': 1, 'etag': etag}])
        completed = True
        assert int(op('head')['size']) == len(b'phase0 direct chunk')
    finally:
        if completed:
            op('delete')
        elif uid:
            op('abort')
    uid = None
    uid = op('create')['upload_id']
    op('abort')


def test_remote_jobs(client):
    event = str(uuid.uuid4())
    for _ in range(2):
        response = client.post('/probe/jobs', json={'id': event})
        assert response.status_code == 200
    for _ in range(30):
        receipt = client.get('/probe/jobs/' + event)
        if receipt.status_code == 200 and receipt.json()['status'] == 'SUCCEEDED':
            break
        time.sleep(2)
    else:
        pytest.fail('PostgreSQL job completion not observed')
    assert receipt.json()['attempts'] == 2
