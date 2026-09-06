from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from db import billing_repository as repository
from server.billing.entitlement_service import load_allowance_usage
from server.billing.grant_service import (
    inspect_subscription_grant,
    issue_subscription_grant,
    monthly_grant_bounds,
    resolve_grant_user,
    revoke_subscription_grant,
)
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import resolve_effective_subscription
from server.dependencies import AuthResult, get_auth
from server.routes import billing as billing_route, entitlements as entitlements_route
from tests.test_billing_entitlements import (
    billing_service_db as _billing_service_db,
    _create_user,
    _snapshot,
)

billing_service_db = _billing_service_db

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def issue(db, user_id, plan="plus", **kwargs):
    return issue_subscription_grant(
        db,
        user_id,
        plan_code=plan,
        expires_at=kwargs.pop("expires_at", NOW + timedelta(days=90)),
        granted_by="operator@example.com",
        reason="early_beta",
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


@pytest.mark.parametrize("plan_code", ["free", "plus", "pro"])
def test_disabled_stripe_uses_catalogue_without_secrets(billing_service_db, monkeypatch, plan_code):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    for key in (
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PLUS_MONTHLY_PRICE_ID",
        "STRIPE_PRO_MONTHLY_PRICE_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    if plan_code != "free":
        issue(db, user_id, plan_code)
    effective = resolve_effective_subscription(db, user_id, now=NOW)
    assert effective.plan == get_plan_catalog().require(plan_code)
    assert effective.source == ("free_default" if plan_code == "free" else "cortex_grant")
    assert effective.provider is None
    assert effective.provider_subscription_id is None
    assert not effective.cancel_at_period_end
    assert (
        load_allowance_usage(db, effective)["ai_credits"].remaining
        == effective.plan.allowances.ai_credits
    )
    assert db.execute(select(tables["subscriptions"])).all() == []


@pytest.mark.parametrize("state", ["future", "expired", "revoked", "unknown", "free"])
def test_invalid_grants_never_give_paid_access(billing_service_db, monkeypatch, state):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    grant = issue(db, user_id, "pro")
    if state == "future":
        db.execute(update(tables["subscription_grants"]).values(starts_at=NOW + timedelta(days=1)))
    elif state == "expired":
        db.execute(
            update(tables["subscription_grants"]).values(expires_at=NOW + timedelta(seconds=1))
        )
    elif state == "revoked":
        revoke_subscription_grant(db, user_id, revoked_by="operator", reason="ended", now=NOW)
    else:
        # Corrupt/legacy candidate returned by storage must also fail conservatively.
        monkeypatch.setattr(
            "server.billing.subscription_service.get_effective_subscription_grant",
            lambda *_: {**grant, "plan_code": "unknown" if state == "unknown" else "free"},
        )
    effective = resolve_effective_subscription(db, user_id, now=NOW + timedelta(seconds=1))
    assert effective.plan.code == "free"


@pytest.mark.parametrize("old,new", [("plus", "pro"), ("pro", "plus")])
def test_changes_preserve_history_even_at_identical_start(billing_service_db, old, new):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    first_grant = issue(db, user_id, old)
    first = resolve_effective_subscription(db, user_id, now=NOW)
    counter = repository.get_or_create_usage_counter(db, first.usage_period_id, "ai_credits")
    db.execute(
        update(tables["usage_counters"])
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=1234, reserved_quantity=99)
    )
    new_grant = issue(db, user_id, new, change=True)
    effective = resolve_effective_subscription(db, user_id, now=NOW)
    assert effective.plan.code == new
    assert effective.usage_period_id != first.usage_period_id
    periods = {row["id"]: row for row in db.execute(select(tables["usage_periods"])).mappings()}
    assert periods[first.usage_period_id]["status"] == "closed"
    assert periods[first.usage_period_id]["plan_code"] == old
    assert periods[first.usage_period_id]["subscription_grant_id"] == first_grant["id"]
    assert periods[effective.usage_period_id]["subscription_grant_id"] == new_grant["id"]
    old_counter = db.execute(select(tables["usage_counters"])).mappings().one()
    assert (old_counter["used_quantity"], old_counter["reserved_quantity"]) == (1234, 99)
    revoked = (
        db.execute(
            select(tables["subscription_grants"]).where(
                tables["subscription_grants"].c.id == first_grant["id"]
            )
        )
        .mappings()
        .one()
    )
    assert revoked["status"] == "revoked"
    assert revoked["reason"] == "early_beta"
    assert revoked["revoked_by"] == "operator@example.com"
    assert load_allowance_usage(db, effective)["ai_credits"].used == 0


def test_free_boundary_is_separate_and_revoke_restores_existing_free_usage(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    at_time = datetime(2026, 9, 1, tzinfo=UTC)
    free = resolve_effective_subscription(db, user_id, now=at_time)
    counter = repository.get_or_create_usage_counter(db, free.usage_period_id, "ai_credits")
    db.execute(
        update(tables["usage_counters"])
        .where(tables["usage_counters"].c.id == counter["id"])
        .values(used_quantity=123)
    )
    issue(db, user_id, now=at_time)
    paid = resolve_effective_subscription(db, user_id, now=at_time)
    assert free.usage_period_id != paid.usage_period_id
    revoke_subscription_grant(db, user_id, revoked_by="operator", reason="beta_ended", now=at_time)
    restored = resolve_effective_subscription(db, user_id, now=at_time)
    assert restored.plan.code == "free"
    assert restored.usage_period_id == free.usage_period_id
    assert load_allowance_usage(db, restored)["ai_credits"].used == 123


def test_one_open_grant_service_and_database_and_rollback(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    db.commit()
    first = issue(db, user_id)
    with pytest.raises(ValueError, match="open grant"):
        issue(db, user_id, "pro")
    with db.begin_nested(), pytest.raises(IntegrityError):
        db.execute(insert(tables["subscription_grants"]).values(**{**first, "id": uuid4()}))
    db.rollback()
    assert db.execute(select(tables["subscription_grants"])).all() == []
    assert db.execute(select(tables["usage_periods"])).all() == []


def test_expired_open_grant_is_retired_on_issue(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    first = issue(db, user_id, expires_at=NOW + timedelta(days=1))
    second = issue(db, user_id, "pro", now=NOW + timedelta(days=1))
    assert second["id"] != first["id"]
    assert (
        repository.get_current_subscription_grant(db, first["billing_account_id"])["id"]
        == second["id"]
    )
    assert (
        db.execute(
            select(tables["subscription_grants"].c.status).where(
                tables["subscription_grants"].c.id == first["id"]
            )
        ).scalar_one()
        == "expired"
    )


def test_monthly_periods_reset_and_final_period_is_clipped(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    expiry = datetime(2026, 12, 3, 12, tzinfo=UTC)
    grant = issue(db, user_id, "pro", expires_at=expiry)
    ids = []
    for month in [9, 10, 11]:
        start = datetime(2026, month, 5, 12, tzinfo=UTC)
        effective = resolve_effective_subscription(db, user_id, now=start)
        repeated = resolve_effective_subscription(db, user_id, now=start + timedelta(hours=1))
        assert repeated.usage_period_id == effective.usage_period_id
        assert effective.current_period_start == start
        assert effective.current_period_end == min(
            datetime(2026, month + 1, 5, 12, tzinfo=UTC), expiry
        )
        assert (
            load_allowance_usage(db, effective)["ai_credits"].remaining
            == get_plan_catalog().require("pro").allowances.ai_credits
        )
        db.execute(
            update(tables["usage_counters"])
            .where(tables["usage_counters"].c.usage_period_id == effective.usage_period_id)
            .values(used_quantity=321)
        )
        ids.append(effective.usage_period_id)
    assert len(set(ids)) == 3
    assert all(
        row.subscription_grant_id == grant["id"] and row.subscription_id is None
        for row in db.execute(select(tables["usage_periods"]))
    )
    assert resolve_effective_subscription(db, user_id, now=expiry).plan.code == "free"


@pytest.mark.parametrize("year,feb_day", [(2026, 28), (2028, 29)])
def test_month_end_uses_original_anchor(year, feb_day):
    anchor = datetime(year, 1, 31, 10, 45, tzinfo=UTC)
    expiry = datetime(year, 5, 1, tzinfo=UTC)
    feb = datetime(year, 2, feb_day, 10, 45, tzinfo=UTC)
    march = datetime(year, 3, 31, 10, 45, tzinfo=UTC)
    assert monthly_grant_bounds(anchor, expiry, feb - timedelta(microseconds=1)) == (anchor, feb)
    assert monthly_grant_bounds(anchor, expiry, feb) == (feb, march)
    assert monthly_grant_bounds(anchor, expiry, march)[0] == march


@pytest.mark.parametrize(
    "stripe_status,expected",
    [
        ("active", "plus"),
        ("trialing", "pro"),
        ("unpaid", "pro"),
        ("incomplete", "pro"),
        ("paused", "pro"),
    ],
)
def test_stripe_precedence_and_grant_fallback(
    billing_service_db, monkeypatch, stripe_status, expected
):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    grant = issue(db, user_id, "pro")
    monkeypatch.setenv("BILLING_ENABLED", "true")
    _snapshot(
        db,
        account_id=grant["billing_account_id"],
        status=stripe_status,
        starts_at=NOW,
        ends_at=NOW + timedelta(days=30),
    )
    effective = resolve_effective_subscription(db, user_id, now=NOW)
    assert effective.plan.code == expected
    assert effective.source == ("stripe" if stripe_status == "active" else "cortex_grant")


def test_guarded_override_still_precedes_grant(billing_service_db, monkeypatch):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    issue(db, user_id, "pro")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("DEV_SUBSCRIPTION_PLAN", "plus")
    assert resolve_effective_subscription(db, user_id, now=NOW).source == "development_override"
    monkeypatch.setenv("APP_ENV", "production")
    assert resolve_effective_subscription(db, user_id, now=NOW).plan.code == "pro"


@pytest.mark.parametrize("bad_plan", ["free", "unknown", "unrestricted", ""])
def test_service_rejects_invalid_plans_without_writes(billing_service_db, bad_plan):
    db, tables = billing_service_db
    with pytest.raises(ValueError, match="Plus or Pro"):
        issue(db, _create_user(db, tables), bad_plan)
    assert db.execute(select(tables["subscription_grants"])).all() == []


def test_identity_and_inspection_do_not_provision_users(billing_service_db):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    assert resolve_grant_user(db, email=f"{user_id}@EXAMPLE.COM") == user_id
    assert resolve_grant_user(db, user_id=user_id) == user_id
    with pytest.raises(ValueError, match="exactly one existing user"):
        resolve_grant_user(db, email="missing@example.com")
    db.execute(insert(tables["users"]).values(id=uuid4(), email=f"{user_id}@EXAMPLE.COM"))
    with pytest.raises(ValueError, match="ambiguous emails"):
        resolve_grant_user(db, email=f"{user_id}@example.com")
    assert inspect_subscription_grant(db, user_id)["billing_account_id"] is None
    assert db.execute(select(tables["billing_accounts"])).all() == []


@pytest.mark.parametrize("plan_code", ["plus", "pro"])
def test_grant_api_source_and_no_payment_management(billing_service_db, monkeypatch, plan_code):
    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    now = datetime.now(UTC)
    issue(db, user_id, plan_code, now=now, expires_at=now + timedelta(days=90))
    db.commit()

    @contextmanager
    def uow():
        yield db
        db.commit()

    for route in [billing_route, entitlements_route]:
        monkeypatch.setattr(route, "API_DB_ENABLED", True)
        monkeypatch.setattr(route, "_db_uow", uow)
    app = FastAPI()
    app.include_router(billing_route.router)
    app.include_router(entitlements_route.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None, cognito_claims=None, user_id=user_id
    )
    with TestClient(app) as client:
        subscription = client.get("/v1/billing/subscription")
        entitlements = client.get("/v1/entitlements")
        assert client.get("/v1/billing/plans").json()["billing_enabled"] is False
        assert (
            client.post(
                "/v1/billing/checkout-session",
                json={"plan_code": "plus", "billing_period": "monthly"},
            ).status_code
            == 503
        )
        assert client.post("/v1/billing/portal-session", json={}).status_code == 503
        assert client.post("/v1/billing/webhook", content=b"{}").status_code == 503
    assert subscription.status_code == entitlements.status_code == 200
    assert subscription.json()["can_manage"] is False
    assert subscription.json()["provider"] is None
    assert subscription.json()["plan_code"] == plan_code
    assert entitlements.json()["plan"]["source"] == "cortex_grant"


@pytest.mark.parametrize("plan_code", ["free", "plus", "pro"])
def test_real_grant_resolution_flows_through_work_gates(billing_service_db, monkeypatch, plan_code):
    from server.work import service
    from tests.test_work_service_policy import _config

    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    if plan_code != "free":
        issue(db, user_id, plan_code)
    monkeypatch.setattr(
        service,
        "resolve_effective_subscription",
        lambda db, uid: resolve_effective_subscription(db, uid, now=NOW),
    )
    monkeypatch.setattr(service.repository, "count_active_work_runs", lambda *_: 0)
    if plan_code == "free":
        with pytest.raises(HTTPException) as exc:
            service._plan_and_budget(db, user_id, None, _config())
        assert exc.value.detail["code"] == "work_not_in_plan"
        return
    effective, budget = service._plan_and_budget(db, user_id, None, _config())
    plan = get_plan_catalog().require(plan_code)
    assert effective.plan == plan
    assert budget == min(_config().default_credit_budget, plan.limits.max_work_credit_budget)
    monkeypatch.setattr(
        service.repository, "count_active_work_runs", lambda *_: plan.limits.max_active_work_runs
    )
    with pytest.raises(HTTPException) as exc:
        service._plan_and_budget(db, user_id, None, _config())
    assert exc.value.detail["code"] == "active_work_run_limit"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"granted_by": ""},
        {"reason": " "},
        {"expires_at": NOW},
        {"expires_at": NOW.replace(tzinfo=None)},
    ],
)
def test_service_requires_audit_and_valid_expiry(billing_service_db, kwargs):
    db, tables = billing_service_db
    args = dict(
        plan_code="pro",
        expires_at=NOW + timedelta(days=90),
        granted_by="operator",
        reason="beta",
        now=NOW,
    )
    args.update(kwargs)
    with pytest.raises(ValueError):
        issue_subscription_grant(db, _create_user(db, tables), **args)


def test_cli_requires_identity_expiry_reason_and_actor():
    from scripts.manage_subscription_grant import build_parser

    parser = build_parser()
    valid = [
        "grant",
        "--email",
        "user@example.com",
        "--plan",
        "pro",
        "--days",
        "90",
        "--actor",
        "operator",
        "--reason",
        "beta",
    ]
    assert parser.parse_args(valid).days == 90
    for option in ["--email", "--days", "--actor", "--reason"]:
        index = valid.index(option)
        with pytest.raises(SystemExit):
            parser.parse_args(valid[:index] + valid[index + 2 :])


def test_cli_commits_changes_and_rolls_back_duplicate_issue(
    billing_service_db, monkeypatch, capsys
):
    from scripts import manage_subscription_grant as cli

    db, tables = billing_service_db
    user_id = _create_user(db, tables)
    db.commit()
    monkeypatch.setattr(cli, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "validate_billing_schema", lambda: None)
    monkeypatch.setattr(cli, "SessionLocal", lambda: db)
    target = ["--user-id", str(user_id)]
    audit = ["--actor", "operator", "--reason", "beta"]
    assert cli.main(["grant", *target, "--plan", "plus", "--days", "90", *audit]) == 0
    with pytest.raises(SystemExit):
        cli.main(["grant", *target, "--plan", "pro", "--days", "90", *audit])
    assert cli.main(["change", *target, "--plan", "pro", "--days", "90", *audit]) == 0
    assert cli.main(["inspect", *target]) == 0
    assert '"plan_code": "pro"' in capsys.readouterr().out
    assert cli.main(["revoke", *target, *audit]) == 0
    assert resolve_effective_subscription(db, user_id).plan.code == "free"
    assert len(db.execute(select(tables["subscription_grants"])).all()) == 2
