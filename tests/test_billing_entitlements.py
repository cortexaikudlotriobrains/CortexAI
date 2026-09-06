from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Uuid,
    create_engine,
    insert,
    select,
)
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db import billing_repository as repository
from server.billing.account_service import get_or_create_user_billing_account
from server.billing.entitlement_service import (
    AllowanceUsage,
    ModelTargetIntent,
    SubscriptionRequestIntent,
    evaluate_entitlement,
    required_quantities,
)
from server.billing.entitlement_service import EntitlementDenial
from server.billing.errors import BillingIdentityError, entitlement_http_exception
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import (
    EffectiveSubscription,
    resolve_effective_subscription,
)
from server.dependencies import AuthResult, get_auth
from server.routes import entitlements as entitlements_route


@pytest.fixture()
def billing_service_db(monkeypatch):
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
        Column("currency", String(3), nullable=False, default="USD"),
        Column("country", String(2)),
        Column("created_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        Column("updated_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        Index("uq_billing_accounts_owner", "owner_type", "owner_id", unique=True),
    )
    subscriptions = Table(
        "subscriptions",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("provider", String(32), nullable=False, default="stripe"),
        Column("provider_subscription_id", String(255)),
        Column("provider_price_id", String(255)),
        Column("plan_code", String(64), nullable=False),
        Column("status", String(64), nullable=False),
        Column("current_period_start", DateTime(timezone=True)),
        Column("current_period_end", DateTime(timezone=True)),
        Column("cancel_at_period_end", Boolean, nullable=False, default=False),
        Column("canceled_at", DateTime(timezone=True)),
        Column("trial_end", DateTime(timezone=True)),
        Column("grace_until", DateTime(timezone=True)),
        Column("latest_invoice_id", String(255)),
        Column("last_provider_event_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        Column("updated_at", DateTime(timezone=True), nullable=False, default=datetime.now),
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
        Column("status", String(32), nullable=False, default="active"),
        Column("created_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        Column("updated_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        CheckConstraint("ends_at > starts_at"),
        Index("uq_usage_period_account_start", "billing_account_id", "starts_at", unique=True),
    )
    usage_counters = Table(
        "usage_counters",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("usage_period_id", Uuid, ForeignKey("usage_periods.id"), nullable=False),
        Column("meter_key", String(64), nullable=False),
        Column("used_quantity", BigInteger, nullable=False, default=0),
        Column("reserved_quantity", BigInteger, nullable=False, default=0),
        Column("updated_at", DateTime(timezone=True), nullable=False, default=datetime.now),
        CheckConstraint("used_quantity >= 0 AND reserved_quantity >= 0"),
        Index(
            "uq_usage_counter_period_meter",
            "usage_period_id",
            "meter_key",
            unique=True,
        ),
    )
    credit_transactions = Table(
        "credit_transactions",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, nullable=False),
        Column("usage_period_id", Uuid, nullable=False),
        Column("reservation_id", Uuid),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("item_index", Integer, nullable=False, default=0),
        Column("item_type", String(32), nullable=False),
        Column("provider", String(64)),
        Column("model", String(255)),
        Column("input_tokens", BigInteger, nullable=False, default=0),
        Column("normal_input_tokens", BigInteger, nullable=False, default=0),
        Column("cached_input_tokens", BigInteger, nullable=False, default=0),
        Column("cache_write_tokens", BigInteger, nullable=False, default=0),
        Column("reasoning_tokens", BigInteger, nullable=False, default=0),
        Column("output_tokens", BigInteger, nullable=False, default=0),
        Column("input_credits", BigInteger, nullable=False, default=0),
        Column("normal_input_credits", BigInteger, nullable=False, default=0),
        Column("cached_input_credits", BigInteger, nullable=False, default=0),
        Column("cache_write_credits", BigInteger, nullable=False, default=0),
        Column("output_credits", BigInteger, nullable=False, default=0),
        Column("fixed_credits", BigInteger, nullable=False, default=0),
        Column("total_credits", BigInteger, nullable=False),
        Column("uncached_equivalent_credits", BigInteger, nullable=False, default=0),
        Column("cache_savings_credits", BigInteger, nullable=False, default=0),
        Column("provider_cost_usd", Float, nullable=False, default=0),
        Column("usage_estimated", Boolean, nullable=False, default=False),
        Column("pricing_version", String(64), nullable=False),
        Column("metadata", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), nullable=False, default=datetime.now),
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
            credit_transactions,
        )
    }
    metadata.create_all(engine)

    import db.tables as db_tables

    monkeypatch.setattr(db_tables, "get_table", tables.__getitem__)
    monkeypatch.setenv("BILLING_ENABLED", "false")
    monkeypatch.delenv("DEV_SUBSCRIPTION_PLAN", raising=False)
    monkeypatch.delenv("SUBSCRIPTION_PAYMENT_GRACE_DAYS", raising=False)
    db = sessionmaker(bind=engine)()
    try:
        yield db, tables
    finally:
        db.close()
        engine.dispose()


