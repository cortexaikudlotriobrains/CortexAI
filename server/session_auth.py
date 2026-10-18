"""Server-side browser sessions.

The ``cortex_session`` cookie carries a random opaque token. Only its keyed
hash (HMAC-SHA256 with ``SESSION_SECRET``) is stored in ``auth_sessions``, so a
database read cannot be replayed as a login, every session expires on the
server, and any session can be revoked (logout, password reset, disabled user).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from utils.logger import get_logger

logger = get_logger(__name__)

SESSION_COOKIE_NAME = "cortex_session"

# Auth methods allowed by ck_auth_sessions_method.
AUTH_METHODS = frozenset({"password", "google", "cognito_token", "dev"})

_DEV_DEFAULT_SECRET = "dev-session-secret"
_MIN_PRODUCTION_SECRET_LENGTH = 32
_PRODUCTION_ENV_VALUES = {"prod", "production"}
_DEFAULT_SESSION_MAX_AGE = 7 * 24 * 3600
_NON_PERSISTENT_SESSION_LIFETIME = 12 * 3600
_LAST_SEEN_INTERVAL = timedelta(minutes=5)
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def runtime_environment() -> str:
    for key in ("APP_ENV", "ENVIRONMENT", "ENV"):
        value = str(os.getenv(key, "") or "").strip().lower()
        if value:
            return value
    return ""


def is_production_runtime() -> bool:
    return runtime_environment() in _PRODUCTION_ENV_VALUES


def session_secret() -> str:
    return str(os.getenv("SESSION_SECRET") or "").strip() or _DEV_DEFAULT_SECRET


def validate_session_secret_config() -> None:
    """Refuse to start a production runtime with a guessable session secret."""
    if not is_production_runtime():
        return
    secret = session_secret()
    if secret == _DEV_DEFAULT_SECRET or len(secret) < _MIN_PRODUCTION_SECRET_LENGTH:
        raise RuntimeError(
            "SESSION_SECRET must be set to a random value of at least "
            f"{_MIN_PRODUCTION_SECRET_LENGTH} characters in production."
        )


def session_max_age_seconds() -> int | None:
    """Cookie lifetime. ``None`` means a browser-session cookie."""
    raw = str(os.getenv("SESSION_MAX_AGE_SECONDS", "") or "").strip()
    if not raw:
        return _DEFAULT_SESSION_MAX_AGE
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_SESSION_MAX_AGE
    return value if value > 0 else None


def _server_lifetime() -> timedelta:
    max_age = session_max_age_seconds()
    return timedelta(seconds=max_age if max_age else _NON_PERSISTENT_SESSION_LIFETIME)


def hash_value(value: str) -> str:
    """Keyed hash for tokens and low-entropy identifiers stored at rest."""
    return hmac.new(
        session_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def issue_session(
    db: Session,
    *,
    user_id: UUID,
    auth_method: str,
    user_agent: str | None = None,
    client_ip: str | None = None,
) -> str:
    """Create a session row and return the cookie token. Caller commits."""
    from db.tables import get_table

    if auth_method not in AUTH_METHODS:
        raise ValueError(f"Unsupported auth method: {auth_method}")
    token = secrets.token_urlsafe(32)
    now = _now()
    table = get_table("auth_sessions")
    db.execute(
        table.insert().values(
            user_id=user_id,
            token_hash=hash_value(token),
            auth_method=auth_method,
            created_at=now,
            last_seen_at=now,
            expires_at=now + _server_lifetime(),
            user_agent=(user_agent or "")[:256] or None,
            ip_hash=hash_value(client_ip) if client_ip else None,
        )
    )
    return token


def parse_session(cookie_value: str | None) -> UUID | None:
    """Return the user for a live session cookie, else None (fail closed)."""
    if not cookie_value or not _TOKEN_PATTERN.match(cookie_value):
        return None
    try:
        from db.session import SessionLocal
        from db.tables import get_table

        sessions = get_table("auth_sessions")
        users = get_table("users")
        now = _now()
        db = SessionLocal()
        try:
            row = db.execute(
                select(sessions.c.id, sessions.c.user_id, sessions.c.last_seen_at)
                .select_from(sessions.join(users, users.c.id == sessions.c.user_id))
                .where(
                    and_(
                        sessions.c.token_hash == hash_value(cookie_value),
                        sessions.c.revoked_at.is_(None),
                        sessions.c.expires_at > now,
                        users.c.is_active.is_(True),
                    )
                )
            ).first()
            if row is None:
                return None
            if now - _as_utc(row.last_seen_at) >= _LAST_SEEN_INTERVAL:
                db.execute(update(sessions).where(sessions.c.id == row.id).values(last_seen_at=now))
                db.commit()
            return row.user_id
        finally:
            db.close()
    except Exception:
        logger.exception(
            "Session lookup failed; treating request as unauthenticated",
            extra={"extra_fields": {"event": "auth.session.lookup_failed"}},
        )
        return None


def revoke_session(cookie_value: str | None, *, reason: str = "logout") -> str | None:
    """Revoke the session behind a cookie. Returns its auth method, else None."""
    if not cookie_value or not _TOKEN_PATTERN.match(cookie_value):
        return None
    from db.session import SessionLocal
    from db.tables import get_table

    sessions = get_table("auth_sessions")
    db = SessionLocal()
    try:
        row = db.execute(
            update(sessions)
            .where(
                and_(
                    sessions.c.token_hash == hash_value(cookie_value),
                    sessions.c.revoked_at.is_(None),
                )
            )
            .values(revoked_at=_now(), revoked_reason=reason[:64])
            .returning(sessions.c.auth_method)
        ).first()
        db.commit()
        return str(row[0]) if row else None
    except Exception:
        db.rollback()
        logger.exception(
            "Session revocation failed",
            extra={"extra_fields": {"event": "auth.session.revoke_failed"}},
        )
        raise
    finally:
        db.close()


def revoke_user_sessions(db: Session, user_id: UUID, *, reason: str) -> int:
    """Revoke every live session of a user (password reset, disable). Caller commits."""
    from db.tables import get_table

    sessions = get_table("auth_sessions")
    result: Any = db.execute(
        update(sessions)
        .where(and_(sessions.c.user_id == user_id, sessions.c.revoked_at.is_(None)))
        .values(revoked_at=_now(), revoked_reason=reason[:64])
    )
    return int(result.rowcount or 0)
