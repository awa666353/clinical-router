"""Fact-constrained deterministic patient simulator.

The complete case dictionary is intentionally kept private to this component. The
Router and Experts receive only ``ClinicalState`` and never see hidden facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.slot_utils import SLOT_PRIORITY, detect_question_slots, infer_slot_status


@dataclass(frozen=True)
class PatientResponse:
    patient_answer: str
    success: bool
    question_slots: tuple[str, ...] = ()
    slot_updates: dict[str, dict[str, str]] | None = None
    failure_reason: str | None = None


class PatientSimulator:
    def __init__(self, case: dict[str, Any]) -> None:
        self._hidden_facts = dict(case["hidden_facts"])
        self._failure_script = (
            case.get("simulator_behavior", {}).get("scripted_failures", {})
        )
        self._attempts: dict[tuple[str, str], int] = {}

    def answer(self, question: str, channel: str) -> PatientResponse:
        question_slots = detect_question_slots(question)
        if not question_slots:
            return PatientResponse(
                patient_answer="我不太明白您想问哪一方面，能换一种问法吗？",
                success=False,
                failure_reason="question_concept_not_detected",
            )

        primary_slot = next(
            slot for slot in SLOT_PRIORITY if slot in question_slots
        )
        attempt_key = (primary_slot, channel)
        attempt = self._attempts.get(attempt_key, 0) + 1
        self._attempts[attempt_key] = attempt

        scripted_failures = int(
            self._failure_script.get(primary_slot, {}).get(channel, 0)
        )
        if attempt <= scripted_failures:
            return PatientResponse(
                patient_answer=self._failure_answer(primary_slot, attempt),
                success=False,
                question_slots=tuple(question_slots),
                failure_reason="scripted_ambiguity",
            )

        slot_updates: dict[str, dict[str, str]] = {}
        answers: list[str] = []
        for slot in question_slots:
            fact = self._hidden_facts.get(slot)
            if fact is None or str(fact).strip() == "":
                continue
            value = str(fact)
            slot_updates[slot] = {
                "status": infer_slot_status(value),
                "value": value,
            }
            answers.append(self._natural_answer(slot, value))

        if not slot_updates:
            return PatientResponse(
                patient_answer="这个我不清楚，也想不起来了。",
                success=False,
                question_slots=tuple(question_slots),
                failure_reason="fact_unavailable",
            )

        return PatientResponse(
            patient_answer=" ".join(answers),
            success=True,
            question_slots=tuple(question_slots),
            slot_updates=slot_updates,
        )

    @staticmethod
    def _failure_answer(slot: str, attempt: int) -> str:
        messages = {
            "radiation": "您说的放射是什么意思？我不太会描述。",
            "duration": "当时挺紧张的，我没算过具体时间。",
            "dyspnea": "我只觉得很不舒服，不知道算不算呼吸困难。",
            "sweating": "我没留意是不是因为紧张才出汗。",
        }
        if attempt > 1:
            return "还是不太明白，能不能再具体一点问我？"
        return messages.get(slot, "我不太明白您的问题，能再说具体一点吗？")

    @staticmethod
    def _natural_answer(slot: str, value: str) -> str:
        templates = {
            "onset": "大概是{value}。",
            "location": "主要在{value}。",
            "character": "感觉是{value}。",
            "radiation": "会，疼痛{value}。" if not value.startswith("无") else "没有，{value}。",
            "duration": "每次大约{value}。",
            "dyspnea": "是的，{value}。" if not value.startswith("无") else "没有，{value}。",
            "sweating": "是的，{value}。" if not value.startswith("无") else "没有，{value}。",
        }
        return templates.get(slot, "{value}。").format(value=value)
