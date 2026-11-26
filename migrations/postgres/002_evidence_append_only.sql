-- Evidence is append-only (REQ 050, NFR 07): updates and deletes are rejected at the database,
-- independently of application code. Retention purges run under a separate, audited role that
-- detaches partitions per the approved schedule (NFR 11) rather than deleting rows.
CREATE OR REPLACE FUNCTION factory_evidence_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'evidence is append-only (%.%)', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_no_update ON evidence;
CREATE TRIGGER evidence_no_update BEFORE UPDATE OR DELETE ON evidence
  FOR EACH ROW EXECUTE FUNCTION factory_evidence_immutable();

-- Runtime role: DML only, no DDL; auditors: read-only on evidence.
-- (Role names are placeholders; create them through the platform's identity process.)
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO factory_runtime;
-- REVOKE UPDATE, DELETE ON evidence FROM factory_runtime;
-- GRANT SELECT ON evidence TO factory_auditor;
