"""Frozen rule baseline, replaceable later by a learned Qwen Router."""

from __future__ import annotations

from typing import Any

from environment.state import ClinicalState


class RuleRouter:
    name = "rule_router_v1"

    def route(self, state: ClinicalState, target_slot: str) -> dict[str, Any]:
        del target_slot  # retained in the interface for future learned policies

        if state.failure_count >= 2 and state.human_budget > 0:
            return {"action": "Human", "reason": "连续失败达到 2 次，升级真人"}
        if state.previous_success is False and state.llm_budget > 0:
            return {"action": "LLM", "reason": "上一通道未获得目标信息，升级 LLM"}
        if state.failure_count >= 2 and state.llm_budget > 0:
            return {"action": "LLM", "reason": "真人预算耗尽，回退至 LLM"}
        return {"action": "Structured", "reason": "使用低成本结构化问询"}
