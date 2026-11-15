# Phase 0 decisions

## Phase 4 validation — 2026-11-14

- Preserve Phase 1–3 admission, captain, immutable metadata and selection contracts.
  Production validation uses existing validation_jobs and VALIDATE outbox only;
  dispatcher acknowledges internal PostgreSQL delivery transactionally, and claims
  require SENT. Execution remains at least once with fenced atomic outcomes.
- Use short SKIP LOCKED claims, database-time leases, UUID owners, monotonic
  generations, heartbeat and bounded durable retries. Infrastructure exhaustion
  leaves CHECKING with a retained FAILED job; invalid content completes NEEDS_FIX.
- Stream exact finalized R2 objects, verify frozen part hashes and size/metadata,
  record SHA-256. Stage attempt-specific private preview intents before upload;
  only the fenced result transaction registers authoritative references.
- Linux subprocess limits are mandatory for untrusted ZIP/PDF processing; fail
  readiness on unsupported OS. PyMuPDF 1.27.2.2 passed the local Linux feasibility
  gate. No hosted resource or full 100 MiB hostile-corpus proof is implied.
- README is not mandatory in current PRD/TRD; show escaped README if present plus
  bounded inventory. HTTP link checks pin the approved resolved address and verify
  every redirect; HTTPS preserves hostname certificate verification.
- Migration 005 is additive and tested only in disposable schemas. Historical
  migration checksums and neon_auth are unchanged. No production rollout or Phase 5
  dashboard. See PHASE_4.md for evidence and limitations.

## Phase 3 storage and recovery — 2026-11-14

- After observing failed localhost preflight, the user explicitly approved an
  additive localhost-only CORS rule and refreshed the existing Wrangler login.
  Before was empty; readback confirms exact localhost:5173, PUT/HEAD, Content-Type,
  ETag exposure and max age 120. No new credentials, public bucket or permission
  change. Actual OPTIONS/PUT/HEAD/origin-denial checks passed; see
  phase3-cors-results.json. No connected browser JavaScript surface was available.
- Final partitioned verification covers 162 distinct backend cases and five
  frontend cases, all passing after resolving one lifespan-state regression.
  Preserve the interrupted monolithic run separately; do not count it as passed.
  Recommendation remains CONDITIONAL GO for isolated Phase 4 development.

- Retain Phase 2 gates, manifests, captain policy and atomic ledger. Add real R2
  multipart initialization/sign/list/resume/completion/HEAD and a dedicated
  PostgreSQL-backed recovery process; no broker or Phase 4 validation.
- One durable create attempt per session, SDK automatic retries disabled. Unknown
  create outcomes are not recreated/adopted: expire and repeatedly abort exact-key
  multipart orphans after conservative retention. Availability is sacrificed for
  immutable upload identity and no post-deadline binding.
- Object evidence combines session/version metadata, exact size and R2's documented
  multipart ETag construction, not SHA-256. Administrative overwrites remain a
  limitation; full content hashing belongs in the later worker.
- Internal recovery uses a database lease/fencing token and may finish an already
  admitted intent without a captain session. Participant retries always recheck
  today's captain. Cleanup never deletes completed objects or intent-owned uploads.
- Preserve existing gate-time semantics: intent admission timestamp does not prove
  strict commit-before-grace. Signed capabilities cannot guarantee instantaneous
  revocation or prevention of every post-grace in-flight byte transfer. See PHASE_3.md.
- Migration 004 is tested only in disposable schemas; no production migration,
  managed-auth writes or bucket-policy change is authorized by test execution.

## Authoritative captain-only submission policy — 2026-11-14

- The user supersedes the earlier all-members submission policy: only the selected
  team's active leader/captain may create/edit through immutable versions, upload,
  resume or finalize. Ordinary current members retain read access to their team's
  submissions. Explicit team_id selection remains mandatory.
- Every mutation checks current database CAPTAIN membership plus matching
  teams.captain_id under team/member locks, before retries and again after storage
  observations. JWT/body roles and organizer privileges grant no bypass. A former
  captain retains member reads but cannot replay a previously authorized mutation;
  the new captain may recover the existing immutable upload/intent.
- Deadline/grace, version numbering, metadata immutability, idempotency scope and
  highest-finalized-version selection are unchanged. No schema migration, public
  captain-transfer endpoint, real upload implementation or production data update
  is introduced. Tests transfer roles only in disposable schemas.
