-- Least-privilege database roles for Mawjood.
--
-- Two roles, because they need different things and conflating them means the
-- application can rewrite its own schema:
--
--   mawjood_migrate  — owns the schema. Runs `alembic upgrade`. DDL rights.
--   mawjood_app      — the application. DML only, on the tables it uses. No DDL,
--                      no ownership, and specifically no DELETE on audit_log.
--
-- Run as a superuser, once, after creating the database:
--
--     psql -v app_password="'...'" -v migrate_password="'...'" \
--          -d mawjood -f scripts/grants.sql
--
-- Then point MAWJOOD_DATABASE_URL at mawjood_app and run migrations as
-- mawjood_migrate. DEPLOY.md walks through it.

\set ON_ERROR_STOP on

-- --------------------------------------------------------------------------
-- Roles
-- --------------------------------------------------------------------------
-- \gexec rather than a DO block: psql does not interpolate its variables
-- inside dollar quoting, so :app_password would arrive literally.
SELECT format('CREATE ROLE mawjood_migrate LOGIN PASSWORD %L', :migrate_password)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mawjood_migrate')
\gexec

SELECT format('CREATE ROLE mawjood_app LOGIN PASSWORD %L', :app_password)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mawjood_app')
\gexec

-- --------------------------------------------------------------------------
-- Schema ownership
-- --------------------------------------------------------------------------
-- The migration role owns the schema; the app role may only look at it.
ALTER SCHEMA public OWNER TO mawjood_migrate;
GRANT USAGE ON SCHEMA public TO mawjood_app;

-- Nobody gets CREATE on public. Without this revoke, PUBLIC can create objects
-- in the schema on older PostgreSQL, which makes "no DDL for the app" untrue.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database())
\gexec
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO mawjood_app, mawjood_migrate', current_database()
)
\gexec

-- --------------------------------------------------------------------------
-- Table privileges
-- --------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mawjood_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mawjood_app;

-- New tables from future migrations inherit the same grants, so a migration
-- does not silently leave the app unable to read its own new table.
ALTER DEFAULT PRIVILEGES FOR ROLE mawjood_migrate IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mawjood_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mawjood_migrate IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO mawjood_app;

-- --------------------------------------------------------------------------
-- The audit log
-- --------------------------------------------------------------------------
-- Append-only in privilege as well as by trigger. The trigger is the primary
-- control and it is the one that carries the deliberate purge escape hatch;
-- this is the second lock on the same door, and it means an attacker who finds
-- a way to disable the trigger still cannot rewrite history.
--
-- UPDATE is revoked outright: nothing in Mawjood ever updates an audit row.
--
-- DELETE is *retained* because PDPL erasure and the retention sweep must be
-- able to remove rows, and both go through the trigger's `mawjood.allow_purge`
-- gate. Revoking it here would mean erasure could not run as the app role,
-- which would push a routine compliance operation onto the migration role — a
-- worse outcome than the one it prevents.
REVOKE UPDATE ON audit_log FROM mawjood_app;

-- --------------------------------------------------------------------------
-- Verification
-- --------------------------------------------------------------------------
-- Printed so whoever runs this can see the result rather than assume it.
SELECT
    'mawjood_app can CREATE in public'  AS check,
    has_schema_privilege('mawjood_app', 'public', 'CREATE') AS result
UNION ALL SELECT
    'mawjood_app can UPDATE audit_log',
    has_table_privilege('mawjood_app', 'audit_log', 'UPDATE')
UNION ALL SELECT
    'mawjood_app can SELECT conversations',
    has_table_privilege('mawjood_app', 'conversations', 'SELECT')
UNION ALL SELECT
    'mawjood_app can INSERT messages',
    has_table_privilege('mawjood_app', 'messages', 'INSERT');
-- Expected: false, false, true, true.