def _create_user(db, tables) -> object:
    user_id = uuid4()
    db.execute(insert(tables["users"]).values(id=user_id, email=f"{user_id}@example.com"))
    db.flush()
    return user_id


def _snapshot(
    db,
    *,
    account_id,
    plan_code="plus",
    status="active",
    starts_at=datetime(2026, 7, 1, tzinfo=UTC),
    ends_at=datetime(2026, 8, 1, tzinfo=UTC),
    cancel_at_period_end=False,
    grace_until=None,
):
    return repository.upsert_subscription_snapshot(
        db,
        repository.SubscriptionSnapshot(
            billing_account_id=account_id,
            provider="stripe",
            provider_subscription_id=f"sub_{uuid4().hex}",
            plan_code=plan_code,
            status=status,
            current_period_start=starts_at,
            current_period_end=ends_at,
            cancel_at_period_end=cancel_at_period_end,
            grace_until=grace_until,
            last_provider_event_at=datetime(2026, 7, 15, tzinfo=UTC),
        ),
    )


def _effective(plan_code: str) -> EffectiveSubscription:
    plan = get_plan_catalog().require(plan_code)
    return EffectiveSubscription(
        billing_account_id=uuid4(),
        usage_period_id=uuid4(),
        plan=plan,
        source="test",
        provider=None,
        provider_subscription_id=None,
        status="active" if plan_code != "free" else "free",
        current_period_start=datetime(2026, 7, 1, tzinfo=UTC),
        current_period_end=datetime(2026, 8, 1, tzinfo=UTC),
        cancel_at_period_end=False,
        grace_until=None,
    )


def _allowances(plan_code: str) -> dict[str, AllowanceUsage]:
    plan = get_plan_catalog().require(plan_code)
    return {
        meter: AllowanceUsage(used=0, reserved=0, limit=int(limit), remaining=int(limit))
        for meter, limit in vars(plan.allowances).items()
    }


@pytest.mark.unit
def test_account_service_validates_owner_and_lazy_creates(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)

    first = get_or_create_user_billing_account(db, user_id)
    second = get_or_create_user_billing_account(db, user_id)

    assert first.id == second.id
    assert first.owner_id == user_id
    with pytest.raises(BillingIdentityError):
        get_or_create_user_billing_account(db, uuid4())


@pytest.mark.unit
def test_existing_user_resolves_to_free_calendar_period_by_default(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)

    effective = resolve_effective_subscription(db, user_id, now=now)
    repeated = resolve_effective_subscription(db, user_id, now=now)

    assert effective.plan.code == "free"
    assert effective.source == "free_default"
    assert effective.current_period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert effective.current_period_end == datetime(2026, 8, 1, tzinfo=UTC)
    assert repeated.usage_period_id == effective.usage_period_id
    assert db.execute(select(tables["billing_accounts"])).all()