- Narrow contract changes and hashes: CAPTAIN_POLICY_CONTRACT.patch and
  captain-contract-audit.json. Historical Phase 2 decisions/evidence below remain
  recorded but their all-members mutation policy is superseded by this decision.

## Phase 2 — 2026-11-14

- User authorized explicit selected team, any current member admission/finalization,
  membership-based hackathon eligibility and pre-authorized missing parts during
  grace. Query `team_id` preserves existing routes/bodies. The narrow contract
  amendment and its hashes are recorded in PHASE_2_CONTRACT.patch and
  phase2-contract-audit.json; deadline inclusivity/selection semantics are unchanged.
- Use fresh database-clock gates after locks, immutable policy/upload snapshots,
  principal-scoped admission retries and one session-scoped completion manifest.
  Preserve the TRD transactional winner pointer and use indexed version ordering
  as authoritative API selection. No fallback on NEEDS_FIX.
- Storage observations occur outside transactions, with locked rechecks before
  intent/finalization commits. Default verifier fails closed; real R2 adapter and
  reconciliation are Phase 3 obligations. No production pipeline or multipart API
  is introduced. See PHASE_2.md for scope, state machine and evidence.

## Phase 1 — 2026-11-14

- Managed Better Auth remains authoritative for credentials/sessions. Use the installed SDK's getSession().data.session.token JWT for FastAPI; verify real branch JWKS/Ed25519, issuer, audience and lifetime. Domain profiles key on verified issuer plus opaque subject, never email or token role. No managed auth schema migrations.
- The user approved self-service team creation, creator-as-captain ownership and auditing. Invitation/removal eligibility rules remain undefined and are deferred. Organizer privileges use explicit privileged domain administration only. No deadline/grace/version semantics changed.
- The user confirmed there is no deployed frontend origin. Use localhost:5173 in Phase 1 and configurable explicit CORS; production Neon domain registration is deferred to deployment. No new management keys, init reruns or restoration of the revoked credential.
- Use checksummed transactional SQL migrations in a separate application schema and transaction-local search_path with bounded API/worker pools. Live tests apply only disposable schemas; production application migration remains an explicit operator command. See [Phase 1 report](PHASE_1.md), [contract diff](PHASE_1_CONTRACT.patch), and its hash audit.

## Managed identity selection — 2026-11-13

The user explicitly requested Neon managed Better Auth on project empty-mud-47004781, production branch. Enabled auth in the existing neon.ts and deployed it. React uses the managed Auth SDK for email signup, signin, signout and session retrieval. The prior request for local password hashes/JWT issuance is superseded by this instruction. Application identity mapping and FastAPI verification remain unresolved Phase 1 work; the unapplied schema draft must be reviewed first. See [managed auth setup and live evidence](MANAGED_AUTH.md). Core contract files remain unchanged for reviewed amendment. R2, CPython, asyncpg and PostgreSQL jobs remain in place.

## Neon setup after migration

- Linked the user-specified org/project/production branch with `neon init`, then ran `neon deploy`. Only Postgres is utilized; no Neon Auth, Data API, Functions, Object Storage, or external queue was introduced. The existing R2 and Render/Railway decisions stand.
- Consulted the installed neon and neon-postgres skills. Kept asyncpg for long-running CPython and the pooled DATABASE_URL for runtime traffic; DATABASE_URL_UNPOOLED is used to apply the existing versioned Phase 0 SQL. No production submission schema was applied.
- Root npm commands load ignored Neon environment files into Python processes only. Added a generated local PROBE_TOKEN without displaying it. The verification command exercises real transactions and an API-to-worker job, then deletes only its UUID. It refuses a nonempty Phase 0 job table before processing any jobs.
- Actual results and failures are in NEON_SETUP.md. Earlier auth/credential blockers remain preserved as historical findings, but Neon database/API/job connectivity is now verified against the real service.

## Current architecture migration — 2026-11-13

