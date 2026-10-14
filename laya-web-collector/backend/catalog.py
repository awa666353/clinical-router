"""Fact and question catalog used by the collector UI and validator."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


APP_DIR = Path(__file__).resolve().parents[1]
CATALOG_PATH = APP_DIR / "data" / "catalog.json"


@dataclass(frozen=True)
class FactDefinition:
    clinical_fact_id: str
    display_name: str
    answer_schema: Mapping[str, Any]
    criticality: int
    review_status: str
    notes: str = ""

    @property
    def kind(self) -> str:
        return str(self.answer_schema.get("kind", "text"))

    @property
    def allowed_values(self) -> tuple[str, ...]:
        return tuple(str(v) for v in self.answer_schema.get("allowed_values", ()))

    @property
    def value_labels(self) -> Mapping[str, str]:
        return dict(self.answer_schema.get("value_labels", {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clinical_fact_id": self.clinical_fact_id,
            "display_name": self.display_name,
            "answer_schema": dict(self.answer_schema),
            "criticality": self.criticality,
            "review_status": self.review_status,
            "notes": self.notes,
        }


class Catalog:
    def __init__(self, payload: Mapping[str, Any]):
        self.topic = str(payload["topic"])
        self.catalog_version = str(payload["catalog_version"])
        self.review_status = str(payload.get("review_status", "unknown"))
        self._facts = {
            str(item["clinical_fact_id"]): FactDefinition(
                clinical_fact_id=str(item["clinical_fact_id"]),
                display_name=str(item.get("display_name", item["clinical_fact_id"])),
                answer_schema=dict(item.get("answer_schema", {})),
                criticality=int(item.get("criticality", 3)),
                review_status=str(item.get("review_status", "unknown")),
                notes=str(item.get("notes", "")),
            )
            for item in payload.get("facts", ())
        }
        self._questions: dict[str, list[dict[str, Any]]] = {}
        for item in payload.get("question_variants", ()):
            fact_id = str(item["clinical_fact_id"])
            self._questions.setdefault(fact_id, []).append(dict(item))
        for questions in self._questions.values():
            questions.sort(key=lambda row: (row.get("channel") != "Structured", str(row.get("question_variant_id", ""))))

    @property
    def facts(self) -> tuple[FactDefinition, ...]:
        return tuple(sorted(self._facts.values(), key=lambda fact: (-fact.criticality, fact.clinical_fact_id)))

    def fact(self, clinical_fact_id: str) -> FactDefinition:
        try:
            return self._facts[clinical_fact_id]
        except KeyError as exc:
            raise KeyError(f"unknown clinical_fact_id: {clinical_fact_id}") from exc

    def structured_questions(self, clinical_fact_id: str) -> list[dict[str, Any]]:
        return list(self._questions.get(clinical_fact_id, ()))

    def first_structured_question(self, clinical_fact_id: str) -> dict[str, Any] | None:
        questions = self.structured_questions(clinical_fact_id)
        return questions[0] if questions else None


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    return Catalog(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))
