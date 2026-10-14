"""Supabase authentication for the hosted collector (W5).

Browser -> collector domain only: the backend proxies Supabase Auth and stores
the access/refresh tokens in ``HttpOnly; Secure; SameSite=Strict`` cookies.
Business roles live in ``pilot.app_users`` keyed by the verified JWT ``sub``;
the JWT ``role`` claim is always ``authenticated`` and is never trusted for
authorization.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import config as config_module
from .models import AppUser

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "laya_access_token"
REFRESH_COOKIE = "laya_refresh_token"
REQUESTED_WITH = "laya-collector"

# Every /api/* path is authenticated except these exact paths and the
# patient-token endpoints (the token in the URL is their credential).
PUBLIC_API_PATHS = frozenset(
    {
        "/api/health",
        "/api/health/ready",
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/logout",
    }
)
PUBLIC_API_PATTERN = re.compile(r"^/api/patient/session/[^/]+(?:/.*)?$")


class AuthenticationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def is_public_api_path(path: str) -> bool:
    return path in PUBLIC_API_PATHS or bool(PUBLIC_API_PATTERN.match(path))


def _now() -> float:
    return time.monotonic()


class InMemoryRateLimiter:
    """Single-instance limiter; if we ever run more workers this moves to Redis."""

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = _now()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if not hits:
                self._hits.pop(key, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = InMemoryRateLimiter(limit=10)
patient_limiter = InMemoryRateLimiter(limit=120)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        value = header[7:].strip()
        if value:
            return value
    return request.cookies.get(ACCESS_COOKIE)


def refresh_token_from_request(request: Request) -> str | None:
    return request.cookies.get(REFRESH_COOKIE)


# ---------------------------------------------------------------------------
# Token verification: JWKS (new projects) or legacy HS256 JWT secret.
# ---------------------------------------------------------------------------
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}
_JWKS_LOCK = threading.Lock()


def _jwks_client(supabase_url: str) -> PyJWKClient:
    with _JWKS_LOCK:
        client = _JWKS_CLIENTS.get(supabase_url)
        if client is None:
            client = PyJWKClient(
                f"{supabase_url}/auth/v1/.well-known/jwks.json",
                cache_keys=True,
                lifespan=600,
                timeout=5,
            )
            _JWKS_CLIENTS[supabase_url] = client
        return client


def clear_jwks_cache() -> None:
    with _JWKS_LOCK:
        _JWKS_CLIENTS.clear()


def verify_supabase_token(token: str, config=None) -> dict[str, Any]:
    """Verify signature, exp, aud and iss; return the JWT claims."""
    config = config or config_module.settings
    if not config.supabase_url:
        raise AuthenticationError(500, "SUPABASE_URL is not configured")
    issuer = f"{config.supabase_url}/auth/v1"
    common = {
        "audience": "authenticated",
        "issuer": issuer,
        "options": {"require": ["exp", "sub"], "verify_aud": True, "verify_iss": True},
    }
    try:
        if config.supabase_jwt_secret:
            # Legacy Supabase projects sign user tokens with the project JWT
            # secret (HS256).  Keep this path only for those projects.
            return jwt.decode(token, config.supabase_jwt_secret, algorithms=["HS256"], **common)
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg", "")
        if algorithm not in {"RS256", "ES256"}:
            raise AuthenticationError(401, "unsupported token algorithm")
        signing_key = _jwks_client(config.supabase_url).get_signing_key_from_jwt(token).key
        return jwt.decode(token, signing_key, algorithms=[algorithm], **common)
    except AuthenticationError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(401, "access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(401, "invalid access token") from exc
    except httpx.HTTPError as exc:
        raise AuthenticationError(503, "JWKS endpoint unavailable") from exc


def load_actor(claims: dict[str, Any], config=None) -> dict[str, str | None] | None:
    """Join the verified ``sub`` against pilot.app_users (403 if missing/off)."""
    from . import db as db_module

    sub = claims.get("sub")
    if not sub:
        raise AuthenticationError(401, "access token has no subject")
    with db_module.SessionLocal() as session:
        user = session.get(AppUser, str(sub))
        if user is None:
            raise AuthenticationError(403, "account is not provisioned for this pilot")
        if not user.active:
            raise AuthenticationError(403, "account is disabled")
        return {
            "user_id": user.pilot_user_id,
            "role": user.role,
            "site_id": user.site_id,
            "auth_user_id": user.auth_user_id,
        }


class SupabaseAuthMiddleware(BaseHTTPMiddleware):
    """Gate every /api/* route before routing/body parsing.

    Protected routes receive 401 without a token and 403 when the verified
    Supabase identity has no active ``pilot.app_users`` row.
    """

    async def dispatch(self, request: Request, call_next):
        config = config_module.settings
        path = request.url.path
        if config.auth_mode != "supabase" or not path.startswith("/api/"):
            return await call_next(request)
        public = is_public_api_path(path)
        actor: dict[str, str | None] | None = None
        token = bearer_token(request)
        if token:
            try:
                claims = verify_supabase_token(token, config)
                actor = load_actor(claims, config)
            except AuthenticationError as exc:
                if not public:
                    return _error_response(exc.status_code, exc.detail)
                actor = None
            except Exception:  # noqa: BLE001 - never leak verification internals
                if not public:
                    return _error_response(401, "invalid access token")
                actor = None
        elif not public:
            return _error_response(401, "authentication required")
        # W5.4: SameSite=Strict already blocks cross-site cookie use; requiring
        # this header on every unsafe /api call is defence in depth against
        # CSRF (and non-browser clients using only the session cookie).  The
        # 401 above wins for unauthenticated protected routes so the authz
        # enumeration test stays unambiguous.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("x-requested-with") != REQUESTED_WITH:
                return _error_response(403, f"missing X-Requested-With: {REQUESTED_WITH}")
        request.state.actor = actor
        return await call_next(request)


def _error_response(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


# ---------------------------------------------------------------------------
# Supabase Auth REST proxy (server-side only; the publishable key is required
# by the Auth API, the secret key is never used for password login).
# ---------------------------------------------------------------------------
def _auth_headers(config=None) -> dict[str, str]:
    config = config or config_module.settings
    headers = {"Content-Type": "application/json"}
    if config.supabase_publishable_key:
        headers["apikey"] = config.supabase_publishable_key
    return headers


def _auth_url(config, path: str) -> str:
    if not config.supabase_url:
        raise AuthenticationError(500, "SUPABASE_URL is not configured")
    return f"{config.supabase_url}/auth/v1/{path}"


def supabase_password_login(email: str, password: str, config=None) -> dict[str, Any]:
    config = config or config_module.settings
    try:
        response = httpx.post(
            _auth_url(config, "token?grant_type=password"),
            headers=_auth_headers(config),
            json={"email": email, "password": password},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise AuthenticationError(503, "authentication service unavailable") from exc
    if response.status_code != 200:
        # Deliberately identical for unknown user / wrong password.
        raise AuthenticationError(401, "invalid email or password")
    return response.json()


def supabase_refresh(refresh_token: str, config=None) -> dict[str, Any]:
    config = config or config_module.settings
    try:
        response = httpx.post(
            _auth_url(config, "token?grant_type=refresh_token"),
            headers=_auth_headers(config),
            json={"refresh_token": refresh_token},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise AuthenticationError(503, "authentication service unavailable") from exc
    if response.status_code != 200:
        raise AuthenticationError(401, "refresh token rejected")
    return response.json()


def supabase_logout(access_token: str | None, config=None) -> None:
    config = config or config_module.settings
    if not access_token or not config.supabase_url:
        return
    headers = _auth_headers(config)
    headers["Authorization"] = f"Bearer {access_token}"
    try:
        httpx.post(_auth_url(config, "logout"), headers=headers, timeout=10)
    except httpx.HTTPError:
        # The local cookies are cleared regardless; logout is best effort.
        logger.warning("supabase logout call failed")


def set_auth_cookies(response: Response, tokens: dict[str, Any]) -> None:
    access_token = str(tokens.get("access_token") or "")
    refresh_token = str(tokens.get("refresh_token") or "")
    expires_in = int(tokens.get("expires_in") or 3600)
    if not access_token or not refresh_token:
        raise AuthenticationError(502, "authentication service returned an incomplete token set")
    response.set_cookie(
        ACCESS_COOKIE,
        access_token,
        max_age=expires_in,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    # Supabase refresh tokens last much longer than the access token; 30 days
    # matches the default project refresh-token lifetime.
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie(REFRESH_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
