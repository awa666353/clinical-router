"""Router-visible state for sequential clinical history taking."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


COUNTED_STATUSES = {"obtained", "denied"}


@dataclass
class ClinicalState:
    chief_complaint: str
    slots: dict[str, dict[str, Any]]
    last_patient_answer: str = ""
    previous_channel: str | None = None
    previous_success: bool | None = None
    failure_count: int = 0
    turn: int = 0
    current_completeness: float = 0.0
    cumulative_cost: float = 0.0
    current_score: float = 0.0
    llm_budget: int = 4
    human_budget: int = 1
    dialogue_history: list[dict[str, Any]] = field(default_factory=list)
    channel_history: list[str] = field(default_factory=list)
    score_history: list[dict[str, Any]] = field(default_factory=list)
    is_terminated: bool = False
    termination_reason: str | None = None

    @classmethod
    def create(
        cls,
        chief_complaint: str,
        core_slots: list[str],
        llm_budget: int = 4,
        human_budget: int = 1,
    ) -> "ClinicalState":
        return cls(
            chief_complaint=chief_complaint,
            slots={slot: {"status": "unknown", "value": None} for slot in core_slots},
            llm_budget=llm_budget,
            human_budget=human_budget,
        )

    def known_information(self) -> dict[str, dict[str, Any]]:
        return {
            slot: deepcopy(payload)
            for slot, payload in self.slots.items()
            if payload["status"] in COUNTED_STATUSES
        }

    def unresolved_slots(self) -> list[str]:
        return [
            slot
            for slot, payload in self.slots.items()
            if payload["status"] == "unknown"
        ]

    def recompute_completeness(self) -> float:
        if not self.slots:
            self.current_completeness = 1.0
        else:
            known_count = sum(
                payload["status"] in COUNTED_STATUSES for payload in self.slots.values()
            )
            self.current_completeness = known_count / len(self.slots)
        return self.current_completeness

    def to_dict(self) -> dict[str, Any]:
        """Return a safe Router/logging view. Hidden case facts never enter this object."""
        return {
            "chief_complaint": self.chief_complaint,
            "slots": deepcopy(self.slots),
            "last_patient_answer": self.last_patient_answer,
            "previous_channel": self.previous_channel,
            "previous_success": self.previous_success,
            "failure_count": self.failure_count,
            "turn": self.turn,
            "current_completeness": self.current_completeness,
            "cumulative_cost": self.cumulative_cost,
            "current_score": self.current_score,
            "llm_budget": self.llm_budget,
            "human_budget": self.human_budget,
            "dialogue_history": deepcopy(self.dialogue_history),
            "channel_history": list(self.channel_history),
            "score_history": deepcopy(self.score_history),
            "is_terminated": self.is_terminated,
            "termination_reason": self.termination_reason,
        }
