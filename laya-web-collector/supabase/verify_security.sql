-- W3 acceptance checks (guide section 5). Run on Supabase as postgres.
-- The anon/authenticated roles exist on Supabase project databases; query 2 will
-- error on a plain local Postgres without those roles.

-- 1. Expected: tables_without_rls = 0.
--    Every relation in the pilot schema must have RLS enabled.
SELECT count(*) FILTER (WHERE NOT relrowsecurity) AS tables_without_rls
FROM pg_class
WHERE relnamespace = 'pilot'::regnamespace AND relkind = 'r';

-- 2. Expected: false, false.
--    anon and authenticated must not even have USAGE on pilot.
SELECT has_schema_privilege('anon', 'pilot', 'USAGE')          AS anon_has_usage,
       has_schema_privilege('authenticated', 'pilot', 'USAGE') AS authenticated_has_usage;

-- 3. Expected: 0.
--    No business tables may be created in public (Data API / PostgREST surface).
SELECT count(*) AS public_tables
FROM pg_tables
WHERE schemaname = 'public';

-- 4. Expected: no rows.
--    Lists any pilot table missing RLS; useful when a later migration adds a table
--    and security.sql has not been re-run yet.
SELECT c.relname AS table_without_rls
FROM pg_class c
WHERE c.relnamespace = 'pilot'::regnamespace
  AND c.relkind = 'r'
  AND NOT c.relrowsecurity
ORDER BY c.relname;
