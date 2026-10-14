#!/usr/bin/env python3
"""Run an end-to-end API smoke test against a temporary SQLite database."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="laya-web-smoke-"))
    # Defaults target SQLite.  Preset DATABASE_URL/EXPORT_DIR (for example to a
    # migrated local Postgres, guide 9.1) are respected via setdefault.
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{tmp / 'pilot.sqlite3'}")
    os.environ.setdefault("EXPORT_DIR", str(tmp / "exports"))
    os.environ.setdefault("AUTO_SEED_DEMO", "1")
    os.environ.setdefault("ADJUDICATION_REVIEWS_REQUIRED", "2")
    os.environ.setdefault("REQUIRE_CONSENSUS_FOR_EXPORT", "1")
    os.environ.setdefault("AUTH_MODE", "gateway")
    os.environ.setdefault("AUTH_GATEWAY_SECRET", "smoke-secret")
    os.environ.setdefault("CORS_ORIGINS", "http://testserver")
    from fastapi.testclient import TestClient
    from backend.app import app

    with TestClient(
        app,
        headers={
            "X-User-Id": "smoke-user",
            "X-User-Role": "admin",
            "X-Gateway-Secret": "smoke-secret",
        },
    ) as client:
        assert client.get("/api/health").status_code == 200
        encounter = client.get("/api/encounters/latest").json()["encounter_id"]
        view = client.get(f"/api/encounters/{encounter}/doctor-view").json()
        fact_id = view["facts"][0]["clinical_fact_id"]

        demo_case_id = client.get(f"/api/encounters/{encounter}").json()["case_id"]
        case2 = client.post(
            "/api/cases",
            json={
                "case_code": "DEMO-CHEST-PAIN-002",
                "source_type": "synthetic",
                "topic": "chest_pain",
                "split_role": "train",
                "patient_group_id": "smoke-patient-2",
                "collection_time_bucket": "smoke",
                "site_id": "local-demo",
            },
        )
        assert case2.status_code == 200, case2.text
        patient_actor = client.post(
            "/api/patient-actors",
            json={
                "display_name": "Smoke Actor",
                "actor_type": "llm_simulator",
                "max_parallel_runs": 2,
            },
        )
        assert patient_actor.status_code == 200, patient_actor.text
        actor_id = patient_actor.json()["actor_id"]
        batch = client.post(
            "/api/case-batches",
            json={
                "name": "Smoke Batch",
                "case_ids": [demo_case_id, case2.json()["case_id"]],
                "patient_actor_id": actor_id,
                "interviewer_id": "smoke-doctor",
            },
        )
        assert batch.status_code == 200, batch.text
        runs = client.get(f"/api/patient-actors/{actor_id}/runs").json()
        assert len(runs) == 2, runs
        assert all(run["status"] == "running" for run in runs), runs
        round_row = client.post(
            "/api/doctor-rounds",
            json={"batch_id": batch.json()["batch_id"]},
        )
        assert round_row.status_code == 200, round_row.text
        human_attempt = client.post(
            f"/api/encounters/{runs[0]['encounter_id']}/attempts",
            json={
                "target_fact_id": fact_id,
                "executed_channel": "Human",
                "override_reason": "smoke_doctor_round",
            },
        ).json()
        task = client.post(
            "/api/doctor-tasks",
            json={
                "round_id": round_row.json()["round_id"],
                "run_id": runs[0]["run_id"],
                "encounter_id": runs[0]["encounter_id"],
                "attempt_id": human_attempt["attempt_id"],
                "state_snapshot_id": human_attempt["state_snapshot_id"],
                "task_type": "doctor_question",
                "payload_json": {"request": "请医生补一个 Human 问题"},
            },
        )
        assert task.status_code == 200, task.text
        workbook = client.post(
            f"/api/doctor-rounds/{round_row.json()['round_id']}/workbook"
        )
        assert workbook.status_code == 200, workbook.text
        assert workbook.json()["storage_key"], workbook.json()
        imported = client.post(
            f"/api/doctor-rounds/{round_row.json()['round_id']}/import",
            json={
                "responses": [
                    {
                        "task_id": task.json()["task_id"],
                        "version": task.json()["version"],
                        "response_json": {"question_text": "请再描述一下疼痛的性质"},
                    }
                ]
            },
        )
        assert imported.status_code == 200, imported.text
        resumed = client.post("/api/case-runs/resume-ready")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["resumed"] >= 1, resumed.json()

        attempt = client.post(
            f"/api/encounters/{encounter}/attempts",
            json={
                "target_fact_id": fact_id,
                "executed_channel": "Structured",
                "override_reason": "smoke_test",
            },
        ).json()
        assert attempt["assignment_schedule"], "attempt did not receive a randomization schedule"
        assert attempt["interaction_mode"] == "async", attempt
        schedule = attempt["assignment_schedule"]
        assert schedule["strategy"] == "stratified_simple", schedule
        assert abs(schedule["propensity"] - 1 / 3) < 1e-9, schedule
        assert attempt["target_declared_at"] is not None
        assert attempt["channel_revealed_at"] is not None
        assert attempt["target_declared_at"] <= attempt["channel_revealed_at"], attempt
        assert attempt["target_declared_source"] in {"server_receipt", "client"}, attempt
        encounter_out = client.get(f"/api/encounters/{encounter}").json()
        assert "randomization_seed" not in encounter_out, encounter_out
        forbidden = client.post(
            f"/api/encounters/{encounter}/attempts",
            json={"target_fact_id": fact_id, "randomization_seed": 123},
        )
        assert forbidden.status_code in {200, 422}, forbidden.text
        attempt_id = attempt["attempt_id"]
        planned = client.post(f"/api/attempts/{attempt_id}/question/plan", json={})
        assert planned.status_code == 200, planned.text
        question = planned.json()
        assert question["status"] in {"generated", "planned"}, question
        if question["status"] == "planned":
            # LLM generation may still be running in a real deployment.
            for _ in range(10):
                detail = client.get(f"/api/attempts/{attempt_id}").json()
                question = detail["question"]
                if question["status"] == "generated":
                    break
        assert question["question_text"], question
        question_id = question["question_event_id"]
        assert client.post(f"/api/questions/{question_id}/approve").status_code == 200
        assert client.post(f"/api/questions/{question_id}/queue").status_code == 200
        link = client.post(f"/api/encounters/{encounter}/patient-session-link")
        assert link.status_code == 200, link.text
        token = link.json()["token"]
        session = client.get(f"/api/patient/session/{token}")
        assert session.status_code == 200, session.text
        assert any(item["question_event_id"] == question_id for item in session.json()["questions"])
        # A patient link must expose only its own encounter's queue: leaking
        # another encounter's question text is a privacy defect, not a UX bug.
        other_encounter = client.post(
            "/api/encounters",
            json={"case_id": case2.json()["case_id"], "interviewer_id": "smoke-doctor"},
        ).json()["encounter_id"]
        other_attempt = client.post(
            f"/api/encounters/{other_encounter}/attempts",
            json={
                "target_fact_id": fact_id,
                "executed_channel": "Structured",
                "override_reason": "smoke_isolation",
            },
        ).json()
        other_question = client.post(
            f"/api/attempts/{other_attempt['attempt_id']}/question/plan", json={}
        ).json()
        client.post(f"/api/questions/{other_question['question_event_id']}/approve")
        client.post(f"/api/questions/{other_question['question_event_id']}/queue")
        session_again = client.get(f"/api/patient/session/{token}")
        assert session_again.status_code == 200, session_again.text
        leaked = [
            item
            for item in session_again.json()["questions"]
            if item["question_event_id"] == other_question["question_event_id"]
        ]
        assert not leaked, leaked
        assert all(
            item["attempt_id"] != other_attempt["attempt_id"]
            for item in session_again.json()["questions"]
        ), session_again.json()
        assert client.post(
            f"/api/patient/session/{token}/questions/{question_id}/view"
        ).status_code == 200
        answer = client.post(
            f"/api/patient/session/{token}/questions/{question_id}/answer",
            json={"answer_text": "没有这种情况"},
        )
        assert answer.status_code == 200, answer.text
        extracted = client.get(f"/api/attempts/{attempt_id}").json()
        assert extracted["resource_units"], extracted
        assert extracted["resource_units"]["patient_turns"] >= 1, extracted

        for rater in ("rater-a", "rater-b"):
            response = client.post(
                f"/api/attempts/{attempt_id}/outcomes",
                json={
                    "rater_id": rater,
                    "acquired_label": "yes",
                    "recorded_label": "yes",
                    "usable_label": "yes",
                    "actionable_label": "yes",
                },
            )
            assert response.status_code == 200, response.text
        detail = client.get(f"/api/attempts/{attempt_id}").json()
        assert detail["consensus"]["status"] == "final", detail
        llm_attempt = client.post(
            f"/api/encounters/{encounter}/attempts",
            json={
                "target_fact_id": fact_id,
                "executed_channel": "LLM",
                "override_reason": "smoke_llm_async",
            },
        ).json()
        assert llm_attempt["interaction_mode"] == "async", llm_attempt
        llm_question = client.post(
            f"/api/attempts/{llm_attempt['attempt_id']}/question/plan", json={}
        )
        assert llm_question.status_code == 200, llm_question.text
        llm_detail = client.get(f"/api/attempts/{llm_attempt['attempt_id']}").json()
        assert llm_detail["question"]["question_text"], llm_detail

        bad = client.post(
            f"/api/attempts/{attempt_id}/outcomes",
            json={
                "rater_id": "doctor-demo",
                "acquired_label": "yes",
                "recorded_label": "yes",
                "usable_label": "yes",
                "actionable_label": "yes",
            },
        )
        assert bad.status_code == 400, bad.text

        refused_attempt = client.post(
            f"/api/encounters/{encounter}/attempts",
            json={
                "target_fact_id": fact_id,
                "executed_channel": "Human",
                "override_reason": "smoke_refusal",
            },
        ).json()
        refused_id = refused_attempt["attempt_id"]
        assert client.post(
            f"/api/attempts/{refused_id}/question",
            json={"question_text": "请描述一下这个症状"},
        ).status_code == 200
        refused = client.post(
            f"/api/attempts/{refused_id}/answer",
            json={"answer_status": "refused"},
        )
        assert refused.status_code == 200, refused.text
        refused_detail = client.get(f"/api/attempts/{refused_id}").json()
        assert refused_detail["failure_events"][0]["failure_code"] == "patient_refused", refused_detail

        export = client.post("/api/exports", json={})
        assert export.status_code == 200, export.text
        manifest = export.json()["manifest_json"]
        assert manifest["success_head_rows"] == 4, manifest
        assert manifest["analysis_population"] == "itt", manifest
        assert "encounter_outcomes.jsonl" in manifest["files"], manifest
        export_dir = Path(export.json()["export_dir"])
        itt_rows = [
            json.loads(line)
            for line in (export_dir / "success_train.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert itt_rows, "no success rows exported"
        for row in itt_rows:
            assert row["audit"]["arm"] == row["audit"]["assigned_channel"], row
        encounter_rows = [
            json.loads(line)
            for line in (export_dir / "encounter_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert encounter_rows, "no encounter outcome rows exported"
        # The documented target needs question text and channel as inputs, not
        # only as prose in the instruction string.
        for row in itt_rows:
            assert row["action"]["channel"] == row["audit"]["arm"], row
            assert row["state"]["action_channel"] == row["audit"]["arm"], row
        coverage = manifest["coverage"]
        assert coverage["counting_unit"] == "distinct_attempt", coverage
        assert coverage["per_target_fact_arm"], coverage
        for cell in coverage["per_target_fact_arm"].values():
            assert cell["mdd"] is not None, cell
        backlog = manifest["adjudication_backlog"]
        assert backlog["attempts"] >= backlog["finalized"] >= 1, backlog
        assert "pending_rate" in backlog, backlog
        # Every un-adjudicated attempt is silently excluded from the export, so
        # the gate must be able to reject an export that is only the easy subset.
        gated = client.post("/api/exports", json={"max_pending_rate": 1e-9})
        assert gated.status_code == 400, gated.text
        assert "backlog" in gated.json()["detail"], gated.json()

        print("SMOKE_API_OK", tmp)


if __name__ == "__main__":
    main()
