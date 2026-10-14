-- Laya real-pilot web collector schema: pilot schema migration.
-- Converted from the legacy public-schema schema.sql; this file is now part of the
-- single source of truth under supabase/migrations/.
-- Run as role laya_app (search_path=pilot) after 20260930120200_security.sql created
-- the role and schema. All tables must be owned by laya_app so RLS does not block the
-- backend owner (see W3). pgcrypto is intentionally not created here (B11).

-- Laya real-pilot web collector schema
-- Target: PostgreSQL 15+ / Supabase-compatible Postgres.
-- The application writes through the backend service role. Do not let the
-- browser write clinical tables directly.


CREATE TABLE IF NOT EXISTS pilot.cases (
    case_id TEXT PRIMARY KEY,
    case_code TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL DEFAULT 'synthetic'
        CHECK (source_type IN ('synthetic', 'standardized_patient', 'real')),
    topic TEXT NOT NULL DEFAULT 'chest_pain',
    template_id TEXT,
    source_case_id TEXT,
    split_role TEXT NOT NULL DEFAULT 'train'
        CHECK (split_role IN ('train', 'calib', 'valid', 'test', 'heldout_shift')),
    shift_scope TEXT,
    patient_group_id TEXT,
    collection_time_bucket TEXT,
    site_id TEXT,
    success_context_json JSONB,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.encounters (
    encounter_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES pilot.cases(case_id),
    interviewer_id TEXT,
    patient_actor_id TEXT,
    site_id TEXT,
    protocol_version TEXT NOT NULL DEFAULT 'real-pilot-v2',
    language TEXT NOT NULL DEFAULT 'zh-CN',
    device TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    randomization_seed BIGINT NOT NULL,
    randomization_seed_source TEXT NOT NULL DEFAULT 'generated',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pilot.turn_events (
    turn_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    turn_no INTEGER NOT NULL,
    speaker TEXT NOT NULL CHECK (speaker IN ('interviewer', 'patient', 'system')),
    raw_text TEXT NOT NULL,
    input_mode TEXT NOT NULL DEFAULT 'typed',
    audio_object_key TEXT,
    asr_confidence DOUBLE PRECISION,
    client_event_id TEXT,
    content_sha256 TEXT NOT NULL,
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (encounter_id, turn_no)
);

CREATE TABLE IF NOT EXISTS pilot.route_state_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    before_turn_id TEXT REFERENCES pilot.turn_events(turn_id),
    target_fact_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    state_json JSONB NOT NULL,
    state_schema_version TEXT NOT NULL DEFAULT 'route-state-v2',
    state_serialization_version TEXT NOT NULL DEFAULT 'canonical-json-v1',
    state_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.fact_attempts (
    attempt_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    target_fact_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    assigned_channel TEXT NOT NULL CHECK (assigned_channel IN ('Structured', 'LLM', 'Human')),
    executed_channel TEXT NOT NULL CHECK (executed_channel IN ('Structured', 'LLM', 'Human')),
    interaction_mode TEXT NOT NULL DEFAULT 'async'
        CHECK (interaction_mode IN ('async', 'sync', 'semi_async_human')),
    assignment_method TEXT NOT NULL DEFAULT 'randomized',
    propensity DOUBLE PRECISION NOT NULL DEFAULT 0.3333333333333333,
    block_id TEXT,
    assignment_schedule_id TEXT,
    execution_propensity DOUBLE PRECISION,
    override_reason TEXT,
    state_snapshot_id TEXT NOT NULL REFERENCES pilot.route_state_snapshots(snapshot_id),
    start_turn_id TEXT REFERENCES pilot.turn_events(turn_id),
    end_turn_id TEXT REFERENCES pilot.turn_events(turn_id),
    target_declared_at TIMESTAMPTZ,
    target_declared_source TEXT,
    channel_revealed_at TIMESTAMPTZ,
    is_retry BOOLEAN NOT NULL DEFAULT FALSE,
    previous_attempt_id TEXT,
    selection_reason TEXT,
    duration_ms INTEGER,
    resource_units_json JSONB,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (encounter_id, target_fact_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS pilot.question_events (
    question_event_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES pilot.fact_attempts(attempt_id),
    question_text TEXT NOT NULL DEFAULT '',
    question_source TEXT NOT NULL CHECK (question_source IN ('structured_template', 'llm', 'human')),
    question_variant_id TEXT,
    generated_by_call_id TEXT,
    approved_by TEXT,
    status TEXT NOT NULL DEFAULT 'generated'
        CHECK (status IN ('planned', 'generated', 'approved', 'queued', 'delivered', 'viewed', 'answered', 'cancelled', 'expired')),
    version INTEGER NOT NULL DEFAULT 1,
    previous_question_event_id TEXT REFERENCES pilot.question_events(question_event_id),
    generated_at TIMESTAMPTZ DEFAULT now(),
    approved_at TIMESTAMPTZ,
    queued_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    viewed_at TIMESTAMPTZ,
    answered_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.patient_question_links (
    link_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    attempt_id TEXT REFERENCES pilot.fact_attempts(attempt_id),
    question_event_id TEXT REFERENCES pilot.question_events(question_event_id),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    first_viewed_at TIMESTAMPTZ,
    answered_at TIMESTAMPTZ,
    max_uses INTEGER NOT NULL DEFAULT 100,
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.question_jobs (
    job_id TEXT PRIMARY KEY,
    attempt_id TEXT REFERENCES pilot.fact_attempts(attempt_id),
    question_event_id TEXT REFERENCES pilot.question_events(question_event_id),
    job_type TEXT NOT NULL DEFAULT 'generate_llm_question'
        CHECK (job_type IN ('generate_llm_question', 'generate_structured_question', 'extract_answer', 'expire_question')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    idempotency_key TEXT NOT NULL UNIQUE,
    scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.question_status_events (
    event_id TEXT PRIMARY KEY,
    question_event_id TEXT NOT NULL REFERENCES pilot.question_events(question_event_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    actor_id TEXT,
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.llm_calls (
    call_id TEXT PRIMARY KEY,
    encounter_id TEXT REFERENCES pilot.encounters(encounter_id),
    attempt_id TEXT REFERENCES pilot.fact_attempts(attempt_id),
    purpose TEXT NOT NULL CHECK (purpose IN ('question_generation', 'extraction', 'verification')),
    provider TEXT NOT NULL DEFAULT 'dashscope',
    model TEXT NOT NULL,
    model_version TEXT,
    prompt_version TEXT NOT NULL,
    prompt_hash TEXT,
    request_hash TEXT NOT NULL,
    request_encrypted TEXT,
    response_encrypted TEXT,
    token_in INTEGER,
    token_out INTEGER,
    latency_ms INTEGER,
    retry_count INTEGER NOT NULL DEFAULT 0,
    safety_filter_result TEXT,
    provider_request_id TEXT,
    temperature DOUBLE PRECISION,
    top_p DOUBLE PRECISION,
    max_tokens INTEGER,
    retrieval_strategy TEXT,
    retrieved_doc_ids_json JSONB,
    status TEXT NOT NULL DEFAULT 'ok',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.fact_assertion_candidates (
    candidate_id TEXT PRIMARY KEY,
    llm_call_id TEXT REFERENCES pilot.llm_calls(call_id),
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    turn_id TEXT REFERENCES pilot.turn_events(turn_id),
    attempt_id TEXT REFERENCES pilot.fact_attempts(attempt_id),
    clinical_fact_id TEXT NOT NULL,
    slot_key TEXT NOT NULL,
    assertion_status TEXT NOT NULL
        CHECK (assertion_status IN ('positive', 'negative', 'unknown', 'ambiguous', 'not_applicable')),
    value_json JSONB,
    value_text TEXT,
    unit TEXT,
    polarity TEXT,
    subject TEXT,
    temporality TEXT,
    certainty TEXT,
    confidence DOUBLE PRECISION,
    evidence_quote TEXT,
    evidence_start INTEGER,
    evidence_end INTEGER,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_errors_json JSONB,
    validator_version TEXT NOT NULL DEFAULT 'fact-validator-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.fact_state_events (
    state_event_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    clinical_fact_id TEXT NOT NULL,
    before_status TEXT NOT NULL,
    before_value_json JSONB,
    after_status TEXT NOT NULL,
    after_value_json JSONB,
    source_candidate_ids JSONB,
    projector_version TEXT NOT NULL DEFAULT 'state-projector-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.fact_current_state (
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    clinical_fact_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    value_json JSONB,
    value_text TEXT,
    polarity TEXT,
    last_channel TEXT,
    last_attempt_turn INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (encounter_id, clinical_fact_id)
);

CREATE TABLE IF NOT EXISTS pilot.attempt_outcomes (
    outcome_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES pilot.fact_attempts(attempt_id),
    rater_id TEXT NOT NULL,
    rater_role TEXT NOT NULL DEFAULT 'adjudicator',
    adjudication_round INTEGER NOT NULL DEFAULT 1,
    blinded_to_channel BOOLEAN NOT NULL DEFAULT TRUE,
    blinded_to_ai BOOLEAN NOT NULL DEFAULT TRUE,
    acquired_label TEXT NOT NULL CHECK (acquired_label IN ('yes', 'no', 'uncertain', 'not_applicable', 'unable_to_assess')),
    recorded_label TEXT NOT NULL CHECK (recorded_label IN ('yes', 'no', 'uncertain', 'not_applicable', 'unable_to_assess')),
    usable_label TEXT NOT NULL CHECK (usable_label IN ('yes', 'no', 'uncertain', 'not_applicable', 'unable_to_assess')),
    actionable_label TEXT NOT NULL CHECK (actionable_label IN ('yes', 'no', 'uncertain', 'not_applicable', 'unable_to_assess')),
    reason_code TEXT,
    reason_text TEXT,
    label_codebook_version TEXT NOT NULL DEFAULT 'real-pilot-label-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, rater_id)
);

CREATE TABLE IF NOT EXISTS pilot.randomization_blocks (
    block_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    target_fact_id TEXT NOT NULL,
    schedule_version TEXT NOT NULL DEFAULT 'target-fact-blocked-v1',
    target_per_arm INTEGER NOT NULL DEFAULT 30,
    strategy TEXT NOT NULL DEFAULT 'stratified_simple',
    salt TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (topic, target_fact_id, schedule_version)
);

CREATE TABLE IF NOT EXISTS pilot.assignment_schedule (
    schedule_id TEXT PRIMARY KEY,
    block_id TEXT NOT NULL REFERENCES pilot.randomization_blocks(block_id),
    case_id TEXT NOT NULL REFERENCES pilot.cases(case_id),
    target_fact_id TEXT NOT NULL,
    assigned_channel TEXT NOT NULL CHECK (assigned_channel IN ('Structured', 'LLM', 'Human')),
    sequence_index INTEGER NOT NULL,
    strategy TEXT NOT NULL DEFAULT 'stratified_simple',
    block_sequence_index INTEGER,
    block_position INTEGER,
    segment_length INTEGER,
    propensity DOUBLE PRECISION NOT NULL DEFAULT 0.3333333333333333,
    schedule_version TEXT NOT NULL DEFAULT 'target-fact-blocked-v1',
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ,
    used_by_attempt_id TEXT,
    UNIQUE (block_id, sequence_index)
);

CREATE TABLE IF NOT EXISTS pilot.attempt_failure_events (
    failure_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES pilot.fact_attempts(attempt_id),
    failure_stage TEXT NOT NULL CHECK (failure_stage IN ('acquisition', 'recording', 'extraction', 'system')),
    failure_code TEXT NOT NULL,
    message TEXT,
    recoverable BOOLEAN NOT NULL DEFAULT TRUE,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.adjudication_assignments (
    assignment_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES pilot.fact_attempts(attempt_id),
    rater_id TEXT NOT NULL,
    rater_role TEXT NOT NULL DEFAULT 'adjudicator',
    required BOOLEAN NOT NULL DEFAULT TRUE,
    blinded_to_channel BOOLEAN NOT NULL DEFAULT TRUE,
    blinded_to_ai BOOLEAN NOT NULL DEFAULT TRUE,
    status TEXT NOT NULL DEFAULT 'assigned',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (attempt_id, rater_id)
);

CREATE TABLE IF NOT EXISTS pilot.adjudication_consensus (
    consensus_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE REFERENCES pilot.fact_attempts(attempt_id),
    acquired_label TEXT,
    recorded_label TEXT,
    usable_label TEXT,
    actionable_label TEXT,
    agreement_rate DOUBLE PRECISION,
    disagreement_flag BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'pending',
    final_rater_id TEXT,
    label_codebook_version TEXT NOT NULL DEFAULT 'real-pilot-label-v1',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finalized_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pilot.split_manifest (
    manifest_id TEXT PRIMARY KEY,
    export_id TEXT,
    split_role TEXT NOT NULL,
    group_key_type TEXT NOT NULL,
    group_key TEXT NOT NULL,
    site_id TEXT,
    time_bucket TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.patient_actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    actor_type TEXT NOT NULL DEFAULT 'standardized_patient'
        CHECK (actor_type IN ('standardized_patient', 'human_simulator', 'llm_simulator')),
    max_parallel_runs INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.case_batches (
    batch_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    target_size INTEGER NOT NULL DEFAULT 0,
    policy_json JSONB,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pilot.case_runs (
    run_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot.case_batches(batch_id),
    case_id TEXT NOT NULL REFERENCES pilot.cases(case_id),
    encounter_id TEXT REFERENCES pilot.encounters(encounter_id),
    patient_actor_id TEXT REFERENCES pilot.patient_actors(actor_id),
    interviewer_id TEXT,
    status TEXT NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'running', 'waiting_patient', 'waiting_doctor', 'paused', 'ready_to_resume', 'completed', 'failed')),
    current_round INTEGER NOT NULL DEFAULT 0,
    pause_reason TEXT,
    paused_at TIMESTAMPTZ,
    resumed_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    last_task_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, case_id)
);

CREATE TABLE IF NOT EXISTS pilot.doctor_rounds (
    round_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES pilot.case_batches(batch_id),
    round_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    generated_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ,
    imported_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, round_no)
);

CREATE TABLE IF NOT EXISTS pilot.doctor_tasks (
    task_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES pilot.doctor_rounds(round_id),
    run_id TEXT NOT NULL REFERENCES pilot.case_runs(run_id),
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    attempt_id TEXT REFERENCES pilot.fact_attempts(attempt_id),
    question_event_id TEXT REFERENCES pilot.question_events(question_event_id),
    state_snapshot_id TEXT REFERENCES pilot.route_state_snapshots(snapshot_id),
    task_type TEXT NOT NULL CHECK (task_type IN ('doctor_question', 'doctor_record', 'doctor_adjudication')),
    payload_json JSONB,
    response_json JSONB,
    assigned_to TEXT,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'exported', 'assigned', 'completed', 'imported', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pilot.doctor_documents (
    document_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES pilot.doctor_rounds(round_id),
    format TEXT NOT NULL DEFAULT 'csv',
    storage_key TEXT NOT NULL,
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'generated',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    imported_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pilot.run_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pilot.case_runs(run_id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    actor_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.encounter_assignments (
    assignment_id TEXT PRIMARY KEY,
    encounter_id TEXT NOT NULL REFERENCES pilot.encounters(encounter_id),
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'interviewer',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    UNIQUE (encounter_id, user_id, role)
);

CREATE TABLE IF NOT EXISTS pilot.audit_events (
    audit_id TEXT PRIMARY KEY,
    actor_id TEXT,
    event_type TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    before_hash TEXT,
    after_hash TEXT,
    detail_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pilot.training_exports (
    export_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    export_dir TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    manifest_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

-- Hidden ground truth is for standardized-patient/patient-simulator workflows
-- only. It must never be joined into a model-visible state snapshot or export.
CREATE TABLE IF NOT EXISTS pilot.case_ground_truth (
    case_id TEXT NOT NULL REFERENCES pilot.cases(case_id),
    clinical_fact_id TEXT NOT NULL,
    reference_status TEXT NOT NULL DEFAULT 'unknown',
    reference_value_json JSONB,
    encryption_key_ref TEXT,
    PRIMARY KEY (case_id, clinical_fact_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_events_encounter ON pilot.turn_events(encounter_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_attempts_encounter ON pilot.fact_attempts(encounter_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON pilot.fact_attempts(status);
CREATE INDEX IF NOT EXISTS idx_candidates_attempt ON pilot.fact_assertion_candidates(attempt_id);
CREATE INDEX IF NOT EXISTS idx_candidates_fact ON pilot.fact_assertion_candidates(clinical_fact_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_attempt ON pilot.attempt_outcomes(attempt_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_state_events_encounter ON pilot.fact_state_events(encounter_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_created ON pilot.audit_events(created_at);
CREATE INDEX IF NOT EXISTS idx_assignment_fact ON pilot.assignment_schedule(target_fact_id, assigned_channel);
CREATE INDEX IF NOT EXISTS idx_question_status ON pilot.question_events(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_patient_links_encounter ON pilot.patient_question_links(encounter_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_question_jobs_status ON pilot.question_jobs(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_case_runs_actor ON pilot.case_runs(patient_actor_id, status);
CREATE INDEX IF NOT EXISTS idx_case_runs_doctor ON pilot.case_runs(interviewer_id, status);
CREATE INDEX IF NOT EXISTS idx_doctor_tasks_round ON pilot.doctor_tasks(round_id, status);
CREATE INDEX IF NOT EXISTS idx_doctor_tasks_assignee ON pilot.doctor_tasks(assigned_to, status);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON pilot.run_events(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_encounter_assign_user ON pilot.encounter_assignments(user_id, status);
CREATE INDEX IF NOT EXISTS idx_assignment_case ON pilot.assignment_schedule(case_id, target_fact_id);
CREATE INDEX IF NOT EXISTS idx_failures_attempt ON pilot.attempt_failure_events(attempt_id, created_at);
CREATE INDEX IF NOT EXISTS idx_adjudication_attempt ON pilot.adjudication_assignments(attempt_id, status);
CREATE INDEX IF NOT EXISTS idx_consensus_status ON pilot.adjudication_consensus(status);

COMMENT ON TABLE pilot.turn_events IS 'Immutable raw interaction event log. Correct by appending a correction event, never by updating content.';
COMMENT ON TABLE pilot.route_state_snapshots IS 'Pre-attempt visible state. It must not contain target answers, future turns, hidden facts, or adjudication labels.';
COMMENT ON TABLE pilot.attempt_outcomes IS 'Independent clinical adjudication. AI may assist extraction but cannot author a reviewed outcome.';
COMMENT ON COLUMN pilot.assignment_schedule.propensity IS 'Conditional allocation probability at this block position under permuted-block randomization.';
COMMENT ON COLUMN pilot.fact_attempts.execution_propensity IS 'Probability of the executed channel; null after an unmodeled override.';
COMMENT ON TABLE pilot.case_ground_truth IS 'Restricted ground truth for patient actors/simulators. Never export into model-visible state.';
