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
    llm_budget: int = 4
    human_budget: int = 1

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
            "llm_budget": self.llm_budget,
            "human_budget": self.human_budget,
        }
