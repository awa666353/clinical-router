"""Frozen score module for completeness, interaction length, and channel cost."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import Settings
from environment.state import ClinicalState


@dataclass(frozen=True)
class ScoreResult:
    metrics: dict[str, Any]
    score_delta: float


class ClinicalScorer:
    """Compute a versionable potential score without reading hidden case facts."""

    version = "clinical_score_v1"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.channel_costs = {
            "Structured": settings.structured_cost,
            "LLM": settings.llm_cost,
            "Human": settings.human_cost,
        }

    def initialise(self, state: ClinicalState) -> ScoreResult:
        metrics = self._metrics(state)
        state.current_score = metrics["integrated_score"]
        state.score_history = [metrics]
        return ScoreResult(metrics=metrics, score_delta=0.0)

    def update_after_turn(self, state: ClinicalState, channel: str) -> ScoreResult:
        previous_score = state.current_score
        state.cumulative_cost += self.channel_costs[channel]
        metrics = self._metrics(state)
        state.current_score = metrics["integrated_score"]
        state.score_history.append(metrics)
        return ScoreResult(
            metrics=metrics,
            score_delta=metrics["integrated_score"] - previous_score,
        )

    def _metrics(self, state: ClinicalState) -> dict[str, Any]:
        score = (
            self.settings.completeness_weight * state.current_completeness
            - self.settings.turn_penalty * state.turn
            - self.settings.cost_weight * state.cumulative_cost
        )
        return {
            "scorer_version": self.version,
            "turn": state.turn,
            "completeness": state.current_completeness,
            "communication_rounds": state.turn,
            "cumulative_cost": round(state.cumulative_cost, 6),
            "integrated_score": round(score, 6),
            "weights": {
                "completeness": self.settings.completeness_weight,
                "turn_penalty": self.settings.turn_penalty,
                "cost": self.settings.cost_weight,
            },
        }
