from __future__ import annotations

import json
import unittest
from pathlib import Path

from environment.patient_simulator import PatientSimulator
from environment.state import ClinicalState
from environment.state_tracker import StateTracker
from environment.target_selector import TargetSlotSelector
from experts.structured import StructuredExpert
from router.rule_router import RuleRouter


def load_case() -> dict:
    path = Path(__file__).parents[1] / "cases" / "chest_pain_cases.json"
    return json.loads(path.read_text(encoding="utf-8"))[0]


class CoreFlowTests(unittest.TestCase):
    def test_state_never_contains_hidden_facts(self) -> None:
        case = load_case()
        state = ClinicalState.create(case["chief_complaint"], case["core_slots"])
        serialized = json.dumps(state.to_dict(), ensure_ascii=False)
        self.assertNotIn("hidden_facts", serialized)
        self.assertNotIn("左上臂", serialized)

    def test_router_escalates_after_failures(self) -> None:
        state = ClinicalState.create("胸痛", ["dyspnea"], llm_budget=3, human_budget=1)
        router = RuleRouter()
        self.assertEqual(router.route(state, "dyspnea")["action"], "Structured")
        state.previous_success = False
        state.failure_count = 1
        self.assertEqual(router.route(state, "dyspnea")["action"], "LLM")
        state.failure_count = 2
        self.assertEqual(router.route(state, "dyspnea")["action"], "Human")

    def test_scripted_case_starts_structured_then_llm(self) -> None:
        case = load_case()
        state = ClinicalState.create(case["chief_complaint"], case["core_slots"])
        selector = TargetSlotSelector()
        router = RuleRouter()
        simulator = PatientSimulator(case)
        tracker = StateTracker()
        expert = StructuredExpert()

        target = selector.select(state)["target_slot"]
        self.assertEqual(target, "radiation")
        action = router.route(state, target)["action"]
        question = expert.ask(state, target)["question"]
        response = simulator.answer(question, target, action)
        self.assertFalse(response.success)
        tracker.update(
            state,
            target_slot=target,
            patient_answer=response.patient_answer,
            success=response.success,
            channel=action,
        )
        self.assertEqual(router.route(state, target)["action"], "LLM")

    def test_completeness_counts_obtained_and_denied(self) -> None:
        state = ClinicalState.create("胸痛", ["dyspnea", "sweating"])
        tracker = StateTracker()
        result = tracker.update(
            state,
            target_slot="dyspnea",
            patient_answer="没有呼吸困难",
            success=True,
            channel="Structured",
            value="无呼吸困难",
            slot_status="denied",
        )
        self.assertEqual(result.completeness, 0.5)
        self.assertEqual(state.slots["dyspnea"]["status"], "denied")

    def test_default_case_completes_through_all_three_experts(self) -> None:
        case = load_case()
        state = ClinicalState.create(
            case["chief_complaint"], case["core_slots"], llm_budget=4, human_budget=1
        )
        selector = TargetSlotSelector()
        router = RuleRouter()
        simulator = PatientSimulator(case)
        tracker = StateTracker()
        structured = StructuredExpert()
        llm_questions = {
            "radiation": "除了胸口以外，疼痛有没有串到肩膀、手臂、后背或下巴？",
            "dyspnea": "胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？",
        }
        actions: list[str] = []

        while state.turn < 12:
            target = selector.select(state)["target_slot"]
            if target is None:
                break
            action = router.route(state, target)["action"]
            if action == "Structured":
                question = structured.ask(state, target)["question"]
            elif action == "LLM":
                question = llm_questions[target]
            else:
                question = "胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？"
            response = simulator.answer(question, target, action)
            tracker.update(
                state,
                target_slot=target,
                patient_answer=response.patient_answer,
                success=response.success,
                channel=action,
                value=response.value,
                slot_status=response.slot_status,
            )
            actions.append(action)

        self.assertEqual(state.current_completeness, 1.0)
        self.assertEqual(state.turn, 10)
        self.assertIn("Structured", actions)
        self.assertIn("LLM", actions)
        self.assertIn("Human", actions)
        self.assertEqual(state.llm_budget, 2)
        self.assertEqual(state.human_budget, 0)


if __name__ == "__main__":
    unittest.main()
