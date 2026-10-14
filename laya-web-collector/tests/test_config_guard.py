"""W1 acceptance tests: the production startup guard must reject each unsafe
configuration and must leave ``APP_ENV=development`` untouched."""
from __future__ import annotations

import dataclasses
import os
import unittest
from unittest import mock

from backend.config import (
    Settings,
    _default_db_auto_create,
    production_config_problems,
    settings,
    validate_production_settings,
)


def _production(**overrides) -> Settings:
    """A minimal configuration that is safe for production."""
    base = dataclasses.replace(
        settings,
        app_env="production",
        database_url="postgresql+psycopg://laya_app:secret@pooler.example:5432/postgres",
        storage_backend="supabase",
        auth_mode="supabase",
        db_auto_create=False,
        auto_seed_demo=False,
        randomization_deterministic_seed=None,
        cors_origins="https://collector.example.org",
        supabase_url="https://projectref.supabase.co",
        supabase_publishable_key="publishable-key",
        supabase_secret_key="server-only-secret-key",
    )
    return dataclasses.replace(base, **overrides)


class ProductionGuardTest(unittest.TestCase):
    def test_safe_production_config_has_no_problems(self):
        self.assertEqual(production_config_problems(_production()), [])

    def test_development_is_never_rejected(self):
        dev = dataclasses.replace(
            settings,
            app_env="development",
            database_url="sqlite:///local.sqlite3",
            auth_mode="disabled",
            storage_backend="local",
            auto_seed_demo=True,
        )
        self.assertEqual(production_config_problems(dev), [])
        validate_production_settings(dev)  # must not raise

    def test_each_rejection_condition(self):
        cases = {
            "auth_mode": _production(auth_mode="disabled"),
            "sqlite": _production(database_url="sqlite:///local.sqlite3"),
            "storage": _production(storage_backend="local"),
            "db_auto_create": _production(db_auto_create=True),
            "seed": _production(auto_seed_demo=True),
            "deterministic_seed": _production(randomization_deterministic_seed=42),
            "cors_wildcard": _production(cors_origins="*"),
            "cors_localhost": _production(cors_origins="http://localhost:8080"),
            "missing_secret": _production(supabase_secret_key=""),
            "missing_url": _production(supabase_url=""),
            "missing_publishable_key": _production(supabase_publishable_key=""),
        }
        for name, config in cases.items():
            with self.subTest(name=name):
                problems = production_config_problems(config)
                self.assertTrue(problems, f"{name} was not rejected")
                with self.assertRaises(RuntimeError):
                    validate_production_settings(config)

    def test_db_auto_create_defaults(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://u:p@h:5432/db"}):
            self.assertFalse(_default_db_auto_create())
        with mock.patch.dict(os.environ, {"DATABASE_URL": "sqlite:///x.db"}):
            self.assertTrue(_default_db_auto_create())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(_default_db_auto_create())


if __name__ == "__main__":
    unittest.main()
