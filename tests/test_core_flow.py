from __future__ import annotations

import json
import unittest
from pathlib import Path

from config import Settings
from environment.manual_patient import ManualPatientInterpreter
from environment.patient_simulator import PatientSimulator
from environment.scorer import ClinicalScorer
from environment.state import ClinicalState
from environment.state_tracker import StateTracker
from experts.deepseek_llm import DeepSeekLLMExpert
from experts.structured import StructuredExpert
from router.rule_router import RuleRouter
from utils.slot_utils import detect_question_slots


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

    def test_router_escalates_and_can_stop(self) -> None:
        state = ClinicalState.create("胸痛", ["dyspnea"], llm_budget=3, human_budget=1)
        router = RuleRouter(max_turns=12)
        self.assertEqual(router.route(state)["action"], "Structured")
        state.previous_channel = "Structured"
        state.previous_success = False
        state.failure_count = 1
        self.assertEqual(router.route(state)["action"], "LLM")
        state.previous_channel = "LLM"
        state.failure_count = 2
        self.assertEqual(router.route(state)["action"], "Human")
        state.slots["dyspnea"] = {"status": "obtained", "value": "伴呼吸困难"}
        state.recompute_completeness()
        self.assertEqual(router.route(state)["action"], "STOP")

    def test_structured_expert_selects_internally(self) -> None:
        case = load_case()
        state = ClinicalState.create(case["chief_complaint"], case["core_slots"])
        result = StructuredExpert().ask(state)
        self.assertIn("放射", result["question"])
        self.assertNotIn("target_slot", result)

    def test_patient_simulator_detects_question_concepts(self) -> None:
        case = load_case()
        simulator = PatientSimulator(case)
        question = "胸痛的时候有没有感觉呼吸困难或者冒冷汗？"
        self.assertEqual(detect_question_slots(question), ["dyspnea", "sweating"])
        response = simulator.answer(question, "Human")
        self.assertTrue(response.success)
        self.assertEqual(set(response.slot_updates or {}), {"dyspnea", "sweating"})

    def test_manual_patient_answer_uses_typed_text_not_hidden_facts(self) -> None:
        interpreter = ManualPatientInterpreter()
        response = interpreter.interpret(
            "疼痛会放射到肩膀、手臂或后背吗？", "会，主要放射到左肩。"
        )
        self.assertTrue(response.success)
        self.assertEqual(response.question_slots, ("radiation",))
        self.assertEqual(
            response.slot_updates,
            {"radiation": {"status": "obtained", "value": "会，主要放射到左肩。"}},
        )

        uncertain = interpreter.interpret("疼痛会放射到其他地方吗？", "我不清楚。")
        self.assertFalse(uncertain.success)
        self.assertIsNone(uncertain.slot_updates)
        self.assertEqual(uncertain.failure_reason, "manual_answer_uncertain")

    def test_score_delta_combines_completeness_turns_and_cost(self) -> None:
        state = ClinicalState.create("胸痛", ["dyspnea", "sweating"])
        settings = Settings(
            deepseek_api_key="",
            completeness_weight=1.0,
            turn_penalty=0.02,
            cost_weight=0.05,
            structured_cost=0.0,
            llm_cost=0.2,
            human_cost=1.0,
        )
        scorer = ClinicalScorer(settings)
        scorer.initialise(state)
        tracker = StateTracker()
        update = tracker.update(
            state,
            question="胸痛时有没有呼吸困难？",
            patient_answer="是的，伴呼吸困难。",
            channel="Structured",
            response_success=True,
            question_slots=("dyspnea",),
            slot_updates={
                "dyspnea": {"status": "obtained", "value": "伴呼吸困难"}
            },
        )
        result = scorer.update_after_turn(state, "Structured")
        self.assertEqual(update.completeness, 0.5)
        self.assertAlmostEqual(result.score_delta, 0.48)
        self.assertEqual(state.score_history[-1]["communication_rounds"], 1)

    def test_default_case_completes_with_open_dialogue_and_all_experts(self) -> None:
        case = load_case()
        settings = Settings(deepseek_api_key="")
        state = ClinicalState.create(
            case["chief_complaint"], case["core_slots"], llm_budget=4, human_budget=1
        )
        router = RuleRouter(max_turns=12)
        simulator = PatientSimulator(case)
        tracker = StateTracker()
        scorer = ClinicalScorer(settings)
        scorer.initialise(state)
        experts = {
            "Structured": StructuredExpert(),
            "LLM": DeepSeekLLMExpert(settings),
        }
        actions: list[str] = []

        while state.turn < 12:
            decision = router.route(state)
            action = decision["action"]
            if action == "STOP":
                break
            if action == "Human":
                question = "胸痛时有没有觉得气不够用、喘不上来或必须停下来呼吸？"
            else:
                question = experts[action].ask(state)["question"]
            response = simulator.answer(question, action)
            tracker.update(
                state,
                question=question,
                patient_answer=response.patient_answer,
                channel=action,
                response_success=response.success,
                question_slots=response.question_slots,
                slot_updates=response.slot_updates,
            )
            scorer.update_after_turn(state, action)
            actions.append(action)

        self.assertEqual(state.current_completeness, 1.0)
        self.assertEqual(state.turn, 10)
        self.assertIn("Structured", actions)
        self.assertIn("LLM", actions)
        self.assertIn("Human", actions)
        self.assertEqual(router.route(state)["action"], "STOP")
        self.assertEqual(len(state.dialogue_history), 10)
        self.assertEqual(len(state.score_history), 11)


if __name__ == "__main__":
    unittest.main()
