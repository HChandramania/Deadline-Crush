# Phase 0 CPython setup

Current storage operations and limitations: [Phase 3](PHASE_3.md). Initialize an
admitted upload before D, sign frozen parts, upload via direct PUT, resume through
authoritative status, and submit the exact ordered manifest. Keep `R2_URL_TTL_SECONDS`
at 60 by default (maximum 120). Never log URLs, SDK wire responses or credentials.

The approved localhost CORS rule is now applied and verified; see
`docs/phase3-cors-results.json` for before/after and real HTTP evidence. Production
Pages origins still require their actual exact URL and explicit configuration.
For future explicitly approved CORS maintenance, `node scripts/python.mjs cors-cli`
records a proposal using an existing current Wrangler session; `--apply` writes
only the additive localhost rule. Refresh through Wrangler itself when expired;
do not create new credentials, expose OAuth tokens or broaden bucket permissions.

Run `node scripts/python.mjs recovery` as a separate always-on CPython process with
the same APP_SCHEMA/R2 server configuration and worker pool bounds. It recovers
durable intents and sweeps expired exact-key multipart uploads; it does not process
validation jobs. `--check` checks imports/configuration, `--once` processes at most
one operation. Monitor storage_operations attempts/available_at/last_error and
pending completion intents. Repeated dependency failure retains intent and backs
off up to an hour; after restoring dependencies, operators can move available_at
forward using an explicitly reviewed application-schema operation. Never reset
manifest, session identity or fencing tokens. No automatic production migration.

Current submission authorization is captain-only for admission, upload/resume and
completion/finalization; ordinary members retain submission reads. See
[captain policy and verification](CAPTAIN_POLICY.md). Explicit team_id is required;
CAPTAIN_REQUIRED (403) means the authenticated member is not the current database
captain. A stored idempotency result does not bypass a role change. No new schema
migration or production role update is needed for this API policy change.

For the current application schema, JWT verification, teams, administrator commands and isolated integration tests, use the [Phase 1 runbook/report](PHASE_1.md). Production domain registration is deferred until the actual frontend URL exists.

Managed email signup is now configured separately: see [managed Auth setup, commands and live test evidence](MANAGED_AUTH.md). The Phase 0 instructions below describe the retained infrastructure probes.

Use CPython 3.12, uv, Node 24 and k6. Keep all values from `.env.example` server-side. Root npm commands now load Neon's `.env.local` or `.env` into server-side Python child processes, preserving existing process variables. Direct Python commands still require environment injection. No browser variable may contain database, storage or probe credentials. See [Neon setup and proof](NEON_SETUP.md).

From the repository root (PowerShell):

```powershell
uv sync --directory apps/api --locked
uv sync --directory apps/consumer --locked
apps/api/.venv/Scripts/python.exe -m pytest -q
apps/consumer/.venv/Scripts/python.exe apps/consumer/main.py --check
npm.cmd ci --prefix apps/web
npm.cmd run build --prefix apps/web
apps/api/.venv/Scripts/python.exe -m uvicorn main:app --app-dir apps/api --host 127.0.0.1 --port 8787 --no-access-log
```

In another terminal: `Invoke-RestMethod http://127.0.0.1:8787/health`; `k6 run tests/health.js`. Without DATABASE_URL, the API starts for health only; guarded dependency checks fail rather than pretending a database exists. `--check` checks worker imports/configuration only. A real polling worker requires DATABASE_URL and the isolated schema.

## Neon and worker probe

Use a disposable Neon database and TLS pooled URL (`-pooler` hostname). Certificate verification is enabled. Default pool is 0–5 connections per process; set DB_POOL_MIN/MAX explicitly for the host plan. Budget web instances × Uvicorn processes × pool maximum plus worker instances × pool maximum against Neon limits. No transaction stays open across R2 calls.

Apply `infra/phase0.sql` explicitly in Neon SQL Editor or with your migration-role connection. It creates only phase0_jobs, not submission tables. Use a direct endpoint for migrations if transaction pooling limitations affect the tool. Give runtime roles only the permissions needed for SELECT/INSERT/UPDATE on this table plus TEMP for database probes; do not put migration credentials into the browser.

Start the actual process:

