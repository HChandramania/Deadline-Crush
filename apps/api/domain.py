"""Application-owned profiles and database-backed team authorization."""
import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from deadline_crush_spikes import db
from identity import verified_identity

router = APIRouter(prefix='/api/v1')


class Profile(BaseModel):
    id: UUID
    email: str
    role: Literal['MEMBER', 'ORGANIZER']
    created_at: datetime


class Team(BaseModel):
    id: UUID
    hackathon_id: UUID
    name: str
    captain_id: UUID
    created_at: datetime


class Teams(BaseModel):
    teams: list[Team]


class TeamInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    hackathon_id: UUID
    name: str = Field(min_length=1, max_length=100)


class Member(BaseModel):
    user_id: UUID
    role: Literal['MEMBER', 'CAPTAIN']


class Members(BaseModel):
    members: list[Member]


class Metrics(BaseModel):
    teams: int
    finalized_versions: int
    late_attempts: int
    failed_checks: int
    queue_pending: int
    validation: dict[str,int | float | None] = Field(default_factory=dict)


def transaction(request):
    return db.transaction(request.app.state.pool, request.app.state.settings.APP_SCHEMA)


async def current_profile(request: Request, claims: dict = Depends(verified_identity)) -> Profile:
    email = claims.get('email', '')
    if not isinstance(email, str) or len(email) > 254:
        email = ''
    async with transaction(request) as connection:
        # No JWT role is copied. Subject is opaque text scoped by verified issuer.
        row = await connection.fetchrow('''INSERT INTO users(auth_issuer,auth_subject,email)
            VALUES($1,$2,$3) ON CONFLICT(auth_issuer,auth_subject)
            DO UPDATE SET email=EXCLUDED.email RETURNING id,email,role,created_at''',
            claims['iss'], claims['sub'], email)
    return Profile(**dict(row))


async def organizer(profile: Profile = Depends(current_profile)) -> Profile:
    if profile.role != 'ORGANIZER':
        raise HTTPException(403, 'Organizer access required')
    return profile


@router.get('/users/me', response_model=Profile)
async def me(profile: Profile = Depends(current_profile)):
    return profile


@router.get('/teams', response_model=Teams)
async def teams(request: Request, profile: Profile = Depends(current_profile)):
    async with transaction(request) as connection:
        rows = await connection.fetch('''SELECT t.id,t.hackathon_id,t.name,t.captain_id,t.created_at
            FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.user_id=$1
            ORDER BY t.created_at,t.id LIMIT 100''', profile.id)
    return {'teams': [dict(row) for row in rows]}


async def authorized_team(team_id: UUID, request: Request, profile: Profile = Depends(current_profile)) -> Team:
    async with transaction(request) as connection:
        row = await connection.fetchrow('''SELECT t.id,t.hackathon_id,t.name,t.captain_id,t.created_at FROM teams t
            WHERE t.id=$1 AND EXISTS(SELECT 1 FROM team_members m WHERE m.team_id=t.id AND m.user_id=$2)''',
            team_id, profile.id)
    if row is None:
        raise HTTPException(404, 'Team not found')
    return Team(**dict(row))


@router.get('/teams/{team_id}', response_model=Team)
async def team(team: Team = Depends(authorized_team)):
    return team


@router.get('/teams/{team_id}/members', response_model=Members)
async def members(request: Request, team: Team = Depends(authorized_team), profile: Profile = Depends(current_profile)):
    async with transaction(request) as connection:
        # Recheck membership in the same query that reads member data.
        rows = await connection.fetch('''SELECT user_id,role FROM team_members WHERE team_id=$1
            AND EXISTS(SELECT 1 FROM team_members WHERE team_id=$1 AND user_id=$2) ORDER BY user_id LIMIT 100''',
            team.id, profile.id)
    if not rows:
        raise HTTPException(404, 'Team not found')
    return {'members': [dict(row) for row in rows]}


@router.post('/teams', response_model=Team, status_code=201)
async def create_team(body: TeamInput, request: Request, profile: Profile = Depends(current_profile),
                      idempotency_key: UUID = Header(alias='Idempotency-Key')):
    digest = hashlib.sha256(json.dumps(body.model_dump(mode='json'), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    async with transaction(request) as connection:
        # Serialize this creator's retries. No admission/deadline semantics are changed.
        await connection.fetchval('SELECT id FROM users WHERE id=$1 FOR UPDATE', profile.id)
        row = await connection.fetchrow('SELECT * FROM teams WHERE captain_id=$1 AND idempotency_key=$2', profile.id, idempotency_key)
        if row:
            if row['request_hash'] != digest:
                raise HTTPException(409, 'Idempotency key conflicts with request')
            from fastapi.responses import JSONResponse
            return JSONResponse(Team(**dict(row)).model_dump(mode='json'), status_code=200)
        if not await connection.fetchval('SELECT EXISTS(SELECT 1 FROM hackathons WHERE id=$1)', body.hackathon_id):
            raise HTTPException(404, 'Hackathon not found')
        row = await connection.fetchrow('''INSERT INTO teams(hackathon_id,name,captain_id,idempotency_key,request_hash)
            VALUES($1,$2,$3,$4,$5) RETURNING *''', body.hackathon_id, body.name, profile.id, idempotency_key, digest)
        await connection.execute("INSERT INTO team_members(team_id,user_id,role) VALUES($1,$2,'CAPTAIN')", row['id'], profile.id)
        await connection.execute("INSERT INTO audit_events(team_id,actor_id,event_type,entity_id) VALUES($1,$2,'TEAM_CREATED',$1)", row['id'], profile.id)
    return Team(**dict(row))


@router.get('/organizer/metrics', response_model=Metrics)
async def metrics(request: Request, profile: Profile = Depends(organizer)):
    async with transaction(request) as connection:
        row = await connection.fetchrow('''SELECT (SELECT count(*) FROM teams) AS teams,
          (SELECT count(*) FROM submission_versions WHERE lifecycle='FINALIZED') AS finalized_versions,
          (SELECT count(*) FROM late_attempts) AS late_attempts,
          (SELECT count(*) FROM submission_versions WHERE validation_status='NEEDS_FIX') AS failed_checks,
          (SELECT count(*) FROM validation_jobs WHERE status='PENDING') AS queue_pending''')
    from validation_jobs import Queue
    settings=request.app.state.settings
    validation=await Queue(request.app.state.pool,settings.APP_SCHEMA).metrics()
    return {**dict(row),'validation':validation}
