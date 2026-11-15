# Phase 5 — participant workspace and organizer operations

Date: 2026-11-15 (Asia/Calcutta). Production remains **NO-GO**. Phase 6 has not
started. No commit, push, production migration, direct managed-auth DDL/DML,
production permission grant, bucket-policy change or hosted deployment performed.

## Scope, sequence, risks and verification plan

Inspected AGENTS/PRD/TRD, phase 1–4.5 reports, captain policy, contract fixtures,
actual domain/submission/upload/validation code, frontend SDK integration, CI and
deployment templates. Preserve existing mutation routes and guarantees. Build the
smallest vertical slice in order: participant teams/admission/direct upload;
polling/deadline/recovery; organizer aggregates/list/drilldown/export; browser
contracts and actual-service integration. Principal risks: stale team responses,
wrong-file resume, completion uncertainty, selecting unfinished versions,
untrusted previews, privileged read leakage, and confusing infrastructure failure
with invalid content. Verify these through client tests, existing regression
tests, focused API tests, disposable Neon schemas and browser scenarios.

## Screens and architecture

- Managed SDK signup/signin/signout, reload restoration and expired-session UI.
- Event-backed team creation/selection, captain identification and membership list.
- Captain immutable version form, direct multipart progress and recovery record.
- Server-offset deadline/grace countdown, authoritative current version, paginated
  history, validation findings and private preview/download controls.
- Organizer aggregates, queue failures/retries, recent finalizations per minute,
  team search/status filter, keyset pages, version drilldown and complete CSV export.
- Responsive CSS, form labels, keyboard focus, status announcements and empty/error states.

React/Vite and @neon/auth remain unchanged. `workspace.tsx` composes screens;
`upload.ts` contains multipart orchestration; `poll.ts` owns sequential cancelled
polling; `api.ts` obtains the managed session JWT per request and never persists it.
`product.py` supplies missing read APIs; existing submission/captain/deadline
mutation implementations are preserved. No new application migration is needed.

