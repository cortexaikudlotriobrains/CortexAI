"""Fail-fast validation for the server-side browser session schema."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from db.engine import get_engine
from db.tables import DB_SCHEMA

REQUIRED_AUTH_SESSION_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "token_hash",
        "auth_method",
        "created_at",
        "last_seen_at",
        "expires_at",
        "revoked_at",
        "revoked_reason",
    }
)


@dataclass
class AuthSchemaPreflightError(RuntimeError):
    """Raised when browser sessions are used before their migration is applied."""

    missing: tuple[str, ...]

    def __str__(self) -> str:
        details = ", ".join(self.missing)
        return (
            "Database schema is missing required browser-session objects: "
            f"{details}. Apply db/migrations/20261018_add_auth_sessions.sql "
            "before starting the API."
        )


def validate_auth_session_schema(
    *,
    engine: Engine | None = None,
    schema: str | None = None,
) -> None:
    """Require the ``auth_sessions`` table and its columns."""

    target_engine = engine or get_engine()
    target_schema = schema or DB_SCHEMA
    inspector = inspect(target_engine)
    if "auth_sessions" not in set(inspector.get_table_names(schema=target_schema)):
        raise AuthSchemaPreflightError((f"table {target_schema}.auth_sessions",))
    present = {str(c["name"]) for c in inspector.get_columns("auth_sessions", schema=target_schema)}
    missing = sorted(REQUIRED_AUTH_SESSION_COLUMNS - present)
    if missing:
        raise AuthSchemaPreflightError(
            tuple(f"column {target_schema}.auth_sessions.{name}" for name in missing)
        )
