"""Security helpers for the in-app login endpoints.

Covers trusted browser origins (auth POSTs and credentialed CORS), brute-force
throttles shared across API instances, open-redirect-safe ``next`` targets, the
signed one-shot OAuth transaction cookie (state + PKCE verifier + nonce), the
sign-up password policy including a breached-password check, and auth audit logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from server.session_auth import (
    hash_value,
    is_production_runtime,
    session_cookie_secure,
    session_secret,
)
from utils.logger import get_logger

logger = get_logger(__name__)

OAUTH_TX_COOKIE_NAME = "cortex_oauth_tx"
SECURE_OAUTH_TX_COOKIE_NAME = "__Host-cortex_oauth_tx"
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


def trusted_browser_origins() -> set[str]:
    """Origins (besides the API's own) that may send cookies to the API.

    ``AUTH_ALLOWED_ORIGINS`` plus the origin of ``COGNITO_SUCCESS_REDIRECT_URI``
    for a separately hosted frontend. Used for auth POSTs and credentialed CORS.
    """
    origins: set[str] = set()
    for raw in (os.getenv("AUTH_ALLOWED_ORIGINS") or "").split(","):
        normalized = _normalize_origin(raw)
        if normalized:
            origins.add(normalized)
    success = _normalize_origin(os.getenv("COGNITO_SUCCESS_REDIRECT_URI") or "")
    if success.startswith(("http://", "https://")):
        origins.add(success)
    return origins


# Any port on a loopback host; only trusted outside production.
LOOPBACK_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


def is_trusted_browser_origin(origin: str) -> bool:
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    if normalized in trusted_browser_origins():
        return True
    return not is_production_runtime() and re.match(LOOPBACK_ORIGIN_REGEX, normalized) is not None


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
    if _normalize_origin(origin) == _normalize_origin(str(request.base_url)):
        return
    if is_trusted_browser_origin(origin):
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

_IP_WINDOW_SECONDS = 60
_IDENTITY_WINDOW_SECONDS = 300
_MEMORY_MAX_BUCKETS = 50_000


class _FixedWindowCounter:
    """Bounded in-memory fallback used only when the shared table is unavailable."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, int], int] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, window_start: int, oldest_live: int) -> int:
        with self._lock:
            if len(self._counts) >= _MEMORY_MAX_BUCKETS:
                self._counts = {k: v for k, v in self._counts.items() if k[1] >= oldest_live}
                if len(self._counts) >= _MEMORY_MAX_BUCKETS:
                    self._counts.clear()
            bucket = (key, window_start)
            self._counts[bucket] = self._counts.get(bucket, 0) + 1
            return self._counts[bucket]

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


_MEMORY_COUNTER = _FixedWindowCounter()
_DB_FALLBACK_WARNED = False


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _db_hit(bucket_key: str, window_start: datetime) -> int:
    """Atomically count one hit in the shared ``auth_rate_limits`` table."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from db.session import SessionLocal
    from db.tables import get_table

    table = get_table("auth_rate_limits")
    db = SessionLocal()
    try:
        insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
        stmt = insert(table).values(bucket_key=bucket_key, window_start=window_start, hits=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["bucket_key", "window_start"],
            set_={"hits": table.c.hits + 1},
        ).returning(table.c.hits)
        hits = int(db.execute(stmt).scalar_one())
        db.commit()
        return hits
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _hit(key: str, window_seconds: int) -> tuple[int, int]:
    """Return (hits in the current window, seconds until it resets)."""
    global _DB_FALLBACK_WARNED
    now = time.time()
    start = int(now // window_seconds) * window_seconds
    retry_after = max(1, int(start + window_seconds - now))
    bucket_key = hash_value(key)
    try:
        hits = _db_hit(bucket_key, datetime.fromtimestamp(start, tz=timezone.utc))
    except Exception:
        if not _DB_FALLBACK_WARNED:
            _DB_FALLBACK_WARNED = True
            logger.warning(
                "Shared auth rate-limit table unavailable; using per-process limits",
                exc_info=True,
                extra={"extra_fields": {"event": "auth.rate_limit.db_unavailable"}},
            )
        hits = _MEMORY_COUNTER.hit(bucket_key, start, start - _IDENTITY_WINDOW_SECONDS)
    return hits, retry_after


def enforce_auth_rate_limit(request: Request, *, action: str, identity: str | None = None) -> None:
    """Throttle per client IP and, when given, per identity (hashed email).

    Counters live in the shared ``auth_rate_limits`` table so the limits hold
    across every API instance (per-process fallback if the table is unavailable);
    Cognito adds its own account-level lockouts. ``AUTH_RATE_LIMIT_IP_PER_MINUTE``
    / ``AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN`` tune the limits; 0 disables a limit.
    """
    ip_limit = _int_env("AUTH_RATE_LIMIT_IP_PER_MINUTE", 20)
    identity_limit = _int_env("AUTH_RATE_LIMIT_IDENTITY_PER_5_MIN", 8)
    checks: list[tuple[str, int, int]] = []
    if ip_limit:
        checks.append((f"{action}:ip:{client_ip(request)}", ip_limit, _IP_WINDOW_SECONDS))
    if identity and identity_limit:
        checks.append((f"{action}:id:{identity}", identity_limit, _IDENTITY_WINDOW_SECONDS))
    for key, limit, window in checks:
        hits, retry_after = _hit(key, window)
        if hits > limit:
            audit_event(
                request,
                "auth.rate_limited",
                action=action,
                email=identity,
                scope="identity" if ":id:" in key else "ip",
            )
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
    """Test helper: clear in-memory counters (database counters live per test DB)."""
    global _DB_FALLBACK_WARNED
    _MEMORY_COUNTER.reset()
    _DB_FALLBACK_WARNED = False


def purge_rate_limit_windows(db, *, older_than: timedelta) -> int:
    """Delete counter windows that ended long ago. Caller commits."""
    from sqlalchemy import delete

    from db.tables import get_table

    table = get_table("auth_rate_limits")
    cutoff = datetime.now(timezone.utc) - older_than
    result = db.execute(delete(table).where(table.c.window_start < cutoff))
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Breached passwords (Have I Been Pwned, k-anonymity range API)
# --------------------------------------------------------------------------- #

_PWNED_RANGE_URL = "https://api.pwnedpasswords.com/range/"


def _breach_check_enabled() -> bool:
    raw = str(os.getenv("AUTH_BREACHED_PASSWORD_CHECK", "true") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def password_breach_count(password: str) -> int | None:
    """How often a password appears in known breaches; None if the check could not run.

    Only the first 5 hex characters of the SHA-1 leave the server (k-anonymity), and
    ``Add-Padding`` hides the response size. Fails open: an outage never blocks sign-up.
    """
    if not _breach_check_enabled():
        return None
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    request = urllib.request.Request(
        _PWNED_RANGE_URL + prefix,
        headers={"Add-Padding": "true", "User-Agent": "CortexAI-password-check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning(
            "Breached-password check unavailable; continuing without it",
            extra={
                "extra_fields": {
                    "event": "auth.breach_check.unavailable",
                    "error_type": type(exc).__name__,
                }
            },
        )
        return None
    for line in body.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate == suffix:
            try:
                return int(count)
            except ValueError:
                return None
    return 0


def reject_breached_password(password: str) -> None:
    count = password_breach_count(password)
    if count:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=error_detail(
                "breached_password",
                "This password has appeared in a data breach. Choose a different one.",
            ),
        )


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


def audit_event(
    request: Request,
    event: str,
    *,
    email: str | None = None,
    user_id: object | None = None,
    **fields: object,
) -> None:
    """Structured auth audit record. Email and IP are keyed hashes, never raw values.

    Search logs for ``event`` values starting with ``auth.`` to build alerts
    (failed-login spikes per ``email_hash``/``ip_hash``, sign-up bursts).
    """
    record: dict[str, object] = {
        "event": event,
        "ip_hash": hash_value(client_ip(request)),
        "request_id": getattr(request.state, "request_id", None),
        **fields,
    }
    if email:
        record["email_hash"] = hash_value(email)
    if user_id is not None:
        record["user_id"] = str(user_id)
    logger.info("Auth audit event", extra={"extra_fields": record})


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


def oauth_tx_cookie_name(request: Request) -> str:
    return SECURE_OAUTH_TX_COOKIE_NAME if session_cookie_secure(request) else OAUTH_TX_COOKIE_NAME


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
