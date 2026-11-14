"""Phase 0.3 orchestration; report only safe, bounded infrastructure evidence."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path('tests').resolve()))
from live_support import isolated_pool
from deadline_crush_spikes.settings import Settings

REPORT = {'commands': [], 'checks': {}, 'contracts': {}}


def audit():
    for filename in ('AGENTS.md', 'PRD.md', 'TRD.md'):
        REPORT['contracts'][filename] = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    names = ('DATABASE_URL', 'DATABASE_URL_UNPOOLED', 'PROBE_TOKEN', 'R2_ACCOUNT_ID',
             'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')
    missing = [name for name in names if not os.environ.get(name) or os.environ[name].startswith('replace-')]
    if missing:
        raise RuntimeError('Missing required settings: ' + ', '.join(missing))
    settings = Settings.load()
    for name, pooled in [('DATABASE_URL', True), ('DATABASE_URL_UNPOOLED', False)]:
        url = urlsplit(os.environ[name])
        if url.scheme not in ('postgres', 'postgresql') or not url.username or not url.password or not url.path.strip('/'):
            raise RuntimeError(name + ' has invalid URL structure')
        if not url.hostname or not url.hostname.endswith('.neon.tech') or ('-pooler.' in url.hostname) != pooled:
            raise RuntimeError(name + ' has incorrect Neon pooling endpoint')
        if parse_qs(url.query).get('sslmode', ['require'])[0] not in ('require', 'verify-ca', 'verify-full'):
            raise RuntimeError(name + ' disables TLS')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', settings.R2_ACCOUNT_ID):
        raise RuntimeError('R2_ACCOUNT_ID format invalid')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', settings.R2_BUCKET):
        raise RuntimeError('R2_BUCKET format invalid')
    from storage import create_client
    client = create_client(settings)
    try:
        endpoint = urlsplit(client.meta.endpoint_url)
        if endpoint.scheme != 'https' or not endpoint.hostname.endswith('.r2.cloudflarestorage.com'):
            raise RuntimeError('R2 endpoint invalid')
    finally:
        client.close()
    ignored = subprocess.run(['git', 'check-ignore', '.env.local', '.neon'], capture_output=True, text=True)
    if '.env.local' not in ignored.stdout:
        raise RuntimeError('Local secret file is not gitignored')
    if any(name.startswith(('VITE_DATABASE', 'VITE_R2_SECRET', 'VITE_R2_ACCESS', 'VITE_PROBE_TOKEN')) for name in os.environ):
        raise RuntimeError('Server credential name configured as browser environment variable')
    REPORT['checks']['environment'] = 'PASS: all required keys present, formats/pooling/TLS/endpoint/ignore checks passed; values omitted'
    print(REPORT['checks']['environment'], flush=True)
    return settings


def command(args, env):
    REPORT['commands'].append(args)
    # --tb=no avoids URLs/credentials in assertion failure diagnostics.
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    print(result.stdout, end='', flush=True)
    if result.returncode:
        # stderr may contain SDK request URLs; do not print it.
        raise RuntimeError('Verification command failed (exit ' + str(result.returncode) + ')')


async def main(infrastructure_only=False):
    settings = audit()
    async with isolated_pool(settings) as pool:
        REPORT['isolation_schema'] = pool.schema
        port_socket = socket.socket()
        port_socket.bind(('127.0.0.1', 0))
        port = port_socket.getsockname()[1]
        port_socket.close()
        env = {**os.environ, 'PHASE03_SCHEMA': pool.schema, 'PHASE03_PORT': str(port),
               'PHASE0_URL': f'http://127.0.0.1:{port}', 'PHASE03_LIVE': '1'}
        server = subprocess.Popen([sys.executable, 'scripts/phase03_child.py', 'api'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import httpx
        workers = []
        try:
            with httpx.Client(base_url=env['PHASE0_URL'], headers={'x-probe-token': settings.PROBE_TOKEN}, timeout=30) as client:
                for _ in range(60):
                    if server.poll() is not None:
                        raise RuntimeError('Isolated API startup failed')
                    try:
                        if client.get('/health').status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError('Isolated API startup timeout')
                response = client.get('/health/dependencies')
                if response.status_code != 200 or response.json()['postgres']['status'] != 'passed':
                    raise RuntimeError('API dependency probe failed')
                REPORT['checks']['api'] = 'PASS: local Uvicorn health and real Neon dependency probe'
            # Run the full existing suite and extra direct live tests before launching pollers.
            selected = ['tests/test_live_phase03.py','tests/test_remote.py'] if infrastructure_only else []
            command([sys.executable, '-m', 'pytest', '-q', '--tb=no', *selected,
                     '--deselect=tests/test_remote.py::test_remote_jobs'], env)
            # This marker lives only in the disposable schema used by API and workers.
            import uuid
            event = uuid.uuid4()
            from deadline_crush_spikes import jobs
            await jobs.enqueue(pool, event)
            for _ in range(2):
                workers.append(subprocess.Popen([sys.executable, 'scripts/phase03_child.py', 'worker'],
                    env={**env, 'PHASE03_DISCONNECT': '1'}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True))
            command([sys.executable, '-m', 'pytest', 'tests/test_remote.py::test_remote_jobs', '-q', '--tb=no'], env)
            for worker in workers:
                output, _ = worker.communicate(timeout=45)
                print(output, end='', flush=True)
                if worker.returncode or 'Worker graceful shutdown and pool close: PASS' not in output or 'Job dependency failed; retrying' not in output:
                    raise RuntimeError('Worker recovery/shutdown verification failed')
            async with pool.acquire() as connection:
                row = await connection.fetchrow('SELECT status,attempts FROM phase0_jobs WHERE id=$1', event)
            if row['status'] != 'SUCCEEDED' or row['attempts'] != 2:
                raise RuntimeError('Concurrent process outcome incorrect')
            REPORT['checks']['workers'] = 'PASS: two separate processes, injected client disconnect, reconnect/retry, one marker outcome, signal shutdown, pools closed'
            k6env = {**env, 'BASE_URL': env['PHASE0_URL']}
            command(['k6', 'run', 'tests/health.js'], k6env)
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.wait()
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    REPORT['checks']['cleanup'] = 'PASS: disposable schema dropped; no production job rows touched'


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--infrastructure-only',action='store_true',help='Run live probe/worker/k6 checks after a separate full backend regression run')
    args=parser.parse_args()
    REPORT['infrastructure_only']=args.infrastructure_only
    try:
        asyncio.run(main(args.infrastructure_only))
    except Exception as exc:
        REPORT['failure'] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print('Phase 0.3 FAILED: ' + REPORT['failure'], flush=True)
        code = 1
    else:
        code = 0
        print('Phase 0.3 live verification: PASS', flush=True)
    Path('docs/phase03-results.json').write_text(json.dumps(REPORT, indent=2) + '\n')
    raise SystemExit(code)
