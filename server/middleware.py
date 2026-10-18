"""Request correlation and lifecycle logging middleware."""

from __future__ import annotations

import hashlib
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from utils.logger import get_logger, normalize_request_id, reset_request_id, set_request_id

logger = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"


def _resolve_request_id(header_value: str | None) -> str:
    normalized = normalize_request_id(header_value)
    if normalized:
        return normalized
    return str(uuid.uuid4())


def _hash_user_agent(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = _resolve_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        request_id_token = set_request_id(request_id)
        started = time.perf_counter()

        method = str(request.method or "").upper()
        path = str(request.url.path or "")
        client_ip = request.client.host if request.client else ""
        user_agent_hash = _hash_user_agent(request.headers.get("user-agent", ""))
        has_query = bool(request.url.query)

        logger.info(
            "HTTP request started",
            extra={
                "extra_fields": {
                    "event": "http.request.start",
                    "request_id": request_id,
                    "method": method,
                    "path": path,
                    "client_ip": client_ip,
                    "user_agent_hash": user_agent_hash,
                    "has_query_string": has_query,
                }
            },
        )

        response = None
        try:
            response = await call_next(request)
            return response
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.exception(
                "HTTP request raised unhandled exception",
                extra={
                    "extra_fields": {
                        "event": "http.request.exception",
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "duration_ms": duration_ms,
                        "status_code": 500,
                    }
                },
            )
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            status_code = int(getattr(response, "status_code", 500) or 500)
            if response is not None:
                response.headers[REQUEST_ID_HEADER] = request_id

            logger.info(
                "HTTP request completed",
                extra={
                    "extra_fields": {
                        "event": "http.request.complete",
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                    }
                },
            )
            reset_request_id(request_id_token)


class SecurityHeadersMiddleware:
    """Add baseline browser-hardening headers to every HTTP response.

    Pure ASGI (no response buffering), so streaming endpoints are unaffected.
    Headers already set by a route or reverse proxy are left untouched.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        https = scope.get("scheme") == "https"

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    # The app is never embedded by another site (clickjacking defense
                    # for the login form).
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"content-security-policy", b"frame-ancestors 'self'"),
                ]
                if https:
                    defaults.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                headers.extend((name, value) for name, value in defaults if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CredentialAwareCORSMiddleware:
    """CORS that only lets trusted origins make credentialed (cookie) requests.

    Every other origin still gets ``Access-Control-Allow-Origin: *`` for
    API-key/Bearer calls, but no ``Access-Control-Allow-Credentials``, so browsers
    refuse to expose cookie-authenticated responses to it. The React app calls
    the API on its own origin and never needs credentialed CORS; separately
    hosted frontends are trusted through ``AUTH_ALLOWED_ORIGINS`` or the origin
    of ``COGNITO_SUCCESS_REDIRECT_URI`` (and loopback outside production).
    """

    def __init__(self, app) -> None:
        from starlette.middleware.cors import CORSMiddleware

        # Only origins that pass is_trusted_browser_origin reach this instance, so
        # it may reflect the origin with credentials.
        self.trusted = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self.public = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            from server.auth_security import is_trusted_browser_origin

            origin = next(
                (value.decode("latin-1") for name, value in scope["headers"] if name == b"origin"),
                None,
            )
            if origin and is_trusted_browser_origin(origin):
                await self.trusted(scope, receive, send)
                return
        await self.public(scope, receive, send)
