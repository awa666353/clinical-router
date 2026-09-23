"""Frozen rule baseline, replaceable later by a learned Qwen Router."""

from __future__ import annotations

from typing import Any

from environment.state import ClinicalState


class RuleRouter:
    name = "rule_router_v1"

    def __init__(self, max_turns: int = 12) -> None:
        self.max_turns = max_turns

    def route(self, state: ClinicalState) -> dict[str, Any]:
        if state.is_terminated:
            return {
                "action": "STOP",
                "stop": True,
                "is_routed": False,
                "reason": state.termination_reason or "问诊已经结束",
            }
        if not state.unresolved_slots():
            return {
                "action": "STOP",
                "stop": True,
                "is_routed": False,
                "reason": "核心病史信息已经完整",
            }
        if state.turn >= self.max_turns:
            return {
                "action": "STOP",
                "stop": True,
                "is_routed": False,
                "reason": "达到最大问诊轮数",
            }

        if state.failure_count >= 2 and state.human_budget > 0:
            action, reason = "Human", "连续失败达到 2 次，升级真人"
        elif state.previous_success is False and state.llm_budget > 0:
            action, reason = "LLM", "上一通道未获得新信息，升级 LLM"
        elif state.failure_count >= 2 and state.llm_budget > 0:
            action, reason = "LLM", "真人预算耗尽，回退至 LLM"
        else:
            action, reason = "Structured", "使用低成本结构化问询"

        return {
            "action": action,
            "stop": False,
            "is_routed": state.previous_channel is not None
            and action != state.previous_channel,
            "reason": reason,
        }
