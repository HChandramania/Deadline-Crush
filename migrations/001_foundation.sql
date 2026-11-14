CREATE TABLE hackathons (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 name text NOT NULL, deadline_at timestamptz NOT NULL,
 grace_seconds integer NOT NULL DEFAULT 600 CHECK (grace_seconds = 600),
 max_file_bytes bigint NOT NULL DEFAULT 104857600 CHECK (max_file_bytes > 0)
);
CREATE TABLE users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 auth_issuer text NOT NULL CHECK (length(auth_issuer) BETWEEN 1 AND 2048),
 auth_subject text NOT NULL CHECK (length(auth_subject) BETWEEN 1 AND 255),
 email text NOT NULL DEFAULT '',
 role text NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('MEMBER','ORGANIZER')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (auth_issuer, auth_subject)
);
CREATE TABLE teams (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 hackathon_id uuid NOT NULL REFERENCES hackathons(id),
 name text NOT NULL,
 captain_id uuid NOT NULL REFERENCES users(id),
 idempotency_key uuid NOT NULL,
 request_hash char(64) NOT NULL,
 next_version_no integer NOT NULL DEFAULT 1 CHECK (next_version_no > 0),
 selected_version_id uuid,
 selected_version_no integer,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (captain_id, idempotency_key),
 CHECK ((selected_version_id IS NULL) = (selected_version_no IS NULL)),
 CHECK (selected_version_no IS NULL OR selected_version_no > 0)
);
CREATE TABLE team_members (
 team_id uuid NOT NULL REFERENCES teams(id),
 user_id uuid NOT NULL REFERENCES users(id),
 role text NOT NULL DEFAULT 'MEMBER' CHECK (role IN ('CAPTAIN','MEMBER')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY (team_id,user_id)
);
CREATE INDEX team_members_user ON team_members(user_id,team_id);
CREATE UNIQUE INDEX team_one_captain ON team_members(team_id) WHERE role='CAPTAIN';
ALTER TABLE teams ADD CONSTRAINT captain_is_member FOREIGN KEY (id,captain_id)
 REFERENCES team_members(team_id,user_id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX teams_hackathon ON teams(hackathon_id);
CREATE TABLE submission_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid NOT NULL REFERENCES teams(id),
 version_no integer NOT NULL CHECK (version_no > 0),
 idempotency_key uuid NOT NULL, request_hash char(64) NOT NULL,
 title text NOT NULL, description text NOT NULL DEFAULT '',
 links jsonb NOT NULL DEFAULT '[]'::jsonb,
 object_key text NOT NULL UNIQUE,
 expected_size bigint NOT NULL CHECK (expected_size > 0 AND expected_size <= 104857600),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 admitted_at timestamptz NOT NULL,
 finalized_at timestamptz,
 lifecycle text NOT NULL DEFAULT 'CREATED' CHECK (lifecycle IN ('CREATED','UPLOADING','COMPLETING','FINALIZED','EXPIRED','ABORTED')),
 validation_status text NOT NULL DEFAULT 'NOT_STARTED' CHECK (validation_status IN ('NOT_STARTED','CHECKING','ACCEPTED','NEEDS_FIX')),
 verified_sha256 char(64),
 UNIQUE (team_id,version_no), UNIQUE (team_id,idempotency_key),
 UNIQUE (team_id,id)
);
ALTER TABLE teams ADD CONSTRAINT selected_version_same_team
 FOREIGN KEY (id,selected_version_id) REFERENCES submission_versions(team_id,id)
 DEFERRABLE INITIALLY IMMEDIATE;
CREATE TABLE upload_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL UNIQUE REFERENCES submission_versions(id),
 object_key text NOT NULL UNIQUE,
 r2_upload_id text,
 status text NOT NULL CHECK (status IN ('INITIATING','ACTIVE','COMPLETING','COMPLETED','ABORTED','EXPIRED')),
 part_size integer NOT NULL CHECK (part_size >= 5242880),
 expected_parts integer NOT NULL CHECK (expected_parts BETWEEN 1 AND 10000),
 expires_at timestamptz NOT NULL,
 completed_at timestamptz,
 last_error text
);
CREATE TABLE completion_intents (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 upload_session_id uuid NOT NULL UNIQUE REFERENCES upload_sessions(id),
 idempotency_key uuid NOT NULL UNIQUE,
 manifest jsonb NOT NULL, manifest_sha256 char(64) NOT NULL,
 admitted_at timestamptz NOT NULL,
 status text NOT NULL CHECK (status IN ('ADMITTED','STORAGE_COMPLETED','FINALIZED','RECOVERY_REQUIRED','FAILED')),
 attempt_count integer NOT NULL DEFAULT 0,
 last_error text
);
CREATE TABLE validation_jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL UNIQUE REFERENCES submission_versions(id),
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
 attempts integer NOT NULL DEFAULT 0,
 lease_owner uuid, lease_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 result jsonb, last_error text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX validation_jobs_ready ON validation_jobs(available_at,created_at)
 WHERE status IN ('PENDING','RUNNING');
CREATE TABLE outbox (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 event_type text NOT NULL, aggregate_id uuid NOT NULL,
 payload jsonb NOT NULL DEFAULT '{}'::jsonb,
 status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENDING','SENT')),
 attempts integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 lease_until timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (event_type,aggregate_id)
);
CREATE INDEX outbox_pending ON outbox(available_at) WHERE status IN ('PENDING','SENDING');
CREATE TABLE validation_artifacts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version_id uuid NOT NULL REFERENCES submission_versions(id),
 artifact_type text NOT NULL, object_key text NOT NULL,
 UNIQUE(version_id,artifact_type)
);
CREATE TABLE audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid REFERENCES teams(id), actor_id uuid REFERENCES users(id),
 event_type text NOT NULL, entity_id uuid, detail jsonb NOT NULL DEFAULT '{}'::jsonb,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE late_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 team_id uuid REFERENCES teams(id), actor_id uuid REFERENCES users(id),
 action text NOT NULL, reason text NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
