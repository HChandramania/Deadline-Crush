-- Foundation guards. Admission/state transition services belong to later phases.
ALTER TABLE submission_versions ADD CHECK (jsonb_typeof(links)='array');
ALTER TABLE completion_intents ADD CHECK (jsonb_typeof(manifest)='array');
ALTER TABLE completion_intents ADD CHECK (attempt_count >= 0);
ALTER TABLE validation_jobs ADD CHECK (attempts >= 0 AND lease_token >= 0);
ALTER TABLE outbox ADD CHECK (attempts >= 0);
CREATE INDEX audit_events_team_time ON audit_events(team_id,created_at);
CREATE INDEX late_attempts_team_time ON late_attempts(team_id,occurred_at);
CREATE INDEX submission_versions_team_status ON submission_versions(team_id,lifecycle,version_no);
CREATE INDEX validation_artifacts_version ON validation_artifacts(version_id);

CREATE FUNCTION guard_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR
 (to_jsonb(NEW) - ARRAY['lifecycle','validation_status','finalized_at','verified_sha256']) IS DISTINCT FROM
 (to_jsonb(OLD) - ARRAY['lifecycle','validation_status','finalized_at','verified_sha256']) OR
 (OLD.lifecycle='FINALIZED' AND
  (to_jsonb(NEW)-'validation_status') IS DISTINCT FROM (to_jsonb(OLD)-'validation_status')) THEN
  RAISE EXCEPTION 'immutable submission version' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_version BEFORE UPDATE OR DELETE ON submission_versions
 FOR EACH ROW EXECUTE FUNCTION guard_version();

CREATE FUNCTION guard_completion() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR
 (to_jsonb(NEW)-ARRAY['status','attempt_count','last_error']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['status','attempt_count','last_error']) THEN
  RAISE EXCEPTION 'immutable completion manifest' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER immutable_completion BEFORE UPDATE OR DELETE ON completion_intents
 FOR EACH ROW EXECUTE FUNCTION guard_completion();

CREATE FUNCTION guard_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'audit events are append only' USING ERRCODE='23514';
END $$;
CREATE TRIGGER append_only_audit BEFORE UPDATE OR DELETE ON audit_events
 FOR EACH ROW EXECUTE FUNCTION guard_audit();

CREATE FUNCTION check_selection() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM teams t JOIN submission_versions v ON v.id=t.selected_version_id
            WHERE t.id=NEW.id AND (v.team_id<>t.id OR v.version_no<>t.selected_version_no
                                   OR v.lifecycle<>'FINALIZED')) THEN
  RAISE EXCEPTION 'invalid selected version' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER selected_version_number AFTER INSERT OR UPDATE ON teams
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_selection();

CREATE FUNCTION check_captain() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
DECLARE target_team uuid;
BEGIN
 IF TG_TABLE_NAME='teams' THEN target_team=NEW.id;
 ELSE target_team=COALESCE(NEW.team_id,OLD.team_id); END IF;
 IF EXISTS (SELECT 1 FROM teams t JOIN team_members m ON m.team_id=t.id
            WHERE t.id=target_team
              AND ((m.user_id=t.captain_id) <> (m.role='CAPTAIN'))) THEN
  RAISE EXCEPTION 'captain role mismatch' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER captain_role AFTER INSERT OR UPDATE OR DELETE ON team_members
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_captain();
CREATE CONSTRAINT TRIGGER team_captain_role AFTER INSERT OR UPDATE ON teams
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_captain();

CREATE FUNCTION guard_deadline() RETURNS trigger LANGUAGE plpgsql
 SET search_path FROM CURRENT AS $$
BEGIN
 IF (NEW.deadline_at,NEW.grace_seconds) IS DISTINCT FROM (OLD.deadline_at,OLD.grace_seconds)
 AND EXISTS (SELECT 1 FROM submission_versions v JOIN teams t ON t.id=v.team_id WHERE t.hackathon_id=OLD.id) THEN
  RAISE EXCEPTION 'deadline policy change requires separate audited operation' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER protect_deadline BEFORE UPDATE ON hackathons
 FOR EACH ROW EXECUTE FUNCTION guard_deadline();
