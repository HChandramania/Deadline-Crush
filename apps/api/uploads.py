"""Captain-only multipart capabilities over the Phase 2 immutable ledger."""
from datetime import datetime, timedelta
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from domain import Profile, current_profile
from submissions import (SubmissionError, StorageVerifier, storage_verifier, operation,
                         lock_team, upload_row, authorization, gate, audit, observe_storage)
from submissions import canonical, fingerprint

router = APIRouter(prefix='/api/v1/uploads')


class SignInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    part_numbers: list[int] = Field(min_length=1, max_length=13)

    @field_validator('part_numbers', mode='before')
    @classmethod
    def valid_numbers(cls, values):
        if not isinstance(values, list) or any(type(n) is not int or n<1 or n>13 for n in values):
            raise ValueError('Invalid part numbers')
        if len(set(values)) != len(values):
            raise ValueError('Duplicate part number')
        return values


class UploadState(BaseModel):
    status: str
    part_size_bytes: int
    expected_parts: int
    authorized_part_numbers: list[int]
    completed_parts: list[dict]
    missing_parts: list[int]
    deadline_at: datetime
    grace_until: datetime
    completion_pending: bool
    recoverable: bool
    parts_source: str


class SignedPart(BaseModel):
    part_number: int
    url: str = Field(repr=False)
    expires_at: datetime
    method: str = 'PUT'
    headers: dict[str, str] = Field(default_factory=dict)


class SignedParts(BaseModel):
    parts: list[SignedPart]


def require_adapter(verifier):
    if not hasattr(verifier, 'create'):
        raise SubmissionError('STORAGE_NOT_CONFIGURED', 503)


async def request_identity(c, actor, team_id, session_id, key, action, body):
    event = action+'_REQUEST'
    digest = fingerprint({'session_id': str(session_id), 'body': body})
    old = await c.fetchval('''SELECT detail->>'hash' FROM audit_events WHERE team_id=$1 AND actor_id=$2
        AND event_type=$3 AND detail->>'key'=$4''', team_id, actor, event, str(key))
    if old is not None:
        if old != digest:
            raise SubmissionError('IDEMPOTENCY_CONFLICT')
        return
    await c.execute('''INSERT INTO audit_events(team_id,actor_id,event_type,entity_id,detail)
        VALUES($1,$2,$3,$4,$5::jsonb)''',team_id,actor,event,session_id,canonical({'key':str(key),'hash':digest}))


@router.post('/{session_id}/initialize', response_model=UploadState)
async def initialize(session_id: UUID, team_id: UUID, request: Request, response: Response,
                     profile: Profile = Depends(current_profile),
                     idempotency_key: UUID = Header(alias='Idempotency-Key'),
                     verifier: StorageVerifier = Depends(storage_verifier)):
    async with operation(request, profile.id, team_id, 'UPLOAD_INITIALIZE') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        await request_identity(c,profile.id,team_id,session_id,idempotency_key,'UPLOAD_INITIALIZE',{})
        if row['status'] == 'ACTIVE':
            return await state_from_row(c, row, ())
        if row['status'] != 'INITIATING' or row['lifecycle'] != 'CREATED':
            raise SubmissionError('UPLOAD_NOT_INITIALIZABLE')
        if not (await gate(c, row['deadline_at']))['eligible']:
            raise SubmissionError('DEADLINE_CLOSED')
        require_adapter(verifier)
        # Natural idempotency scope is the session: never issue a second create,
        # even with another request key or an expired/unknown first attempt.
        inserted = await c.fetchval('''INSERT INTO storage_operations(upload_session_id)
            VALUES($1) ON CONFLICT DO NOTHING RETURNING upload_session_id''', session_id)
        if not inserted:
            response.status_code = 202
            return await state_from_row(c, row, ())
        snapshot = authorization(row)
        content_type = await c.fetchval('SELECT content_type FROM submission_versions WHERE id=$1', row['version_id'])
    upload_id = await observe_storage(request, profile.id, team_id, 'UPLOAD_INITIALIZE',
                                      verifier.create(snapshot, content_type))
    # Failure here leaves a durable STARTED record. Cleanup finds the orphan by
    # exact opaque key. Never claim an unpersisted UploadId is recoverable.
    async with operation(request, profile.id, team_id, 'UPLOAD_INITIALIZE') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        if not (await gate(c, row['deadline_at']))['eligible']:
            raise SubmissionError('DEADLINE_CLOSED')
        if authorization(row) != snapshot or row['status'] != 'INITIATING':
            raise SubmissionError('INVALID_TRANSITION')
        await c.execute("UPDATE upload_sessions SET r2_upload_id=$2,status='ACTIVE' WHERE id=$1", session_id, upload_id)
        await c.execute("UPDATE submission_versions SET lifecycle='UPLOADING' WHERE id=$1", row['version_id'])
        await c.execute("UPDATE storage_operations SET create_state='BOUND' WHERE upload_session_id=$1", session_id)
        await audit(c, profile.id, team_id, 'UPLOAD_INITIALIZED', session_id)
        row = await upload_row(c, team_id, session_id)
        return await state_from_row(c, row, ())


