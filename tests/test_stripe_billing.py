from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import (
    Boolean,
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
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import billing_repository as repository
from server.billing.errors import (
    BillingConfigurationError,
    BillingProviderError,
    InvalidWebhookSignatureError,
)
from server.billing.stripe_gateway import (
    StripeBillingConfig,
    StripeGateway,
    load_stripe_billing_config,
    provider_failure_details,
    stripe_billing_is_enabled,
)
from server.dependencies import AuthResult, get_auth
from server.routes import billing as billing_route


def _enabled_environment() -> dict[str, str]:
    return {
        "BILLING_ENABLED": "true",
        "STRIPE_SECRET_KEY": "sk_test_unit",
        "STRIPE_WEBHOOK_SECRET": "whsec_unit",
        "STRIPE_PLUS_MONTHLY_PRICE_ID": "price_plus123",
        "STRIPE_PRO_MONTHLY_PRICE_ID": "price_pro123",
        "STRIPE_CHECKOUT_SUCCESS_URL": "https://app.example.com/billing/success",
        "STRIPE_CHECKOUT_CANCEL_URL": "https://app.example.com/plans",
        "STRIPE_PORTAL_RETURN_URL": "https://app.example.com/settings/billing",
        "STRIPE_API_VERSION": "2026-06-24.dahlia",
    }


@pytest.mark.unit
def test_disabled_billing_config_requires_no_stripe_values():
    config = load_stripe_billing_config(environment={"BILLING_ENABLED": "false"})

    assert config.enabled is False
    assert config.secret_key is None
    assert dict(config.price_ids) == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [("true", True), ("false", False)],
)
def test_public_billing_availability_flag_uses_validated_boolean(raw_value, expected):
    assert stripe_billing_is_enabled({"BILLING_ENABLED": raw_value}) is expected


@pytest.mark.unit
def test_public_billing_availability_flag_rejects_ambiguous_values():
    with pytest.raises(BillingConfigurationError, match="BILLING_ENABLED"):
        stripe_billing_is_enabled({"BILLING_ENABLED": "maybe"})


@pytest.mark.unit
def test_enabled_billing_config_resolves_all_prices_and_server_urls():
    config = load_stripe_billing_config(environment=_enabled_environment())

    assert config.enabled is True
    assert config.webhook_secret == "whsec_unit"
    assert config.require_price_id("plus") == "price_plus123"
    assert config.require_price_id("pro") == "price_pro123"
    assert config.checkout_success_url == "https://app.example.com/billing/success"
    assert config.portal_return_url == "https://app.example.com/settings/billing"
    assert config.api_version == "2026-06-24.dahlia"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("STRIPE_SECRET_KEY", "pk_test_public"),
        ("STRIPE_WEBHOOK_SECRET", "not_a_webhook_secret"),
        ("STRIPE_PLUS_MONTHLY_PRICE_ID", "prod_not_a_price"),
        ("STRIPE_CHECKOUT_SUCCESS_URL", "https://user:pass@app.example.com/return"),
        ("STRIPE_PORTAL_RETURN_URL", "http://app.example.com/settings"),
        ("STRIPE_API_VERSION", "latest"),
    ],
)
def test_enabled_billing_config_rejects_unsafe_values(field, value):
    environment = _enabled_environment()
    environment[field] = value

    with pytest.raises(BillingConfigurationError):
        load_stripe_billing_config(environment=environment)


@pytest.mark.unit
def test_enabled_billing_config_rejects_duplicate_paid_plan_prices():
    environment = _enabled_environment()
    environment["STRIPE_PRO_MONTHLY_PRICE_ID"] = environment["STRIPE_PLUS_MONTHLY_PRICE_ID"]

    with pytest.raises(BillingConfigurationError):
        load_stripe_billing_config(environment=environment)


@pytest.mark.unit
def test_enabled_billing_config_requires_webhook_secret():
    environment = _enabled_environment()
    environment.pop("STRIPE_WEBHOOK_SECRET")

    with pytest.raises(BillingConfigurationError, match="STRIPE_WEBHOOK_SECRET"):
        load_stripe_billing_config(environment=environment)


class _FakeStripeCreateService:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[dict, dict | None]] = []

    async def create_async(self, params, *, options=None):
        self.calls.append((params, options))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _fake_stripe_client():
    customers = _FakeStripeCreateService({"id": "cus_unit123"})
    checkout = _FakeStripeCreateService({"url": "https://checkout.stripe.com/c/pay/unit"})
    portal = _FakeStripeCreateService({"url": "https://billing.stripe.com/p/session/unit"})
    client = SimpleNamespace(
        v1=SimpleNamespace(
            customers=customers,
            checkout=SimpleNamespace(sessions=checkout),
            billing_portal=SimpleNamespace(sessions=portal),
        )
    )
    return client, customers, checkout, portal