```powershell
apps/consumer/.venv/Scripts/python.exe apps/consumer/main.py
```

With PROBE_TOKEN configured, POST `/probe/jobs` with `{"id":"a UUID"}` and x-probe-token. Repeating the same UUID creates one durable row. The worker claims it, retries once intentionally, then succeeds. GET `/probe/jobs/{id}` reports attempts and token. Ctrl+C/SIGTERM shuts down polling and closes the pool. Crash recovery uses expired leases; completion is owner/token/expiry conditional. This is a marker job, not production validation. No production retry cap, lease renewal, terminal failure policy, or outbox implementation is claimed.

`tests/test_database.py` runs only when DATABASE_URL exists. Use an empty isolated phase0_jobs table and stop other workers: tests cover transaction semantics, pool release/reuse, duplicate enqueue, SKIP LOCKED under a competing lock, retry, expired-lease reclaim and stale-token rejection. Tests delete only their UUID afterward. Never point them at production.

## R2 probes and browser CORS

Use a private bucket and bucket-scoped S3 read/write credentials. Set R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY in the API process only. boto3 control calls run in a thread; bytes upload directly through presigned UploadPart URLs expiring in 120 seconds. Never enable SDK wire logging or log responses containing URLs. Probe endpoints use a separate random PROBE_TOKEN, not user authentication.

Retain `infra/r2-cors.json` as the policy template for Cloudflare's bucket CORS UI/API. Replace localhost with the exact Pages origin. Allow PUT/HEAD, Content-Type and expose ETag. Do not enable public bucket access. Verify real browser OPTIONS preflight, PUT and readable ETag; server-side HTTP tests cannot establish CORS.

Set PHASE0_URL and PROBE_TOKEN privately in the test environment, then:

```powershell
apps/api/.venv/Scripts/python.exe -m pytest tests/test_remote.py -q
```

These opt-in tests invoke DB readiness, create/sign/direct PUT/list/complete/HEAD/delete/abort, and enqueue duplicate PostgreSQL marker jobs while a separate worker runs. R2 responses changed from raw XML to boto3-derived JSON for internal probes only; no production API contract changed.

The ZIP spike remains capped at 8 MiB input/32 MiB expansion; it does not establish the production file-size requirement. POST `/probe/validation` reads a fixed private `phase0/pdf-fixture` using HEAD plus a bounded ranged GET, closing its response stream. PyMuPDF remains an optional candidate, absent from the base install. Run rendering in an isolated process/container with CPU/memory/time limits before processing untrusted production PDFs; the current probe is not a secure production renderer.

## Render alternative

Use repository-root Docker context with `infra/Dockerfile`; select `infra/render.yaml` as the Blueprint path. It declares separate web and background-worker services with **paid Starter plans**. Supply environment secrets through Render, apply the probe schema before starting the worker, and invoke the resulting /health URL before claiming deployment. No Render account deployment has been attempted.

