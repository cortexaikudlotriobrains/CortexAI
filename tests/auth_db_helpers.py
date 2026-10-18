"""SQLite schema helpers for auth tests (users + auth_sessions)."""

from __future__ import annotations

from contextlib import suppress

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, Uuid, text


def reset_db_runtime_state(monkeypatch, *, schema_name: str = "main") -> None:
    import db.engine as db_engine
    import db.tables as db_tables

    with suppress(Exception):
        if db_engine._ENGINE is not None:
            db_engine._ENGINE.dispose()

    monkeypatch.setattr(db_engine, "_ENGINE", None)
    monkeypatch.setattr(db_tables, "DB_SCHEMA", schema_name)
    db_tables._tables_cache.clear()
    db_tables.metadata.clear()


def bootstrap_auth_sqlite_schema() -> None:
    import db.engine as db_engine

    engine = db_engine.get_engine()
    metadata = MetaData()
    random_id = text("(lower(hex(randomblob(16))))")
    Table(
        "users",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False, server_default=random_id),
        Column("email", String, unique=True),
        Column("display_name", String),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("auth_provider", String),
        Column("auth_subject", String),
        Column("auth_issuer", String),
    )
    Table(
        "auth_sessions",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False, server_default=random_id),
        Column("user_id", Uuid, nullable=False),
        Column("token_hash", String, nullable=False, unique=True),
        Column("auth_method", String, nullable=False),
        Column("created_at", DateTime(timezone=True)),
        Column("last_seen_at", DateTime(timezone=True)),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("revoked_at", DateTime(timezone=True)),
        Column("revoked_reason", String),
        Column("user_agent", String),
        Column("ip_hash", String),
    )
    Table(
        "auth_rate_limits",
        metadata,
        Column("bucket_key", String, primary_key=True),
        Column("window_start", DateTime(timezone=True), primary_key=True),
        Column("hits", Integer, nullable=False, default=0),
    )
    metadata.create_all(engine)

    # In-memory SQLite shares one connection (StaticPool): lazily reflecting a table
    # inside an open transaction would roll that transaction back. Warm the cache.
    from db.tables import get_table

    get_table("users")
    get_table("auth_sessions")
    get_table("auth_rate_limits")
