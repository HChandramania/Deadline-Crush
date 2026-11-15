"""Read-only product surfaces; lifecycle mutations remain in their original services."""
import asyncio
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel
from domain import Profile, current_profile, organizer, transaction
from submissions import History

router = APIRouter(prefix='/api/v1')


class Event(BaseModel):
    id: UUID
    name: str
    deadline_at: datetime
    grace_until: datetime
    max_file_bytes: int


class Events(BaseModel):
    server_time: datetime
    events: list[Event]


@router.get('/hackathons', response_model=Events)
async def events(request: Request, profile: Profile = Depends(current_profile)):
    async with transaction(request) as c:
        stamp = await c.fetchval('SELECT clock_timestamp()')
        rows = await c.fetch('''SELECT id,name,deadline_at,deadline_at+interval '600 seconds' AS grace_until,
            max_file_bytes FROM hackathons ORDER BY deadline_at DESC,id LIMIT 100''')
    return {'server_time': stamp, 'events': [dict(r) for r in rows]}


async def visible_version(c, version_id, profile):
    row = await c.fetchrow('''SELECT v.id,v.team_id,v.object_key,v.filename,v.lifecycle FROM submission_versions v
        WHERE v.id=$1 AND ($3::boolean OR EXISTS
        (SELECT 1 FROM team_members m WHERE m.team_id=v.team_id AND m.user_id=$2))''',
        version_id, profile.id, profile.role == 'ORGANIZER')
    if row is None:
        raise HTTPException(404, 'Submission not found')
    return row


class Detail(BaseModel):
    job_status: str | None
    attempts: int
    findings: list[str]
    completed_at: datetime | None
    previews: list[str]
    upload_session_id: UUID | None


@router.get('/submissions/{version_id}/details', response_model=Detail)
async def details(version_id: UUID, request: Request, profile: Profile = Depends(current_profile)):
    async with transaction(request) as c:
        await visible_version(c, version_id, profile)
        job = await c.fetchrow('SELECT status,attempts,result FROM validation_jobs WHERE version_id=$1', version_id)
        artifacts = await c.fetch('SELECT artifact_type FROM validation_artifacts WHERE version_id=$1', version_id)
        session = await c.fetchval('SELECT id FROM upload_sessions WHERE version_id=$1', version_id)
        finished = await c.fetchval("SELECT max(created_at) FROM audit_events WHERE entity_id=$1 AND event_type='VALIDATION_COMPLETED'", version_id)
    result = json.loads(job['result']) if job and job['result'] else {}
    return {'job_status': job['status'] if job else None, 'attempts': job['attempts'] if job else 0,
            'findings': result.get('findings', []), 'completed_at': finished,
            'previews': [r['artifact_type'] for r in artifacts], 'upload_session_id': session}


def read_preview(client, bucket, key):
    obj = client.get_object(Bucket=bucket, Key=key)
    with obj['Body'] as body:
        data = body.read(262145)
    if len(data) > 262144:
        raise HTTPException(503, 'Preview unavailable')
    return data


