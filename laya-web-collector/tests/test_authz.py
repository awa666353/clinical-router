"""W5 acceptance tests: every non-public /api route is authenticated, roles are
enforced from pilot.app_users, and identities cannot be forged.

The tests run the real FastAPI app with the Supabase auth middleware enabled
and an isolated SQLite database; JWT verification uses the legacy HS256 secret
path with a throw-away test secret.
"""
from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import jwt as pyjwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config as config_module
from backend import db as db_module
from backend.app import app
from backend.auth import PUBLIC_API_PATHS, PUBLIC_API_PATTERN, clear_jwks_cache
from backend.db import get_db
from backend.models import AppUser, Base

TEST_SECRET = "unit-test-jwt-secret-not-used-anywhere-else-0123456789abcdef"
TEST_URL = "https://testref.supabase.co"


def make_token(
    sub: str,
    *,
    secret: str = TEST_SECRET,
    url: str = TEST_URL,
    expires_in: int = 3600,
    audience: str = "authenticated",
) -> str:
    now = datetime.now(timezone.utc)
    return pyjwt.encode(
        {
            "sub": sub,
            "aud": audience,
            "iss": f"{url}/auth/v1",
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
        },
        secret,
        algorithm="HS256",
    )


class AuthzTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._engine = create_engine(
            f"sqlite:///{Path(cls._tmp.name) / 'authz.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(cls._engine)
        cls._session_factory = sessionmaker(bind=cls._engine, expire_on_commit=False)
        cls._original_session_factory = db_module.SessionLocal
        db_module.SessionLocal = cls._session_factory

        def override_db():
            session = cls._session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_db
        cls._settings_snapshot = {
            "auth_mode": config_module.settings.auth_mode,
            "app_env": config_module.settings.app_env,
            "supabase_url": config_module.settings.supabase_url,
            "supabase_jwt_secret": config_module.settings.supabase_jwt_secret,
            "supabase_secret_key": config_module.settings.supabase_secret_key,
            "auto_seed_demo": config_module.settings.auto_seed_demo,
            "allow_real_patient_data": config_module.settings.allow_real_patient_data,
            "storage_backend": config_module.settings.storage_backend,
            "export_dir": config_module.settings.export_dir,
        }
        # object.__setattr__ bypasses the frozen dataclass for tests only.
        for key, value in {
            "auth_mode": "supabase",
            "app_env": "development",
            "supabase_url": TEST_URL,
            "supabase_jwt_secret": TEST_SECRET,
            "supabase_secret_key": "test-secret-key",
            "auto_seed_demo": False,
            "allow_real_patient_data": False,
            "storage_backend": "local",
            "export_dir": Path(cls._tmp.name) / "exports",
        }.items():
            object.__setattr__(config_module.settings, key, value)
        clear_jwks_cache()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        app.dependency_overrides.pop(get_db, None)
        db_module.SessionLocal = cls._original_session_factory
        for key, value in cls._settings_snapshot.items():
            object.__setattr__(config_module.settings, key, value)
        clear_jwks_cache()
        cls._engine.dispose()
        cls._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def provision(self, *, role: str, pilot_user_id: str, active: bool = True) -> tuple[str, str]:
        auth_id = uuid.uuid4().hex
        with self._session_factory() as session:
            session.add(
                AppUser(
                    auth_user_id=auth_id,
                    pilot_user_id=pilot_user_id,
                    role=role,
                    active=active,
                )
            )
            session.commit()
        return auth_id, make_token(auth_id)

    def auth(self, token: str, **extra):
        headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "laya-collector"}
        headers.update(extra)
        return headers

    # -- tests -------------------------------------------------------------
    def test_every_non_public_api_route_requires_a_token(self):
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            if not path.startswith("/api/"):
                continue
            if path in PUBLIC_API_PATHS or PUBLIC_API_PATTERN.match(path):
                continue
            method = "GET" if "GET" in methods else sorted(methods)[0]
            target = path
            while "{" in target:
                start = target.index("{")
                end = target.index("}", start)
                target = target[:start] + "test-id" + target[end + 1 :]
            response = self.client.request(method, target)
            with self.subTest(path=path, method=method):
                self.assertEqual(response.status_code, 401, response.text)
            checked += 1
        self.assertGreater(checked, 20, "route enumeration found suspiciously few routes")

    def test_public_endpoints_do_not_need_a_token(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/health/ready").status_code, 200)
        self.assertEqual(self.client.get("/api/patient/session/nope").status_code, 401)

    def test_invalid_expired_and_unprovisioned_tokens(self):
        expired = make_token("someone", expires_in=-10)
        self.assertEqual(self.client.get("/api/catalog", headers=self.auth(expired)).status_code, 401)
        wrong_signature = make_token("someone", secret="not-the-project-secret")
        self.assertEqual(
            self.client.get("/api/catalog", headers=self.auth(wrong_signature)).status_code, 401
        )
        self.assertEqual(
            self.client.get("/api/health", headers=self.auth(wrong_signature)).status_code, 200,
            "invalid tokens must be ignored on public endpoints",
        )
        unprovisioned = make_token(uuid.uuid4().hex)
        self.assertEqual(
            self.client.get("/api/catalog", headers=self.auth(unprovisioned)).status_code, 403
        )

    def test_inactive_account_is_forbidden(self):
        auth_id, token = self.provision(role="doctor", pilot_user_id="doc-inactive", active=False)
        del auth_id
        self.assertEqual(
            self.client.get("/api/catalog", headers=self.auth(token)).status_code, 403
        )

    def test_roles_come_from_app_users_not_headers(self):
        _, doctor = self.provision(role="doctor", pilot_user_id="doc-1")
        response = self.client.get(
            "/api/stats", headers=self.auth(doctor, **{"X-User-Role": "admin"})
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/catalog", headers=self.auth(doctor)).status_code, 200)

    def test_adjudicator_cannot_read_attempt_detail(self):
        _, adjudicator = self.provision(role="adjudicator", pilot_user_id="adj-1")
        response = self.client.get("/api/attempts/does-not-exist", headers=self.auth(adjudicator))
        self.assertEqual(response.status_code, 403)

    def test_doctor_cannot_use_another_interviewers_encounter(self):
        # Missing encounter returns 404, but a doctor querying a foreign
        # encounter id must never leak data.  The 404 path proves authz passed
        # while the role itself was accepted.
        _, doctor = self.provision(role="doctor", pilot_user_id="doc-2")
        response = self.client.get("/api/encounters/does-not-exist", headers=self.auth(doctor))
        self.assertEqual(response.status_code, 404)

    def test_forged_rater_id_is_rejected(self):
        _, adjudicator = self.provision(role="adjudicator", pilot_user_id="adj-2")
        payload = {
            "rater_id": "adj-1",
            "acquired_label": "yes",
            "recorded_label": "yes",
            "usable_label": "yes",
            "actionable_label": "yes",
        }
        response = self.client.post(
            "/api/attempts/does-not-exist/outcomes",
            json=payload,
            headers=self.auth(adjudicator),
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("rater_id", response.json()["detail"])

    def test_csrf_header_required_for_unsafe_requests(self):
        _, curator = self.provision(role="curator", pilot_user_id="cur-1")
        response = self.client.post(
            "/api/cases",
            json={"case_code": "CSRF-1"},
            headers={"Authorization": f"Bearer {curator}"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("X-Requested-With", response.json()["detail"])

    def test_real_patient_data_is_rejected(self):
        _, curator = self.provision(role="curator", pilot_user_id="cur-2")
        response = self.client.post(
            "/api/cases",
            json={"case_code": "REAL-1", "source_type": "real"},
            headers=self.auth(curator),
        )
        self.assertEqual(response.status_code, 400)
        ok = self.client.post(
            "/api/cases",
            json={"case_code": "SYNTH-1", "source_type": "synthetic"},
            headers=self.auth(curator),
        )
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_production_hides_health_details_and_dev_routes(self):
        _, doctor = self.provision(role="doctor", pilot_user_id="doc-3")
        object.__setattr__(config_module.settings, "app_env", "production")
        try:
            health = self.client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok"})
            latest = self.client.get("/api/encounters/latest", headers=self.auth(doctor))
            self.assertEqual(latest.status_code, 404)
            patient_view = self.client.get(
                "/api/encounters/anything/patient-view", headers=self.auth(doctor)
            )
            self.assertEqual(patient_view.status_code, 404)
            blocked_real = self.client.post(
                "/api/cases",
                json={"case_code": "REAL-2", "source_type": "real"},
                headers=self.auth(doctor),
            )
            self.assertEqual(blocked_real.status_code, 403, "doctor cannot create cases")
        finally:
            object.__setattr__(config_module.settings, "app_env", "development")


if __name__ == "__main__":
    unittest.main()
