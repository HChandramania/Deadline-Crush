-- Nullable additions preserve existing Phase 1 fixture/data compatibility.
ALTER TABLE submission_versions ADD COLUMN admitted_by uuid REFERENCES users(id);
ALTER TABLE submission_versions ADD COLUMN filename text NOT NULL DEFAULT '';
ALTER TABLE submission_versions ADD COLUMN content_type text NOT NULL DEFAULT '';
ALTER TABLE submission_versions ADD COLUMN deadline_at timestamptz;
ALTER TABLE submission_versions ADD COLUMN grace_until timestamptz;
ALTER TABLE submission_versions DROP CONSTRAINT submission_versions_team_id_idempotency_key_key;
CREATE UNIQUE INDEX version_admission_retry ON submission_versions(team_id,admitted_by,idempotency_key);
ALTER TABLE completion_intents ADD COLUMN admitted_by uuid REFERENCES users(id);
ALTER TABLE completion_intents DROP CONSTRAINT completion_intents_idempotency_key_key;
CREATE INDEX submission_finalized_latest ON submission_versions(team_id,version_no DESC)
 WHERE lifecycle='FINALIZED';
ALTER TABLE submission_versions ADD CONSTRAINT admitted_policy CHECK (
 (admitted_by IS NULL AND deadline_at IS NULL AND grace_until IS NULL) OR
 (admitted_by IS NOT NULL AND deadline_at IS NOT NULL AND grace_until IS NOT NULL
  AND admitted_at<=deadline_at AND grace_until=deadline_at+interval '600 seconds'));

CREATE FUNCTION submission_time_eligible(stamp timestamptz, cutoff timestamptz)
 RETURNS boolean LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT stamp<=cutoff $$;

-- The session's identity and authorized range are fixed at admission. R2 upload
-- identity may be assigned once by the later storage adapter, never replaced.
CREATE FUNCTION guard_upload() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
DECLARE cutoff timestamptz;
BEGIN
 IF TG_OP='DELETE' OR
 (to_jsonb(NEW)-ARRAY['status','r2_upload_id','completed_at','last_error']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['status','r2_upload_id','completed_at','last_error']) OR
 (OLD.r2_upload_id IS NOT NULL AND NEW.r2_upload_id IS DISTINCT FROM OLD.r2_upload_id) OR
 (OLD.status<>NEW.status AND NOT (
   (OLD.status='INITIATING' AND NEW.status IN ('ACTIVE','ABORTED','EXPIRED')) OR
   (OLD.status='ACTIVE' AND NEW.status IN ('COMPLETING','ABORTED','EXPIRED')) OR
   (OLD.status='COMPLETING' AND NEW.status IN ('COMPLETED','ABORTED')))) THEN
  RAISE EXCEPTION 'immutable upload authorization or illegal transition' USING ERRCODE='23514';
 END IF;
 IF OLD.r2_upload_id IS NULL AND NEW.r2_upload_id IS NOT NULL THEN
  SELECT deadline_at INTO cutoff FROM submission_versions WHERE id=NEW.version_id;
  IF cutoff IS NOT NULL AND clock_timestamp()>cutoff THEN
   RAISE EXCEPTION 'new upload identity after deadline' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_upload BEFORE UPDATE OR DELETE ON upload_sessions
 FOR EACH ROW EXECUTE FUNCTION guard_upload();

CREATE FUNCTION check_upload_authorization() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
DECLARE version submission_versions;
BEGIN
 SELECT * INTO version FROM submission_versions WHERE id=NEW.version_id;
 IF version.admitted_by IS NOT NULL AND
    (NEW.object_key<>version.object_key OR NEW.part_size<>8388608 OR
     NEW.expected_parts<>(version.expected_size+8388607)/8388608 OR
     NEW.expires_at<>version.grace_until) THEN
  RAISE EXCEPTION 'upload authorization does not match admitted version' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER upload_authorization BEFORE INSERT ON upload_sessions
 FOR EACH ROW EXECUTE FUNCTION check_upload_authorization();

CREATE FUNCTION guard_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.lifecycle<>NEW.lifecycle AND NOT (
  (OLD.lifecycle='CREATED' AND NEW.lifecycle IN ('UPLOADING','EXPIRED','ABORTED')) OR
  (OLD.lifecycle='UPLOADING' AND NEW.lifecycle IN ('COMPLETING','EXPIRED','ABORTED')) OR
  (OLD.lifecycle='COMPLETING' AND NEW.lifecycle IN ('FINALIZED','ABORTED'))) THEN
  RAISE EXCEPTION 'illegal submission transition' USING ERRCODE='23514';
 END IF;
 IF NEW.lifecycle='FINALIZED' AND (NEW.finalized_at IS NULL) THEN
  RAISE EXCEPTION 'finalization timestamp required' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER legal_version_transition BEFORE UPDATE ON submission_versions
 FOR EACH ROW EXECUTE FUNCTION guard_lifecycle();

CREATE FUNCTION guard_intent_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status<>NEW.status AND NOT (
  (OLD.status IN ('ADMITTED','RECOVERY_REQUIRED') AND NEW.status IN ('STORAGE_COMPLETED','RECOVERY_REQUIRED','FAILED')) OR
  (OLD.status='STORAGE_COMPLETED' AND NEW.status='FINALIZED')) THEN
  RAISE EXCEPTION 'illegal completion transition' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER legal_intent_transition BEFORE UPDATE ON completion_intents
 FOR EACH ROW EXECUTE FUNCTION guard_intent_state();

-- New admissions may finalize only with the entire durable ledger at commit.
-- Existing Phase 1 rows (which have no admitted_by) remain valid.
CREATE FUNCTION check_finalization_ledger() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
BEGIN
 IF NEW.lifecycle='FINALIZED' AND NEW.admitted_by IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM upload_sessions s JOIN completion_intents i ON i.upload_session_id=s.id
  JOIN validation_jobs j ON j.version_id=s.version_id
  JOIN outbox o ON o.aggregate_id=s.version_id AND o.event_type='VALIDATE'
  WHERE s.version_id=NEW.id AND s.status='COMPLETED' AND s.completed_at IS NOT NULL
   AND i.status='FINALIZED' AND i.admitted_at<=NEW.grace_until) THEN
  RAISE EXCEPTION 'incomplete finalization ledger' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER finalization_ledger AFTER INSERT OR UPDATE ON submission_versions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_finalization_ledger();
