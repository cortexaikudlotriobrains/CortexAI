from __future__ import annotations

import json
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import (
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
    create_engine,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import billing_repository as repository
from server.billing.errors import (
    BillingProviderError,
    BillingWebhookProcessingError,
    InvalidWebhookSignatureError,
)
from server.billing.stripe_gateway import load_stripe_billing_config
from server.billing.subscription_service import resolve_effective_subscription
from server.billing.webhook_service import reconcile_billing_account
from server.routes import billing as billing_route


def _enabled_environment() -> dict[str, str]:
    return {
        "BILLING_ENABLED": "true",
        "STRIPE_SECRET_KEY": "sk_test_webhook",
        "STRIPE_WEBHOOK_SECRET": "whsec_webhook_unit",
        "STRIPE_PLUS_MONTHLY_PRICE_ID": "price_plus123",
        "STRIPE_PRO_MONTHLY_PRICE_ID": "price_pro123",
        "STRIPE_CHECKOUT_SUCCESS_URL": "https://app.example.com/billing/success",
        "STRIPE_CHECKOUT_CANCEL_URL": "https://app.example.com/plans",
        "STRIPE_PORTAL_RETURN_URL": "https://app.example.com/settings/billing",
    }


class _FakeWebhookGateway:
    def __init__(self) -> None:
        self.subscriptions: dict[str, dict | Exception] = {}
        self.listed_subscriptions: tuple[dict, ...] = ()
        self.retrieve_calls: list[str] = []
        self.assert_outside_transaction: Callable[[], None] = lambda: None

    def verify_webhook_event(self, *, payload: bytes, signature: str):
        if signature != "valid-signature":
            raise InvalidWebhookSignatureError("invalid")
        return json.loads(payload)

    async def retrieve_subscription(self, subscription_id: str):
        self.assert_outside_transaction()
        self.retrieve_calls.append(subscription_id)
        response = self.subscriptions[subscription_id]
        if isinstance(response, Exception):
            raise response
        return response

    async def list_customer_subscriptions(self, *, customer_id: str):
        self.assert_outside_transaction()
        assert customer_id.startswith("cus_")
        return self.listed_subscriptions


def _subscription(
    *,
    account_id: UUID,
    customer_id: str = "cus_webhook123",
    subscription_id: str = "sub_webhook123",
    price_id: str = "price_plus123",
    status: str = "active",
    starts_at: datetime,
    ends_at: datetime,
    cancel_at_period_end: bool = False,
) -> dict:
    return {
        "id": subscription_id,
        "customer": customer_id,
        "metadata": {"cortex_billing_account_id": str(account_id)},
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": None,
        "trial_end": None,
        "latest_invoice": "in_latest123",
        "items": {
            "data": [
                {
                    "price": {"id": price_id},
                    "current_period_start": int(starts_at.timestamp()),
                    "current_period_end": int(ends_at.timestamp()),
                }
            ]
        },
    }


def _event(event_id: str, event_type: str, event_object: dict, *, created_at: datetime) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "created": int(created_at.timestamp()),
            "data": {"object": event_object},
        },
        separators=(",", ":"),
    ).encode()


