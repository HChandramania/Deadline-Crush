"""Phase 2 ledger. No R2 orchestration or validation is performed here."""
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain import Profile, current_profile, transaction

router = APIRouter(prefix='/api/v1')
PART_SIZE = 8388608


class SubmissionError(HTTPException):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        super().__init__(status, code.replace('_', ' ').capitalize())


class VersionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default='', max_length=20000)
    links: list[str] = Field(default_factory=list, max_length=20)
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, strict=True)
    content_type: str = Field(min_length=1, max_length=255)

    @field_validator('links')
    @classmethod
    def bounded_links(cls, values):
        if any(len(value) > 2048 for value in values):
            raise ValueError('Link too long')
        return values


class PartInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    part_number: int = Field(ge=1, le=10000, strict=True)
    etag: str = Field(min_length=1, max_length=256)


class CompletionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    parts: list[PartInput] = Field(min_length=1, max_length=13)

    @field_validator('parts')
    @classmethod
    def ordered_unique(cls, parts):
        if [p.part_number for p in parts] != list(range(1, len(parts) + 1)):
            raise ValueError('Manifest must be complete and ordered')
        return parts


class Admission(BaseModel):
    version_id: UUID
    version_no: int
    upload_session_id: UUID
    status: Literal['CREATED'] = 'CREATED'
    admitted_at: datetime
    deadline_at: datetime
    grace_until: datetime
    part_size_bytes: int


class VersionView(BaseModel):
    id: UUID
    version_no: int
    title: str
    description: str
    links: list[str]
    filename: str
    size_bytes: int
    content_type: str
    lifecycle: str
    validation_status: str
    admitted_at: datetime
    finalized_at: datetime | None


class History(BaseModel):
    versions: list[VersionView]
    next_before_version_no: int | None = None


class Current(BaseModel):
    selected_version_id: UUID | None = None
    version_no: int | None = None
    lifecycle: str | None = None
    validation_status: str | None = None
    finalized_at: datetime | None = None
    validation_errors: list[str] = Field(default_factory=list)


class CompletionResult(BaseModel):
    completion_intent_id: UUID
    status: Literal['ADMITTED', 'FINALIZED']
    status_url: str
    version_id: UUID | None = None


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def fingerprint(value) -> str:
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class UploadAuthorization:
    session_id: UUID
    version_id: UUID
    object_key: str
    r2_upload_id: str | None
    expected_size: int
    part_size: int
    expected_parts: int


@dataclass(frozen=True)
class StoredPart:
    part_number: int
    etag: str
    size_bytes: int


@dataclass(frozen=True)
class ObjectEvidence:
    session_id: UUID
    object_key: str
    size_bytes: int
    manifest_sha256: str


class StorageVerifier(Protocol):
    """Trusted adapter must bind observations to this exact immutable session.

    Phase 3 must independently ListParts, compare ETags at completion, and verify
    HEAD identity/size. Returning client claims from an implementation is unsafe.
    None means object completion is pending, never successful evidence.
    """
    async def parts(self, upload: UploadAuthorization) -> tuple[StoredPart, ...]: ...
    async def completed_object(self, upload: UploadAuthorization, manifest: tuple[PartInput, ...]) -> ObjectEvidence | None: ...


class UnavailableStorage:
    async def parts(self, upload):
        raise SubmissionError('STORAGE_NOT_CONFIGURED', 503)

    async def completed_object(self, upload, manifest):
        raise SubmissionError('STORAGE_NOT_CONFIGURED', 503)


def storage_verifier(request: Request) -> StorageVerifier:
    # Never fall back to the Phase 0 probe client or a participant claim.
    return getattr(request.app.state, 'submission_verifier', None) or UnavailableStorage()


async def audit(c, actor, team, event, entity=None, reason=None):
    await c.execute('''INSERT INTO audit_events(actor_id,team_id,event_type,entity_id,detail)
        VALUES($1,$2,$3,$4,$5::jsonb)''', actor, team, event, entity,
        canonical({'reason': reason} if reason else {}))


async def observe_storage(request, actor, team_id, action, observation):
    try:
        return await observation
    except Exception as exc:
        # Persist a safe category only; SDK exceptions may contain bearer URLs.
        failure = exc if isinstance(exc, SubmissionError) else SubmissionError('STORAGE_VERIFICATION_FAILED', 503)
        async with operation(request, actor, team_id, action):
            raise failure from None


