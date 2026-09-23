"""Interpret answers typed by a person using the patient-facing chat UI."""

from __future__ import annotations

from environment.patient_simulator import PatientResponse
from utils.slot_utils import detect_question_slots, infer_slot_status


UNKNOWN_MARKERS = (
    "不知道",
    "不清楚",
    "没注意",
    "没留意",
    "想不起来",
    "说不好",
    "不明白",
)


class ManualPatientInterpreter:
    """Convert a manual answer into observable slot updates without hidden facts."""

    def interpret(self, question: str, patient_answer: str) -> PatientResponse:
        answer = patient_answer.strip()
        question_slots = tuple(detect_question_slots(question))
        if not answer:
            return PatientResponse(
                patient_answer="",
                success=False,
                question_slots=question_slots,
                failure_reason="empty_manual_answer",
            )
        if not question_slots:
            return PatientResponse(
                patient_answer=answer,
                success=False,
                failure_reason="question_concept_not_detected",
            )
        if any(marker in answer for marker in UNKNOWN_MARKERS):
            return PatientResponse(
                patient_answer=answer,
                success=False,
                question_slots=question_slots,
                failure_reason="manual_answer_uncertain",
            )

        status = infer_slot_status(answer)
        return PatientResponse(
            patient_answer=answer,
            success=True,
            question_slots=question_slots,
            slot_updates={
                slot: {"status": status, "value": answer}
                for slot in question_slots
            },
        )
