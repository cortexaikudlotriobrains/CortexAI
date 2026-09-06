"""Resolve authenticated users to conservative effective subscriptions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import os
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from db.billing_repository import (
    close_usage_period,
    get_active_usage_period,
    get_effective_subscription_grant,
    get_latest_subscription_for_account,
    get_live_subscription_for_account,
    lock_billing_account,
    synchronize_usage_period,
)
from server.billing.account_service import get_or_create_user_billing_account
from server.billing.errors import BillingConfigurationError
from server.billing.grant_service import monthly_grant_bounds
from server.billing.models import SubscriptionPlan
from server.billing.plan_catalog import PlanCatalog, get_plan_catalog
from utils.logger import get_logger

logger = get_logger(__name__)

_PAID_STATUSES = frozenset({"active"})
_FREE_STATUSES = frozenset({"unpaid", "incomplete", "incomplete_expired", "paused"})
_DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "dev", "development"})
_UNRESTRICTED_PLAN_NAME = "unrestricted"
_UNRESTRICTED_COUNT_ALLOWANCE = 1_000_000_000


@dataclass(frozen=True)
class EffectiveSubscription:
    billing_account_id: UUID
    usage_period_id: UUID
    plan: SubscriptionPlan
    source: str
    provider: str | None
    provider_subscription_id: str | None
    status: str
    current_period_start: datetime
    current_period_end: datetime
    cancel_at_period_end: bool
    grace_until: datetime | None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_month_bounds(at_time: datetime) -> tuple[datetime, datetime]:
    starts_at = datetime(at_time.year, at_time.month, 1, tzinfo=UTC)
    if at_time.month == 12:
        ends_at = datetime(at_time.year + 1, 1, 1, tzinfo=UTC)
    else:
        ends_at = datetime(at_time.year, at_time.month + 1, 1, tzinfo=UTC)
    return starts_at, ends_at


def subscription_payment_grace_days() -> int:
    raw = str(os.getenv("SUBSCRIPTION_PAYMENT_GRACE_DAYS", "3") or "3").strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid SUBSCRIPTION_PAYMENT_GRACE_DAYS; using 3")
        return 3
    if value < 0:
        logger.warning("Negative SUBSCRIPTION_PAYMENT_GRACE_DAYS; using 3")
        return 3
    return value


def _development_unrestricted_plan(catalog: PlanCatalog) -> SubscriptionPlan:
    pro = catalog.require("pro")
    return replace(
        pro,
        display_name="Local Unrestricted",
        stripe_price_env=None,
        allowances=replace(
            pro.allowances,
            ai_credits=_UNRESTRICTED_COUNT_ALLOWANCE,
        ),
        limits=replace(
            pro.limits,
            requests_per_minute=_UNRESTRICTED_COUNT_ALLOWANCE,
        ),
    )


def _development_override(catalog: PlanCatalog) -> SubscriptionPlan | None:
    raw_plan = str(os.getenv("DEV_SUBSCRIPTION_PLAN", "") or "").strip().lower()
    if not raw_plan or _env_bool("BILLING_ENABLED", default=False):
        return None

    runtime_env = next(
        (
            str(os.getenv(name, "") or "").strip().lower()
            for name in ("APP_ENV", "ENVIRONMENT", "ENV")
            if str(os.getenv(name, "") or "").strip()
        ),
        "",
    )
    if runtime_env not in _DEVELOPMENT_ENVIRONMENTS:
        logger.warning("Ignoring DEV_SUBSCRIPTION_PLAN outside a local development environment")
        return None

    plan: SubscriptionPlan | None
    if raw_plan == _UNRESTRICTED_PLAN_NAME:
        if not _env_bool("DEV_SUBSCRIPTION_BYPASS_ENABLED", default=False):
            logger.warning(
                "Ignoring unrestricted development plan without explicit local bypass enablement"
            )
            return None
        plan = _development_unrestricted_plan(catalog)
    else:
        plan = catalog.get(raw_plan)
    if plan is None:
        logger.warning("Ignoring unknown DEV_SUBSCRIPTION_PLAN '%s'", raw_plan)
        return None

    logger.warning(
        "Using local development subscription override",
        extra={
            "extra_fields": {
                "plan_code": plan.code,
                "development_profile": raw_plan,
            }
        },
    )
    return plan


def _snapshot_text(snapshot: dict[str, Any], key: str) -> str | None:
    value = str(snapshot.get(key) or "").strip()
    return value or None


def _ensure_usage_period(
    db: Session,
    *,
    billing_account_id: UUID,
    subscription_id: UUID | None,
    plan_code: str,
    starts_at: datetime,
    ends_at: datetime,
    now: datetime,
    subscription_grant_id: UUID | None = None,
) -> dict[str, Any]:
    active = get_active_usage_period(db, billing_account_id, now)
    if active is not None:
        active_start = _as_utc(active.get("starts_at"))
        active_end = _as_utc(active.get("ends_at"))
        if (
            str(active.get("plan_code") or "").lower() == plan_code
            and active.get("subscription_id") == subscription_id
            and active.get("subscription_grant_id") == subscription_grant_id
            and active_start == starts_at
            and active_end == ends_at
        ):
            return active
        close_usage_period(db, active["id"])

    try:
        return synchronize_usage_period(
            db,
            billing_account_id=billing_account_id,
            subscription_id=subscription_id,
            subscription_grant_id=subscription_grant_id,
            plan_code=plan_code,
            starts_at=starts_at,
            ends_at=ends_at,
        )
    except Exception as exc:
        raise BillingConfigurationError("Current usage period could not be resolved") from exc


def _grant_or_free_effective(
    db: Session,
    *,
    billing_account_id: UUID,
    catalog: PlanCatalog,
    now: datetime,
    source: str = "free_default",
    provider: str | None = None,
    provider_subscription_id: str | None = None,
    status: str = "free",
    cancel_at_period_end: bool = False,
    grace_until: datetime | None = None,
) -> EffectiveSubscription:
    # Every conservative Stripe fallback first considers trusted Cortex access.
    try:
        grant = get_effective_subscription_grant(db, billing_account_id, now)
    except Exception as exc:
        raise BillingConfigurationError("Subscription grant could not be resolved") from exc
    if grant is not None:
        grant_plan = catalog.get(str(grant.get("plan_code") or ""))
        if grant_plan is not None and grant_plan.code in {"plus", "pro"}:
            grant_start = _as_utc(grant.get("starts_at"))
            grant_expiry = _as_utc(grant.get("expires_at"))
            if grant_start is not None and grant_expiry is not None:
                starts_at, ends_at = monthly_grant_bounds(grant_start, grant_expiry, now)
                period = _ensure_usage_period(
                    db,
                    billing_account_id=billing_account_id,
                    subscription_id=None,
                    subscription_grant_id=grant["id"],
                    plan_code=grant_plan.code,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    now=now,
                )
                return EffectiveSubscription(
                    billing_account_id=billing_account_id,
                    usage_period_id=period["id"],
                    plan=grant_plan,
                    source="cortex_grant",
                    provider=None,
                    provider_subscription_id=None,
                    status="active",
                    current_period_start=starts_at,
                    current_period_end=ends_at,
                    cancel_at_period_end=False,
                    grace_until=None,
                )
        logger.error("Ignoring subscription grant with invalid plan configuration")
    plan = catalog.require("free")
    starts_at, ends_at = _utc_month_bounds(now)
    period = _ensure_usage_period(
        db,
        billing_account_id=billing_account_id,
        subscription_id=None,
        plan_code=plan.code,
        starts_at=starts_at,
        ends_at=ends_at,
        now=now,
    )
    return EffectiveSubscription(
        billing_account_id=billing_account_id,
        usage_period_id=period["id"],
        plan=plan,
        source=source,
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        status=status,
        current_period_start=starts_at,
        current_period_end=ends_at,
        cancel_at_period_end=cancel_at_period_end,
        grace_until=grace_until,
    )


def _paid_effective(
    db: Session,
    *,
    billing_account_id: UUID,
    snapshot: dict[str, Any],
    plan: SubscriptionPlan,
    now: datetime,
    grace_until: datetime | None,
) -> EffectiveSubscription:
    starts_at = _as_utc(snapshot.get("current_period_start"))
    ends_at = _as_utc(snapshot.get("current_period_end"))
    if starts_at is None or ends_at is None or not (starts_at <= now < ends_at):
        raise BillingConfigurationError("Paid subscription period is missing or stale")

    period = _ensure_usage_period(
        db,
        billing_account_id=billing_account_id,
        subscription_id=snapshot["id"],
        plan_code=plan.code,
        starts_at=starts_at,
        ends_at=ends_at,
        now=now,
    )
    return EffectiveSubscription(
        billing_account_id=billing_account_id,
        usage_period_id=period["id"],
        plan=plan,
        source=_snapshot_text(snapshot, "provider") or "provider_snapshot",
        provider=_snapshot_text(snapshot, "provider"),
        provider_subscription_id=_snapshot_text(snapshot, "provider_subscription_id"),
        status=_snapshot_text(snapshot, "status") or "active",
        current_period_start=starts_at,
        current_period_end=ends_at,
        cancel_at_period_end=bool(snapshot.get("cancel_at_period_end")),
        grace_until=grace_until,
    )


def resolve_effective_subscription(
    db: Session,
    user_id: UUID,
    *,
    now: datetime | None = None,
    catalog: PlanCatalog | None = None,
) -> EffectiveSubscription:
    """Resolve one user to a plan and ensure its current usage period exists."""
    resolved_now = _as_utc(now or datetime.now(UTC))
    if resolved_now is None:  # pragma: no cover - defensive for type narrowing
        raise BillingConfigurationError("Current time could not be resolved")
    plan_catalog = catalog or get_plan_catalog()
    account = get_or_create_user_billing_account(db, user_id)
    # Serialize resolution/period transitions with operator grant changes.
    lock_billing_account(db, account.id)

    override = _development_override(plan_catalog)
    if override is not None:
        starts_at, ends_at = _utc_month_bounds(resolved_now)
        # Keep the local synthetic paid period deterministic but distinct from
        # the real Free calendar-month key, which may already exist from a
        # previous local run.
        starts_at += timedelta(microseconds=1)
        period = _ensure_usage_period(
            db,
            billing_account_id=account.id,
            subscription_id=None,
            plan_code=override.code,
            starts_at=starts_at,
            ends_at=ends_at,
            now=resolved_now,
        )
        return EffectiveSubscription(
            billing_account_id=account.id,
            usage_period_id=period["id"],
            plan=override,
            source="development_override",
            provider=None,
            provider_subscription_id=None,
            status="active",
            current_period_start=starts_at,
            current_period_end=ends_at,
            cancel_at_period_end=False,
            grace_until=None,
        )

    if not _env_bool("BILLING_ENABLED", default=False):
        return _grant_or_free_effective(
            db,
            billing_account_id=account.id,
            catalog=plan_catalog,
            now=resolved_now,
        )

    try:
        snapshot = get_live_subscription_for_account(db, account.id)
        if snapshot is None:
            snapshot = get_latest_subscription_for_account(db, account.id)
    except Exception as exc:
        raise BillingConfigurationError("Subscription snapshot could not be resolved") from exc
    if snapshot is None:
        return _grant_or_free_effective(
            db,
            billing_account_id=account.id,
            catalog=plan_catalog,
            now=resolved_now,
        )

    status_value = (_snapshot_text(snapshot, "status") or "unknown").lower()
    provider = _snapshot_text(snapshot, "provider")
    provider_subscription_id = _snapshot_text(snapshot, "provider_subscription_id")
    cancel_at_period_end = bool(snapshot.get("cancel_at_period_end"))
    plan = plan_catalog.get(str(snapshot.get("plan_code") or ""))
    if plan is None:
        logger.error(
            "Subscription references an unknown plan; falling back to Free",
            extra={
                "extra_fields": {
                    "billing_account_id": str(account.id),
                    "plan_code": str(snapshot.get("plan_code") or ""),
                }
            },
        )
        return _grant_or_free_effective(
            db,
            billing_account_id=account.id,
            catalog=plan_catalog,
            now=resolved_now,
            source="configuration_fallback",
            status="configuration_error",
        )

    if plan.code == "free":
        return _grant_or_free_effective(
            db,
            billing_account_id=account.id,
            catalog=plan_catalog,
            now=resolved_now,
            source=provider or "free_default",
            provider=provider,
            provider_subscription_id=provider_subscription_id,
            status="free",
        )

    grace_until = _as_utc(snapshot.get("grace_until"))
    if status_value == "past_due" and grace_until is None:
        failure_observed_at = _as_utc(snapshot.get("last_provider_event_at"))
        if failure_observed_at is not None:
            grace_until = failure_observed_at + timedelta(days=subscription_payment_grace_days())

    grants_paid_access = status_value in _PAID_STATUSES
    if status_value == "past_due":
        grants_paid_access = grace_until is not None and resolved_now < grace_until
    elif status_value == "canceled":
        period_end = _as_utc(snapshot.get("current_period_end"))
        grants_paid_access = bool(
            cancel_at_period_end and period_end is not None and resolved_now < period_end
        )
    elif status_value in _FREE_STATUSES:
        grants_paid_access = False
    elif status_value not in _PAID_STATUSES:
        logger.warning(
            "Unknown subscription status '%s'; falling back to Free",
            status_value,
            extra={"extra_fields": {"billing_account_id": str(account.id)}},
        )
        grants_paid_access = False

    if grants_paid_access:
        try:
            return _paid_effective(
                db,
                billing_account_id=account.id,
                snapshot=snapshot,
                plan=plan,
                now=resolved_now,
                grace_until=grace_until,
            )
        except BillingConfigurationError:
            logger.exception(
                "Paid subscription period is invalid; falling back to Free",
                extra={"extra_fields": {"billing_account_id": str(account.id)}},
            )

    return _grant_or_free_effective(
        db,
        billing_account_id=account.id,
        catalog=plan_catalog,
        now=resolved_now,
        source=provider or "lifecycle_fallback",
        provider=provider,
        provider_subscription_id=provider_subscription_id,
        status=status_value,
        cancel_at_period_end=cancel_at_period_end,
        grace_until=grace_until,
    )
