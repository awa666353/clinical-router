# Supabase migrations and security runbook

`supabase/migrations/` is the single source of truth for the Postgres schema.
The old top-level `schema.sql` is only a pointer. Never create clinical tables
in `public` and never add DDL outside these files.

| File | Purpose |
|---|---|
| `migrations/20260930120000_pilot_schema.sql` | 31 clinical/ops tables in the `pilot` schema (converted from legacy `schema.sql`, no `pgcrypto`). |
| `migrations/20260930120100_app_users.sql` | W5 `pilot.app_users` identity/role mapping. |
| `migrations/20260930120200_security.sql` | Role `laya_app`, `pilot` schema privileges, default privileges, RLS enablement (W3). |

Run pass 1 and pass 3 as a superuser (`postgres`). Run pass 2 as `laya_app`
(the pass 1 `ALTER ROLE ... SET search_path = pilot` is already in effect).
`pilot.schema_migrations` is created by the security pass so the runner can
record applied versions.

On Supabase, `postgres` has CREATEROLE but is not a true superuser. After
creating `laya_app`, `security.sql` therefore runs
`GRANT laya_app TO postgres WITH SET TRUE` before `CREATE SCHEMA ...
AUTHORIZATION laya_app`; without that grant, pass 1 fails with
`must be able to SET ROLE "laya_app"`. The same file restores the role-level
`search_path = pilot` that pass 2 and the backend's production
`check_database_schema()` guard depend on.

## Pass-by-pass procedure (psql)

Set shell variables first; never commit passwords:

```bash
export MIGRATION_DATABASE_URL='postgresql://postgres:<postgres-password>@<host>:5432/postgres?sslmode=require'
export LAYA_APP_DATABASE_URL='postgresql://laya_app.<project_ref>:<laya-app-password>@<pooler-host>:5432/postgres?sslmode=require'
export LAYA_APP_PASSWORD='<laya-app-password>'   # same value as in LAYA_APP_DATABASE_URL
```

Pass 1 - as `postgres`, creates the role, `pilot` schema and version ledger:

```bash
cd research/clinical_router/real_pilot/web_collector/supabase
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v laya_app_password="$LAYA_APP_PASSWORD" \
  -f migrations/20260930120200_security.sql
```

Pass 2 - as `laya_app`; these objects must be owned by `laya_app`, otherwise
RLS blocks the backend owner. `pilot_schema.sql` first, then `app_users.sql`:

```bash
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930120000_pilot_schema.sql
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/20260930120100_app_users.sql
psql "$LAYA_APP_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO pilot.schema_migrations(version) VALUES ('20260930120000_pilot_schema'), ('20260930120100_app_users') ON CONFLICT (version) DO NOTHING;"
```

Pass 3 - as `postgres`; re-running `security.sql` now enables RLS on every
table created in pass 2 (the file is repeatable):

```bash
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -v laya_app_password="$LAYA_APP_PASSWORD" \
  -f migrations/20260930120200_security.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "INSERT INTO pilot.schema_migrations(version) VALUES ('20260930120200_security') ON CONFLICT (version) DO NOTHING;"
```

Finally run `psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f verify_security.sql`.

Dashboard SQL editor: psql variables are not supported. Create `laya_app`
manually once (for example via the SQL editor), then run the file with the
`ALTER ROLE laya_app ... :'laya_app_password';` line removed or replaced by a
literal placeholder password, as instructed in the file header.

## Supabase CLI status for this migration set

The current three-pass bootstrap is **not safe to apply with `supabase db push`**.
The security migration must run before and after the business-table migration,
but timestamp ordering puts it last; its psql password variable is also not a
standard CLI migration variable. In addition, this runbook writes
`pilot.schema_migrations`, while the CLI tracks
`supabase_migrations.schema_migrations`. Linking the project does not reconcile
these histories or validate object ownership.

`supabase link --project-ref <staging_project_ref>` may be used to scope local
CLI/MCP tooling to staging, but do not run `db push`, `db reset --linked`,
`db pull`, or `migration repair` on this project until a separate migration
refactor and baseline-adoption procedure has been reviewed. Use the three-pass
psql procedure above for initial setup. The complete operator checklist,
staging tests, AI/MCP read-only setup, and CLI boundaries are documented in
`../deploy/SUPABASE_DEPLOYMENT_AND_TESTING.md`.

## W3 acceptance queries

Expected values assume a Supabase project where `anon` and `authenticated`
exist. Full script: `verify_security.sql`.

```sql
-- 1. Expected: 0
SELECT count(*) FILTER (WHERE NOT relrowsecurity) AS tables_without_rls
FROM pg_class
WHERE relnamespace = 'pilot'::regnamespace AND relkind = 'r';

-- 2. Expected: false, false
SELECT has_schema_privilege('anon', 'pilot', 'USAGE'),
       has_schema_privilege('authenticated', 'pilot', 'USAGE');

-- 3. Expected: 0 (no business tables in public)
SELECT count(*) FROM pg_tables WHERE schemaname = 'public';

-- 4. Expected: no rows
SELECT c.relname AS table_without_rls
FROM pg_class c
WHERE c.relnamespace = 'pilot'::regnamespace
  AND c.relkind = 'r'
  AND NOT c.relrowsecurity
ORDER BY c.relname;
```

Also confirm Dashboard -> Advisors -> Security Advisor has no error-level
findings, and keep `pilot` out of Dashboard -> Data API -> Exposed schemas.
