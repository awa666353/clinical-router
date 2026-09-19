"""Frozen target-slot policy used by every routing strategy."""

from __future__ import annotations

from environment.state import ClinicalState


DEFAULT_PRIORITY = [
    "radiation",
    "duration",
    "dyspnea",
    "sweating",
    "onset",
    "location",
    "character",
]


class TargetSlotSelector:
    def __init__(self, priority: list[str] | None = None) -> None:
        self.priority = priority or DEFAULT_PRIORITY

    def select(self, state: ClinicalState) -> dict[str, str | None]:
        ordered = [slot for slot in self.priority if slot in state.slots]
        ordered.extend(slot for slot in state.slots if slot not in ordered)
        target = next(
            (slot for slot in ordered if state.slots[slot]["status"] == "unknown"),
            None,
        )
        return {"target_slot": target}
