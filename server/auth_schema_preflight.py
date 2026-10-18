"""Fail-fast validation for the server-side browser session schema."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from db.engine import get_engine
from db.tables import DB_SCHEMA

REQUIRED_AUTH_RATE_LIMIT_COLUMNS = frozenset({"bucket_key", "window_start", "hits"})

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
    """Require the ``auth_sessions`` and ``auth_rate_limits`` tables and their columns."""

    target_engine = engine or get_engine()
    target_schema = schema or DB_SCHEMA
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names(schema=target_schema))
    missing: list[str] = []
    for table, required in (
        ("auth_sessions", REQUIRED_AUTH_SESSION_COLUMNS),
        ("auth_rate_limits", REQUIRED_AUTH_RATE_LIMIT_COLUMNS),
    ):
        if table not in tables:
            missing.append(f"table {target_schema}.{table}")
            continue
        present = {str(c["name"]) for c in inspector.get_columns(table, schema=target_schema)}
        missing.extend(
            f"column {target_schema}.{table}.{name}" for name in sorted(required - present)
        )
    if missing:
        raise AuthSchemaPreflightError(tuple(missing))
