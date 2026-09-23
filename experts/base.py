"""Shared questioning-expert contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from environment.state import ClinicalState


class Expert(ABC):
    name: str

    @abstractmethod
    def ask(self, state: ClinicalState, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError
