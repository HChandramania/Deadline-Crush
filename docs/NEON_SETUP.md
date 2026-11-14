# Neon setup and verification

## What was configured

Linked org `org-restless-salad-93337323`, project `empty-mud-47004781`, branch `production` (`br-still-voice-b3ido4lh`). Installed the latest Neon CLI and its agent plugins/skills via the requested init command. Browser sign-in completed successfully.

`neon.ts` is the CLI-generated Postgres-only configuration. `neon deploy` applies Neon infrastructure; it does not deploy this Python API or React frontend to a hosting provider.

Deploy output:

```text
Auth complete (profile DEFAULT)
Applying to branch production (br-still-voice-b3ido4lh)
Utilized services: Postgres
No changes — branch production already matches the policy.
Pulled 5 Neon variables into .env.local
```

Variables pulled: DATABASE_URL, DATABASE_URL_UNPOOLED, NEON_BRANCH, NEON_BRANCH_ID, NEON_FUNCTION_HOST_SUFFIX. Only Postgres connection variables are needed by this app. Branch/host metadata does not represent another deployed service. Credential values are not included here.

## Wiring

- Existing asyncpg application pool continues using pooled DATABASE_URL, verified TLS, bounded connections and disabled statement cache.
- scripts/python.mjs loads `.env.local` / `.env` for server-side subprocesses. Existing process variables win. Database secrets never enter Vite/browser configuration.
- Root package.json provides build, start, worker, test, verify:neon and dev:web commands. Root dependencies/lockfile include generated @neon/config and @neon/env for Neon tooling, not a replacement database driver.
- scripts/verify_neon.py uses DATABASE_URL_UNPOOLED to apply infra/phase0.sql, checks pooled transactions, starts Uvicorn, calls real DB readiness, enqueues the same UUID twice, runs the separate worker twice (intentional retry then success), checks the API result, stops the temporary API and removes its own test job.
- `.env.local` and `.neon` are ignored. A random local PROBE_TOKEN was added to the ignored file for guarded probes; its value was never printed. Keep it private and rotate it if exposed.
- No authentication, production submission schema, versioning, or production validation workflow was added. The only persistent probe relation is phase0_jobs and its index.

## Commands and observed proof

```powershell
npm.cmd install -g neon@latest
neon.cmd init -y --org-id org-restless-salad-93337323 --project-id empty-mud-47004781 --branch production --services postgres
neon.cmd deploy
npm.cmd run build
npm.cmd run verify:neon
npm.cmd test
npm.cmd start
npm.cmd run dev:web
```

Build output:

```text
vite v7.3.8 building client environment for production...
26 modules transformed.
built in 1.23s
CPython backend imports: PASS
```

End-to-end output against real Neon:

```text
Neon schema and pooled transaction probe: PASS
HTTP /health and /health/dependencies -> real Neon: PASS
API duplicate enqueue -> PostgreSQL -> separate worker retry -> SUCCEEDED (attempts=2): PASS
```

Test output:

```text
19 passed, 3 skipped, 1 warning in 13.11s
```

The two previously skipped direct database tests now execute against Neon. They verify parameterized queries, INSERT RETURNING, unique violation/savepoint rollback, row lock, server timestamp, pool reuse/cleanup, duplicate enqueue, SKIP LOCKED under a competing lock, expired-lease recovery and fencing. Three tests requiring PHASE0_URL plus PROBE_TOKEN were skipped in that suite; the separate verification command proves local API/worker integration with real Neon without needing a hosted API URL. R2 multipart is not claimed verified.

## Failures and limits

- CLI init initially linked offline because credentials were absent. Deploy opened browser authentication; the user completed it and deploy succeeded.
- The installed global CLI was available in the approved shell, but one sandboxed `neon.cmd --version` lookup failed due to its different PATH. No npx fallback was needed; installation and approved CLI execution succeeded.
- The first end-to-end run hit UnicodeEncodeError while printing a non-ASCII arrow on Windows, after database and HTTP assertions passed. Changed console output to ASCII and reran the complete probe successfully.
- One existing Starlette/httpx TestClient deprecation warning remains; no assertions failed in the test suite.
- An optional host argument passed through nested npm scripts was initially misrouted to Vite's root-directory argument. Restarted with `npm run dev:web` successfully. A diagnostic Python dotenv import also found that optional package absent; the app intentionally uses Node's built-in env-file loader and does not require it.
- R2 remains Cloudflare R2 and needs its own credentials/tests. Render/Railway hosting, production load, archive/PDF resource limits and remaining Gate 0 requirements remain outside this Neon setup verification.
- The Neon skills recommend keeping the existing driver and using pooled runtime/direct migration URLs. Used installed [neon skill](https://github.com/neondatabase/agent-skills/tree/main/skills/neon) and [neon-postgres skill](https://github.com/neondatabase/agent-skills/tree/main/skills/neon-postgres).
