"""Role-play entry points must reuse the normal attempt and answer records."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import app
from backend.config import settings
from backend.db import get_db
from backend.llm import LLMResult
from backend.game import score_game_event
from backend.models import Base, GameScoreEvent, LLMCall, PatientQuestionLink, QuestionJob
from backend.services import sha256_text


class GameFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.tmp.name) / 'game.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

        def override_db():
            with self.sessions() as db:
                yield db

        app.dependency_overrides[get_db] = override_db
        self.config = mock.patch.dict(settings.__dict__, {
            "app_env": "development", "auth_mode": "disabled", "llm_api_key": "",
        })
        self.config.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.config.stop()
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        self.tmp.cleanup()

    def _send_human_question(self, encounter_id):
        doctor_view = self.client.get(f"/api/encounters/{encounter_id}/doctor-view").json()
        fact_id = doctor_view["facts"][0]["clinical_fact_id"]
        attempt_response = self.client.post(
            f"/api/encounters/{encounter_id}/attempts",
            json={"target_fact_id": fact_id, "executed_channel": "Human", "override_reason": "test"},
        )
        self.assertEqual(attempt_response.status_code, 200, attempt_response.text)
        attempt = attempt_response.json()
        self.assertEqual(attempt["interaction_mode"], "semi_async_human")
        question_response = self.client.post(
            f"/api/attempts/{attempt['attempt_id']}/question/plan",
            json={"question_text": "请描述一下现在的胸痛。"},
        )
        self.assertEqual(question_response.status_code, 200, question_response.text)
        question = question_response.json()
        self.assertEqual(self.client.post(f"/api/questions/{question['question_event_id']}/approve").status_code, 200)
        self.assertEqual(self.client.post(f"/api/questions/{question['question_event_id']}/queue").status_code, 200)
        return attempt, question

    def test_human_patient_receives_private_card_and_answer_is_recorded(self):
        created = self.client.post("/api/game/sessions", json={"patient_mode": "human"})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        encounter_id = session["encounter_id"]
        token = session["patient_token"]
        self.assertTrue(token)
        regenerated = self.client.post(f"/api/encounters/{encounter_id}/patient-session-link")
        self.assertEqual(regenerated.status_code, 200, regenerated.text)
        with self.sessions() as db:
            new_link = db.scalar(select(PatientQuestionLink).where(
                PatientQuestionLink.token_hash == sha256_text(regenerated.json()["token"])
            ))
            self.assertEqual(new_link.max_uses, 200_000)
        doctor_summary = self.client.get(f"/api/game/encounters/{encounter_id}").json()
        self.assertNotIn("answers", doctor_summary)
        doctor_view = self.client.get(f"/api/encounters/{encounter_id}/doctor-view").json()
        self.assertNotIn("persona", str(doctor_view))
        brief = self.client.get(f"/api/patient/session/{token}/brief")
        self.assertEqual(brief.status_code, 200, brief.text)
        self.assertTrue(brief.json()["answers"])
        attempt, question = self._send_human_question(encounter_id)
        pending = self.client.get(f"/api/patient/session/{token}").json()
        self.assertEqual(len(pending["questions"]), 1)
        self.assertEqual(pending["history"], [])
        answer = self.client.post(
            f"/api/patient/session/{token}/questions/{question['question_event_id']}/answer",
            json={"answer_text": "胸口闷闷的。"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        after = self.client.get(f"/api/patient/session/{token}").json()
        self.assertEqual(after["questions"], [])
        self.assertEqual(after["history"][0]["answer_text"], "胸口闷闷的。")
        detail = self.client.get(f"/api/attempts/{attempt['attempt_id']}").json()
        self.assertEqual(detail["turns"][-1]["raw_text"], "胸口闷闷的。")

    def test_ai_patient_uses_script_when_key_absent_and_keeps_one_answer(self):
        created = self.client.post("/api/game/sessions", json={"patient_mode": "ai"})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        self.assertIsNone(session["patient_token"])
        attempt, _ = self._send_human_question(session["encounter_id"])
        url = f"/api/game/encounters/{session['encounter_id']}/ai-reply"
        first = self.client.post(url, json={"attempt_id": attempt["attempt_id"]})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["used_fallback"])
        self.assertTrue(first.json()["answer_text"])
        second = self.client.post(url, json={"attempt_id": attempt["attempt_id"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["answer_text"], first.json()["answer_text"])
        detail = self.client.get(f"/api/attempts/{attempt['attempt_id']}").json()
        self.assertEqual(detail["turns"][-1]["raw_text"], first.json()["answer_text"])

    def test_ai_patient_model_answer_is_recorded_with_call_metadata(self):
        created = self.client.post("/api/game/sessions", json={"patient_mode": "ai"})
        self.assertEqual(created.status_code, 200, created.text)
        encounter_id = created.json()["encounter_id"]
        attempt, _ = self._send_human_question(encounter_id)
        result = LLMResult(
            payload={"answer_text": "胸口发闷，走快时更明显。"},
            model="test-model",
            prompt_version="roleplay-patient-v1",
            request_hash="test-request-hash",
            latency_ms=12,
            request_text="test request",
            response_text='{"answer_text":"胸口发闷，走快时更明显。"}',
        )
        with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}), mock.patch(
            "backend.game.LLMClient._chat_json", return_value=result
        ) as chat:
            reply = self.client.post(
                f"/api/game/encounters/{encounter_id}/ai-reply",
                json={"attempt_id": attempt["attempt_id"]},
            )
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertFalse(reply.json()["used_fallback"])
        self.assertEqual(reply.json()["answer_text"], result.payload["answer_text"])
        chat.assert_called_once()
        with self.sessions() as db:
            calls = db.scalars(
                select(LLMCall).where(
                    LLMCall.attempt_id == attempt["attempt_id"],
                    LLMCall.purpose == "patient_simulation",
                )
            ).all()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].model, "test-model")

    def test_free_question_scores_answer_once_and_keeps_collection_records(self):
        created = self.client.post("/api/game/sessions", json={"patient_mode": "human"})
        self.assertEqual(created.status_code, 200, created.text)
        encounter_id = created.json()["encounter_id"]
        token = created.json()["patient_token"]
        ask_url = f"/api/game/encounters/{encounter_id}/ask"
        first = self.client.post(ask_url, json={"question_text": "胸痛持续多久了？"})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.client.post(ask_url, json={"question_text": "哪里痛？"}).status_code, 400)
        state = self.client.get(f"/api/game/encounters/{encounter_id}/state").json()
        self.assertTrue(state["waiting_for_patient"])
        self.assertEqual(state["score"], 0)
        self.assertNotIn("facts", state)
        question = self.client.get(f"/api/patient/session/{token}").json()["questions"][0]
        self.assertEqual(question["question_text"], "胸痛持续多久了？")
        answer = self.client.post(
            f"/api/patient/session/{token}/questions/{question['question_event_id']}/answer",
            json={"answer_text": "大约二十分钟。"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        with self.sessions() as db:
            score_row = db.scalar(select(GameScoreEvent).where(GameScoreEvent.encounter_id == encounter_id))
            jobs = db.scalars(select(QuestionJob).where(QuestionJob.attempt_id == first.json()["attempt_id"])).all()
            self.assertEqual(score_row.status, "queued")
            self.assertTrue(any(job.job_type == "extract_answer" for job in jobs))
            score_game_event(db, score_row.score_id)
            self.assertEqual(score_row.status, "unavailable")
            self.assertEqual(score_row.note_status, "unavailable")
            result = LLMResult(
                payload={"matched_fact_ids": ["pain_duration"]},
                model="test-model",
                prompt_version="roleplay-score-v1",
                request_hash="test-score-hash",
                latency_ms=9,
                request_text="test score request",
                response_text='{"matched_fact_ids":["pain_duration"]}',
            )
            note_result = LLMResult(
                payload={"note_text": "主诉：胸痛。\n现病史：患者称胸痛持续约二十分钟。"},
                model="test-model", prompt_version="roleplay-note-v1",
                request_hash="test-note-hash", latency_ms=8,
                request_text="test note request", response_text='{"note_text":"病历"}',
            )
            note_prompts = []

            def game_model(*, messages, model, prompt_version):
                if prompt_version == "roleplay-note-v1":
                    note_prompts.append(messages)
                    return note_result
                return result

            with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}), mock.patch(
                "backend.game.LLMClient._chat_json", side_effect=game_model
            ):
                score_game_event(db, score_row.score_id)
        state = self.client.get(f"/api/game/encounters/{encounter_id}/state").json()
        self.assertEqual(state["score"], 10)
        self.assertFalse(state["waiting_for_patient"])
        self.assertEqual(len(state["turns"]), 2)
        self.assertEqual(state["rounds"][0]["points_earned"], 10)
        self.assertIn("现病史", state["rounds"][0]["note_text"])
        self.assertEqual(len(note_prompts), 1)
        private_brief = self.client.get(f"/api/patient/session/{token}/brief").json()
        self.assertNotIn(private_brief["answers"]["pain_radiation"], str(note_prompts[0]))

        second = self.client.post(ask_url, json={"question_text": "再说一下持续多久？"})
        self.assertEqual(second.status_code, 200, second.text)
        question = self.client.get(f"/api/patient/session/{token}").json()["questions"][0]
        self.client.post(
            f"/api/patient/session/{token}/questions/{question['question_event_id']}/answer",
            json={"answer_text": "还是二十分钟左右。"},
        )
        with self.sessions() as db:
            second_score = db.scalar(select(GameScoreEvent).where(
                GameScoreEvent.attempt_id == second.json()["attempt_id"]
            ))
            with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}), mock.patch(
                "backend.game.LLMClient._chat_json", side_effect=game_model
            ):
                score_game_event(db, second_score.score_id)
        final_state = self.client.get(f"/api/game/encounters/{encounter_id}/state").json()
        self.assertEqual(final_state["score"], 10)
        self.assertEqual(final_state["rounds"][1]["points_earned"], 0)
        self.assertEqual(len(note_prompts), 2)

    def test_ai_doctor_questions_human_patient_without_reading_private_card(self):
        with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}):
            created = self.client.post("/api/game/sessions", json={"patient_mode": "human", "doctor_mode": "ai"})
        self.assertEqual(created.status_code, 200, created.text)
        session = created.json()
        encounter_id, token = session["encounter_id"], session["patient_token"]
        brief = self.client.get(f"/api/patient/session/{token}/brief").json()
        self.assertEqual(brief["doctor_mode"], "ai")
        self.assertTrue(brief["answers"])
        self.assertEqual(self.client.post(f"/api/game/encounters/{encounter_id}/ask", json={"question_text": "哪里疼？"}).status_code, 400)

        prompts = []
        questions = iter(("胸口是从什么时候开始不舒服的？", "胸口具体什么感觉？"))

        def model_reply(*, messages, model, prompt_version):
            prompts.append((prompt_version, messages))
            payload = {"question_text": next(questions)} if prompt_version == "roleplay-doctor-v1" else {"target_fact_id": "pain_onset_pattern"}
            return LLMResult(
                payload=payload, model="test-model", prompt_version=prompt_version,
                request_hash=f"test-{len(prompts)}", latency_ms=4,
                request_text="request", response_text="response",
            )

        next_url = f"/api/patient/session/{token}/ai-doctor/next"
        with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}), mock.patch(
            "backend.game.LLMClient._chat_json", side_effect=model_reply
        ):
            first = self.client.post(next_url)
            repeat = self.client.post(next_url)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "question_ready")
        self.assertEqual(repeat.json()["status"], "waiting_for_answer")
        self.assertEqual(len([p for p, _ in prompts if p == "roleplay-doctor-v1"]), 1)
        first_prompt = next(messages for purpose, messages in prompts if purpose == "roleplay-doctor-v1")
        self.assertNotIn(brief["persona"], str(first_prompt))
        self.assertNotIn(next(iter(brief["answers"].values())), str(first_prompt))
        pending = self.client.get(f"/api/patient/session/{token}").json()["questions"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["question_text"], "胸口是从什么时候开始不舒服的？")
        answer = self.client.post(
            f"/api/patient/session/{token}/questions/{pending[0]['question_event_id']}/answer",
            json={"answer_text": "今天早上开始的。"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}), mock.patch(
            "backend.game.LLMClient._chat_json", side_effect=model_reply
        ):
            second = self.client.post(next_url)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.client.get(f"/api/patient/session/{token}").json()["questions"][0]["question_text"], "胸口具体什么感觉？")
        with self.sessions() as db:
            calls = db.scalars(select(LLMCall).where(LLMCall.purpose == "doctor_simulation")).all()
            score_rows = db.scalars(select(GameScoreEvent).where(GameScoreEvent.encounter_id == encounter_id)).all()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(score_rows), 1)

    def test_ai_doctor_requires_key_and_valid_patient_link(self):
        no_key = self.client.post("/api/game/sessions", json={"patient_mode": "human", "doctor_mode": "ai"})
        self.assertEqual(no_key.status_code, 400)
        with mock.patch.dict(settings.__dict__, {"llm_api_key": "test-key"}):
            created = self.client.post("/api/game/sessions", json={"patient_mode": "human", "doctor_mode": "ai"})
            invalid = self.client.post("/api/patient/session/invalid/ai-doctor/next")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(invalid.status_code, 400)

    def test_game_creation_is_unavailable_outside_local_demo(self):
        with mock.patch.dict(settings.__dict__, {"app_env": "production"}):
            response = self.client.post("/api/game/sessions", json={"patient_mode": "human"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