The API client refreshes a rejected managed access token once using the installed
SDK's supported `getSession({query:{disableCookieCache:true},fetchOptions:{headers:
{'X-Force-Fetch':'true'}}})`. The installed wrapper removes that header before the
network request. The retry retains the original principal generation, body and
idempotency key. A second 401 ends application access; a temporary managed-auth
error instead reports AUTH_UNAVAILABLE and retains recoverable upload state.
No custom JWT issuance, role claim trust or persistent application token cache.

## API integration matrix

All paths below have `/api/v1` prefix. New schemas appear in generated OpenAPI.

| Requirement | Existing endpoint / minimal addition | Access |
|---|---|---|
| Managed sign-up/in/out/session | Existing @neon/auth SDK; no custom auth API | Managed Auth |
| Profile / application role | GET /users/me | Managed bearer |
| Event selection, server time, deadlines | **NEW** GET /hackathons; latest 100 events | Managed bearer |
| Teams/create/member roles | GET/POST /teams; GET /teams/{id}/members | Own membership; server-authoritative |
| Immutable admission | POST /submissions/versions?team_id= | Both captain invariants |
| Authoritative winner | GET /submissions/current?team_id= | Team member |
| History | GET /submissions/versions?team_id=&before_version_no= | Team member; existing 100-row keyset |
| Multipart creation | POST /uploads/{id}/initialize?team_id= | Both captain invariants |
| Authoritative resume | GET /uploads/{id}?team_id= | Both captain invariants; R2 ListParts |
| Part signing | POST /uploads/{id}/parts/sign?team_id= | Both captain invariants |
| Completion/recovery | POST /uploads/{id}/complete?team_id= | Both captain invariants; exact manifest |
| Validation details/job infrastructure state | **NEW** GET /submissions/{id}/details | Current team member or DB organizer |
| Private preview bytes | **NEW** GET /submissions/{id}/preview/{ZIP_PREVIEW\|PDF_PREVIEW} | Current team member or DB organizer |
| Source download | **NEW** GET /submissions/{id}/download-url | Finalized; current team member or DB organizer |
| Aggregate counts/activity | Existing GET /organizer/metrics, additive total/checking/accepted/activity fields | DB organizer |
| Team ledger, search, status, pagination | **NEW** GET /organizer/submissions?after=&search=&status=&limit= | DB organizer; max 100 |
| Team version drilldown | **NEW** GET /organizer/teams/{id}/versions?before= | DB organizer; max 100 |
| Complete export | **NEW** GET /organizer/export.csv | DB organizer |

Organizer access to new read APIs does not grant submission mutation rights.
Members cannot call upload state/sign/complete routes. The UI requires both the
membership CAPTAIN role and matching captain_id; backend checks remain decisive.
Organizer provisioning is the existing privileged command:

```text
node scripts/python.mjs admin organizer --schema <reviewed-application-schema> --user-id <application-users-id>
```

This runs an explicit application `users.role='ORGANIZER'` update and
`ORGANIZER_PROVISIONED` audit insert in one transaction through the existing
admin connection. It never changes neon_auth. A verified identity must first sign
in and provision its application profile. **User approval is required before
running this privilege operation**, including any proposed disposable test grant.
There is no automatic organizer signup, JWT role trust or frontend role override.

## Multipart and recovery

File limit is the established exact 104857600 bytes (100 MiB). ZIP/PDF extension
checks are an early usability check; server-side content validation remains
authoritative. UI limits project links to 10 HTTPS URLs because the existing
renderer accepts 10, although the API currently permits 20 (pre-existing mismatch).

Before admission the browser computes SHA-256 for every 8 MiB slice, using bounded
memory. A principal/team-scoped recovery record contains frozen metadata, the
whole-file sequence of hashes, resource UUIDs and stable operation keys. Tokens,
signed capabilities and file bytes are never persisted. Individual part requests
retry at most three times, obtaining a fresh capability under the same logical
key. The final smaller slice uses the server-provided part size. A visible error
keeps the admitted record recoverable. Separate version records remain available
when starting a new version in the same browser.

After refresh/tab reopening, reselect the original file if parts are missing.
Filename, size and every byte hash must match before any missing part is sent.
Other devices/cleared browser storage cannot safely resume missing bytes without
the original fingerprint; they can still recover a fully uploaded/frozen intent.
GET upload state always reconciles R2 or the frozen manifest. Completion uses the
ordered server-confirmed ETags, never browser-local part claims. The same
admission/completion UUIDs survive lost responses and reloads. Backend idempotency
conflicts remain visible. Removing captain privileges is enforced by every API
mutation even if stale controls are visible briefly.

Progress separates successful transferred bytes, current-part in-flight bytes,
finalizing, intent admitted/recovery pending, FINALIZED and validation outcome.
100% transferred does not mean acceptance. Metadata remains read-only after the
first admission attempt, including uncertain responses. A new operation requires
the explicit separate-version action and a new key.

## Deadlines and live status

The event endpoint returns database clock_timestamp(), D and D+600 seconds.
Initial round-trip midpoint estimates browser offset. Countdown is informational;
inclusive lock-time gates remain unchanged. During grace no new candidate is
allowed. Missing parts must remain within the original session/range; after grace
only a previously admitted immutable intent may recover. Existing URLs cannot be
claimed instantly revoked.

Participant workspace polls every 5 seconds; selected version details every 7
seconds. Organizer aggregate/list reads share a 10-second polling loop; open
drilldown/details are separately bounded surfaces. Each loop awaits the preceding
request, cancels on identity/team/version/unmount changes, backs off to at most
60 seconds on errors and uses at least 30 seconds while the tab is hidden. Stale
responses are discarded. A temporary request failure never becomes NEEDS_FIX.
Job FAILED displays operator-attention infrastructure text rather than invalid
content. These are polling updates, **not streaming real time**.

## Preview and CSV safety

Preview reads only committed artifact references, bounds bytes at 256 KiB, closes
the S3 body, uses private/no-store and nosniff with restrictive sandbox CSP.
ZIP worker output already escapes README/inventory. Browser displays it inside an
empty-sandbox iframe without same-origin or script permission; no uploaded HTML
enters React markup. PDF output is a bounded worker-rendered PNG. Blob URLs are
revoked on replacement/unmount. Source download capabilities expire after 60
seconds, use attachment disposition, and are not persisted. Bucket remains private.

CSV walks the database in 100-team keyset pages; a transaction is never held over
a yielded chunk. It includes all matching dataset teams, authoritative current
eligible finalized version and status, latest attempt number and finalized time.
Standard csv.writer quotes commas, quotes/newlines. Leading formula characters,
whitespace-hidden formulas and leading control whitespace are escaped. Role is
rechecked per page. No secrets, source keys or internal job metadata are exported.
Export is a moving read, **not a repeatable-read snapshot**; concurrent finalization
may be reflected between pages. Browser buffers the small expected 2,000-team CSV
for authenticated download; server memory remains page-bounded. Metrics count
versions globally; ledger counts/status refer to each team's authoritative winner.

## Verification evidence

Observed results in this working session (not hosted CI claims):

| Run | Result | Actual scope |
|---|---|---|
| Existing full Windows backend suite, before new tests | 248 passed, 2 failed, 10 skipped | Existing configured isolated Neon/R2/remote integrations; Linux-only cases skipped |
| Focused deadline retry + new product unit/API cases | 17 passed | 16 new cases + original four-second grace fixture retry |
| New product database integration | 1 passed | Real disposable Neon schema, controlled identity/storage; authoritative v1 vs unfinished v2, aggregates/CSV/query bounds and cross-team denial |
| Credential-free Linux cached-image regression | 214 passed, 63 skipped | Network disabled, 1 GiB/one CPU/read-only/nonroot; includes Linux parsing/process tests and new API cases; live integrations intentionally skipped |
| Frontend client/upload tests | 14 passed | Managed token propagation/cancellation, bounded SDK refresh preserving mutation identity, auth-infrastructure distinction, actionable gate/conflict codes, complete-byte file identity, limits, stable admission/manifest keys, post-grace recovery |
| Browser controlled suite — final | **8 passed, 2 live cases skipped**, 26.7 seconds | Real Chromium/React/SDK, mocked Auth/API/R2/worker responses; startup recovery, first-team creation, authorized team restoration, interrupted upload/reload/wrong-file/archive recovery, live polling, member mobile, organizer CSV and signout |
| Frontend TypeScript/Vite build | PASS | Production assets built; only existing dependency annotation warnings |
| Genuine local browser E2E | **1 passed in 1.9 minutes** | Real managed Auth, local CPython FastAPI, disposable Neon application schema, real R2 multipart, separate Linux dispatcher/validator, approved isolated organizer role; no service-response mocking |

The broad run's recoverable grace fixture failed with 409 instead of 202: its
four-second post-deadline grace budget can expire over live database round trips.
The unchanged test passed focused retry; exact inclusive boundary tests remain
unchanged. The remote marker-job case failed because no Phase 0 marker consumer
was running; this is not a production validation-job result. No blanket all-green
claim is made for that run. Linux-only tests are exercised in the offline image.

Initial controlled browser run: 4 passed, 1 failed, 1 skipped; the failure was an
error-text assertion expecting an R2 HTTP failure for an injected transport abort.
Corrected assertion checks the actual interruption message; complete rerun passed.
Additional live workflow/interactive execution and final build evidence are tracked
below as they run. No genuine managed-session browser-to-R2-to-worker success is
inferred from the controlled browser suite or prior Phase 4.5 evidence.

Actual local integration environment started successfully with a disposable
allowlisted application schema and independent Linux dispatcher/validator. The
first genuine-service interactive browser attempt **timed out awaiting sign-in
after 10 minutes**, before any team/submission mutation. A second attempt reached
managed sign-in but encountered 404 from an older API listener. Investigation found
that the original isolated harness had failed to bind the occupied legacy port,
then correctly cleaned its schema. No team/submission was created by those attempts.
The harness now reserves an available loopback socket before creating fixtures;
the older listener is preserved. Health 200, Phase 5 OpenAPI routes and anonymous
401 were explicitly verified on the reserved API before reopening the browser.
Neither earlier attempt proves upload or worker success. The corrected interactive
attempts and final passing outcome are recorded below. No supported existing
browser session was available to restore in that new Chromium context. Desktop
participant/organizer and mobile-member screenshots from controlled fixtures were
visually reviewed; they are UI evidence, not genuine-service proof.

Subsequent genuine-service run verified managed sign-in, team creation, current
captain assignment and actual presigned-R2 transport interruption. It then exposed
a **product defect**: reload selected the first membership instead of the upload's
team. Selection now persists only a principal-scoped nonsecret team UUID and is
validated against the returned memberships. A dedicated two-team Chromium test
passes restoration and fallback from an inaccessible saved identifier. A separate
first-team test covers the native details/open toggle. Earlier runs of that test
harness had a details-toggle error and a too-strict label locator; both were
corrected before the latest live retry. No interrupted attempt is reported as a
finalized or accepted submission.

The user explicitly approved organizer designation for application profile
`3c06f832-8f37-4b3d-9753-6d8e3382dc7d` only in disposable schema
`phase1_6b39182303784b7fb4eda6d5fa70a728`. The documented existing command succeeded.
Read-only inspection verified role ORGANIZER and exactly one
ORGANIZER_PROVISIONED audit event. This is the existing participant's **isolated
application role**, not a separate managed organizer account or production grant.
No runtime SQL role, credential, managed-auth table or production profile changed.

Final read-only fixture inspection found three version records across the test
teams: two earlier interrupted attempts remain UPLOADING/NOT_STARTED, and the
successful attempt is FINALIZED/ACCEPTED. There is exactly one validation job,
SUCCEEDED in one attempt, and one organizer-provisioning audit. The unfinished
attempts are not acknowledged finalizations and do not displace a finalized winner.

### Final genuine-service browser result — PASS

The final run verified, in one real Chromium managed session:

1. Existing-account managed sign-in and application profile mapping.
2. Team creation through the actual API and both captain indicators.
3. Immutable admission and signed multipart requests; injected transport abort
   of PUTs only (no Auth/API response mocking).
4. Page reload, server-authorized selected-team restoration, original-file
   reselection and byte-fingerprint verification.
5. Actual browser-to-R2 PUT, exposed ETag, authoritative parts and finalization.
6. Actual PostgreSQL outbox dispatch and separate Linux validation worker:
   **ACCEPTED without manual refresh**.
7. Another reload, correct current version and genuine authorized private README
   preview rendered in the sandboxed iframe.
8. The explicitly approved organizer's real aggregate dashboard, team drilldown
   and authenticated streamed CSV download containing the test team and ACCEPTED.

The successful run observed valid managed session responses and, after reload,
three initial API read 401s. The prior client treated the first 401 as definitive
logout; the revised client requested fresh SDK session/JWT data and the requests
succeeded. The exact provider-side reason for those initial rejected tokens was
not independently isolated; no JWT/credential contents were logged to diagnose it.
This verifies bounded managed refresh recovery, not a claim of instant revocation.
Transient auth failures no longer masquerade as invalid/expired content.

Earlier genuine attempts remain failures in the history above: two initial
sign-in/API-connection waits, one test-toggle wait, and two interruption/reload
failures before the final successful run. The two-team context defect was fixed
and independently regressed; the later failure snapshot explicitly showed session
expiry, leading to the bounded SDK refresh correction. The final passing result
does not erase those earlier results.

Live evidence uses the **same approved existing account** for participant and
organizer roles in a disposable application schema; a separate managed organizer
account remains unavailable. Source artifact download signing/expiry/attachment
is unit/contract verified, but its actual signed R2 GET was not exercised by this
browser scenario. CHECKING is covered by controlled browser transitions; the real
worker run verified pending/finalized UI followed by ACCEPTED, without claiming
every short-lived badge was captured by the five-second poll.

CI workflow preserves existing backend/worker checks and adds upload tests and
Playwright Chromium installation/scenarios. **Hosted GitHub Actions for these
uncommitted changes is UNVERIFIED**; no push is authorized. The earlier Phase 4.5
green CI belongs to its previous revision only.

## Local and hosted integration

Inspection found the occupied legacy configured local API origin `http://127.0.0.1:8787`;
frontend Vite's established local origin is `http://localhost:5173`. The existing
public Auth URL is configured, but no supported existing browser session was
visible through the browser tool. Never read browser credential databases or
introduce a production token-handoff route.

