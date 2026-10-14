"""SQLAlchemy models mirroring the portable Postgres schema in schema.sql."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB, "postgresql")


def new_id() -> str:
    return uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Case(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    case_code: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, default="synthetic")
    topic: Mapped[str] = mapped_column(String(80), nullable=False, default="chest_pain")
    template_id: Mapped[str | None] = mapped_column(String(120))
    source_case_id: Mapped[str | None] = mapped_column(String(120))
    split_role: Mapped[str] = mapped_column(String(32), nullable=False, default="train")
    shift_scope: Mapped[str | None] = mapped_column(String(80))
    patient_group_id: Mapped[str | None] = mapped_column(String(120), index=True)
    collection_time_bucket: Mapped[str | None] = mapped_column(String(40), index=True)
    site_id: Mapped[str | None] = mapped_column(String(80))
    success_context_json: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    encounters: Mapped[list["Encounter"]] = relationship(back_populates="case")

    __table_args__ = (
        CheckConstraint("split_role in ('train','calib','valid','test','heldout_shift')", name="ck_cases_split_role"),
    )


class Encounter(Base):
    __tablename__ = "encounters"

    encounter_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), nullable=False, index=True)
    interviewer_id: Mapped[str | None] = mapped_column(String(80))
    patient_actor_id: Mapped[str | None] = mapped_column(String(80))
    site_id: Mapped[str | None] = mapped_column(String(80))
    protocol_version: Mapped[str] = mapped_column(String(80), nullable=False, default="real-pilot-v2")
    language: Mapped[str] = mapped_column(String(40), nullable=False, default="zh-CN")
    device: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # schema.sql/migrations declare BIGINT; a 63-bit seed overflows INTEGER.
    randomization_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    randomization_seed_source: Mapped[str] = mapped_column(String(20), nullable=False, default="generated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[Case] = relationship(back_populates="encounters")
    turns: Mapped[list["TurnEvent"]] = relationship(back_populates="encounter", order_by="TurnEvent.turn_no")
    attempts: Mapped[list["FactAttempt"]] = relationship(back_populates="encounter")


class TurnEvent(Base):
    __tablename__ = "turn_events"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    input_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="typed")
    audio_object_key: Mapped[str | None] = mapped_column(String(512))
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    client_event_id: Mapped[str | None] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    server_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    encounter: Mapped[Encounter] = relationship(back_populates="turns")

    __table_args__ = (
        UniqueConstraint("encounter_id", "turn_no", name="uq_turn_encounter_turn_no"),
        CheckConstraint("speaker in ('interviewer','patient','system')", name="ck_turn_speaker"),
    )


class RouteStateSnapshot(Base):
    __tablename__ = "route_state_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    before_turn_id: Mapped[str | None] = mapped_column(ForeignKey("turn_events.turn_id"))
    target_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(200), nullable=False)
    state_json: Mapped[dict] = mapped_column(JSONType, nullable=False)
    state_schema_version: Mapped[str] = mapped_column(String(60), nullable=False, default="route-state-v2")
    state_serialization_version: Mapped[str] = mapped_column(String(60), nullable=False, default="canonical-json-v1")
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactAttempt(Base):
    __tablename__ = "fact_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    target_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(200), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_channel: Mapped[str] = mapped_column(String(24), nullable=False)
    executed_channel: Mapped[str] = mapped_column(String(24), nullable=False)
    interaction_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="async")
    assignment_method: Mapped[str] = mapped_column(String(32), nullable=False, default="randomized")
    propensity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0 / 3.0)
    block_id: Mapped[str | None] = mapped_column(String(128))
    assignment_schedule_id: Mapped[str | None] = mapped_column(ForeignKey("assignment_schedule.schedule_id"))
    execution_propensity: Mapped[float | None] = mapped_column(Float)
    override_reason: Mapped[str | None] = mapped_column(Text)
    state_snapshot_id: Mapped[str] = mapped_column(ForeignKey("route_state_snapshots.snapshot_id"), nullable=False)
    start_turn_id: Mapped[str | None] = mapped_column(ForeignKey("turn_events.turn_id"))
    end_turn_id: Mapped[str | None] = mapped_column(ForeignKey("turn_events.turn_id"))
    target_declared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    target_declared_source: Mapped[str | None] = mapped_column(String(20))
    channel_revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_retry: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    previous_attempt_id: Mapped[str | None] = mapped_column(String(64))
    selection_reason: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    resource_units_json: Mapped[dict | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    encounter: Mapped[Encounter] = relationship(back_populates="attempts")

    __table_args__ = (
        UniqueConstraint("encounter_id", "target_fact_id", "attempt_no", name="uq_attempt_target_no"),
        CheckConstraint("assigned_channel in ('Structured','LLM','Human')", name="ck_attempt_assigned_channel"),
        CheckConstraint("executed_channel in ('Structured','LLM','Human')", name="ck_attempt_executed_channel"),
        CheckConstraint(
            "interaction_mode in ('async','sync','semi_async_human')",
            name="ck_attempt_interaction_mode",
        ),
    )


class QuestionEvent(Base):
    __tablename__ = "question_events"

    question_event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, index=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    question_source: Mapped[str] = mapped_column(String(40), nullable=False)
    question_variant_id: Mapped[str | None] = mapped_column(String(120))
    generated_by_call_id: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    previous_question_event_id: Mapped[str | None] = mapped_column(ForeignKey("question_events.question_event_id"))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("question_source in ('structured_template','llm','human')", name="ck_question_source"),
        CheckConstraint(
            "status in ('planned','generated','approved','queued','delivered','viewed','answered','cancelled','expired')",
            name="ck_question_status",
        ),
    )


class PatientQuestionLink(Base):
    __tablename__ = "patient_question_links"

    link_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("fact_attempts.attempt_id"), index=True)
    question_event_id: Mapped[str | None] = mapped_column(ForeignKey("question_events.question_event_id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    use_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QuestionJob(Base):
    __tablename__ = "question_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, index=True)
    question_event_id: Mapped[str | None] = mapped_column(ForeignKey("question_events.question_event_id"))
    job_type: Mapped[str] = mapped_column(String(40), nullable=False, default="generate_llm_question")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "job_type in ('generate_llm_question','generate_structured_question','extract_answer','expire_question')",
            name="ck_question_job_type",
        ),
        CheckConstraint(
            "status in ('queued','running','succeeded','failed','cancelled')",
            name="ck_question_job_status",
        ),
    )


class QuestionStatusEvent(Base):
    __tablename__ = "question_status_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    question_event_id: Mapped[str] = mapped_column(ForeignKey("question_events.question_event_id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(80))
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str | None] = mapped_column(ForeignKey("encounters.encounter_id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("fact_attempts.attempt_id"))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False, default="dashscope")
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_encrypted: Mapped[str | None] = mapped_column(Text)
    response_encrypted: Mapped[str | None] = mapped_column(Text)
    token_in: Mapped[int | None] = mapped_column(Integer)
    token_out: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    safety_filter_result: Mapped[str | None] = mapped_column(String(200))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    temperature: Mapped[float | None] = mapped_column(Float)
    top_p: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieval_strategy: Mapped[str | None] = mapped_column(String(120))
    retrieved_doc_ids_json: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GameScoreEvent(Base):
    """Durable, per-answer scoring for local synthetic role-play games."""

    __tablename__ = "game_score_events"

    score_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False)
    answer_turn_id: Mapped[str] = mapped_column(ForeignKey("turn_events.turn_id"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    matched_fact_ids_json: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.call_id"))
    note_text: Mapped[str | None] = mapped_column(Text)
    note_llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.call_id"))
    note_status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    note_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FactAssertionCandidate(Base):
    __tablename__ = "fact_assertion_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    llm_call_id: Mapped[str | None] = mapped_column(ForeignKey("llm_calls.call_id"))
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turn_events.turn_id"))
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("fact_attempts.attempt_id"), index=True)
    clinical_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(200), nullable=False)
    assertion_status: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[object | None] = mapped_column(JSONType)
    value_text: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(String(80))
    polarity: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(String(40))
    temporality: Mapped[str | None] = mapped_column(String(40))
    certainty: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_quote: Mapped[str | None] = mapped_column(Text)
    evidence_start: Mapped[int | None] = mapped_column(Integer)
    evidence_end: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    validation_errors_json: Mapped[list | None] = mapped_column(JSONType)
    validator_version: Mapped[str] = mapped_column(String(80), nullable=False, default="fact-validator-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactStateEvent(Base):
    __tablename__ = "fact_state_events"

    state_event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    clinical_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    before_status: Mapped[str] = mapped_column(String(32), nullable=False)
    before_value_json: Mapped[object | None] = mapped_column(JSONType)
    after_status: Mapped[str] = mapped_column(String(32), nullable=False)
    after_value_json: Mapped[object | None] = mapped_column(JSONType)
    source_candidate_ids: Mapped[list | None] = mapped_column(JSONType)
    projector_version: Mapped[str] = mapped_column(String(80), nullable=False, default="state-projector-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FactCurrentState(Base):
    __tablename__ = "fact_current_state"

    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), primary_key=True)
    clinical_fact_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    value_json: Mapped[object | None] = mapped_column(JSONType)
    value_text: Mapped[str | None] = mapped_column(Text)
    polarity: Mapped[str | None] = mapped_column(String(32))
    last_channel: Mapped[str | None] = mapped_column(String(24))
    last_attempt_turn: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AttemptOutcome(Base):
    __tablename__ = "attempt_outcomes"

    outcome_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, index=True)
    rater_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rater_role: Mapped[str] = mapped_column(String(40), nullable=False, default="adjudicator")
    adjudication_round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    blinded_to_channel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blinded_to_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    acquired_label: Mapped[str] = mapped_column(String(16), nullable=False)
    recorded_label: Mapped[str] = mapped_column(String(16), nullable=False)
    usable_label: Mapped[str] = mapped_column(String(16), nullable=False)
    actionable_label: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80))
    reason_text: Mapped[str | None] = mapped_column(Text)
    label_codebook_version: Mapped[str] = mapped_column(String(80), nullable=False, default="real-pilot-label-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("acquired_label in ('yes','no','uncertain','not_applicable','unable_to_assess')", name="ck_outcome_acquired"),
        CheckConstraint("recorded_label in ('yes','no','uncertain','not_applicable','unable_to_assess')", name="ck_outcome_recorded"),
        CheckConstraint("usable_label in ('yes','no','uncertain','not_applicable','unable_to_assess')", name="ck_outcome_usable"),
        CheckConstraint("actionable_label in ('yes','no','uncertain','not_applicable','unable_to_assess')", name="ck_outcome_actionable"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(String(80))
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    object_id: Mapped[str] = mapped_column(String(120), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    detail_json: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class RandomizationBlock(Base):
    __tablename__ = "randomization_blocks"

    block_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    topic: Mapped[str] = mapped_column(String(80), nullable=False)
    target_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    schedule_version: Mapped[str] = mapped_column(String(80), nullable=False, default="target-fact-blocked-v1")
    target_per_arm: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False, default="stratified_simple")
    salt: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("topic", "target_fact_id", "schedule_version", name="uq_randomization_block"),
    )


class AssignmentSchedule(Base):
    __tablename__ = "assignment_schedule"

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    block_id: Mapped[str] = mapped_column(ForeignKey("randomization_blocks.block_id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), nullable=False, index=True)
    target_fact_id: Mapped[str] = mapped_column(String(120), nullable=False)
    assigned_channel: Mapped[str] = mapped_column(String(24), nullable=False)
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy: Mapped[str] = mapped_column(String(40), nullable=False, default="stratified_simple")
    block_sequence_index: Mapped[int | None] = mapped_column(Integer)
    block_position: Mapped[int | None] = mapped_column(Integer)
    segment_length: Mapped[int | None] = mapped_column(Integer)
    propensity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0 / 3.0)
    schedule_version: Mapped[str] = mapped_column(String(80), nullable=False, default="target-fact-blocked-v1")
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_attempt_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("block_id", "sequence_index", name="uq_assignment_block_sequence"),
        CheckConstraint("assigned_channel in ('Structured','LLM','Human')", name="ck_assignment_channel"),
    )


class AttemptFailureEvent(Base):
    __tablename__ = "attempt_failure_events"

    failure_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, index=True)
    failure_stage: Mapped[str] = mapped_column(String(40), nullable=False)
    failure_code: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    recoverable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    actor_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "failure_stage in ('acquisition','recording','extraction','system')",
            name="ck_failure_stage",
        ),
    )


class AdjudicationAssignment(Base):
    __tablename__ = "adjudication_assignments"

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, index=True)
    rater_id: Mapped[str] = mapped_column(String(80), nullable=False)
    rater_role: Mapped[str] = mapped_column(String(40), nullable=False, default="adjudicator")
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blinded_to_channel: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    blinded_to_ai: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="assigned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("attempt_id", "rater_id", name="uq_adjudication_attempt_rater"),
    )


class AdjudicationConsensus(Base):
    __tablename__ = "adjudication_consensus"

    consensus_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("fact_attempts.attempt_id"), nullable=False, unique=True)
    acquired_label: Mapped[str | None] = mapped_column(String(16))
    recorded_label: Mapped[str | None] = mapped_column(String(16))
    usable_label: Mapped[str | None] = mapped_column(String(16))
    actionable_label: Mapped[str | None] = mapped_column(String(16))
    agreement_rate: Mapped[float | None] = mapped_column(Float)
    disagreement_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    final_rater_id: Mapped[str | None] = mapped_column(String(80))
    label_codebook_version: Mapped[str] = mapped_column(String(80), nullable=False, default="real-pilot-label-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseGroundTruth(Base):
    __tablename__ = "case_ground_truth"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), primary_key=True)
    clinical_fact_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    reference_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    reference_value_json: Mapped[object | None] = mapped_column(JSONType)
    encryption_key_ref: Mapped[str | None] = mapped_column(String(200))


class SplitManifest(Base):
    __tablename__ = "split_manifest"

    manifest_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    export_id: Mapped[str | None] = mapped_column(String(64), index=True)
    split_role: Mapped[str] = mapped_column(String(32), nullable=False)
    group_key_type: Mapped[str] = mapped_column(String(40), nullable=False)
    group_key: Mapped[str] = mapped_column(String(200), nullable=False)
    site_id: Mapped[str | None] = mapped_column(String(80))
    time_bucket: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PatientActor(Base):
    __tablename__ = "patient_actors"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(40), nullable=False, default="standardized_patient")
    max_parallel_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    metadata_json: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "actor_type in ('standardized_patient','human_simulator','llm_simulator')",
            name="ck_patient_actor_type",
        ),
    )


class CaseBatch(Base):
    __tablename__ = "case_batches"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    target_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_json: Mapped[dict | None] = mapped_column(JSONType)
    created_by: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseRun(Base):
    __tablename__ = "case_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("case_batches.batch_id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), nullable=False, index=True)
    encounter_id: Mapped[str | None] = mapped_column(ForeignKey("encounters.encounter_id"))
    patient_actor_id: Mapped[str | None] = mapped_column(ForeignKey("patient_actors.actor_id"), index=True)
    interviewer_id: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    current_round: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pause_reason: Mapped[str | None] = mapped_column(String(80))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_task_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "case_id", name="uq_case_run_batch_case"),
        CheckConstraint(
            "status in ('created','running','waiting_patient','waiting_doctor','paused','ready_to_resume','completed','failed')",
            name="ck_case_run_status",
        ),
    )


class DoctorRound(Base):
    __tablename__ = "doctor_rounds"

    round_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(ForeignKey("case_batches.batch_id"), nullable=False, index=True)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("batch_id", "round_no", name="uq_doctor_round_batch_no"),
    )


class DoctorTask(Base):
    __tablename__ = "doctor_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    round_id: Mapped[str] = mapped_column(ForeignKey("doctor_rounds.round_id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.run_id"), nullable=False, index=True)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("fact_attempts.attempt_id"))
    question_event_id: Mapped[str | None] = mapped_column(ForeignKey("question_events.question_event_id"))
    state_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("route_state_snapshots.snapshot_id"))
    task_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSONType)
    response_json: Mapped[dict | None] = mapped_column(JSONType)
    assigned_to: Mapped[str | None] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "task_type in ('doctor_question','doctor_record','doctor_adjudication')",
            name="ck_doctor_task_type",
        ),
        CheckConstraint(
            "status in ('open','exported','assigned','completed','imported','cancelled')",
            name="ck_doctor_task_status",
        ),
    )


class DoctorDocument(Base):
    __tablename__ = "doctor_documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    round_id: Mapped[str] = mapped_column(ForeignKey("doctor_rounds.round_id"), nullable=False, index=True)
    format: Mapped[str] = mapped_column(String(20), nullable=False, default="csv")
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generated")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.run_id"), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(120))
    actor_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EncounterAssignment(Base):
    __tablename__ = "encounter_assignments"

    assignment_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    encounter_id: Mapped[str] = mapped_column(ForeignKey("encounters.encounter_id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False, default="interviewer")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("encounter_id", "user_id", "role", name="uq_encounter_assignment"),)


class TrainingExport(Base):
    __tablename__ = "training_exports"

    export_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    export_dir: Mapped[str] = mapped_column(String(512), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_json: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppUser(Base):
    """Pilot identity/role mapping keyed by ``auth.users.id`` (JWT ``sub``).

    Business roles must NOT live in Supabase ``user_metadata`` (users can edit
    it) nor in the JWT ``role`` claim (always ``authenticated`` on Supabase), so
    the backend joins the verified ``sub`` against this table (W5/B2).
    """

    __tablename__ = "app_users"

    auth_user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True)
    pilot_user_id: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    site_id: Mapped[str | None] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "role in ('doctor','interviewer','adjudicator','curator','admin')",
            name="ck_app_users_role",
        ),
    )
