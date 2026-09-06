from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Uuid,
    func,
    select,
    update,
)
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db import billing_repository as repository


@pytest.fixture()
def billing_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()

    billing_accounts = Table(
        "billing_accounts",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("owner_type", String(32), nullable=False),
        Column("owner_id", Uuid, nullable=False),
        Column("stripe_customer_id", String(255), unique=True),
        Column("currency", String(3), nullable=False, server_default="USD"),
        Column("country", String(2)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint("owner_type IN ('user', 'organization')"),
        Index("uq_billing_accounts_owner", "owner_type", "owner_id", unique=True),
    )

    subscriptions = Table(
        "subscriptions",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("provider", String(32), nullable=False, server_default="stripe"),
        Column("provider_subscription_id", String(255)),
        Column("provider_price_id", String(255)),
        Column("plan_code", String(64), nullable=False),
        Column("status", String(64), nullable=False),
        Column("current_period_start", DateTime(timezone=True)),
        Column("current_period_end", DateTime(timezone=True)),
        Column("cancel_at_period_end", Boolean, nullable=False, server_default="0"),
        Column("canceled_at", DateTime(timezone=True)),
        Column("trial_end", DateTime(timezone=True)),
        Column("grace_until", DateTime(timezone=True)),
        Column("latest_invoice_id", String(255)),
        Column("last_provider_event_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Index(
            "uq_subscriptions_provider_id",
            "provider",
            "provider_subscription_id",
            unique=True,
        ),
        Index(
            "uq_subscriptions_one_live_per_account",
            "billing_account_id",
            unique=True,
            sqlite_where=Column("status").in_(sorted(repository.LIVE_SUBSCRIPTION_STATUSES)),
        ),
    )

    usage_periods = Table(
        "usage_periods",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("subscription_id", Uuid, ForeignKey("subscriptions.id")),
        Column("plan_code", String(64), nullable=False),
        Column("starts_at", DateTime(timezone=True), nullable=False),
        Column("ends_at", DateTime(timezone=True), nullable=False),
        Column("status", String(32), nullable=False, server_default="active"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint("ends_at > starts_at"),
        Index(
            "uq_usage_period_account_start",
            "billing_account_id",
            "starts_at",
            unique=True,
        ),
    )

    usage_counters = Table(
        "usage_counters",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("usage_period_id", Uuid, ForeignKey("usage_periods.id"), nullable=False),
        Column("meter_key", String(64), nullable=False),
        Column("used_quantity", BigInteger, nullable=False, server_default="0"),
        Column("reserved_quantity", BigInteger, nullable=False, server_default="0"),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint("used_quantity >= 0 AND reserved_quantity >= 0"),
        Index(
            "uq_usage_counter_period_meter",
            "usage_period_id",
            "meter_key",
            unique=True,
        ),
    )

    usage_reservations = Table(
        "usage_reservations",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("usage_period_id", Uuid, ForeignKey("usage_periods.id"), nullable=False),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("state", String(32), nullable=False),
        Column("requested_quantities", JSON, nullable=False),
        Column("settled_quantities", JSON),
        Column("release_reason", String(255)),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("settled_at", DateTime(timezone=True)),
        Column("released_at", DateTime(timezone=True)),
        CheckConstraint("state IN ('reserved', 'settled', 'released', 'expired')"),
        Index(
            "uq_usage_reservations_request",
            "billing_account_id",
            "request_id",
            unique=True,
        ),
    )

    billing_webhook_events = Table(
        "billing_webhook_events",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("provider", String(32), nullable=False, server_default="stripe"),
        Column("provider_event_id", String(255), nullable=False),
        Column("event_type", String(255), nullable=False),
        Column("payload_hash", String(64), nullable=False),
        Column("processing_status", String(32), nullable=False),
        Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("processed_at", DateTime(timezone=True)),
        Column("error_message", String),
        CheckConstraint("processing_status IN ('received', 'processed', 'ignored', 'failed')"),
        Index(
            "uq_billing_webhook_provider_event",
            "provider",
            "provider_event_id",
            unique=True,
        ),
    )

    from tests.billing_schema_helpers import add_subscription_grant_schema

    subscription_grants = add_subscription_grant_schema(metadata)
    tables = {
        table.name: table
        for table in (
            subscription_grants,
            billing_accounts,
            subscriptions,
            usage_periods,
            usage_counters,
            usage_reservations,
            billing_webhook_events,
        )
    }
    metadata.create_all(engine)

    import db.tables as db_tables

    monkeypatch.setattr(db_tables, "get_table", tables.__getitem__)
    db = sessionmaker(bind=engine)()
    try:
        yield db, tables
    finally:
        db.close()
        engine.dispose()


def _account_and_period(db):
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=31),
    )
    return account, period