@pytest.fixture()
def webhook_harness(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("email", String, unique=True),
    )
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
        Index(
            "uq_usage_counter_period_meter",
            "usage_period_id",
            "meter_key",
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
            users,
            billing_accounts,
            subscriptions,
            usage_periods,
            usage_counters,
            billing_webhook_events,
        )
    }
    metadata.create_all(engine)
    import db.tables as db_tables

    monkeypatch.setattr(db_tables, "get_table", tables.__getitem__)
    session_factory = sessionmaker(bind=engine)
    observer = session_factory()
    user_id = uuid4()
    observer.execute(insert(users).values(id=user_id, email="webhook@example.com"))
    account = repository.get_or_create_billing_account_for_user(observer, user_id)
    observer.commit()

    active_uows = 0

    @contextmanager
    def test_uow():
        nonlocal active_uows
        session = session_factory()
        active_uows += 1
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            active_uows -= 1
            session.close()

    for name, value in _enabled_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(billing_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(billing_route, "_db_uow", test_uow)
    gateway = _FakeWebhookGateway()

    def assert_outside_transaction() -> None:
        assert active_uows == 0, "Stripe provider call occurred inside a database transaction"

    gateway.assert_outside_transaction = assert_outside_transaction
    monkeypatch.setattr(billing_route, "_gateway_factory", lambda _config: gateway)

    app = FastAPI()
    app.include_router(billing_route.router)
    client = TestClient(app)
    try:
        yield client, observer, tables, gateway, test_uow, account, user_id
    finally:
        client.close()
        observer.close()
        engine.dispose()


def _post(client: TestClient, payload: bytes, *, signature: str = "valid-signature"):
    return client.post(
        "/v1/billing/webhook",
        content=payload,
        headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
    )


@pytest.mark.integration
def test_invalid_signature_is_rejected_before_event_persistence(webhook_harness):
    client, db, tables, _, _, _, _ = webhook_harness
    now = datetime.now(UTC)
    response = _post(
        client,
        _event("evt_invalid123", "customer.created", {"id": "cus_x"}, created_at=now),
        signature="invalid",
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_webhook_signature"
    assert (
        db.execute(select(func.count()).select_from(tables["billing_webhook_events"])).scalar_one()
        == 0
    )


@pytest.mark.integration
def test_unknown_and_duplicate_events_are_harmless(webhook_harness):
    client, db, tables, _, _, _, _ = webhook_harness
    payload = _event(
        "evt_unknown123",
        "customer.created",
        {"id": "cus_unhandled123"},
        created_at=datetime.now(UTC),
    )

    first = _post(client, payload)
    duplicate = _post(client, payload)

    assert first.status_code == duplicate.status_code == 200
    assert first.json() == duplicate.json() == {"received": True}
    rows = db.execute(select(tables["billing_webhook_events"])).mappings().all()
    assert len(rows) == 1
    assert rows[0]["processing_status"] == "ignored"


@pytest.mark.integration
def test_webhook_disabled_and_payload_limit_fail_before_processing(
    webhook_harness,
    monkeypatch,
):
    client, db, tables, gateway, _, _, _ = webhook_harness
    payload = _event(
        "evt_disabled123",
        "customer.created",
        {"id": "cus_disabled123"},
        created_at=datetime.now(UTC),
    )
    monkeypatch.setenv("BILLING_ENABLED", "false")

    disabled = _post(client, payload)
    too_large = _post(client, b"x" * (1024 * 1024 + 1))

    assert disabled.status_code == 503
    assert disabled.json()["detail"]["code"] == "billing_not_configured"
    assert too_large.status_code == 413
    assert gateway.retrieve_calls == []
    assert (
        db.execute(select(func.count()).select_from(tables["billing_webhook_events"])).scalar_one()
        == 0
    )


@pytest.mark.integration
def test_checkout_completed_claims_customer_and_creates_one_paid_period(webhook_harness):
    client, db, tables, gateway, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    starts_at = now - timedelta(days=2)
    ends_at = now + timedelta(days=28)
    gateway.subscriptions["sub_checkout123"] = _subscription(
        account_id=account["id"],
        subscription_id="sub_checkout123",
        starts_at=starts_at,
        ends_at=ends_at,
    )
    payload = _event(
        "evt_checkout123",
        "checkout.session.completed",
        {
            "id": "cs_checkout123",
            "mode": "subscription",
            "customer": "cus_webhook123",
            "subscription": "sub_checkout123",
            "client_reference_id": str(account["id"]),
            "metadata": {"cortex_billing_account_id": str(account["id"])},
        },
        created_at=now,
    )

    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200

    customer_id = db.execute(
        select(tables["billing_accounts"].c.stripe_customer_id).where(
            tables["billing_accounts"].c.id == account["id"]
        )
    ).scalar_one()
    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    periods = db.execute(select(tables["usage_periods"])).mappings().all()
    assert customer_id == "cus_webhook123"
    assert subscription["plan_code"] == "plus"
    assert subscription["status"] == "active"
    assert len(periods) == 1
    assert periods[0]["plan_code"] == "plus"
    assert gateway.retrieve_calls == ["sub_checkout123"]


@pytest.mark.integration
def test_subscription_update_changes_plan_without_resetting_period_counters(webhook_harness):
    client, db, tables, _, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    starts_at = now - timedelta(days=3)
    ends_at = now + timedelta(days=27)
    created = _event(
        "evt_created123",
        "customer.subscription.created",
        _subscription(account_id=account["id"], starts_at=starts_at, ends_at=ends_at),
        created_at=now - timedelta(minutes=1),
    )
    assert _post(client, created).status_code == 200
    period = db.execute(select(tables["usage_periods"])).mappings().one()
    counter = repository.get_or_create_usage_counter(db, period["id"], "ai_credits")
    db.execute(
        update(tables["usage_counters"])
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=7)
    )
    db.commit()

    updated = _event(
        "evt_updated123",
        "customer.subscription.updated",
        _subscription(
            account_id=account["id"],
            price_id="price_pro123",
            starts_at=starts_at,
            ends_at=ends_at,
        ),
        created_at=now,
    )
    assert _post(client, updated).status_code == 200

    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    periods = db.execute(select(tables["usage_periods"])).mappings().all()
    used = db.execute(select(tables["usage_counters"].c.used_quantity)).scalar_one()
    assert subscription["plan_code"] == "pro"
    assert len(periods) == 1
    assert periods[0]["id"] == period["id"]
    assert periods[0]["plan_code"] == "pro"
    assert used == 7


@pytest.mark.integration
def test_deleted_subscription_downgrades_to_free_without_touching_history(webhook_harness):
    client, db, tables, _, _, account, user_id = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    starts_at = now - timedelta(days=2)
    ends_at = now + timedelta(days=28)
    active_object = _subscription(account_id=account["id"], starts_at=starts_at, ends_at=ends_at)
    assert (
        _post(
            client,
            _event(
                "evt_active123",
                "customer.subscription.created",
                active_object,
                created_at=now - timedelta(minutes=1),
            ),
        ).status_code
        == 200
    )
    deleted_object = {**active_object, "status": "canceled", "cancel_at_period_end": False}
    assert (
        _post(
            client,
            _event(
                "evt_deleted123",
                "customer.subscription.deleted",
                deleted_object,
                created_at=now,
            ),
        ).status_code
        == 200
    )

    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    effective = resolve_effective_subscription(db, user_id, now=now + timedelta(seconds=1))
    db.commit()
    assert subscription["status"] == "canceled"
    assert effective.plan.code == "free"
    assert db.execute(select(func.count()).select_from(tables["subscriptions"])).scalar_one() == 1


@pytest.mark.integration
def test_invoice_paid_resets_period_once_and_duplicate_preserves_usage(webhook_harness):
    client, db, tables, gateway, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    starts_at = now - timedelta(hours=1)
    ends_at = now + timedelta(days=30)
    gateway.subscriptions["sub_invoice123"] = _subscription(
        account_id=account["id"],
        subscription_id="sub_invoice123",
        starts_at=starts_at,
        ends_at=ends_at,
    )
    payload = _event(
        "evt_invoice_paid123",
        "invoice.paid",
        {
            "id": "in_paid123",
            "customer": "cus_webhook123",
            "parent": {"subscription_details": {"subscription": "sub_invoice123"}},
        },
        created_at=now,
    )
    assert _post(client, payload).status_code == 200
    period = db.execute(select(tables["usage_periods"])).mappings().one()
    counter = repository.get_or_create_usage_counter(db, period["id"], "ai_credits")
    db.execute(
        update(tables["usage_counters"])
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=11)
    )
    db.commit()

    assert _post(client, payload).status_code == 200
    assert db.execute(select(func.count()).select_from(tables["usage_periods"])).scalar_one() == 1
    assert db.execute(select(tables["usage_counters"].c.used_quantity)).scalar_one() == 11
    assert gateway.retrieve_calls == ["sub_invoice123"]


