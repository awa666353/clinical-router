"""Deterministic state updates and CE-PCIC calculation."""

from __future__ import annotations

from dataclasses import dataclass

from environment.state import ClinicalState


@dataclass(frozen=True)
class UpdateResult:
    information_gain: float
    completeness: float
    success: bool
    question_slots: tuple[str, ...]
    newly_obtained_slots: tuple[str, ...]


class StateTracker:
    def update(
        self,
        state: ClinicalState,
        *,
        question: str,
        patient_answer: str,
        channel: str,
        response_success: bool,
        question_slots: tuple[str, ...] = (),
        slot_updates: dict[str, dict[str, str]] | None = None,
    ) -> UpdateResult:
        before = state.current_completeness
        newly_obtained: list[str] = []

        for slot, update in (slot_updates or {}).items():
            if slot not in state.slots:
                continue
            if state.slots[slot]["status"] == "unknown":
                newly_obtained.append(slot)
            state.slots[slot] = {
                "status": update.get("status", "obtained"),
                "value": update.get("value", patient_answer),
            }

        success = response_success and bool(newly_obtained)
        if success:
            state.failure_count = 0
        else:
            state.failure_count += 1

        state.turn += 1
        state.last_patient_answer = patient_answer
        state.previous_channel = channel
        state.previous_success = success
        state.channel_history.append(channel)
        state.dialogue_history.append(
            {
                "turn": state.turn,
                "channel": channel,
                "question": question,
                "patient_answer": patient_answer,
                "question_slots": list(question_slots),
                "newly_obtained_slots": list(newly_obtained),
                "success": success,
            }
        )

        if channel == "LLM":
            state.llm_budget = max(0, state.llm_budget - 1)
        elif channel == "Human":
            state.human_budget = max(0, state.human_budget - 1)

        after = state.recompute_completeness()
        return UpdateResult(
            information_gain=after - before,
            completeness=after,
            success=success,
            question_slots=question_slots,
            newly_obtained_slots=tuple(newly_obtained),
        )
