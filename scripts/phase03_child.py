"""Test-only subprocess launcher: isolate existing API/worker jobs by schema."""
import asyncio
import importlib.util
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path('tests').resolve()))
from live_support import Pool
from deadline_crush_spikes import db

original_create = db.create_pool
created = []


async def create(settings):
    pool = Pool(await original_create(settings), os.environ['PHASE03_SCHEMA'],
                disconnect=os.environ.get('PHASE03_DISCONNECT') == '1')
    created.append(pool)
    return pool


db.create_pool = create


async def worker():
    spec = importlib.util.spec_from_file_location('probe_worker', 'apps/consumer/main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    asyncio.get_running_loop().call_later(8, lambda: signal.raise_signal(signal.SIGTERM))
    await module.run(interval=0.2)
    if not created or not all(pool.closed for pool in created):
        raise RuntimeError('Worker pool was not closed')
    print('Worker graceful shutdown and pool close: PASS', flush=True)


if __name__ == '__main__':
    try:
        if sys.argv[1] == 'api':
            import uvicorn
            uvicorn.run('main:app', app_dir='apps/api', host='127.0.0.1',
                        port=int(os.environ['PHASE03_PORT']), access_log=False)
        else:
            asyncio.run(worker())
    except Exception as exc:
        print('Isolated child failed: ' + type(exc).__name__, flush=True)
        raise SystemExit(1) from None
