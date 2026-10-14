-- ============================================================================
-- OBSOLETE - DO NOT USE FOR THE SUPABASE DEPLOYMENT.
-- ============================================================================
-- This file is kept only as historical reference. It is NOT the schema source of
-- truth; see supabase/migrations/ and supabase/README.md.
--
-- Why it is wrong on Supabase (B2):
--   * Its policies gate access with auth.jwt() ->> 'role'. On Supabase that JWT
--     role claim is always 'authenticated' for signed-in end users, so it can
--     never carry the business role (doctor/interviewer/adjudicator/...).
--   * The examples also omitted training_exports, leaving that table uncovered.
--   * The modern approach is: clinical tables live in the pilot schema, are not
--     exposed through the Data API, have RLS enabled with no anon/authenticated
--     policies, and are reached only by the backend's laya_app role.
--
-- The DDL below is the original public-schema draft, unqualified and unmaintained.
-- ============================================================================

-- Optional Supabase/Postgres RLS example.
-- The FastAPI backend should use a server-only database role with BYPASSRLS
-- (or service_role) for writes. Browser clients should only receive scoped JWTs.

ALTER TABLE cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE encounters ENABLE ROW LEVEL SECURITY;
ALTER TABLE turn_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE route_state_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE question_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_assertion_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_state_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE fact_current_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempt_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE randomization_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE assignment_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE attempt_failure_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE adjudication_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE adjudication_consensus ENABLE ROW LEVEL SECURITY;
ALTER TABLE split_manifest ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_ground_truth ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_question_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE question_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE question_status_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE patient_actors ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_rounds ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE doctor_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE encounter_assignments ENABLE ROW LEVEL SECURITY;

-- Example claim shape:
--   {"site_id": "site-a", "role": "doctor", "sub": "doctor-001"}
--
-- Replace these policies with your real site/project membership model before
-- allowing any browser-side database access.

DROP POLICY IF EXISTS site_read_cases ON cases;
CREATE POLICY site_read_cases ON cases
    FOR SELECT USING (
        site_id = (auth.jwt() ->> 'site_id')
        OR (auth.jwt() ->> 'role') IN ('curator', 'admin')
    );

DROP POLICY IF EXISTS site_read_encounters ON encounters;
CREATE POLICY site_read_encounters ON encounters
    FOR SELECT USING (
        site_id = (auth.jwt() ->> 'site_id')
        OR (auth.jwt() ->> 'role') IN ('curator', 'admin')
    );

-- For pilot simplicity, all clinical writes go through backend API. Therefore
-- there are intentionally no browser INSERT/UPDATE policies on event tables.
