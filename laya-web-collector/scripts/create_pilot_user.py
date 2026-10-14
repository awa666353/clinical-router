#!/usr/bin/env python3
"""Provision a pilot account (W5).

Creates (or reuses) a Supabase Auth user through the Admin API with the
server-only secret key, then inserts the ``pilot.app_users`` role mapping.
Public sign-up is disabled in the Supabase dashboard, so this script (or an
admin invite) is the only account-creation path.

The password is read from ``LAYA_PILOT_PASSWORD`` or ``--password``; it is
never printed and never written to logs or Git.

Example:
    python scripts/create_pilot_user.py \
        --email doctor1@example.org --pilot-user-id doctor-01 --role doctor --site-id site-a
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.db import SessionLocal  # noqa: E402
from backend.models import AppUser  # noqa: E402


ROLES = ("doctor", "interviewer", "adjudicator", "curator", "admin")


def _admin_headers(secret_key: str) -> dict[str, str]:
    return {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }


def create_auth_user(email: str, password: str) -> str:
    """Create the Supabase auth user and return its UUID (``sub``)."""
    if not settings.supabase_url or not settings.supabase_secret_key:
        raise SystemExit("SUPABASE_URL and SUPABASE_SECRET_KEY must be configured in the environment")
    url = f"{settings.supabase_url}/auth/v1/admin/users"
    response = httpx.post(
        url,
        headers=_admin_headers(settings.supabase_secret_key),
        json={"email": email, "password": password, "email_confirm": True},
        timeout=20,
    )
    if response.status_code not in (200, 201):
        raise SystemExit(
            f"Supabase Admin API rejected the user (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )
    payload = response.json()
    user_id = payload.get("id") or payload.get("user", {}).get("id")
    if not user_id:
        raise SystemExit("Supabase Admin API response did not contain a user id")
    return str(user_id)


def upsert_app_user(auth_user_id: str, pilot_user_id: str, role: str, site_id: str | None) -> AppUser:
    with SessionLocal() as db:
        existing = db.get(AppUser, auth_user_id)
        if existing is None:
            existing = db.scalar(select(AppUser).where(AppUser.pilot_user_id == pilot_user_id))
        if existing is None:
            existing = AppUser(auth_user_id=auth_user_id, pilot_user_id=pilot_user_id, role=role, site_id=site_id)
        else:
            existing.auth_user_id = auth_user_id
            existing.role = role
            existing.site_id = site_id
            existing.active = True
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--pilot-user-id", required=True, help="stable id stored in audit/outcome rows")
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--site-id", default=None)
    parser.add_argument("--password", default=os.getenv("LAYA_PILOT_PASSWORD", ""))
    args = parser.parse_args()

    password = args.password
    if not password:
        raise SystemExit("set LAYA_PILOT_PASSWORD (or --password); refusing to create an account without one")
    if len(password) < 12:
        raise SystemExit("password must be at least 12 characters")

    auth_user_id = create_auth_user(args.email, password)
    user = upsert_app_user(auth_user_id, args.pilot_user_id, args.role, args.site_id)
    print(f"provisioned {user.pilot_user_id} ({user.role}) for {args.email}")


if __name__ == "__main__":
    main()
