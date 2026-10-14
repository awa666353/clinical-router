#!/usr/bin/env python3
"""Process queued jobs for the hosted collector (W6).

This is the correctness-critical worker: BackgroundTasks in the API only
trigger an immediate pass for latency, while this loop guarantees that
question generation, answer extraction and question expiry eventually run even
across API/worker restarts.

Run exactly ONE instance (guide W6.6).  ``FOR UPDATE SKIP LOCKED`` would allow
more, but the current pilot does not need it.
"""
from __future__ import annotations

import argparse
import time

from backend.config import settings, validate_production_settings
from backend.db import SessionLocal, check_database_schema, init_db
from backend.game import process_pending_game_scores
from backend.log_redaction import install_log_redaction
from backend.services import expire_due_questions, process_pending_question_jobs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--sleep", type=int, default=settings.async_worker_poll_seconds)
    parser.add_argument(
        "--expiry-scan-seconds",
        type=int,
        default=settings.question_expiry_scan_seconds,
        help="how often to expire due questions (default 60s)",
    )
    args = parser.parse_args()

    # The worker is a production process too; it must not bypass the same
    # configuration/schema guards as the API and accidentally run create_all
    # against a misconfigured hosted database.
    install_log_redaction()
    validate_production_settings()
    check_database_schema()
    init_db()
    last_expiry_scan = 0.0
    while True:
        now = time.monotonic()
        with SessionLocal() as db:
            process_pending_game_scores(db, limit=args.limit)
            jobs = process_pending_question_jobs(db, limit=args.limit)
            for job in jobs:
                print(job.job_id, job.status, job.error or "", flush=True)
            # W6.4: expiry is a worker responsibility, not a manual API call.
            if args.once or now - last_expiry_scan >= max(1, args.expiry_scan_seconds):
                expired = expire_due_questions(db)
                if expired:
                    print(f"expired {expired} question(s)", flush=True)
                last_expiry_scan = now
        if args.once:
            break
        time.sleep(max(1, args.sleep))


if __name__ == "__main__":
    main()
