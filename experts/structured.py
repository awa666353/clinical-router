"""Frozen, auditable structured question bank."""

from __future__ import annotations

from typing import Any

from environment.state import ClinicalState
from experts.base import Expert


QUESTION_BANK = {
    "onset": "这次胸痛是什么时候开始的，是突然出现还是慢慢出现的？",
    "location": "胸痛最明显的位置在哪里？",
    "character": "这种胸痛是什么样的感觉，比如压榨、闷痛还是针刺样？",
    "radiation": "胸痛有没有向左肩、左臂、背部或者其他地方放射？",
    "duration": "每次胸痛持续多久？",
    "dyspnea": "胸痛的时候有没有感觉呼吸困难？",
    "sweating": "胸痛发作时有没有出汗或冒冷汗？",
}


class StructuredExpert(Expert):
    name = "Structured"

    def ask(
        self, state: ClinicalState, target_slot: str, **kwargs: Any
    ) -> dict[str, Any]:
        del state, kwargs
        question = QUESTION_BANK.get(target_slot)
        if not question:
            raise KeyError(f"Structured question is not configured for slot: {target_slot}")
        return {"question": question, "provider": "structured_question_bank"}
