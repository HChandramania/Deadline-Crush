import asyncio
import io
import zipfile
import httpx
import pytest
from main import app
from storage import create_client, operation
from deadline_crush_spikes.settings import Settings
from main import R2Request
from validation import inspect_zip, pdf_probe


def zip_bytes(name='README.md', data=b'hello'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(name, data)
    return output.getvalue()


def test_zip_safe():
    assert inspect_zip(zip_bytes()) == {'entries': 1, 'expanded_bytes': 5}


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', '..\\escape', 'a.zip'])
def test_zip_paths(name):
    with pytest.raises(ValueError):
        inspect_zip(zip_bytes(name))


def test_zip_count():
    with pytest.raises(ValueError, match='entry limit'):
        inspect_zip(zip_bytes(), max_entries=0)


def test_zip_expanded():
    with pytest.raises(ValueError, match='expanded'):
        inspect_zip(zip_bytes(), max_expanded=1)


def test_zip_symlink():
    output = io.BytesIO()
    info = zipfile.ZipInfo('link')
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(info, 'target')
    with pytest.raises(ValueError, match='symlink'):
        inspect_zip(output.getvalue())


def test_bad_zip():
    with pytest.raises(zipfile.BadZipFile):
        inspect_zip(b'not zip')


def test_signing():
    settings = Settings(R2_ACCOUNT_ID='example', R2_BUCKET='bucket', R2_ACCESS_KEY_ID='test', R2_SECRET_ACCESS_KEY='test')
    client = create_client(settings)
    try:
        url = operation(client, 'bucket', R2Request(operation='sign', key='phase0/abc', upload_id='a+b/='))['url']
    finally:
        client.close()
    assert 'uploadId=a%2Bb%2F%3D' in url
    assert 'X-Amz-Signature=' in url and 'X-Amz-Expires=120' in url


def test_health_and_fail_closed():
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health')).json()['status'] == 'ok'
            assert (await client.get('/health/dependencies')).status_code == 403
            assert (await client.post('/probe/jobs', json={'id': '00000000-0000-0000-0000-000000000001'})).status_code == 403
    asyncio.run(check())


def test_exception_redaction(monkeypatch):
    from main import authorized
    async def fail(env):
        raise RuntimeError('secret-password')
    monkeypatch.setattr('deadline_crush_spikes.db.probe', fail)
    from types import SimpleNamespace
    app.dependency_overrides[authorized] = lambda: SimpleNamespace(pool=None)
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
            response = await client.get('/health/dependencies')
            assert response.status_code == 503
            assert 'secret-password' not in response.text
    try:
        asyncio.run(check())
    finally:
        app.dependency_overrides.clear()
