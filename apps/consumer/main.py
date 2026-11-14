"""Dedicated Phase 0 process polling durable PostgreSQL jobs."""
import argparse
import asyncio
import signal
from dataclasses import replace
from uuid import uuid4
from deadline_crush_spikes import db, jobs
from deadline_crush_spikes.settings import Settings


async def run(once=False, interval=2):
    settings = Settings.load()
    pool = await db.create_pool(replace(settings, DB_POOL_MIN=settings.WORKER_DB_POOL_MIN,
                                        DB_POOL_MAX=settings.WORKER_DB_POOL_MAX))
    if pool is None:
        raise RuntimeError('DATABASE_URL is required')
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    owner = uuid4()
    print('Phase 0 PostgreSQL worker started', flush=True)
    try:
        while not stop.is_set():
            try:
                worked = await jobs.process_once(pool, owner)
            except Exception:
                print('Job dependency failed; retrying', flush=True)
                worked = False
                if once:
                    raise RuntimeError('Job dependency failed') from None
            if once:
                return
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                except TimeoutError:
                    pass
    finally:
        await db.close_pool(pool)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Check imports/config without connecting')
    parser.add_argument('--once', action='store_true', help='Claim one disposable probe job')
    args = parser.parse_args()
    try:
        if args.check:
            Settings.load()
            print('Phase 0 worker entrypoint ready; database not verified')
        else:
            asyncio.run(run(once=args.once))
    except Exception:
        raise SystemExit('Worker unavailable; check private configuration') from None
