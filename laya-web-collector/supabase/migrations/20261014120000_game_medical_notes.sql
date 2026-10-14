-- Each game round keeps its AI-produced note separately from private scenario data.
ALTER TABLE pilot.game_score_events ADD COLUMN IF NOT EXISTS note_text TEXT;
ALTER TABLE pilot.game_score_events ADD COLUMN IF NOT EXISTS note_llm_call_id TEXT REFERENCES pilot.llm_calls(call_id);
ALTER TABLE pilot.game_score_events ADD COLUMN IF NOT EXISTS note_status TEXT NOT NULL DEFAULT 'queued';
ALTER TABLE pilot.game_score_events ADD COLUMN IF NOT EXISTS note_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pilot.game_score_events ADD COLUMN IF NOT EXISTS note_started_at TIMESTAMPTZ;

ALTER TABLE pilot.llm_calls DROP CONSTRAINT IF EXISTS llm_calls_purpose_check;
ALTER TABLE pilot.llm_calls ADD CONSTRAINT llm_calls_purpose_check
    CHECK (purpose IN (
        'question_generation', 'extraction', 'verification',
        'patient_simulation', 'doctor_simulation', 'game_question_route', 'game_scoring', 'game_note'
    ));
