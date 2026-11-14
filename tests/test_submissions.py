"""Deterministic contract tests; no live storage claims."""
import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from submissions import CompletionInput, VersionInput, UnavailableStorage, SubmissionError, canonical, fingerprint


PAYLOAD = {'title': 'Project', 'description': 'Description', 'links': ['https://example.com'],
           'filename': 'project.zip', 'size_bytes': 7340032, 'content_type': 'application/zip'}


def test_canonical_request_is_stable_and_unicode_preserving():
    assert fingerprint(PAYLOAD) == fingerprint(dict(reversed(list(PAYLOAD.items()))))
    assert canonical({'title': 'é'}) == '{"title":"é"}'
    assert fingerprint({**PAYLOAD, 'title': 'Changed'}) != fingerprint(PAYLOAD)


@pytest.mark.parametrize('extra', ['version_no', 'admitted_at', 'deadline_at', 'role', 'uploaded', 'team_id'])
def test_untrusted_admission_fields_rejected(extra):
    with pytest.raises(ValidationError):
        VersionInput(**PAYLOAD, **{extra: 'forged'})


@pytest.mark.parametrize('size', [0, -1, True, '100'])
def test_invalid_file_size_rejected(size):
    with pytest.raises(ValidationError):
        VersionInput(**{**PAYLOAD, 'size_bytes': size})


@pytest.mark.parametrize('parts', [[], [{'part_number': 2, 'etag': 'a'}],
    [{'part_number': 1, 'etag': 'a'}, {'part_number': 1, 'etag': 'a'}],
    [{'part_number': 1, 'etag': ''}], [{'part_number': True, 'etag': 'a'}]])
def test_invalid_completion_manifests_rejected(parts):
    with pytest.raises(ValidationError):
        CompletionInput(parts=parts)


def test_forged_completion_flag_rejected():
    with pytest.raises(ValidationError):
        CompletionInput(parts=[{'part_number': 1, 'etag': 'a'}], uploaded=True)


def test_default_storage_fails_closed():
    async def check():
        for method, args in [(UnavailableStorage().parts, (None,)),
                             (UnavailableStorage().completed_object, (None, ()) )]:
            with pytest.raises(SubmissionError) as exc:
                await method(*args)
            assert exc.value.status_code == 503
    asyncio.run(check())


@pytest.mark.parametrize('method,path,body', [
    ('post', '/submissions/versions', PAYLOAD),
    ('post', '/uploads/'+str(uuid4())+'/complete', {'parts': [{'part_number': 1, 'etag': 'a'}]}),
    ('get', '/submissions/current', None), ('get', '/submissions/versions', None)])
def test_every_submission_route_requires_managed_identity(method, path, body):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.request(method, '/api/v1'+path, params={'team_id': str(uuid4())},
                                  json=body, headers={'Idempotency-Key': str(uuid4()), 'x-probe-token': 'not-participant-auth'})
        assert response.status_code == 401


def test_openapi_exposes_team_selector_and_strict_bodies():
    schema = app.openapi()
    for path, method in [('/api/v1/submissions/versions','post'), ('/api/v1/submissions/versions','get'),
                         ('/api/v1/submissions/current','get'), ('/api/v1/uploads/{session_id}/complete','post')]:
        assert any(p['name']=='team_id' and p['required'] for p in schema['paths'][path][method]['parameters'])
    assert schema['components']['schemas']['VersionInput']['additionalProperties'] is False
