-- Explicit opt-in schema for an isolated Phase 0 database only.
CREATE TABLE IF NOT EXISTS phase0_jobs (
 id uuid PRIMARY KEY,
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','SUCCEEDED')),
 attempts integer NOT NULL DEFAULT 0,
 lease_owner uuid,
 lease_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS phase0_jobs_available ON phase0_jobs(available_at,id);
