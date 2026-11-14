# PRD.md — Deadline Crush

**Status:** implementation baseline | **Challenge:** Hack4Hack Crew Hiring Challenge 2026, PS2 | **Stack:** React/Cloudflare Pages, FastAPI/Uvicorn on Render or Railway (CPython), Neon PostgreSQL, Cloudflare R2, PostgreSQL-backed durable jobs.

## 1. Problem and goal
During the last 10 minutes of a hackathon, approximately 2,000 teams may upload files simultaneously. The system must accept eligible submissions without losing acknowledged work, support interruptions, apply a fair server-controlled deadline, process submissions asynchronously, and give organizers trustworthy visibility. The deliverable is a deployed, tested system and reproducible proof of its guarantees—not a mockup.

## Managed identity and Phase 1 team policy
Managed Better Auth owns authentication credentials and sessions. The application maps verified issuer/subject to profiles and controls memberships and organizer privileges. Authenticated participants may create teams and become captain. Invitation/removal eligibility rules remain unresolved, so membership mutations are deferred. Signup cannot grant organizer privileges.

## Phase 2 submission eligibility clarification
Submission operations require an explicit selected team and current database membership associated with that hackathon. Only its active leader/captain may create or edit submissions through new immutable versions, upload, resume, or finalize. Ordinary members may view their team's submissions but may not mutate them. Every mutation verifies the authenticated user's current CAPTAIN membership role and matching team captain_id server-side, including retries and after external storage calls; client roles grant no authority. Organizer status grants no submission bypass. This authoritative product decision supersedes the earlier all-members submission policy; deadline, grace, idempotency and version-selection rules are unchanged.

## 2. Personas
- **Team leader/captain:** signs in, creates immutable metadata versions, uploads up to 100 MB, resumes and finalizes their team's submissions.
- **Team member:** signs in and views only their team's submissions, status, versions and permitted downloads; cannot mutate submissions.
- **Organizer:** monitors live counts, late attempts, validation failures, and exports CSV.
- **Evaluator:** verifies correctness through tests, load reports, design tradeoffs, deployed demo, and a code walkthrough.

## 3. User journeys
1. Captain signs in, selects their team, enters title/description/links and file metadata.
2. Captain freezes a candidate version before the official deadline; the system allocates a monotonically increasing team version number.
3. Browser uploads file chunks directly to private R2 using short-lived signed URLs. On disconnect or refresh, it resumes from R2-confirmed parts.
4. Captain requests completion; the server freezes the full part manifest and durably admits a completion intent within the 10-minute grace window.
5. The system completes/observes the R2 object, verifies identity and size, durably finalizes the version, and queues validation.
6. The team sees UPLOADING, CHECKING, ACCEPTED or NEEDS_FIX. Validation may finish after the grace window.
7. The organizer views counts and exports the submission ledger.

## 4. Functional requirements and acceptance criteria
| ID | Requirement | Acceptance |
|---|---|---|
| FR-01 | Authentication and authorization | Team members view only own submissions and permitted downloads; only the current team captain creates versions, uploads, resumes or finalizes; organizer endpoints require organizer role. |
| FR-02 | Versioning | Every candidate has immutable metadata and unique increasing `(team_id, version_no)`; edits create a new version. |
| FR-03 | Deadline | No new candidate admitted after D; no completion intent admitted after D+600 seconds; timestamps come from server/database. |
| FR-04 | Grace | Pre-deadline frozen candidates may upload or retry missing parts only within the immutable session, authorized part range and constraints frozen before the deadline; no new versions, sessions, expanded ranges or metadata edits during grace. |
| FR-05 | Direct uploads | R2 multipart, max 100 MB, API does not proxy file bytes; signed part URLs are short-lived. |
| FR-06 | Resume | Browser can list authoritative completed parts and retry missing chunks without reuploading all completed parts. |
| FR-07 | Finalization | Durable idempotent completion intent; verified object before finalization; exactly one logical validation job per version. |
| FR-08 | Selection | Highest eligible finalized version number is selected even if validation status becomes NEEDS_FIX; no automatic fallback. |
| FR-09 | Validation | Content sniffing, safe ZIP checks, URL responsiveness with SSRF protection, PDF first-page or ZIP README preview. |
| FR-10 | Recovery | Queue retries, crashes, stale workers, uncertain R2 completion, and failed dispatch recover without duplicate logical outcomes. |
| FR-11 | Status | Team sees authoritative progress and validation state via polling; refresh does not lose status. |
| FR-12 | Organizer | Counts for finalized submissions, late attempts, failed checks; CSV export with formula injection protection. |
| FR-13 | Auditability | Immutable audit records for creation, rejection, completion intent, finalization, selection, validation and recovery. |
| FR-14 | Delivery | Public repo, live app, CI, >=20 meaningful tests, load report, one-page architecture, <=5-minute walkthrough. |

## 5. Product state model
- Candidate: `CREATED -> UPLOADING -> COMPLETING -> FINALIZED`, or `EXPIRED` / `ABORTED`.
- Validation (independent): `NOT_STARTED -> CHECKING -> ACCEPTED | NEEDS_FIX`; infrastructure retries do not imply NEEDS_FIX.
- Completion intent: `ADMITTED -> STORAGE_COMPLETED -> FINALIZED`, or `RECOVERY_REQUIRED` / terminal failure after bounded recovery.
- Team selection: highest-numbered eligible FINALIZED candidate; it can be CHECKING or NEEDS_FIX.

## 6. Deadline policy
- Organizer configures UTC deadline `D`; grace cutoff is `D + 600s`.
- Creation must pass a database-side admission gate after team-row lock acquisition. This is a documented **admission-time policy**, not a claim of precise commit-time cutoff.
- Only a pre-deadline candidate with a verified complete part manifest may have completion intent admitted by grace cutoff. After admission, the exact same manifest may be retried beyond grace to recover from server/R2 failure; no new chunks are authorized.
- Version selection is based on version number, not validation finish time or upload duration.
- Display precise server timestamps and failure reasons for late attempts.

## 7. Nonfunctional requirements
- Load scenario: 2,000 teams in 10 minutes, including burst peaks; API p95 target below 300 ms (report measured scope and environment).
- Durability: zero acknowledged finalizations missing from the authoritative ledger or referenced R2 objects after reconciliation.
- Security: private storage, expiring capabilities, JWT verification, least privilege, strict CORS, input validation, SSRF protection, bounded file processing.
- Observability: structured logs with correlation IDs, state-transition counters, queue lag, outbox lag, lease recovery, reconciliation exceptions.
- Accessibility: upload progress and errors must be understandable; keyboard-accessible forms and status announcements.

## 8. Out of scope for MVP
Virus scanning, email receipts, advanced analytics, elaborate team invitations, multi-region failover, Kafka, Redis, Kubernetes, and visual polish beyond a usable responsive interface. Optional enhancements only after all mandatory checks pass.

## 9. Definition of done
All FR acceptance tests pass; deployment and actual R2/Neon/PostgreSQL job integrations are smoke-tested; >=20 CI tests; crash/duplicate/boundary tests; reproducible load run with truthful p95 and data reconciliation; one-page design, demo, runbook, and known limitations. If the dedicated Python worker cannot safely run archive/PDF validation within hosting resource limits, a documented isolated processing fallback is acceptable only after feasibility testing and must not be misrepresented as a verified worker feature.
