"""Disposable local product integration environment. Never initializes production."""
import asyncio
import json
import os
import re
import signal
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import uvicorn
from deadline_crush_spikes.migrations import apply
from deadline_crush_spikes.settings import Settings
from storage import create_client


async def docker(*args):
    process = await asyncio.create_subprocess_exec('docker', *args, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
    if await process.wait():
        raise RuntimeError('LOCAL_WORKER_START_FAILED')


async def run():
    schema = 'phase1_' + uuid4().hex
    assert re.fullmatch(r'phase1_[a-f0-9]{32}', schema)
    origin = urlsplit(os.environ.get('PHASE0_URL', 'http://127.0.0.1:8787'))
    if origin.scheme != 'http' or origin.hostname not in ('127.0.0.1', 'localhost') or not origin.port:
        raise ValueError('Configured local probe API origin must be explicit loopback HTTP with a port')
    os.environ['APP_SCHEMA'] = schema
    os.environ['CORS_ORIGINS'] = 'http://localhost:5173'
    os.environ['PROBE_TOKEN'] = ''
    # Reserve an available port before any fixture creation; never replace a listener.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(2048)
    api_origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    settings = Settings.load()
    admin = await asyncpg.connect(os.environ['DATABASE_URL_UNPOOLED'], ssl=ssl.create_default_context(), timeout=15)
    names = []
    r2 = create_client(settings)
    try:
        await apply(admin, schema)
        async with admin.transaction():
            await admin.execute(f'SET LOCAL search_path TO "{schema}",pg_catalog')
            metadata_path = Path('tmp/phase5-local.json')
            event = UUID(json.loads(metadata_path.read_text())['event_id']) if metadata_path.exists() else uuid4()
            await admin.execute("INSERT INTO hackathons(id,name,deadline_at) VALUES($1,'Phase 5 isolated browser fixture',clock_timestamp()+interval '2 hours')", event)
        metadata = {'schema': schema, 'event_id': str(event), 'api_origin': api_origin, 'frontend_origin': 'http://localhost:5173'}
        Path('tmp/phase5-local.json').write_text(json.dumps(metadata, indent=2))
        env_names = ['DATABASE_URL', 'APP_SCHEMA', 'R2_ACCOUNT_ID', 'R2_BUCKET', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY']
        for suffix, flags in [('dispatcher', ['--dispatch']), ('validation', [])]:
            name = 'phase5-' + suffix + '-' + schema[-8:]
            await docker('run', '--rm', '-d', '--name', name, '--memory', '1g', '--cpus', '1', '--pids-limit', '64',
                         '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                         '--tmpfs', '/tmp:rw,noexec,nosuid,size=256m',
                         *[item for key in env_names for item in ('-e', key)], 'deadline-crush-phase5-offline',
                         'python', 'apps/consumer/validation_main.py', *flags)
            names.append(name)
        print('Reserved isolated Phase 5 API:', api_origin, flush=True)
        print('Isolated schema and event recorded in tmp/phase5-local.json; no production schema changes.', flush=True)
        server = uvicorn.Server(uvicorn.Config('main:app', access_log=False, log_level='warning'))
        stop_file = Path('tmp') / (schema + '.stop')
        async def watch_stop():
            while not server.should_exit:
                if stop_file.exists():
                    server.should_exit = True
                    return
                await asyncio.sleep(.5)
        monitor = asyncio.create_task(watch_stop())
        try:
            await server.serve(sockets=[listener])
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            stop_file.unlink(missing_ok=True)
            listener.close()
    finally:
        for name in names:
            await docker('stop', '--time', '20', name)
        # Exact fixture-owned keys only; stop consumers before inventory/deletion.
        async with admin.transaction():
            await admin.execute(f'SET LOCAL search_path TO "{schema}",pg_catalog')
            sessions = await admin.fetch('SELECT object_key,r2_upload_id,status FROM upload_sessions')
            previews = await admin.fetch('SELECT object_key FROM validation_preview_intents')
        if r2:
            from botocore.exceptions import ClientError
            for row in sessions:
                if row['r2_upload_id'] and row['status'] != 'COMPLETED':
                    try:
                        await asyncio.to_thread(r2.abort_multipart_upload, Bucket=settings.R2_BUCKET, Key=row['object_key'], UploadId=row['r2_upload_id'])
                    except ClientError as exc:
                        if exc.response['Error']['Code'] != 'NoSuchUpload': raise
                await asyncio.to_thread(r2.delete_object, Bucket=settings.R2_BUCKET, Key=row['object_key'])
            for row in previews:
                await asyncio.to_thread(r2.delete_object, Bucket=settings.R2_BUCKET, Key=row['object_key'])
            r2.close()
        assert re.fullmatch(r'phase1_[a-f0-9]{32}', schema)
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
        print('Disposable Phase 5 schema and its exact storage keys cleaned.', flush=True)


if __name__ == '__main__':
    try: asyncio.run(run())
    except KeyboardInterrupt: pass
    except Exception as exc: raise SystemExit('Local integration harness failed: '+type(exc).__name__) from None
