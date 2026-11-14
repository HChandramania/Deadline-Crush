# TRD.md — Deadline Crush Technical Requirements & Design

**Version:** 3.0 | **Status:** implementation-ready design, runtime assumptions require Gate 0 verification.

## 1. Architecture
- React/TypeScript SPA on Cloudflare Pages.
- FastAPI/Uvicorn on standard CPython hosted on Render or Railway for auth, versioning, signing, completion, status, organizer APIs.
- Neon PostgreSQL (authoritative ledger) directly via a pooled Neon connection string and asyncpg; use short-lived transactions and bounded application-level connection pools.
- Private Cloudflare R2 via boto3/S3-compatible operations for direct browser multipart uploads and immutable completed objects/previews.
- PostgreSQL-backed durable jobs for at-least-once processing. Transactional PostgreSQL outbox bridges database commit to job dispatch. Dedicated Python worker process; use isolated validation service if hosting resource limits make ZIP/PDF processing unsafe.
- A dedicated Python dispatcher/reconciler process scans pending outbox rows, uncertain R2 operations, and expired job leases. Use bounded polling and graceful shutdown.

### Deployment feasibility gate (must execute first)
1. Deploy `GET /health` FastAPI/Uvicorn CPython service on Render or Railway.
2. Connect directly to Neon using a bounded asyncpg pool, perform insert + transaction + rollback + row lock; measure warm/cold latency.
3. Prove R2 `CreateMultipartUpload`, signed `UploadPart`, `ListParts`, `CompleteMultipartUpload`, `HeadObject`, `AbortMultipartUpload`, and browser CORS. Use boto3/S3-compatible operations in CPython; verify signing and transport against actual R2.
4. Enqueue and claim a PostgreSQL-backed job, including duplicate execution and retry behavior; verify the dedicated Python worker process.
5. Attempt safe ZIP validation and PDF preview with realistic files, enforcing execution/memory limits. If unsupported, isolate in a container service; retain PostgreSQL job orchestration and the API on Render or Railway.
6. Confirm Render/Railway plan quotas (CPU, memory, request duration, persistent background processes, sleeping), R2 operation quotas, Neon free limits, and CORS on actual accounts. Record results, not assumptions.

## 2. Database schema (PostgreSQL)
Use `uuid` PKs, `timestamptz`, `bigint` sizes, UTC timestamps, parameterized queries, `CHECK`, `UNIQUE`, foreign keys, and migrations. Prefer `gen_random_uuid()` and `clock_timestamp()`.