@pytest.mark.unit
def test_gateway_sends_only_server_owned_checkout_and_portal_parameters():
    account_id = uuid4()
    user_id = uuid4()
    client, customers, checkout, portal = _fake_stripe_client()
    config = StripeBillingConfig(
        enabled=True,
        secret_key="sk_test_unit",
        checkout_success_url="https://app.example.com/success",
        checkout_cancel_url="https://app.example.com/cancel",
        portal_return_url="https://app.example.com/account",
        api_version="2026-06-24.dahlia",
        price_ids={"plus": "price_plus123"},
    )
    gateway = StripeGateway(config, client=client)

    customer_id = asyncio.run(
        gateway.create_customer(
            billing_account_id=account_id,
            user_id=user_id,
            email="person@example.com",
        )
    )
    checkout_url = asyncio.run(
        gateway.create_checkout_session(
            billing_account_id=account_id,
            user_id=user_id,
            customer_id=customer_id,
            plan_code="plus",
            price_id=config.require_price_id("plus"),
            idempotency_key="checkout-unit",
        )
    )
    portal_url = asyncio.run(gateway.create_portal_session(customer_id=customer_id))

    assert checkout_url.startswith("https://checkout.stripe.com/")
    assert portal_url.startswith("https://billing.stripe.com/")
    customer_params, customer_options = customers.calls[0]
    assert customer_params == {
        "email": "person@example.com",
        "metadata": {
            "cortex_billing_account_id": str(account_id),
            "cortex_user_id": str(user_id),
        },
    }
    assert customer_options == {
        "idempotency_key": f"cortex-customer-{account_id}",
        "stripe_version": "2026-06-24.dahlia",
    }

    checkout_params, checkout_options = checkout.calls[0]
    assert checkout_params["mode"] == "subscription"
    assert checkout_params["customer"] == "cus_unit123"
    assert checkout_params["line_items"] == [{"price": "price_plus123", "quantity": 1}]
    assert checkout_params["success_url"] == "https://app.example.com/success"
    assert checkout_params["cancel_url"] == "https://app.example.com/cancel"
    assert checkout_params["client_reference_id"] == str(account_id)
    assert checkout_params["ui_mode"] == "hosted_page"
    assert checkout_params["origin_context"] == "web"
    assert checkout_params["submit_type"] == "auto"
    assert checkout_params["billing_address_collection"] == "auto"
    assert checkout_params["phone_number_collection"] == {"enabled": False}
    assert checkout_params["automatic_tax"] == {"enabled": False}
    assert checkout_params["allow_promotion_codes"] is True
    assert checkout_params["payment_method_collection"] == "if_required"
    assert "discounts" not in checkout_params
    assert "amount" not in checkout_params
    assert "currency" not in checkout_params
    assert checkout_options["idempotency_key"].startswith("checkout-unit-")
    assert checkout_options["stripe_version"] == "2026-06-24.dahlia"
    assert portal.calls == [
        (
            {
                "customer": "cus_unit123",
                "return_url": "https://app.example.com/account",
            },
            {"stripe_version": "2026-06-24.dahlia"},
        )
    ]


@pytest.mark.unit
def test_gateway_normalizes_sdk_failures_without_exposing_provider_details():
    client, _, checkout, _ = _fake_stripe_client()
    checkout.response = RuntimeError("provider secret should never reach the API response")
    config = StripeBillingConfig(
        enabled=True,
        secret_key="sk_test_unit",
        checkout_success_url="https://app.example.com/success",
        checkout_cancel_url="https://app.example.com/cancel",
        portal_return_url="https://app.example.com/account",
        price_ids={"plus": "price_plus123"},
    )
    gateway = StripeGateway(config, client=client)

    with pytest.raises(BillingProviderError) as exc_info:
        asyncio.run(
            gateway.create_checkout_session(
                billing_account_id=uuid4(),
                user_id=uuid4(),
                customer_id="cus_unit123",
                plan_code="plus",
                price_id="price_plus123",
                idempotency_key="checkout-unit",
            )
        )

    assert "secret" not in str(exc_info.value).lower()


