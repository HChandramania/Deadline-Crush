# Deadline Crush — Phase 0

Infrastructure spikes only. No authentication, submissions, versioning, or production upload lifecycle.

See [setup and commands](docs/runbook.md), [findings](docs/PHASE_0_FEASIBILITY.md), and [decisions](docs/decisions.md).

Neon is linked to the requested production branch. After `neon deploy` pulls the server-only `.env.local` (or `.env`), run `npm run build`, `npm start` for the API, and `npm run dev:web` for the frontend. `npm run worker` starts the separate Python job process. `npm run verify:neon` applies only the Phase 0 probe schema and tests the real API/database/worker path, then removes its test job. It requires an empty Phase 0 job table and an unused localhost port 8788. See [Neon verification evidence](docs/NEON_SETUP.md).

`apps/api` is FastAPI/Uvicorn on CPython 3.12; `apps/consumer` is a dedicated PostgreSQL job process; `apps/web` is a React/Vite placeholder on Cloudflare Pages. `packages/shared` contains pool, configuration and disposable job probes. `infra` holds Render/Railway deployment alternatives, the isolated probe schema, and retained R2 CORS policy. GitHub Actions runs local checks. No deployment has been performed.