```sql
CREATE TABLE hackathons (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 name text NOT NULL, deadline_at timestamptz NOT NULL,
 grace_seconds integer NOT NULL DEFAULT 600 CHECK (grace_seconds = 600),
 max_file_bytes bigint NOT NULL DEFAULT 104857600 CHECK (max_file_bytes > 0)
);
CREATE TABLE users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 auth_issuer text NOT NULL, auth_subject text NOT NULL,
 email text NOT NULL DEFAULT '',
 role text NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('MEMBER','ORGANIZER')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX users_external_identity ON users (auth_issuer,auth_subject);
CREATE TABLE teams (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 hackathon_id uuid NOT NULL REFERENCES hackathons(id),
 name text NOT NULL,
 next_version_no integer NOT NULL DEFAULT 1 CHECK (next_version_no > 0),
 selected_version_id uuid,
 selected_version_no integer,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE team_members (
 team_id uuid NOT NULL REFERENCES teams(id),
 user_id uuid NOT NULL REFERENCES users(id),
 PRIMARY KEY (team_id,user_id)
);
CREATE TABLE submission_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid NOT NULL REFERENCES teams(id),
 version_no integer NOT NULL CHECK (version_no > 0),
 idempotency_key uuid NOT NULL, request_hash char(64) NOT NULL,
 title text NOT NULL, description text NOT NULL DEFAULT '',
 links jsonb NOT NULL DEFAULT '[]'::jsonb,
 object_key text NOT NULL UNIQUE,
 expected_size bigint NOT NULL CHECK (expected_size > 0 AND expected_size <= 104857600),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 admitted_at timestamptz NOT NULL,
 finalized_at timestamptz,
 lifecycle text NOT NULL DEFAULT 'CREATED' CHECK (lifecycle IN ('CREATED','UPLOADING','COMPLETING','FINALIZED','EXPIRED','ABORTED')),
 validation_status text NOT NULL DEFAULT 'NOT_STARTED' CHECK (validation_status IN ('NOT_STARTED','CHECKING','ACCEPTED','NEEDS_FIX')),
 verified_sha256 char(64),
 UNIQUE (team_id,version_no), UNIQUE (team_id,idempotency_key),
 UNIQUE (team_id,id)
);
ALTER TABLE teams ADD CONSTRAINT selected_version_same_team
 FOREIGN KEY (id,selected_version_id) REFERENCES submission_versions(team_id,id)
 DEFERRABLE INITIALLY IMMEDIATE;
CREATE TABLE upload_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL UNIQUE REFERENCES submission_versions(id),
 object_key text NOT NULL UNIQUE,
 r2_upload_id text,
 status text NOT NULL CHECK (status IN ('INITIATING','ACTIVE','COMPLETING','COMPLETED','ABORTED','EXPIRED')),
 part_size integer NOT NULL CHECK (part_size >= 5242880),
 expected_parts integer NOT NULL CHECK (expected_parts BETWEEN 1 AND 10000),
 expires_at timestamptz NOT NULL,
 completed_at timestamptz,
 last_error text
);
CREATE TABLE completion_intents (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 upload_session_id uuid NOT NULL UNIQUE REFERENCES upload_sessions(id),
 idempotency_key uuid NOT NULL UNIQUE,
 manifest jsonb NOT NULL, manifest_sha256 char(64) NOT NULL,
 admitted_at timestamptz NOT NULL,
 status text NOT NULL CHECK (status IN ('ADMITTED','STORAGE_COMPLETED','FINALIZED','RECOVERY_REQUIRED','FAILED')),
 attempt_count integer NOT NULL DEFAULT 0,
 last_error text
);
CREATE TABLE validation_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL UNIQUE REFERENCES submission_versions(id),
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
 attempts integer NOT NULL DEFAULT 0,
 lease_owner uuid, lease_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 result jsonb, last_error text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX validation_jobs_ready ON validation_jobs(available_at,created_at)
 WHERE status IN ('PENDING','RUNNING');
CREATE TABLE outbox (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 event_type text NOT NULL, aggregate_id uuid NOT NULL,
 payload jsonb NOT NULL DEFAULT '{}'::jsonb,
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENDING','SENT')),
 attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (event_type,aggregate_id)
);
CREATE INDEX outbox_pending ON outbox(available_at) WHERE status IN ('PENDING','SENDING');
CREATE TABLE validation_artifacts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL REFERENCES submission_versions(id),
 artifact_type text NOT NULL, object_key text NOT NULL,
 UNIQUE(version_id,artifact_type)
);
CREATE TABLE audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid REFERENCES teams(id), actor_id uuid REFERENCES users(id),
 event_type text NOT NULL, entity_id uuid, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE late_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid REFERENCES teams(id), actor_id uuid REFERENCES users(id),
 action text NOT NULL, reason text NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
```

**Migration hardening:** Add immutable-column triggers for finalized versions and completion manifest, state-transition constraints in service logic, an audit append-only DB role, and a deferred constraint/trigger validating `selected_version_no` matches the referenced version. Do not permit an organizer to change the deadline silently after candidate creation. If deadline configuration changes, require a separate audited policy operation. Do not assume the SQL above alone enforces every invariant.

## 3. Admission, selection and deadline invariants
- `D = hackathons.deadline_at`, `G = interval '600 seconds'`.
- **Candidate admission:** transaction locks `teams` row (`FOR UPDATE`), checks `clock_timestamp() <= D` *after lock acquisition*, allocates `next_version_no`, inserts frozen version, upload-session intent and audit, then commits. This defines a **gate-time**, not strict commit-time, cutoff. A lock acquired before D with a commit after D remains a known theoretical boundary risk; do not claim otherwise.
- **Grace admission:** server checks `clock_timestamp() <= D+G` after locking `upload_sessions`; only pre-deadline versions; observe R2 `ListParts` outside any database transaction, then reacquire team/session locks, recheck database time and unchanged authorization, and verify the observation equals the proposed complete, ordered manifest before writing immutable `completion_intents` row. To prevent races, stop signing new parts once completion begins, but already-issued URLs may still write: compare ETags at final completion, reject changed manifest and do not silently include new parts.
- **Completion:** once intent is durably admitted, identical retries may continue after grace; no new manifest or parts permitted. Verify completed object via `HEAD` size/key and separate cryptographic hash as required; R2 multipart ETag is not SHA-256.
- **Selection:** in one transaction lock team and version, mark version FINALIZED, create unique job + outbox event + audit, and update selected version iff `new_version_no > selected_version_no`. Highest eligible FINALIZED version wins even if validation later NEEDS_FIX.
- **Durability:** `202 completion-intent admitted` means only durable intent, **not** finalized submission. `200/201 finalized` means R2 object verified and version/job/outbox committed. Return distinct statuses.
- **Cross-system race:** PostgreSQL and R2 have no distributed transaction. Reconciler retries uncertain R2 completion, checks object via HEAD, and finalizes from durable intent. Never acknowledge finalization before database commit.

