from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Uuid,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import sessionmaker

from db import billing_repository as repository
from server.billing.credit_calculator import calculate_research_credit_charge
from server.billing.enforcement_service import (
    BillableModelUsage,
    authorize_and_reserve_usage,
    finalize_reserved_usage,
)
from server.billing.entitlement_service import ModelTargetIntent
from server.billing.errors import (
    EntitlementDeniedError,
    UsageReservationConflictError,
)
from server.billing.metering_service import (
    expire_stale_reservations,
    release_usage,
    reserve_usage,
    settle_usage,
    settle_usage_with_supplement,
)
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import (
    EffectiveSubscription,
    resolve_effective_subscription,
)


def test_grant_revocation_keeps_reserved_settlement_on_original_ledger(metering_db):
    from server.billing.grant_service import issue_subscription_grant, revoke_subscription_grant

    db, tables = metering_db
    user_id = _user(db, tables)
    now = datetime.now(UTC)
    grant = issue_subscription_grant(
        db,
        user_id,
        plan_code="pro",
        expires_at=now + timedelta(days=90),
        granted_by="operator",
        reason="beta",
        now=now,
    )
    effective = resolve_effective_subscription(db, user_id)
    reservation = authorize_and_reserve_usage(
        db,
        user_id=user_id,
        request_id="grant-in-flight",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        input_text="Explain reservations.",
        max_output_tokens=500,
    )
    revoke_subscription_grant(db, user_id, revoked_by="operator", reason="ended")
    assert resolve_effective_subscription(db, user_id).plan.code == "free"
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=100,
                output_tokens=200,
                provider_cost_usd=0.001,
            ),
        ),
        research_provider_credits_used=0,
        file_analysis_performed=False,
    )
    item = db.execute(select(tables["credit_transactions"])).mappings().one()
    assert item["usage_period_id"] == effective.usage_period_id
    assert item["total_credits"] == 900
    assert _counter(db, tables, effective.usage_period_id)["reserved_quantity"] == 0
    period = (
        db.execute(
            select(tables["usage_periods"]).where(
                tables["usage_periods"].c.id == effective.usage_period_id
            )
        )
        .mappings()
        .one()
    )
    assert period["status"] == "closed"
    assert period["subscription_grant_id"] == grant["id"]