@pytest.mark.unit
def test_billing_tables_are_registered_for_lazy_reflection():
    from db.tables import TABLE_NAMES

    assert {
        "billing_accounts",
        "subscriptions",
        "usage_periods",
        "usage_counters",
        "usage_reservations",
        "billing_webhook_events",
        "cache_reuse_events",
    }.issubset(TABLE_NAMES)


@pytest.mark.unit
def test_get_or_create_billing_account_is_idempotent_and_does_not_commit(billing_db):
    db, tables = billing_db
    user_id = uuid4()

    first = repository.get_or_create_billing_account_for_user(db, user_id)
    second = repository.get_or_create_billing_account_for_user(db, user_id)

    assert first["id"] == second["id"]
    assert (
        db.execute(select(func.count()).select_from(tables["billing_accounts"])).scalar_one() == 1
    )

    db.rollback()
    assert (
        db.execute(select(func.count()).select_from(tables["billing_accounts"])).scalar_one() == 0
    )


@pytest.mark.unit
def test_duplicate_stripe_customer_is_rejected(billing_db):
    db, _ = billing_db
    first = repository.get_or_create_billing_account_for_user(db, uuid4())
    second = repository.get_or_create_billing_account_for_user(db, uuid4())
    repository.set_stripe_customer_id(db, first["id"], "cus_shared")

    with pytest.raises(IntegrityError):
        repository.set_stripe_customer_id(db, second["id"], "cus_shared")


@pytest.mark.unit
def test_claim_stripe_customer_id_sets_once_without_overwriting_winner(billing_db):
    db, _ = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())

    claimed = repository.claim_stripe_customer_id(db, account["id"], "cus_first")
    existing = repository.claim_stripe_customer_id(db, account["id"], "cus_second")

    assert claimed["stripe_customer_id"] == "cus_first"
    assert existing["stripe_customer_id"] == "cus_first"


@pytest.mark.unit
def test_subscription_upsert_is_idempotent_and_prevents_cross_account_reassignment(billing_db):
    db, tables = billing_db
    first_account = repository.get_or_create_billing_account_for_user(db, uuid4())
    second_account = repository.get_or_create_billing_account_for_user(db, uuid4())
    snapshot = repository.SubscriptionSnapshot(
        billing_account_id=first_account["id"],
        provider="Stripe",
        provider_subscription_id="sub_123",
        provider_price_id="price_plus",
        plan_code="PLUS",
        status="ACTIVE",
    )

    created = repository.upsert_subscription_snapshot(db, snapshot)
    updated = repository.upsert_subscription_snapshot(
        db,
        replace(snapshot, provider_price_id="price_plus_v2"),
    )

    assert created["id"] == updated["id"]
    assert updated["provider_price_id"] == "price_plus_v2"
    assert (
        repository.get_live_subscription_for_account(db, first_account["id"])["id"] == created["id"]
    )
    assert db.execute(select(func.count()).select_from(tables["subscriptions"])).scalar_one() == 1

    with pytest.raises(ValueError, match="another billing account"):
        repository.upsert_subscription_snapshot(
            db,
            replace(snapshot, billing_account_id=second_account["id"]),
        )


