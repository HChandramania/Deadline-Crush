# Captain-only submission authorization

The user's 2026-11-14 product decision supersedes the earlier all-members mutation
policy. Only the selected team's current leader/captain creates or edits through
new versions, uploads, resumes or finalizes. Ordinary current members may view
their team's submissions. Explicit team_id remains required. All deadline,
grace, immutable metadata, idempotency and latest-finalized-version rules remain.

## Implementation

`apps/api/submissions.py` makes `lock_team` captain-only by default. It locks the
team, hackathon policy and current membership, then requires both database
`team_members.role='CAPTAIN'` and `teams.captain_id=authenticated_user_id`.
Ordinary-member reads explicitly pass `require_captain=False`. Client/JWT role
claims and organizer status never grant captain privileges. Membership existence
means active membership in the current schema; removal is denied on the next check.

Every current submission mutation calls the check in each transaction, before
idempotent lookup and again after storage observations. Team/member locks serialize
captain changes with the database transition. No transaction spans storage I/O.
The old captain may still read as a member, but retries cannot bypass demotion.
The new captain can recover an existing immutable completion intent without
changing its original metadata, session or manifest. Existing records are retained.

Ordinary member mutations return 403 CAPTAIN_REQUIRED; inaccessible teams return
404. Existing rejection audits commit before these errors are returned. No public
captain-transfer API, migration, production role update, managed-auth change or
multipart implementation was added. Future upload/sign/resume handlers must reuse
this captain authorization. Already issued R2 bearer URLs require Phase 3 expiry/
storage enforcement; role changes cannot retroactively erase issued capabilities.

## Contract amendment

PRD/TRD changes are limited to submission actor/authorization wording, personas and
affected journeys. `CAPTAIN_POLICY_CONTRACT.patch` and `captain-contract-audit.json`
preserve exact replacements and before/after hashes. `tests/test_migration.py`
reverses this amendment before the existing Phase 2, Phase 1 and architecture
baseline assertions; historical audit files remain unchanged.

## Tests and results

`tests/test_submission_captain.py` adds real isolated PostgreSQL cases for:

- Captain success and member read-only access, including edits via new versions.
- Valid captain transfer: old captain cannot replay admitted/finalized mutations;
  new captain may admit and recover an existing finalized submission.
- Simultaneous captain/member requests with forged CAPTAIN token claims; captaincy
  of a different team does not authorize the selected team; outsider access denied.
- Revocation during parts observation and object verification: no unauthorized
  intent/finalization writes; the new captain can recover the retained state.
- An admission waiting behind a committed captain transfer observes the new role.

The former all-members mutation assertions in `tests/test_phase2_database.py` now
assert 403 while keeping member reads and captain finalization checks.
Verification commands:

```powershell
node scripts/python.mjs phase03
node --test apps/web/api.test.mjs
```

Observed results: **113 distinct backend tests passed, zero failed, zero skipped**.
The main batch passed 112 in 470.02s and deliberately deselected the remote-job
case, which passed separately in 3.40s with the dedicated workers running. All
five new captain cases passed against real isolated PostgreSQL; verified identity
and storage observations are controlled test doubles. Existing actual Neon/R2
and independent worker regression checks remained enabled and passed. The two
worker processes recovered from injected disconnects and closed their pools;
disposable schema/R2 probe cleanup passed. Health smoke: **3/3**, zero HTTP errors;
this is not a performance benchmark. Frontend authentication/request tests:
**5 passed, zero failed/skipped**. The existing Starlette/httpx warning remains.

The full batch includes the existing deadline/grace boundaries, concurrent version
allocation, immutable metadata, idempotency, finalization rollback, durable-intent
recovery, highest-finalized-version selection and Phase 1 regressions. No production
application data/role or managed-auth data was changed. No migration was added,
no hosted deployment was attempted, and real submission upload/resume enforcement
remains Phase 3 work. Evidence: captain-policy-results.json, phase03-results.json
and phase03-r2-results.json. Earlier phase2-results.json remains historical evidence
for the pre-amendment Phase 2 run.

Changed files: `apps/api/submissions.py`, `tests/test_submission_captain.py`,
`tests/test_phase2_database.py`, `tests/test_migration.py`, `PRD.md`, `TRD.md`,
`docs/decisions.md`, `docs/PHASE_2.md`, `docs/runbook.md`, this report,
`docs/CAPTAIN_POLICY_CONTRACT.patch`, `docs/captain-contract-audit.json`,
`docs/captain-policy-results.json`; refreshed `docs/phase03-results.json` and
`docs/phase03-r2-results.json`.