- The user's final decision explicitly supersedes the Cloudflare Python Workers stack. The observed blocker was no usable asyncpg 0.32.0 wheel for Pyodide 3.14.2/index 314.0.7. This was a dependency-resolution failure, not proof that every Python Worker driver is unsupported. The new decision is conventional CPython, not another WASM driver experiment.
- Keep FastAPI, health endpoints, direct browser-to-R2 uploads, Neon, ZIP safety tests, PDF candidate probe, React placeholder and k6 smoke. Replace ASGI runtime adapter with Uvicorn, Hyperdrive with bounded asyncpg pools, manual SigV4 with boto3, and Cloudflare Queues with a dedicated Python PostgreSQL job process. Delete the two obsolete Wrangler configs and custom signer. No Redis/Celery/Kafka introduced.
- Standardize CPython 3.12 across manifests, Docker and CI. API and worker each own a pool (default 0–5); all replicas/processes multiply that connection budget. Neon pooler is transaction based: statement cache disabled; no session state, LISTEN/NOTIFY, or session advisory locks. PostgreSQL job polling works with pooled connections. Use a direct endpoint for migrations when needed.
- Job proof uses only isolated phase0_jobs. Unique UUID enqueue is idempotent; SKIP LOCKED claim uses a 30-second lease and increasing token. Completion requires owner/token/live lease; the marker job injects one retry. No production submission, validation, outbox dispatch, dead-letter policy, lease extension, or retry cap is implemented. Those remain documented later-phase requirements.
- Render and Railway remain alternatives; neither is claimed deployed or free for this workload. Render template chooses paid Starter web and worker services. Railway requires separate services, explicit config paths and disabled Serverless for the poller. Account restrictions, quotas, resources and costs must be checked before deployment.
- Current recommendation is CONDITIONAL GO for reviewing the migration; Gate 0 remains incomplete until real Neon/R2/job and hosting checks pass. No Phase 1 work.
- Root decisions.md does not exist; docs/decisions.md is the existing canonical decision log. Prior findings are retained below and as an exact historical copy in history/WORKERS_DECISIONS.md. Prior feasibility report is retained in history/PHASE_0_WORKERS_FEASIBILITY.md.

## Historical decisions before migration (superseded infrastructure only)

- Read AGENTS.md, PRD.md and TRD.md before implementation. No core stack contradiction found. The user-requested `apps/` layout replaces the explicitly suggested layout in AGENTS.md.
- Contract ambiguities: PRD says 100 MB, while TRD fixes 104857600 bytes (100 MiB); use the precise TRD limit when Phase 1 is authorized. PRD FR-04 says “already-uploaded parts” during grace, while resume journey and TRD signing policy do not explicitly forbid missing-part upload during grace. Resolve before production lifecycle implementation; Phase 0 has no deadline policy.
- TRD grace description mentions checking R2 while holding a row lock; AGENTS prohibits network calls inside database transactions. Future implementation must use staged observation and revalidation without holding transactions across R2 calls. No lifecycle implementation here.
- Use documented asyncpg Hyperdrive path with compatibility date 2026-11-13. No conventional CPython socket assumptions; deployed proof remains mandatory.
- Request-scoped connection, reused for multiple transactions/queries, always closed. Hyperdrive pools upstream connections. No isolate-global application pool, prepared statement cache disabled. Disable Hyperdrive query caching for these probes, including server timestamps.
- Python Queue consumer is documented today; no evidence justifies a TypeScript fallback yet. Isolated `phase0_queue_receipts` table proves duplicate logical inserts, not production job fencing. DDL in this spike is not a production migration strategy.
- Minimal stdlib SigV4 signer avoids assuming boto3 transport works in Pyodide. It remains a candidate until real UploadPart and control requests pass. If it fails, stop and propose an authenticated TypeScript service binding as allowed by the contract.
- ZIP spike cap is 8 MiB compressed / 32 MiB expanded. This is deliberately not proof that 100 MiB ZIPs fit Workers memory. No extraction to disk. PDF candidate is PyMuPDF; unavailable renderer requires a separately approved isolated container fallback, not silently omitted previews.
- Probe routes fail closed without PROBE_TOKEN; no user authentication implemented. Disable/delete probe deployments after review. R2 capabilities are bearer secrets and must never be logged.
# Phase 5 product integration (2026-11-15)

- Preserve managed Auth and all existing captain/deadline/finalization mutations.
  Add only missing event, validation/preview/download and organizer read surfaces.
- Use cancelled adaptive polling (participant 5s, details 7s, organizer 10s),
  not a new broker or streaming dependency. Persist only nonsecret upload identity
  and stable operation UUIDs; verify all file bytes before missing-part resume.
- Preview committed worker artifacts through authenticated bounded reads; sandbox
  archive HTML without same-origin/script permissions. Export teams through bounded
  keyset pages, with formula escaping; export is a moving read, not a DB snapshot.
- Production remains NO-GO. See PHASE_5.md for the persistent schema/privilege/hosted
  blocker register and separate controlled/live evidence. No Phase 6 authorization.