@pytest.mark.unit
def test_subscription_upsert_cannot_replace_newer_provider_event(billing_db):
    db, _ = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    newer_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    snapshot = repository.SubscriptionSnapshot(
        billing_account_id=account["id"],
        provider="stripe",
        provider_subscription_id="sub_ordered",
        provider_price_id="price_pro",
        plan_code="pro",
        status="active",
        last_provider_event_at=newer_at,
    )
    repository.upsert_subscription_snapshot(db, snapshot)

    persisted = repository.upsert_subscription_snapshot(
        db,
        replace(
            snapshot,
            provider_price_id="price_plus",
            plan_code="plus",
            status="past_due",
            last_provider_event_at=newer_at - timedelta(minutes=5),
        ),
    )

    assert persisted["plan_code"] == "pro"
    assert persisted["status"] == "active"


@pytest.mark.unit
def test_only_one_live_subscription_is_allowed_per_account(billing_db):
    db, _ = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    repository.upsert_subscription_snapshot(
        db,
        repository.SubscriptionSnapshot(
            billing_account_id=account["id"],
            provider="stripe",
            provider_subscription_id="sub_first",
            plan_code="plus",
            status="active",
        ),
    )

    with pytest.raises(IntegrityError):
        repository.upsert_subscription_snapshot(
            db,
            repository.SubscriptionSnapshot(
                billing_account_id=account["id"],
                provider="stripe",
                provider_subscription_id="sub_second",
                plan_code="pro",
                status="trialing",
            ),
        )


@pytest.mark.unit
def test_usage_period_lifecycle_and_duplicate_start_protection(billing_db):
    db, _ = billing_db
    account, period = _account_and_period(db)
    at_time = datetime(2026, 7, 15, tzinfo=UTC)

    active = repository.get_active_usage_period(db, account["id"], at_time)
    assert active is not None
    assert active["id"] == period["id"]

    closed = repository.close_usage_period(db, period["id"])
    assert closed is not None
    assert closed["status"] == "closed"
    assert repository.get_active_usage_period(db, account["id"], at_time) is None

    with pytest.raises(IntegrityError):
        repository.create_usage_period(
            db,
            billing_account_id=account["id"],
            plan_code="free",
            starts_at=datetime(2026, 7, 1, tzinfo=UTC),
            ends_at=datetime(2026, 9, 1, tzinfo=UTC),
        )


@pytest.mark.unit
def test_usage_period_rejects_invalid_dates(billing_db):
    db, _ = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="after starts_at"):
        repository.create_usage_period(
            db,
            billing_account_id=account["id"],
            plan_code="free",
            starts_at=starts_at,
            ends_at=starts_at,
        )


@pytest.mark.unit
def test_get_or_create_usage_period_is_idempotent(billing_db):
    db, _ = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    ends_at = datetime(2026, 8, 1, tzinfo=UTC)

    first = repository.get_or_create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=ends_at,
    )
    second = repository.get_or_create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=ends_at,
    )

    assert first["id"] == second["id"]

    repository.close_usage_period(db, first["id"])
    reactivated = repository.get_or_create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="free",
        starts_at=starts_at,
        ends_at=ends_at,
    )
    assert reactivated["id"] == first["id"]
    assert reactivated["status"] == "active"


@pytest.mark.unit
def test_synchronize_usage_period_preserves_row_and_counters(billing_db):
    db, tables = billing_db
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    starts_at = datetime(2026, 7, 1, tzinfo=UTC)
    period = repository.create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="plus",
        starts_at=starts_at,
        ends_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    counter = repository.get_or_create_usage_counter(db, period["id"], "ai_credits")
    db.execute(
        update(tables["usage_counters"])
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=9)
    )

    synchronized = repository.synchronize_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code="pro",
        starts_at=starts_at,
        ends_at=datetime(2026, 8, 2, tzinfo=UTC),
    )

    assert synchronized["id"] == period["id"]
    assert synchronized["plan_code"] == "pro"
    assert db.execute(select(tables["usage_counters"].c.used_quantity)).scalar_one() == 9


@pytest.mark.unit
def test_usage_counter_is_idempotent_and_nonnegative(billing_db):
    db, tables = billing_db
    _, period = _account_and_period(db)

    first = repository.get_or_create_usage_counter(db, period["id"], "ai_credits")
    second = repository.get_or_create_usage_counter(db, period["id"], "ai_credits")

    assert first["id"] == second["id"]
    with pytest.raises(ValueError, match="Unknown usage meter"):
        repository.get_or_create_usage_counter(db, period["id"], "tokens")
    with pytest.raises(IntegrityError):
        db.execute(
            update(tables["usage_counters"])
            .where(tables["usage_counters"].c.id == first["id"])
            .values(reserved_quantity=-1)
        )