@pytest.mark.integration
def test_payment_failure_sets_grace_only_for_authoritative_past_due_state(
    webhook_harness,
    monkeypatch,
):
    client, db, tables, gateway, _, account, _ = webhook_harness
    monkeypatch.setenv("SUBSCRIPTION_PAYMENT_GRACE_DAYS", "3")
    now = datetime.now(UTC).replace(microsecond=0)
    gateway.subscriptions["sub_failed123"] = _subscription(
        account_id=account["id"],
        subscription_id="sub_failed123",
        status="past_due",
        starts_at=now - timedelta(days=5),
        ends_at=now + timedelta(days=25),
    )
    payload = _event(
        "evt_failed123",
        "invoice.payment_failed",
        {
            "id": "in_failed123",
            "customer": "cus_webhook123",
            "subscription": "sub_failed123",
        },
        created_at=now,
    )
    assert _post(client, payload).status_code == 200

    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    grace_until = subscription["grace_until"].replace(tzinfo=UTC)
    assert subscription["status"] == "past_due"
    assert grace_until == now + timedelta(days=3)


@pytest.mark.integration
def test_older_subscription_event_does_not_overwrite_newer_snapshot(webhook_harness):
    client, db, tables, _, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    base = dict(
        account_id=account["id"],
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=29),
    )
    newer = _event(
        "evt_newer123",
        "customer.subscription.updated",
        _subscription(price_id="price_pro123", **base),
        created_at=now,
    )
    older = _event(
        "evt_older123",
        "customer.subscription.updated",
        _subscription(price_id="price_plus123", status="past_due", **base),
        created_at=now - timedelta(minutes=5),
    )

    assert _post(client, newer).status_code == 200
    assert _post(client, older).status_code == 200
    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    assert subscription["plan_code"] == "pro"
    assert subscription["status"] == "active"


