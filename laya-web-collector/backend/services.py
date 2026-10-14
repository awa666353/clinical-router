"""Domain services for event capture, extraction, state projection and export."""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import secrets
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .catalog import Catalog, FactDefinition, load_catalog
from .config import block_size_options, settings
from .llm import LLMClient, fallback_extract, fallback_question
from .storage import get_storage
from .models import (
    AdjudicationAssignment,
    AdjudicationConsensus,
    AssignmentSchedule,
    AttemptFailureEvent,
    AttemptOutcome,
    AuditEvent,
    Case,
    CaseBatch,
    CaseGroundTruth,
    CaseRun,
    DoctorDocument,
    DoctorRound,
    DoctorTask,
    Encounter,
    EncounterAssignment,
    FactAssertionCandidate,
    FactAttempt,
    FactCurrentState,
    FactStateEvent,
    GameScoreEvent,
    LLMCall,
    PatientActor,
    PatientQuestionLink,
    QuestionEvent,
    QuestionJob,
    QuestionStatusEvent,
    RandomizationBlock,
    RunEvent,
    RouteStateSnapshot,
    TurnEvent,
    utcnow,
)

CHANNELS = ("Structured", "LLM", "Human")
STATE_STATUSES = {"unknown", "asked_no_answer", "ambiguous", "obtained", "denied", "conflicting", "not_applicable"}
VALID_INTERACTION_MODES = {"async", "sync", "semi_async_human"}


def _is_roleplay_attempt(db: Session, attempt: FactAttempt) -> bool:
    encounter = db.get(Encounter, attempt.encounter_id)
    case = db.get(Case, encounter.case_id) if encounter else None
    return bool(case and case.source_type == "synthetic" and (case.template_id or "").startswith("roleplay:"))


def default_interaction_mode(channel: str) -> str:
    return "sync" if channel == "Human" else "async"


def _parse_client_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_expired(value: datetime | None) -> bool:
    return value is not None and _as_utc(value) < utcnow()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def audit(
    db: Session,
    *,
    actor_id: str | None,
    event_type: str,
    object_type: str,
    object_id: str,
    before_hash: str | None = None,
    after_hash: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    db.add(
        AuditEvent(
            actor_id=actor_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            before_hash=before_hash,
            after_hash=after_hash,
            detail_json=dict(detail or {}),
        )
    )


def _get_encounter_locked(db: Session, encounter_id: str) -> Encounter:
    encounter = db.scalar(
        select(Encounter).where(Encounter.encounter_id == encounter_id).with_for_update()
    )
    if encounter is None:
        raise ValueError(f"Encounter not found: {encounter_id}")
    return encounter


def _get_or_404(db: Session, model, key: str, value: str):
    item = db.get(model, value)
    if item is None:
        raise ValueError(f"{model.__name__} not found: {value}")
    return item


def create_case(db: Session, payload: Mapping[str, Any]) -> Case:
    source_type = str(payload.get("source_type", "standardized_patient"))
    if source_type == "real" and not settings.allow_real_patient_data:
        # D1: this deployment only accepts synthetic / standardized-patient
        # data; real patient data would leave the region and needs a separate
        # compliance review.
        raise ValueError("source_type='real' is disabled on this deployment (ALLOW_REAL_PATIENT_DATA=0)")
    case = Case(
        case_code=str(payload["case_code"]),
        source_type=source_type,
        topic=str(payload.get("topic", "chest_pain")),
        template_id=payload.get("template_id"),
        source_case_id=payload.get("source_case_id"),
        split_role=str(payload.get("split_role", "train")),
        shift_scope=payload.get("shift_scope"),
        success_context_json=payload.get("success_context"),
        patient_group_id=payload.get("patient_group_id"),
        collection_time_bucket=payload.get("collection_time_bucket"),
        site_id=payload.get("site_id"),
        status="ready",
    )
    db.add(case)
    db.flush()
    audit(db, actor_id=payload.get("actor_id"), event_type="case.created", object_type="case", object_id=case.case_id)
    db.commit()
    db.refresh(case)
    return case


def _new_encounter_seed() -> int:
    if settings.randomization_deterministic_seed is not None:
        return int(settings.randomization_deterministic_seed)
    return secrets.randbits(63)


def create_encounter(db: Session, payload: Mapping[str, Any]) -> Encounter:
    case = _get_or_404(db, Case, "case_id", str(payload["case_id"]))
    encounter = Encounter(
        case_id=case.case_id,
        interviewer_id=payload.get("interviewer_id"),
        patient_actor_id=payload.get("patient_actor_id"),
        site_id=payload.get("site_id") or case.site_id,
        protocol_version=str(payload.get("protocol_version", "real-pilot-v2")),
        language=str(payload.get("language", "zh-CN")),
        device=payload.get("device"),
        randomization_seed=int(payload.get("randomization_seed") or _new_encounter_seed()),
        randomization_seed_source=(
            "provided" if payload.get("randomization_seed") is not None else "generated"
        ),
    )
    db.add(encounter)
    db.flush()
    _ensure_current_state_rows(db, encounter.encounter_id)
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="encounter.created",
        object_type="encounter",
        object_id=encounter.encounter_id,
    )
    db.commit()
    db.refresh(encounter)
    return encounter


def _ensure_current_state_rows(db: Session, encounter_id: str) -> None:
    catalog = load_catalog()
    existing = {
        row.clinical_fact_id
        for row in db.scalars(
            select(FactCurrentState).where(FactCurrentState.encounter_id == encounter_id)
        )
    }
    for fact in catalog.facts:
        if fact.clinical_fact_id not in existing:
            db.add(
                FactCurrentState(
                    encounter_id=encounter_id,
                    clinical_fact_id=fact.clinical_fact_id,
                    status="unknown",
                )
            )


def append_turn(db: Session, encounter_id: str, payload: Mapping[str, Any]) -> TurnEvent:
    _get_encounter_locked(db, encounter_id)
    content = str(payload["raw_text"])
    max_turn = db.scalar(select(func.max(TurnEvent.turn_no)).where(TurnEvent.encounter_id == encounter_id)) or 0
    turn = TurnEvent(
        encounter_id=encounter_id,
        turn_no=int(max_turn) + 1,
        speaker=str(payload["speaker"]),
        raw_text=content,
        input_mode=str(payload.get("input_mode", "typed")),
        audio_object_key=payload.get("audio_object_key"),
        asr_confidence=payload.get("asr_confidence"),
        client_event_id=payload.get("client_event_id"),
        content_sha256=sha256_text(content),
    )
    db.add(turn)
    db.flush()
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="turn.appended",
        object_type="turn",
        object_id=turn.turn_id,
        after_hash=turn.content_sha256,
        detail={"speaker": turn.speaker, "turn_no": turn.turn_no},
    )
    db.commit()
    db.refresh(turn)
    return turn


def current_fact_states(db: Session, encounter_id: str) -> dict[str, FactCurrentState]:
    _ensure_current_state_rows(db, encounter_id)
    rows = db.scalars(
        select(FactCurrentState).where(FactCurrentState.encounter_id == encounter_id)
    ).all()
    return {row.clinical_fact_id: row for row in rows}


def build_visible_state(
    db: Session,
    *,
    encounter: Encounter,
    target_fact_id: str,
    question_text: str = "",
) -> dict[str, Any]:
    catalog = load_catalog()
    states = current_fact_states(db, encounter.encounter_id)
    turn_count = db.scalar(select(func.max(TurnEvent.turn_no)).where(TurnEvent.encounter_id == encounter.encounter_id)) or 0
    attempts = db.scalars(
        select(FactAttempt).where(FactAttempt.encounter_id == encounter.encounter_id)
    ).all()
    attempts_by_fact: dict[str, int] = defaultdict(int)
    channel_history: dict[str, dict[str, int]] = {channel: {"attempts": 0} for channel in CHANNELS}
    for attempt in attempts:
        attempts_by_fact[attempt.target_fact_id] += 1
        channel_history[attempt.executed_channel]["attempts"] += 1
    observed = []
    covered_weight = 0
    total_weight = 0
    for fact in catalog.facts:
        state = states.get(fact.clinical_fact_id)
        status = state.status if state else "unknown"
        total_weight += fact.criticality
        if status in {"obtained", "denied", "not_applicable"}:
            covered_weight += fact.criticality
        observed.append(
            {
                "clinical_fact_id": fact.clinical_fact_id,
                "status": status,
                "value": state.value_json if state else None,
                "polarity": state.polarity if state else None,
            }
        )
    state = {
        "state_schema_version": "route-state-v3",
        "topic": catalog.topic,
        "chief_complaint": _chief_complaint_for_case(db, encounter.case_id),
        "turn": int(turn_count),
        "max_turns": 30,
        "target_fact_id": target_fact_id,
        "slot_key": f"{catalog.topic}::{target_fact_id}",
        "observed_facts": observed,
        "channel_history": channel_history,
        "fact_attempts": dict(attempts_by_fact),
        "coverage_estimate": (covered_weight / total_weight) if total_weight else 0.0,
        "success_context": _success_context(db, encounter=encounter, target_fact_id=target_fact_id),
    }
    return state


SUCCESS_CONTEXT_KEYS = (
    "question_form",
    "communication_clarity",
    "language_support_match",
    "device_help_needed",
    "current_symptom_activity",
)


def _success_context(db: Session, *, encounter: Encounter, target_fact_id: str) -> dict[str, Any]:
    """Pre-attempt covariates declared before the question exists.

    Only case/script-declared observational covariates may enter here.  Action
    availability and resource context stay out of the model-visible state, and
    nothing in this block may be derived from the patient's answer.
    """
    case = db.get(Case, encounter.case_id)
    declared = dict((case.success_context_json or {}) if case else {})
    context: dict[str, Any] = {
        key: declared[key] for key in SUCCESS_CONTEXT_KEYS if declared.get(key) is not None
    }
    context.setdefault("language_support_match", encounter.language is not None)
    context.setdefault("device_help_needed", bool(encounter.device))
    try:
        fact = load_catalog().fact(target_fact_id)
        context["fact_answer_schema"] = dict(fact.answer_schema)
    except Exception:
        pass
    return context


def _chief_complaint_for_case(db: Session, case_id: str) -> str:
    case = db.get(Case, case_id)
    if case is None:
        return ""
    # The initial pilot catalog uses a stable demo chief complaint.  Real cases
    # should carry this in a dedicated column or encrypted case profile.
    return "胸痛"


