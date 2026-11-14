-- One create attempt per immutable session. An uncertain attempt is never
-- replaced: late/unknown creates are repeatedly swept by exact key after expiry.
CREATE TABLE storage_operations (
 upload_session_id uuid PRIMARY KEY REFERENCES upload_sessions(id),
 create_started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 create_state text NOT NULL DEFAULT 'STARTED' CHECK(create_state IN ('STARTED','BOUND','ABANDONED')),
 recovery_token bigint NOT NULL DEFAULT 0,
 recovery_until timestamptz,
 available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 attempts integer NOT NULL DEFAULT 0 CHECK(attempts>=0),
 last_error text
);
CREATE INDEX storage_operations_ready ON storage_operations(available_at);
CREATE UNIQUE INDEX upload_request_idempotency ON audit_events
 (team_id,actor_id,event_type,(detail->>'key'))
 WHERE event_type IN ('UPLOAD_INITIALIZE_REQUEST','UPLOAD_SIGN_REQUEST','UPLOAD_RESUME_REQUEST');
CREATE FUNCTION guard_storage_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR NEW.upload_session_id<>OLD.upload_session_id OR
 NEW.create_started_at<>OLD.create_started_at OR
 (OLD.create_state<>NEW.create_state AND NOT
  (OLD.create_state='STARTED' AND NEW.create_state IN ('BOUND','ABANDONED'))) THEN
  RAISE EXCEPTION 'immutable storage operation' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_storage_operation BEFORE UPDATE OR DELETE ON storage_operations
 FOR EACH ROW EXECUTE FUNCTION guard_storage_operation();
