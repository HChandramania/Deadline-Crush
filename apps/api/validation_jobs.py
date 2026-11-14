"""Production PostgreSQL queue. At-least-once execution, fenced terminal writes."""
import json
import random
from deadline_crush_spikes import db


class Queue:
    def __init__(self, pool, schema, lease_seconds=60, max_attempts=5):
        if not 3 <= lease_seconds <= 600 or not 1 <= max_attempts <= 20:
            raise ValueError('Invalid queue limits')
        self.pool, self.schema = pool, schema
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts

    async def dispatch(self, limit=20):
        # Delivery to the existing PostgreSQL job is itself transactional. No
        # external broker or network operation belongs inside this transaction.
        async with db.transaction(self.pool, self.schema) as c:
            rows = await c.fetch('''SELECT o.id,o.aggregate_id FROM outbox o
                JOIN validation_jobs j ON j.version_id=o.aggregate_id
                WHERE o.event_type='VALIDATE' AND o.available_at<=clock_timestamp()
                 AND (o.status='PENDING' OR (o.status='SENDING' AND o.lease_until<clock_timestamp()))
                ORDER BY o.available_at,o.id FOR UPDATE OF o SKIP LOCKED LIMIT $1''', limit)
            for row in rows:
                await c.execute("UPDATE outbox SET status='SENT',attempts=attempts+1,lease_until=NULL WHERE id=$1", row['id'])
            return len(rows)

    async def claim(self, owner):
        async with db.transaction(self.pool, self.schema) as c:
            # Exhausted crashed attempts are retained; infrastructure failure
            # deliberately leaves the version CHECKING.
            await c.execute('''WITH exhausted AS (
                SELECT id FROM validation_jobs WHERE attempts >= $1
                 AND ((status='RUNNING' AND lease_until<=clock_timestamp()) OR status='PENDING')
                FOR UPDATE SKIP LOCKED LIMIT 20)
                UPDATE validation_jobs j SET status='FAILED',lease_owner=NULL,lease_until=NULL,
                 last_error='RETRY_EXHAUSTED' FROM exhausted e WHERE j.id=e.id''', self.max_attempts)
            row = await c.fetchrow('''WITH candidate AS (
                SELECT j.id,j.status='RUNNING' AS recovered FROM validation_jobs j
                JOIN submission_versions v ON v.id=j.version_id
                WHERE j.attempts<$3 AND v.lifecycle='FINALIZED'
                 AND EXISTS(SELECT 1 FROM outbox o WHERE o.event_type='VALIDATE'
                    AND o.aggregate_id=j.version_id AND o.status='SENT')
                 AND ((j.status='PENDING' AND j.available_at<=clock_timestamp())
                   OR (j.status='RUNNING' AND j.lease_until<=clock_timestamp()))
                ORDER BY j.available_at,j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1)
                UPDATE validation_jobs j SET status='RUNNING',attempts=attempts+1,
                 lease_owner=$1,lease_token=lease_token+1,
                 lease_until=clock_timestamp()+$2*interval '1 second'
                FROM candidate c WHERE j.id=c.id RETURNING j.*,c.recovered''',
                owner, self.lease_seconds, self.max_attempts)
            if row and row['recovered']:
                await c.execute('''INSERT INTO audit_events(event_type,entity_id)
                    VALUES('VALIDATION_LEASE_RECOVERED',$1)''', row['id'])
            return dict(row) if row else None

    async def heartbeat(self, job):
        async with db.transaction(self.pool, self.schema) as c:
            return await c.fetchval('''UPDATE validation_jobs SET
                lease_until=clock_timestamp()+$4*interval '1 second'
                WHERE id=$1 AND lease_owner=$2 AND lease_token=$3
                 AND status='RUNNING' AND lease_until>clock_timestamp() RETURNING true''',
                job['id'], job['lease_owner'], job['lease_token'], self.lease_seconds) is True

    async def source(self, job):
        async with db.transaction(self.pool, self.schema) as c:
            row = await c.fetchrow('''SELECT v.*,s.id AS session_id,s.part_size,s.expected_parts,
                s.r2_upload_id,i.manifest FROM submission_versions v
                JOIN upload_sessions s ON s.version_id=v.id AND s.object_key=v.object_key
                JOIN completion_intents i ON i.upload_session_id=s.id
                JOIN validation_jobs j ON j.version_id=v.id
                WHERE j.id=$1 AND j.lease_owner=$2 AND j.lease_token=$3
                 AND j.status='RUNNING' AND j.lease_until>clock_timestamp()
                 AND v.lifecycle='FINALIZED' AND s.status='COMPLETED' AND i.status='FINALIZED' ''',
                job['id'], job['lease_owner'], job['lease_token'])
            if row is None:
                raise RuntimeError('SOURCE_NOT_AUTHORIZED')
            return dict(row)

    async def complete(self, job, result, artifacts=()):
        encoded = json.dumps(result, allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError('Result exceeds bound')
        if result['status'] not in ('ACCEPTED', 'NEEDS_FIX'):
            raise ValueError('Invalid validation status')
        prefix = f"previews/{job['version_id']}/{job['id']}/{job['lease_token']}/"
        if any(not key.startswith(prefix) or kind not in ('ZIP_PREVIEW','PDF_PREVIEW')
               for kind, key in artifacts) or len(artifacts)>2:
            raise ValueError('Invalid attempt artifact')
        async with db.transaction(self.pool, self.schema) as c:
            won = await c.fetchval('''UPDATE validation_jobs SET status='SUCCEEDED',
                result=$4::jsonb,last_error=NULL,lease_owner=NULL,lease_until=NULL
                WHERE id=$1 AND lease_owner=$2 AND lease_token=$3 AND status='RUNNING'
                 AND lease_until>clock_timestamp() RETURNING true''',
                job['id'], job['lease_owner'], job['lease_token'], encoded)
            if not won:
                await c.execute("INSERT INTO audit_events(event_type,entity_id) VALUES('VALIDATION_STALE_REJECTED',$1)", job['id'])
                return False
            team = await c.fetchval('''UPDATE submission_versions SET validation_status=$2
                WHERE id=$1 AND lifecycle='FINALIZED' AND validation_status='CHECKING' RETURNING team_id''',
                job['version_id'], result['status'])
            if team is None:
                raise RuntimeError('INVALID_VALIDATION_TRANSITION')
            for kind, key in artifacts:
                if not await c.fetchval('''SELECT true FROM validation_preview_intents
                    WHERE object_key=$1 AND job_id=$2 AND generation=$3 AND artifact_type=$4''',
                    key,job['id'],job['lease_token'],kind):
                    raise ValueError('Unstaged artifact')
                await c.execute('''INSERT INTO validation_artifacts(version_id,artifact_type,object_key)
                    VALUES($1,$2,$3)''', job['version_id'], kind, key)
            await c.execute('''INSERT INTO audit_events(team_id,event_type,entity_id,detail)
                VALUES($1,'VALIDATION_COMPLETED',$2,$3::jsonb)''', team, job['version_id'],
                json.dumps({'job_id': str(job['id']), 'generation': job['lease_token'], 'status': result['status']}))
            return True

    async def stage(self,job,kind,key):
        prefix = f"previews/{job['version_id']}/{job['id']}/{job['lease_token']}/"
        if not key.startswith(prefix) or kind not in ('ZIP_PREVIEW','PDF_PREVIEW'):
            raise ValueError('Invalid attempt artifact')
        async with db.transaction(self.pool,self.schema) as c:
            return await c.fetchval('''INSERT INTO validation_preview_intents(object_key,job_id,generation,artifact_type)
                SELECT $4,id,lease_token,$5 FROM validation_jobs WHERE id=$1 AND lease_owner=$2
                 AND lease_token=$3 AND status='RUNNING' AND lease_until>clock_timestamp()
                ON CONFLICT DO NOTHING RETURNING true''',job['id'],job['lease_owner'],job['lease_token'],key,kind) is True

    async def abandoned_previews(self):
        async with db.transaction(self.pool,self.schema) as c:
            # Retain rows after deletion: a stalled old PUT may arrive late.
            # Repeated exact-key deletion never touches an authoritative artifact.
            rows = await c.fetch('''SELECT p.object_key FROM validation_preview_intents p
                JOIN validation_jobs j ON j.id=p.job_id
                WHERE p.created_at<clock_timestamp()-interval '1 day'
                 AND NOT (j.status='RUNNING' AND j.lease_token=p.generation AND j.lease_until>clock_timestamp())
                 AND NOT EXISTS(SELECT 1 FROM validation_artifacts a WHERE a.object_key=p.object_key)
                ORDER BY p.created_at LIMIT 20''')
            return [r['object_key'] for r in rows]

    async def retry(self, job, category):
        # Categories are selected by code, never SDK exception text.
        if category not in ('STORAGE_UNAVAILABLE','DATABASE_UNAVAILABLE','PROCESSING_UNAVAILABLE',
                            'LINK_TEMPORARY','SOURCE_IDENTITY_MISMATCH','RENDERER_CRASH'):
            category = 'PROCESSING_UNAVAILABLE'
        delay = min(300, 2 ** min(job['attempts'], 8)) * random.uniform(0.75, 1.25)
        async with db.transaction(self.pool, self.schema) as c:
            return await c.fetchval('''UPDATE validation_jobs SET
                status=CASE WHEN attempts >= $5 THEN 'FAILED' ELSE 'PENDING' END,
                available_at=clock_timestamp()+$4*interval '1 second',last_error=$6,
                lease_owner=NULL,lease_until=NULL
                WHERE id=$1 AND lease_owner=$2 AND lease_token=$3 AND status='RUNNING'
                 AND lease_until>clock_timestamp() RETURNING true''',
                job['id'], job['lease_owner'], job['lease_token'], delay, self.max_attempts, category) is True

    async def metrics(self):
        async with db.transaction(self.pool, self.schema) as c:
            return dict(await c.fetchrow('''SELECT
                count(*) FILTER(WHERE status='PENDING') AS pending,
                count(*) FILTER(WHERE status='RUNNING' AND lease_until>clock_timestamp()) AS active_leases,
                count(*) FILTER(WHERE status='PENDING' AND attempts>0) AS retry_depth,
                count(*) FILTER(WHERE status='FAILED') AS failed,
                count(*) FILTER(WHERE status='SUCCEEDED' AND result->>'status'='ACCEPTED') AS accepted,
                count(*) FILTER(WHERE status='SUCCEEDED' AND result->>'status'='NEEDS_FIX') AS needs_fix,
                avg(CASE WHEN result->>'duration_ms' ~ '^[0-9]+([.][0-9]+)?$'
                    THEN (result->>'duration_ms')::double precision END) AS mean_duration_ms,
                (SELECT count(*) FROM audit_events WHERE event_type='VALIDATION_LEASE_RECOVERED') AS recovered,
                (SELECT count(*) FROM audit_events WHERE event_type='VALIDATION_STALE_REJECTED') AS stale_rejections
                FROM validation_jobs'''))