def start_attempt(db: Session, encounter_id: str, payload: Mapping[str, Any]) -> FactAttempt:
    encounter = _get_encounter_locked(db, encounter_id)
    catalog = load_catalog()
    target_fact_id = str(payload["target_fact_id"])
    catalog.fact(target_fact_id)
    max_no = db.scalar(
        select(func.max(FactAttempt.attempt_no)).where(
            FactAttempt.encounter_id == encounter_id,
            FactAttempt.target_fact_id == target_fact_id,
        )
    ) or 0
    attempt_no = int(max_no) + 1
    assignment = _next_assignment(db, encounter=encounter, target_fact_id=target_fact_id)
    assigned = assignment.assigned_channel
    requested_executed = payload.get("executed_channel")
    executed = str(requested_executed or assigned)
    if executed not in CHANNELS:
        raise ValueError(f"invalid executed_channel: {executed}")
    override_reason = payload.get("override_reason") if executed != assigned else None
    if executed != assigned and not override_reason:
        raise ValueError("override_reason is required when executed_channel differs from assignment")
    state = build_visible_state(db, encounter=encounter, target_fact_id=target_fact_id)
    state_json = canonical_json(state)
    snapshot = RouteStateSnapshot(
        encounter_id=encounter_id,
        target_fact_id=target_fact_id,
        slot_key=f"{catalog.topic}::{target_fact_id}",
        state_json=state,
        state_hash=sha256_text(state_json),
    )
    db.add(snapshot)
    db.flush()
    case = _get_or_404(db, Case, "case_id", encounter.case_id)
    game_human_mode = (
        case.source_type == "synthetic"
        and (case.template_id or "").startswith("roleplay:")
        and executed == "Human"
    )
    interaction_mode = str(
        payload.get("interaction_mode")
        or ("semi_async_human" if game_human_mode else default_interaction_mode(executed))
    )
    if interaction_mode not in VALID_INTERACTION_MODES:
        raise ValueError(f"invalid interaction_mode: {interaction_mode}")
    # Allocation concealment evidence: the target must have been declared
    # before the allocation is revealed to the clinician.
    request_received_at = utcnow()
    declared = _parse_client_time(payload.get("target_selected_at"))
    if declared is None:
        target_declared_at = request_received_at
        target_declared_source = "server_receipt"
    elif declared > request_received_at:
        target_declared_at = request_received_at
        target_declared_source = "client_clamped"
    else:
        target_declared_at = declared
        target_declared_source = "client"
    channel_revealed_at = utcnow()
    previous_attempt_id = payload.get("previous_attempt_id")
    attempt = FactAttempt(
        encounter_id=encounter_id,
        target_fact_id=target_fact_id,
        slot_key=snapshot.slot_key,
        attempt_no=attempt_no,
        assigned_channel=assigned,
        executed_channel=executed,
        interaction_mode=interaction_mode,
        assignment_method=(
            "clinician_override" if executed != assigned else f"randomized_{assignment.strategy}"
        ),
        propensity=assignment.propensity,
        execution_propensity=(
            assignment.propensity if executed == assigned else None
        ),
        block_id=assignment.block_id,
        assignment_schedule_id=assignment.schedule_id,
        override_reason=str(override_reason) if override_reason else None,
        state_snapshot_id=snapshot.snapshot_id,
        target_declared_at=target_declared_at,
        target_declared_source=target_declared_source,
        channel_revealed_at=channel_revealed_at,
        is_retry=bool(payload.get("is_retry")) or bool(previous_attempt_id),
        previous_attempt_id=str(previous_attempt_id) if previous_attempt_id else None,
        selection_reason=payload.get("selection_reason"),
        status="created",
    )
    db.add(attempt)
    db.flush()
    assignment.used_at = utcnow()
    assignment.used_by_attempt_id = attempt.attempt_id
    db.add(assignment)
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="attempt.started",
        object_type="attempt",
        object_id=attempt.attempt_id,
        after_hash=snapshot.state_hash,
        detail={
            "assigned_channel": assigned,
            "executed_channel": executed,
            "interaction_mode": interaction_mode,
            "target_fact_id": target_fact_id,
            "assignment_schedule_id": assignment.schedule_id,
            "assignment_method": attempt.assignment_method,
            "propensity": attempt.propensity,
            "execution_propensity": attempt.execution_propensity,
            "block_sequence_index": assignment.block_sequence_index,
            "block_position": assignment.block_position,
            "strategy": assignment.strategy,
            "allocation_concealed": channel_revealed_at >= target_declared_at,
            "target_declared_source": target_declared_source,
            "is_retry": attempt.is_retry,
        },
    )
    db.commit()
    db.refresh(attempt)
    return attempt


def _ensure_randomization_block(db: Session, *, topic: str, target_fact_id: str) -> RandomizationBlock:
    block = db.scalar(
        select(RandomizationBlock)
        .where(
            RandomizationBlock.topic == topic,
            RandomizationBlock.target_fact_id == target_fact_id,
            RandomizationBlock.schedule_version == settings.randomization_schedule_version,
        )
        .with_for_update()
    )
    if block is not None:
        if not block.salt:
            block.salt = secrets.token_hex(32)
        return block
    block = RandomizationBlock(
        topic=topic,
        target_fact_id=target_fact_id,
        schedule_version=settings.randomization_schedule_version,
        target_per_arm=settings.randomization_target_per_arm,
        strategy=settings.randomization_strategy,
        salt=secrets.token_hex(32),
    )
    db.add(block)
    db.flush()
    return block


def _draw_bits(secret: str, *parts: Any) -> int:
    """Keyed draw so the allocation sequence is not derivable from public ids."""
    message = "|".join(str(part) for part in parts).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return int.from_bytes(digest[:8], "big")


def _permuted_segment(*, salt: str, segment_index: int, segment_length: int) -> list[str]:
    """One permutation per segment.

    The permutation must not depend on the encounter: a single segment spans
    several encounters, and a per-encounter permutation would break the arm
    quota inside the segment.
    """
    quota = segment_length // len(CHANNELS)
    arms = list(CHANNELS) * quota
    for index in range(len(arms) - 1, 0, -1):
        swap = _draw_bits(salt, "block", segment_index, index) % (index + 1)
        arms[index], arms[swap] = arms[swap], arms[index]
    return arms


def _next_segment_position(
    db: Session, *, block: RandomizationBlock
) -> tuple[int, int, int]:
    last = db.scalar(
        select(AssignmentSchedule)
        .where(AssignmentSchedule.block_id == block.block_id)
        .order_by(AssignmentSchedule.sequence_index.desc())
        .limit(1)
    )
    if (
        last is None
        or last.block_position is None
        or last.segment_length is None
        or last.block_sequence_index is None
        or last.block_position + 1 >= last.segment_length
    ):
        segment_index = (last.block_sequence_index or 0) + 1 if last is not None else 1
        sizes = block_size_options()
        size = sizes[_draw_bits(block.salt or "", "length", segment_index) % len(sizes)]
        return segment_index, 0, size
    return last.block_sequence_index, last.block_position + 1, last.segment_length


def _next_assignment(db: Session, *, encounter: Encounter, target_fact_id: str) -> AssignmentSchedule:
    case = _get_or_404(db, Case, "case_id", encounter.case_id)
    existing = db.scalar(
        select(AssignmentSchedule)
        .where(
            AssignmentSchedule.case_id == case.case_id,
            AssignmentSchedule.target_fact_id == target_fact_id,
            AssignmentSchedule.used_at.is_(None),
        )
        .order_by(AssignmentSchedule.sequence_index.asc())
    )
    if existing is not None:
        return existing

    catalog = load_catalog()
    block = _ensure_randomization_block(db, topic=catalog.topic, target_fact_id=target_fact_id)
    sequence_index = (
        db.scalar(
            select(func.max(AssignmentSchedule.sequence_index)).where(
                AssignmentSchedule.block_id == block.block_id
            )
        )
        or 0
    ) + 1
    strategy = settings.randomization_strategy
    salt = block.salt or ""
    if strategy == "stratified_simple":
        # Constant 1/3 allocation probability, independent of position and of
        # the visible state, so no position is ever deterministic.
        channel = CHANNELS[
            _draw_bits(salt, encounter.randomization_seed, sequence_index, "arm") % len(CHANNELS)
        ]
        allocation_probability = 1.0 / len(CHANNELS)
        block_sequence_index = None
        block_position = None
        segment_length = None
    elif strategy == "permuted_block":
        block_sequence_index, block_position, segment_length = _next_segment_position(db, block=block)
        permutation = _permuted_segment(
            salt=salt,
            segment_index=block_sequence_index,
            segment_length=segment_length,
        )
        quota = segment_length // len(CHANNELS)
        prefix = permutation[:block_position]
        available = [arm for arm in CHANNELS if prefix.count(arm) < quota]
        if not available:
            raise ValueError("randomization segment is exhausted")
        channel = permutation[block_position]
        allocation_probability = 1.0 / len(available)
    else:
        raise ValueError(f"unsupported randomization strategy: {strategy}")
    assignment = AssignmentSchedule(
        block_id=block.block_id,
        case_id=case.case_id,
        target_fact_id=target_fact_id,
        assigned_channel=channel,
        sequence_index=sequence_index,
        strategy=strategy,
        block_sequence_index=block_sequence_index,
        block_position=block_position,
        segment_length=segment_length,
        propensity=allocation_probability,
        schedule_version=settings.randomization_schedule_version,
    )
    db.add(assignment)
    db.flush()
    return assignment


def _assign_channel(seed: int, encounter_id: str, target_fact_id: str, attempt_no: int) -> str:
    """Legacy hash helper kept for diagnostics/tests. New attempts use blocks."""
    raw = f"{seed}|{encounter_id}|{target_fact_id}|{attempt_no}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    return CHANNELS[value % len(CHANNELS)]


def create_question(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> QuestionEvent:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
    catalog = load_catalog()
    fact = catalog.fact(attempt.target_fact_id)
    question_text = (payload.get("question_text") or "").strip()
    source = "human"
    variant_id = None
    call_id = None
    if attempt.executed_channel == "Structured":
        variant = catalog.first_structured_question(fact.clinical_fact_id)
        if variant is None:
            question_text = question_text or f"请描述一下{fact.display_name}。"
        else:
            question_text = str(variant["text"])
            variant_id = str(variant.get("question_variant_id")) or None
        source = "structured_template"
    elif attempt.executed_channel == "LLM":
        state = _load_snapshot_state(db, attempt.state_snapshot_id)
        recent = _recent_dialogue(db, attempt.encounter_id)
        try:
            result = LLMClient().generate_question(
                topic=catalog.topic,
                chief_complaint=_chief_complaint_for_case(db, encounter.case_id),
                state=state,
                target_fact=fact,
                dialogue=recent,
            )
            question_text = str(result.payload.get("question_text") or "").strip()
            call = _persist_llm_call(db, attempt, result, purpose="question_generation")
            call_id = call.call_id
        except Exception:
            question_text = question_text or fallback_question(fact, catalog)
        source = "llm"
    else:
        source = "human"
    if not question_text:
        raise ValueError("question_text is required for Human channel")
    question = QuestionEvent(
        attempt_id=attempt.attempt_id,
        question_text=question_text,
        question_source=source,
        question_variant_id=variant_id,
        generated_by_call_id=call_id,
        approved_by=payload.get("approved_by"),
        status="generated",
        generated_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=settings.question_expiry_minutes),
    )
    db.add(question)
    attempt.status = "questioned"
    db.flush()
    audit(
        db,
        actor_id=payload.get("actor_id") or payload.get("approved_by"),
        event_type="question.created",
        object_type="question",
        object_id=question.question_event_id,
        detail={"attempt_id": attempt.attempt_id, "source": source},
    )
    db.commit()
    db.refresh(question)
    return question


def latest_question(db: Session, attempt_id: str) -> QuestionEvent | None:
    return db.scalar(
        select(QuestionEvent)
        .where(QuestionEvent.attempt_id == attempt_id)
        .order_by(QuestionEvent.version.desc(), QuestionEvent.created_at.desc())
    )


