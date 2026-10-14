"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


Label = Literal[
    "yes",
    "no",
    "uncertain",
    "not_applicable",
    "unable_to_assess",
]


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class GameSessionCreate(BaseModel):
    patient_mode: Literal["human", "ai"]
    doctor_mode: Literal["human", "ai"] = "human"


class GameReplyCreate(BaseModel):
    attempt_id: str


class GameQuestionCreate(BaseModel):
    question_text: str = Field(min_length=1, max_length=500)


class CaseCreate(BaseModel):
    case_code: str = Field(min_length=2, max_length=120)
    source_type: Literal["synthetic", "standardized_patient", "real"] = "standardized_patient"
    topic: str = "chest_pain"
    template_id: str | None = None
    source_case_id: str | None = None
    split_role: Literal["train", "calib", "valid", "test", "heldout_shift"] = "train"
    shift_scope: str | None = None
    patient_group_id: str | None = None
    collection_time_bucket: str | None = None
    site_id: str | None = None
    success_context: dict[str, Any] | None = None


class CaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    case_id: str
    case_code: str
    source_type: str
    topic: str
    template_id: str | None
    source_case_id: str | None
    split_role: str
    shift_scope: str | None
    patient_group_id: str | None
    collection_time_bucket: str | None
    site_id: str | None
    success_context_json: dict[str, Any] | None = None
    status: str
    created_at: datetime


class EncounterCreate(BaseModel):
    case_id: str
    interviewer_id: str | None = None
    patient_actor_id: str | None = None
    site_id: str | None = None
    protocol_version: str = "real-pilot-v2"
    language: str = "zh-CN"
    device: str | None = None
    randomization_seed: int | None = None


class EncounterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    encounter_id: str
    case_id: str
    interviewer_id: str | None
    patient_actor_id: str | None
    site_id: str | None
    protocol_version: str
    language: str
    device: str | None
    status: str
    created_at: datetime
    ended_at: datetime | None


class PatientActorCreate(BaseModel):
    display_name: str
    actor_type: Literal["standardized_patient", "human_simulator", "llm_simulator"] = "standardized_patient"
    max_parallel_runs: int = 1
    metadata_json: dict[str, Any] | None = None


class PatientActorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    actor_id: str
    display_name: str
    actor_type: str
    max_parallel_runs: int
    status: str
    created_at: datetime


class CaseBatchCreate(BaseModel):
    name: str
    case_ids: list[str] = Field(min_length=1)
    patient_actor_id: str | None = None
    interviewer_id: str | None = None
    policy_json: dict[str, Any] | None = None


class CaseBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    batch_id: str
    name: str
    status: str
    target_size: int
    policy_json: dict[str, Any] | None
    created_by: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CaseRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: str
    batch_id: str
    case_id: str
    encounter_id: str | None
    patient_actor_id: str | None
    interviewer_id: str | None
    status: str
    current_round: int
    pause_reason: str | None
    paused_at: datetime | None
    resumed_at: datetime | None
    created_at: datetime


class RunStatusUpdate(BaseModel):
    reason: str | None = None


class EncounterAssignmentCreate(BaseModel):
    encounter_id: str
    user_id: str
    role: str = "interviewer"


class EncounterAssignmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    assignment_id: str
    encounter_id: str
    user_id: str
    role: str
    status: str
    created_at: datetime


class DoctorRoundCreate(BaseModel):
    batch_id: str
    round_no: int | None = None
    due_at: datetime | None = None


class DoctorRoundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    round_id: str
    batch_id: str
    round_no: int
    status: str
    generated_at: datetime | None
    due_at: datetime | None
    imported_at: datetime | None
    created_at: datetime


class DoctorTaskCreate(BaseModel):
    round_id: str
    run_id: str
    encounter_id: str
    task_type: Literal["doctor_question", "doctor_record", "doctor_adjudication"]
    attempt_id: str | None = None
    question_event_id: str | None = None
    state_snapshot_id: str | None = None
    payload_json: dict[str, Any] | None = None
    assigned_to: str | None = None


class DoctorTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    task_id: str
    round_id: str
    run_id: str
    encounter_id: str
    attempt_id: str | None
    question_event_id: str | None
    state_snapshot_id: str | None
    task_type: str
    payload_json: dict[str, Any] | None
    response_json: dict[str, Any] | None
    assigned_to: str | None
    status: str
    version: int
    created_at: datetime
    completed_at: datetime | None


class DoctorTaskResponse(BaseModel):
    task_id: str
    version: int
    response_json: dict[str, Any]


class DoctorRoundImport(BaseModel):
    responses: list[DoctorTaskResponse] = Field(min_length=1)


class DoctorDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    document_id: str
    round_id: str
    format: str
    storage_key: str
    checksum: str | None
    status: str
    generated_at: datetime
    imported_at: datetime | None


class TurnCreate(BaseModel):
    speaker: Literal["interviewer", "patient", "system"]
    raw_text: str = Field(min_length=1)
    input_mode: Literal["typed", "asr", "import"] = "typed"
    audio_object_key: str | None = None
    asr_confidence: float | None = None
    client_event_id: str | None = None


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    encounter_id: str
    turn_no: int
    speaker: str
    raw_text: str
    input_mode: str
    audio_object_key: str | None
    asr_confidence: float | None
    content_sha256: str
    server_received_at: datetime


class AttemptCreate(BaseModel):
    target_fact_id: str
    executed_channel: Literal["Structured", "LLM", "Human"] | None = None
    interaction_mode: Literal["async", "sync", "semi_async_human"] | None = None
    override_reason: str | None = None
    target_selected_at: datetime | None = None
    is_retry: bool = False
    previous_attempt_id: str | None = None
    selection_reason: str | None = None


class AttemptOut(BaseModel):
    attempt_id: str
    encounter_id: str
    target_fact_id: str
    slot_key: str
    attempt_no: int
    assigned_channel: str
    executed_channel: str
    interaction_mode: str
    assignment_method: str
    propensity: float
    execution_propensity: float | None = None
    target_declared_at: datetime | None = None
    target_declared_source: str | None = None
    channel_revealed_at: datetime | None = None
    is_retry: bool = False
    previous_attempt_id: str | None = None
    selection_reason: str | None = None
    duration_ms: int | None = None
    resource_units: dict[str, Any] = {}
    status: str
    state_snapshot_id: str
    created_at: datetime
    target_fact: dict[str, Any]
    state: dict[str, Any]
    assignment_schedule: dict[str, Any] | None = None


class QuestionCreate(BaseModel):
    question_text: str | None = None
    approved_by: str | None = None


class QuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    question_event_id: str
    attempt_id: str
    question_text: str
    question_source: str
    question_variant_id: str | None
    approved_by: str | None
    status: str
    version: int
    previous_question_event_id: str | None
    generated_at: datetime | None
    approved_at: datetime | None
    queued_at: datetime | None
    delivered_at: datetime | None
    viewed_at: datetime | None
    answered_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class QuestionQueueCreate(BaseModel):
    question_text: str | None = None
    approved_by: str | None = None
    ttl_minutes: int | None = None


class QuestionRevise(BaseModel):
    question_text: str
    approved_by: str | None = None


class PatientQuestionOut(BaseModel):
    question_event_id: str
    attempt_id: str
    target_fact_id: str
    slot_key: str
    question_text: str
    question_source: str
    status: str
    expires_at: datetime | None


class PatientSessionOut(BaseModel):
    encounter_id: str
    chief_complaint: str
    expires_at: datetime
    questions: list[PatientQuestionOut]
    history: list[dict[str, str]] = Field(default_factory=list)


class PatientLinkOut(BaseModel):
    link_id: str
    encounter_id: str
    attempt_id: str | None
    question_event_id: str | None
    token: str
    expires_at: datetime