@pytest.mark.parametrize(
    ("status", "plan_code"),
    [("active", "plus"), ("active", "pro")],
)
def test_active_snapshots_grant_paid_plan(
    billing_service_db,
    monkeypatch,
    status,
    plan_code,
):
    db, tables = billing_service_db
    monkeypatch.setenv("BILLING_ENABLED", "true")
    user_id = _create_user(db, tables)
    account = get_or_create_user_billing_account(db, user_id)
    snapshot = _snapshot(db, account_id=account.id, status=status, plan_code=plan_code)

    effective = resolve_effective_subscription(
        db,
        user_id,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert effective.plan.code == plan_code
    assert effective.status == status
    period = repository.get_active_usage_period(db, account.id, datetime(2026, 7, 18))
    assert period["subscription_id"] == snapshot["id"]
    assert period["plan_code"] == plan_code


def test_trialing_snapshot_does_not_grant_paid_access(
    billing_service_db,
    monkeypatch,
):
    db, tables = billing_service_db
    monkeypatch.setenv("BILLING_ENABLED", "true")
    user_id = _create_user(db, tables)
    account = get_or_create_user_billing_account(db, user_id)
    _snapshot(db, account_id=account.id, status="trialing", plan_code="plus")

    effective = resolve_effective_subscription(
        db,
        user_id,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert effective.plan.code == "free"
    assert effective.status == "trialing"


@pytest.mark.parametrize(
    ("status", "grace_until", "cancel_at_period_end", "now", "expected_plan"),
    [
        (
            "past_due",
            datetime(2026, 7, 20, tzinfo=UTC),
            False,
            datetime(2026, 7, 18, tzinfo=UTC),
            "plus",
        ),
        ("past_due", None, False, datetime(2026, 7, 17, tzinfo=UTC), "plus"),
        ("past_due", None, False, datetime(2026, 7, 19, tzinfo=UTC), "free"),
        (
            "past_due",
            datetime(2026, 7, 17, tzinfo=UTC),
            False,
            datetime(2026, 7, 18, tzinfo=UTC),
            "free",
        ),
        ("unpaid", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
        ("incomplete", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
        ("incomplete_expired", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
        ("paused", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
        ("canceled", None, True, datetime(2026, 7, 18, tzinfo=UTC), "plus"),
        ("canceled", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
        ("canceled", None, True, datetime(2026, 8, 2, tzinfo=UTC), "free"),
        ("unexpected", None, False, datetime(2026, 7, 18, tzinfo=UTC), "free"),
    ],
)
def test_subscription_lifecycle_policy(
    billing_service_db,
    monkeypatch,
    status,
    grace_until,
    cancel_at_period_end,
    now,
    expected_plan,
):
    db, tables = billing_service_db
    monkeypatch.setenv("BILLING_ENABLED", "true")
    user_id = _create_user(db, tables)
    account = get_or_create_user_billing_account(db, user_id)
    _snapshot(
        db,
        account_id=account.id,
        status=status,
        cancel_at_period_end=cancel_at_period_end,
        grace_until=grace_until,
    )

    effective = resolve_effective_subscription(db, user_id, now=now)

    assert effective.plan.code == expected_plan
    if expected_plan == "free":
        assert effective.provider_subscription_id is not None


@pytest.mark.unit
def test_unknown_plan_fails_conservatively_to_free(
    billing_service_db,
    monkeypatch,
    caplog,
):
    db, tables = billing_service_db
    monkeypatch.setenv("BILLING_ENABLED", "true")
    user_id = _create_user(db, tables)
    account = get_or_create_user_billing_account(db, user_id)
    _snapshot(db, account_id=account.id, plan_code="missing-plan")

    effective = resolve_effective_subscription(
        db,
        user_id,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert effective.plan.code == "free"
    assert effective.status == "configuration_error"
    assert "unknown plan" in caplog.text.lower()


@pytest.mark.unit
def test_development_override_is_local_only(billing_service_db, monkeypatch):
    db, tables = billing_service_db
    monkeypatch.setenv("DEV_SUBSCRIPTION_PLAN", "pro")

    local_user = _create_user(db, tables)
    monkeypatch.setenv("APP_ENV", "local")
    local = resolve_effective_subscription(
        db,
        local_user,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    production_user = _create_user(db, tables)
    monkeypatch.setenv("APP_ENV", "production")
    production = resolve_effective_subscription(
        db,
        production_user,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert local.plan.code == "pro"
    assert local.source == "development_override"
    assert local.provider_subscription_id is None
    assert production.plan.code == "free"


@pytest.mark.unit
def test_unrestricted_development_override_is_explicit_and_local_only(
    billing_service_db,
    monkeypatch,
):
    db, tables = billing_service_db
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DEV_SUBSCRIPTION_PLAN", "unrestricted")

    disabled_user = _create_user(db, tables)
    disabled = resolve_effective_subscription(
        db,
        disabled_user,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    monkeypatch.setenv("DEV_SUBSCRIPTION_BYPASS_ENABLED", "true")
    enabled_user = _create_user(db, tables)
    enabled = resolve_effective_subscription(
        db,
        enabled_user,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )

    assert disabled.plan.code == "free"
    assert enabled.plan.code == "pro"
    assert enabled.plan.display_name == "Local Unrestricted"
    assert enabled.source == "development_override"
    assert enabled.provider is None
    assert min(vars(enabled.plan.allowances).values()) >= 1_000_000_000
    assert enabled.plan.limits.max_files_per_request == 5
    assert enabled.plan.limits.max_file_bytes == 20_000_000

    monkeypatch.setenv("BILLING_ENABLED", "true")
    billing_enabled_user = _create_user(db, tables)
    billing_enabled = resolve_effective_subscription(
        db,
        billing_enabled_user,
        now=datetime(2026, 7, 18, tzinfo=UTC),
    )
    assert billing_enabled.plan.code == "free"


@pytest.mark.unit
def test_development_override_can_follow_existing_free_period(
    billing_service_db,
    monkeypatch,
):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    now = datetime(2026, 7, 18, tzinfo=UTC)
    free = resolve_effective_subscription(db, user_id, now=now)

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEV_SUBSCRIPTION_PLAN", "pro")
    overridden = resolve_effective_subscription(db, user_id, now=now)

    monkeypatch.delenv("DEV_SUBSCRIPTION_PLAN")
    returned_to_free = resolve_effective_subscription(db, user_id, now=now)

    assert overridden.plan.code == "pro"
    assert overridden.current_period_start == datetime(2026, 7, 1, tzinfo=UTC).replace(
        microsecond=1
    )
    assert returned_to_free.plan.code == "free"
    assert returned_to_free.usage_period_id == free.usage_period_id


@pytest.mark.parametrize("plan_code", ["free", "plus"])
def test_premium_model_is_denied_until_pro(plan_code):
    effective = _effective(plan_code)
    intent = SubscriptionRequestIntent(
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "premium-model", "premium"),),
    )

    decision = evaluate_entitlement(effective, intent, _allowances(plan_code))

    assert decision.allowed is False
    assert decision.denial.code == "model_not_in_plan"
    assert decision.denial.recommended_plan == "pro"


@pytest.mark.unit
def test_economical_and_standard_models_are_allowed_on_free():
    effective = _effective("free")
    for billing_class in ("economical", "standard"):
        decision = evaluate_entitlement(
            effective,
            SubscriptionRequestIntent(
                operation_type="ask",
                model_targets=(ModelTargetIntent("openai", "model", billing_class),),
            ),
            _allowances("free"),
        )
        assert decision.allowed is True


@pytest.mark.unit
def test_pro_allows_premium_and_three_model_compare():
    effective = _effective("pro")
    intent = SubscriptionRequestIntent(
        operation_type="compare",
        model_targets=(
            ModelTargetIntent("openai", "standard", "standard"),
            ModelTargetIntent("gemini", "advanced", "advanced"),
            ModelTargetIntent("xai", "premium", "premium"),
        ),
        research_enabled=True,
        estimated_credits=42_000,
    )

    decision = evaluate_entitlement(effective, intent, _allowances("pro"))

    assert decision.allowed is True
    assert decision.required_quantities == {"ai_credits": 42_000}


@pytest.mark.parametrize("plan_code", ["free", "plus"])
def test_three_model_compare_recommends_pro(plan_code):
    targets = tuple(ModelTargetIntent("openai", f"model-{index}", "standard") for index in range(3))
    decision = evaluate_entitlement(
        _effective(plan_code),
        SubscriptionRequestIntent(operation_type="compare", model_targets=targets),
        _allowances(plan_code),
    )

    assert decision.allowed is False
    assert decision.denial.feature == "compare_model_count"
    assert decision.denial.recommended_plan == "pro"


@pytest.mark.unit
def test_unified_credit_allowance_fails_with_actionable_denial():
    effective = _effective("free")
    for intent in (
        SubscriptionRequestIntent(
            operation_type="ask",
            research_enabled=True,
            estimated_credits=15_001,
        ),
        SubscriptionRequestIntent(operation_type="optimize", estimated_credits=15_001),
    ):
        allowances = _allowances("free")
        limit = allowances["ai_credits"].limit
        allowances["ai_credits"] = AllowanceUsage(limit, 0, limit, 0)

        decision = evaluate_entitlement(effective, intent, allowances)

        assert decision.allowed is False
        assert decision.denial.code == "insufficient_credits"
        assert decision.denial.meter == "ai_credits"
        assert decision.denial.required == 15_001
        assert decision.denial.remaining == 0
        assert decision.denial.reset_at == effective.current_period_end


@pytest.mark.unit
def test_disabled_research_feature_is_denied_before_allowance_evaluation():
    effective = _effective("free")
    effective = replace(
        effective,
        plan=replace(
            effective.plan,
            entitlements=replace(effective.plan.entitlements, research_enabled=False),
        ),
    )

    decision = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(operation_type="ask", research_enabled=True),
        {},
    )

    assert decision.denial.code == "feature_not_in_plan"
    assert decision.denial.feature == "research"


@pytest.mark.unit
def test_upload_has_no_credit_quantity():
    effective = _effective("free")
    allowances = _allowances("free")
    decision = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(
            operation_type="file_upload",
            attachment_count=1,
            total_attachment_bytes=1,
            attachment_sizes=(1,),
        ),
        allowances,
    )

    assert decision.allowed is True
    assert decision.required_quantities == {}


@pytest.mark.unit
def test_file_count_and_individual_size_limits_are_enforced():
    effective = _effective("free")
    allowances = _allowances("free")

    count_decision = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(operation_type="ask", attachment_count=2),
        allowances,
    )
    size_decision = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(
            operation_type="file_upload",
            attachment_count=1,
            total_attachment_bytes=10_000_001,
            attachment_sizes=(10_000_001,),
        ),
        allowances,
    )

    assert count_decision.denial.feature == "attachment_count"
    assert count_decision.denial.recommended_plan == "plus"
    assert size_decision.denial.feature == "attachment_size"
    assert size_decision.denial.recommended_plan == "plus"


@pytest.mark.unit
def test_compare_with_files_computes_once_per_turn_quantities():
    intent = SubscriptionRequestIntent(
        operation_type="compare",
        model_targets=(
            ModelTargetIntent("openai", "one", "advanced"),
            ModelTargetIntent("gemini", "two", "advanced"),
        ),
        attachment_count=1,
        total_attachment_bytes=100,
        estimated_credits=25_000,
    )

    assert required_quantities(intent) == {"ai_credits": 25_000}


@pytest.mark.unit
def test_missing_billing_class_and_counter_fail_conservatively():
    effective = _effective("free")
    invalid_model = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(
            operation_type="ask",
            model_targets=(ModelTargetIntent("openai", "model", ""),),
        ),
        _allowances("free"),
    )
    missing_counter = evaluate_entitlement(
        effective,
        SubscriptionRequestIntent(
            operation_type="ask",
            research_enabled=True,
            estimated_credits=15_000,
        ),
        {},
    )

    assert invalid_model.denial.code == "subscription_configuration_error"
    assert missing_counter.denial.code == "subscription_configuration_error"
    assert entitlement_http_exception(invalid_model.denial).status_code == 500

    payment_required = EntitlementDenial(
        code="subscription_payment_required",
        message="Payment is required to restore paid access.",
    )
    assert entitlement_http_exception(payment_required).status_code == 402


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("feature_not_in_plan", 403),
        ("model_not_in_plan", 403),
        ("monthly_allowance_exhausted", 429),
        ("insufficient_credits", 402),
        ("subscription_payment_required", 402),
        ("subscription_configuration_error", 500),
    ],
)
def test_entitlement_denial_codes_map_to_safe_http_statuses(code, expected_status):
    denial = EntitlementDenial(code=code, message="Safe subscription error.")

    error = entitlement_http_exception(denial)

    assert error.status_code == expected_status
    assert error.detail["message"] == "Safe subscription error."


def test_entitlement_reset_timestamp_is_json_safe():
    denial = EntitlementDenial(
        code="insufficient_credits",
        message="Not enough credits.",
        required=15_000,
        remaining=2_000,
        reset_at=datetime(2026, 8, 1, tzinfo=UTC),
    )

    error = entitlement_http_exception(denial)

    assert error.detail["reset_at"] == "2026-08-01T00:00:00+00:00"
    assert error.detail["required"] == 15_000
    assert error.detail["remaining"] == 2_000


@pytest.mark.integration
def test_entitlements_endpoint_lazy_creates_free_snapshot_with_all_counters(
    billing_service_db,
    monkeypatch,
):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    db.commit()

    @contextmanager
    def test_uow():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    monkeypatch.setattr(entitlements_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(entitlements_route, "_db_uow", test_uow)
    app = FastAPI()
    app.include_router(entitlements_route.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=user_id,
    )

    with TestClient(app) as client:
        response = client.get("/v1/entitlements")

    assert response.status_code == 200
    payload = response.json()
    assert payload["plan"]["code"] == "free"
    assert set(payload["allowances"]) == repository.ALLOWED_USAGE_METERS
    assert all(item["remaining"] >= 0 for item in payload["allowances"].values())
    assert payload["limits"] == {
        "max_files_per_request": 1,
        "max_file_bytes": 10_000_000,
        "max_active_work_runs": 0,
        "max_tool_connections": 0,
        "max_mcp_servers_per_run": 0,
        "max_work_credit_budget": 0,
    }
    assert payload["features"]["work_enabled"] is False
    assert payload["period"]["starts_at"].endswith("Z")
    assert payload["period"]["ends_at"].endswith("Z")
    assert len(db.execute(select(tables["usage_counters"])).all()) == 1


@pytest.mark.integration
def test_credit_transactions_endpoint_returns_itemized_reconciliation(
    billing_service_db,
    monkeypatch,
):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    effective = resolve_effective_subscription(
        db,
        user_id,
        now=datetime(2026, 7, 29, tzinfo=UTC),
    )
    db.execute(
        insert(tables["credit_transactions"]).values(
            id=uuid4(),
            billing_account_id=effective.billing_account_id,
            usage_period_id=effective.usage_period_id,
            reservation_id=uuid4(),
            request_id="credit-route-request",
            operation_type="ask",
            item_index=0,
            item_type="model",
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            input_credits=100,
            output_credits=200,
            fixed_credits=0,
            total_credits=300,
            provider_cost_usd=0.001,
            usage_estimated=False,
            pricing_version="2026-07-29",
            metadata={
                "credit_activity_id": "activity-credit-route",
                "file_context": True,
                "initial_query": "How do atomic credit reservations work?",
            },
            created_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        )
    )
    db.commit()

    @contextmanager
    def test_uow():
        yield db

    monkeypatch.setattr(entitlements_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(entitlements_route, "_db_uow", test_uow)
    app = FastAPI()
    app.include_router(entitlements_route.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=user_id,
    )

    with TestClient(app) as client:
        response = client.get("/v1/credits/transactions?limit=20&offset=0")

    assert response.status_code == 200
    item = response.json()["items"][0]
    expected = {
        "id": item["id"],
        "request_id": "credit-route-request",
        "activity_id": "activity-credit-route",
        "query": "How do atomic credit reservations work?",
        "operation_type": "ask",
        "item_type": "model",
        "provider": "openai",
        "model": "gpt-4.1-mini",
        "input_tokens": 100,
        "output_tokens": 50,
        "input_credits": 100,
        "output_credits": 200,
        "fixed_credits": 0,
        "total_credits": 300,
        "provider_cost_usd": 0.001,
        "usage_estimated": False,
        "pricing_version": "2026-07-29",
        "metadata": {
            "credit_activity_id": "activity-credit-route",
            "file_context": True,
            "initial_query": "How do atomic credit reservations work?",
        },
        "created_at": "2026-07-29T12:00:00Z",
    }
    assert {key: item[key] for key in expected} == expected
    assert item["normal_input_tokens"] == 0
    assert item["cached_input_tokens"] == 0
    assert item["cache_write_tokens"] == 0
    assert item["reasoning_tokens"] == 0
    assert item["cache_savings_credits"] == 0


@pytest.mark.integration
def test_entitlements_endpoint_requires_authentication(monkeypatch):
    monkeypatch.setenv("API_KEYS", "test-key")
    monkeypatch.setattr(entitlements_route, "API_DB_ENABLED", True)
    app = FastAPI()
    app.include_router(entitlements_route.router)

    with TestClient(app) as client:
        response = client.get("/v1/entitlements")

    assert response.status_code == 401


@pytest.mark.integration
def test_entitlements_endpoint_requires_database_mode(monkeypatch):
    monkeypatch.setattr(entitlements_route, "API_DB_ENABLED", False)
    app = FastAPI()
    app.include_router(entitlements_route.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None,
        cognito_claims=None,
        user_id=uuid4(),
    )

    with TestClient(app) as client:
        response = client.get("/v1/entitlements")

    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "billing_database_required"