@pytest.mark.unit
def test_checkout_idempotency_key_is_scoped_to_request_parameters():
    client, _, checkout, _ = _fake_stripe_client()
    config = StripeBillingConfig(
        enabled=True,
        secret_key="sk_test_unit",
        checkout_success_url="https://app.example.com/success",
        checkout_cancel_url="https://app.example.com/cancel",
        portal_return_url="https://app.example.com/account",
        price_ids={"plus": "price_plus123"},
    )
    gateway = StripeGateway(config, client=client)
    account_id = uuid4()
    user_id = uuid4()

    for customer_id in ("cus_first123", "cus_first123", "cus_second123"):
        asyncio.run(
            gateway.create_checkout_session(
                billing_account_id=account_id,
                user_id=user_id,
                customer_id=customer_id,
                plan_code="plus",
                price_id="price_plus123",
                idempotency_key="checkout-unit",
            )
        )

    keys = [options["idempotency_key"] for _, options in checkout.calls]
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


@pytest.mark.unit
def test_provider_failure_details_expose_stripe_diagnostics():
    class FakeStripeError(Exception):
        http_status = 400
        code = "resource_missing"
        request_id = "req_unit123"
        error = SimpleNamespace(message="No such customer: 'cus_old123'")

    try:
        try:
            raise FakeStripeError("raw")
        except FakeStripeError as exc:
            raise BillingProviderError("Stripe Checkout Session creation failed") from exc
    except BillingProviderError as wrapped:
        details = provider_failure_details(wrapped)

    assert details == {
        "provider_error_type": "FakeStripeError",
        "provider_http_status": 400,
        "provider_code": "resource_missing",
        "provider_request_id": "req_unit123",
        "provider_message": "No such customer: 'cus_old123'",
    }


@pytest.mark.unit
def test_gateway_verifies_exact_raw_webhook_body_with_stripe_sdk():
    secret = "whsec_signed_unit"
    payload = json.dumps(
        {
            "id": "evt_signed123",
            "object": "event",
            "type": "customer.created",
            "created": int(time.time()),
            "data": {"object": {"id": "cus_signed123"}},
        },
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload.decode()}".encode()
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={signature}"
    config = StripeBillingConfig(
        enabled=True,
        secret_key="sk_test_unit",
        webhook_secret=secret,
        price_ids={"plus": "price_plus123"},
    )
    gateway = StripeGateway(config, client=SimpleNamespace())

    event = gateway.verify_webhook_event(payload=payload, signature=header)

    assert event["id"] == "evt_signed123"
    with pytest.raises(InvalidWebhookSignatureError):
        gateway.verify_webhook_event(payload=payload + b" ", signature=header)


class _FakeBillingGateway:
    def __init__(self):
        self.customers: list[dict] = []
        self.checkouts: list[dict] = []
        self.portals: list[dict] = []
        self.checkout_error: Exception | None = None

    async def create_customer(self, **kwargs):
        self.customers.append(kwargs)
        return "cus_route123"

    async def create_checkout_session(self, **kwargs):
        self.checkouts.append(kwargs)
        if self.checkout_error:
            raise self.checkout_error
        return "https://checkout.stripe.com/c/pay/route"

    async def create_portal_session(self, **kwargs):
        self.portals.append(kwargs)
        return "https://billing.stripe.com/p/session/route"


