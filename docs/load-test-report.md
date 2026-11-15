# Phase 0 load status

Phase 3 adds real direct multipart transfer and crash-boundary reconciliation
correctness tests, not a throughput benchmark. R2 bytes bypass FastAPI; control-plane
API p95 and direct-transfer performance remain unmeasured. No full load run in this
phase. API/recovery R2 concurrency is bounded at five/two per process.

The final isolated infrastructure rerun passed three local health checks with zero
HTTP errors. This remains a health smoke, not a workload p95 measurement. Backend
correctness coverage is 162 distinct passing cases across partitioned runs; see
PHASE_3.md for the interrupted run, corrected failures and classification.

Phase 1 reran the existing isolated local health smoke: 3/3 checks, zero HTTP failures. This remains a three-request smoke test, not a throughput or p95 benchmark. See [Phase 1 evidence and limits](PHASE_1.md).

No 2,000-team load run, latency benchmark, reconciliation guarantee, or production performance claim. `tests/health.js` passed three local Uvicorn health requests after the CPython migration (3/3 checks, zero HTTP failures). This is a smoke check only. Direct R2 transfer timing must be measured separately in Phase 5. Account CPU, memory, duration and quota limits remain unverified.
# Phase 2 scope

## Phase 4 scope

Phase 4 adds local Linux parser resource checks, real PostgreSQL lease/fencing,
and actual R2-to-Neon validation/preview correctness tests. The existing k6 health
smoke passed 3/3 checks with zero errors. This is not a workload latency benchmark.
No 2,000-team load run, <300 ms API p95 claim, or zero-loss end-to-end claim is made.
Validation runs in separate bounded processes and does not execute within upload
or admission API requests. See PHASE_4.md for exact verification scope.

Phase 2 adds correctness/concurrency tests against isolated real PostgreSQL with
controlled storage doubles. It runs no full load benchmark. The API p95 <300ms and
2,000-team/10-minute goals remain unverified; direct R2 transfer must be measured
separately. See [Phase 2 evidence and limitations](PHASE_2.md).

# Phase 5 handoff — no benchmark claim

Phase 5 adds participant polling (5 seconds, details 7 seconds), organizer polling
(10 seconds), bounded keyset reads and 100-row streamed CSV pages. Hidden tabs use
at least 30 seconds and errors back off to 60 seconds. These are implementation
bounds, not measured capacity. No 2,000-team workload, sub-300 ms API p95 or zero-loss
reconciliation result is claimed in Phase 5. Browser PUT transfer must remain a
separate measurement from control-plane API latency.

Carry the OPEN production-schema, excessive-runtime-privilege and hosted-integration
blockers from `PHASE_5.md` into Phases 6 and 7. Do not begin the load phase or apply
production changes without the user's next instruction.