@pytest.mark.unit
def test_counter_lock_is_for_update_for_unified_credit_meter(billing_db, monkeypatch):
    db, _ = billing_db
    _, period = _account_and_period(db)
    repository.get_or_create_usage_counter(db, period["id"], "ai_credits")

    captured = []
    original_execute = db.execute

    def recording_execute(statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            captured.append(statement)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", recording_execute)
    rows = repository.lock_usage_counters(
        db,
        period["id"],
        ["ai_credits"],
    )

    assert [row["meter_key"] for row in rows] == ["ai_credits"]
    assert len(captured) == 1
    assert captured[0]._for_update_arg is not None


@pytest.mark.unit
def test_reservation_duplicate_and_quantity_validation(billing_db):
    db, _ = billing_db
    account, period = _account_and_period(db)
    repository.create_usage_reservation(
        db,
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        request_id="req-1",
        operation_type="ask",
        requested_quantities={"ai_credits": 1},
    )

    with pytest.raises(IntegrityError):
        repository.create_usage_reservation(
            db,
            billing_account_id=account["id"],
            usage_period_id=period["id"],
            request_id="req-1",
            operation_type="ask",
            requested_quantities={"ai_credits": 1},
        )


@pytest.mark.unit
def test_reservation_settlement_and_release_are_idempotent(billing_db):
    db, _ = billing_db
    account, period = _account_and_period(db)
    repository.create_usage_reservation(
        db,
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        request_id="req-settle",
        operation_type="compare",
        requested_quantities={"ai_credits": 2_000},
    )

    settled = repository.settle_usage_reservation(
        db,
        billing_account_id=account["id"],
        request_id="req-settle",
        settled_quantities={"ai_credits": 1_000},
    )
    settled_again = repository.settle_usage_reservation(
        db,
        billing_account_id=account["id"],
        request_id="req-settle",
        settled_quantities={"ai_credits": 1_000},
    )
    assert settled["state"] == "settled"
    assert settled_again["id"] == settled["id"]

    with pytest.raises(ValueError, match="Cannot release"):
        repository.release_usage_reservation(
            db,
            billing_account_id=account["id"],
            request_id="req-settle",
            release_reason="provider_error",
        )

    repository.create_usage_reservation(
        db,
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        request_id="req-release",
        operation_type="ask",
        requested_quantities={"ai_credits": 1},
    )
    released = repository.release_usage_reservation(
        db,
        billing_account_id=account["id"],
        request_id="req-release",
        release_reason="provider_error",
    )
    released_again = repository.release_usage_reservation(
        db,
        billing_account_id=account["id"],
        request_id="req-release",
        release_reason="retry",
    )
    assert released["state"] == "released"
    assert released_again["id"] == released["id"]


@pytest.mark.unit
def test_settlement_cannot_exceed_reserved_quantities(billing_db):
    db, _ = billing_db
    account, period = _account_and_period(db)
    repository.create_usage_reservation(
        db,
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        request_id="req-over",
        operation_type="ask",
        requested_quantities={"ai_credits": 1},
    )

    with pytest.raises(ValueError, match="cannot exceed"):
        repository.settle_usage_reservation(
            db,
            billing_account_id=account["id"],
            request_id="req-over",
            settled_quantities={"ai_credits": 2},
        )


@pytest.mark.unit
def test_webhook_event_creation_is_idempotent_and_hash_bound(billing_db):
    db, tables = billing_db
    digest = hashlib.sha256(b"payload").hexdigest()

    created, was_created = repository.create_webhook_event_if_absent(
        db,
        provider="Stripe",
        provider_event_id="evt_123",
        event_type="customer.subscription.updated",
        payload_hash=digest,
    )
    duplicate, duplicate_was_created = repository.create_webhook_event_if_absent(
        db,
        provider="stripe",
        provider_event_id="evt_123",
        event_type="customer.subscription.updated",
        payload_hash=digest,
    )

    assert was_created is True
    assert duplicate_was_created is False
    assert duplicate["id"] == created["id"]
    assert (
        db.execute(select(func.count()).select_from(tables["billing_webhook_events"])).scalar_one()
        == 1
    )

    with pytest.raises(ValueError, match="different payload hash"):
        repository.create_webhook_event_if_absent(
            db,
            provider="stripe",
            provider_event_id="evt_123",
            event_type="customer.subscription.updated",
            payload_hash=hashlib.sha256(b"tampered").hexdigest(),
        )

    with pytest.raises(ValueError, match="SHA-256"):
        repository.create_webhook_event_if_absent(
            db,
            provider="stripe",
            provider_event_id="evt_invalid_hash",
            event_type="customer.subscription.updated",
            payload_hash="not-a-digest",
        )


@pytest.mark.unit
def test_webhook_event_status_updates(billing_db):
    db, _ = billing_db
    event, _ = repository.create_webhook_event_if_absent(
        db,
        provider="stripe",
        provider_event_id="evt_status",
        event_type="invoice.payment_failed",
        payload_hash=hashlib.sha256(b"status").hexdigest(),
    )

    ignored = repository.mark_webhook_event_processed(db, event["id"], ignored=True)
    assert ignored is not None
    assert ignored["processing_status"] == "ignored"

    failed = repository.mark_webhook_event_failed(
        db,
        event["id"],
        error_message="transient processing failure",
    )
    assert failed is not None
    assert failed["processing_status"] == "failed"

    processed = repository.mark_webhook_event_processed(db, event["id"])
    assert processed is not None
    assert processed["processing_status"] == "processed"
    assert processed["error_message"] is None


@pytest.mark.unit
def test_webhook_event_lock_uses_for_update(billing_db, monkeypatch):
    db, _ = billing_db
    event, _ = repository.create_webhook_event_if_absent(
        db,
        provider="stripe",
        provider_event_id="evt_lock",
        event_type="invoice.paid",
        payload_hash=hashlib.sha256(b"lock").hexdigest(),
    )
    captured = []
    original_execute = db.execute

    def recording_execute(statement, *args, **kwargs):
        if getattr(statement, "_for_update_arg", None) is not None:
            captured.append(statement)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", recording_execute)
    locked = repository.lock_webhook_event(
        db,
        provider="stripe",
        provider_event_id="evt_lock",
    )

    assert locked is not None
    assert locked["id"] == event["id"]
    assert len(captured) == 1


@pytest.mark.unit
def test_billing_migration_contains_required_constraints_and_is_additive():
    migration = (
        Path("db/migrations/20260718_add_b2c_billing_foundation.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    for table_name in (
        "billing_accounts",
        "subscriptions",
        "usage_periods",
        "usage_counters",
        "usage_reservations",
        "billing_webhook_events",
    ):
        assert f"create table if not exists public.{table_name}" in migration

    assert "uq_billing_accounts_owner" in migration
    assert "uq_billing_accounts_stripe_customer" in migration
    assert "uq_subscriptions_provider_id" in migration
    assert "uq_subscriptions_one_live_per_account" in migration
    assert "ck_usage_counter_nonnegative" in migration
    assert "uq_usage_reservations_request" in migration
    assert "uq_billing_webhook_provider_event" in migration
    assert "alter table public.users" not in migration
    assert "llm_requests" not in migration
    assert migration.startswith("begin;")
    assert migration.rstrip().endswith("commit;")

    credit_migration = (
        Path("db/migrations/20260729_add_unified_ai_credits.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists public.credit_transactions" in credit_migration
    assert "uq_credit_transactions_reservation_item" in credit_migration
    assert "usage_counters.meter_key = 'ai_credits'" in credit_migration
    assert credit_migration.startswith("begin;")
    assert credit_migration.rstrip().endswith("commit;")

    cache_migration = (
        Path("db/migrations/20260807_add_cache_aware_credit_accounting.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists public.cache_reuse_events" in cache_migration
    assert "uq_cache_reuse_events_user_operation_request" in cache_migration
    assert cache_migration.startswith("begin;")
    assert cache_migration.rstrip().endswith("commit;")