## 4. API contract (prefix `/api/v1`)
All authenticated endpoints use bearer JWT. Submission routes require a team_id UUID query parameter (the smallest compatible addition to existing request bodies and paths); it selects a team but grants no authority. Every operation verifies current database membership and derives hackathon association from that team. Reads of submissions allow ordinary members. Every submission mutation (version creation/edit through new versions, upload authorization, resume and completion/finalization) requires both current CAPTAIN membership and matching teams.captain_id for the authenticated application user, locked through the transition and rechecked after external observations and on retries. Client/JWT role claims and organizer status grant no bypass. This supersedes the earlier all-members submission policy. Team/hackathon version allocation and principal-scoped admission idempotency use this verified association. Mutations accept `Idempotency-Key` UUID; canonical request hash stored and checked. Standard errors: `{ "error": {"code":"...", "message":"...", "request_id":"..."} }`. Times ISO-8601 UTC.

### Phase 1 managed identity and teams
Managed Better Auth exclusively owns passwords and sessions in neon_auth. No custom signup/login, password storage, JWT issuance or neon_auth migrations. Browser uses the installed @neon/auth client getSession().data.session.token to obtain an access JWT, then sends Authorization: Bearer to FastAPI. FastAPI restricts EdDSA/Ed25519, verifies kid/signature/issuer/audience/expiration/iat and nbf when present against configured branch JWKS. Issuer and audience are the branch Auth URL origin (confirmed against live managed token before release). Subject is opaque text; application identity is unique (auth_issuer,auth_subject), never email. Roles come from PostgreSQL, never token or request role claims. JWKS cache: five minutes, at most 16 keys/64 KiB, five-second network timeout and ten-second refetch cooldown; fail closed. Logout clears frontend identity; issued JWTs remain valid until expiration.

Phase 1 routes: GET /users/me provisions a MEMBER profile idempotently; GET /teams lists own teams; POST /teams accepts {hackathon_id,name} plus Idempotency-Key and atomically creates captain membership/audit (201; identical retry 200; conflicting retry 409). GET /teams/{team_id} and GET /teams/{team_id}/members require current database membership, returning 404 for inaccessible teams. GET /organizer/metrics requires database ORGANIZER. No public role or membership mutations. Organizer provisioning uses an explicit privileged database operation and audit. Captain assignment is immutable through public APIs; invitation/removal rules are deferred. Full reviewed schema is in migrations; illustrative SQL below omits these added team ownership fields. No candidate/upload/finalization lifecycle in Phase 1.

### `POST /submissions/versions`
Request: `{ "title":"Project", "description":"...", "links":["https://example.com"], "filename":"project.zip", "size_bytes":7340032, "content_type":"application/zip" }`.
Response `201`: `{ "version_id":"uuid", "version_no":2, "upload_session_id":"uuid", "status":"CREATED", "deadline_at":"...", "grace_until":"...", "part_size_bytes":8388608 }`.
Retry same key+payload returns same resource (`200`); same key+different payload => `409 IDEMPOTENCY_CONFLICT`; late => `409 DEADLINE_CLOSED`; oversize => `413 FILE_TOO_LARGE`.

### `POST /uploads/{session_id}/parts/sign`
Request: `{ "part_numbers":[1,2] }`.
Response `200`: `{ "parts":[{"part_number":1,"url":"https://...","expires_at":"..."}] }`.
Authorize the active team captain using the shared locked mutation check; ensure session ACTIVE, requested part numbers within expected range and signing time within policy. Do not log URLs.