`node scripts/python.mjs phase5-local` creates only a random allowlisted
`phase1_<uuid>` schema, migrates that disposable schema, creates a two-hour event,
and starts the actual CPython API on a reserved available loopback socket, plus separate bounded Linux dispatcher and
validator. Probes are disabled. Nonsecret schema/event/origin metadata goes to
ignored `tmp/phase5-local.json` (the verified corrected run used API port 49830).
`node scripts/phase5_web.mjs` starts the actual
frontend with this API origin and existing SDK configuration. Stop the harness
gracefully to stop workers, delete its exact test-owned storage keys and remove
its schema. No production schema is initialized. The first harness launch rejected
an unsupported test-schema name before connection; corrected to the existing
allowlist, without relaxing that allowlist.

The local review environment is intentionally left available. Run
`node scripts/phase5_stop.mjs` to request graceful API/worker shutdown and exact
test-owned R2/schema cleanup; wait for the harness confirmation. Its privileged
application organizer designation disappears with that disposable schema. This
does not stop or alter the older API listener. The local Vite preview can be
stopped separately through its owning process. The Linux verification image uses
existing cached Phase 4 dependencies plus current product API/test files; it is
not evidence of a fresh production image build or hosted rollout.

## File inventory

Created: `apps/api/product.py`; `apps/web/workspace.tsx`, `upload.ts`, `poll.ts`,
`upload.test.mjs`, `playwright.config.ts`, `e2e/product.spec.ts`;
`tests/test_product.py`, `test_product_database.py`;
`scripts/phase5_local.py`, `phase5_web.mjs`, `phase5_browser.mjs`,
`phase5_inspect.py`, `phase5_stop.mjs`; this report.

