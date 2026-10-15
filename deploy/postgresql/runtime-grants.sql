-- Apply after migration or restore, before starting the backend. Safe to repeat.
\set ON_ERROR_STOP on
BEGIN;
GRANT USAGE ON SCHEMA public TO scrap_app, scrap_backup;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO scrap_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO scrap_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO scrap_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO scrap_backup;
-- Runtime code can inspect the revision but must never rewrite migration history.
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM scrap_app;
COMMIT;