@pytest.fixture()
def metering_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
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
    usage_periods = Table(
        "usage_periods",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("subscription_id", Uuid),
        Column("plan_code", String(64), nullable=False),
        Column("starts_at", DateTime(timezone=True), nullable=False),
        Column("ends_at", DateTime(timezone=True), nullable=False),
        Column("status", String(32), nullable=False, server_default="active"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Index("uq_usage_period_account_start", "billing_account_id", "starts_at", unique=True),
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
        Index("uq_usage_counter_period_meter", "usage_period_id", "meter_key", unique=True),
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
        Column(
            "last_activity_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        Column("settled_at", DateTime(timezone=True)),
        Column("released_at", DateTime(timezone=True)),
        Index("uq_usage_reservations_request", "billing_account_id", "request_id", unique=True),
    )
    credit_transactions = Table(
        "credit_transactions",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("usage_period_id", Uuid, ForeignKey("usage_periods.id"), nullable=False),
        Column("reservation_id", Uuid, ForeignKey("usage_reservations.id")),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("item_index", Integer, nullable=False, server_default="0"),
        Column("item_type", String(32), nullable=False),
        Column("provider", String(64)),
        Column("model", String(255)),
        Column("input_tokens", BigInteger, nullable=False, server_default="0"),
        Column("normal_input_tokens", BigInteger, nullable=False, server_default="0"),
        Column("cached_input_tokens", BigInteger, nullable=False, server_default="0"),
        Column("cache_write_tokens", BigInteger, nullable=False, server_default="0"),
        Column("reasoning_tokens", BigInteger, nullable=False, server_default="0"),
        Column("output_tokens", BigInteger, nullable=False, server_default="0"),
        Column("input_credits", BigInteger, nullable=False, server_default="0"),
        Column("normal_input_credits", BigInteger, nullable=False, server_default="0"),
        Column("cached_input_credits", BigInteger, nullable=False, server_default="0"),
        Column("cache_write_credits", BigInteger, nullable=False, server_default="0"),
        Column("output_credits", BigInteger, nullable=False, server_default="0"),
        Column("fixed_credits", BigInteger, nullable=False, server_default="0"),
        Column("total_credits", BigInteger, nullable=False),
        Column("uncached_equivalent_credits", BigInteger, nullable=False, server_default="0"),
        Column("cache_savings_credits", BigInteger, nullable=False, server_default="0"),
        Column("provider_cost_usd", Float, nullable=False, server_default="0"),
        Column("usage_estimated", Boolean, nullable=False, server_default="0"),
        Column("pricing_version", String(64), nullable=False),
        Column("metadata", JSON, nullable=False, server_default="{}"),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Index(
            "uq_credit_transactions_reservation_item",
            "reservation_id",
            "item_index",
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
            usage_periods,
            usage_counters,
            usage_reservations,
            credit_transactions,
        )
    }
    metadata.create_all(engine)
    import db.tables as db_tables

    monkeypatch.setattr(db_tables, "get_table", tables.__getitem__)
    monkeypatch.setenv("BILLING_ENABLED", "false")
    monkeypatch.delenv("DEV_SUBSCRIPTION_PLAN", raising=False)
    db = sessionmaker(bind=engine)()
    try:
        yield db, tables
    finally:
        db.close()
        engine.dispose()


def _effective(db, plan_code: str = "free") -> EffectiveSubscription:
    account = repository.get_or_create_billing_account_for_user(db, uuid4())
    now = datetime.now(UTC)
    starts_at = now - timedelta(days=1)
    ends_at = now + timedelta(days=30)
    period = repository.create_usage_period(
        db,
        billing_account_id=account["id"],
        plan_code=plan_code,
        starts_at=starts_at,
        ends_at=ends_at,
    )
    return EffectiveSubscription(
        billing_account_id=account["id"],
        usage_period_id=period["id"],
        plan=get_plan_catalog().require(plan_code),
        source="test",
        provider=None,
        provider_subscription_id=None,
        status="free",
        current_period_start=starts_at,
        current_period_end=ends_at,
        cancel_at_period_end=False,
        grace_until=None,
    )


def _user(db, tables):
    user_id = uuid4()
    db.execute(tables["users"].insert().values(id=user_id, email=f"{user_id}@example.com"))
    return user_id


def _counter(db, tables, period_id):
    return (
        db.execute(
            select(tables["usage_counters"]).where(
                tables["usage_counters"].c.usage_period_id == period_id,
                tables["usage_counters"].c.meter_key == "ai_credits",
            )
        )
        .mappings()
        .one()
    )


def test_credit_reservation_is_idempotent_and_conflict_safe(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    first = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="same",
        operation_type="ask",
        requested_quantities={"ai_credits": 20_000},
    )
    repeated = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="same",
        operation_type="ask",
        requested_quantities={"ai_credits": 20_000},
    )
    assert repeated.id == first.id
    assert _counter(db, tables, effective.usage_period_id)["reserved_quantity"] == 20_000
    with pytest.raises(UsageReservationConflictError):
        reserve_usage(
            db,
            effective_subscription=effective,
            request_id="same",
            operation_type="ask",
            requested_quantities={"ai_credits": 20_001},
        )


def test_settle_and_release_move_only_the_unified_counter(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    settled = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="settle",
        operation_type="ask",
        requested_quantities={"ai_credits": 10_000},
    )
    settle_usage(
        db,
        reservation_id=settled.id,
        successful_quantities={"ai_credits": 3_000},
    )
    counter = _counter(db, tables, effective.usage_period_id)
    assert (counter["used_quantity"], counter["reserved_quantity"]) == (3_000, 0)

    released = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="release",
        operation_type="ask",
        requested_quantities={"ai_credits": 5_000},
    )
    release_usage(db, reservation_id=released.id, reason="provider_failed")
    counter = _counter(db, tables, effective.usage_period_id)
    assert (counter["used_quantity"], counter["reserved_quantity"]) == (3_000, 0)


def test_stale_reservations_release_credit_capacity(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    reservation = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="stale",
        operation_type="ask",
        requested_quantities={"ai_credits": 4_000},
    )
    db.execute(
        tables["usage_reservations"]
        .update()
        .where(tables["usage_reservations"].c.id == reservation.id)
        .values(last_activity_at=datetime.now(UTC) - timedelta(hours=1))
    )
    outcome = expire_stale_reservations(
        db,
        older_than=datetime.now(UTC) - timedelta(minutes=30),
    )
    assert outcome.inspected == 1
    assert outcome.released == 1
    assert outcome.credits_released == 4_000
    assert _counter(db, tables, effective.usage_period_id)["reserved_quantity"] == 0


