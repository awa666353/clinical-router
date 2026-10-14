from __future__ import annotations

import importlib.util
import unittest
from unittest import mock


HAS_DEPS = all(importlib.util.find_spec(name) for name in ("fastapi", "sqlalchemy", "httpx"))


@unittest.skipUnless(HAS_DEPS, "web collector dependencies are not installed")
class ApiSmokeTest(unittest.TestCase):
    def test_app_imports(self) -> None:
        from backend.app import app

        self.assertEqual(app.title, "Laya Real-pilot Clinical Collector")

    def test_disabled_auth_uses_frontend_no_login_contract(self) -> None:
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.config import settings

        # No lifespan/database is needed for these auth and static-page routes.
        with mock.patch.dict(settings.__dict__, {"auth_mode": "disabled"}):
            client = TestClient(app)
            try:
                self.assertEqual(client.get("/api/auth/me").status_code, 404)
                for path in ("/", "/doctor", "/adjudicator"):
                    with self.subTest(path=path):
                        self.assertEqual(client.get(path).status_code, 200)
            finally:
                client.close()

    def test_gateway_auth_keeps_verified_identity(self) -> None:
        from fastapi.testclient import TestClient
        from backend.app import app
        from backend.config import settings

        with mock.patch.dict(settings.__dict__, {
            "auth_mode": "gateway",
            "auth_gateway_secret": "local-test-gateway-secret",
        }):
            client = TestClient(app)
            try:
                self.assertEqual(client.get("/api/auth/me").status_code, 401)
                response = client.get("/api/auth/me", headers={
                    "X-User-Id": "test-doctor",
                    "X-User-Role": "doctor",
                    "X-Gateway-Secret": "local-test-gateway-secret",
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["role"], "doctor")
                self.assertEqual(response.json()["pilot_user_id"], "test-doctor")
            finally:
                client.close()


if __name__ == "__main__":
    unittest.main()
