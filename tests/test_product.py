import asyncio
import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import product
from domain import Profile, current_profile
from main import app


@pytest.mark.parametrize('value', ['=1+1', '+SUM(A1)', '-1', '@test', '  =cmd', '\tformula', '\nvalue'])
def test_csv_formula_protection(value):
    assert product.csv_cell(value).startswith("'")


def test_csv_quoting_and_nulls():
    output = io.StringIO()
    csv.writer(output).writerow([product.csv_cell('a,"b\nc'), product.csv_cell(None)])
    assert next(csv.reader(io.StringIO(output.getvalue()))) == ['a,"b\nc', '']


def test_preview_bounded_and_closed():
    body = io.BytesIO(b'x' * 262145)
    with pytest.raises(Exception):
        product.read_preview(SimpleNamespace(get_object=lambda **kw: {'Body': body}), 'private', 'key')
    assert body.closed


def test_private_version_missing_fails_closed():
    async def check():
        class Connection:
            async def fetchrow(self, sql, *args):
                assert 'team_members' in sql
                assert args[2] is False
                return None
        with pytest.raises(Exception) as error:
            await product.visible_version(Connection(), uuid4(), SimpleNamespace(id=uuid4(), role='MEMBER'))
        assert error.value.status_code == 404
    asyncio.run(check())


@pytest.mark.parametrize('path', ['/organizer/submissions', '/organizer/export.csv', '/organizer/teams/'+str(uuid4())+'/versions'])
def test_organizer_endpoints_reject_members(path):
    async def check():
        profile = Profile(id=uuid4(), email='', role='MEMBER', created_at=datetime.now(timezone.utc))
        app.dependency_overrides[current_profile] = lambda: profile
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/api/v1'+path)
                assert response.status_code == 403
        finally:
            app.dependency_overrides.clear()
    asyncio.run(check())


def test_product_read_contracts_in_openapi():
    paths = app.openapi()['paths']
    for route in ['/hackathons', '/submissions/{version_id}/details', '/submissions/{version_id}/preview/{kind}',
                  '/submissions/{version_id}/download-url', '/organizer/submissions', '/organizer/export.csv']:
        assert 'get' in paths['/api/v1'+route]


def test_preview_headers_and_authorization(monkeypatch):
    async def check():
        profile = Profile(id=uuid4(), email='', role='MEMBER', created_at=datetime.now(timezone.utc))
        class Connection:
            async def fetchrow(self, *args): return {'id': uuid4()}
            async def fetchval(self, *args): return 'committed/private/preview'
        @asynccontextmanager
        async def tx(request): yield Connection()
        monkeypatch.setattr(product, 'transaction', tx)
        monkeypatch.setattr(product, 'read_preview', lambda *args: b'<script>bad()</script>')
        monkeypatch.setattr(app.state, 'storage', object(), raising=False)
        monkeypatch.setattr(app.state, 'settings', SimpleNamespace(R2_BUCKET='private'), raising=False)
        app.dependency_overrides[current_profile] = lambda: profile
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(f'/api/v1/submissions/{uuid4()}/preview/ZIP_PREVIEW')
                assert response.status_code == 200
                assert response.headers['cache-control'] == 'private, no-store'
                assert "sandbox; default-src 'none'" in response.headers['content-security-policy']
                assert response.headers['x-content-type-options'] == 'nosniff'
        finally: app.dependency_overrides.clear()
    asyncio.run(check())


def test_download_expiry_and_attachment_outside_transaction(monkeypatch):
    async def check():
        profile = Profile(id=uuid4(), email='', role='MEMBER', created_at=datetime.now(timezone.utc))
        inside = False
        class Connection:
            async def fetchrow(self, *args): return {'lifecycle': 'FINALIZED', 'object_key': 'opaque'}
        @asynccontextmanager
        async def tx(request):
            nonlocal inside
            inside = True
            try: yield Connection()
            finally: inside = False
        def sign(operation, **kwargs):
            assert not inside
            assert operation == 'get_object' and kwargs['ExpiresIn'] == 60
            assert kwargs['Params']['ResponseContentDisposition'] == 'attachment'
            return 'https://storage.invalid/test-capability'
        monkeypatch.setattr(product, 'transaction', tx)
        monkeypatch.setattr(app.state, 'storage', SimpleNamespace(generate_presigned_url=sign), raising=False)
        monkeypatch.setattr(app.state, 'settings', SimpleNamespace(R2_BUCKET='private'), raising=False)
        app.dependency_overrides[current_profile] = lambda: profile
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get(f'/api/v1/submissions/{uuid4()}/download-url')
                assert response.status_code == 200 and response.json()['expires_in_seconds'] == 60
                seconds = (datetime.fromisoformat(response.json()['expires_at'].replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds()
                assert 50 < seconds <= 60
                assert response.headers['cache-control'] == 'private, no-store'
        finally: app.dependency_overrides.clear()
    asyncio.run(check())