@pytest.fixture()
def billing_route_harness(monkeypatch):
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
    )
    tables = {table.name: table for table in (users, billing_accounts, subscriptions)}
    metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    user_id = uuid4()
    db.execute(insert(users).values(id=user_id, email="route-person@example.com"))
    db.commit()

    import db.tables as db_tables

    monkeypatch.setattr(db_tables, "get_table", tables.__getitem__)

    @contextmanager
    def test_uow():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    for name, value in _enabled_environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(billing_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(billing_route, "_db_uow", test_uow)
    fake_gateway = _FakeBillingGateway()
    monkeypatch.setattr(billing_route, "_gateway_factory", lambda _config: fake_gateway)

    app = FastAPI()
    app.include_router(billing_route.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=user_id,
    )
    client = TestClient(app)
    try:
        yield client, db, tables, fake_gateway, app, user_id
    finally:
        client.close()
        db.close()
        engine.dispose()


@pytest.mark.integration
def test_public_plans_returns_display_safe_server_catalogue(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, _, app, _ = billing_route_harness
    monkeypatch.setenv("BILLING_ENABLED", "false")
    monkeypatch.setattr(billing_route, "API_DB_ENABLED", False)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=None,
    )

    response = client.get("/v1/billing/plans")

    assert response.status_code == 200
    payload = response.json()
    assert payload["currency"] == "USD"
    assert payload["billing_period"] == "monthly"
    assert payload["billing_enabled"] is False
    assert [plan["code"] for plan in payload["plans"]] == ["free", "plus", "pro"]
    assert [plan["monthly_price"] for plan in payload["plans"]] == [0.0, 6.99, 12.99]
    assert [plan["code"] for plan in payload["plans"] if plan["recommended"]] == ["plus"]
    serialized = json.dumps(payload).lower()
    assert "stripe" not in serialized
    assert "price_id" not in serialized
    assert "customer" not in serialized


@pytest.mark.integration
def test_generation_estimate_uses_managed_auto_profile_without_reserving(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, _, _, _ = billing_route_harness
    monkeypatch.setattr(
        billing_route,
        "resolve_effective_subscription",
        lambda *_args, **_kwargs: SimpleNamespace(plan=SimpleNamespace(code="plus")),
    )
    monkeypatch.setattr(
        billing_route,
        "load_allowance_usage",
        lambda *_args, **_kwargs: {
            "ai_credits": SimpleNamespace(remaining=1_000_000),
        },
    )

    response = client.post(
        "/v1/billing/estimate-generation",
        json={
            "prompt": "Explain the rollout plan",
            "generation": {"profile": "auto"},
            "targets": [{"provider": "openai", "model": "gpt-4o-mini"}],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["targets"] == [
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "profile": "auto",
            "effective_max_output_tokens": 4096,
            "estimated_max_ai_credits": payload["targets"][0]["estimated_max_ai_credits"],
        }
    ]
    assert payload["estimated_max_ai_credits"] > 0
    assert payload["remaining_ai_credits"] == 1_000_000
    assert payload["can_authorize"] is True
    assert payload["temporary_hold_released_after_settlement"] is True


@pytest.mark.integration
def test_current_subscription_returns_effective_provider_safe_state(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, _, _, _ = billing_route_harness
    effective = SimpleNamespace(
        plan=SimpleNamespace(code="plus"),
        status="active",
        provider="stripe",
        provider_subscription_id="sub_secret123",
        current_period_start=datetime(2026, 7, 18, tzinfo=UTC),
        current_period_end=datetime(2026, 8, 18, tzinfo=UTC),
        cancel_at_period_end=False,
    )
    monkeypatch.setattr(
        billing_route,
        "resolve_effective_subscription",
        lambda *_args, **_kwargs: effective,
    )

    response = client.get("/v1/billing/subscription")

    assert response.status_code == 200
    assert response.json() == {
        "plan_code": "plus",
        "status": "active",
        "provider": "stripe",
        "current_period_start": "2026-07-18T00:00:00Z",
        "current_period_end": "2026-08-18T00:00:00Z",
        "cancel_at_period_end": False,
        "can_manage": True,
    }
    assert "sub_secret123" not in response.text


@pytest.mark.integration
def test_current_subscription_requires_database_mode(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, _, _, _ = billing_route_harness
    monkeypatch.setattr(billing_route, "API_DB_ENABLED", False)

    response = client.get("/v1/billing/subscription")

    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "billing_database_required"


@pytest.mark.integration
def test_checkout_creates_and_reuses_customer_with_server_price(billing_route_harness):
    client, db, tables, gateway, _, user_id = billing_route_harness

    first = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )
    second = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )

    assert first.status_code == 200
    assert first.json() == {
        "checkout_url": "https://checkout.stripe.com/c/pay/route",
        "destination": "checkout",
    }
    assert second.status_code == 200
    assert len(gateway.customers) == 1
    assert gateway.customers[0]["user_id"] == user_id
    assert gateway.customers[0]["email"] == "route-person@example.com"
    assert len(gateway.checkouts) == 2
    assert all(call["price_id"] == "price_plus123" for call in gateway.checkouts)
    assert all(call["customer_id"] == "cus_route123" for call in gateway.checkouts)
    assert gateway.checkouts[0]["idempotency_key"] == gateway.checkouts[1]["idempotency_key"]
    persisted_customer = db.execute(
        select(tables["billing_accounts"].c.stripe_customer_id).where(
            tables["billing_accounts"].c.owner_id == user_id
        )
    ).scalar_one()
    assert persisted_customer == "cus_route123"


@pytest.mark.integration
def test_checkout_rejects_client_controlled_billing_fields(billing_route_harness):
    client, _, _, gateway, _, _ = billing_route_harness

    response = client.post(
        "/v1/billing/checkout-session",
        json={
            "plan_code": "plus",
            "billing_period": "monthly",
            "price_id": "price_attacker",
            "amount": 1,
            "customer_id": "cus_attacker",
            "success_url": "https://attacker.example/success",
        },
    )

    assert response.status_code == 422
    assert gateway.customers == []
    assert gateway.checkouts == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("plan_code", "error_code"),
    [
        ("missing", "invalid_subscription_plan"),
        ("free", "paid_subscription_plan_required"),
    ],
)
def test_checkout_rejects_unknown_and_free_plans(
    billing_route_harness,
    plan_code,
    error_code,
):
    client, _, _, gateway, _, _ = billing_route_harness

    response = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": plan_code, "billing_period": "monthly"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == error_code
    assert gateway.checkouts == []


@pytest.mark.integration
def test_live_subscriber_is_redirected_to_portal_not_checkout(billing_route_harness):
    client, db, _, gateway, _, user_id = billing_route_harness
    account = repository.get_or_create_billing_account_for_user(db, user_id)
    repository.set_stripe_customer_id(db, account["id"], "cus_existing123")
    repository.upsert_subscription_snapshot(
        db,
        repository.SubscriptionSnapshot(
            billing_account_id=account["id"],
            provider="stripe",
            provider_subscription_id="sub_existing123",
            provider_price_id="price_plus123",
            plan_code="plus",
            status="active",
        ),
    )
    db.commit()

    response = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "pro", "billing_period": "monthly"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "checkout_url": "https://billing.stripe.com/p/session/route",
        "destination": "portal",
    }
    assert gateway.checkouts == []
    assert gateway.portals == [{"customer_id": "cus_existing123"}]


@pytest.mark.integration
def test_portal_requires_existing_customer_and_rejects_client_customer(billing_route_harness):
    client, _, _, gateway, _, _ = billing_route_harness

    missing = client.post("/v1/billing/portal-session", json={})
    injected = client.post(
        "/v1/billing/portal-session",
        json={"customer_id": "cus_attacker"},
    )

    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "stripe_customer_required"
    assert injected.status_code == 422
    assert gateway.portals == []


@pytest.mark.integration
def test_portal_uses_persisted_customer_and_server_return_url(billing_route_harness):
    client, db, _, gateway, _, user_id = billing_route_harness
    account = repository.get_or_create_billing_account_for_user(db, user_id)
    repository.set_stripe_customer_id(db, account["id"], "cus_existing123")
    db.commit()

    response = client.post("/v1/billing/portal-session")

    assert response.status_code == 200
    assert response.json() == {"portal_url": "https://billing.stripe.com/p/session/route"}
    assert gateway.portals == [{"customer_id": "cus_existing123"}]


@pytest.mark.integration
def test_billing_disabled_returns_structured_503_without_gateway_calls(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, gateway, _, _ = billing_route_harness
    monkeypatch.setenv("BILLING_ENABLED", "false")

    checkout = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )
    portal = client.post("/v1/billing/portal-session", json={})

    assert checkout.status_code == 503
    assert checkout.json()["detail"] == {
        "code": "billing_not_configured",
        "message": "Billing is not configured in this environment.",
    }
    assert portal.status_code == 503
    assert gateway.customers == []
    assert gateway.checkouts == []
    assert gateway.portals == []


@pytest.mark.integration
def test_billing_disabled_precedes_database_mode_error(
    billing_route_harness,
    monkeypatch,
):
    client, _, _, _, _, _ = billing_route_harness
    monkeypatch.setenv("BILLING_ENABLED", "false")
    monkeypatch.setattr(billing_route, "API_DB_ENABLED", False)

    response = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "billing_not_configured"


@pytest.mark.integration
def test_billing_routes_reject_api_key_only_auth(billing_route_harness):
    client, _, _, gateway, app, _ = billing_route_harness
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key="service-key",
        cognito_claims=None,
        user_id=None,
    )

    response = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )
    subscription = client.get("/v1/billing/subscription")

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "session_auth_required"
    assert subscription.status_code == 403
    assert subscription.json()["detail"]["code"] == "session_auth_required"
    assert gateway.customers == []


@pytest.mark.integration
def test_provider_failure_returns_safe_502(billing_route_harness):
    client, _, _, gateway, _, _ = billing_route_harness
    gateway.checkout_error = BillingProviderError("raw provider detail")

    response = client.post(
        "/v1/billing/checkout-session",
        json={"plan_code": "plus", "billing_period": "monthly"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "code": "billing_provider_unavailable",
        "message": "The billing provider is temporarily unavailable. Please try again.",
    }
    assert "raw provider detail" not in response.text