def test_active_reservation_heartbeat_prevents_stale_cleanup(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    reservation = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="active-heartbeat",
        operation_type="ask",
        requested_quantities={"ai_credits": 4_000},
    )
    db.execute(
        tables["usage_reservations"]
        .update()
        .where(tables["usage_reservations"].c.id == reservation.id)
        .values(last_activity_at=datetime.now(UTC) - timedelta(hours=1))
    )

    assert repository.touch_usage_reservation_activity(db, [reservation.id]) == 1
    outcome = expire_stale_reservations(
        db,
        older_than=datetime.now(UTC) - timedelta(minutes=30),
    )

    assert outcome.inspected == 0
    assert outcome.released == 0
    assert _counter(db, tables, effective.usage_period_id)["reserved_quantity"] == 4_000


def test_recent_and_terminal_reservations_are_not_expired(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    recent = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="recent",
        operation_type="ask",
        requested_quantities={"ai_credits": 1_000},
    )
    settled = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="already-settled",
        operation_type="ask",
        requested_quantities={"ai_credits": 2_000},
    )
    settle_usage(
        db,
        reservation_id=settled.id,
        successful_quantities={"ai_credits": 800},
    )
    released = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="already-released",
        operation_type="ask",
        requested_quantities={"ai_credits": 3_000},
    )
    release_usage(db, reservation_id=released.id, reason="provider_failed")
    db.execute(
        tables["usage_reservations"]
        .update()
        .where(tables["usage_reservations"].c.id.in_([settled.id, released.id]))
        .values(last_activity_at=datetime.now(UTC) - timedelta(hours=1))
    )

    outcome = expire_stale_reservations(
        db,
        older_than=datetime.now(UTC) - timedelta(minutes=30),
    )

    assert outcome.inspected == 0
    assert outcome.released == 0
    assert repository.get_usage_reservation_by_id(db, recent.id)["state"] == "reserved"
    assert repository.get_usage_reservation_by_id(db, settled.id)["state"] == "settled"
    assert repository.get_usage_reservation_by_id(db, released.id)["state"] == "released"


def test_supplemental_settlement_bills_full_actual_usage_when_capacity_exists(
    metering_db,
):
    db, tables = metering_db
    effective = _effective(db)
    reservation = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="supplement-success",
        operation_type="ask",
        requested_quantities={"ai_credits": 1_000},
    )

    outcome = settle_usage_with_supplement(
        db,
        reservation_id=reservation.id,
        actual_quantity=1_500,
        allowance_limit=effective.plan.allowances.ai_credits,
    )

    assert outcome.billed_quantity == 1_500
    assert outcome.supplemented_quantity == 500
    assert outcome.unbilled_quantity == 0
    assert _counter(db, tables, effective.usage_period_id)["used_quantity"] == 1_500
    assert _counter(db, tables, effective.usage_period_id)["reserved_quantity"] == 0
    persisted = repository.get_usage_reservation_by_id(db, reservation.id)
    assert persisted["requested_quantities"] == {"ai_credits": 1_500}
    assert persisted["settled_quantities"] == {"ai_credits": 1_500}


def test_supplemental_settlement_caps_billing_without_negative_balance(metering_db):
    db, tables = metering_db
    effective = _effective(db)
    reservation = reserve_usage(
        db,
        effective_subscription=effective,
        request_id="supplement-insufficient",
        operation_type="ask",
        requested_quantities={"ai_credits": 1_000},
    )
    counter = _counter(db, tables, effective.usage_period_id)
    db.execute(
        tables["usage_counters"]
        .update()
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=98_500)
    )

    outcome = settle_usage_with_supplement(
        db,
        reservation_id=reservation.id,
        actual_quantity=2_000,
        allowance_limit=effective.plan.allowances.ai_credits,
    )

    assert outcome.billed_quantity == 1_000
    assert outcome.supplemented_quantity == 0
    assert outcome.unbilled_quantity == 1_000
    final_counter = _counter(db, tables, effective.usage_period_id)
    assert final_counter["used_quantity"] == 99_500
    assert final_counter["reserved_quantity"] == 0
    repeated = settle_usage_with_supplement(
        db,
        reservation_id=reservation.id,
        actual_quantity=2_000,
        allowance_limit=effective.plan.allowances.ai_credits,
    )
    assert repeated.billed_quantity == 1_000
    assert _counter(db, tables, effective.usage_period_id)["used_quantity"] == 99_500


