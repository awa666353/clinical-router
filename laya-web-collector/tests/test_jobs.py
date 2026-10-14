"""W6 acceptance tests for durable background jobs.

* repeating an answer enqueue produces exactly one job (idempotency key);
* a worker killed mid-job is reaped from ``running`` on the next pass;
* extraction failures set ``needs_review`` and are bounded by max_attempts.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.catalog import load_catalog
from backend.models import AuditEvent, Base, FactAttempt, QuestionJob, utcnow
from backend.services import (
    append_turn,
    create_case,
    create_encounter,
    enqueue_extract_job,
    process_pending_question_jobs,
    process_question_job,
    reap_stuck_question_jobs,
    start_attempt,
)


class QuestionJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self._tmp.name) / 'jobs.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self._tmp.cleanup()

    def _make_attempt(self, *, with_answer: bool) -> FactAttempt:
        case = create_case(
            self.db,
            {
                "case_code": f"JOB-{uuid.uuid4().hex[:10]}",
                "source_type": "synthetic",
                "actor_id": "test",
            },
        )
        encounter = create_encounter(
            self.db,
            {"case_id": case.case_id, "interviewer_id": "doc-test", "actor_id": "test"},
        )
        fact = load_catalog().facts[0]
        attempt = start_attempt(
            self.db,
            encounter.encounter_id,
            {
                "target_fact_id": fact.clinical_fact_id,
                "executed_channel": "LLM",
                "override_reason": "unit test",
                "actor_id": "test",
            },
        )
        if with_answer:
            turn = append_turn(
                self.db,
                encounter.encounter_id,
                {"speaker": "patient", "raw_text": "胸痛三天", "actor_id": "test"},
            )
            attempt.end_turn_id = turn.turn_id
        attempt.status = "extracting"
        self.db.add(attempt)
        self.db.commit()
        return attempt

    def test_duplicate_answer_enqueue_creates_one_job(self):
        attempt = self._make_attempt(with_answer=True)
        first = enqueue_extract_job(
            self.db, attempt=attempt, answer_turn_id=attempt.end_turn_id, question_event_id=None
        )
        second = enqueue_extract_job(
            self.db, attempt=attempt, answer_turn_id=attempt.end_turn_id, question_event_id=None
        )
        self.db.commit()
        count = self.db.scalar(
            select(func.count()).select_from(QuestionJob).where(
                QuestionJob.idempotency_key == f"extract:{attempt.attempt_id}:{attempt.end_turn_id}"
            )
        )
        self.assertEqual(count, 1)
        self.assertEqual(first.job_id, second.job_id)

    def test_stale_running_job_is_reaped(self):
        attempt = self._make_attempt(with_answer=False)
        job = QuestionJob(
            attempt_id=attempt.attempt_id,
            job_type="extract_answer",
            status="running",
            attempts=1,
            max_attempts=3,
            idempotency_key=f"extract:{attempt.attempt_id}:stale",
            started_at=utcnow() - timedelta(minutes=11),
        )
        self.db.add(job)
        self.db.commit()
        reaped = reap_stuck_question_jobs(self.db)
        self.db.refresh(job)
        self.assertEqual(reaped, 1)
        self.assertEqual(job.status, "queued")
        self.assertIsNone(job.started_at)

    def test_stale_job_at_max_attempts_becomes_failed_with_audit(self):
        attempt = self._make_attempt(with_answer=False)
        job = QuestionJob(
            attempt_id=attempt.attempt_id,
            job_type="extract_answer",
            status="running",
            attempts=3,
            max_attempts=3,
            idempotency_key=f"extract:{attempt.attempt_id}:exhausted",
            started_at=utcnow() - timedelta(minutes=30),
        )
        self.db.add(job)
        self.db.commit()
        reap_stuck_question_jobs(self.db)
        self.db.refresh(job)
        self.assertEqual(job.status, "failed")
        audit_count = self.db.scalar(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.object_id == job.job_id,
                AuditEvent.event_type == "question_job.reaped",
            )
        )
        self.assertEqual(audit_count, 1)

    def test_extract_failure_marks_needs_review_and_bounds_retries(self):
        attempt = self._make_attempt(with_answer=False)  # no end_turn_id -> extraction fails
        job = enqueue_extract_job(
            self.db, attempt=attempt, answer_turn_id="turn-missing", question_event_id=None
        )
        self.db.commit()
        result = process_question_job(self.db, job.job_id)
        self.db.refresh(attempt)
        self.assertEqual(result.status, "failed")
        self.assertIn("no patient answer", result.error)
        self.assertEqual(attempt.status, "needs_review")
        # attempts must increment despite the internal rollback, so the job
        # stops being retried after max_attempts.
        self.assertEqual(result.attempts, 1)
        process_question_job(self.db, job.job_id)
        process_question_job(self.db, job.job_id)
        self.db.refresh(job)
        self.assertEqual(job.attempts, 3)
        self.assertEqual(len(process_pending_question_jobs(self.db)), 0)


if __name__ == "__main__":
    unittest.main()