Modified: `apps/api/domain.py`, `main.py`; `apps/web/api.ts`, `api.test.mjs`,
`src.tsx`, `style.css`, `package.json`, `package-lock.json`;
`scripts/python.mjs`; `.github/workflows/phase0.yml`; `.gitignore`;
`docs/decisions.md`, `runbook.md`, `load-test-report.md` (existing previously ignored
load report is now included). The pre-existing untracked `.gitattributes` is
preserved and was not authored or modified in this phase. Existing migrations,
managed-auth verification, submission mutations, storage mutations, queue and
validation implementations are unchanged.

Playwright's opt-in live workflow uses actual SDK/API/R2/worker responses, interrupts
only PUT transport, reselects the original valid ZIP, resumes, waits for ACCEPTED,
then verifies reload and preview availability. `PW_INTERACTIVE=1` opens a visible
sign-in window; user enters credentials directly. Credentials are never requested
in chat or written by this test. Tracing/video/screenshots are disabled for live
tests. Run only against the disposable fixture with `PW_LIVE_MUTATIONS=isolated-only`.

Cloudflare Pages: root `apps/web`, build `npm ci && npm run build`, output `dist`.
Set public `VITE_API_ORIGIN` to the actual HTTPS API origin and
`VITE_NEON_AUTH_URL` to the existing managed endpoint. Empty API origin uses the
same-origin `/api/v1` path; Pages needs the explicit separate API origin. Never
put database/R2/admin/JWT secrets in VITE variables. Configure exact Pages origin
in backend CORS, R2 PUT CORS with ETag exposure, and managed Auth trusted origins
through separately authorized administration. No production localhost origin is
hardcoded in client source. Use CPython 3.12 API and distinct validation,
dispatcher and recovery services, not the historical marker worker. Hosted
startup/resource/stop-window/origin integration remains unverified.

