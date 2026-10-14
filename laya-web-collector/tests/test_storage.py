"""W4 acceptance tests: storage backends and all-or-nothing export uploads."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend import export_training as export_training_module
from backend import config as config_module
from backend.models import Base, TrainingExport
from backend.storage import (
    LocalStorage,
    StorageError,
    SupabaseStorage,
    reset_storage_cache,
    validate_key,
)


class LocalStorageTest(unittest.TestCase):
    def test_roundtrip_and_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = LocalStorage(Path(tmp))
            self.assertTrue(storage.ping())
            storage.put_bytes("exports/e1/route_samples_v2.jsonl", b"{}\n")
            self.assertEqual(storage.get_bytes("exports/e1/route_samples_v2.jsonl"), b"{}\n")
            self.assertTrue(storage.exists("exports/e1/route_samples_v2.jsonl"))
            self.assertFalse(storage.exists("exports/e1/missing.json"))
            for bad in ("../escape", "/abs", "a//b", "a/../../b", "a\\\\b"):
                with self.subTest(key=bad), self.assertRaises(StorageError):
                    validate_key(bad)


class _MockSupabase:
    def __init__(self, *, fail_upload: bool = False):
        self.fail_upload = fail_upload
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.startswith("/storage/v1/object/pilot-private/"):
            if self.fail_upload:
                return httpx.Response(500, text="boom")
            return httpx.Response(200, json={"Key": path})
        if request.method == "GET" and path.startswith("/storage/v1/object/pilot-private/"):
            return httpx.Response(200, content=b"payload")
        if request.method == "HEAD":
            return httpx.Response(200) if "present" in path else httpx.Response(404)
        if request.method == "POST" and path.startswith("/storage/v1/object/list/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    def storage(self) -> SupabaseStorage:
        return SupabaseStorage(
            "https://projectref.supabase.co",
            "server-secret",
            "pilot-private",
            transport=httpx.MockTransport(self.handler),
        )


class SupabaseStorageTest(unittest.TestCase):
    def test_upload_download_exists_ping_headers(self):
        mock_backend = _MockSupabase()
        storage = mock_backend.storage()
        storage.put_bytes("exports/e1/a.jsonl", b"{}", "application/x-ndjson")
        self.assertEqual(storage.get_bytes("exports/e1/a.jsonl"), b"payload")
        self.assertTrue(storage.exists("exports/e1/present.json"))
        self.assertFalse(storage.exists("exports/e1/absent.json"))
        self.assertTrue(storage.ping())
        upload = mock_backend.requests[0]
        self.assertEqual(upload.headers["authorization"], "Bearer server-secret")
        self.assertEqual(upload.headers["apikey"], "server-secret")
        self.assertEqual(upload.headers["x-upsert"], "false")
        self.assertEqual(upload.headers["content-type"], "application/x-ndjson")
        self.assertIn("/storage/v1/object/pilot-private/exports/e1/a.jsonl", str(upload.url))


    def test_ping_rejects_unauthorized_storage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, request=request, text="unauthorized")

        storage = SupabaseStorage(
            "https://projectref.supabase.co",
            "server-secret",
            "pilot-private",
            transport=httpx.MockTransport(handler),
        )
        self.assertFalse(storage.ping())

    def test_upload_failure_raises(self):
        storage = _MockSupabase(fail_upload=True).storage()
        with self.assertRaises(StorageError):
            storage.put_bytes("exports/e1/a.jsonl", b"{}")


class ExportUploadRollbackTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._export_dir_snapshot = config_module.settings.export_dir
        object.__setattr__(config_module.settings, "export_dir", Path(self._tmp.name))
        reset_storage_cache()
        self.engine = create_engine(
            f"sqlite:///{Path(self._tmp.name) / 'export.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        object.__setattr__(config_module.settings, "export_dir", self._export_dir_snapshot)
        reset_storage_cache()
        self._tmp.cleanup()

    def test_failed_upload_leaves_no_export_record(self):
        class FailingStorage:
            def put_bytes(self, key, data, content_type=None):
                raise StorageError("simulated upload failure")

        with mock.patch.object(
            export_training_module, "get_storage", return_value=FailingStorage()
        ):
            with self.assertRaises(StorageError):
                export_training_module.export_training(self.db, {})
        count = self.db.scalar(select(func.count()).select_from(TrainingExport))
        self.assertEqual(count, 0, "a failed upload must not leave an export record")

    def test_local_export_writes_files_and_record(self):
        record = export_training_module.export_training(self.db, {})
        self.assertEqual(record.status, "ready")
        manifest = record.manifest_json or {}
        self.assertEqual(manifest["storage_prefix"], f"exports/{record.export_id}")
        export_path = Path(record.export_dir)
        self.assertTrue((export_path / "manifest.json").is_file())
        self.assertTrue((export_path / "checksums.txt").is_file())
        self.assertTrue((export_path / "route_samples_v2.jsonl").is_file())
        checksums = (export_path / "checksums.txt").read_text(encoding="utf-8")
        manifest_hash = __import__("hashlib").sha256(
            (export_path / "manifest.json").read_bytes()
        ).hexdigest()
        self.assertIn(manifest_hash, checksums)


class ExportDownloadEndpointTest(unittest.TestCase):
    def test_download_streams_manifest_file_and_rejects_traversal(self):
        import asyncio
        from fastapi import HTTPException

        from backend.app import export_file_api

        with tempfile.TemporaryDirectory() as tmp:
            snapshot = config_module.settings.export_dir
            object.__setattr__(config_module.settings, "export_dir", Path(tmp))
            reset_storage_cache()
            try:
                engine = create_engine(f"sqlite:///{Path(tmp) / 'db.sqlite3'}")
                Base.metadata.create_all(engine)
                session = sessionmaker(bind=engine, expire_on_commit=False)()
                try:
                    record = export_training_module.export_training(session, {})
                    response = export_file_api(
                        record.export_id,
                        "checksums.txt",
                        db=session,
                        actor={"user_id": "curator-1", "role": "curator"},
                    )
                    body = b"".join(asyncio.run(_collect(response.body_iterator)))
                    self.assertTrue(body)
                    self.assertIn("attachment", response.headers["content-disposition"])
                    with self.assertRaises(HTTPException) as caught:
                        export_file_api(
                            record.export_id,
                            "../checksums.txt",
                            db=session,
                            actor={"user_id": "curator-1", "role": "curator"},
                        )
                    self.assertEqual(caught.exception.status_code, 404)
                finally:
                    session.close()
                    engine.dispose()
            finally:
                object.__setattr__(config_module.settings, "export_dir", snapshot)
                reset_storage_cache()


async def _collect(async_iterator):
    chunks = []
    async for chunk in async_iterator:
        chunks.append(chunk)
    return chunks


if __name__ == "__main__":
    unittest.main()
