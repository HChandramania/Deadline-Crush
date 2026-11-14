"""Production worker lifecycle; the Phase 0 marker process remains separate."""
import argparse
import asyncio
import json
import signal
import sys
import time
import os
import shutil
import tempfile
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timezone
from uuid import uuid4
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings
from storage import create_client
from validation_jobs import Queue
from validation_pipeline import Pipeline,ProcessingFailure,blocking


def log(event,**values):
    print(json.dumps({'event':event,**values}),flush=True)


def cleanup_temporary_files():
    # Crash debris only: normal work is bounded well below one hour. Never
    # follow symlinks, or remove another OS user's directory.
    root=Path(tempfile.gettempdir()).resolve()
    for path in root.glob('deadline-validation-*'):
        info=path.lstat()
        if (not path.is_symlink() and path.is_dir() and info.st_uid==os.getuid()
                and time.time()-info.st_mtime>3600 and path.parent.resolve()==root):
            shutil.rmtree(path)


async def process_once(queue,pipeline,owner):
    start = time.monotonic()
    job = await queue.claim(owner)
    if job is None:
        return False
    log('validation_claim',job_id=str(job['id']),version_id=str(job['version_id']),
        generation=job['lease_token'],claim_ms=round((time.monotonic()-start)*1000,2))
    lost = asyncio.Event()
    async def heartbeat():
        while True:
            await asyncio.sleep(queue.lease_seconds/3)
            try:
                if not await queue.heartbeat(job):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return
    renewal = asyncio.create_task(heartbeat())
    work = None
    try:
        source = await queue.source(job)
        work = asyncio.create_task(pipeline.process(source,job))
        lease_loss = asyncio.create_task(lost.wait())
        try:
            done,_ = await asyncio.wait((work,lease_loss),return_when=asyncio.FIRST_COMPLETED)
            if lease_loss in done:
                work.cancel()
                await asyncio.gather(work,return_exceptions=True)
                return True
            result,artifacts = await work
        finally:
            lease_loss.cancel()
            await asyncio.gather(lease_loss,return_exceptions=True)
        result['duration_ms'] = round((time.monotonic()-start)*1000,2)
        result['completed_at'] = datetime.now(timezone.utc).isoformat()
        won = await queue.complete(job,result,artifacts)
        log('validation_result',job_id=str(job['id']),generation=job['lease_token'],
            committed=won,status=result['status'],duration_ms=result['duration_ms'])
    except asyncio.CancelledError:
        if work:
            work.cancel()
            await asyncio.gather(work,return_exceptions=True)
        raise
    except Exception as exc:
        category = str(exc) if isinstance(exc,ProcessingFailure) else 'PROCESSING_UNAVAILABLE'
        try:
            await queue.retry(job,category)
        except Exception:
            pass  # Durable live/expired lease remains recoverable after outage.
        log('validation_retry',job_id=str(job['id']),generation=job['lease_token'],category=category)
    finally:
        renewal.cancel()
        await asyncio.gather(renewal,return_exceptions=True)
    return True


async def run(args):
    settings = Settings.load()
    pool = await db.create_pool(replace(settings,DB_POOL_MIN=settings.WORKER_DB_POOL_MIN,
        DB_POOL_MAX=settings.WORKER_DB_POOL_MAX))
    if pool is None:
        raise RuntimeError('Database required')
    queue = Queue(pool,settings.APP_SCHEMA,args.lease_seconds,args.max_attempts)
    sdk = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:loop.call_soon_threadsafe(stop.set))
    try:
        if args.metrics:
            log('validation_metrics',**await queue.metrics())
            return
        if not args.dispatch:
            if sys.platform!='linux':
                raise RuntimeError('Linux resource enforcement required')
            import pymupdf  # Fail readiness before claiming if renderer absent.
            cleanup_temporary_files()
            sdk = create_client(settings)
            if sdk is None:
                raise RuntimeError('R2 required')
        log('validation_ready',mode='dispatcher' if args.dispatch else 'consumer',concurrency=args.concurrency)
        async def slot():
            owner = uuid4()
            pipeline = Pipeline(sdk,settings.R2_BUCKET,queue) if sdk else None
            while not stop.is_set():
                try:
                    worked = await queue.dispatch() if args.dispatch else await process_once(queue,pipeline,owner)
                    if args.dispatch:
                        for key in await queue.abandoned_previews():
                            if sdk is None:
                                sdk_cleanup = create_client(settings)
                                if sdk_cleanup:
                                    try:
                                        await blocking(sdk_cleanup.delete_object,Bucket=settings.R2_BUCKET,Key=key)
                                    finally:
                                        sdk_cleanup.close()
                except Exception:
                    worked = False
                    log('validation_dependency_unavailable')
                    if args.once:
                        raise
                if args.once:
                    return
                if not worked:
                    try:
                        await asyncio.wait_for(stop.wait(),args.poll_seconds)
                    except TimeoutError:
                        pass
        tasks = [asyncio.create_task(slot()) for _ in range(1 if args.dispatch else args.concurrency)]
        if args.once:
            await asyncio.gather(*tasks)
        else:
            await stop.wait()
            _,pending = await asyncio.wait(tasks,timeout=args.shutdown_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    finally:
        if sdk:
            sdk.close()
        await db.close_pool(pool)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dispatch',action='store_true')
    parser.add_argument('--metrics',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--concurrency',type=int,default=1)
    parser.add_argument('--lease-seconds',type=int,default=60)
    parser.add_argument('--max-attempts',type=int,default=5)
    parser.add_argument('--poll-seconds',type=float,default=2)
    parser.add_argument('--shutdown-seconds',type=float,default=20)
    args = parser.parse_args()
    if not (1<=args.concurrency<=4 and 3<=args.lease_seconds<=600 and 1<=args.max_attempts<=20
            and 0.2<=args.poll_seconds<=30 and 1<=args.shutdown_seconds<=60):
        parser.error('Invalid worker limits')
    if args.check:
        Settings.load()
        log('validation_configuration_ready',runtime_ready=sys.platform=='linux')
    else:
        asyncio.run(run(args))


if __name__=='__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Validation worker unavailable; inspect safe diagnostics') from None
