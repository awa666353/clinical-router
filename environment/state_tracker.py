"""Deterministic state updates and CE-PCIC calculation."""

from __future__ import annotations

from dataclasses import dataclass

from environment.state import ClinicalState


@dataclass(frozen=True)
class UpdateResult:
    information_gain: float
    completeness: float


class StateTracker:
    def update(
        self,
        state: ClinicalState,
        *,
        target_slot: str,
        patient_answer: str,
        success: bool,
        channel: str,
        value: str | None = None,
        slot_status: str | None = None,
    ) -> UpdateResult:
        before = state.current_completeness

        if success:
            state.slots[target_slot] = {
                "status": slot_status or "obtained",
                "value": value if value is not None else patient_answer,
            }
            state.failure_count = 0
        else:
            state.failure_count += 1

        state.turn += 1
        state.last_patient_answer = patient_answer
        state.previous_channel = channel
        state.previous_success = success

        if channel == "LLM":
            state.llm_budget = max(0, state.llm_budget - 1)
        elif channel == "Human":
            state.human_budget = max(0, state.human_budget - 1)

        after = state.recompute_completeness()
        return UpdateResult(information_gain=after - before, completeness=after)
