"""Security helpers for the in-app login endpoints.

Covers request-origin checks, brute-force throttles, open-redirect-safe ``next``
targets, the signed one-shot OAuth transaction cookie (state + PKCE verifier +
nonce) and the sign-up password policy.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from server.rate_limit import InMemoryRateLimiter
from server.session_auth import hash_value, is_production_runtime, session_secret

OAUTH_TX_COOKIE_NAME = "cortex_oauth_tx"
OAUTH_TX_TTL_SECONDS = 600

_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
_CODE_PATTERN = re.compile(r"^\d{6}$")
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 128
_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "password12345",
        "123456789012",
        "qwertyuiop12",
        "letmein12345",
        "welcome12345",
        "iloveyou1234",
        "administrator",
    }
)
_BLOCKED_NEXT_PREFIXES = ("/login", "/auth", "/v1/")


def error_detail(code: str, message: str, **extra: object) -> dict[str, object]:
    return {"code": code, "message": message, **extra}


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #


def normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if len(email) > 254 or not _EMAIL_PATTERN.match(email):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("invalid_email", "Enter a valid email address."),
        )
    return email


def validate_code(value: str) -> str:
    code = (value or "").strip()
    if not _CODE_PATTERN.match(code):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("invalid_code", "Enter the 6-digit code from your email."),
        )
    return code


def validate_new_password(password: str, *, email: str) -> None:
    """Length-first policy (NIST 800-63B); Cognito's pool policy still applies."""
    problem: str | None = None
    if len(password) < _MIN_PASSWORD_LENGTH:
        problem = f"Use at least {_MIN_PASSWORD_LENGTH} characters."
    elif len(password) > _MAX_PASSWORD_LENGTH:
        problem = f"Use at most {_MAX_PASSWORD_LENGTH} characters."
    elif len(set(password)) < 5:
        problem = "Choose a less repetitive password."
    else:
        lowered = password.lower()
        local_part = email.split("@", 1)[0]
        if lowered in _COMMON_PASSWORDS:
            problem = "That password is too common."
        elif email in lowered or (len(local_part) >= 4 and local_part in lowered):
            problem = "Do not include your email address in your password."
    if problem:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail("weak_password", problem),
        )


def validate_login_password(password: str) -> None:
    if not password or len(password) > _MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail("invalid_credentials", "Incorrect email or password."),
        )


# --------------------------------------------------------------------------- #
# Origin check (CSRF defense in depth on top of SameSite=Lax)
# --------------------------------------------------------------------------- #


def _normalize_origin(value: str) -> str:
    parts = urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def _allowed_origins(request: Request) -> set[str]:
    allowed = {_normalize_origin(str(request.base_url))}
    for raw in (os.getenv("AUTH_ALLOWED_ORIGINS") or "").split(","):
        normalized = _normalize_origin(raw)
        if normalized:
            allowed.add(normalized)
    return allowed


def _is_loopback_origin(origin: str) -> bool:
    host = urlsplit(origin).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def require_same_origin(request: Request) -> None:
    """Reject browser requests that originate from another site.

    Browsers always send ``Origin`` on cross-site POSTs, so a missing header means
    a non-browser client, which has no ambient cookies to abuse.
    """
    origin = request.headers.get("origin")
    if origin is None:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            raise _forbidden_origin()
        return
    normalized = _normalize_origin(origin)
    if normalized in _allowed_origins(request):
        return
    if not is_production_runtime() and normalized and _is_loopback_origin(normalized):
        return
    raise _forbidden_origin()


def _forbidden_origin() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=error_detail("origin_not_allowed", "This request is not allowed."),
    )


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #

_IP_LIMITER = InMemoryRateLimiter(window_seconds=60)
_IDENTITY_LIMITER = InMemoryRateLimiter(window_seconds=300)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_auth_rate_limit(request: Request, *, action: str, identity: str | None = None) -> None:
    """Throttle per client IP and, when given, per identity (hashed email).

    Limits are per API process; Cognito applies its own account-level lockouts.
    ``AUTH_RATE_LIMIT_IP_PER_MINUTE`` / ``AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN``
    tune the limits; 0 disables a limit.
    """
    ip_limit = _int_env("AUTH_RATE_LIMIT_IP_PER_MINUTE", 20)
    identity_limit = _int_env("AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN", 8)
    checks: list[tuple[InMemoryRateLimiter, str, int]] = []
    if ip_limit:
        checks.append((_IP_LIMITER, f"{action}:ip:{hash_value(client_ip(request))}", ip_limit))
    if identity and identity_limit:
        checks.append((_IDENTITY_LIMITER, f"{action}:id:{hash_value(identity)}", identity_limit))
    for limiter, key, limit in checks:
        allowed, retry_after = limiter.check_and_record(key, limit=limit)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_detail(
                    "rate_limited",
                    "Too many attempts. Please wait a few minutes and try again.",
                    retry_after_seconds=retry_after,
                ),
                headers={"Retry-After": str(retry_after)},
            )


def reset_auth_rate_limits() -> None:
    _IP_LIMITER.reset()
    _IDENTITY_LIMITER.reset()


# --------------------------------------------------------------------------- #
# Redirect targets
# --------------------------------------------------------------------------- #


def sanitize_next_path(value: str | None) -> str:
    """Return a same-origin absolute path, or ``/``. Never an external URL."""
    candidate = (value or "").strip()
    if not candidate or len(candidate) > 512:
        return "/"
    if not candidate.startswith("/") or candidate.startswith(("//", "/\\")):
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch == "\\" for ch in candidate):
        return "/"
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return "/"
    if parts.path.startswith(_BLOCKED_NEXT_PREFIXES):
        return "/"
    return candidate


# --------------------------------------------------------------------------- #
# OAuth transaction cookie (state + PKCE + nonce), signed and short-lived
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OAuthTransaction:
    state: str
    code_verifier: str
    nonce: str
    next_path: str


def _sign(payload: bytes) -> str:
    return hmac.new(session_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_oauth_transaction(next_path: str) -> OAuthTransaction:
    return OAuthTransaction(
        state=secrets.token_urlsafe(32),
        code_verifier=secrets.token_urlsafe(64),
        nonce=secrets.token_urlsafe(24),
        next_path=sanitize_next_path(next_path),
    )


def encode_oauth_transaction(tx: OAuthTransaction) -> str:
    body = json.dumps(
        {
            "s": tx.state,
            "v": tx.code_verifier,
            "n": tx.nonce,
            "p": tx.next_path,
            "e": int(time.time()) + OAUTH_TX_TTL_SECONDS,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    # Unpadded base64url keeps the cookie value free of characters that need quoting.
    return f"{base64.urlsafe_b64encode(body).rstrip(b'=').decode('ascii')}.{_sign(body)}"


def decode_oauth_transaction(cookie_value: str | None) -> OAuthTransaction | None:
    if not cookie_value or "." not in cookie_value or len(cookie_value) > 2048:
        return None
    encoded, signature = cookie_value.rsplit(".", 1)
    try:
        body = base64.urlsafe_b64decode(encoded.encode("ascii") + b"=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(body)):
            return None
        data = json.loads(body.decode("utf-8"))
        if int(data["e"]) < time.time():
            return None
        return OAuthTransaction(
            state=str(data["s"]),
            code_verifier=str(data["v"]),
            nonce=str(data["n"]),
            next_path=sanitize_next_path(str(data.get("p", "/"))),
        )
    except Exception:
        return None


def states_match(expected: str, provided: str | None) -> bool:
    return bool(provided) and hmac.compare_digest(expected, str(provided))
