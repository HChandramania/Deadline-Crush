# AGENTS.md — Deadline Crush / Hack4Hack PS2

## Mission and authority
Build a reliable, deadline-fair submission platform for 2,000 teams in the last 10 minutes. Optimize for correctness, demonstrated failure recovery, test evidence, and an interview-defensible implementation. Read `PRD.md` and `TRD.md` before implementing. These three documents supersede all prior D1, PostgreSQL-on-VPS, or Cloudflare Python Worker designs. If the official challenge conflicts with these documents, flag the discrepancy and update the documents before coding.

## Locked stack
- Frontend: React + TypeScript + Vite, deployed on Cloudflare Pages.
- API: FastAPI/Pydantic v2 on **standard CPython with Uvicorn**, hosted on Render or Railway. Use a supported CPython version, bounded resources, environment variables, and graceful process startup/shutdown; do not rely on filesystem persistence.
- Identity: Neon managed Better Auth owns credentials and sessions; FastAPI verifies managed access tokens. Application profiles, team membership and organizer privileges remain application-owned. Never migrate neon_auth or introduce custom password storage/JWT issuance.
- Database: **Neon PostgreSQL**, directly via a pooled Neon connection string and a bounded application-level `asyncpg` pool. Test transaction semantics in the deployed CPython service.
- Object storage: **Cloudflare R2**; private bucket, browser-to-R2 multipart uploads using boto3/S3-compatible operations. Confirm signing and R2 API compatibility in the deployed CPython service; preserve the FastAPI public API.
- Background delivery: **PostgreSQL-backed durable jobs** with a dedicated Python worker process; at-least-once execution. PostgreSQL outbox is authoritative for dispatch, and PostgreSQL jobs are authoritative for state. Job consumers must be idempotent. If archive/PDF processing requires further isolation, use an isolated container validation service, documenting the deviation; verify Python package and resource requirements.
- Deadline: UTC server-side gate; 10-minute upload grace. Winning version: highest eligible finalized version, even when validation is NEEDS_FIX.

## Agent protocol
1. Before each phase, identify requirements, risks, test plan, and minimal vertical slice. Start with runtime feasibility spikes, not UI polish.
2. Make small, reviewable changes; update schema, OpenAPI contracts, tests and docs together.
3. Run tests, lint, migrations and deployment smoke checks. Report observed results and unverified claims separately; never fabricate load results or URLs.
4. Use parameterized SQL, migrations (Alembic or versioned SQL), unique constraints, short transactions, `FOR UPDATE`/`SKIP LOCKED` only within Neon PostgreSQL, and idempotency keys with request hashes.
5. Never hold a database transaction across an R2 or other external network call. Use persisted intent, outbox, and reconciliation.
6. Never use client timestamps for deadline decisions. Use `clock_timestamp()` after relevant row locks, and document that timestamp-before-deadline is not proof of commit-before-deadline.
7. Keep JWTs, database credentials, signed URLs, service secrets, and file bytes out of logs and Git. Enforce team-scoped authorization on all operations; organizer role is separate.
8. Treat signed URLs as bearer capabilities. Short expiry is not sufficient for deadline enforcement. Verify R2 multipart support, `ListParts`, `HEAD`, CORS, ETag visibility and cleanup against actual R2.
9. All queue and worker actions are retryable. Use unique logical jobs, fencing tokens, conditional terminal writes, and immutable attempt-specific artifacts.
10. Validate archive safety (ZIP bombs, traversal, symlinks), protect link checking from SSRF and redirects, bound CPU/memory/time, and sanitize preview output.
11. Prefer polling for status in v1. Add SSE only if it is proven reliable on the chosen runtime.
12. Maintain `docs/decisions.md`, `docs/runbook.md`, `docs/load-test-report.md`, and a candid limitations section.

## Required execution gates
- **Gate 0 — Feasibility:** Deploy a CPython FastAPI/Uvicorn service on Render or Railway; connect directly to Neon with an asyncpg pool; sign and complete an R2 multipart upload with boto3; enqueue/claim a PostgreSQL job in a dedicated Python process; verify Python ZIP/PDF libraries and CPU/memory constraints. Record exact package versions and limitations. If a capability fails, stop and propose the smallest documented fallback.
- **Gate 1 — Database:** Migrations, team authorization, version allocation under row lock, deadline tests, immutable versions.
- **Gate 2 — Storage:** Multipart create/sign/list/resume/complete; frozen completion manifest, grace admission, R2 reconciliation, idempotent finalization.
- **Gate 3 — Processing:** Transactional outbox dispatcher, PostgreSQL job consumer, leases/fencing, validation, preview, worker-kill recovery.
- **Gate 4 — Product:** Team upload UI, status/version history, organizer metrics and CSV.
- **Gate 5 — Proof:** >=20 automated CI tests (target >=35), deadline boundary tests, 2,000-team/10-minute load test, p95 API target <300 ms, zero acknowledged-submission losses by reconciliation, live deployment, <=5-minute demo, one-page design.

## Suggested repository
```
AGENTS.md PRD.md TRD.md
frontend/
worker-api/             # FastAPI/Uvicorn CPython service
worker-consumer/        # Dedicated Python job worker process
worker-dispatcher/      # PostgreSQL outbox/reconciliation process
backend-core/           # shared domain logic/schema where portable
migrations/
tests/{unit,integration,concurrency,e2e}/
load-tests/
docs/{decisions.md,runbook.md,load-test-report.md}
infra/{render.yaml,railway*.json}
.github/workflows/
```

## Non-negotiable correctness properties
- Acknowledged candidate/version/finalization records survive retries and crashes.
- Only frozen pre-deadline candidate metadata may enter grace; grace cannot be used to create or edit candidates.
- One immutable completion manifest per upload; one logical validation job per finalized version.
- Last eligible finalized **version number** wins, regardless of validation result; no automatic fallback.
- No acknowledged finalization without verified R2 object and durable Neon version + job + outbox transaction.
- Job execution is at least once; logical outcomes are idempotent and fenced.
- API p95 excludes direct R2 byte transfer, which must be measured separately and disclosed.