### `GET /uploads/{session_id}`
Resume/authoritative part listing requires the active team captain; ordinary members view status/history through submission read endpoints. Require explicit team_id and verify session ownership server-side.
Response `200`: `{ "status":"ACTIVE", "part_size_bytes":8388608, "expected_parts":2, "completed_parts":[{"part_number":1,"etag":"...","size_bytes":8388608}], "grace_until":"..." }`. Server queries R2 ListParts; pagination required. Never trust browser-local part list as authoritative.

### `POST /uploads/{session_id}/complete`
Request: `{ "parts":[{"part_number":1,"etag":"..."},{"part_number":2,"etag":"..."}] }`.
Response `202`: `{ "completion_intent_id":"uuid", "status":"ADMITTED", "status_url":"/api/v1/uploads/{session_id}" }`.
After R2 completion and Neon finalization, `GET /submissions/current` reports FINALIZED. If already finalized, retry returns `200` with existing result. Late first intent => `409 GRACE_EXPIRED`; inconsistent manifest => `409 PARTS_MISMATCH`.

### `GET /submissions/current`
Response `200`: `{ "selected_version_id":"uuid", "version_no":2, "lifecycle":"FINALIZED", "validation_status":"CHECKING", "finalized_at":"...", "validation_errors":[] }` or `selected_version_id:null`.

### Other endpoints
- `GET /submissions/versions` -> `{ "versions":[{ "id":"uuid", "version_no":1, "lifecycle":"FINALIZED", "validation_status":"ACCEPTED" }] }`.
- `GET /submissions/{version_id}/download-url` -> `{ "url":"...", "expires_at":"..." }` after team authorization.
- `GET /organizer/metrics` -> `{ "teams":2000, "finalized_versions":..., "late_attempts":..., "failed_checks":..., "queue_pending":... }` (values calculated, never hardcoded).
- `GET /organizer/export.csv` -> CSV, formula-escaped, organizer-only.
- `GET /health` -> no secret disclosure; readiness checks should not leak internals.

## 5. R2 multipart and recovery
- Use 8 MiB parts (last part may be smaller); enforce max file 100 MiB. Reserve a unique opaque key such as `submissions/{hackathon_uuid}/{team_uuid}/{version_uuid}/source`.
- `INITIATING` DB row precedes external R2 create. Reconcile crash after create; use tagged/identifiable object keys and cleanup stale multipart sessions. Do not assume S3 APIs are fully supported by R2 without integration tests.
- Freeze the session identity, expected part range, part size, object key, size and grace cutoff at pre-deadline admission. Grace permits uploading/retrying missing parts only within that existing authorization; no new session or expanded range. Sign each part only for authenticated session and authorized part number; browser PUTs bytes directly to R2. Configure exact CORS origin, methods, headers and exposed ETag.
- On resume query R2 `ListParts` and reconcile with expected size/part count. Do not trust browser ETags or completion status.
- Freeze complete manifest in DB before R2 CompleteMultipartUpload. If response is lost, HEAD object and verify; if absent, retry same completion manifest or surface recoverable failure. Never change the manifest on retry.
- Completed R2 objects are immutable by application policy. If the storage platform permits overwriting a key, use IAM restrictions or unique keys and refuse repeated create/overwrite paths. Signed URLs can remain valid until expiry, so avoid presigning beyond policy and validate completed object identity.
- Clean up expired multipart uploads and orphaned objects only after verifying no finalized or recoverable intent references them.

## 6. Durable queue and worker state
**Outbox:** Finalization transaction inserts `validation_jobs(version_id UNIQUE)` and `outbox(event_type='VALIDATE',aggregate_id=version_id UNIQUE)`. A dedicated Python dispatcher claims outbox rows with `FOR UPDATE SKIP LOCKED`, marks SENDING with lease, makes the durable PostgreSQL job available, then marks SENT. If dispatch succeeds but acknowledgment/DB update fails, duplicate execution is expected and harmless. Expired SENDING rows are redispatched.

**Consumer:** The dedicated Python process polls PostgreSQL and attempts atomic job claim; terminal jobs require no work. For long processing, check/extend lease. On crash, retry or periodic recovery makes expired jobs available again. Hosting execution limits and retry semantics must be verified; do not assume a Python worker can run unbounded validation.

