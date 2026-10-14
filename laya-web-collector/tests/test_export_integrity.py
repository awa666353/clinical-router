"""Sample-size and export-integrity guards for the real pilot.

The review's two open objections about the collector were that (a) the export
cannot answer the question it claims to answer at the available sample size,
and (b) the "model input" did not actually contain the target definition's
inputs.  These tests pin the reporting and the gate that make both visible.
"""
from __future__ import annotations

import importlib.util
import unittest


HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "sqlalchemy", "httpx"))


@unittest.skipUnless(HAS_DEPS, "web collector dependencies are not installed")
class MinimumDetectableDifferenceTest(unittest.TestCase):
    def test_mdd_shrinks_with_sample_size(self):
        from backend.export_training import minimum_detectable_difference

        small = minimum_detectable_difference(30)
        large = minimum_detectable_difference(800)
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)
        self.assertGreater(small, large)
        # 30 per arm is only good enough for a per-fact intercept, not for a
        # 5-point difference: this is the number the team needs to see.
        self.assertGreater(small, 0.05)
        self.assertLess(large, 0.09)

    def test_mdd_is_undefined_without_samples(self):
        from backend.export_training import minimum_detectable_difference

        self.assertIsNone(minimum_detectable_difference(0))

    def test_mdd_grows_towards_a_maximally_uncertain_baseline(self):
        from backend.export_training import minimum_detectable_difference

        # p=0.5 maximises the binomial variance, so it is the hardest baseline
        uncertain = minimum_detectable_difference(100, baseline=0.5)
        confident = minimum_detectable_difference(100, baseline=0.95)
        self.assertGreater(uncertain, confident)

    def test_eight_hundred_attempts_per_arm_resolves_a_five_point_difference(self):
        """The review's own arithmetic, pinned so the pilot target is explicit."""
        from backend.export_training import minimum_detectable_difference

        self.assertEqual(round(minimum_detectable_difference(800, baseline=0.85), 3), 0.05)
        self.assertGreater(minimum_detectable_difference(30, baseline=0.85), 0.2)


@unittest.skipUnless(HAS_DEPS, "web collector dependencies are not installed")
class CoverageReportTest(unittest.TestCase):
    def _row(self, attempt_id, arm, fact, split, head="usable"):
        return {
            "head": head,
            "audit": {
                "attempt_id": attempt_id,
                "arm": arm,
                "target_fact_id": fact,
                "split": split,
            },
        }

    def test_coverage_counts_distinct_attempts_not_exported_rows(self):
        from backend.export_training import _coverage_report

        rows = [
            # one attempt produces four head rows; the cell size is one attempt
            *[self._row("a1", "Structured", "pain_character", "train", head=h)
              for h in ("acquired", "recorded", "usable", "actionable")],
            *[self._row("a2", "Human", "pain_character", "train", head=h)
              for h in ("acquired", "recorded", "usable", "actionable")],
        ]
        report = _coverage_report(rows)
        self.assertEqual(report["counting_unit"], "distinct_attempt")
        cell = report["per_target_fact_arm"]["pain_character"]
        self.assertEqual(cell["attempts_per_arm"], {"Human": 1, "Structured": 1})
        self.assertEqual(cell["min_arm"], 1)
        self.assertIsNotNone(cell["mdd"])
        self.assertEqual(
            report["per_head_target_fact_arm"]["usable"]["pain_character"],
            {"Structured": 1, "Human": 1},
        )

    def test_coverage_reports_every_split(self):
        from backend.export_training import _coverage_report

        report = _coverage_report(
            [
                self._row("a1", "LLM", "pain_duration", "train"),
                self._row("a2", "LLM", "pain_duration", "test"),
            ]
        )
        self.assertEqual(report["per_split_arm"]["train"], {"LLM": 1})
        self.assertEqual(report["per_split_arm"]["test"], {"LLM": 1})


if __name__ == "__main__":
    unittest.main()
