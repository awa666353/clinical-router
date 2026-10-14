"""Object storage backends for exports and doctor-round documents (W4).

Two implementations behind a tiny interface:

* ``LocalStorage`` keeps the development/test behaviour (files under EXPORT_DIR).
* ``SupabaseStorage`` calls the Storage REST API with the server-only secret
  key.  No ``supabase-py`` dependency is added.

Keys are always relative POSIX paths such as
``exports/<export_id>/route_samples_v2.jsonl``; they are validated against
traversal before any filesystem or HTTP call.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from .config import settings

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class StorageError(RuntimeError):
    """Raised when an object cannot be written, read or reached."""


def validate_key(key: str) -> str:
    if not key or key.startswith("/") or "\\" in key or "//" in key:
        raise StorageError(f"invalid storage key: {key!r}")
    if ".." in key.split("/"):
        raise StorageError(f"invalid storage key: {key!r}")
    if not _KEY_RE.match(key):
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def ping(self) -> bool: ...


class LocalStorage:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.base_dir / key).resolve()
        base = self.base_dir.resolve()
        if base != path and base not in path.parents:
            raise StorageError(f"storage key escapes EXPORT_DIR: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise StorageError(f"object not found: {key}")
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def ping(self) -> bool:
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            return False


class SupabaseStorage:
    def __init__(
        self,
        base_url: str,
        secret_key: str,
        bucket: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url or not secret_key or not bucket:
            raise StorageError("Supabase storage requires SUPABASE_URL, SUPABASE_SECRET_KEY and STORAGE_BUCKET")
        self.base_url = base_url.rstrip("/")
        self.secret_key = secret_key
        self.bucket = bucket
        self.timeout = timeout
        self.transport = transport

    # -- helpers ----------------------------------------------------------
    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "apikey": self.secret_key,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _object_url(self, key: str) -> str:
        return f"{self.base_url}/storage/v1/object/{self.bucket}/{quote(key, safe='/')}"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, transport=self.transport)

    def _raise_for_status(self, response: httpx.Response, action: str) -> None:
        if response.status_code >= 300:
            raise StorageError(
                f"supabase storage {action} failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

    # -- interface --------------------------------------------------------
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        validate_key(key)
        with self._client() as client:
            response = client.post(
                self._object_url(key),
                content=data,
                headers={**self._headers(content_type or "application/octet-stream"), "x-upsert": "false"},
            )
        self._raise_for_status(response, "upload")
        return key

    def get_bytes(self, key: str) -> bytes:
        validate_key(key)
        with self._client() as client:
            response = client.get(self._object_url(key), headers=self._headers())
        if response.status_code == 404:
            raise StorageError(f"object not found: {key}")
        self._raise_for_status(response, "download")
        return response.content

    def exists(self, key: str) -> bool:
        validate_key(key)
        with self._client() as client:
            response = client.head(self._object_url(key), headers=self._headers())
        if response.status_code == 404:
            return False
        self._raise_for_status(response, "stat")
        return True

    def ping(self) -> bool:
        url = f"{self.base_url}/storage/v1/object/list/{self.bucket}"
        try:
            with self._client() as client:
                response = client.post(url, headers=self._headers("application/json"), json={"prefix": "", "limit": 1})
        except httpx.HTTPError:
            return False
        # A 4xx (for example a missing bucket or invalid secret) is not a
        # healthy storage backend. Returning True for 401/404 made the
        # readiness endpoint report OK while exports would still fail.
        return 200 <= response.status_code < 300


_storage_cache: Storage | None = None


def build_storage(config=None) -> Storage:
    config = config or settings
    if config.storage_backend == "supabase":
        return SupabaseStorage(
            config.supabase_url,
            config.supabase_secret_key,
            config.storage_bucket,
        )
    return LocalStorage(config.export_dir)


def get_storage(config=None) -> Storage:
    global _storage_cache
    if _storage_cache is None or config is not None:
        return build_storage(config)
    _storage_cache = build_storage()
    return _storage_cache


def reset_storage_cache() -> None:
    global _storage_cache
    _storage_cache = None
