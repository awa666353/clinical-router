-- W5: pilot identity/role mapping keyed by Supabase auth.users.id (JWT sub).
-- Business roles live here, never in user_metadata or the JWT role claim (B2).
-- Run as laya_app with search_path=pilot, after pilot_schema.sql.

CREATE TABLE IF NOT EXISTS pilot.app_users (
    auth_user_id UUID PRIMARY KEY,
    pilot_user_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('doctor','interviewer','adjudicator','curator','admin')),
    site_id TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Named lookup index for pilot_user_id (the UNIQUE constraint also indexes it).
CREATE INDEX IF NOT EXISTS idx_app_users_pilot_user_id ON pilot.app_users(pilot_user_id);
