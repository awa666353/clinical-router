"""Regression tests for independent adjudication and blinding."""
from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.catalog import load_catalog
from backend.models import Base
from backend.services import (
    adjudication_queue,
    create_case,
    create_encounter,
    finalize_consensus,
    record_outcome,
    start_attempt,
)


class AdjudicationReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'review.sqlite3'}")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _make_attempt(self):
        case = create_case(
            self.db,
            {"case_code": f"REVIEW-{uuid.uuid4().hex[:10]}", "source_type": "synthetic", "actor_id": "test"},
        )
        encounter = create_encounter(
            self.db,
            {"case_id": case.case_id, "interviewer_id": "doctor-1", "actor_id": "test"},
        )
        attempt = start_attempt(
            self.db,
            encounter.encounter_id,
            {
                "target_fact_id": load_catalog().facts[0].clinical_fact_id,
                "executed_channel": "LLM",
                "override_reason": "test",
                "actor_id": "test",
            },
        )
        attempt.status = "extracted"
        self.db.commit()
        return attempt

    @staticmethod
    def _labels(rater_id: str):
        return {
            "rater_id": rater_id,
            "rater_role": "adjudicator",
            "acquired_label": "yes",
            "recorded_label": "yes",
            "usable_label": "yes",
            "actionable_label": "yes",
        }

    def test_second_rater_still_sees_item_and_queue_does_not_expose_channel(self):
        attempt = self._make_attempt()
        first = adjudication_queue(self.db)
        self.assertEqual(len(first), 1)
        self.assertNotIn("channel_audit", first[0])

        record_outcome(self.db, attempt.attempt_id, self._labels("rater-1"))
        after_one = adjudication_queue(self.db)
        self.assertEqual(len(after_one), 1, "the second independent review must remain queueable")
        self.assertEqual(after_one[0]["review_count"], 1)
        self.assertNotIn("channel_audit", after_one[0])

        record_outcome(self.db, attempt.attempt_id, self._labels("rater-2"))
        self.assertEqual(adjudication_queue(self.db), [])

    def test_consensus_cannot_be_finalized_before_required_reviews(self):
        attempt = self._make_attempt()
        with self.assertRaisesRegex(ValueError, "independent reviews"):
            finalize_consensus(
                self.db,
                attempt.attempt_id,
                {"final_rater_id": "rater-1", **self._labels("rater-1")},
            )


if __name__ == "__main__":
    unittest.main()