@router.get('/submissions/{version_id}/preview/{kind}')
async def preview(version_id: UUID, kind: str, request: Request, profile: Profile = Depends(current_profile)):
    if kind not in ('ZIP_PREVIEW', 'PDF_PREVIEW'):
        raise HTTPException(404, 'Preview not found')
    async with transaction(request) as c:
        await visible_version(c, version_id, profile)
        key = await c.fetchval('SELECT object_key FROM validation_artifacts WHERE version_id=$1 AND artifact_type=$2', version_id, kind)
    if not key:
        raise HTTPException(404, 'Preview not available yet')
    data = await asyncio.to_thread(read_preview, request.app.state.storage, request.app.state.settings.R2_BUCKET, key)
    return Response(data, media_type='text/html' if kind == 'ZIP_PREVIEW' else 'image/png', headers={
        'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'"})


class Download(BaseModel):
    url: str
    expires_at: datetime
    expires_in_seconds: int


@router.get('/submissions/{version_id}/download-url', response_model=Download)
async def download(version_id: UUID, request: Request, profile: Profile = Depends(current_profile)):
    async with transaction(request) as c:
        row = await visible_version(c, version_id, profile)
        if row['lifecycle'] != 'FINALIZED':
            raise HTTPException(409, 'Submission is not finalized')
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=60)
    url = await asyncio.to_thread(request.app.state.storage.generate_presigned_url, 'get_object',
        Params={'Bucket': request.app.state.settings.R2_BUCKET, 'Key': row['object_key'],
                'ResponseContentDisposition': 'attachment'}, ExpiresIn=60)
    return {'url': url, 'expires_at': expires_at, 'expires_in_seconds': 60}


class LedgerRow(BaseModel):
    team_id: UUID
    team_name: str
    version_id: UUID | None
    version_no: int | None
    title: str | None
    validation_status: str | None
    finalized_at: datetime | None
    latest_version_no: int | None


class Ledger(BaseModel):
    rows: list[LedgerRow]
    next_after: UUID | None


LEDGER = '''SELECT t.id AS team_id,t.name AS team_name,v.id AS version_id,v.version_no,v.title,
    v.validation_status,v.finalized_at,
    (SELECT max(version_no) FROM submission_versions WHERE team_id=t.id) AS latest_version_no
    FROM teams t LEFT JOIN LATERAL (SELECT id,version_no,title,validation_status,finalized_at
    FROM submission_versions WHERE team_id=t.id AND lifecycle='FINALIZED' AND admitted_at<=deadline_at
    ORDER BY version_no DESC LIMIT 1) v ON true
    WHERE ($1::uuid IS NULL OR t.id>$1) AND strpos(lower(t.name),lower($2))>0
    AND ($3::text='' OR v.validation_status=$3) ORDER BY t.id LIMIT $4'''


@router.get('/organizer/submissions', response_model=Ledger)
async def ledger(request: Request, profile: Profile = Depends(organizer), after: UUID | None = None,
                 search: str = Query(default='', max_length=100),
                 status: str = Query(default='', pattern='^(|CHECKING|ACCEPTED|NEEDS_FIX)$'),
                 limit: int = Query(default=50, ge=1, le=100)):
    async with transaction(request) as c:
        rows = await c.fetch(LEDGER, after, search, status, limit+1)
    return {'rows': [dict(r) for r in rows[:limit]], 'next_after': rows[limit-1]['team_id'] if len(rows)>limit else None}


@router.get('/organizer/teams/{team_id}/versions', response_model=History)
async def organizer_versions(team_id: UUID, request: Request, profile: Profile = Depends(organizer),
                             before: int | None = Query(default=None, ge=1)):
    from submissions import History, VersionView
    async with transaction(request) as c:
        rows = await c.fetch('''SELECT id,version_no,title,description,links,filename,expected_size AS size_bytes,
            content_type,lifecycle,validation_status,admitted_at,finalized_at FROM submission_versions
            WHERE team_id=$1 AND ($2::integer IS NULL OR version_no<$2) ORDER BY version_no DESC LIMIT 101''', team_id, before)
    return History(versions=[VersionView(**{**dict(r), 'links': json.loads(r['links'])}) for r in rows[:100]],
                   next_before_version_no=rows[99]['version_no'] if len(rows)>100 else None)


def csv_cell(value):
    text = '' if value is None else str(value)
    # Also guard formulas hidden behind whitespace/control characters.
    return "'"+text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text


@router.get('/organizer/export.csv')
async def export(request: Request, profile: Profile = Depends(organizer)):
    async def chunks():
        columns = list(LedgerRow.model_fields)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        yield output.getvalue()
        after = None
        while True:
            async with transaction(request) as c:
                # Recheck privilege for each bounded page; no transaction spans a yield.
                if await c.fetchval('SELECT role FROM users WHERE id=$1', profile.id) != 'ORGANIZER':
                    return
                rows = await c.fetch(LEDGER, after, '', '', 100)
            if not rows:
                return
            output.seek(0)
            output.truncate()
            writer.writerows([csv_cell(r[k]) for k in columns] for r in rows)
            yield output.getvalue()
            after = rows[-1]['team_id']
    return StreamingResponse(chunks(), media_type='text/csv', headers={
        'Content-Disposition': 'attachment; filename="deadline-crush.csv"', 'Cache-Control': 'private, no-store'})