Render free web services sleep after 15 idle minutes and have quota restrictions; background workers do not support a Free instance. The supplied two-process configuration therefore is not a free deployment. See [Render free limits](https://render.com/docs/free) and [continuous background workers](https://render.com/docs/background-workers). Choose service memory/CPU based on measurements; no plan size has passed the 2,000-team workload.

## Railway alternative

Create two services from this repository, keeping repository root as build context. Select `/infra/railway-api.json` and `/infra/railway-worker.json` as their respective config paths. Both build the same Dockerfile, with distinct start commands. Set API secrets as above and DATABASE_URL on the worker. Give only the API a public domain. Disable Serverless/app sleeping on the polling worker; database polling can keep it awake and consume usage. For a deadline event, keep the API awake as well.

Railway Free/Trial is credit/resource limited, not a guarantee of two always-on processes. Current documentation lists $1/month Free credit, a one-time $5 Trial grant, and Free per-service ceilings of 0.5 GB RAM/1 vCPU; these are ceilings, not a validated sizing recommendation. Paid usage may be needed. Confirm actual account eligibility, credit balance, resource settings and outbound access before deploying. See [plans and resources](https://docs.railway.com/pricing/plans), [trial restrictions](https://docs.railway.com/pricing/free-trial), and [Serverless sleeping](https://docs.railway.com/deployments/serverless). No Railway deployment has been attempted.

## Pages, CI and cleanup

Cloudflare Pages: root directory apps/web; build `npm ci && npm run build`; output dist. No backend secrets. GitHub Actions runs tests, worker configuration check and frontend build; remote tests require separately supplied credentials and are skipped by default. The workflow has not been invoked remotely. CI does not automatically deploy paid services; connect the chosen host to GitHub only after review and configure deployment to require successful checks.

For a local image check when Docker is available: `docker build -f infra/Dockerfile -t deadline-crush-phase0 .`. API image CMD starts Uvicorn; worker command is `python apps/consumer/main.py`. Docker daemon was unavailable during this migration, so image build and host config acceptance remain unverified.

Stop probes, remove only their known keys/multipart uploads, revoke temporary credentials and drop isolated phase0_jobs after review. Disable the probe deployment before any production release. Existing production deadline, grace, immutable manifest, version selection, auth, validation and performance requirements are unchanged. Stop at Phase 0.
# Phase 2 lifecycle operations

## Current Phase 4 validation processes

The historical phase descriptions below remain evidence for earlier phases.
Production validation uses the separate entrypoint, not the Phase 0 marker worker.
See [Phase 4 architecture, limits and evidence](PHASE_4.md).

On Linux with the existing private server environment and explicitly migrated
application schema:

```sh
PYTHONPATH=apps/api:packages/shared/src python apps/consumer/validation_main.py --dispatch
PYTHONPATH=apps/api:packages/shared/src python apps/consumer/validation_main.py --concurrency 1
PYTHONPATH=apps/api:packages/shared/src python apps/consumer/validation_main.py --metrics
```

Alternatively use the existing environment wrapper:
`node scripts/python.mjs validation --dispatch` and
`node scripts/python.mjs validation --concurrency 1`. Windows supports configuration
checks/metrics/dispatch but intentionally refuses production file-processing claims.
The API still starts with `npm start`; neither process auto-migrates production.
Migration 005 must be reviewed/applied by the existing explicit operator workflow.

Provision one worker with parent + 384 MiB child headroom and >=101 MiB temporary
disk per concurrent job, plus crash debris. Local container verification uses
1 GiB memory, 1 CPU, nonroot, read-only root, tmpfs and dropped capabilities.
These are measured local test constraints, not a certified hosted plan.

Monitor organizer metrics.validation or the worker --metrics command: FAILED jobs,
retry depth, active leases, recovered leases, stale write rejection and durations.
Inspect safe last_error/result categories in the application schema. FAILED is an
infrastructure processing failure; do not manually relabel its version NEEDS_FIX.
No public retry/requeue API exists; use a reviewed audited operator procedure after
repairing the dependency. Never change immutable metadata or selected version order.

SIGTERM/SIGINT stops new claims; the process finishes or cancels active work and
closes its pool. Forced death leaves the lease recoverable. A blocking SDK writer
is awaited before temporary cleanup, so give shutdown sufficient time beyond the
nominal 20-second finish window or allow lease expiry on restart. Startup sweeps
only same-user validation temp directories older than one hour. The dispatcher
repeatedly deletes abandoned preview-intent keys older than one day, preserving
all authoritative artifact references. Never bucket-sweep originals.

Use `node scripts/phase4_docker.mjs` after building both documented images for
Linux validation integration. Existing credentials pass by environment-variable
name, not command-line values; schemas and source/preview keys are fixture-owned.


See [Phase 2 report](PHASE_2.md). Migration 003 uses the existing explicit operator
migration command; do not apply it to production as part of test execution.
Use `node scripts/python.mjs phase03` for the isolated full regression harness,
or `node scripts/python.mjs test tests/test_submissions.py tests/test_phase2_database.py --tb=no`
for targeted Phase 2 checks. Tests create/drop only UUID-scoped application schemas.

Participant routes require bearer managed authentication and `team_id`. Completion
is intentionally unavailable until the trusted submission verifier is wired in
Phase 3; never use probe endpoints, uploaded flags, or manual production state
updates to bypass it. A 202 acknowledges durable intent only. A 200 FINALIZED
requires storage evidence plus committed version/job/outbox/audit. Retries may
recover an existing qualifying intent beyond grace without changing its manifest.