def test_actual_model_tokens_settle_and_create_reconciliation_item(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="ask-actual",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        input_text="Explain atomic reservations.",
        initial_query="How do atomic credit reservations work?",
        credit_activity_id="activity-model-research",
        max_output_tokens=500,
    )
    reserved = reservation.requested_quantities["ai_credits"]
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=100,
                output_tokens=200,
                provider_cost_usd=0.001,
            ),
        ),
        research_provider_credits_used=0,
        file_analysis_performed=True,
    )
    counter = _counter(
        db,
        tables,
        repository.get_usage_reservation_by_id(db, reservation.reservation_id)["usage_period_id"],
    )
    assert counter["used_quantity"] == 900
    assert counter["reserved_quantity"] == 0
    assert counter["used_quantity"] < reserved
    item = db.execute(select(tables["credit_transactions"])).mappings().one()
    assert item["input_credits"] == 100
    assert item["output_credits"] == 800
    assert item["usage_estimated"] is False
    expected_metadata = {
        "file_context": True,
        "credit_activity_id": "activity-model-research",
        "initial_query": "How do atomic credit reservations work?",
        "prompt_optimization": False,
    }
    assert {key: item["metadata"][key] for key in expected_metadata} == expected_metadata
    assert item["metadata"]["credit_policy_version"]
    assert item["metadata"]["cache_aware_shadow_total"] == item["total_credits"]


def test_optimizer_reservation_covers_every_configured_attempt(metering_db):
    db, _tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, _tables),
        request_id="optimizer-three-attempts",
        operation_type="optimize",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        optimization_enabled=True,
        input_text="Improve this prompt.",
        max_output_tokens=1800,
        model_attempt_count=3,
    )

    assert len(reservation.model_estimates) == 3
    assert len({item.total_credits for item in reservation.model_estimates}) == 1
    assert reservation.requested_quantities["ai_credits"] == sum(
        item.total_credits for item in reservation.model_estimates
    )


def test_under_reserved_provider_result_keeps_answer_billable_and_records_adjustment(
    metering_db,
):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="ask-under-reserved",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        input_text="Short request",
        initial_query="Why did this request exceed its reservation?",
        credit_activity_id="activity-under-reserved",
        max_output_tokens=10,
    )
    reserved = reservation.requested_quantities["ai_credits"]
    persisted = repository.get_usage_reservation_by_id(db, reservation.reservation_id)
    counter = _counter(db, tables, persisted["usage_period_id"])
    db.execute(
        tables["usage_counters"]
        .update()
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=100_000 - reserved)
    )

    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=100,
                output_tokens=200,
                provider_cost_usd=0.01,
            ),
        ),
        research_provider_credits_used=0,
    )

    final_counter = _counter(db, tables, persisted["usage_period_id"])
    assert final_counter["used_quantity"] == 100_000
    assert final_counter["reserved_quantity"] == 0
    items = (
        db.execute(
            select(tables["credit_transactions"]).order_by(
                tables["credit_transactions"].c.item_index
            )
        )
        .mappings()
        .all()
    )
    assert sum(item["total_credits"] for item in items) == reserved
    assert items[0]["metadata"]["under_reserved"] is True
    assert items[0]["metadata"]["initial_query"] == ("Why did this request exceed its reservation?")
    assert items[0]["metadata"]["credit_activity_id"] == "activity-under-reserved"
    assert items[0]["metadata"]["billed_total_credits"] == reserved
    assert items[1]["item_type"] == "adjustment"
    assert items[1]["total_credits"] == 0
    assert items[1]["metadata"]["unbilled_credits"] > 0
    assert items[1]["metadata"]["unbilled_provider_cost_usd"] > 0
    assert items[1]["metadata"]["initial_query"] == ("Why did this request exceed its reservation?")
    assert items[1]["metadata"]["credit_activity_id"] == "activity-under-reserved"


def test_compare_partial_success_charges_only_delivered_model(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="compare-partial",
        operation_type="compare",
        model_targets=(
            ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),
            ModelTargetIntent("gemini", "gemini-2.5-flash", "standard"),
        ),
        research_enabled=False,
        input_text="Compare two approaches.",
        max_output_tokens=300,
    )
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=50,
                output_tokens=100,
            ),
        ),
        research_provider_credits_used=0,
    )
    items = db.execute(select(tables["credit_transactions"])).mappings().all()
    assert [(item["provider"], item["model"]) for item in items] == [("openai", "gpt-4.1-mini")]
    assert sum(item["total_credits"] for item in items) == 450