def _record_question_status(
    db: Session,
    *,
    question: QuestionEvent,
    to_status: str,
    actor_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    from_status = question.status
    if from_status == to_status:
        return
    question.status = to_status
    db.add(
        QuestionStatusEvent(
            question_event_id=question.question_event_id,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            metadata_json=dict(metadata or {}),
        )
    )


def plan_question(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> QuestionEvent:
    """Create an immutable question version and optionally enqueue LLM generation."""
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    current = latest_question(db, attempt_id)
    if current is not None and current.status not in {"cancelled", "expired"}:
        return current
    catalog = load_catalog()
    fact = catalog.fact(attempt.target_fact_id)
    question_text = str(payload.get("question_text") or "").strip()
    if attempt.executed_channel == "Structured":
        variant = catalog.first_structured_question(fact.clinical_fact_id)
        if variant is None:
            question_text = question_text or f"请描述一下{fact.display_name}。"
        else:
            question_text = str(variant["text"])
        question = QuestionEvent(
            attempt_id=attempt.attempt_id,
            question_text=question_text,
            question_source="structured_template",
            question_variant_id=str(variant.get("question_variant_id")) if variant else None,
            status="generated",
            generated_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=settings.question_expiry_minutes),
        )
        db.add(question)
    elif attempt.executed_channel == "LLM":
        question = QuestionEvent(
            attempt_id=attempt.attempt_id,
            question_text="",
            question_source="llm",
            status="planned",
            expires_at=utcnow() + timedelta(minutes=settings.question_expiry_minutes),
        )
        db.add(question)
        db.flush()
        job = QuestionJob(
            attempt_id=attempt.attempt_id,
            question_event_id=question.question_event_id,
            job_type="generate_llm_question",
            idempotency_key=f"generate_llm_question:{question.question_event_id}",
        )
        db.add(job)
    else:
        if not question_text:
            raise ValueError("question_text is required for Human channel")
        question = QuestionEvent(
            attempt_id=attempt.attempt_id,
            question_text=question_text,
            question_source="human",
            status="generated",
            generated_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=settings.question_expiry_minutes),
        )
        db.add(question)
    db.flush()
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="question.planned",
        object_type="question",
        object_id=question.question_event_id,
        detail={"attempt_id": attempt_id, "source": question.question_source},
    )
    db.commit()
    db.refresh(question)
    return question


def enqueue_extract_job(
    db: Session,
    *,
    attempt: FactAttempt,
    answer_turn_id: str,
    question_event_id: str | None = None,
) -> QuestionJob | None:
    """Queue answer extraction durably (W6/B7).

    ``idempotency_key`` makes repeated answers/retries collapse onto one job.
    Regular Human-channel attempts use ``record_human_candidate``. Local
    role-play questions keep the Human-authored question but still queue LLM
    extraction so their answers enter the normal candidate and review flow.
    """
    if (attempt.executed_channel == "Human" and not _is_roleplay_attempt(db, attempt)) or attempt.status.startswith("failed_"):
        return None
    key = f"extract:{attempt.attempt_id}:{answer_turn_id}"
    existing = db.scalar(select(QuestionJob).where(QuestionJob.idempotency_key == key))
    if existing is not None:
        return existing
    job = QuestionJob(
        attempt_id=attempt.attempt_id,
        question_event_id=question_event_id,
        job_type="extract_answer",
        status="queued",
        idempotency_key=key,
    )
    db.add(job)
    db.flush()
    return job


def process_question_job(db: Session, job_id: str) -> QuestionJob:
    job = _get_or_404(db, QuestionJob, "job_id", job_id)
    if job.status in {"succeeded", "cancelled"}:
        return job
    job.status = "running"
    job.attempts += 1
    job.started_at = utcnow()
    db.add(job)
    db.flush()
    attempt = None
    question = None
    try:
        attempt = _get_or_404(db, FactAttempt, "attempt_id", job.attempt_id)
        if job.job_type == "extract_answer":
            if (attempt.executed_channel == "Human" and not _is_roleplay_attempt(db, attempt)) or attempt.status.startswith("failed_"):
                job.status = "succeeded"
                job.error = "skipped: attempt does not need LLM extraction"
            else:
                extract_attempt(db, attempt.attempt_id)
                job.status = "succeeded"
                job.error = None
        elif job.job_type != "generate_llm_question":
            raise ValueError(f"unsupported queued job_type: {job.job_type}")
        else:
            question = _get_or_404(db, QuestionEvent, "question_event_id", job.question_event_id)
            encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
            catalog = load_catalog()
            fact = catalog.fact(attempt.target_fact_id)
            result = LLMClient().generate_question(
                topic=catalog.topic,
                chief_complaint=_chief_complaint_for_case(db, encounter.case_id),
                state=_load_snapshot_state(db, attempt.state_snapshot_id),
                target_fact=fact,
                dialogue=_recent_dialogue(db, attempt.encounter_id),
            )
            question.question_text = str(result.payload.get("question_text") or "").strip()
            if not question.question_text:
                raise ValueError("LLM returned an empty question")
            question.generated_by_call_id = _persist_llm_call(
                db, attempt, result, purpose="question_generation"
            ).call_id
            question.status = "generated"
            question.generated_at = utcnow()
            db.add(question)
            job.status = "succeeded"
            job.error = None
        job.finished_at = utcnow()
    except Exception as exc:
        if job.job_type == "extract_answer":
            # Extraction failures are recoverable: flag the attempt for human
            # review and let the worker retry until max_attempts is reached.
            return _fail_extract_job(
                db,
                job_id=job_id,
                attempt_id=attempt.attempt_id if attempt else None,
                error=str(exc),
            )
        # Keep staging/demo usable when the LLM key is absent. Production
        # health check must still require llm_configured=true.
        if attempt is not None and question is not None:
            catalog = load_catalog()
            question.question_text = fallback_question(catalog.fact(attempt.target_fact_id), catalog)
            question.status = "generated"
            question.generated_at = utcnow()
            db.add(question)
            job.status = "succeeded"
            job.error = f"fallback_used: {exc}"
        else:
            job.status = "failed"
            job.error = str(exc)
        job.finished_at = utcnow()
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _fail_extract_job(db: Session, *, job_id: str, attempt_id: str | None, error: str) -> QuestionJob:
    """Roll back a failed extraction and mark both attempt and job."""
    db.rollback()
    fresh_attempt = db.get(FactAttempt, attempt_id) if attempt_id else None
    if fresh_attempt is not None and not fresh_attempt.status.startswith("failed_"):
        fresh_attempt.status = "needs_review"
        db.add(fresh_attempt)
    fresh_job = db.get(QuestionJob, job_id)
    if fresh_job is None:
        raise ValueError(f"question job disappeared during extraction: {job_id}")
    fresh_job.status = "failed"
    # The increment before the rollback was discarded; restore it so
    # max_attempts still bounds retries.
    fresh_job.attempts = fresh_job.attempts + 1
    fresh_job.error = error
    fresh_job.finished_at = utcnow()
    if fresh_job.attempts >= fresh_job.max_attempts:
        audit(
            db,
            actor_id="system",
            event_type="question_job.failed",
            object_type="question_job",
            object_id=fresh_job.job_id,
            detail={"job_type": "extract_answer", "error": error[:500]},
        )
    db.add(fresh_job)
    db.commit()
    db.refresh(fresh_job)
    return fresh_job


def reap_stuck_question_jobs(db: Session, *, stale_minutes: int = 10) -> int:
    """Return jobs stuck in ``running`` (crashed worker) to the queue (W6.3)."""
    cutoff = utcnow() - timedelta(minutes=stale_minutes)
    rows = db.scalars(
        select(QuestionJob).where(
            QuestionJob.status == "running",
            QuestionJob.started_at.is_not(None),
            QuestionJob.started_at < cutoff,
        )
    ).all()
    for job in rows:
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.finished_at = utcnow()
            job.error = ((job.error or "") + " | reaped: stale running job").strip(" |")
            audit(
                db,
                actor_id="system",
                event_type="question_job.reaped",
                object_type="question_job",
                object_id=job.job_id,
                detail={"job_type": job.job_type, "attempts": job.attempts},
            )
        else:
            job.status = "queued"
            job.started_at = None
        db.add(job)
    if rows:
        db.commit()
    return len(rows)


def process_pending_question_jobs(db: Session, limit: int = 20) -> list[QuestionJob]:
    reap_stuck_question_jobs(db)
    jobs = db.scalars(
        select(QuestionJob)
        .where(
            QuestionJob.status.in_(("queued", "failed")),
            QuestionJob.attempts < QuestionJob.max_attempts,
        )
        .order_by(QuestionJob.scheduled_at.asc())
        .with_for_update(skip_locked=True)
        .limit(limit)
    ).all()
    results = []
    for job in jobs:
        results.append(process_question_job(db, job.job_id))
    return results


def approve_question(db: Session, question_event_id: str, actor_id: str | None = None) -> QuestionEvent:
    question = _get_or_404(db, QuestionEvent, "question_event_id", question_event_id)
    if not question.question_text:
        raise ValueError("question has no generated text")
    question.approved_by = actor_id
    question.approved_at = utcnow()
    _record_question_status(db, question=question, to_status="approved", actor_id=actor_id)
    db.commit()
    db.refresh(question)
    return question


def queue_question(db: Session, question_event_id: str, actor_id: str | None = None) -> QuestionEvent:
    question = _get_or_404(db, QuestionEvent, "question_event_id", question_event_id)
    if question.status == "planned":
        raise ValueError("question generation is not complete")
    if not question.question_text:
        raise ValueError("question has no generated text")
    question.queued_at = utcnow()
    _record_question_status(db, question=question, to_status="queued", actor_id=actor_id)
    attempt = _get_or_404(db, FactAttempt, "attempt_id", question.attempt_id)
    attempt.status = "queued"
    db.add(attempt)
    db.commit()
    db.refresh(question)
    return question


def revise_question(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> QuestionEvent:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    previous = latest_question(db, attempt_id)
    if previous is not None:
        _record_question_status(
            db,
            question=previous,
            to_status="cancelled",
            actor_id=payload.get("actor_id"),
            metadata={"reason": "revised"},
        )
    version = (previous.version if previous else 0) + 1
    question = QuestionEvent(
        attempt_id=attempt_id,
        question_text=str(payload["question_text"]).strip(),
        question_source=previous.question_source if previous else "human",
        question_variant_id=previous.question_variant_id if previous else None,
        approved_by=payload.get("approved_by"),
        status="generated",
        version=version,
        previous_question_event_id=previous.question_event_id if previous else None,
        generated_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=settings.question_expiry_minutes),
    )
    db.add(question)
    attempt.status = "questioned"
    db.add(attempt)
    db.flush()
    audit(
        db,
        actor_id=payload.get("actor_id") or payload.get("approved_by"),
        event_type="question.revised",
        object_type="question",
        object_id=question.question_event_id,
        detail={"attempt_id": attempt_id, "version": version},
    )
    db.commit()
    db.refresh(question)
    return question


def create_patient_session_link(
    db: Session,
    *,
    encounter_id: str,
    ttl_minutes: int | None = None,
    actor_id: str | None = None,
) -> tuple[PatientQuestionLink, str]:
    encounter = _get_or_404(db, Encounter, "encounter_id", encounter_id)
    case = _get_or_404(db, Case, "case_id", encounter.case_id)
    is_roleplay = case.source_type == "synthetic" and (case.template_id or "").startswith("roleplay:")
    token = secrets.token_urlsafe(32)
    link = PatientQuestionLink(
        token_hash=sha256_text(token),
        encounter_id=encounter_id,
        attempt_id=None,
        question_event_id=None,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes or settings.patient_link_ttl_minutes),
        max_uses=200_000 if is_roleplay else 100,
    )
    db.add(link)
    audit(
        db,
        actor_id=actor_id,
        event_type="patient_session_link.created",
        object_type="encounter",
        object_id=encounter_id,
        detail={"expires_at": link.expires_at.isoformat()},
    )
    db.commit()
    db.refresh(link)
    return link, token


