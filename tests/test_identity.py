import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from main import app
from identity import TokenVerifier

ISSUER = 'https://branch.example.com'


def signing_material():
    private = Ed25519PrivateKey.generate()
    public = json.loads(jwt.algorithms.OKPAlgorithm.to_jwk(private.public_key()))
    public.update(kid='test-key', alg='EdDSA', use='sig')
    return private, public


def token(private, **changes):
    claims = dict(iss=ISSUER, aud=ISSUER, sub='opaque-not-a-uuid', role='authenticated', exp=int(time.time())+60, iat=int(time.time()))
    claims.update(changes)
    return jwt.encode(claims, private, algorithm='EdDSA', headers={'kid': 'test-key'})


@pytest.mark.parametrize('case', ['valid', 'expired', 'future_nbf', 'future_iat', 'wrong_issuer',
    'wrong_audience', 'signature', 'unsigned', 'malformed', 'missing_sub', 'empty_sub',
    'banned', 'anonymous', 'missing_exp', 'unknown_kid', 'wrong_algorithm', 'oversize'])
def test_token_verification(case):
    private, public = signing_material()
    changes = {'expired': {'exp': 1}, 'future_nbf': {'nbf': int(time.time())+3600},
        'future_iat': {'iat': int(time.time())+3600}, 'wrong_issuer': {'iss': 'https://other.example'},
        'wrong_audience': {'aud': 'other'}, 'empty_sub': {'sub': ''}, 'banned': {'banned': True}, 'anonymous': {'role':'anonymous'}}
    proof = token(private, **changes.get(case, {}))
    if case == 'signature': proof = token(Ed25519PrivateKey.generate())
    if case == 'unsigned': proof = jwt.encode({'sub': 'x'}, key=None, algorithm='none')
    if case == 'malformed': proof = 'not.a.jwt'
    if case == 'unknown_kid': proof = jwt.encode({'sub': 'x'}, private, algorithm='EdDSA', headers={'kid': 'other'})
    if case == 'wrong_algorithm': proof = jwt.encode({'sub': 'x'}, 'a'*64, algorithm='HS256', headers={'kid': 'test-key'})
    if case == 'oversize': proof = 'a'*17000
    if case in ('missing_exp','missing_sub'):
        claims = jwt.decode(proof, options={'verify_signature': False})
        del claims[case.removeprefix('missing_')]
        proof = jwt.encode(claims, private, algorithm='EdDSA', headers={'kid': 'test-key'})
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'keys':[public]}))) as client:
            verifier = TokenVerifier(ISSUER+'/auth', ISSUER+'/auth/jwks', client)
            if case == 'valid':
                assert (await verifier.verify(proof))['sub'] == 'opaque-not-a-uuid'
            else:
                with pytest.raises((ValueError, jwt.PyJWTError)):
                    await verifier.verify(proof)
    asyncio.run(check())


@pytest.mark.parametrize('status', [500, 503, 302])
def test_jwks_unavailable_fails_closed(status):
    private, _ = signing_material()
    async def check():
        calls = []
        def respond(request):
            calls.append(1)
            return httpx.Response(status)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            verifier = TokenVerifier(ISSUER+'/auth', ISSUER+'/jwks', client)
            with pytest.raises(httpx.HTTPError): await verifier.verify(token(private))
            with pytest.raises(ValueError): await verifier.verify(token(private))
            assert len(calls)==1
    asyncio.run(check())


def test_jwks_cache_and_rotation():
    private, public = signing_material()
    async def check():
        calls=[]
        def respond(request):
            calls.append(1); return httpx.Response(200,json={'keys':[public]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            verifier=TokenVerifier(ISSUER+'/auth',ISSUER+'/jwks',client)
            for _ in range(3): await verifier.verify(token(private))
            assert len(calls)==1
            verifier.expires=0; verifier.retry_after=0
            await verifier.verify(token(private))
            assert len(calls)==2
    asyncio.run(check())


@pytest.mark.parametrize('headers', [{}, {'x-probe-token':'test'}, {'Authorization':'Bearer malformed'}, {'Authorization':'Basic x'}])
def test_application_unauthorized(headers):
    with TestClient(app, raise_server_exceptions=False) as client:
        response=client.get('/api/v1/users/me',headers=headers)
        assert response.status_code==401
        assert response.json()['error']['code']=='UNAUTHORIZED'
        assert response.headers['www-authenticate']=='Bearer'


def test_cors_rejects_untrusted_origin():
    with TestClient(app) as client:
        response=client.options('/api/v1/teams', headers={'Origin':'https://evil.example', 'Access-Control-Request-Method':'POST'})
        assert response.status_code==400
        assert 'access-control-allow-origin' not in response.headers


def test_no_custom_auth_or_membership_mutations():
    paths=app.openapi()['paths']
    assert '/api/v1/login' not in paths and '/api/v1/signup' not in paths
    assert list(paths['/api/v1/teams/{team_id}/members']) == ['get']


@pytest.mark.parametrize('schema', ['public','neon_auth','x; DROP SCHEMA public','', 'phase1_bad'])
def test_schema_rejects_unsafe_names(schema):
    from deadline_crush_spikes.migrations import schema_name
    with pytest.raises(ValueError): schema_name(schema)