def test_provider_reported_research_usage_is_charged_when_model_fails(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="research-only",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=True,
        input_text="Current facts",
        max_output_tokens=200,
    )
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(),
        research_provider_credits_used=3,
    )
    item = db.execute(select(tables["credit_transactions"])).mappings().one()
    assert item["item_type"] == "research"
    assert item["total_credits"] == calculate_research_credit_charge(3)
    assert item["metadata"] == {
        "provider_credits_used": 3,
        "cortex_credits_per_provider_credit": 5_000,
    }


def test_research_fallback_usage_is_marked_estimated_in_ledger(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="research-estimated",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=True,
        input_text="Current facts",
        max_output_tokens=200,
    )

    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(),
        research_provider_credits_used=2,
        research_usage_estimated=True,
    )

    item = db.execute(select(tables["credit_transactions"])).mappings().one()
    assert item["total_credits"] == 10_000
    assert item["usage_estimated"] is True


def test_missing_provider_usage_is_estimated_and_marked(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="estimated",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        input_text="A short prompt",
        max_output_tokens=200,
    )
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model="gpt-4.1-mini",
                input_tokens=0,
                output_tokens=0,
                output_text="A short answer.",
            ),
        ),
        research_provider_credits_used=0,
    )
    item = db.execute(select(tables["credit_transactions"])).mappings().one()
    assert item["usage_estimated"] is True
    assert item["total_credits"] > 0


def test_insufficient_credits_prevents_reservation(metering_db):
    db, tables = metering_db
    user_id = _user(db, tables)
    effective = resolve_effective_subscription(db, user_id)
    counter = repository.get_or_create_usage_counter(db, effective.usage_period_id, "ai_credits")
    db.execute(
        tables["usage_counters"]
        .update()
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=99_999)
    )
    with pytest.raises(EntitlementDeniedError) as exc_info:
        authorize_and_reserve_usage(
            db,
            user_id=user_id,
            request_id="too-expensive",
            operation_type="ask",
            model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
            research_enabled=False,
            input_text="Cannot fit",
            max_output_tokens=100,
        )
    assert exc_info.value.denial.code == "insufficient_credits"
    assert exc_info.value.denial.required > exc_info.value.denial.remaining
    assert exc_info.value.denial.remaining == 1
    assert db.execute(select(tables["usage_reservations"])).all() == []


def test_smart_routing_reserves_allowed_worst_case_and_settles_actual(metering_db):
    db, tables = metering_db
    reservation = authorize_and_reserve_usage(
        db,
        user_id=_user(db, tables),
        request_id="smart",
        operation_type="ask",
        model_targets=(ModelTargetIntent("openai", "gpt-4.1-mini", "standard"),),
        research_enabled=False,
        smart_routing=True,
        input_text="Route this",
        max_output_tokens=100,
    )
    assert reservation.allowed_billing_classes == frozenset({"economical", "standard"})
    reserved = reservation.requested_quantities["ai_credits"]
    finalize_reserved_usage(
        db,
        reservation=reservation,
        model_usages=(
            BillableModelUsage(
                provider="deepseek",
                model="deepseek-chat",
                input_tokens=20,
                output_tokens=40,
            ),
        ),
        research_provider_credits_used=0,
    )
    persisted = repository.get_usage_reservation_by_id(db, reservation.reservation_id)
    assert persisted["settled_quantities"]["ai_credits"] == 50
    assert reserved > 50


def test_smart_routing_skips_unaffordable_expensive_candidate(
    metering_db,
    monkeypatch,
):
    db, tables = metering_db
    effective = _effective(db)
    monkeypatch.setattr(
        "server.billing.enforcement_service.resolve_effective_subscription",
        lambda _db, _user_id: effective,
    )
    counter = repository.get_or_create_usage_counter(
        db,
        effective.usage_period_id,
        "ai_credits",
    )
    db.execute(
        tables["usage_counters"]
        .update()
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=99_700)
    )

    reservation = authorize_and_reserve_usage(
        db,
        user_id=uuid4(),
        request_id="smart-low-balance",
        operation_type="ask",
        model_targets=(
            ModelTargetIntent("openai", "gpt-5.4-mini", "standard"),
            ModelTargetIntent("openai", "gpt-4.1-nano", "economical"),
        ),
        research_enabled=False,
        smart_routing=True,
        input_text="Give me one short fact.",
        max_output_tokens=100,
    )

    assert reservation.smart_candidates
    assert reservation.smart_candidates[0].model == "gpt-4.1-nano"
    assert all(item.model != "gpt-5.4-mini" for item in reservation.smart_candidates)
    assert reservation.requested_quantities["ai_credits"] <= 300


