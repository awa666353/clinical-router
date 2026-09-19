"""Human expert adapter. Streamlit owns the actual dialog rendering."""

from __future__ import annotations

from typing import Any

from environment.state import ClinicalState
from experts.base import Expert


class HumanExpert(Expert):
    name = "Human"

    def ask(
        self, state: ClinicalState, target_slot: str, **kwargs: Any
    ) -> dict[str, Any]:
        del state, target_slot
        question = str(kwargs.get("question", "")).strip()
        if not question:
            return {"question": "", "requires_human_input": True, "provider": "human"}
        return {"question": question, "requires_human_input": False, "provider": "human"}
