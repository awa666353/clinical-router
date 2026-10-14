-- W3 Supabase database security for the pilot schema (B1/B2/B11).
--
-- Run as superuser/postgres, passing the password as a psql variable:
--   psql -v ON_ERROR_STOP=1 -v laya_app_password="$LAYA_APP_PASSWORD" \
--        -f 20260930120200_security.sql
--
-- Dashboard SQL editor (no psql variables): create role laya_app manually once,
-- then remove or replace the "ALTER ROLE ... :'laya_app_password'" line below.
-- The role is created without a password and gets one from that ALTER line, so
-- re-running this file is safe.
--
-- Pass 1 (as postgres): creates laya_app, the pilot schema and schema_migrations.
-- Pass 2 (as laya_app): run pilot_schema.sql then app_users.sql.
-- Pass 3 (as postgres): re-run this file to enable RLS on the created tables.
--
-- Keep the pilot schema out of Dashboard -> Data API -> Exposed schemas.
-- Tables must be owned by laya_app; do not FORCE RLS or the owner is blocked.

-- Guide W3 line is CREATE ROLE laya_app LOGIN PASSWORD :'laya_app_password';
-- It is split below for idempotency: psql cannot interpolate :'...' inside a
-- dollar-quoted DO block, so the password is applied by ALTER ROLE afterwards.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'laya_app') THEN
        CREATE ROLE laya_app LOGIN;
    END IF;
END
$$;

ALTER ROLE laya_app WITH LOGIN PASSWORD :'laya_app_password';

-- Supabase's postgres role has CREATEROLE but is not a superuser, and role
-- membership does not necessarily include SET ROLE.  CREATE SCHEMA ...
-- AUTHORIZATION laya_app requires the executor to be able to SET ROLE
-- laya_app, otherwise pass 1 fails with:
--   ERROR: must be able to SET ROLE "laya_app"
-- Keep this idempotent and before the schema statement.
DO $$
BEGIN
    IF current_user = 'postgres' THEN
        EXECUTE 'GRANT laya_app TO postgres WITH SET TRUE';
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS pilot AUTHORIZATION laya_app;
ALTER ROLE laya_app SET search_path = pilot;

-- Schema privileges: PUBLIC always exists; anon/authenticated may not exist on
-- a plain local Postgres, so guard those revokes on pg_roles.
REVOKE ALL ON SCHEMA pilot FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA pilot FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA pilot FROM authenticated';
    END IF;
END
$$;

-- Future objects created by the role running this file.
ALTER DEFAULT PRIVILEGES IN SCHEMA pilot REVOKE ALL ON TABLES FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA pilot REVOKE ALL ON TABLES FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA pilot REVOKE ALL ON TABLES FROM authenticated';
    END IF;
END
$$;

-- Version ledger for the psql-based migration runner.
CREATE TABLE IF NOT EXISTS pilot.schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Pass 2 runs as laya_app and records applied versions here.
ALTER TABLE pilot.schema_migrations OWNER TO laya_app;

-- Defense in depth: enable RLS on every pilot table, with no anon/authenticated
-- policies. laya_app owns the clinical tables and is not blocked without FORCE.
DO $$
DECLARE t record;
BEGIN
  FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'pilot' LOOP
    EXECUTE format('ALTER TABLE pilot.%I ENABLE ROW LEVEL SECURITY', t.tablename);
  END LOOP;
END $$;