def create_attempt_patient_link(
    db: Session,
    *,
    question_event_id: str,
    ttl_minutes: int | None = None,
    actor_id: str | None = None,
) -> tuple[PatientQuestionLink, str]:
    question = _get_or_404(db, QuestionEvent, "question_event_id", question_event_id)
    attempt = _get_or_404(db, FactAttempt, "attempt_id", question.attempt_id)
    if question.status == "planned":
        raise ValueError("question generation is not complete")
    token = secrets.token_urlsafe(32)
    link = PatientQuestionLink(
        token_hash=sha256_text(token),
        encounter_id=attempt.encounter_id,
        attempt_id=attempt.attempt_id,
        question_event_id=question.question_event_id,
        expires_at=utcnow() + timedelta(minutes=ttl_minutes or settings.patient_link_ttl_minutes),
    )
    db.add(link)
    audit(
        db,
        actor_id=actor_id,
        event_type="patient_link.created",
        object_type="question",
        object_id=question.question_event_id,
        detail={"attempt_id": attempt.attempt_id, "expires_at": link.expires_at.isoformat()},
    )
    db.commit()
    db.refresh(link)
    return link, token


def resolve_patient_link(db: Session, token: str) -> PatientQuestionLink:
    link = db.scalar(
        select(PatientQuestionLink)
        .where(PatientQuestionLink.token_hash == sha256_text(token))
        .with_for_update()
    )
    if link is None:
        raise ValueError("invalid patient link")
    if link.revoked_at is not None:
        raise ValueError("patient link revoked")
    if _is_expired(link.expires_at):
        raise ValueError("patient link expired")
    if link.use_count >= link.max_uses:
        raise ValueError("patient link exceeded maximum uses")
    link.use_count += 1
    if link.first_viewed_at is None:
        link.first_viewed_at = utcnow()
    db.add(link)
    return link


def patient_session_view(db: Session, token: str) -> dict[str, Any]:
    link = resolve_patient_link(db, token)
    encounter = _get_or_404(db, Encounter, "encounter_id", link.encounter_id)
    case = _get_or_404(db, Case, "case_id", encounter.case_id)
    questions = db.scalars(
        select(QuestionEvent)
        .join(FactAttempt, FactAttempt.attempt_id == QuestionEvent.attempt_id)
        .where(
            FactAttempt.encounter_id == encounter.encounter_id,
            QuestionEvent.status.in_(("approved", "queued", "delivered", "viewed")),
            *(
                [QuestionEvent.question_event_id == link.question_event_id]
                if link.question_event_id
                else []
            ),
        )
        .order_by(QuestionEvent.queued_at.asc(), QuestionEvent.created_at.asc())
    ).all()
    result = []
    history = []
    changed = False
    for question in questions:
        if _is_expired(question.expires_at):
            continue
        attempt = _get_or_404(db, FactAttempt, "attempt_id", question.attempt_id)
        if question.status in {"approved", "queued"}:
            question.delivered_at = utcnow()
            _record_question_status(db, question=question, to_status="delivered", actor_id="patient")
            changed = True
        result.append(
            {
                "question_event_id": question.question_event_id,
                "attempt_id": question.attempt_id,
                "target_fact_id": attempt.target_fact_id,
                "slot_key": attempt.slot_key,
                "question_text": question.question_text,
                "question_source": question.question_source,
                "status": question.status,
                "expires_at": question.expires_at,
            }
        )
    answered_questions = db.scalars(
        select(QuestionEvent)
        .join(FactAttempt, FactAttempt.attempt_id == QuestionEvent.attempt_id)
        .where(
            FactAttempt.encounter_id == encounter.encounter_id,
            QuestionEvent.status == "answered",
            *(
                [QuestionEvent.question_event_id == link.question_event_id]
                if link.question_event_id
                else []
            ),
        )
        .order_by(QuestionEvent.created_at.asc())
    ).all()
    for answered_question in answered_questions:
        answered_attempt = db.get(FactAttempt, answered_question.attempt_id)
        answer_turn = db.get(TurnEvent, answered_attempt.end_turn_id) if answered_attempt.end_turn_id else None
        if answer_turn is not None:
            history.append({"question_text": answered_question.question_text, "answer_text": answer_turn.raw_text})
    db.commit()
    return {
        "encounter_id": encounter.encounter_id,
        "chief_complaint": _chief_complaint_for_case(db, encounter.case_id),
        "expires_at": link.expires_at,
        "questions": result,
        "history": history,
    }


def mark_patient_question_viewed(db: Session, token: str, question_event_id: str) -> QuestionEvent:
    link = resolve_patient_link(db, token)
    question = _get_or_404(db, QuestionEvent, "question_event_id", question_event_id)
    attempt = _get_or_404(db, FactAttempt, "attempt_id", question.attempt_id)
    if attempt.encounter_id != link.encounter_id:
        raise ValueError("question does not belong to patient session")
    question.viewed_at = utcnow()
    _record_question_status(db, question=question, to_status="viewed", actor_id="patient")
    db.commit()
    db.refresh(question)
    return question


def answer_patient_question(
    db: Session,
    token: str,
    question_event_id: str,
    payload: Mapping[str, Any],
) -> TurnEvent:
    link = resolve_patient_link(db, token)
    question = _get_or_404(db, QuestionEvent, "question_event_id", question_event_id)
    attempt = _get_or_404(db, FactAttempt, "attempt_id", question.attempt_id)
    if attempt.encounter_id != link.encounter_id:
        raise ValueError("question does not belong to patient session")
    if question.status not in {"approved", "queued", "delivered", "viewed"}:
        raise ValueError(f"question is not answerable in status {question.status}")
    turn = submit_answer(db, attempt.attempt_id, payload)
    question.answered_at = utcnow()
    _record_question_status(db, question=question, to_status="answered", actor_id="patient")
    link.answered_at = utcnow()
    db.add(link)
    db.add(question)
    db.commit()
    return turn


def expire_due_questions(db: Session) -> int:
    rows = db.scalars(
        select(QuestionEvent).where(
            QuestionEvent.status.in_(("planned", "generated", "approved", "queued", "delivered", "viewed")),
        )
    ).all()
    rows = [question for question in rows if _is_expired(question.expires_at)]
    for question in rows:
        _record_question_status(db, question=question, to_status="expired", actor_id="system")
    if rows:
        db.commit()
    return len(rows)