Example job claim (run in a short Neon transaction):
```sql
WITH candidate AS (
 SELECT id FROM validation_jobs
 WHERE (status='PENDING' AND available_at <= clock_timestamp())
    OR (status='RUNNING' AND lease_until < clock_timestamp())
 ORDER BY available_at, created_at
 FOR UPDATE SKIP LOCKED LIMIT 1
)
UPDATE validation_jobs j SET status='RUNNING',
 attempts=attempts+1, lease_token=lease_token+1,
 lease_owner=$1, lease_until=clock_timestamp()+interval '60 seconds'
FROM candidate WHERE j.id=candidate.id
RETURNING j.*;
```
**Fencing:** final write must require `id`, `lease_owner`, `lease_token`, `status='RUNNING'`. Use immutable attempt-specific preview object keys; only winner's committed artifact reference becomes visible. Validation failures are `NEEDS_FIX` with job SUCCEEDED; transient infrastructure failures retry and may ultimately become job FAILED without pretending the file is invalid.

## 7. Validation security
- Detect PDF/ZIP by magic bytes and structural checks; do not trust extension/MIME.
- ZIP: max 100 MiB compressed, bounded total uncompressed size (e.g., 500 MiB), max entries (e.g., 2,000), depth, compression ratio, no traversal, absolute paths, symlinks, encrypted or nested archives without explicit policy. Avoid unbounded extraction.
- URL checks: HTTPS only where practical; reject localhost, private/link-local/reserved ranges, cloud metadata endpoints, and DNS rebinding; validate every redirect; bound redirects, response bytes and timeouts.
- PDF: safe first-page rendering in isolated environment with memory/CPU/time limits. If the dedicated Python worker cannot safely run the renderer within hosting resource limits, use a container service; do not disable preview requirement silently.
- SHA-256: compute by bounded streaming; never assume multipart ETag equals file checksum.
- Preview HTML is escaped/sanitized; downloadable objects require authorization and short-lived signed URLs.

## 8. Tests and observability
Minimum 20 CI tests; target >=35: auth/team isolation (5), version/deadline (8), multipart/resume (7), finalization/idempotency (6), worker/outbox/fencing (6), validation security (6), organizer/export (3). Add deployed integration tests for direct Neon/asyncpg pooling, R2 multipart/CORS and PostgreSQL jobs.

Failure injection: concurrent version creation; two versions finalize out of order; lock crosses deadline; complete at grace cutoff; crash after R2 complete before DB finalize; duplicate queue delivery; worker killed mid-job; stale lease holder resumes; malformed ZIP; cross-team download attempt.

Load: k6 2,000 distinct teams in 600s, realistic burst ramp, separate control-plane p95 (<300ms target) and direct R2 transfer measurements. Capture HTTP errors, Neon lock waits, queue lag, R2 failures, retry counts. Reconcile acknowledged finalizations to durable versions, verified R2 objects, unique logical jobs, and terminal outcomes; report limitations honestly. Free-tier quotas and cold starts can affect results.

## 9. Implementation phases
0. **Feasibility spike** (highest priority): FastAPI/Uvicorn CPython service, direct Neon/asyncpg transaction and pooling, boto3 R2 multipart, PostgreSQL jobs, ZIP/PDF feasibility.
1. **Schema + identity**: migrations, auth, membership, immutable version creation, admission tests.
2. **Storage**: R2 multipart sign/list/resume, immutable completion intent, finalize/reconcile.
3. **Async processing**: outbox, dispatcher, PostgreSQL job consumer, fenced jobs, validation/preview.
4. **Product**: team UI, status polling, organizer dashboard/CSV.
5. **Hardening**: integration and concurrency tests, k6, CI, deployment, documentation, video.

## 10. Deployment and known tradeoffs
- Configure environment variables and secrets for Render/Railway web and dedicated Python worker processes; Neon and R2 credentials are server-only. Restrict R2 bucket to private access; browser gets signed capabilities only.
- Use separate environment configurations for preview/production; ensure migrations run before deployment.
- No guarantee of sub-300ms p95 on free tiers or cold starts; measure and disclose.
- No cross-system atomicity between Neon and R2: outbox, completion intent, retries, and reconciliation provide eventual consistency.
- PostgreSQL job at-least-once execution semantics are expected; 'exactly once' applies to logical persisted outcomes, not executions.
- **Open feasibility risk:** Render/Railway service plans must support the FastAPI/Uvicorn API, boto3 R2 operations, and a persistent dedicated Python worker; Gate 0 determines hosting resource requirements and whether isolated container validation is required.
