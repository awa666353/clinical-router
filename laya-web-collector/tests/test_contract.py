from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.catalog import load_catalog


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None


def migration_sql() -> str:
    """Combined SQL from the single schema source of truth (W2)."""
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
    )


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CollectorContractTest(unittest.TestCase):
    def test_catalog_has_required_identity_fields(self) -> None:
        catalog = load_catalog()
        self.assertEqual(catalog.topic, "chest_pain")
        self.assertGreaterEqual(len(catalog.facts), 9)
        for fact in catalog.facts:
            self.assertTrue(fact.clinical_fact_id)
            self.assertTrue(fact.display_name)
            self.assertIn(fact.kind, {"presence", "categorical", "ordinal", "temporal", "numeric", "text", "list"})

    def test_canonical_state_is_stable(self) -> None:
        left = {"b": 2, "a": [1, 2]}
        right = {"a": [1, 2], "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(sha256_text(canonical_json(left)), sha256_text(canonical_json(right)))

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_permuted_segment_respects_arm_quota(self) -> None:
        from backend.services import CHANNELS, _permuted_segment

        for length in (3, 6):
            segment = _permuted_segment(
                salt="unit-test-salt",
                segment_index=1,
                segment_length=length,
            )
            self.assertEqual(len(segment), length)
            for channel in CHANNELS:
                self.assertEqual(segment.count(channel), length // len(CHANNELS))

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_allocation_draw_is_secret_dependent_and_state_independent(self) -> None:
        from backend.services import _draw_bits

        first = _draw_bits("salt-a", 11, 1, "arm")
        self.assertEqual(first, _draw_bits("salt-a", 11, 1, "arm"))
        self.assertNotEqual(first, _draw_bits("salt-b", 11, 1, "arm"))
        # The draw takes only (salt, encounter seed, sequence index): no state,
        # no patient answer, no clinician input can shift the allocation.
        with self.assertRaises(TypeError):
            _draw_bits("salt-a", 11, 1, "arm", state={})

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_encounter_seed_is_secret_by_default(self) -> None:
        from backend.services import _new_encounter_seed

        seeds = {_new_encounter_seed() for _ in range(20)}
        self.assertGreater(len(seeds), 15)
        self.assertNotIn(20260928, seeds)

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_permuted_block_keeps_arm_quota_inside_each_segment(self) -> None:
        """Balance is exact at segment boundaries, not at an arbitrary stop.

        Segments are filled to completion in order, but the run can stop inside
        the final segment (e.g. 6+3+3 of a 6-long last segment), so the exact
        guarantee is per completed segment.  A segment spans several encounters,
        so the permutation must not be recomputed per encounter either.
        """
        from collections import Counter
        from dataclasses import replace

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from backend import services as svc
        from backend.models import AssignmentSchedule, Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        session_factory = sessionmaker(bind=engine)
        original = svc.settings
        svc.settings = replace(original, randomization_strategy="permuted_block")
        try:
            with session_factory() as db:
                case = svc.create_case(
                    db,
                    {"case_code": "PERMUTED-BLOCK-QUOTA", "topic": "chest_pain", "split_role": "train"},
                )
                for _ in range(12):
                    encounter = svc.create_encounter(
                        db,
                        {"case_id": case.case_id, "interviewer_id": "doc", "patient_actor_id": "actor"},
                    )
                    svc.start_attempt(
                        db, encounter.encounter_id, {"target_fact_id": "associated_symptoms"}
                    )
                rows = db.scalars(
                    select(AssignmentSchedule).order_by(AssignmentSchedule.sequence_index)
                ).all()
        finally:
            svc.settings = original
        self.assertEqual(len(rows), 12)
        segments: dict[Any, list[Any]] = {}
        for row in rows:
            segments.setdefault(row.block_sequence_index, []).append(row)
        self.assertTrue(segments)
        for index, segment_rows in segments.items():
            length = segment_rows[0].segment_length
            self.assertIsNotNone(length, index)
            self.assertIn(length, (3, 6), index)
            quota = length // len(("Structured", "LLM", "Human"))
            counts = Counter(row.assigned_channel for row in segment_rows)
            for channel in ("Structured", "LLM", "Human"):
                self.assertLessEqual(counts[channel], quota, (index, counts))
            if len(segment_rows) == length:
                for channel in ("Structured", "LLM", "Human"):
                    self.assertEqual(counts[channel], quota, (index, counts))
            for row in segment_rows:
                self.assertIn(round(row.propensity, 6), {0.333333, 0.5, 1.0})
        # Only the final segment may be short, and only it may be unbalanced.
        short = [index for index, segment_rows in segments.items() if len(segment_rows) != segment_rows[0].segment_length]
        self.assertLessEqual(len(short), 1, short)

    def test_allocation_seed_is_not_exposed_by_the_encounter_api(self) -> None:
        schema_text = (ROOT / "backend" / "schemas.py").read_text(encoding="utf-8")
        encounter_out = schema_text.split("class EncounterOut", 1)[1].split("class ", 1)[0]
        self.assertNotIn("randomization_seed", encounter_out)

    def test_schema_records_concealment_and_resource_units(self) -> None:
        sql = migration_sql()
        attempts = sql.split("CREATE TABLE IF NOT EXISTS pilot.fact_attempts", 1)[1].split(");", 1)[0]
        for column in (
            "target_declared_at",
            "channel_revealed_at",
            "is_retry",
            "previous_attempt_id",
            "selection_reason",
            "duration_ms",
            "resource_units_json",
        ):
            self.assertIn(column, attempts)
        self.assertIn("success_context_json", sql)
        self.assertIn("salt", sql)

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_default_interaction_mode_matches_channel_policy(self) -> None:
        from backend.services import default_interaction_mode

        self.assertEqual(default_interaction_mode("Structured"), "async")
        self.assertEqual(default_interaction_mode("LLM"), "async")
        self.assertEqual(default_interaction_mode("Human"), "sync")

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_label_dependency_allows_unable_to_assess_but_not_false_positive(self) -> None:
        from backend.services import _validate_label_dependencies

        _validate_label_dependencies(
            {
                "acquired_label": "unable_to_assess",
                "recorded_label": "no",
                "usable_label": "no",
                "actionable_label": "no",
            }
        )
        with self.assertRaises(ValueError):
            _validate_label_dependencies(
                {
                    "acquired_label": "no",
                    "recorded_label": "yes",
                    "usable_label": "yes",
                    "actionable_label": "yes",
                }
            )

    def test_schema_contains_training_critical_tables(self) -> None:
        sql = migration_sql()
        for table in (
            "turn_events",
            "route_state_snapshots",
            "fact_attempts",
            "llm_calls",
            "fact_assertion_candidates",
            "fact_state_events",
            "attempt_outcomes",
            "randomization_blocks",
            "assignment_schedule",
            "attempt_failure_events",
            "adjudication_assignments",
            "adjudication_consensus",
            "split_manifest",
            "patient_question_links",
            "question_jobs",
            "question_status_events",
            "patient_actors",
            "case_batches",
            "case_runs",
            "doctor_rounds",
            "doctor_tasks",
            "doctor_documents",
            "run_events",
            "encounter_assignments",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS pilot.{table}", sql)

    @unittest.skipUnless(HAS_SQLALCHEMY, "sqlalchemy is not installed")
    def test_schema_sql_matches_orm_models(self) -> None:
        """Migrations are the schema source of truth; drift from the ORM is a real risk."""
        import re

        from backend.models import Base

        keywords = {"CHECK", "UNIQUE", "PRIMARY", "FOREIGN", "CONSTRAINT", "EXCLUDE"}
        sql = migration_sql()
        blocks: dict[str, list[str]] = {}
        for match in re.finditer(r"CREATE TABLE IF NOT EXISTS (?:pilot\.)?(\w+)\s*\((.*?)\n\);", sql, flags=re.S):
            columns = []
            for line in match.group(2).splitlines():
                line = line.strip()
                if not line or line.startswith("--"):
                    continue
                token = line.split()[0].strip(",")
                if token.split("(")[0].upper() in keywords:
                    continue
                columns.append(token)
            blocks[match.group(1)] = columns
        for table in Base.metadata.sorted_tables:
            self.assertIn(table.name, blocks, f"{table.name} missing from supabase/migrations/")
            model_columns = {column.name for column in table.columns}
            schema_columns = set(blocks[table.name])
            self.assertEqual(
                model_columns - schema_columns,
                set(),
                f"{table.name} columns missing from supabase/migrations/",
            )
            self.assertEqual(
                schema_columns - model_columns,
                set(),
                f"{table.name} has migration columns with no model field",
            )

    def test_manifest_contract_mentions_pre_attempt_state(self) -> None:
        self.assertIn("state is the pre-attempt snapshot", (ROOT / "backend" / "export_training.py").read_text(encoding="utf-8"))
        self.assertIn("real-pilot-label-v1", (ROOT / "data" / "label_codebook_v1.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
