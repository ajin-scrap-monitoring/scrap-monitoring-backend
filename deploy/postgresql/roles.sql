-- Run once by the official PostgreSQL image on a completely new volume.
BEGIN;
DO $$ BEGIN
    IF current_setting('server_version_num')::integer / 10000 <> 17 THEN
        RAISE EXCEPTION 'This deployment requires PostgreSQL 17';
    END IF;
END $$;

CREATE ROLE scrap_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'owner_verifier';
CREATE ROLE scrap_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'app_verifier';
CREATE ROLE scrap_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD :'backup_verifier';

-- A malformed SCRAM-looking value can be treated as a plaintext password by PostgreSQL.
-- Require unchanged catalog storage before committing; \gset keeps verifiers out of output.
SELECT count(*) = 4 AS verifiers_stored
FROM (VALUES
    ('postgres', :'admin_verifier'),
    ('scrap_owner', :'owner_verifier'),
    ('scrap_app', :'app_verifier'),
    ('scrap_backup', :'backup_verifier')
) AS expected(name, verifier)
JOIN pg_authid ON rolname = expected.name AND rolpassword = expected.verifier
\gset
\if :verifiers_stored
\else
    DO $$ BEGIN RAISE EXCEPTION 'Invalid SCRAM verifier; initialization aborted'; END $$;
\endif

ALTER DATABASE scrap_monitoring OWNER TO scrap_owner;
REVOKE ALL ON DATABASE scrap_monitoring FROM PUBLIC;
GRANT CONNECT ON DATABASE scrap_monitoring TO scrap_owner, scrap_app, scrap_backup;
ALTER SCHEMA public OWNER TO scrap_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO scrap_app, scrap_backup;

-- Alembic must connect as scrap_owner so that its new objects inherit these grants.
ALTER DEFAULT PRIVILEGES FOR ROLE scrap_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO scrap_app;
ALTER DEFAULT PRIVILEGES FOR ROLE scrap_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO scrap_app;
ALTER DEFAULT PRIVILEGES FOR ROLE scrap_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO scrap_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE scrap_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO scrap_backup;
COMMIT;