class AnswerCreate(BaseModel):
    answer_text: str | None = None
    answer_status: Literal["answered", "no_response", "refused", "off_topic", "system_error"] = "answered"
    input_mode: Literal["typed", "asr", "import"] = "typed"
    audio_object_key: str | None = None
    asr_confidence: float | None = None

    def model_post_init(self, __context) -> None:
        if self.answer_status == "answered" and not (self.answer_text or "").strip():
            raise ValueError("answer_text is required when answer_status=answered")


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    candidate_id: str
    clinical_fact_id: str
    slot_key: str
    assertion_status: str
    value_json: Any | None
    value_text: str | None
    unit: str | None
    polarity: str | None
    subject: str | None
    temporality: str | None
    certainty: str | None
    confidence: float | None
    evidence_quote: str | None
    validation_status: str
    validation_errors_json: list[Any] | None
    created_at: datetime


class FailureCreate(BaseModel):
    failure_stage: Literal["acquisition", "recording", "extraction", "system"]
    failure_code: str = Field(min_length=2, max_length=80)
    message: str | None = None
    recoverable: bool = True


class FailureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    failure_id: str
    attempt_id: str
    failure_stage: str
    failure_code: str
    message: str | None
    recoverable: bool
    actor_id: str | None
    created_at: datetime


class AdjudicationAssignmentCreate(BaseModel):
    rater_ids: list[str] = Field(min_length=1)
    rater_role: str = "adjudicator"
    blinded_to_channel: bool = True
    blinded_to_ai: bool = True


class ConsensusFinalize(BaseModel):
    final_rater_id: str
    acquired_label: Label
    recorded_label: Label
    usable_label: Label
    actionable_label: Label
    reason_text: str | None = None


class GroundTruthUpsert(BaseModel):
    clinical_fact_id: str
    reference_status: str = "unknown"
    reference_value: Any | None = None
    encryption_key_ref: str | None = None


class AttemptDetailOut(AttemptOut):
    question: QuestionOut | None = None
    turns: list[TurnOut] = []
    candidates: list[CandidateOut] = []
    outcome: dict[str, Any] | None = None
    failure_events: list[dict[str, Any]] = []
    consensus: dict[str, Any] | None = None
    assignment_schedule: dict[str, Any] | None = None


class HumanRecordCreate(BaseModel):
    clinical_fact_id: str | None = None
    human_minutes: float | None = None
    assertion_status: Literal["positive", "negative", "unknown", "ambiguous", "not_applicable"] = "ambiguous"
    value: Any | None = None
    value_text: str | None = None
    unit: str | None = None
    polarity: Literal["positive", "negative", "neutral", "unknown"] | None = None
    subject: Literal["patient", "family", "other"] | None = "patient"
    temporality: Literal["current", "past", "recurrent", "unknown"] | None = "current"
    certainty: Literal["high", "medium", "low"] | None = "medium"
    confidence: float = 1.0
    evidence_quote: str | None = None


class OutcomeCreate(BaseModel):
    rater_id: str
    rater_role: str = "adjudicator"
    adjudication_round: int = 1
    blinded_to_channel: bool = True
    blinded_to_ai: bool = True
    acquired_label: Label
    recorded_label: Label
    usable_label: Label
    actionable_label: Label
    reason_code: str | None = None
    reason_text: str | None = None
    label_codebook_version: str = "real-pilot-label-v1"


class OutcomeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    outcome_id: str
    attempt_id: str
    rater_id: str
    rater_role: str
    adjudication_round: int
    acquired_label: str
    recorded_label: str
    usable_label: str
    actionable_label: str
    reason_code: str | None
    reason_text: str | None
    label_codebook_version: str
    created_at: datetime


class ExportCreate(BaseModel):
    include_roles: list[Literal["train", "calib", "valid", "test", "heldout_shift"]] | None = None
    require_consensus: bool = True
    analysis_population: Literal["itt", "per_protocol"] = "itt"
    min_cell_size: int = 0
    max_pending_rate: float = 0.0
    baseline_success_rate: float = 0.85


class ExportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    export_id: str
    status: str
    export_dir: str
    row_count: int
    manifest_json: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None
