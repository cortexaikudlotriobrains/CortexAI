"""Operator-owned Cortex access grants. Callers own the transaction boundary."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db import billing_repository as repository
from server.billing.account_service import get_or_create_user_billing_account
from server.billing.plan_catalog import PlanCatalog, get_plan_catalog


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Grant times must include a timezone")
    return value.astimezone(UTC)


def _required(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def resolve_grant_user(
    db: Session, *, email: str | None = None, user_id: UUID | None = None
) -> UUID:
    """Resolve an existing identity exactly; never provision or guess a user."""
    from db.tables import get_table

    if (email is None) == (user_id is None):
        raise ValueError("Specify exactly one of email or user ID")
    users = get_table("users")
    predicate = (
        func.lower(users.c.email) == _required(email, "Email").lower()
        if email is not None
        else users.c.id == user_id
    )
    matches = db.execute(select(users.c.id).where(predicate).limit(2)).scalars().all()
    if len(matches) != 1:
        raise ValueError(
            "Target must match exactly one existing user; use user ID for ambiguous emails"
        )
    return matches[0]


def monthly_grant_bounds(
    starts_at: datetime, expires_at: datetime, now: datetime
) -> tuple[datetime, datetime]:
    """Clamp each anniversary from the original anchor (Jan 31 -> Feb 28 -> Mar 31)."""
    anchor, expiry, at_time = _utc(starts_at), _utc(expires_at), _utc(now)
    if not anchor <= at_time < expiry:
        raise ValueError("Grant is not effective at the requested time")

    def anniversary(offset: int) -> datetime:
        year, month_index = divmod(anchor.year * 12 + anchor.month - 1 + offset, 12)
        month = month_index + 1
        return anchor.replace(
            year=year, month=month, day=min(anchor.day, monthrange(year, month)[1])
        )

    offset = (at_time.year - anchor.year) * 12 + at_time.month - anchor.month
    start = anniversary(offset)
    if start > at_time:
        offset -= 1
        start = anniversary(offset)
    # Avoid overflowing year 9999 for a valid final partial month.
    end = (
        expiry
        if (start.year, start.month) == (expiry.year, expiry.month)
        else min(anniversary(offset + 1), expiry)
    )
    return start, end


def issue_subscription_grant(
    db: Session,
    user_id: UUID,
    *,
    plan_code: str,
    expires_at: datetime,
    granted_by: str,
    reason: str,
    now: datetime | None = None,
    change: bool = False,
    catalog: PlanCatalog | None = None,
) -> repository.BillingRecord:
    """Issue now, or atomically revoke and replace an existing Plus/Pro grant."""
    at_time = _utc(now or datetime.now(UTC))
    expiry = _utc(expires_at)
    plan = (catalog or get_plan_catalog()).get(plan_code)
    if plan is None or plan.code not in {"plus", "pro"}:
        raise ValueError("Cortex grants require an existing Plus or Pro plan")
    actor, explanation = _required(granted_by, "Granted by"), _required(reason, "Reason")
    if expiry <= at_time:
        raise ValueError("Expiry must be after the grant start")
    account = get_or_create_user_billing_account(db, user_id)
    repository.lock_billing_account(db, account.id)
    current = repository.get_current_subscription_grant(db, account.id)
    if change:
        if current is None:
            raise ValueError("No open grant exists; use grant to issue access")
        revoked = repository.revoke_current_subscription_grant(
            db,
            billing_account_id=account.id,
            now=at_time,
            revoked_by=actor,
            reason=explanation,
        )
        if revoked is not None:
            repository.close_grant_usage_periods(db, revoked["id"])
    grant = repository.create_subscription_grant(
        db,
        billing_account_id=account.id,
        plan_code=plan.code,
        starts_at=at_time,
        expires_at=expiry,
        granted_by=actor,
        reason=explanation,
        now=at_time,
    )
    # Resolve immediately so a transition closes its previous period atomically.
    from server.billing.subscription_service import resolve_effective_subscription

    resolve_effective_subscription(db, user_id, now=at_time, catalog=catalog)
    return grant


def revoke_subscription_grant(
    db: Session,
    user_id: UUID,
    *,
    revoked_by: str,
    reason: str,
    now: datetime | None = None,
) -> repository.BillingRecord | None:
    at_time = _utc(now or datetime.now(UTC))
    actor, explanation = _required(revoked_by, "Revoked by"), _required(reason, "Reason")
    account = get_or_create_user_billing_account(db, user_id)
    revoked = repository.revoke_current_subscription_grant(
        db,
        billing_account_id=account.id,
        now=at_time,
        revoked_by=actor,
        reason=explanation,
    )
    if revoked is not None:
        repository.close_grant_usage_periods(db, revoked["id"])
    from server.billing.subscription_service import resolve_effective_subscription

    resolve_effective_subscription(db, user_id, now=at_time)
    return revoked


def inspect_subscription_grant(db: Session, user_id: UUID) -> dict:
    """Read lifecycle and time-valid grant without creating an account or period."""
    account = repository.get_billing_account_for_user(db, user_id)
    if account is None:
        return {
            "user_id": user_id,
            "billing_account_id": None,
            "current_grant": None,
            "effective_grant": None,
        }
    current = repository.get_current_subscription_grant(db, account["id"])
    effective = repository.get_effective_subscription_grant(db, account["id"], datetime.now(UTC))
    if effective is not None:
        plan = get_plan_catalog().get(effective["plan_code"])
        if plan is None or plan.code not in {"plus", "pro"}:
            effective = None
    return {
        "user_id": user_id,
        "billing_account_id": account["id"],
        "current_grant": current,
        "effective_grant": effective,
    }
