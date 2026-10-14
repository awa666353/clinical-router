-- Durable scoring records for local synthetic role-play sessions.
-- The API exposes this mode only when APP_ENV=development and AUTH_MODE=disabled.
CREATE TABLE IF NOT EXISTS pilot.game_score_events (
    score_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    attempt_id TEXT NOT NULL REFERENCES pilot.fact_attempts(attempt_id),
    answer_turn_id TEXT NOT NULL UNIQUE REFERENCES pilot.turn_events(turn_id),
    status TEXT NOT NULL DEFAULT 'queued',
    matched_fact_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    llm_call_id TEXT REFERENCES pilot.llm_calls(call_id),
    note_text TEXT,
    note_llm_call_id TEXT REFERENCES pilot.llm_calls(call_id),
    note_status TEXT NOT NULL DEFAULT 'queued',
    note_attempts INTEGER NOT NULL DEFAULT 0,
    note_started_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_game_score_events_encounter ON pilot.game_score_events(encounter_id);
ALTER TABLE pilot.game_score_events ENABLE ROW LEVEL SECURITY;

-- These purpose values are stored by the role-play question, patient and score calls.
ALTER TABLE pilot.llm_calls DROP CONSTRAINT IF EXISTS llm_calls_purpose_check;
ALTER TABLE pilot.llm_calls ADD CONSTRAINT llm_calls_purpose_check
    CHECK (purpose IN (
        'question_generation', 'extraction', 'verification',
        'patient_simulation', 'doctor_simulation', 'game_question_route', 'game_scoring', 'game_note'
    ));