def test_smart_routing_never_authorizes_model_outside_plan(
    metering_db,
    monkeypatch,
):
    db, _tables = metering_db
    effective = _effective(db, "free")
    monkeypatch.setattr(
        "server.billing.enforcement_service.resolve_effective_subscription",
        lambda _db, _user_id: effective,
    )

    reservation = authorize_and_reserve_usage(
        db,
        user_id=uuid4(),
        request_id="smart-plan-restriction",
        operation_type="ask",
        model_targets=(
            ModelTargetIntent("claude", "claude-opus-4-6", "premium"),
            ModelTargetIntent("openai", "gpt-4.1-nano", "economical"),
        ),
        research_enabled=False,
        smart_routing=True,
        input_text="Brief answer.",
        max_output_tokens=50,
    )

    assert {item.billing_class for item in reservation.smart_candidates} <= {
        "economical",
        "standard",
    }
    assert all(item.model != "claude-opus-4-6" for item in reservation.smart_candidates)


def test_smart_routing_keeps_advanced_candidate_for_plus_with_sufficient_balance(
    metering_db,
    monkeypatch,
):
    db, _tables = metering_db
    effective = _effective(db, "plus")
    monkeypatch.setattr(
        "server.billing.enforcement_service.resolve_effective_subscription",
        lambda _db, _user_id: effective,
    )

    reservation = authorize_and_reserve_usage(
        db,
        user_id=uuid4(),
        request_id="smart-plus-advanced",
        operation_type="ask",
        model_targets=(
            ModelTargetIntent("openai", "gpt-5.1", "advanced"),
            ModelTargetIntent("openai", "gpt-4.1-nano", "economical"),
        ),
        research_enabled=False,
        smart_routing=True,
        input_text="Analyze the failure modes of this distributed transaction.",
        max_output_tokens=500,
    )

    assert reservation.current_plan == "plus"
    assert reservation.smart_candidates[0].model == "gpt-5.1"
    assert reservation.smart_candidates[0].billing_class == "advanced"
    assert reservation.requested_quantities["ai_credits"] == (
        reservation.smart_candidates[0].reservation_credits
    )


def test_smart_routing_keeps_premium_candidate_only_for_pro(
    metering_db,
    monkeypatch,
):
    db, _tables = metering_db
    effective = _effective(db, "pro")
    monkeypatch.setattr(
        "server.billing.enforcement_service.resolve_effective_subscription",
        lambda _db, _user_id: effective,
    )

    reservation = authorize_and_reserve_usage(
        db,
        user_id=uuid4(),
        request_id="smart-pro-premium",
        operation_type="ask",
        model_targets=(
            ModelTargetIntent("claude", "claude-opus-4-6", "premium"),
            ModelTargetIntent("openai", "gpt-5.4", "advanced"),
        ),
        research_enabled=False,
        smart_routing=True,
        input_text="Perform a rigorous architecture and security review.",
        max_output_tokens=1_000,
    )

    assert reservation.current_plan == "pro"
    assert reservation.smart_candidates[0].model == "claude-opus-4-6"
    assert reservation.smart_candidates[0].billing_class == "premium"
    assert "premium" in reservation.allowed_billing_classes


def test_smart_routing_rejects_only_when_no_appropriate_candidate_is_affordable(
    metering_db,
    monkeypatch,
):
    db, tables = metering_db
    effective = _effective(db)
    monkeypatch.setattr(
        "server.billing.enforcement_service.resolve_effective_subscription",
        lambda _db, _user_id: effective,
    )
    counter = repository.get_or_create_usage_counter(
        db,
        effective.usage_period_id,
        "ai_credits",
    )
    db.execute(
        tables["usage_counters"]
        .update()
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=99_999)
    )

    with pytest.raises(EntitlementDeniedError) as exc_info:
        authorize_and_reserve_usage(
            db,
            user_id=uuid4(),
            request_id="smart-no-affordable",
            operation_type="ask",
            model_targets=(
                ModelTargetIntent("openai", "gpt-5.4-mini", "standard"),
                ModelTargetIntent("openai", "gpt-4.1-nano", "economical"),
            ),
            research_enabled=False,
            smart_routing=True,
            input_text="Brief answer.",
            max_output_tokens=50,
        )

    assert exc_info.value.denial.code == "insufficient_credits"
    assert exc_info.value.denial.remaining == 1
    assert db.execute(select(tables["usage_reservations"])).all() == []