@asynccontextmanager
async def operation(request, actor, team_id, action):
    """Commit deliberate rejection audits; unexpected/DB errors still roll back."""
    rejected = None
    async with transaction(request) as c:
        try:
            yield c
        except SubmissionError as exc:
            rejected = exc
            # Do not attach unknown or inaccessible team identifiers to audits.
            visible = await c.fetchval('''SELECT t.id FROM teams t JOIN team_members m ON m.team_id=t.id
                WHERE t.id=$1 AND m.user_id=$2''', team_id, actor)
            await audit(c, actor, visible, action + '_REJECTED', reason=exc.code)
            if exc.code in ('DEADLINE_CLOSED', 'GRACE_EXPIRED'):
                await c.execute('INSERT INTO late_attempts(actor_id,team_id,action,reason) VALUES($1,$2,$3,$4)',
                                actor, visible, action, exc.code)
    if rejected:
        raise rejected


async def lock_team(c, team_id, actor, *, require_captain: bool = True):
    row = await c.fetchrow('''SELECT t.*, h.deadline_at, h.max_file_bytes,
        h.deadline_at + interval '600 seconds' AS grace_until FROM teams t
        JOIN hackathons h ON h.id=t.hackathon_id WHERE t.id=$1
        AND EXISTS(SELECT 1 FROM team_members m WHERE m.team_id=t.id AND m.user_id=$2)
        FOR UPDATE OF t FOR SHARE OF h''', team_id, actor)
    if row is None:
        raise SubmissionError('NOT_FOUND', 404)
    # Team and membership locks make role changes serialize with the transition.
    # Rechecked on retries and on both sides of every external storage observation.
    member = await c.fetchval('SELECT role FROM team_members WHERE team_id=$1 AND user_id=$2 FOR SHARE', team_id, actor)
    if member is None:
        raise SubmissionError('NOT_FOUND', 404)
    if require_captain and (member != 'CAPTAIN' or row['captain_id'] != actor):
        raise SubmissionError('CAPTAIN_REQUIRED', 403)
    return row


async def gate(c, cutoff):
    return await c.fetchrow('''WITH time AS MATERIALIZED (SELECT clock_timestamp() AS stamp)
        SELECT stamp,submission_time_eligible(stamp,$1) AS eligible FROM time''', cutoff)


async def upload_row(c, team_id, session_id):
    row = await c.fetchrow('''SELECT s.*, v.team_id,v.lifecycle,v.expected_size,v.admitted_at,
        v.deadline_at,v.grace_until,v.version_no FROM upload_sessions s
        JOIN submission_versions v ON v.id=s.version_id
        WHERE s.id=$1 AND v.team_id=$2 FOR UPDATE OF s,v''', session_id, team_id)
    if row is None:
        raise SubmissionError('NOT_FOUND', 404)
    return row


def authorization(row):
    return UploadAuthorization(row['id'], row['version_id'], row['object_key'], row['r2_upload_id'],
                               row['expected_size'], row['part_size'], row['expected_parts'])


def admission(row):
    return Admission(version_id=row['id'], version_no=row['version_no'], upload_session_id=row['session_id'],
                     admitted_at=row['admitted_at'], deadline_at=row['deadline_at'], grace_until=row['grace_until'],
                     part_size_bytes=row['part_size'])