## Requirement checklist and handoff

| Requirement group | Status |
|---|---|
| Managed auth UI/session/expired states | VERIFIED controlled browser and genuine existing-account sign-in/reload with bounded managed refresh |
| Teams/captain/read-only member | VERIFIED controlled browser and existing backend integrations |
| Multipart/admission/completion/reload safety | VERIFIED controlled browser + client contracts + actual Auth/Neon/R2/worker browser proof |
| Winner/history/deadline/grace | VERIFIED database/unit contracts; controlled browser display |
| Live participant statuses | VERIFIED controlled polling transitions and actual worker ACCEPTED without manual refresh |
| Validation/private previews | VERIFIED API authorization/sandbox + controlled browser + genuine private README preview; PDF security/rendering covered by Linux regressions; live PDF UI unverified |
| Organizer metrics/search/pages/drilldown/CSV | VERIFIED disposable Neon integration + controlled browser + genuine approved-role metrics/drilldown/CSV; live search/pagination and change-delivery timing not separately exercised |
| Accessibility/responsive | IMPLEMENTED; controlled mobile flow verified; exhaustive assistive-technology audit UNVERIFIED |
| Build | VERIFIED |
| Current hosted CI | IMPLEMENTED BUT UNVERIFIED |
| Hosted frontend/API/worker integration | BLOCKED / UNVERIFIED |
| 2,000-team benchmark/p95/zero-loss reconciliation | NOT IMPLEMENTED in Phase 5; Phase 6 scope |