async def state_from_row(c, row, parts, source='NOT_LISTED'):
    intent = await c.fetchrow('SELECT status,admitted_at,manifest FROM completion_intents WHERE upload_session_id=$1', row['id'])
    decision = await gate(c, row['grace_until'])
    numbers = list(range(1, row['expected_parts']+1))
    completed = [{'part_number': p.part_number, 'etag': p.etag, 'size_bytes': p.size_bytes} for p in parts]
    if intent and row['status'] in ('COMPLETING','COMPLETED'):
        completed = [{**p, 'size_bytes': min(row['part_size'], row['expected_size']-(p['part_number']-1)*row['part_size'])}
                     for p in json.loads(intent['manifest'])]
        source = 'FROZEN_MANIFEST'
    return UploadState(status=row['status'], part_size_bytes=row['part_size'], expected_parts=row['expected_parts'],
        authorized_part_numbers=numbers,
        completed_parts=completed,
        missing_parts=[n for n in numbers if n not in {p['part_number'] for p in completed}],
        deadline_at=row['deadline_at'], grace_until=row['grace_until'],
        completion_pending=bool(intent and intent['status'] not in ('FINALIZED','FAILED')),
        parts_source=source, recoverable=bool((row['status']=='ACTIVE' and decision['eligible']) or
            (intent and intent['status'] not in ('FINALIZED','FAILED') and intent['admitted_at']<=row['grace_until'])))


@router.get('/{session_id}', response_model=UploadState)
async def status(session_id: UUID, team_id: UUID, request: Request,
                 profile: Profile = Depends(current_profile), verifier: StorageVerifier = Depends(storage_verifier)):
    async with operation(request, profile.id, team_id, 'UPLOAD_STATUS') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        snapshot = authorization(row)
        if row['status'] != 'ACTIVE':
            return await state_from_row(c, row, ())
    parts = await observe_storage(request, profile.id, team_id, 'UPLOAD_STATUS', verifier.parts(snapshot))
    async with operation(request, profile.id, team_id, 'UPLOAD_STATUS') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        return await state_from_row(c, row, parts if row['status']=='ACTIVE' else (), 'R2' if row['status']=='ACTIVE' else 'NOT_LISTED')


@router.post('/{session_id}/parts/sign', response_model=SignedParts)
async def sign(session_id: UUID, body: SignInput, team_id: UUID, request: Request,
               profile: Profile = Depends(current_profile),
               idempotency_key: UUID = Header(alias='Idempotency-Key'),
               verifier: StorageVerifier = Depends(storage_verifier)):
    async with operation(request, profile.id, team_id, 'UPLOAD_SIGN') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        await request_identity(c,profile.id,team_id,session_id,idempotency_key,'UPLOAD_SIGN',body.model_dump())
        if row['status'] != 'ACTIVE' or not row['r2_upload_id']:
            raise SubmissionError('UPLOAD_NOT_ACTIVE')
        if any(n>row['expected_parts'] for n in body.part_numbers):
            raise SubmissionError('PART_OUT_OF_RANGE')
        if not (await gate(c, row['grace_until']))['eligible']:
            raise SubmissionError('GRACE_EXPIRED')
        snapshot = authorization(row)
    # ListParts observes missing/retryable parts; authorized range never expands.
    await observe_storage(request, profile.id, team_id, 'UPLOAD_SIGN', verifier.parts(snapshot))
    async with operation(request, profile.id, team_id, 'UPLOAD_SIGN') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        decision = await gate(c, row['grace_until'])
        if not decision['eligible']:
            raise SubmissionError('GRACE_EXPIRED')
        if authorization(row) != snapshot or row['status'] != 'ACTIVE':
            raise SubmissionError('UPLOAD_NOT_ACTIVE')
        seconds = min(request.app.state.settings.R2_URL_TTL_SECONDS,
                      int((row['grace_until']-decision['stamp']).total_seconds()))
        if seconds < 1:
            raise SubmissionError('GRACE_EXPIRED')
        stamp = decision['stamp']
    require_adapter(verifier)
    urls = await observe_storage(request, profile.id, team_id, 'UPLOAD_SIGN', verifier.sign(snapshot, body.part_numbers, seconds))
    # Signing is local CPU work, nevertheless discard capabilities after a role,
    # state or cutoff change. No network call while database locks are held.
    async with operation(request, profile.id, team_id, 'UPLOAD_SIGN') as c:
        await lock_team(c, team_id, profile.id)
        row = await upload_row(c, team_id, session_id)
        if row['status'] != 'ACTIVE' or authorization(row)!=snapshot:
            raise SubmissionError('UPLOAD_NOT_ACTIVE')
        if not (await gate(c, row['grace_until']))['eligible']:
            raise SubmissionError('GRACE_EXPIRED')
    return SignedParts(parts=[SignedPart(part_number=n, url=url, expires_at=stamp+timedelta(seconds=seconds))
                              for n,url in zip(body.part_numbers, urls, strict=True)])


@router.post('/{session_id}/resume', response_model=UploadState)
async def resume(session_id: UUID, team_id: UUID, request: Request,
                 profile: Profile = Depends(current_profile),
                 idempotency_key: UUID = Header(alias='Idempotency-Key'),
                 verifier: StorageVerifier = Depends(storage_verifier)):
    # Two-step browser protocol: authoritative state, then /parts/sign for chosen
    # missing/retryable numbers. Never initialize or replace an upload on resume.
    async with operation(request, profile.id, team_id, 'UPLOAD_RESUME') as c:
        await lock_team(c, team_id, profile.id)
        await upload_row(c,team_id,session_id)
        await request_identity(c,profile.id,team_id,session_id,idempotency_key,'UPLOAD_RESUME',{})
    return await status(session_id, team_id, request, profile, verifier)
