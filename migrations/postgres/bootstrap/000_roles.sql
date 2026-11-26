-- Run once per environment as the Entra admin (the migrate workload identity), before 001.
-- Maps the api/worker managed identities to least-privilege database roles.
-- Replace :api_identity / :worker_identity with the identity names from `terraform output`.
SELECT * FROM pgaadauth_create_principal(:'api_identity', false, false);
SELECT * FROM pgaadauth_create_principal(:'worker_identity', false, false);
CREATE ROLE factory_rw NOLOGIN;
GRANT USAGE ON SCHEMA public TO factory_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO factory_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO factory_rw;
GRANT factory_rw TO :"api_identity", :"worker_identity";
-- Evidence is append-only for everyone (enforced again by the trigger in 002).
