"""Create reproducible multi-head training exports from real-pilot attempts.

The exporter never uses the latest arbitrary outcome as a training label.  It
uses the versioned adjudication consensus, validates split isolation, and emits
one target row per configured head/channel pair.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .catalog import load_catalog
from .config import settings
from .models import (
    AdjudicationConsensus,
    AssignmentSchedule,
    AttemptFailureEvent,
    AttemptOutcome,
    Case,
    Encounter,
    FactAttempt,
    QuestionEvent,
    RouteStateSnapshot,
    SplitManifest,
    TrainingExport,
)
from .services import canonical_json
from .storage import get_storage


def _content_type(filename: str) -> str:
    if filename.endswith(".jsonl"):
        return "application/x-ndjson"
    if filename.endswith(".json"):
        return "application/json"
    if filename.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


CHANNEL_SLUG = {"Structured": "structured", "LLM": "llm", "Human": "human"}
HEADS = {
    "acquired": "acquired_label",
    "recorded": "recorded_label",
    "usable": "usable_label",
    "actionable": "actionable_label",
}
SPLIT_FILES = {
    "train": "success_train.jsonl",
    "calib": "success_calib.jsonl",
    "valid": "success_valid.jsonl",
    "test": "success_test.jsonl",
    "heldout_shift": "success_heldout_shift.jsonl",
}
def _iso(value):
    return value.isoformat() if value is not None else None


SPLIT_GROUP_FIELDS = (
    ("case", "case_id"),
    ("patient_group", "patient_group_id"),
    ("source_case", "source_case_id"),
    ("template", "template_id"),
)


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _final_consensus(db: Session, attempt_id: str) -> AdjudicationConsensus | None:
    return db.scalar(
        select(AdjudicationConsensus)
        .where(
            AdjudicationConsensus.attempt_id == attempt_id,
            AdjudicationConsensus.status == "final",
        )
    )


def _split_rows(
    db: Session,
    include_roles: set[str] | None = None,
) -> list[tuple[FactAttempt, Case, Encounter, AdjudicationConsensus]]:
    stmt = (
        select(FactAttempt, Case, Encounter, AdjudicationConsensus)
        .join(Encounter, Encounter.encounter_id == FactAttempt.encounter_id)
        .join(Case, Case.case_id == Encounter.case_id)
        .join(AdjudicationConsensus, AdjudicationConsensus.attempt_id == FactAttempt.attempt_id)
        .where(AdjudicationConsensus.status == "final")
        .order_by(Case.case_code, FactAttempt.created_at)
    )
    if include_roles:
        stmt = stmt.where(Case.split_role.in_(include_roles))
    return list(db.execute(stmt).all())


def _validate_split_integrity(
    rows: list[tuple[FactAttempt, Case, Encounter, AdjudicationConsensus]],
) -> list[dict[str, Any]]:
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    manifest_rows: list[dict[str, Any]] = []
    for _attempt, case, encounter, _consensus in rows:
        if case.split_role not in SPLIT_FILES:
            raise ValueError(f"invalid split role: {case.split_role}")
        if case.split_role == "heldout_shift" and not case.shift_scope:
            raise ValueError(f"heldout_shift case lacks shift_scope: {case.case_code}")
        for group_type, field in SPLIT_GROUP_FIELDS:
            group_key = getattr(case, field)
            if not group_key:
                continue
            owners[(group_type, str(group_key))].add(case.split_role)
            manifest_rows.append(
                {
                    "split_role": case.split_role,
                    "group_key_type": group_type,
                    "group_key": str(group_key),
                    "site_id": case.site_id,
                    "time_bucket": case.collection_time_bucket,
                }
            )
    leaked = {
        key: sorted(splits)
        for key, splits in owners.items()
        if len(splits) > 1
    }
    if leaked:
        raise ValueError(f"split leakage detected: {leaked}")
    # Keep the manifest reproducible and free of duplicate rows.
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in manifest_rows:
        deduped[
            (
                row["split_role"],
                row["group_key_type"],
                row["group_key"],
                row["site_id"],
                row["time_bucket"],
            )
        ] = row
    return list(deduped.values())


def _split_metadata_coverage(
    rows: list[tuple[FactAttempt, Case, Encounter, AdjudicationConsensus]],
) -> dict[str, Any]:
    fields = ("patient_group_id", "site_id", "collection_time_bucket")
    missing = {field: 0 for field in fields}
    for _attempt, case, _encounter, _consensus in rows:
        for field in fields:
            if not getattr(case, field):
                missing[field] += 1
    return {
        "rows": len(rows),
        "missing": missing,
        "complete": all(value == 0 for value in missing.values()),
    }


def _outcome_label(consensus: AdjudicationConsensus, head: str) -> str | None:
    return getattr(consensus, HEADS[head])


def _cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    categories = sorted({value for pair in pairs for value in pair})
    n = len(pairs)
    observed = sum(1 for left, right in pairs if left == right) / n
    expected = 0.0
    for category in categories:
        left_rate = sum(1 for left, _right in pairs if left == category) / n
        right_rate = sum(1 for _left, right in pairs if right == category) / n
        expected += left_rate * right_rate
    if expected >= 1.0:
        return None
    return (observed - expected) / (1.0 - expected)


def _inter_rater_metrics(
    db: Session,
    rows: list[tuple[FactAttempt, Case, Encounter, AdjudicationConsensus]],
) -> dict[str, Any]:
    by_head: dict[str, dict[str, Any]] = {
        head: {"attempts": 0, "pairs": 0, "observed_agreement": [], "kappa": []}
        for head in HEADS
    }
    by_fact: dict[str, dict[str, Any]] = defaultdict(lambda: {"attempts": 0, "agreements": []})
    by_channel: dict[str, dict[str, Any]] = defaultdict(lambda: {"attempts": 0, "agreements": []})
    for attempt, _case, _encounter, _consensus in rows:
        outcomes = db.scalars(
            select(AttemptOutcome)
            .where(AttemptOutcome.attempt_id == attempt.attempt_id)
            .order_by(AttemptOutcome.created_at.asc())
        ).all()
        if len(outcomes) < 2:
            continue
        attempt_agreements: list[float] = []
        for head, field in HEADS.items():
            pairs = [
                (getattr(outcomes[index], field), getattr(outcomes[index + 1], field))
                for index in range(len(outcomes) - 1)
            ]
            if not pairs:
                continue
            observed = sum(1 for left, right in pairs if left == right) / len(pairs)
            kappa = _cohen_kappa(pairs)
            by_head[head]["attempts"] += 1
            by_head[head]["pairs"] += len(pairs)
            by_head[head]["observed_agreement"].append(observed)
            if kappa is not None:
                by_head[head]["kappa"].append(kappa)
            attempt_agreements.append(observed)
        if attempt_agreements:
            by_fact[attempt.target_fact_id]["attempts"] += 1
            by_fact[attempt.target_fact_id]["agreements"].append(
                sum(attempt_agreements) / len(attempt_agreements)
            )
            by_channel[attempt.executed_channel]["attempts"] += 1
            by_channel[attempt.executed_channel]["agreements"].append(
                sum(attempt_agreements) / len(attempt_agreements)
            )
    def summarize(values: dict[str, Any]) -> dict[str, Any]:
        agreements = values.get("agreements", [])
        return {
            "attempts": values.get("attempts", 0),
            "observed_agreement": (sum(agreements) / len(agreements)) if agreements else None,
        }
    head_summary = {}
    for head, values in by_head.items():
        agreements = values["observed_agreement"]
        kappas = values["kappa"]
        head_summary[head] = {
            "attempts": values["attempts"],
            "pairs": values["pairs"],
            "observed_agreement": (sum(agreements) / len(agreements)) if agreements else None,
            "mean_cohen_kappa": (sum(kappas) / len(kappas)) if kappas else None,
        }
    return {
        "by_head": head_summary,
        "by_target_fact": {key: summarize(value) for key, value in sorted(by_fact.items())},
        "by_executed_channel": {key: summarize(value) for key, value in sorted(by_channel.items())},
        "note": "observed_agreement is averaged over attempts; kappa is pairwise for available raters.",
    }


def export_training(db: Session, payload: Mapping[str, Any]) -> TrainingExport:
    include_roles = set(payload.get("include_roles") or SPLIT_FILES)
    require_consensus = bool(
        payload.get("require_consensus", settings.require_consensus_for_export)
    )
    analysis_population = str(payload.get("analysis_population") or "itt")
    min_cell_size = int(payload.get("min_cell_size") or 0)
    max_pending_rate = float(payload.get("max_pending_rate") or 0.0)
    baseline_rate = float(payload.get("baseline_success_rate") or 0.85)
    if analysis_population not in {"itt", "per_protocol"}:
        raise ValueError(f"unsupported analysis_population: {analysis_population}")
    export_id = hashlib.sha256(
        f"{datetime.now(timezone.utc).isoformat()}|"
        f"{canonical_json({'roles': sorted(include_roles), 'consensus': require_consensus})}".encode("utf-8")
    ).hexdigest()[:24]
    # W4.2: build every artifact in a scratch directory, verify checksums,
    # then upload all-or-nothing exactly once before any DB row exists.
    temp_dir = tempfile.TemporaryDirectory(prefix=f"laya-export-{export_id}-")
    export_dir = Path(temp_dir.name)
    catalog = load_catalog()

    source_rows = _split_rows(db, include_roles)
    if require_consensus and any(row[3].status != "final" for row in source_rows):
        raise ValueError("all exported attempts require final consensus")
    split_manifest = _validate_split_integrity(source_rows)
    split_metadata_coverage = _split_metadata_coverage(source_rows)
    if settings.require_split_metadata and not split_metadata_coverage["complete"]:
        raise ValueError(f"split metadata incomplete: {split_metadata_coverage}")
    inter_rater_metrics = _inter_rater_metrics(db, source_rows)
    adjudication_backlog = _adjudication_backlog(db)
    if max_pending_rate > 0 and adjudication_backlog["pending_rate"] > max_pending_rate:
        raise ValueError(
            f"adjudication backlog {adjudication_backlog['pending_rate']:.3f} exceeds "
            f"max_pending_rate {max_pending_rate}: the export would be the easy subset"
        )

    route_rows: list[dict[str, Any]] = []
    success_rows_by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    success_rows_flat: list[dict[str, Any]] = []
    head_rows_by_split: dict[str, dict[str, list[dict[str, Any]]]] = {
        head: defaultdict(list) for head in HEADS
    }
    split_counts = Counter()
    head_counts = Counter()
    skip_counts = Counter()

    for attempt, case, encounter, consensus in source_rows:
        snapshot = db.get(RouteStateSnapshot, attempt.state_snapshot_id)
        if snapshot is None:
            skip_counts["missing_state_snapshot"] += 1
            continue
        if case.split_role not in SPLIT_FILES:
            skip_counts["invalid_split_role"] += 1
            continue
        state = dict(snapshot.state_json)
        question = _question_record(db, attempt.attempt_id)
        question_text = question.question_text if question else ""
        assignment = (
            db.get(AssignmentSchedule, attempt.assignment_schedule_id)
            if attempt.assignment_schedule_id
            else None
        )
        failures = db.scalars(
            select(AttemptFailureEvent).where(AttemptFailureEvent.attempt_id == attempt.attempt_id)
        ).all()
        route_sample = {
            "sample_schema_version": "route-sample-v2",
            "contract_version": "real-pilot-route-v1",
            "sample_id": attempt.attempt_id,
            "encounter_id": encounter.encounter_id,
            "case_id": case.case_id,
            "split": case.split_role,
            "split_group_id": case.case_code,
            "turn": state.get("turn", 0),
            "target": {
                "target_kind": "clinical_fact",
                "clinical_fact_id": attempt.target_fact_id,
                "slot_key": attempt.slot_key,
                "question_text": question_text,
                "question_variant_id": None,
            },
            "question_event": (
                {
                    "question_event_id": question.question_event_id,
                    "status": question.status,
                    "version": question.version,
                    "generated_at": _iso(question.generated_at),
                    "approved_at": _iso(question.approved_at),
                    "queued_at": _iso(question.queued_at),
                    "delivered_at": _iso(question.delivered_at),
                    "viewed_at": _iso(question.viewed_at),
                    "answered_at": _iso(question.answered_at),
                    "expires_at": _iso(question.expires_at),
                }
                if question
                else None
            ),
            "state": {
                "visible_state": state,
                "state_schema_version": snapshot.state_schema_version,
                "state_serialization_version": snapshot.state_serialization_version,
                "state_hash": snapshot.state_hash,
            },
            "channel": {
                "assigned": attempt.assigned_channel,
                "executed": attempt.executed_channel,
                "interaction_mode": attempt.interaction_mode,
                "assignment_method": attempt.assignment_method,
                "strategy": assignment.strategy if assignment else None,
                "target_declared_at": _iso(attempt.target_declared_at),
                "target_declared_source": attempt.target_declared_source,
                "channel_revealed_at": _iso(attempt.channel_revealed_at),
                "assignment_method": attempt.assignment_method,
                "propensity": attempt.propensity,
                "execution_propensity": attempt.execution_propensity,
                "assignment_schedule_id": attempt.assignment_schedule_id,
                "block_sequence_index": assignment.block_sequence_index if assignment else None,
                "block_position": assignment.block_position if assignment else None,
                "override_reason": attempt.override_reason,
                "itt_channel": attempt.assigned_channel,
                "per_protocol_channel": attempt.executed_channel,
                "override": attempt.assigned_channel != attempt.executed_channel,
                "observed_channel_only": True,
                "counterfactual_label": False,
            },
            "outcomes": {
                "acquired": consensus.acquired_label,
                "recorded": consensus.recorded_label,
                "usable": consensus.usable_label,
                "actionable": consensus.actionable_label,
                "agreement_rate": consensus.agreement_rate,
                "disagreement_flag": consensus.disagreement_flag,
                "label_codebook_version": consensus.label_codebook_version,
            },
            "selection": {
                "is_retry": bool(attempt.is_retry),
                "previous_attempt_id": attempt.previous_attempt_id,
                "selection_reason": attempt.selection_reason,
            },
            "resources": {
                "duration_ms": attempt.duration_ms,
                "units": dict(attempt.resource_units_json or {}),
            },
            "failures": [
                {
                    "failure_stage": row.failure_stage,
                    "failure_code": row.failure_code,
                    "recoverable": row.recoverable,
                }
                for row in failures
            ],
            "metadata": {
                "site_id": case.site_id,
                "interviewer_id": encounter.interviewer_id,
                "patient_group_id": case.patient_group_id,
                "collection_time_bucket": case.collection_time_bucket,
                "protocol_version": encounter.protocol_version,
                "label_codebook_version": consensus.label_codebook_version,
                "catalog_version": catalog.catalog_version,
            },
        }
        route_rows.append(route_sample)

        # ITT attributes the observed outcome to the randomized arm, which is
        # the only arm whose allocation probability is known.  The executed
        # channel stays in the audit block for per-protocol or CACE analysis.
        arm_channel = (
            attempt.assigned_channel if analysis_population == "itt" else attempt.executed_channel
        )
        channel_slug = CHANNEL_SLUG[arm_channel]
        for head in HEADS:
            label = _outcome_label(consensus, head)
            if label not in {"yes", "no"}:
                skip_counts[f"non_binary_{head}"] += 1
                continue
            probability = 1.0 if label == "yes" else 0.0
            # The documented target is
            #   P(success | visible_state, target_fact, question_text, channel)
            # so question text and channel must be readable model inputs, not
            # only prose inside the instruction string.
            item_state = dict(state)
            if question_text:
                item_state["question_text"] = question_text
            item_state["action_channel"] = arm_channel
            success_row = {
                "sample_id": f"{attempt.attempt_id}::success_{channel_slug}_{head}",
                "head": head,
                "action": {
                    "channel": arm_channel,
                    "arm_basis": "assigned" if analysis_population == "itt" else "executed",
                    "question_text": question_text,
                    "question_source": question.question_source if question else None,
                    "question_known_at_decision": bool(
                        question and question.question_source != "llm"
                    ),
                },
                "question": {
                    "type": "noul",
                    "key": f"success_{channel_slug}_{head}",
                    "instructions": (
                        f"Will the {arm_channel} channel satisfy {head} "
                        f"for {attempt.slot_key} in the current state?"
                    ),
                },
                "state": item_state,
                "gold": {
                    "probabilities": {"false": 1.0 - probability, "true": probability},
                    "success_count": int(probability),
                    "trials": 1,
                    "raw_probability": probability,
                    "probability_source": "real_adjudication_consensus",
                    "label_source": "real_adjudication_consensus",
                    "head": head,
                    "label": label,
                    "label_codebook_version": consensus.label_codebook_version,
                },
                "audit": {
                    "attempt_id": attempt.attempt_id,
                    "encounter_id": encounter.encounter_id,
                    "case_id": case.case_id,
                    "case_code": case.case_code,
                    "split": case.split_role,
                    "target_fact_id": attempt.target_fact_id,
                    "slot_key": attempt.slot_key,
                    "question_text": question_text,
                    "analysis_population": analysis_population,
                    "arm": arm_channel,
                    "assigned_channel": attempt.assigned_channel,
                    "executed_channel": attempt.executed_channel,
                    "interaction_mode": attempt.interaction_mode,
                    "duration_ms": attempt.duration_ms,
                    "resource_units": dict(attempt.resource_units_json or {}),
                    "assignment_method": attempt.assignment_method,
                    "propensity": attempt.propensity,
                    "execution_propensity": attempt.execution_propensity,
                    "observed_channel_only": True,
                    "counterfactual_label": False,
                    "state_hash": snapshot.state_hash,
                    "outcome": {
                        "acquired": consensus.acquired_label,
                        "recorded": consensus.recorded_label,
                        "usable": consensus.usable_label,
                        "actionable": consensus.actionable_label,
                    },
                },
            }
            success_rows_by_split[case.split_role].append(success_row)
            success_rows_flat.append(success_row)
            head_rows_by_split[head][case.split_role].append(success_row)
            split_counts[case.split_role] += 1
            head_counts[head] += 1

    coverage = _coverage_report(success_rows_flat, baseline_rate=baseline_rate)
    if min_cell_size > 0:
        too_small = {
            fact: report["min_arm"]
            for fact, report in coverage["per_target_fact_arm"].items()
            if report["min_arm"] < min_cell_size
        }
        if too_small:
            raise ValueError(
                f"target fact x arm cells below min_cell_size={min_cell_size}: {too_small}"
            )
    encounter_rows = _encounter_outcome_rows(db, source_rows)
    route_hash = _write_jsonl(export_dir / "route_samples_v2.jsonl", route_rows)
    file_hashes: dict[str, str] = {"route_samples_v2.jsonl": route_hash}
    for split, filename in SPLIT_FILES.items():
        rows = success_rows_by_split.get(split, [])
        file_hashes[filename] = _write_jsonl(export_dir / filename, rows)
    for head, rows_by_split in head_rows_by_split.items():
        for split, rows in rows_by_split.items():
            filename = f"success_{split}_{head}.jsonl"
            file_hashes[filename] = _write_jsonl(export_dir / filename, rows)

    split_manifest_path = export_dir / "split_manifest.json"
    split_manifest_path.write_text(
        json.dumps(split_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    file_hashes["split_manifest.json"] = hashlib.sha256(split_manifest_path.read_bytes()).hexdigest()
    file_hashes["encounter_outcomes.jsonl"] = _write_jsonl(
        export_dir / "encounter_outcomes.jsonl", encounter_rows
    )
    metrics_path = export_dir / "inter_rater_metrics.json"
    metrics_path.write_text(
        json.dumps(inter_rater_metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    file_hashes["inter_rater_metrics.json"] = hashlib.sha256(metrics_path.read_bytes()).hexdigest()

    manifest = {
        "export_version": "real-pilot-export-v2",
        "export_id": export_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "catalog_version": catalog.catalog_version,
        "label_codebook_version": settings.label_codebook_version,
        "randomization_schedule_version": settings.randomization_schedule_version,
        "randomization_strategy": settings.randomization_strategy,
        "include_roles": sorted(include_roles),
        "require_consensus": require_consensus,
        "analysis_population": analysis_population,
        "adjudication_reviews_required": settings.adjudication_reviews_required,
        "coverage": coverage,
        "adjudication_backlog": adjudication_backlog,
        "sample_size_policy": {
            "min_cell_size": min_cell_size,
            "max_pending_rate": max_pending_rate,
        },
        "row_count": len(route_rows),
        "success_head_rows": sum(split_counts.values()),
        "head_counts": dict(head_counts),
        "split_counts": dict(split_counts),
        "inter_rater_metrics": inter_rater_metrics,
        "split_metadata_coverage": split_metadata_coverage,
        "skip_counts": dict(skip_counts),
        "files": file_hashes,
        "storage_prefix": f"exports/{export_id}",
        "notes": [
            "state is the pre-attempt snapshot",
            "question_text is audit metadata, not an opaque model feature",
            "recorded is not self-certified by the LLM extractor",
            "export uses final adjudication consensus and versioned label codebook",
            "itt arm is the randomized channel; override stays in audit for per-protocol analysis",
            "head-specific files are available as success_{split}_{head}.jsonl",
        ],
    }
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (export_dir / "checksums.txt").write_text(
        "".join(
            f"{digest}  {name}\n"
            for name, digest in sorted(
                {
                    **file_hashes,
                    "manifest.json": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                }.items()
            )
        ),
        encoding="utf-8",
    )

    storage = get_storage()
    storage_prefix = f"exports/{export_id}"
    try:
        for name in sorted({*file_hashes, "manifest.json", "checksums.txt"}):
            storage.put_bytes(
                f"{storage_prefix}/{name}",
                (export_dir / name).read_bytes(),
                _content_type(name),
            )
    except Exception:
        # No DB record is written; the caller reports a 5xx.  Uploaded
        # fragments are unreferenced and safe to garbage-collect.
        temp_dir.cleanup()
        raise
    temp_dir.cleanup()
    if settings.storage_backend == "supabase":
        export_uri = f"supabase://{settings.storage_bucket}/{storage_prefix}"
    else:
        export_uri = str(settings.export_dir / storage_prefix)

    for row in split_manifest:
        db.add(
            SplitManifest(
                export_id=export_id,
                split_role=row["split_role"],
                group_key_type=row["group_key_type"],
                group_key=row["group_key"],
                site_id=row.get("site_id"),
                time_bucket=row.get("time_bucket"),
            )
        )
    record = TrainingExport(
        export_id=export_id,
        status="ready",
        export_dir=export_uri,
        row_count=len(route_rows),
        manifest_json=manifest,
        completed_at=datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def minimum_detectable_difference(
    n_per_arm: int,
    *,
    baseline: float = 0.85,
    alpha: float = 0.05,
    power: float = 0.8,
) -> float | None:
    """Smallest absolute difference two arms can resolve at this cell size.

    Normal approximation for two independent proportions; reported so the team
    can see whether a per-fact cell is big enough for the question it is meant
    to answer instead of discovering it after training.
    """
    if n_per_arm <= 0:
        return None
    z_alpha = 1.959963985 if abs(alpha - 0.05) < 1e-9 else 2.575829304
    z_power = 0.841621234 if abs(power - 0.8) < 1e-9 else 1.281551566
    variance = 2.0 * baseline * (1.0 - baseline)
    return float((z_alpha + z_power) * (variance / n_per_arm) ** 0.5)


def _coverage_report(rows, *, baseline_rate: float = 0.85) -> dict[str, Any]:
    """Distinct-attempt counts per cell, because the four heads share an attempt.

    Counting exported rows would multiply every attempt by the number of heads
    and overstate the evidence behind any single head.
    """
    per_fact_arm: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    per_split_arm: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    per_head_fact_arm: dict[str, dict[str, dict[str, set[str]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(set))
    )
    for row in rows:
        audit = row["audit"]
        attempt_id = audit["attempt_id"]
        per_fact_arm[audit["target_fact_id"]][audit["arm"]].add(attempt_id)
        per_split_arm[audit["split"]][audit["arm"]].add(attempt_id)
        per_head_fact_arm[row["head"]][audit["target_fact_id"]][audit["arm"]].add(attempt_id)
    fact_cells = {}
    for fact, counts in sorted(per_fact_arm.items()):
        sizes = {arm: len(ids) for arm, ids in counts.items()}
        min_arm = min(sizes.values()) if sizes else 0
        fact_cells[fact] = {
            "attempts_per_arm": sizes,
            "min_arm": min_arm,
            "mdd": minimum_detectable_difference(min_arm, baseline=baseline_rate),
        }
    return {
        "per_target_fact_arm": fact_cells,
        "per_head_target_fact_arm": {
            head: {
                fact: {arm: len(ids) for arm, ids in counts.items()}
                for fact, counts in sorted(facts.items())
            }
            for head, facts in sorted(per_head_fact_arm.items())
        },
        "per_split_arm": {
            split: {arm: len(ids) for arm, ids in counts.items()}
            for split, counts in sorted(per_split_arm.items())
        },
        "counting_unit": "distinct_attempt",
        "baseline_rate_for_mdd": baseline_rate,
        "mdd_note": (
            "mdd is the smallest absolute difference in success rate this cell can "
            "resolve at 80% power; a cell big enough only for a per-fact intercept "
            "cannot support a state-dependent model"
        ),
    }


def _adjudication_backlog(db: Session, limit: int = 5000) -> dict[str, Any]:
    """Report attempts that never reached a final consensus.

    Only final consensus rows are exported, so a large pending backlog means the
    training set is the easy subset: hard attempts stay stuck waiting on a
    curator and silently drop out.
    """
    attempts = db.scalars(select(FactAttempt).limit(limit)).all()
    pending = 0
    disagreement = 0
    finalized = 0
    for attempt in attempts:
        consensus = db.scalar(
            select(AdjudicationConsensus).where(AdjudicationConsensus.attempt_id == attempt.attempt_id)
        )
        if consensus is None or consensus.status != "final":
            pending += 1
            if consensus is not None and consensus.disagreement_flag:
                disagreement += 1
        else:
            finalized += 1
    total = len(attempts)
    return {
        "attempts": total,
        "finalized": finalized,
        "pending": pending,
        "pending_with_disagreement": disagreement,
        "pending_rate": (pending / total) if total else 0.0,
        "note": (
            "pending attempts are excluded from every export, so a high pending rate "
            "biases the training set toward the samples that were easy to agree on"
        ),
    }


def _encounter_outcome_rows(
    db: Session,
    rows: list[tuple[FactAttempt, Case, Encounter, AdjudicationConsensus]],
) -> list[dict[str, Any]]:
    """Encounter-level outcomes and physical cost units.

    These rows are the input for utility and policy level evaluation; they are
    deliberately separate from the per-attempt success heads.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for attempt, case, encounter, consensus in rows:
        record = grouped.setdefault(
            encounter.encounter_id,
            {
                "encounter_id": encounter.encounter_id,
                "case_id": case.case_id,
                "case_code": case.case_code,
                "split": case.split_role,
                "site_id": case.site_id,
                "patient_group_id": case.patient_group_id,
                "interviewer_id": encounter.interviewer_id,
                "attempts": 0,
                "usable_yes": 0,
                "usable_no": 0,
                "acquired_yes": 0,
                "recorded_yes": 0,
                "actionable_yes": 0,
                "overrides": 0,
                "retries": 0,
                "duration_ms": 0,
                "resource_units": {
                    "patient_turns": 0,
                    "llm_calls": 0,
                    "llm_tokens": 0,
                    "human_minutes": 0.0,
                },
                "distinct_facts": set(),
            },
        )
        record["attempts"] += 1
        record["distinct_facts"].add(attempt.target_fact_id)
        if consensus.usable_label == "yes":
            record["usable_yes"] += 1
        elif consensus.usable_label == "no":
            record["usable_no"] += 1
        if consensus.acquired_label == "yes":
            record["acquired_yes"] += 1
        if consensus.recorded_label == "yes":
            record["recorded_yes"] += 1
        if consensus.actionable_label == "yes":
            record["actionable_yes"] += 1
        if attempt.assigned_channel != attempt.executed_channel:
            record["overrides"] += 1
        if attempt.is_retry:
            record["retries"] += 1
        record["duration_ms"] += int(attempt.duration_ms or 0)
        units = dict(attempt.resource_units_json or {})
        for key in ("patient_turns", "llm_calls", "llm_tokens"):
            record["resource_units"][key] += int(units.get(key) or 0)
        human_minutes = units.get("human_minutes")
        if human_minutes is not None:
            record["resource_units"]["human_minutes"] += float(human_minutes)
    output = []
    for record in grouped.values():
        record["distinct_facts"] = len(record["distinct_facts"])
        if not record["resource_units"]["human_minutes"]:
            record["resource_units"]["human_minutes"] = None
        output.append(record)
    return sorted(output, key=lambda item: item["case_code"])


def _question_record(db: Session, attempt_id: str):
    return db.scalar(
        select(QuestionEvent)
        .where(QuestionEvent.attempt_id == attempt_id)
        .order_by(QuestionEvent.version.desc(), QuestionEvent.created_at.desc())
    )