Do not begin Phase 6 automatically. Conditional handoff for reviewing/testing the
implementation only. The actual local participant and approved organizer browser
flows now have passing evidence. Current hosted CI and deployment remain unverified;
production stays NO-GO and the Phase 6 recommendation is CONDITIONAL GO for an
explicitly requested proof/hardening phase, not release authorization.

## Persistent production blocker register — carry into Phases 6 and 7

| ID | Status / severity | Impact | Required remediation | Owner / approval | Verification / evidence |
|---|---|---|---|---|---|
| A — production application schema absent | OPEN / release-blocking | Production application routes cannot use an initialized ledger | Review checksummed application migrations; explicitly authorize production apply; never migrate neon_auth | Database operator + explicit user approval | Inspect production schema/ledger/constraints, idempotent reapply and authenticated smoke. **No resolution evidence**; disposable schema tests do not resolve this. |
| B — excessive runtime DB privileges | OPEN / release-blocking | Runtime compromise has excessive database authority | Create/select least-privilege runtime identity and reviewed grants, separate migration/admin identity; no owner/admin workaround | Database security operator + explicit approval | Verify actual grants with runtime identity, required SQL allowed, DDL/managed-auth mutation denied. **No resolution evidence**; no role/grant changes performed. |
| C — hosted deployment unverified | OPEN / release-blocking | Local success does not establish hosted CORS/Auth/R2/worker/resource correctness | Deploy approved Pages/API/dispatcher/validator/recovery; configure exact origins and resource/stop limits | Hosting operator + approved accounts/configuration | Genuine hosted managed JWT → captain → R2 multipart/resume → durable finalize → worker → preview/CSV; restart/recovery smoke. **No hosted URL or resolution evidence**. |

Production disposition: **NO-GO** until all three blockers have concrete evidence.
Also carry forward the existing 20-vs-10 link contract mismatch, resource-isolation
limitations, aggregation/query performance proof and moving-export consistency.
