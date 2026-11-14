# Managed email signup

Phase 1 now implements the application identity mapping, secure FastAPI JWT verification and team authorization. Use [PHASE_1.md](PHASE_1.md) for current setup and results. The following sections retain the earlier signup-only checkpoint and its then-unresolved limitations.

The user explicitly selected Neon managed Better Auth on 2026-11-13. This supersedes the earlier request for application-owned password hashing and JWT issuance. Auth uses the `neon_auth` schema. No custom password tables, Better Auth server, or auth migrations are needed.

The existing React/Vite app uses `@neon/auth` 0.8.2. It supports email/password signup, signin, signout and current-session restoration, with pending and error states. No team privileges are assigned by signup. No submission API is enabled by this change.

## Setup

```powershell
npm.cmd install -g neon@latest
neon init -y --project-id empty-mud-47004781 --branch production --services postgres,auth
neon deploy
npm.cmd ci --prefix apps/web
```

The existing neon.ts is preserved with `auth: true`. Deploy pulled Auth and Postgres variables into ignored root `.env`. Set only `VITE_NEON_AUTH_URL` in ignored `apps/web/.env.local` to the pulled `NEON_AUTH_BASE_URL`. Vite reads configuration from apps/web; do not copy server credentials there. Configure the same public variable in Pages before building.

```powershell
npm.cmd run dev --prefix apps/web
npm.cmd run build --prefix apps/web
node apps/web/verify-auth.mjs
npm.cmd test
```

The live smoke script creates a synthetic account with a random password held in memory, checks signup/current user, signout, wrong-password rejection, signin and session restoration through a fresh SDK client, then signs out. It prints only pass/fail labels. It retains the synthetic account and discards its credentials. Each invocation creates another account; do not use it as a load test. Two synthetic accounts were created during implementation, including the first run that stopped on the expected wrong-password exception.

## Observed results and limits

Live signup, current-user lookup, signin, invalid-password rejection, signout and fresh-client cookie-session restoration passed on the requested production branch. TypeScript/Vite build passed. The verification uses the actual installed SDK and a Node in-memory cookie jar with a localhost Origin. Browser UI interaction, browser reload persistence and third-party-cookie behavior remain unverified because no browser automation surface was available. Email delivery/verification was not tested; synthetic test addresses have no inbox.

Final validation: `npm.cmd run build` passed frontend build and CPython import checks. Local Phase 0/migration regression selection passed 17 tests. `npm.cmd test` completed with 20 passed, 2 skipped and 2 failed: the configured remote R2 probe returned 503 on multipart creation, and remote worker job completion was not observed. These remote infrastructure failures remain unresolved; no tests were weakened. An initial sandbox pytest import permission failure was resolved by running the tests with approved access. Git ignore checks covered all three actual env files, and the browser bundle contained neither a PostgreSQL URL nor an R2 secret-key configuration name.

Before publishing, register the real frontend origin using `neon neon-auth domain add <origin>` and configure production email settings as required. Do not weaken domain or network protections. npm reported a peer version mismatch between the SDK's better-fetch dependency and an upstream OAuth provider; installation completed with zero reported vulnerabilities and the email flow passed. Rollup removed two malformed dependency annotations during build.

## Phase 1 contract discrepancy

TRD's illustrative users table still describes local `password_hash` storage; it has not been applied. The earlier Phase 1 schema draft in migrations/001_foundation.sql is unapplied and must be reviewed for managed identity mapping before use. Do not run it as an authentication migration. Team membership, organizer provisioning and FastAPI JWT verification remain Phase 1 work. Browser session display is not backend authorization. The existing probes remain protected by their independent PROBE_TOKEN.

Keep AGENTS.md/PRD.md/TRD.md intact pending a reviewed identity contract amendment: use Neon's JWKS and issuer to verify access tokens on FastAPI, map the verified subject to application users, and keep team roles under application control. Never trust client-provided roles. No external broker, Data API, Functions or replacement storage was added.
