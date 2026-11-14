"""Verify managed JWTs; browser sessions and probe tokens are not API identity."""
import asyncio
import time
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


class TokenVerifier:
    def __init__(self, base_url: str, jwks_url: str, client: httpx.AsyncClient):
        base, keys = urlsplit(base_url), urlsplit(jwks_url)
        if base.scheme != 'https' or not base.hostname or keys.scheme != 'https' or keys.netloc != base.netloc:
            raise ValueError('Invalid managed Auth endpoint configuration')
        self.issuer = f'{base.scheme}://{base.netloc}'
        self.audience = self.issuer
        self.url, self.client = jwks_url, client
        self.keys: dict[str, object] = {}
        self.expires = 0.0
        self.retry_after = 0.0
        self.lock = asyncio.Lock()

    async def verify(self, token: str) -> dict:
        if len(token) > 16384:
            raise ValueError('Invalid token')
        header = jwt.get_unverified_header(token)
        kid = header.get('kid')
        if header.get('alg') != 'EdDSA' or not isinstance(kid, str) or not 1 <= len(kid) <= 256:
            raise ValueError('Invalid token header')
        async with self.lock:
            now = time.monotonic()
            if now >= self.expires or kid not in self.keys:
                if now < self.retry_after:
                    raise ValueError('Verification unavailable')
                self.retry_after = now + 10  # Bound unknown-kid and outage refetches.
                async with self.client.stream('GET', self.url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise ValueError('JWKS too large')
                import json
                entries = json.loads(body).get('keys', [])
                if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
                    raise ValueError('Invalid JWKS')
                keys = {}
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError('Invalid JWKS entry')
                    if (entry.get('kty') == 'OKP' and entry.get('crv') == 'Ed25519'
                            and entry.get('use', 'sig') == 'sig' and entry.get('alg', 'EdDSA') == 'EdDSA'
                            and isinstance(entry.get('kid'), str)):
                        if entry['kid'] in keys:
                            raise ValueError('Duplicate key id')
                        keys[entry['kid']] = jwt.PyJWK.from_dict(entry, algorithm='EdDSA').key
                self.keys, self.expires = keys, time.monotonic() + 300
            key = self.keys.get(kid)
        if key is None:
            raise ValueError('Unknown key')
        claims = jwt.decode(token, key, algorithms=['EdDSA'], issuer=self.issuer,
                            audience=self.audience, options={'require': ['iss', 'aud', 'exp', 'iat', 'sub']})
        subject = claims['sub']
        if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
                or claims.get('banned') is True or claims.get('role') != 'authenticated'):
            raise ValueError('Invalid subject')
        return claims


async def verified_identity(request: Request, proof: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    verifier = getattr(request.app.state, 'verifier', None)
    try:
        if proof is None or verifier is None or proof.scheme.lower() != 'bearer':
            raise ValueError('Missing identity')
        return await verifier.verify(proof.credentials)
    except (ValueError, TypeError, KeyError, jwt.PyJWTError, httpx.HTTPError):
        raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate': 'Bearer'}) from None