def submit_answer(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> TurnEvent:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    if attempt.status == "created":
        raise ValueError("question must be created before answer")
    answer_status = str(payload.get("answer_status", "answered"))
    if answer_status not in {"answered", "no_response", "refused", "off_topic", "system_error"}:
        raise ValueError(f"invalid answer_status: {answer_status}")
    answer_text = str(payload.get("answer_text") or "").strip()
    failure_map = {
        "no_response": ("acquisition", "patient_no_response"),
        "refused": ("acquisition", "patient_refused"),
        "off_topic": ("acquisition", "patient_off_topic"),
        "system_error": ("system", "system_error"),
    }
    if answer_status == "answered":
        if not answer_text:
            raise ValueError("answer_text is required when answer_status=answered")
        speaker = "patient"
        raw_text = answer_text
    else:
        speaker = "system" if answer_status in {"no_response", "system_error"} else "patient"
        raw_text = {
            "no_response": "患者未回答",
            "refused": answer_text or "患者拒绝回答",
            "off_topic": answer_text or "患者答非所问",
            "system_error": answer_text or "采集系统错误",
        }[answer_status]
    turn = append_turn(
        db,
        attempt.encounter_id,
        {
            "speaker": speaker,
            "raw_text": raw_text,
            "input_mode": payload.get("input_mode", "typed"),
            "audio_object_key": payload.get("audio_object_key"),
            "asr_confidence": payload.get("asr_confidence"),
        },
    )
    attempt.end_turn_id = turn.turn_id
    question = latest_question(db, attempt.attempt_id)
    if question is not None:
        question.answered_at = utcnow()
        _record_question_status(
            db,
            question=question,
            to_status="answered",
            actor_id=payload.get("actor_id") or "patient",
            metadata={"answer_status": answer_status},
        )
        db.add(question)
    if answer_status != "answered":
        stage, code = failure_map[answer_status]
        attempt.status = f"failed_{answer_status}"
        db.add(attempt)
        _finalize_attempt_resources(db, attempt)
        record_failure(
            db,
            attempt_id=attempt.attempt_id,
            failure_stage=stage,
            failure_code=code,
            message=raw_text,
            recoverable=answer_status != "refused",
            actor_id=payload.get("actor_id"),
        )
    else:
        game_attempt = _is_roleplay_attempt(db, attempt)
        attempt.status = "answered" if attempt.executed_channel == "Human" and not game_attempt else "extracting"
        db.add(attempt)
        db.commit()
        # W6.1: extraction is a durable queued job, not a fire-and-forget
        # BackgroundTask.  The unique idempotency key collapses duplicates.
        enqueue_extract_job(
            db,
            attempt=attempt,
            answer_turn_id=turn.turn_id,
            question_event_id=question.question_event_id if question is not None else None,
        )
        if game_attempt and db.scalar(
            select(GameScoreEvent).where(GameScoreEvent.answer_turn_id == turn.turn_id)
        ) is None:
            db.add(GameScoreEvent(
                encounter_id=attempt.encounter_id,
                attempt_id=attempt.attempt_id,
                answer_turn_id=turn.turn_id,
                status="queued",
            ))
        db.commit()
    db.refresh(turn)
    return turn


def _finalize_attempt_resources(
    db: Session,
    attempt: FactAttempt,
    *,
    human_minutes: float | None = None,
) -> None:
    """Record physical resource units and wall-clock duration for one attempt."""
    completed_at = attempt.completed_at or utcnow()
    attempt.completed_at = completed_at
    attempt.duration_ms = max(
        0, int((_as_utc(completed_at) - _as_utc(attempt.created_at)).total_seconds() * 1000)
    )
    llm_calls, llm_tokens = db.execute(
        select(
            func.count(LLMCall.call_id),
            func.coalesce(func.sum(func.coalesce(LLMCall.token_in, 0) + func.coalesce(LLMCall.token_out, 0)), 0),
        ).where(LLMCall.attempt_id == attempt.attempt_id)
    ).one()
    units = dict(attempt.resource_units_json or {})
    units["patient_turns"] = 1 if attempt.end_turn_id else 0
    units["llm_calls"] = int(llm_calls or 0)
    units["llm_tokens"] = int(llm_tokens or 0)
    units.setdefault("human_minutes", None)
    if human_minutes is not None:
        units["human_minutes"] = float(human_minutes)
    attempt.resource_units_json = units
    db.add(attempt)


def record_failure(
    db: Session,
    *,
    attempt_id: str,
    failure_stage: str,
    failure_code: str,
    message: str | None = None,
    recoverable: bool = True,
    actor_id: str | None = None,
) -> AttemptFailureEvent:
    _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    if failure_stage not in {"acquisition", "recording", "extraction", "system"}:
        raise ValueError(f"invalid failure_stage: {failure_stage}")
    failure = AttemptFailureEvent(
        attempt_id=attempt_id,
        failure_stage=failure_stage,
        failure_code=failure_code,
        message=message,
        recoverable=recoverable,
        actor_id=actor_id,
    )
    db.add(failure)
    db.flush()
    audit(
        db,
        actor_id=actor_id,
        event_type="attempt.failure_recorded",
        object_type="attempt",
        object_id=attempt_id,
        detail={"failure_stage": failure_stage, "failure_code": failure_code},
    )
    db.commit()
    db.refresh(failure)
    return failure


def extract_attempt(db: Session, attempt_id: str) -> list[FactAssertionCandidate]:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
    catalog = load_catalog()
    fact = catalog.fact(attempt.target_fact_id)
    question = db.scalar(
        select(QuestionEvent).where(QuestionEvent.attempt_id == attempt.attempt_id).order_by(QuestionEvent.created_at.desc())
    )
    answer = db.get(TurnEvent, attempt.end_turn_id) if attempt.end_turn_id else None
    if answer is None:
        raise ValueError("attempt has no patient answer")
    if attempt.status.startswith("failed_"):
        raise ValueError("failed attempt cannot be extracted as a usable answer")
    state = _load_snapshot_state(db, attempt.state_snapshot_id)
    recent = _recent_dialogue(db, attempt.encounter_id)
    llm_error: str | None = None
    if attempt.executed_channel == "Structured":
        payload = fallback_extract(answer_text=answer.raw_text, target_fact=fact)
        result = None
        call = LLMCall(
            encounter_id=attempt.encounter_id,
            attempt_id=attempt.attempt_id,
            purpose="extraction",
            provider="deterministic",
            model="structured-parser-v1",
            prompt_version="structured-parser-v1",
            request_hash=sha256_text(f"{attempt.attempt_id}|{answer.turn_id}"),
            status="ok",
        )
        db.add(call)
        db.flush()
    else:
        try:
            result = LLMClient().extract_candidates(
                topic=catalog.topic,
                chief_complaint=_chief_complaint_for_case(db, encounter.case_id),
                state=state,
                question_text=question.question_text if question else "",
                answer_text=answer.raw_text,
                target_fact_id=fact.clinical_fact_id,
                catalog=catalog,
                recent_dialogue=recent,
            )
            payload = result.payload
            call = _persist_llm_call(db, attempt, result, purpose="extraction")
        except Exception as exc:
            llm_error = str(exc)
            payload = fallback_extract(answer_text=answer.raw_text, target_fact=fact)
            result = None
            call = LLMCall(
                encounter_id=attempt.encounter_id,
                attempt_id=attempt.attempt_id,
                purpose="extraction",
                provider="fallback",
                model="deterministic-fallback-v1",
                prompt_version="fallback-extract-v1",
                request_hash=sha256_text(f"{attempt.attempt_id}|{answer.turn_id}"),
                status="error",
            )
            db.add(call)
            db.flush()
            db.add(
                AttemptFailureEvent(
                    attempt_id=attempt.attempt_id,
                    failure_stage="extraction",
                    failure_code="extraction_failed",
                    message=str(exc),
                    recoverable=True,
                    actor_id="system",
                )
            )

    candidates_payload = payload.get("candidates", [])
    created: list[FactAssertionCandidate] = []
    if not isinstance(candidates_payload, list):
        candidates_payload = []
    for item in candidates_payload:
        if not isinstance(item, Mapping):
            continue
        candidate = _validate_candidate(db, attempt, answer, catalog, item, llm_call_id=call.call_id)
        if candidate is None:
            continue
        created.append(candidate)
        if candidate.validation_status == "valid":
            _project_candidate(db, attempt, answer, candidate)
    attempt.status = "extracted" if any(c.validation_status == "valid" for c in created) else "needs_review"
    if not any(c.validation_status == "valid" for c in created):
        db.add(
            AttemptFailureEvent(
                attempt_id=attempt.attempt_id,
                failure_stage="extraction",
                failure_code="extraction_failed",
                message="no valid candidate passed validator",
                recoverable=True,
                actor_id="system",
            )
        )
    attempt.completed_at = utcnow()
    db.add(attempt)
    _finalize_attempt_resources(db, attempt)
    audit(
        db,
        actor_id="system",
        event_type="attempt.extracted",
        object_type="attempt",
        object_id=attempt.attempt_id,
        detail={
            "candidate_count": len(created),
            "valid_count": sum(c.validation_status == "valid" for c in created),
            "llm_error": llm_error,
        },
    )
    db.commit()
    for candidate in created:
        db.refresh(candidate)
    return created


def record_human_candidate(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> FactAssertionCandidate:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    if attempt.executed_channel != "Human":
        raise ValueError("manual recording is only allowed on the Human channel")
    answer = db.get(TurnEvent, attempt.end_turn_id) if attempt.end_turn_id else None
    if answer is None:
        raise ValueError("patient answer must be recorded before manual clinical recording")
    catalog = load_catalog()
    fact_id = str(payload.get("clinical_fact_id") or attempt.target_fact_id)
    candidate_payload = {
        **dict(payload),
        "clinical_fact_id": fact_id,
        "assertion_status": payload.get("assertion_status", "ambiguous"),
        "evidence_quote": payload.get("evidence_quote") or answer.raw_text,
        "value_text": payload.get("value_text") or answer.raw_text,
    }
    candidate = _validate_candidate(db, attempt, answer, catalog, candidate_payload)
    if candidate is None:
        raise ValueError("manual record was not accepted")
    if candidate.validation_status == "valid":
        _project_candidate(db, attempt, answer, candidate)
    else:
        db.add(
            AttemptFailureEvent(
                attempt_id=attempt.attempt_id,
                failure_stage="recording",
                failure_code="recording_invalid",
                message=str(candidate.validation_errors_json),
                recoverable=True,
                actor_id=payload.get("actor_id"),
            )
        )
    attempt.status = "extracted" if candidate.validation_status == "valid" else "needs_review"
    attempt.completed_at = utcnow()
    db.add(attempt)
    _finalize_attempt_resources(db, attempt, human_minutes=payload.get("human_minutes"))
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="candidate.human_recorded",
        object_type="attempt",
        object_id=attempt.attempt_id,
        detail={"clinical_fact_id": fact_id, "validation_status": candidate.validation_status},
    )
    db.commit()
    db.refresh(candidate)
    return candidate


def _validate_candidate(
    db: Session,
    attempt: FactAttempt,
    answer: TurnEvent,
    catalog: Catalog,
    payload: Mapping[str, Any],
    *,
    llm_call_id: str | None = None,
) -> FactAssertionCandidate | None:
    fact_id = str(payload.get("clinical_fact_id", "")).strip()
    errors: list[dict[str, Any]] = []
    try:
        fact = catalog.fact(fact_id)
    except KeyError:
        errors.append({"code": "unknown_fact", "message": f"unknown fact: {fact_id}"})
        fact = None
    status = str(payload.get("assertion_status", "ambiguous")).strip()
    if status not in {"positive", "negative", "unknown", "ambiguous", "not_applicable"}:
        errors.append({"code": "invalid_status", "message": f"invalid status: {status}"})
    value = payload.get("value")
    if fact is not None and status in {"positive", "negative", "not_applicable"}:
        value_ok = _value_allowed(fact, value)
        if not value_ok:
            errors.append({"code": "invalid_value", "message": f"value not allowed: {value!r}"})
    confidence = _as_float(payload.get("confidence"))
    if confidence is not None and confidence < 0.45:
        errors.append({"code": "low_confidence", "message": "confidence below 0.45"})
    validation_status = "valid" if not errors else ("needs_review" if all(e["code"] == "low_confidence" for e in errors) else "rejected")
    candidate = FactAssertionCandidate(
        llm_call_id=llm_call_id,
        encounter_id=attempt.encounter_id,
        turn_id=answer.turn_id,
        attempt_id=attempt.attempt_id,
        clinical_fact_id=fact_id or attempt.target_fact_id,
        slot_key=f"{catalog.topic}::{fact_id or attempt.target_fact_id}",
        assertion_status=status,
        value_json=value,
        value_text=payload.get("value_text"),
        unit=payload.get("unit"),
        polarity=payload.get("polarity"),
        subject=payload.get("subject"),
        temporality=payload.get("temporality"),
        certainty=payload.get("certainty"),
        confidence=confidence,
        evidence_quote=payload.get("evidence_quote"),
        evidence_start=_as_int(payload.get("evidence_start")),
        evidence_end=_as_int(payload.get("evidence_end")),
        validation_status=validation_status,
        validation_errors_json=errors,
    )
    db.add(candidate)
    db.flush()
    return candidate


def _value_allowed(fact: FactDefinition, value: Any) -> bool:
    allowed = set(fact.allowed_values)
    if fact.kind == "list":
        if value is None:
            return False
        values = value if isinstance(value, list) else [value]
        return all(str(item) in allowed for item in values)
    if value is None:
        return False
    return str(value) in allowed


def _project_candidate(db: Session, attempt: FactAttempt, answer: TurnEvent, candidate: FactAssertionCandidate) -> None:
    state = db.get(
        FactCurrentState,
        {"encounter_id": attempt.encounter_id, "clinical_fact_id": candidate.clinical_fact_id},
    )
    if state is None:
        state = FactCurrentState(
            encounter_id=attempt.encounter_id,
            clinical_fact_id=candidate.clinical_fact_id,
            status="unknown",
        )
        db.add(state)
        db.flush()
    after_status = {
        "positive": "obtained",
        "negative": "denied",
        "unknown": "asked_no_answer",
        "ambiguous": "ambiguous",
        "not_applicable": "not_applicable",
    }.get(candidate.assertion_status, "ambiguous")
    before_status = state.status
    before_value = state.value_json
    state.status = after_status
    state.value_json = candidate.value_json
    state.value_text = candidate.value_text
    state.polarity = candidate.polarity
    state.last_channel = attempt.executed_channel
    state.last_attempt_turn = answer.turn_no
    state.updated_at = utcnow()
    db.add(
        FactStateEvent(
            encounter_id=attempt.encounter_id,
            clinical_fact_id=candidate.clinical_fact_id,
            before_status=before_status,
            before_value_json=before_value,
            after_status=after_status,
            after_value_json=candidate.value_json,
            source_candidate_ids=[candidate.candidate_id],
        )
    )
    db.add(state)


def _persist_llm_call(db: Session, attempt: FactAttempt, result, *, purpose: str) -> LLMCall:
    # NOTE: the columns are named *_encrypted for historical reasons but store
    # plaintext.  With LLM_PERSIST_RAW=0 (D5) we only keep hashes/candidates and
    # write NULL here, so raw prompts/responses never reach the database.
    raw_request = result.request_text if settings.llm_persist_raw else None
    raw_response = result.response_text if settings.llm_persist_raw else None
    call = LLMCall(
        encounter_id=attempt.encounter_id,
        attempt_id=attempt.attempt_id,
        purpose=purpose,
        provider="dashscope-compatible",
        model=result.model,
        model_version=result.model,
        prompt_version=result.prompt_version,
        prompt_hash=sha256_text(str(result.request_text)),
        request_hash=result.request_hash,
        request_encrypted=raw_request,
        response_encrypted=raw_response,
        token_in=result.token_in,
        token_out=result.token_out,
        latency_ms=result.latency_ms,
        retry_count=int(getattr(result, "retry_count", 0) or 0),
        safety_filter_result=getattr(result, "safety_filter_result", None),
        provider_request_id=getattr(result, "provider_request_id", None),
        temperature=getattr(result, "temperature", None),
        top_p=getattr(result, "top_p", None),
        max_tokens=getattr(result, "max_tokens", None),
        retrieval_strategy=getattr(result, "retrieval_strategy", "none"),
        retrieved_doc_ids_json=list(getattr(result, "retrieved_doc_ids", []) or []),
        status=result.status,
    )
    db.add(call)
    db.flush()
    return call


def _load_snapshot_state(db: Session, snapshot_id: str) -> dict[str, Any]:
    snapshot = _get_or_404(db, RouteStateSnapshot, "snapshot_id", snapshot_id)
    return dict(snapshot.state_json)


def _recent_dialogue(db: Session, encounter_id: str, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(TurnEvent)
        .where(TurnEvent.encounter_id == encounter_id)
        .order_by(TurnEvent.turn_no.desc())
        .limit(limit)
    ).all()
    return [
        {"turn_no": row.turn_no, "speaker": row.speaker, "text": row.raw_text}
        for row in reversed(rows)
    ]


def record_outcome(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> AttemptOutcome:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
    rater_id = str(payload["rater_id"])
    if rater_id in {encounter.interviewer_id, encounter.patient_actor_id}:
        raise ValueError("adjudicator must be independent from interviewer/patient actor")
    existing = db.scalar(
        select(AttemptOutcome).where(
            AttemptOutcome.attempt_id == attempt_id,
            AttemptOutcome.rater_id == rater_id,
        )
    )
    if existing is not None:
        raise ValueError(f"rater {rater_id} has already reviewed attempt {attempt_id}")
    labels = {
        "acquired_label": str(payload["acquired_label"]),
        "recorded_label": str(payload["recorded_label"]),
        "usable_label": str(payload["usable_label"]),
        "actionable_label": str(payload["actionable_label"]),
    }
    _validate_label_dependencies(labels)
    outcome = AttemptOutcome(
        attempt_id=attempt_id,
        rater_id=rater_id,
        rater_role=str(payload.get("rater_role", "adjudicator")),
        adjudication_round=int(payload.get("adjudication_round") or 1),
        blinded_to_channel=bool(payload.get("blinded_to_channel", True)),
        blinded_to_ai=bool(payload.get("blinded_to_ai", True)),
        acquired_label=labels["acquired_label"],
        recorded_label=labels["recorded_label"],
        usable_label=labels["usable_label"],
        actionable_label=labels["actionable_label"],
        reason_code=payload.get("reason_code"),
        reason_text=payload.get("reason_text"),
        label_codebook_version=str(
            payload.get("label_codebook_version", settings.label_codebook_version)
        ),
    )
    db.add(outcome)
    assignment = db.scalar(
        select(AdjudicationAssignment).where(
            AdjudicationAssignment.attempt_id == attempt_id,
            AdjudicationAssignment.rater_id == rater_id,
        )
    )
    if assignment is None:
        assignment = AdjudicationAssignment(
            attempt_id=attempt_id,
            rater_id=rater_id,
            rater_role=outcome.rater_role,
            blinded_to_channel=outcome.blinded_to_channel,
            blinded_to_ai=outcome.blinded_to_ai,
        )
        db.add(assignment)
    assignment.status = "completed"
    assignment.completed_at = utcnow()
    db.flush()
    _recompute_consensus(db, attempt_id)
    audit(
        db,
        actor_id=outcome.rater_id,
        event_type="outcome.recorded",
        object_type="attempt",
        object_id=attempt_id,
        detail={"usable_label": outcome.usable_label, "recorded_label": outcome.recorded_label},
    )
    db.commit()
    db.refresh(outcome)
    return outcome


def assign_adjudicators(
    db: Session,
    *,
    attempt_id: str,
    rater_ids: list[str],
    rater_role: str = "adjudicator",
    blinded_to_channel: bool = True,
    blinded_to_ai: bool = True,
) -> list[AdjudicationAssignment]:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
    unique = []
    for value in rater_ids:
        rater_id = str(value)
        if rater_id not in unique:
            unique.append(rater_id)
    if len(unique) < settings.adjudication_reviews_required:
        raise ValueError(
            f"at least {settings.adjudication_reviews_required} distinct adjudicators are required"
        )
    if any(rater_id in {encounter.interviewer_id, encounter.patient_actor_id} for rater_id in unique):
        raise ValueError("adjudicator must be independent from interviewer/patient actor")
    assignments = []
    for rater_id in unique:
        row = db.scalar(
            select(AdjudicationAssignment).where(
                AdjudicationAssignment.attempt_id == attempt_id,
                AdjudicationAssignment.rater_id == rater_id,
            )
        )
        if row is None:
            row = AdjudicationAssignment(
                attempt_id=attempt_id,
                rater_id=rater_id,
                rater_role=rater_role,
                blinded_to_channel=blinded_to_channel,
                blinded_to_ai=blinded_to_ai,
            )
            db.add(row)
        assignments.append(row)
    db.flush()
    db.commit()
    for row in assignments:
        db.refresh(row)
    return assignments


def finalize_consensus(db: Session, attempt_id: str, payload: Mapping[str, Any]) -> AdjudicationConsensus:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    encounter = _get_or_404(db, Encounter, "encounter_id", attempt.encounter_id)
    review_count = db.scalar(
        select(func.count())
        .select_from(AttemptOutcome)
        .where(AttemptOutcome.attempt_id == attempt_id)
    ) or 0
    if review_count < settings.adjudication_reviews_required:
        raise ValueError(
            f"at least {settings.adjudication_reviews_required} independent reviews are required "
            f"before finalizing consensus (got {review_count})"
        )
    final_rater_id = str(payload["final_rater_id"])
    if final_rater_id in {encounter.interviewer_id, encounter.patient_actor_id}:
        raise ValueError("final adjudicator must be independent from interviewer/patient actor")
    labels = {
        "acquired_label": str(payload["acquired_label"]),
        "recorded_label": str(payload["recorded_label"]),
        "usable_label": str(payload["usable_label"]),
        "actionable_label": str(payload["actionable_label"]),
    }
    _validate_label_dependencies(labels)
    consensus = db.scalar(
        select(AdjudicationConsensus).where(AdjudicationConsensus.attempt_id == attempt_id)
    )
    if consensus is None:
        consensus = AdjudicationConsensus(attempt_id=attempt_id)
        db.add(consensus)
    consensus.acquired_label = labels["acquired_label"]
    consensus.recorded_label = labels["recorded_label"]
    consensus.usable_label = labels["usable_label"]
    consensus.actionable_label = labels["actionable_label"]
    consensus.status = "final"
    consensus.final_rater_id = final_rater_id
    consensus.disagreement_flag = bool(payload.get("disagreement_flag", consensus.disagreement_flag))
    consensus.label_codebook_version = str(
        payload.get("label_codebook_version", settings.label_codebook_version)
    )
    consensus.finalized_at = utcnow()
    db.flush()
    audit(
        db,
        actor_id=consensus.final_rater_id,
        event_type="consensus.finalized",
        object_type="attempt",
        object_id=attempt_id,
        detail={"usable_label": consensus.usable_label, "recorded_label": consensus.recorded_label},
    )
    db.commit()
    db.refresh(consensus)
    return consensus


def _validate_label_dependencies(labels: Mapping[str, str]) -> None:
    valid_labels = {"yes", "no", "uncertain", "not_applicable", "unable_to_assess"}
    invalid = {key: value for key, value in labels.items() if value not in valid_labels}
    if invalid:
        raise ValueError(f"invalid label values: {invalid}")
    acquired = labels["acquired_label"]
    recorded = labels["recorded_label"]
    usable = labels["usable_label"]
    actionable = labels["actionable_label"]
    violations = []
    if acquired in {"no", "not_applicable", "unable_to_assess"} and usable == "yes":
        violations.append(f"acquired={acquired} cannot have usable=yes")
    if acquired in {"no", "not_applicable", "unable_to_assess"} and actionable == "yes":
        violations.append(f"acquired={acquired} cannot have actionable=yes")
    if recorded in {"no", "not_applicable", "unable_to_assess"} and usable == "yes":
        violations.append(f"recorded={recorded} cannot have usable=yes")
    if recorded in {"no", "not_applicable", "unable_to_assess"} and actionable == "yes":
        violations.append(f"recorded={recorded} cannot have actionable=yes")
    if usable in {"no", "not_applicable", "unable_to_assess"} and actionable == "yes":
        violations.append(f"usable={usable} cannot have actionable=yes")
    if violations:
        raise ValueError("; ".join(violations))


def _recompute_consensus(db: Session, attempt_id: str) -> AdjudicationConsensus:
    outcomes = db.scalars(
        select(AttemptOutcome)
        .where(AttemptOutcome.attempt_id == attempt_id)
        .order_by(AttemptOutcome.created_at.asc())
    ).all()
    consensus = db.scalar(
        select(AdjudicationConsensus).where(AdjudicationConsensus.attempt_id == attempt_id)
    )
    if consensus is None:
        consensus = AdjudicationConsensus(attempt_id=attempt_id)
        db.add(consensus)
    consensus.disagreement_flag = False
    consensus.status = "pending"
    consensus.acquired_label = None
    consensus.recorded_label = None
    consensus.usable_label = None
    consensus.actionable_label = None
    consensus.final_rater_id = None
    consensus.finalized_at = None
    if len(outcomes) < settings.adjudication_reviews_required:
        consensus.agreement_rate = None
        return consensus
    fields = ("acquired_label", "recorded_label", "usable_label", "actionable_label")
    resolved: dict[str, str | None] = {}
    agreements = []
    for field in fields:
        values = [getattr(row, field) for row in outcomes]
        counts = {value: values.count(value) for value in set(values)}
        winner, winner_count = max(counts.items(), key=lambda item: item[1])
        agreements.append(winner_count / len(values))
        resolved[field] = winner if winner_count == len(values) else None
    consensus.agreement_rate = sum(agreements) / len(agreements)
    labels = {f"{field}": value for field, value in resolved.items()}
    if all(value is not None for value in resolved.values()):
        for field, value in resolved.items():
            setattr(consensus, field, value)
        consensus.status = "final"
        consensus.finalized_at = utcnow()
    else:
        consensus.disagreement_flag = True
    return consensus


def upsert_ground_truth(db: Session, case_id: str, payload: Mapping[str, Any]) -> CaseGroundTruth:
    _get_or_404(db, Case, "case_id", case_id)
    fact_id = str(payload["clinical_fact_id"])
    row = db.get(CaseGroundTruth, {"case_id": case_id, "clinical_fact_id": fact_id})
    if row is None:
        row = CaseGroundTruth(case_id=case_id, clinical_fact_id=fact_id)
        db.add(row)
    row.reference_status = str(payload.get("reference_status", "unknown"))
    row.reference_value_json = payload.get("reference_value")
    row.encryption_key_ref = payload.get("encryption_key_ref")
    db.flush()
    audit(
        db,
        actor_id=payload.get("actor_id"),
        event_type="ground_truth.upserted",
        object_type="case_ground_truth",
        object_id=f"{case_id}:{fact_id}",
    )
    db.commit()
    db.refresh(row)
    return row


def attempt_detail(db: Session, attempt_id: str) -> dict[str, Any]:
    attempt = _get_or_404(db, FactAttempt, "attempt_id", attempt_id)
    snapshot = db.get(RouteStateSnapshot, attempt.state_snapshot_id)
    question = db.scalar(
        select(QuestionEvent)
        .where(QuestionEvent.attempt_id == attempt.attempt_id)
        .order_by(QuestionEvent.version.desc(), QuestionEvent.created_at.desc())
    )
    turns = db.scalars(
        select(TurnEvent)
        .where(TurnEvent.encounter_id == attempt.encounter_id)
        .order_by(TurnEvent.turn_no.asc())
    ).all()
    candidates = db.scalars(
        select(FactAssertionCandidate)
        .where(FactAssertionCandidate.attempt_id == attempt.attempt_id)
        .order_by(FactAssertionCandidate.created_at.asc())
    ).all()
    outcomes = db.scalars(
        select(AttemptOutcome)
        .where(AttemptOutcome.attempt_id == attempt.attempt_id)
        .order_by(AttemptOutcome.created_at.desc())
    ).all()
    answer = db.get(TurnEvent, attempt.end_turn_id) if attempt.end_turn_id else None
    failures = db.scalars(
        select(AttemptFailureEvent)
        .where(AttemptFailureEvent.attempt_id == attempt.attempt_id)
        .order_by(AttemptFailureEvent.created_at.asc())
    ).all()
    consensus = db.scalar(
        select(AdjudicationConsensus).where(AdjudicationConsensus.attempt_id == attempt.attempt_id)
    )
    assignment = (
        db.get(AssignmentSchedule, attempt.assignment_schedule_id)
        if attempt.assignment_schedule_id
        else None
    )
    catalog = load_catalog()
    fact = catalog.fact(attempt.target_fact_id)
    return {
        "attempt_id": attempt.attempt_id,
        "encounter_id": attempt.encounter_id,
        "target_fact_id": attempt.target_fact_id,
        "slot_key": attempt.slot_key,
        "attempt_no": attempt.attempt_no,
        "assigned_channel": attempt.assigned_channel,
        "executed_channel": attempt.executed_channel,
        "interaction_mode": attempt.interaction_mode,
        "assignment_method": attempt.assignment_method,
        "propensity": attempt.propensity,
        "execution_propensity": attempt.execution_propensity,
        "target_declared_at": attempt.target_declared_at,
        "target_declared_source": attempt.target_declared_source,
        "channel_revealed_at": attempt.channel_revealed_at,
        "is_retry": attempt.is_retry,
        "previous_attempt_id": attempt.previous_attempt_id,
        "selection_reason": attempt.selection_reason,
        "duration_ms": attempt.duration_ms,
        "resource_units": dict(attempt.resource_units_json or {}),
        "status": attempt.status,
        "state_snapshot_id": attempt.state_snapshot_id,
        "created_at": attempt.created_at,
        "target_fact": fact.to_dict(),
        "state": dict(snapshot.state_json) if snapshot else {},
        "question": _question_dict(question),
        "answer": _turn_dict(answer) if answer else None,
        "turns": [_turn_dict(turn) for turn in turns],
        "candidates": [_candidate_dict(row) for row in candidates],
        "outcome": _outcome_dict(outcomes[0]) if outcomes else None,
        "outcomes": [_outcome_dict(row) for row in outcomes],
        "failure_events": [_failure_dict(row) for row in failures],
        "consensus": _consensus_dict(consensus) if consensus else None,
        "assignment_schedule": _assignment_dict(assignment) if assignment else None,
    }


def list_facts_with_state(db: Session, encounter_id: str) -> list[dict[str, Any]]:
    _get_or_404(db, Encounter, "encounter_id", encounter_id)
    states = current_fact_states(db, encounter_id)
    catalog = load_catalog()
    return [
        {
            **fact.to_dict(),
            "status": states.get(fact.clinical_fact_id).status if states.get(fact.clinical_fact_id) else "unknown",
            "value": states.get(fact.clinical_fact_id).value_json if states.get(fact.clinical_fact_id) else None,
            "last_channel": states.get(fact.clinical_fact_id).last_channel if states.get(fact.clinical_fact_id) else None,
        }
        for fact in catalog.facts
    ]


def adjudication_queue(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(FactAttempt, Case, Encounter)
        .join(Encounter, Encounter.encounter_id == FactAttempt.encounter_id)
        .join(Case, Case.case_id == Encounter.case_id)
        .where(FactAttempt.status.in_(("extracted", "needs_review")))
        .order_by(FactAttempt.created_at.asc())
    ).all()
    catalog = load_catalog()
    result = []
    for attempt, case, encounter in rows:
        detail = attempt_detail(db, attempt.attempt_id)
        consensus = detail.get("consensus") or {}
        if consensus.get("status") == "final":
            continue
        review_count = db.scalar(
            select(func.count())
            .select_from(AttemptOutcome)
            .where(AttemptOutcome.attempt_id == attempt.attempt_id)
        ) or 0
        result.append(
            {
                "attempt_id": attempt.attempt_id,
                "case_code": case.case_code,
                "encounter_id": encounter.encounter_id,
                "target_fact": detail["target_fact"],
                "question": detail["question"],
                "answer": detail.get("answer"),
                "candidates": detail["candidates"],
                "failure_events": detail.get("failure_events", []),
                "consensus": detail.get("consensus"),
                "review_count": review_count,
                "reviews_required": settings.adjudication_reviews_required,
            }
        )
    return result


def _failure_dict(failure: AttemptFailureEvent) -> dict[str, Any]:
    return {
        "failure_id": failure.failure_id,
        "attempt_id": failure.attempt_id,
        "failure_stage": failure.failure_stage,
        "failure_code": failure.failure_code,
        "message": failure.message,
        "recoverable": failure.recoverable,
        "actor_id": failure.actor_id,
        "created_at": failure.created_at,
    }


def _consensus_dict(row: AdjudicationConsensus) -> dict[str, Any]:
    return {
        "consensus_id": row.consensus_id,
        "attempt_id": row.attempt_id,
        "acquired_label": row.acquired_label,
        "recorded_label": row.recorded_label,
        "usable_label": row.usable_label,
        "actionable_label": row.actionable_label,
        "agreement_rate": row.agreement_rate,
        "disagreement_flag": row.disagreement_flag,
        "status": row.status,
        "final_rater_id": row.final_rater_id,
        "label_codebook_version": row.label_codebook_version,
        "created_at": row.created_at,
        "finalized_at": row.finalized_at,
    }


def _assignment_dict(row: AssignmentSchedule) -> dict[str, Any]:
    return {
        "schedule_id": row.schedule_id,
        "block_id": row.block_id,
        "case_id": row.case_id,
        "target_fact_id": row.target_fact_id,
        "assigned_channel": row.assigned_channel,
        "sequence_index": row.sequence_index,
        "strategy": row.strategy,
        "block_sequence_index": row.block_sequence_index,
        "block_position": row.block_position,
        "segment_length": row.segment_length,
        "propensity": row.propensity,
        "schedule_version": row.schedule_version,
        "assigned_at": row.assigned_at,
        "used_at": row.used_at,
        "used_by_attempt_id": row.used_by_attempt_id,
    }


def create_patient_actor(db: Session, payload: Mapping[str, Any]) -> PatientActor:
    actor = PatientActor(
        display_name=str(payload["display_name"]),
        actor_type=str(payload.get("actor_type", "standardized_patient")),
        max_parallel_runs=int(payload.get("max_parallel_runs", 1)),
        metadata_json=payload.get("metadata_json"),
    )
    db.add(actor)
    db.flush()
    db.commit()
    db.refresh(actor)
    return actor


def create_case_batch(db: Session, payload: Mapping[str, Any]) -> CaseBatch:
    case_ids = [str(value) for value in payload.get("case_ids", [])]
    if not case_ids:
        raise ValueError("case_ids is required")
    batch = CaseBatch(
        name=str(payload["name"]),
        target_size=len(case_ids),
        policy_json=payload.get("policy_json"),
        created_by=payload.get("actor_id"),
        status="running",
        started_at=utcnow(),
    )
    db.add(batch)
    db.flush()
    actor_id = payload.get("patient_actor_id")
    actor = None
    if actor_id:
        actor = _get_or_404(db, PatientActor, "actor_id", str(actor_id))
    active_count = 0
    if actor is not None:
        active_count = db.scalar(
            select(func.count())
            .select_from(CaseRun)
            .where(
                CaseRun.patient_actor_id == actor.actor_id,
                CaseRun.status.in_(("running", "waiting_patient", "waiting_doctor")),
            )
        ) or 0
    for index, case_id in enumerate(case_ids):
        case = _get_or_404(db, Case, "case_id", case_id)
        encounter = create_encounter(
            db,
            {
                "case_id": case.case_id,
                "interviewer_id": payload.get("interviewer_id"),
                "patient_actor_id": actor.actor_id if actor else None,
                "site_id": case.site_id,
                "actor_id": payload.get("actor_id"),
            },
        )
        run_status = "created"
        if actor is None or active_count < actor.max_parallel_runs:
            run_status = "running"
            active_count += 1
        run = CaseRun(
            batch_id=batch.batch_id,
            case_id=case.case_id,
            encounter_id=encounter.encounter_id,
            patient_actor_id=actor.actor_id if actor else None,
            interviewer_id=payload.get("interviewer_id"),
            status=run_status,
            current_round=0,
            started_at=utcnow() if run_status == "running" else None,
        )
        db.add(run)
        db.flush()
        if payload.get("interviewer_id"):
            db.add(
                EncounterAssignment(
                    encounter_id=encounter.encounter_id,
                    user_id=str(payload["interviewer_id"]),
                    role="interviewer",
                )
            )
        db.add(
            RunEvent(
                run_id=run.run_id,
                to_status=run_status,
                reason="batch_created",
                actor_id=payload.get("actor_id"),
            )
        )
    db.commit()
    db.refresh(batch)
    return batch


def list_patient_actor_runs(db: Session, actor_id: str) -> list[CaseRun]:
    _get_or_404(db, PatientActor, "actor_id", actor_id)
    return db.scalars(
        select(CaseRun).where(CaseRun.patient_actor_id == actor_id).order_by(CaseRun.created_at.asc())
    ).all()


def list_interviewer_encounters(db: Session, interviewer_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        select(Encounter, CaseRun, Case)
        .join(CaseRun, CaseRun.encounter_id == Encounter.encounter_id, isouter=True)
        .join(Case, Case.case_id == Encounter.case_id)
        .where(Encounter.interviewer_id == interviewer_id)
        .order_by(Encounter.created_at.desc())
    ).all()
    return [
        {
            "encounter_id": encounter.encounter_id,
            "case_id": case.case_id,
            "case_code": case.case_code,
            "chief_complaint": _chief_complaint_for_case(db, case.case_id),
            "status": run.status if run else encounter.status,
            "run_id": run.run_id if run else None,
            "current_round": run.current_round if run else 0,
        }
        for encounter, run, case in rows
    ]


def create_encounter_assignment(db: Session, payload: Mapping[str, Any]) -> EncounterAssignment:
    assignment = EncounterAssignment(
        encounter_id=str(payload["encounter_id"]),
        user_id=str(payload["user_id"]),
        role=str(payload.get("role", "interviewer")),
    )
    db.add(assignment)
    db.flush()
    db.commit()
    db.refresh(assignment)
    return assignment


def _transition_run(db: Session, run: CaseRun, to_status: str, *, reason: str | None, actor_id: str | None) -> CaseRun:
    from_status = run.status
    run.status = to_status
    if to_status == "paused":
        run.paused_at = utcnow()
    if to_status == "running":
        run.resumed_at = utcnow()
    if to_status == "completed":
        run.completed_at = utcnow()
    db.add(run)
    db.add(
        RunEvent(
            run_id=run.run_id,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            actor_id=actor_id,
        )
    )
    db.commit()
    db.refresh(run)
    return run


def pause_case_run(db: Session, run_id: str, payload: Mapping[str, Any]) -> CaseRun:
    run = _get_or_404(db, CaseRun, "run_id", run_id)
    return _transition_run(
        db,
        run,
        "waiting_doctor" if payload.get("reason") == "doctor_breakpoint" else "paused",
        reason=payload.get("reason"),
        actor_id=payload.get("actor_id"),
    )


def resume_case_run(db: Session, run_id: str, payload: Mapping[str, Any]) -> CaseRun:
    run = _get_or_404(db, CaseRun, "run_id", run_id)
    if run.status not in {"paused", "waiting_doctor", "ready_to_resume"}:
        raise ValueError(f"run cannot resume from status {run.status}")
    if run.patient_actor_id:
        actor = _get_or_404(db, PatientActor, "actor_id", run.patient_actor_id)
        active = db.scalar(
            select(func.count())
            .select_from(CaseRun)
            .where(
                CaseRun.patient_actor_id == actor.actor_id,
                CaseRun.run_id != run.run_id,
                CaseRun.status.in_(("running", "waiting_patient", "waiting_doctor")),
            )
        ) or 0
        if active >= actor.max_parallel_runs:
            return _transition_run(
                db, run, "ready_to_resume", reason="actor_capacity_full", actor_id=payload.get("actor_id")
            )
    return _transition_run(db, run, "running", reason=payload.get("reason"), actor_id=payload.get("actor_id"))


def resume_ready_runs(db: Session, *, actor_id: str | None = None, limit: int = 50) -> list[CaseRun]:
    runs = db.scalars(
        select(CaseRun)
        .where(CaseRun.status == "ready_to_resume")
        .order_by(CaseRun.created_at.asc())
        .limit(limit)
    ).all()
    resumed = []
    for run in runs:
        if actor_id and run.patient_actor_id and run.patient_actor_id != actor_id:
            continue
        resumed.append(resume_case_run(db, run.run_id, {"reason": "capacity_released", "actor_id": actor_id}))
    return resumed


def create_doctor_round(db: Session, payload: Mapping[str, Any]) -> DoctorRound:
    batch = _get_or_404(db, CaseBatch, "batch_id", str(payload["batch_id"]))
    round_no = payload.get("round_no")
    if round_no is None:
        round_no = (
            db.scalar(select(func.max(DoctorRound.round_no)).where(DoctorRound.batch_id == batch.batch_id))
            or 0
        ) + 1
    row = DoctorRound(
        batch_id=batch.batch_id,
        round_no=int(round_no),
        due_at=payload.get("due_at"),
        generated_at=utcnow(),
    )
    db.add(row)
    db.flush()
    db.commit()
    db.refresh(row)
    return row


def create_doctor_task(db: Session, payload: Mapping[str, Any]) -> DoctorTask:
    run = _get_or_404(db, CaseRun, "run_id", str(payload["run_id"]))
    from_status = run.status
    run.status = "waiting_doctor"
    run.pause_reason = str(payload["task_type"])
    run.paused_at = utcnow()
    db.add(run)
    db.add(
        RunEvent(
            run_id=run.run_id,
            from_status=from_status,
            to_status="waiting_doctor",
            reason=str(payload["task_type"]),
            actor_id=payload.get("assigned_to"),
        )
    )
    task = DoctorTask(
        round_id=str(payload["round_id"]),
        run_id=str(payload["run_id"]),
        encounter_id=str(payload["encounter_id"]),
        attempt_id=payload.get("attempt_id"),
        question_event_id=payload.get("question_event_id"),
        state_snapshot_id=payload.get("state_snapshot_id"),
        task_type=str(payload["task_type"]),
        payload_json=payload.get("payload_json"),
        assigned_to=payload.get("assigned_to"),
    )
    db.add(task)
    db.flush()
    db.commit()
    db.refresh(task)
    return task


def generate_doctor_round_workbook(db: Session, round_id: str) -> DoctorDocument:
    round_row = _get_or_404(db, DoctorRound, "round_id", round_id)
    tasks = db.scalars(
        select(DoctorTask).where(DoctorTask.round_id == round_id).order_by(DoctorTask.created_at.asc())
    ).all()
    # W4.2: scratch-build, checksum, then upload both files before the DB row.
    temp_dir = tempfile.TemporaryDirectory(prefix=f"laya-workbook-{round_id}-")
    export_root = Path(temp_dir.name)
    csv_path = export_root / f"doctor_round_{round_row.round_no}.csv"
    jsonl_path = export_root / f"doctor_round_{round_row.round_no}.jsonl"
    fieldnames = [
        "task_id",
        "run_id",
        "encounter_id",
        "attempt_id",
        "question_event_id",
        "state_snapshot_id",
        "task_type",
        "payload_json",
        "response_json",
        "assigned_to",
        "task_version",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for task in tasks:
            writer.writerow(
                {
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "encounter_id": task.encounter_id,
                    "attempt_id": task.attempt_id or "",
                    "question_event_id": task.question_event_id or "",
                    "state_snapshot_id": task.state_snapshot_id or "",
                    "task_type": task.task_type,
                    "payload_json": canonical_json(task.payload_json or {}),
                    "response_json": canonical_json(task.response_json or {}),
                    "assigned_to": task.assigned_to or "",
                    "task_version": task.version,
                }
            )
    jsonl = "".join(
        json.dumps(
            {
                "task_id": task.task_id,
                "run_id": task.run_id,
                "task_version": task.version,
                "task_type": task.task_type,
                "payload_json": task.payload_json or {},
                "response_json": task.response_json or {},
            },
            ensure_ascii=False,
        )
        + "\n"
        for task in tasks
    )
    jsonl_path.write_text(jsonl, encoding="utf-8")
    checksum = sha256_text(csv_path.read_text(encoding="utf-8-sig"))
    storage_prefix = f"doctor_rounds/{round_id}"
    storage = get_storage()
    storage.put_bytes(f"{storage_prefix}/{csv_path.name}", csv_path.read_bytes(), "text/csv; charset=utf-8")
    storage.put_bytes(
        f"{storage_prefix}/{jsonl_path.name}",
        jsonl_path.read_bytes(),
        "application/x-ndjson",
    )
    temp_dir.cleanup()
    if settings.storage_backend == "supabase":
        storage_key = f"supabase://{settings.storage_bucket}/{storage_prefix}"
    else:
        storage_key = str(settings.export_dir / storage_prefix)
    document = DoctorDocument(
        round_id=round_id,
        format="csv+jsonl",
        storage_key=storage_key,
        checksum=checksum,
        status="generated",
        generated_at=utcnow(),
    )
    round_row.status = "exported"
    db.add(document)
    db.add(round_row)
    db.commit()
    db.refresh(document)
    return document


def import_doctor_round_responses(
    db: Session,
    round_id: str,
    responses: list[Mapping[str, Any]],
    *,
    actor_id: str | None = None,
) -> list[DoctorTask]:
    _get_or_404(db, DoctorRound, "round_id", round_id)
    updated: list[DoctorTask] = []
    for response in responses:
        task = _get_or_404(db, DoctorTask, "task_id", str(response["task_id"]))
        if task.round_id != round_id:
            raise ValueError(f"task {task.task_id} does not belong to round {round_id}")
        if int(response.get("version", 0)) != task.version:
            raise ValueError(f"task {task.task_id} version conflict")
        task.response_json = dict(response.get("response_json") or {})
        task.status = "imported"
        task.completed_at = utcnow()
        db.add(task)
        if task.task_type == "doctor_question":
            question_text = str(task.response_json.get("question_text") or "").strip()
            if not question_text:
                raise ValueError(f"task {task.task_id} has no question_text")
            question = create_question(
                db,
                task.attempt_id,
                {"question_text": question_text, "approved_by": task.assigned_to or actor_id},
            )
            queue_question(db, question.question_event_id, actor_id)
            task.question_event_id = question.question_event_id
        elif task.task_type == "doctor_record":
            record_human_candidate(db, task.attempt_id, {**task.response_json, "actor_id": actor_id})
        elif task.task_type == "doctor_adjudication":
            record_outcome(db, task.attempt_id, {**task.response_json, "rater_id": task.assigned_to or "doctor-round"})
        db.add(task)
        updated.append(task)
    run_ids = {task.run_id for task in updated}
    for run_id in run_ids:
        run = _get_or_404(db, CaseRun, "run_id", run_id)
        from_status = run.status
        run.status = "ready_to_resume"
        run.resumed_at = utcnow()
        db.add(run)
        db.add(
            RunEvent(
                run_id=run.run_id,
                from_status=from_status,
                to_status="ready_to_resume",
                reason="doctor_round_imported",
                actor_id=actor_id,
            )
        )
    db.commit()
    for task in updated:
        db.refresh(task)
    return updated


def _question_dict(question: QuestionEvent | None) -> dict[str, Any] | None:
    if question is None:
        return None
    return {
        "question_event_id": question.question_event_id,
        "attempt_id": question.attempt_id,
        "question_text": question.question_text,
        "question_source": question.question_source,
        "question_variant_id": question.question_variant_id,
        "approved_by": question.approved_by,
        "status": question.status,
        "version": question.version,
        "previous_question_event_id": question.previous_question_event_id,
        "generated_at": question.generated_at,
        "approved_at": question.approved_at,
        "queued_at": question.queued_at,
        "delivered_at": question.delivered_at,
        "viewed_at": question.viewed_at,
        "answered_at": question.answered_at,
        "expires_at": question.expires_at,
        "created_at": question.created_at,
    }


def _turn_dict(turn: TurnEvent) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "encounter_id": turn.encounter_id,
        "turn_no": turn.turn_no,
        "speaker": turn.speaker,
        "raw_text": turn.raw_text,
        "input_mode": turn.input_mode,
        "audio_object_key": turn.audio_object_key,
        "asr_confidence": turn.asr_confidence,
        "content_sha256": turn.content_sha256,
        "server_received_at": turn.server_received_at,
    }


def _candidate_dict(candidate: FactAssertionCandidate) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "clinical_fact_id": candidate.clinical_fact_id,
        "slot_key": candidate.slot_key,
        "assertion_status": candidate.assertion_status,
        "value_json": candidate.value_json,
        "value_text": candidate.value_text,
        "unit": candidate.unit,
        "polarity": candidate.polarity,
        "subject": candidate.subject,
        "temporality": candidate.temporality,
        "certainty": candidate.certainty,
        "confidence": candidate.confidence,
        "evidence_quote": candidate.evidence_quote,
        "validation_status": candidate.validation_status,
        "validation_errors_json": candidate.validation_errors_json,
        "created_at": candidate.created_at,
    }


def _outcome_dict(outcome: AttemptOutcome) -> dict[str, Any]:
    return {
        "outcome_id": outcome.outcome_id,
        "attempt_id": outcome.attempt_id,
        "rater_id": outcome.rater_id,
        "rater_role": outcome.rater_role,
        "adjudication_round": outcome.adjudication_round,
        "acquired_label": outcome.acquired_label,
        "recorded_label": outcome.recorded_label,
        "usable_label": outcome.usable_label,
        "actionable_label": outcome.actionable_label,
        "reason_code": outcome.reason_code,
        "reason_text": outcome.reason_text,
        "label_codebook_version": outcome.label_codebook_version,
        "created_at": outcome.created_at,
    }


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
