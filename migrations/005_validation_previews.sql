-- Crash-safe cleanup ownership for immutable, attempt-specific preview objects.
CREATE TABLE validation_preview_intents (
 object_key text PRIMARY KEY,
 job_id uuid NOT NULL REFERENCES validation_jobs(id),
 generation bigint NOT NULL CHECK(generation>0),
 artifact_type text NOT NULL CHECK(artifact_type IN ('ZIP_PREVIEW','PDF_PREVIEW')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(job_id,generation,artifact_type)
);
CREATE INDEX validation_preview_cleanup ON validation_preview_intents(created_at);