@pytest.mark.integration
def test_unknown_price_fails_conservatively_and_remains_retryable(webhook_harness):
    client, db, tables, _, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    payload = _event(
        "evt_unknown_price123",
        "customer.subscription.created",
        _subscription(
            account_id=account["id"],
            price_id="price_not_configured123",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=29),
        ),
        created_at=now,
    )

    response = _post(client, payload)

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "subscription_configuration_error"
    event = db.execute(select(tables["billing_webhook_events"])).mappings().one()
    assert event["processing_status"] == "failed"
    assert db.execute(select(func.count()).select_from(tables["subscriptions"])).scalar_one() == 0


@pytest.mark.integration
def test_failed_event_is_recorded_and_can_be_retried(webhook_harness):
    client, db, tables, gateway, _, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    subscription = _subscription(
        account_id=account["id"],
        subscription_id="sub_retry123",
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=29),
    )
    gateway.subscriptions["sub_retry123"] = BillingProviderError("temporary")
    payload = _event(
        "evt_retry123",
        "invoice.paid",
        {
            "id": "in_retry123",
            "customer": "cus_webhook123",
            "subscription": "sub_retry123",
        },
        created_at=now,
    )

    failed = _post(client, payload)
    event_status = db.execute(
        select(tables["billing_webhook_events"].c.processing_status)
    ).scalar_one()
    assert failed.status_code == 500
    assert failed.json()["detail"]["code"] == "billing_webhook_processing_failed"
    assert event_status == "failed"

    gateway.subscriptions["sub_retry123"] = subscription
    retried = _post(client, payload)
    assert retried.status_code == 200
    assert (
        db.execute(select(tables["billing_webhook_events"].c.processing_status)).scalar_one()
        == "processed"
    )
    assert db.execute(select(func.count()).select_from(tables["subscriptions"])).scalar_one() == 1


@pytest.mark.unit
def test_reconciliation_refreshes_one_authoritative_subscription(webhook_harness):
    _, db, tables, gateway, uow_factory, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    repository.set_stripe_customer_id(db, account["id"], "cus_webhook123")
    db.commit()
    gateway.listed_subscriptions = (
        _subscription(
            account_id=account["id"],
            price_id="price_pro123",
            starts_at=now - timedelta(days=2),
            ends_at=now + timedelta(days=28),
        ),
    )
    config = load_stripe_billing_config(environment=_enabled_environment())

    result = asyncio.run(
        reconcile_billing_account(
            uow_factory=uow_factory,
            gateway=gateway,
            config=config,
            billing_account_id=account["id"],
            now_factory=lambda: now,
        )
    )

    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    assert result.outcome == "reconciled"
    assert subscription["plan_code"] == "pro"
    assert subscription["status"] == "active"


@pytest.mark.unit
def test_reconciliation_rejects_multiple_live_subscriptions(webhook_harness):
    _, db, _, gateway, uow_factory, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    repository.set_stripe_customer_id(db, account["id"], "cus_webhook123")
    db.commit()
    gateway.listed_subscriptions = (
        _subscription(
            account_id=account["id"],
            subscription_id="sub_first123",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=29),
        ),
        _subscription(
            account_id=account["id"],
            subscription_id="sub_second123",
            price_id="price_pro123",
            starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=29),
        ),
    )
    config = load_stripe_billing_config(environment=_enabled_environment())

    with pytest.raises(BillingWebhookProcessingError, match="Multiple live"):
        asyncio.run(
            reconcile_billing_account(
                uow_factory=uow_factory,
                gateway=gateway,
                config=config,
                billing_account_id=account["id"],
                now_factory=lambda: now,
            )
        )


@pytest.mark.unit
def test_reconciliation_with_no_remote_subscription_cancels_stale_local_state(
    webhook_harness,
):
    _, db, tables, gateway, uow_factory, account, _ = webhook_harness
    now = datetime.now(UTC).replace(microsecond=0)
    repository.set_stripe_customer_id(db, account["id"], "cus_webhook123")
    repository.upsert_subscription_snapshot(
        db,
        repository.SubscriptionSnapshot(
            billing_account_id=account["id"],
            provider="stripe",
            provider_subscription_id="sub_stale123",
            provider_price_id="price_plus123",
            plan_code="plus",
            status="active",
            current_period_start=now - timedelta(days=2),
            current_period_end=now + timedelta(days=28),
            last_provider_event_at=now - timedelta(days=1),
        ),
    )
    db.commit()
    config = load_stripe_billing_config(environment=_enabled_environment())

    result = asyncio.run(
        reconcile_billing_account(
            uow_factory=uow_factory,
            gateway=gateway,
            config=config,
            billing_account_id=account["id"],
            now_factory=lambda: now,
        )
    )

    subscription = db.execute(select(tables["subscriptions"])).mappings().one()
    assert result.outcome == "reconciled"
    assert subscription["status"] == "canceled"
