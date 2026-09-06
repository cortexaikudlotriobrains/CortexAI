from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text

from server.app import create_app
from server.billing.reservation_cleanup import ReservationCleanupStats
from server.billing.schema_preflight import (
    BillingSchemaPreflightError,
    REQUIRED_BILLING_SCHEMA,
    validate_billing_schema,
)


def test_schema_preflight_requires_grants_and_period_source(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE usage_periods (id TEXT)"))
    monkeypatch.setattr(
        "server.billing.schema_preflight.REQUIRED_BILLING_SCHEMA",
        {name: REQUIRED_BILLING_SCHEMA[name] for name in ("subscription_grants", "usage_periods")},
    )
    with pytest.raises(BillingSchemaPreflightError) as exc:
        validate_billing_schema(engine=engine, schema="main")
    assert "table main.subscription_grants" in exc.value.missing
    assert "column main.usage_periods.subscription_grant_id" in exc.value.missing


def test_schema_preflight_reports_missing_credit_transactions(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(
        "server.billing.schema_preflight.REQUIRED_BILLING_SCHEMA",
        {"credit_transactions": frozenset({"id", "total_credits"})},
    )

    with pytest.raises(BillingSchemaPreflightError) as exc_info:
        validate_billing_schema(engine=engine, schema="main")

    assert exc_info.value.missing == ("table main.credit_transactions",)
    assert "Apply db migrations in filename order" in str(exc_info.value)


def test_schema_preflight_accepts_required_credit_table(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE credit_transactions "
                "(id TEXT PRIMARY KEY, total_credits INTEGER NOT NULL)"
            )
        )
    monkeypatch.setattr(
        "server.billing.schema_preflight.REQUIRED_BILLING_SCHEMA",
        {"credit_transactions": frozenset({"id", "total_credits"})},
    )

    validate_billing_schema(engine=engine, schema="main")


def test_postgres_startup_fails_before_serving_when_billing_schema_is_missing(
    monkeypatch,
):
    from server.billing import schema_preflight

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("CORTEX_WORK_ENABLED", "false")
    monkeypatch.setenv("ENABLE_ATTACHMENTS_CLEANUP_WORKER", "false")
    monkeypatch.setenv("ENABLE_BILLING_RESERVATION_CLEANUP_WORKER", "false")
    monkeypatch.setattr(
        schema_preflight,
        "validate_billing_schema",
        lambda: (_ for _ in ()).throw(
            BillingSchemaPreflightError(("table public.credit_transactions",))
        ),
    )

    with pytest.raises(
        BillingSchemaPreflightError,
        match=r"table public\.credit_transactions",
    ):
        with TestClient(create_app()):
            pass


def test_postgres_startup_runs_one_reservation_cleanup_cycle(monkeypatch):
    from server.billing import reservation_cleanup, schema_preflight

    cleanup_calls: list[int] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("CORTEX_WORK_ENABLED", "false")
    monkeypatch.setenv("ENABLE_ATTACHMENTS_CLEANUP_WORKER", "false")
    monkeypatch.setenv("ENABLE_BILLING_RESERVATION_CLEANUP_WORKER", "true")
    monkeypatch.setenv("BILLING_RESERVATION_STALE_AFTER_SECONDS", "1800")
    monkeypatch.setenv("BILLING_RESERVATION_HEARTBEAT_INTERVAL_SECONDS", "3600")
    monkeypatch.setattr(schema_preflight, "validate_billing_schema", lambda: None)
    monkeypatch.setattr(
        reservation_cleanup,
        "run_cleanup_cycle",
        lambda *, stale_after_seconds: (
            cleanup_calls.append(stale_after_seconds)
            or ReservationCleanupStats(inspected=2, released=1, credits_released=900)
        ),
    )
    monkeypatch.setattr(
        reservation_cleanup,
        "heartbeat_active_reservations",
        lambda: 0,
    )

    with TestClient(create_app()):
        pass

    assert cleanup_calls == [1800]