@router.post('/submissions/versions', response_model=Admission, status_code=201)
async def admit(body: VersionInput, team_id: UUID, request: Request, response: Response,
                profile: Profile = Depends(current_profile), idempotency_key: UUID = Header(alias='Idempotency-Key')):
    digest = fingerprint(body.model_dump(mode='json'))
    async with operation(request, profile.id, team_id, 'ADMISSION') as c:
        team = await lock_team(c, team_id, profile.id)
        old = await c.fetchrow('''SELECT v.*,s.id AS session_id,s.part_size FROM submission_versions v
            JOIN upload_sessions s ON s.version_id=v.id
            WHERE v.team_id=$1 AND v.admitted_by=$2 AND v.idempotency_key=$3''', team_id, profile.id, idempotency_key)
        if old:
            if old['request_hash'] != digest:
                raise SubmissionError('IDEMPOTENCY_CONFLICT')
            response.status_code = 200
            return admission(old)
        # Linearization point: one fresh database timestamp AFTER team/member locks.
        decision = await gate(c, team['deadline_at'])
        stamp = decision['stamp']
        if not decision['eligible']:
            raise SubmissionError('DEADLINE_CLOSED')
        if body.size_bytes > min(team['max_file_bytes'], 104857600):
            raise SubmissionError('FILE_TOO_LARGE', 413)
        version_id, session_id = uuid4(), uuid4()
        key = f"submissions/{team['hackathon_id']}/{team_id}/{version_id}/source"
        await c.execute('''INSERT INTO submission_versions(id,team_id,version_no,idempotency_key,request_hash,
            title,description,links,object_key,expected_size,admitted_at,admitted_by,filename,content_type,deadline_at,grace_until)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,$11,$12,$13,$14,$15,$16)''',
            version_id, team_id, team['next_version_no'], idempotency_key, digest, body.title, body.description,
            canonical(body.links), key, body.size_bytes, stamp, profile.id, body.filename, body.content_type,
            team['deadline_at'], team['grace_until'])
        await c.execute('''INSERT INTO upload_sessions(id,version_id,object_key,status,part_size,expected_parts,expires_at)
            VALUES($1,$2,$3,'INITIATING',$4,$5,$6)''', session_id, version_id, key, PART_SIZE,
            (body.size_bytes + PART_SIZE - 1) // PART_SIZE, team['grace_until'])
        await c.execute('UPDATE teams SET next_version_no=next_version_no+1 WHERE id=$1', team_id)
        await audit(c, profile.id, team_id, 'VERSION_ADMITTED', version_id)
        result = Admission(version_id=version_id, version_no=team['next_version_no'], upload_session_id=session_id,
                           admitted_at=stamp, deadline_at=team['deadline_at'], grace_until=team['grace_until'], part_size_bytes=PART_SIZE)
    return result


async def admit_intent(request, actor, team_id, session_id, key, body, verifier):
    manifest = body.model_dump(mode='json')['parts']
    digest = fingerprint(manifest)
    # Snapshot under locks, then release all DB resources before storage calls.
    async with operation(request, actor, team_id, 'COMPLETION') as c:
        await lock_team(c, team_id, actor)
        row = await upload_row(c, team_id, session_id)
        old = await c.fetchrow('SELECT * FROM completion_intents WHERE upload_session_id=$1', session_id)
        if old:
            if old['manifest_sha256'] != digest:
                raise SubmissionError('IDEMPOTENCY_CONFLICT')
            return dict(old), authorization(row)
        if row['lifecycle'] != 'UPLOADING' or row['status'] != 'ACTIVE' or not row['r2_upload_id']:
            raise SubmissionError('UPLOAD_NOT_ACTIVE')
        if row['deadline_at'] is None or row['admitted_at'] > row['deadline_at']:
            raise SubmissionError('INELIGIBLE_VERSION')
        if not (await gate(c, row['grace_until']))['eligible']:
            raise SubmissionError('GRACE_EXPIRED')
        snapshot = authorization(row)
    observed = await observe_storage(request, actor, team_id, 'COMPLETION', verifier.parts(snapshot))
    expected = tuple(StoredPart(p.part_number, p.etag,
        min(snapshot.part_size, snapshot.expected_size - (p.part_number - 1) * snapshot.part_size)) for p in body.parts)
    matches = len(body.parts) == snapshot.expected_parts and observed == expected
    async with operation(request, actor, team_id, 'COMPLETION') as c:
        await lock_team(c, team_id, actor)
        row = await upload_row(c, team_id, session_id)
        old = await c.fetchrow('SELECT * FROM completion_intents WHERE upload_session_id=$1', session_id)
        if old:
            if old['manifest_sha256'] != digest:
                raise SubmissionError('IDEMPOTENCY_CONFLICT')
            return dict(old), authorization(row)
        decision = await gate(c, row['grace_until'])
        stamp = decision['stamp']
        if not decision['eligible']:
            raise SubmissionError('GRACE_EXPIRED')
        if authorization(row) != snapshot or row['status'] != 'ACTIVE' or row['lifecycle'] != 'UPLOADING':
            raise SubmissionError('UPLOAD_NOT_ACTIVE')
        if not matches:
            raise SubmissionError('PARTS_MISMATCH')
        intent = await c.fetchrow('''INSERT INTO completion_intents(upload_session_id,idempotency_key,
            manifest,manifest_sha256,admitted_at,status,admitted_by) VALUES($1,$2,$3::jsonb,$4,$5,'ADMITTED',$6) RETURNING *''',
            session_id, key, canonical(manifest), digest, stamp, actor)
        await c.execute("UPDATE upload_sessions SET status='COMPLETING' WHERE id=$1", session_id)
        await c.execute("UPDATE submission_versions SET lifecycle='COMPLETING' WHERE id=$1", row['version_id'])
        await audit(c, actor, team_id, 'COMPLETION_ADMITTED', intent['id'])
    return dict(intent), snapshot


async def finalization_authority(c, actor, team_id, session_id, recovery):
    if recovery is None:
        return await lock_team(c, team_id, actor)
    # Only the dedicated internal caller supplies a lease token. Never a route input.
    await c.fetchrow('SELECT id FROM teams WHERE id=$1 FOR UPDATE', team_id)
    valid = await c.fetchval('''SELECT 1 FROM storage_operations WHERE upload_session_id=$1
        AND recovery_token=$2 AND recovery_until>clock_timestamp() FOR UPDATE''', session_id, recovery)
    if not valid:
        raise SubmissionError('RECOVERY_LEASE_LOST')


async def finalize(request, actor, team_id, session_id, verifier, *, recovery=None):
    """Retry/recovery boundary, also usable by a later authenticated reconciler.

    Storage verification has no open transaction. A committed qualifying intent
    remains recoverable beyond grace; no new manifest can be introduced here.
    """
    async with operation(request, actor, team_id, 'FINALIZATION') as c:
        await finalization_authority(c, actor, team_id, session_id, recovery)
        row = await upload_row(c, team_id, session_id)
        intent = await c.fetchrow('SELECT * FROM completion_intents WHERE upload_session_id=$1', session_id)
        if not intent or intent['status'] == 'FAILED':
            raise SubmissionError('COMPLETION_INTENT_REQUIRED')
        if (row['deadline_at'] is None or row['admitted_at'] > row['deadline_at'] or
                intent['admitted_at'] > row['grace_until']):
            raise SubmissionError('INELIGIBLE_VERSION')
        if row['lifecycle'] == 'FINALIZED':
            if row['status'] != 'COMPLETED' or intent['status'] != 'FINALIZED':
                raise SubmissionError('INVALID_TRANSITION')
            return row['version_id']
        if row['status'] != 'COMPLETING' or row['lifecycle'] != 'COMPLETING':
            raise SubmissionError('INVALID_TRANSITION')
        snapshot = authorization(row)
        parts = tuple(PartInput(**p) for p in json.loads(intent['manifest']))
        intent_id, digest = intent['id'], intent['manifest_sha256']
    evidence = await observe_storage(request, actor, team_id, 'FINALIZATION', verifier.completed_object(snapshot, parts))
    if evidence is None:
        return None
    async with operation(request, actor, team_id, 'FINALIZATION') as c:
        await finalization_authority(c, actor, team_id, session_id, recovery)
        row = await upload_row(c, team_id, session_id)
        current_intent = await c.fetchrow('SELECT * FROM completion_intents WHERE id=$1 FOR UPDATE', intent_id)
        if row['lifecycle'] == 'FINALIZED':
            if row['status'] != 'COMPLETED' or not current_intent or current_intent['status'] != 'FINALIZED':
                raise SubmissionError('INVALID_TRANSITION')
            return row['version_id']
        if (current_intent is None or current_intent['status'] not in ('ADMITTED','RECOVERY_REQUIRED','STORAGE_COMPLETED') or
                row['lifecycle'] != 'COMPLETING' or row['status'] != 'COMPLETING' or authorization(row) != snapshot):
            raise SubmissionError('INVALID_TRANSITION')
        if evidence != ObjectEvidence(session_id, snapshot.object_key, snapshot.expected_size, digest):
            raise SubmissionError('STORAGE_EVIDENCE_MISMATCH')
        if current_intent['status'] != 'STORAGE_COMPLETED':
            await c.execute("UPDATE completion_intents SET status='STORAGE_COMPLETED' WHERE id=$1", intent_id)
        stamp = await c.fetchval('SELECT clock_timestamp()')
        await c.execute("UPDATE upload_sessions SET status='COMPLETED',completed_at=$2 WHERE id=$1", session_id, stamp)
        await c.execute("UPDATE submission_versions SET lifecycle='FINALIZED',validation_status='CHECKING',finalized_at=$2 WHERE id=$1", row['version_id'], stamp)
        await c.execute("INSERT INTO validation_jobs(version_id) VALUES($1) ON CONFLICT(version_id) DO NOTHING", row['version_id'])
        await c.execute("INSERT INTO outbox(event_type,aggregate_id) VALUES('VALIDATE',$1) ON CONFLICT(event_type,aggregate_id) DO NOTHING", row['version_id'])
        await c.execute("UPDATE completion_intents SET status='FINALIZED' WHERE id=$1", intent_id)
        await c.execute('''UPDATE teams SET selected_version_id=$2,selected_version_no=$3 WHERE id=$1
            AND (selected_version_no IS NULL OR selected_version_no<$3)''', team_id, row['version_id'], row['version_no'])
        await audit(c, actor, team_id, 'VERSION_FINALIZED', row['version_id'])
    return row['version_id']


@router.post('/uploads/{session_id}/complete', response_model=CompletionResult, status_code=202)
async def complete(session_id: UUID, body: CompletionInput, team_id: UUID, request: Request, response: Response,
                   profile: Profile = Depends(current_profile), idempotency_key: UUID = Header(alias='Idempotency-Key'),
                   verifier: StorageVerifier = Depends(storage_verifier)):
    intent, _ = await admit_intent(request, profile.id, team_id, session_id, idempotency_key, body, verifier)
    version_id = await finalize(request, profile.id, team_id, session_id, verifier)
    if version_id:
        response.status_code = 200
    return CompletionResult(completion_intent_id=intent['id'], status='FINALIZED' if version_id else 'ADMITTED',
        status_url=f'/api/v1/uploads/{session_id}?team_id={team_id}', version_id=version_id)


@router.get('/submissions/current', response_model=Current)
async def current(team_id: UUID, request: Request, profile: Profile = Depends(current_profile)):
    async with operation(request, profile.id, team_id, 'READ_CURRENT') as c:
        await lock_team(c, team_id, profile.id, require_captain=False)
        row = await c.fetchrow('''SELECT v.id AS selected_version_id,v.version_no,v.lifecycle,v.validation_status,v.finalized_at,
            j.result AS validation_result FROM submission_versions v
            LEFT JOIN validation_jobs j ON j.version_id=v.id
            WHERE v.team_id=$1 AND v.lifecycle='FINALIZED'
            AND v.admitted_at<=v.deadline_at ORDER BY v.version_no DESC LIMIT 1''', team_id)
        errors = []
        if row:
            row = dict(row)
            result = row.pop('validation_result')
            if result:
                errors = json.loads(result).get('findings',[])
    return Current(**dict(row),validation_errors=errors) if row else Current()


@router.get('/submissions/versions', response_model=History)
async def history(team_id: UUID, request: Request, profile: Profile = Depends(current_profile),
                  before_version_no: int | None = Query(default=None, ge=1)):
    async with operation(request, profile.id, team_id, 'READ_HISTORY') as c:
        await lock_team(c, team_id, profile.id, require_captain=False)
        rows = await c.fetch('''SELECT id,version_no,title,description,links,filename,expected_size AS size_bytes,
            content_type,lifecycle,validation_status,admitted_at,finalized_at FROM submission_versions
            WHERE team_id=$1 AND ($2::integer IS NULL OR version_no<$2)
            ORDER BY version_no DESC LIMIT 101''', team_id, before_version_no)
    return History(versions=[VersionView(**{**dict(row), 'links': json.loads(row['links'])}) for row in rows[:100]],
                   next_before_version_no=rows[99]['version_no'] if len(rows)>100 else None)
