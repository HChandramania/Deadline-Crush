import asyncio
import secrets
import sys
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID, uuid4
import httpx
import asyncpg
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from identity import TokenVerifier
from domain import router
from submissions import router as submission_router, SubmissionError
from uploads import router as upload_router
from product import router as product_router
from r2_uploads import R2Storage
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from deadline_crush_spikes import db, jobs
from deadline_crush_spikes.settings import Settings
import storage


@asynccontextmanager
async def lifespan(app):
    settings = Settings.load()
    app.state.settings = settings
    app.state.pool = None
    app.state.storage = None
    app.state.verifier = None
    app.state.submission_verifier = None
    app.state.auth_http = httpx.AsyncClient(timeout=5, follow_redirects=False, limits=httpx.Limits(max_connections=5))
    try:
        app.state.pool = await db.create_pool(settings)
        app.state.storage = storage.create_client(settings)
        if app.state.storage is not None:
            app.state.submission_verifier = R2Storage(app.state.storage, settings.R2_BUCKET)
        if settings.NEON_AUTH_BASE_URL and settings.NEON_AUTH_JWKS_URL:
            app.state.verifier = TokenVerifier(settings.NEON_AUTH_BASE_URL, settings.NEON_AUTH_JWKS_URL, app.state.auth_http)
        yield
    finally:
        submission_verifier = app.state.submission_verifier
        app.state.submission_verifier = None
        if submission_verifier is not None:
            await submission_verifier.close()
        await db.close_pool(app.state.pool)
        await app.state.auth_http.aclose()
        if app.state.storage is not None:
            app.state.storage.close()


app = FastAPI(title='Deadline Crush Phase 0', docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(router)
app.include_router(submission_router)
app.include_router(upload_router)
app.include_router(product_router)
app.add_middleware(CORSMiddleware, allow_origins=Settings.load().CORS_ORIGINS.split(','),
                   allow_credentials=False, allow_methods=['GET', 'POST'],
                   allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'])


@app.middleware('http')
async def redact_dependency_errors(request, call_next):
    request.state.request_id = str(uuid4())
    try:
        response = await call_next(request)
        if request.url.path.startswith('/api/v1/'):
            response.headers['Cache-Control'] = 'private, no-store'
        return response
    except Exception:
        # Prevent ASGI server traceback logs from exposing SDK request URLs.
        return await error(request, None)


def authorized(request: Request):
    settings = getattr(request.app.state, 'settings', None)
    token = settings.PROBE_TOKEN if settings else ''
    if not token or not secrets.compare_digest(request.headers.get('x-probe-token', ''), token):
        raise HTTPException(403, 'Probe disabled or forbidden')
    return request.app.state


@app.exception_handler(Exception)
async def error(request, exc):
    return error_response(request, 503, 'DEPENDENCY_FAILED', 'Service temporarily unavailable')


def error_response(request, status, code, message, headers=None):
    return JSONResponse(status_code=status, headers=headers, content={'error': {
        'code': code, 'message': message, 'request_id': getattr(request.state, 'request_id', str(uuid4()))}})


@app.exception_handler(HTTPException)
async def http_error(request, exc):
    codes = {401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 409: 'CONFLICT'}
    return error_response(request, exc.status_code, exc.code if isinstance(exc, SubmissionError) else codes.get(exc.status_code, 'REQUEST_FAILED'), exc.detail, exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return error_response(request, 422, 'INVALID_REQUEST', 'Request validation failed')


@app.exception_handler(asyncpg.PostgresError)
async def database_error(request, exc):
    # SQL errors can contain values; never echo or log them.
    return error_response(request, 503, 'DATABASE_UNAVAILABLE', 'Service temporarily unavailable')


@app.get('/health')
async def health():
    return {'status': 'ok', 'phase': 0, 'python': sys.version.split()[0]}


@app.get('/health/dependencies')
async def dependencies(state=Depends(authorized)):
    return {'postgres': await db.probe(state.pool), 'r2': 'NOT VERIFIED', 'jobs': 'NOT VERIFIED'}


class Part(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=256)


class R2Request(BaseModel):
    operation: Literal['create', 'sign', 'list', 'complete', 'abort', 'head', 'delete']
    key: str = Field(pattern=r'^phase0/[a-f0-9-]+$')
    upload_id: str = Field(default='', max_length=2048)
    manifest: list[Part] = Field(default_factory=list, max_length=2)


@app.post('/probe/r2')
async def r2(body: R2Request, state=Depends(authorized)):
    return await asyncio.to_thread(storage.operation, state.storage, state.settings.R2_BUCKET, body)


class JobRequest(BaseModel):
    id: UUID


@app.post('/probe/jobs')
async def enqueue(body: JobRequest, state=Depends(authorized)):
    if state.pool is None:
        raise HTTPException(503, 'Database not configured')
    await jobs.enqueue(state.pool, body.id)
    return {'id': str(body.id)}


@app.get('/probe/jobs/{event_id}')
async def receipt(event_id: UUID, state=Depends(authorized)):
    if state.pool is None:
        raise HTTPException(503, 'Database not configured')
    async with state.pool.acquire(timeout=10) as connection:
        row = await connection.fetchrow('SELECT status, attempts, lease_token FROM phase0_jobs WHERE id=$1', event_id)
    if row is None:
        raise HTTPException(404, 'Probe job not found')
    return dict(row)


def validation_probe(client, bucket):
    import io
    import zipfile
    from botocore.exceptions import ClientError
    from validation import inspect_zip, pdf_probe, MAX_INPUT
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('README.md', 'phase0')
    result = {'zip': inspect_zip(output.getvalue())}
    if client is None:
        return {**result, 'pdf': 'BLOCKED: R2 not configured'}
    try:
        metadata = client.head_object(Bucket=bucket, Key='phase0/pdf-fixture')
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
            return {**result, 'pdf': 'BLOCKED: PDF fixture absent'}
        raise
    if metadata['ContentLength'] > MAX_INPUT:
        return {**result, 'pdf': 'BLOCKED: fixture exceeds 8 MiB cap'}
    obj = client.get_object(Bucket=bucket, Key='phase0/pdf-fixture', Range=f'bytes=0-{MAX_INPUT}')
    with obj['Body'] as stream:
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError('PDF fixture limit')
    try:
        result['pdf_png_bytes'] = len(pdf_probe(data))
    except ImportError:
        result['pdf'] = 'BLOCKED: install optional PDF dependency'
    return result


@app.post('/probe/validation')
async def validation(state=Depends(authorized)):
    return await asyncio.to_thread(validation_probe, state.storage, state.settings.R2_BUCKET)
