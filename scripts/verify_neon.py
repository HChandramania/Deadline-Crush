"""Opt-in real Neon setup and API/worker proof. Never print connection strings."""
import asyncio
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import time
from uuid import uuid4
import asyncpg
import httpx
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings


async def setup():
    settings = Settings.load()
    if not settings.DATABASE_URL:
        raise RuntimeError('Run neon deploy to populate database environment variables')
    direct = os.environ.get('DATABASE_URL_UNPOOLED')
    if not direct:
        raise RuntimeError('DATABASE_URL_UNPOOLED is required for schema setup')
    connection = await asyncpg.connect(direct, ssl=ssl.create_default_context(), timeout=15)
    try:
        # Versioned probe SQL, no application/submission schema.
        async with connection.transaction():
            await connection.execute(Path('infra/phase0.sql').read_text())
            if await connection.fetchval('SELECT count(*) FROM phase0_jobs'):
                raise RuntimeError('Phase 0 job table must be empty before isolated verification')
    finally:
        await connection.close()
    pool = await db.create_pool(settings)
    try:
        assert (await db.probe(pool))['status'] == 'passed'
    finally:
        await db.close_pool(pool)
    print('Neon schema and pooled transaction probe: PASS', flush=True)


async def cleanup(event):
    pool = await db.create_pool(Settings.load())
    try:
        async with pool.acquire(timeout=10) as connection:
            await connection.execute('DELETE FROM phase0_jobs WHERE id=$1', event)
    finally:
        await db.close_pool(pool)


def verify():
    asyncio.run(setup())
    token = os.environ.get('PROBE_TOKEN') or secrets.token_urlsafe(32)
    env = {**os.environ, 'PROBE_TOKEN': token}
    event = uuid4()
    server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--app-dir', 'apps/api',
        '--host', '127.0.0.1', '--port', '8788', '--no-access-log'],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url='http://127.0.0.1:8788', headers={'x-probe-token': token}, timeout=30) as client:
            for _ in range(50):
                if server.poll() is not None:
                    raise RuntimeError('API startup failed')
                try:
                    if client.get('/health').status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError('API startup timeout')
            response = client.get('/health/dependencies')
            assert response.status_code == 200
            assert response.json()['postgres']['status'] == 'passed'
            print('HTTP /health and /health/dependencies -> real Neon: PASS', flush=True)
            for _ in range(2):
                assert client.post('/probe/jobs', json={'id': str(event)}).status_code == 200
            for _ in range(2):
                result = subprocess.run([sys.executable, 'apps/consumer/main.py', '--once'],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45)
                assert result.returncode == 0
            result = client.get('/probe/jobs/' + str(event))
            assert result.status_code == 200
            assert result.json()['status'] == 'SUCCEEDED'
            assert result.json()['attempts'] == 2
            print('API duplicate enqueue -> PostgreSQL -> separate worker retry -> SUCCEEDED (attempts=2): PASS', flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        asyncio.run(cleanup(event))


if __name__ == '__main__':
    try:
        verify()
    except Exception as exc:
        # No SDK traceback or URL-bearing exception messages in shared output.
        print(f'Neon end-to-end verification: FAILED ({type(exc).__name__}); inspect private configuration', flush=True)
        raise SystemExit(1) from None
