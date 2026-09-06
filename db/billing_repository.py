"""SQLAlchemy Core repository for B2C billing persistence.

Repository functions never commit. Callers own transaction boundaries so
account creation, subscription updates, and future metering operations can be
composed atomically.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeAlias
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

BillingRecord: TypeAlias = dict[str, Any]

ALLOWED_USAGE_METERS = frozenset({"ai_credits"})

LIVE_SUBSCRIPTION_STATUSES = frozenset(
    {"trialing", "active", "past_due", "unpaid", "paused", "incomplete"}
)


@dataclass(frozen=True)
class SubscriptionSnapshot:
    """Provider subscription fields persisted as an internal snapshot."""

    billing_account_id: UUID
    provider: str
    provider_subscription_id: str
    plan_code: str
    status: str
    provider_price_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    canceled_at: datetime | None = None
    trial_end: datetime | None = None
    grace_until: datetime | None = None
    latest_invoice_id: str | None = None
    last_provider_event_at: datetime | None = None


def _table(name: str):
    from db.tables import get_table

    return get_table(name)


def _dialect_insert(db: Session, table):
    """Return an INSERT supporting ON CONFLICT for runtime and isolated tests."""
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        return pg_insert(table)
    if dialect_name == "sqlite":
        return sqlite_insert(table)
    raise RuntimeError(
        "Billing repository requires PostgreSQL; SQLite is supported only for isolated tests"
    )


def _record(row: RowMapping | None) -> BillingRecord | None:
    return dict(row) if row is not None else None


def _first_record(result) -> BillingRecord | None:
    return _record(result.mappings().first())


def _required_text(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _new_id_for(table) -> UUID | str:
    """Generate an ID compatible with PostgreSQL UUID and reflected SQLite text."""
    value = uuid4()
    try:
        if table.c.id.type.python_type is str:
            return value.hex
    except (AttributeError, NotImplementedError):
        pass
    return value


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    normalized_left = left.replace(tzinfo=UTC) if left.tzinfo is None else left.astimezone(UTC)
    normalized_right = right.replace(tzinfo=UTC) if right.tzinfo is None else right.astimezone(UTC)
    return normalized_left == normalized_right


def _normalized_provider(provider: str) -> str:
    return _required_text(provider, "provider").lower()


def _normalize_quantities(
    quantities: Mapping[str, int],
    *,
    field_name: str,
    require_positive: bool,
    allow_empty: bool = False,
) -> dict[str, int]:
    if not isinstance(quantities, Mapping):
        raise ValueError(f"{field_name} must be a mapping")
    if not quantities and not allow_empty:
        raise ValueError(f"{field_name} must be a non-empty mapping")

    normalized: dict[str, int] = {}
    for meter_key, quantity in quantities.items():
        if meter_key not in ALLOWED_USAGE_METERS:
            raise ValueError(f"Unknown usage meter: {meter_key}")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError(f"{field_name}.{meter_key} must be an integer")
        minimum = 1 if require_positive else 0
        if quantity < minimum:
            comparator = "positive" if require_positive else "nonnegative"
            raise ValueError(f"{field_name}.{meter_key} must be {comparator}")
        if not require_positive and quantity == 0:
            continue
        normalized[meter_key] = quantity
    if not normalized and not allow_empty:
        raise ValueError(f"{field_name} must contain at least one positive quantity")
    return normalized


def get_billing_account_for_user(db: Session, user_id: UUID) -> BillingRecord | None:
    billing_accounts = _table("billing_accounts")
    stmt = select(billing_accounts).where(
        and_(
            billing_accounts.c.owner_type == "user",
            billing_accounts.c.owner_id == user_id,
        )
    )
    return _first_record(db.execute(stmt))


def get_billing_account_by_id(
    db: Session,
    billing_account_id: UUID,
) -> BillingRecord | None:
    billing_accounts = _table("billing_accounts")
    return _first_record(
        db.execute(select(billing_accounts).where(billing_accounts.c.id == billing_account_id))
    )


def get_billing_account_by_stripe_customer_id(
    db: Session,
    stripe_customer_id: str,
) -> BillingRecord | None:
    billing_accounts = _table("billing_accounts")
    customer_id = _required_text(stripe_customer_id, "stripe_customer_id")
    return _first_record(
        db.execute(
            select(billing_accounts).where(billing_accounts.c.stripe_customer_id == customer_id)
        )
    )


def lock_billing_account(
    db: Session,
    billing_account_id: UUID,
) -> BillingRecord | None:
    """Lock one billing owner while a reservation idempotency key is claimed."""
    billing_accounts = _table("billing_accounts")
    stmt = (
        select(billing_accounts)
        .where(billing_accounts.c.id == billing_account_id)
        .with_for_update()
    )
    return _first_record(db.execute(stmt))


def get_or_create_billing_account_for_user(db: Session, user_id: UUID) -> BillingRecord:
    """Get or atomically create the one B2C billing account for a user."""
    billing_accounts = _table("billing_accounts")
    account_id = _new_id_for(billing_accounts)
    stmt = (
        _dialect_insert(db, billing_accounts)
        .values(id=account_id, owner_type="user", owner_id=user_id)
        .on_conflict_do_nothing(index_elements=["owner_type", "owner_id"])
        .returning(*billing_accounts.c)
    )
    created = _first_record(db.execute(stmt))
    if created is not None:
        return created

    existing = get_billing_account_for_user(db, user_id)
    if existing is None:
        raise RuntimeError("Billing account conflict occurred but the account could not be read")
    return existing


def set_stripe_customer_id(
    db: Session,
    billing_account_id: UUID,
    stripe_customer_id: str,
) -> BillingRecord | None:
    billing_accounts = _table("billing_accounts")
    customer_id = _required_text(stripe_customer_id, "stripe_customer_id")
    stmt = (
        update(billing_accounts)
        .where(billing_accounts.c.id == billing_account_id)
        .values(stripe_customer_id=customer_id, updated_at=func.now())
        .returning(*billing_accounts.c)
    )
    return _first_record(db.execute(stmt))


def claim_stripe_customer_id(
    db: Session,
    billing_account_id: UUID,
    stripe_customer_id: str,
) -> BillingRecord:
    """Set a Stripe Customer exactly once and return the winning account row.

    Concurrent Checkout requests use the same provider idempotency key. This
    compare-and-set prevents either request from overwriting a Customer already
    persisted by the other transaction.
    """
    billing_accounts = _table("billing_accounts")
    customer_id = _required_text(stripe_customer_id, "stripe_customer_id")
    stmt = (
        update(billing_accounts)
        .where(
            and_(
                billing_accounts.c.id == billing_account_id,
                billing_accounts.c.stripe_customer_id.is_(None),
            )
        )
        .values(stripe_customer_id=customer_id, updated_at=func.now())
        .returning(*billing_accounts.c)
    )
    claimed = _first_record(db.execute(stmt))
    if claimed is not None:
        return claimed

    existing = _first_record(
        db.execute(select(billing_accounts).where(billing_accounts.c.id == billing_account_id))
    )
    if existing is None:
        raise RuntimeError("Billing account could not be read while claiming Stripe customer")
    if not existing.get("stripe_customer_id"):
        raise RuntimeError("Stripe customer claim completed without a persisted customer")
    return existing


def get_subscription_by_provider_id(
    db: Session,
    provider: str,
    provider_subscription_id: str,
) -> BillingRecord | None:
    subscriptions = _table("subscriptions")
    stmt = select(subscriptions).where(
        and_(
            subscriptions.c.provider == _normalized_provider(provider),
            subscriptions.c.provider_subscription_id
            == _required_text(provider_subscription_id, "provider_subscription_id"),
        )
    )
    return _first_record(db.execute(stmt))


def get_live_subscription_for_account(
    db: Session,
    billing_account_id: UUID,
) -> BillingRecord | None:
    """Return the provider-lifecycle row; WP3 decides whether it grants access."""
    subscriptions = _table("subscriptions")
    stmt = (
        select(subscriptions)
        .where(
            and_(
                subscriptions.c.billing_account_id == billing_account_id,
                subscriptions.c.status.in_(sorted(LIVE_SUBSCRIPTION_STATUSES)),
            )
        )
        .order_by(
            subscriptions.c.current_period_end.desc().nullslast(),
            subscriptions.c.updated_at.desc(),
        )
        .limit(1)
    )
    return _first_record(db.execute(stmt))


def get_latest_subscription_for_account(
    db: Session,
    billing_account_id: UUID,
) -> BillingRecord | None:
    """Return the latest snapshot, including terminal lifecycle statuses.

    The live-subscription query intentionally excludes ``canceled`` rows so the
    database can enforce one provider-live row per account. Effective-plan
    resolution still needs the latest terminal row to honor cancel-at-period-
    end access when no live row exists.
    """
    subscriptions = _table("subscriptions")
    stmt = (
        select(subscriptions)
        .where(subscriptions.c.billing_account_id == billing_account_id)
        .order_by(
            subscriptions.c.last_provider_event_at.desc().nullslast(),
            subscriptions.c.updated_at.desc(),
            subscriptions.c.current_period_end.desc().nullslast(),
        )
        .limit(1)
    )
    return _first_record(db.execute(stmt))


def upsert_subscription_snapshot(
    db: Session,
    snapshot: SubscriptionSnapshot,
) -> BillingRecord:
    """Upsert one provider snapshot without allowing cross-account reassignment."""
    subscriptions = _table("subscriptions")
    payload = asdict(snapshot)
    payload["provider"] = _normalized_provider(snapshot.provider)
    payload["provider_subscription_id"] = _required_text(
        snapshot.provider_subscription_id, "provider_subscription_id"
    )
    payload["plan_code"] = _required_text(snapshot.plan_code, "plan_code").lower()
    payload["status"] = _required_text(snapshot.status, "status").lower()

    insert_payload = {"id": _new_id_for(subscriptions), **payload}
    update_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"billing_account_id", "provider", "provider_subscription_id"}
    }
    update_payload["updated_at"] = func.now()

    update_condition = subscriptions.c.billing_account_id == snapshot.billing_account_id
    if snapshot.last_provider_event_at is not None:
        update_condition = and_(
            update_condition,
            or_(
                subscriptions.c.last_provider_event_at.is_(None),
                subscriptions.c.last_provider_event_at <= snapshot.last_provider_event_at,
            ),
        )

    stmt = (
        _dialect_insert(db, subscriptions)
        .values(**insert_payload)
        .on_conflict_do_update(
            index_elements=["provider", "provider_subscription_id"],
            set_=update_payload,
            where=update_condition,
        )
        .returning(*subscriptions.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is not None:
        return persisted

    existing = get_subscription_by_provider_id(
        db,
        payload["provider"],
        payload["provider_subscription_id"],
    )
    if existing is not None and existing["billing_account_id"] != snapshot.billing_account_id:
        raise ValueError("Provider subscription is already assigned to another billing account")
    if existing is not None and snapshot.last_provider_event_at is not None:
        existing_event_at = existing.get("last_provider_event_at")
        if isinstance(existing_event_at, datetime):
            normalized_existing = (
                existing_event_at.replace(tzinfo=UTC)
                if existing_event_at.tzinfo is None
                else existing_event_at.astimezone(UTC)
            )
            normalized_incoming = (
                snapshot.last_provider_event_at.replace(tzinfo=UTC)
                if snapshot.last_provider_event_at.tzinfo is None
                else snapshot.last_provider_event_at.astimezone(UTC)
            )
            if normalized_existing > normalized_incoming:
                return existing
    raise RuntimeError("Subscription snapshot could not be persisted")


def get_active_usage_period(
    db: Session,
    billing_account_id: UUID,
    at_time: datetime,
) -> BillingRecord | None:
    usage_periods = _table("usage_periods")
    stmt = (
        select(usage_periods)
        .where(
            and_(
                usage_periods.c.billing_account_id == billing_account_id,
                usage_periods.c.status == "active",
                usage_periods.c.starts_at <= at_time,
                usage_periods.c.ends_at > at_time,
            )
        )
        .order_by(usage_periods.c.starts_at.desc())
        .limit(1)
    )
    return _first_record(db.execute(stmt))


def get_current_subscription_grant(
    db: Session,
    billing_account_id: UUID,
) -> BillingRecord | None:
    """Inspect the open lifecycle row, including a scheduled or elapsed grant."""
    grants = _table("subscription_grants")
    return _record(
        db.execute(
            select(grants).where(
                grants.c.billing_account_id == billing_account_id,
                grants.c.status == "active",
            )
        )
        .mappings()
        .one_or_none()
    )


def get_effective_subscription_grant(
    db: Session,
    billing_account_id: UUID,
    at_time: datetime,
) -> BillingRecord | None:
    """Return the sole time-valid candidate; the service validates its catalogue plan."""
    grants = _table("subscription_grants")
    return _record(
        db.execute(
            select(grants).where(
                grants.c.billing_account_id == billing_account_id,
                grants.c.status == "active",
                grants.c.revoked_at.is_(None),
                grants.c.starts_at <= at_time,
                grants.c.expires_at > at_time,
            )
        )
        .mappings()
        .one_or_none()
    )


def create_subscription_grant(
    db: Session,
    *,
    billing_account_id: UUID,
    plan_code: str,
    starts_at: datetime,
    expires_at: datetime,
    granted_by: str,
    reason: str,
    now: datetime,
) -> BillingRecord:
    """Insert under the account lock; an open grant must first be revoked by the service."""
    if lock_billing_account(db, billing_account_id) is None:
        raise ValueError("Billing account does not exist")
    grants = _table("subscription_grants")
    db.execute(
        update(grants)
        .where(
            grants.c.billing_account_id == billing_account_id,
            grants.c.status == "active",
            grants.c.expires_at <= now,
        )
        .values(status="expired", updated_at=now)
    )
    if get_current_subscription_grant(db, billing_account_id) is not None:
        raise ValueError("An open grant already exists; use change to replace it")
    return dict(
        db.execute(
            insert(grants)
            .values(
                id=_new_id_for(grants),
                billing_account_id=billing_account_id,
                plan_code=plan_code,
                status="active",
                starts_at=starts_at,
                expires_at=expires_at,
                granted_by=granted_by,
                reason=reason,
                created_at=now,
                updated_at=now,
            )
            .returning(*grants.c)
        )
        .mappings()
        .one()
    )


def revoke_current_subscription_grant(
    db: Session,
    *,
    billing_account_id: UUID,
    now: datetime,
    revoked_by: str,
    reason: str,
) -> BillingRecord | None:
    """Retain issuance audit and append revocation evidence without committing."""
    lock_billing_account(db, billing_account_id)
    grants = _table("subscription_grants")
    return _first_record(
        db.execute(
            update(grants)
            .where(
                grants.c.billing_account_id == billing_account_id,
                grants.c.status == "active",
            )
            .values(
                status="revoked",
                revoked_at=now,
                revoked_by=revoked_by,
                revocation_reason=reason,
                updated_at=now,
            )
            .returning(*grants.c)
        )
    )


def close_grant_usage_periods(db: Session, subscription_grant_id: UUID) -> None:
    """Close access while preserving dates, counters, reservations and ledger history."""
    periods = _table("usage_periods")
    db.execute(
        update(periods)
        .where(
            periods.c.subscription_grant_id == subscription_grant_id,
            periods.c.status == "active",
        )
        .values(status="closed", updated_at=func.now())
    )


def create_usage_period(
    db: Session,
    *,
    billing_account_id: UUID,
    plan_code: str,
    starts_at: datetime,
    ends_at: datetime,
    subscription_id: UUID | None = None,
    subscription_grant_id: UUID | None = None,
) -> BillingRecord:
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    usage_periods = _table("usage_periods")
    stmt = (
        insert(usage_periods)
        .values(
            id=_new_id_for(usage_periods),
            billing_account_id=billing_account_id,
            subscription_id=subscription_id,
            subscription_grant_id=subscription_grant_id,
            plan_code=_required_text(plan_code, "plan_code").lower(),
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
        )
        .returning(*usage_periods.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage period could not be created")
    return persisted


def get_or_create_usage_period(
    db: Session,
    *,
    billing_account_id: UUID,
    plan_code: str,
    starts_at: datetime,
    ends_at: datetime,
    subscription_id: UUID | None = None,
    subscription_grant_id: UUID | None = None,
) -> BillingRecord:
    """Atomically create an exact usage period or return its matching row."""
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    usage_periods = _table("usage_periods")
    normalized_plan = _required_text(plan_code, "plan_code").lower()
    if subscription_id is not None and subscription_grant_id is not None:
        raise ValueError("Usage period must have only one subscription source")
    grant_period = subscription_grant_id is not None
    source_match = (
        usage_periods.c.subscription_grant_id == subscription_grant_id
        if grant_period
        else usage_periods.c.subscription_grant_id.is_(None)
    )
    stmt = (
        _dialect_insert(db, usage_periods)
        .values(
            id=_new_id_for(usage_periods),
            billing_account_id=billing_account_id,
            subscription_id=subscription_id,
            subscription_grant_id=subscription_grant_id,
            plan_code=normalized_plan,
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
        )
        .on_conflict_do_nothing(
            index_elements=[
                "subscription_grant_id" if grant_period else "billing_account_id",
                "starts_at",
            ],
            index_where=(
                usage_periods.c.subscription_grant_id.is_not(None)
                if grant_period
                else usage_periods.c.subscription_grant_id.is_(None)
            ),
        )
        .returning(*usage_periods.c)
    )
    created = _first_record(db.execute(stmt))
    if created is not None:
        return created

    existing_stmt = select(usage_periods).where(
        and_(
            usage_periods.c.billing_account_id == billing_account_id,
            usage_periods.c.starts_at == starts_at,
            source_match,
        )
    )
    existing = _first_record(db.execute(existing_stmt))
    if existing is None:
        raise RuntimeError("Usage period conflict occurred but the row could not be read")

    definition_conflicts = (
        existing.get("subscription_id") != subscription_id
        or existing.get("subscription_grant_id") != subscription_grant_id
        or existing.get("plan_code") != normalized_plan
        or not _same_datetime(existing.get("ends_at"), ends_at)
    )
    if definition_conflicts:
        raise RuntimeError("Existing usage period conflicts with the effective subscription")
    if existing.get("status") == "closed":
        reactivate_stmt = (
            update(usage_periods)
            .where(
                and_(
                    usage_periods.c.id == existing["id"],
                    usage_periods.c.status == "closed",
                )
            )
            .values(status="active", updated_at=func.now())
            .returning(*usage_periods.c)
        )
        reactivated = _first_record(db.execute(reactivate_stmt))
        if reactivated is not None:
            return reactivated
    if existing.get("status") != "active":
        raise RuntimeError("Existing usage period has an unsupported state")
    return existing


def synchronize_usage_period(
    db: Session,
    *,
    billing_account_id: UUID,
    plan_code: str,
    starts_at: datetime,
    ends_at: datetime,
    subscription_id: UUID | None = None,
    subscription_grant_id: UUID | None = None,
) -> BillingRecord:
    """Synchronize one provider period without replacing its usage counters.

    Stripe can change a plan or period end while retaining the same period
    start. The account/start key identifies that economic period, so updating
    its definition in place preserves already-settled counters and prevents a
    second allowance reset.
    """
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    if subscription_grant_id is not None:
        return get_or_create_usage_period(
            db,
            billing_account_id=billing_account_id,
            plan_code=plan_code,
            starts_at=starts_at,
            ends_at=ends_at,
            subscription_id=subscription_id,
            subscription_grant_id=subscription_grant_id,
        )
    usage_periods = _table("usage_periods")
    normalized_plan = _required_text(plan_code, "plan_code").lower()
    stmt = (
        _dialect_insert(db, usage_periods)
        .values(
            id=_new_id_for(usage_periods),
            billing_account_id=billing_account_id,
            subscription_id=subscription_id,
            plan_code=normalized_plan,
            starts_at=starts_at,
            ends_at=ends_at,
            status="active",
        )
        .on_conflict_do_update(
            index_elements=["billing_account_id", "starts_at"],
            index_where=usage_periods.c.subscription_grant_id.is_(None),
            set_={
                "subscription_id": subscription_id,
                "plan_code": normalized_plan,
                "ends_at": ends_at,
                "status": "active",
                "updated_at": func.now(),
            },
        )
        .returning(*usage_periods.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage period could not be synchronized")
    return persisted


def close_usage_period(db: Session, usage_period_id: UUID) -> BillingRecord | None:
    usage_periods = _table("usage_periods")
    stmt = (
        update(usage_periods)
        .where(usage_periods.c.id == usage_period_id)
        .values(status="closed", updated_at=func.now())
        .returning(*usage_periods.c)
    )
    return _first_record(db.execute(stmt))


def get_or_create_usage_counter(
    db: Session,
    usage_period_id: UUID,
    meter_key: str,
) -> BillingRecord:
    if meter_key not in ALLOWED_USAGE_METERS:
        raise ValueError(f"Unknown usage meter: {meter_key}")

    usage_counters = _table("usage_counters")
    stmt = (
        _dialect_insert(db, usage_counters)
        .values(
            id=_new_id_for(usage_counters),
            usage_period_id=usage_period_id,
            meter_key=meter_key,
            used_quantity=0,
            reserved_quantity=0,
        )
        .on_conflict_do_nothing(index_elements=["usage_period_id", "meter_key"])
        .returning(*usage_counters.c)
    )
    created = _first_record(db.execute(stmt))
    if created is not None:
        return created

    existing_stmt = select(usage_counters).where(
        and_(
            usage_counters.c.usage_period_id == usage_period_id,
            usage_counters.c.meter_key == meter_key,
        )
    )
    existing = _first_record(db.execute(existing_stmt))
    if existing is None:
        raise RuntimeError("Usage counter conflict occurred but the counter could not be read")
    return existing


def lock_usage_counters(
    db: Session,
    usage_period_id: UUID,
    meter_keys: Sequence[str],
) -> list[BillingRecord]:
    """Lock counters in deterministic meter order to avoid lock inversion."""
    normalized_keys = sorted(set(meter_keys))
    unknown = set(normalized_keys) - ALLOWED_USAGE_METERS
    if unknown:
        raise ValueError(f"Unknown usage meters: {', '.join(sorted(unknown))}")
    if not normalized_keys:
        return []

    usage_counters = _table("usage_counters")
    stmt = (
        select(usage_counters)
        .where(
            and_(
                usage_counters.c.usage_period_id == usage_period_id,
                usage_counters.c.meter_key.in_(normalized_keys),
            )
        )
        .order_by(usage_counters.c.meter_key)
        .with_for_update()
    )
    rows = [dict(row) for row in db.execute(stmt).mappings().all()]
    found = {row["meter_key"] for row in rows}
    missing = set(normalized_keys) - found
    if missing:
        raise LookupError(f"Usage counters must exist before locking: {', '.join(sorted(missing))}")
    return rows


def reserve_usage_quantities(
    db: Session,
    usage_period_id: UUID,
    quantities: Mapping[str, int],
) -> list[BillingRecord]:
    """Increment already-locked reservation counters without committing."""
    normalized = _normalize_quantities(
        quantities,
        field_name="quantities",
        require_positive=True,
    )
    usage_counters = _table("usage_counters")
    persisted: list[BillingRecord] = []
    for meter_key in sorted(normalized):
        stmt = (
            update(usage_counters)
            .where(
                and_(
                    usage_counters.c.usage_period_id == usage_period_id,
                    usage_counters.c.meter_key == meter_key,
                )
            )
            .values(
                reserved_quantity=(usage_counters.c.reserved_quantity + normalized[meter_key]),
                updated_at=func.now(),
            )
            .returning(*usage_counters.c)
        )
        row = _first_record(db.execute(stmt))
        if row is None:
            raise LookupError(f"Usage counter was not found: {meter_key}")
        persisted.append(row)
    return persisted


def settle_usage_quantities(
    db: Session,
    usage_period_id: UUID,
    *,
    requested_quantities: Mapping[str, int],
    successful_quantities: Mapping[str, int],
) -> list[BillingRecord]:
    """Move an already-locked reservation from reserved to finalized usage."""
    requested = _normalize_quantities(
        requested_quantities,
        field_name="requested_quantities",
        require_positive=True,
    )
    successful = _normalize_quantities(
        successful_quantities,
        field_name="successful_quantities",
        require_positive=False,
        allow_empty=True,
    )
    if any(key not in requested or value > requested[key] for key, value in successful.items()):
        raise ValueError("Successful quantities cannot exceed the reservation")

    usage_counters = _table("usage_counters")
    persisted: list[BillingRecord] = []
    for meter_key in sorted(requested):
        reserved_quantity = requested[meter_key]
        used_quantity = successful.get(meter_key, 0)
        stmt = (
            update(usage_counters)
            .where(
                and_(
                    usage_counters.c.usage_period_id == usage_period_id,
                    usage_counters.c.meter_key == meter_key,
                    usage_counters.c.reserved_quantity >= reserved_quantity,
                )
            )
            .values(
                used_quantity=usage_counters.c.used_quantity + used_quantity,
                reserved_quantity=(usage_counters.c.reserved_quantity - reserved_quantity),
                updated_at=func.now(),
            )
            .returning(*usage_counters.c)
        )
        row = _first_record(db.execute(stmt))
        if row is None:
            raise RuntimeError(f"Usage counter cannot settle the reserved quantity: {meter_key}")
        persisted.append(row)
    return persisted


def release_usage_quantities(
    db: Session,
    usage_period_id: UUID,
    quantities: Mapping[str, int],
) -> list[BillingRecord]:
    """Release quantities from already-locked counters without changing usage."""
    normalized = _normalize_quantities(
        quantities,
        field_name="quantities",
        require_positive=True,
    )
    usage_counters = _table("usage_counters")
    persisted: list[BillingRecord] = []
    for meter_key in sorted(normalized):
        quantity = normalized[meter_key]
        stmt = (
            update(usage_counters)
            .where(
                and_(
                    usage_counters.c.usage_period_id == usage_period_id,
                    usage_counters.c.meter_key == meter_key,
                    usage_counters.c.reserved_quantity >= quantity,
                )
            )
            .values(
                reserved_quantity=usage_counters.c.reserved_quantity - quantity,
                updated_at=func.now(),
            )
            .returning(*usage_counters.c)
        )
        row = _first_record(db.execute(stmt))
        if row is None:
            raise RuntimeError(f"Usage counter cannot release the reserved quantity: {meter_key}")
        persisted.append(row)
    return persisted


def create_usage_reservation(
    db: Session,
    *,
    billing_account_id: UUID,
    usage_period_id: UUID,
    request_id: str,
    operation_type: str,
    requested_quantities: Mapping[str, int],
) -> BillingRecord:
    usage_reservations = _table("usage_reservations")
    quantities = _normalize_quantities(
        requested_quantities,
        field_name="requested_quantities",
        require_positive=True,
    )
    values: dict[str, Any] = {
        "id": _new_id_for(usage_reservations),
        "billing_account_id": billing_account_id,
        "usage_period_id": usage_period_id,
        "request_id": _required_text(request_id, "request_id"),
        "operation_type": _required_text(operation_type, "operation_type").lower(),
        "state": "reserved",
        "requested_quantities": quantities,
    }
    if "last_activity_at" in usage_reservations.c:
        values["last_activity_at"] = func.now()
    stmt = insert(usage_reservations).values(**values).returning(*usage_reservations.c)
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage reservation could not be created")
    return persisted


def _get_usage_reservation(
    db: Session,
    billing_account_id: UUID,
    request_id: str,
    *,
    for_update: bool,
) -> BillingRecord | None:
    usage_reservations = _table("usage_reservations")
    stmt = select(usage_reservations).where(
        and_(
            usage_reservations.c.billing_account_id == billing_account_id,
            usage_reservations.c.request_id == _required_text(request_id, "request_id"),
        )
    )
    if for_update:
        stmt = stmt.with_for_update()
    return _first_record(db.execute(stmt))


def get_usage_reservation(
    db: Session,
    billing_account_id: UUID,
    request_id: str,
) -> BillingRecord | None:
    return _get_usage_reservation(
        db,
        billing_account_id,
        request_id,
        for_update=False,
    )


def get_usage_reservation_by_id(
    db: Session,
    reservation_id: UUID,
    *,
    for_update: bool = False,
) -> BillingRecord | None:
    usage_reservations = _table("usage_reservations")
    stmt = select(usage_reservations).where(usage_reservations.c.id == reservation_id)
    if for_update:
        stmt = stmt.with_for_update()
    return _first_record(db.execute(stmt))


def lock_stale_usage_reservations(
    db: Session,
    *,
    older_than: datetime,
) -> list[BillingRecord]:
    """Lock clearly stale reservations while skipping rows owned by active workers."""
    usage_reservations = _table("usage_reservations")
    activity_column = (
        usage_reservations.c.last_activity_at
        if "last_activity_at" in usage_reservations.c
        else usage_reservations.c.created_at
    )
    stmt = (
        select(usage_reservations)
        .where(
            and_(
                usage_reservations.c.state == "reserved",
                activity_column < older_than,
            )
        )
        .order_by(activity_column, usage_reservations.c.id)
        .with_for_update(skip_locked=True)
    )
    return [dict(row) for row in db.execute(stmt).mappings().all()]


def touch_usage_reservation_activity(
    db: Session,
    reservation_ids: Sequence[UUID],
) -> int:
    """Refresh active reservation leases without reviving terminal rows."""

    normalized_ids = tuple(dict.fromkeys(reservation_ids))
    if not normalized_ids:
        return 0
    usage_reservations = _table("usage_reservations")
    if "last_activity_at" not in usage_reservations.c:
        raise RuntimeError(
            "usage_reservations.last_activity_at is missing; apply billing migrations"
        )
    result = db.execute(
        update(usage_reservations)
        .where(
            and_(
                usage_reservations.c.id.in_(normalized_ids),
                usage_reservations.c.state == "reserved",
            )
        )
        .values(last_activity_at=func.now())
    )
    return int(result.rowcount or 0)


def update_usage_reservation_requested_quantities(
    db: Session,
    *,
    reservation_id: UUID,
    requested_quantities: Mapping[str, int],
) -> BillingRecord:
    """Expand one locked reservation while it remains active."""

    quantities = _normalize_quantities(
        requested_quantities,
        field_name="requested_quantities",
        require_positive=True,
    )
    usage_reservations = _table("usage_reservations")
    values: dict[str, Any] = {"requested_quantities": quantities}
    if "last_activity_at" in usage_reservations.c:
        values["last_activity_at"] = func.now()
    persisted = _first_record(
        db.execute(
            update(usage_reservations)
            .where(
                and_(
                    usage_reservations.c.id == reservation_id,
                    usage_reservations.c.state == "reserved",
                )
            )
            .values(**values)
            .returning(*usage_reservations.c)
        )
    )
    if persisted is None:
        raise RuntimeError("Usage reservation changed while it was being supplemented")
    return persisted


def settle_usage_reservation(
    db: Session,
    *,
    billing_account_id: UUID,
    request_id: str,
    settled_quantities: Mapping[str, int],
) -> BillingRecord:
    quantities = _normalize_quantities(
        settled_quantities,
        field_name="settled_quantities",
        require_positive=False,
        allow_empty=True,
    )
    existing = _get_usage_reservation(
        db,
        billing_account_id,
        request_id,
        for_update=True,
    )
    if existing is None:
        raise LookupError("Usage reservation was not found")

    requested = dict(existing["requested_quantities"])
    if any(key not in requested or value > requested[key] for key, value in quantities.items()):
        raise ValueError("Settled quantities cannot exceed the reservation")

    if existing["state"] == "settled":
        if dict(existing["settled_quantities"] or {}) != quantities:
            raise ValueError("Usage reservation was already settled with different quantities")
        return existing
    if existing["state"] != "reserved":
        raise ValueError(f"Cannot settle a reservation in state {existing['state']}")

    usage_reservations = _table("usage_reservations")
    stmt = (
        update(usage_reservations)
        .where(
            and_(
                usage_reservations.c.id == existing["id"],
                usage_reservations.c.state == "reserved",
            )
        )
        .values(
            state="settled",
            settled_quantities=quantities,
            settled_at=func.now(),
            release_reason=None,
        )
        .returning(*usage_reservations.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage reservation state changed during settlement")
    return persisted


def release_usage_reservation(
    db: Session,
    *,
    billing_account_id: UUID,
    request_id: str,
    release_reason: str,
) -> BillingRecord:
    reason = _required_text(release_reason, "release_reason")
    existing = _get_usage_reservation(
        db,
        billing_account_id,
        request_id,
        for_update=True,
    )
    if existing is None:
        raise LookupError("Usage reservation was not found")
    if existing["state"] == "released":
        return existing
    if existing["state"] != "reserved":
        raise ValueError(f"Cannot release a reservation in state {existing['state']}")

    usage_reservations = _table("usage_reservations")
    stmt = (
        update(usage_reservations)
        .where(
            and_(
                usage_reservations.c.id == existing["id"],
                usage_reservations.c.state == "reserved",
            )
        )
        .values(state="released", release_reason=reason, released_at=func.now())
        .returning(*usage_reservations.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage reservation state changed during release")
    return persisted


def expire_usage_reservation(
    db: Session,
    *,
    reservation_id: UUID,
    release_reason: str,
) -> BillingRecord:
    """Mark one locked, still-reserved row expired without committing."""
    usage_reservations = _table("usage_reservations")
    stmt = (
        update(usage_reservations)
        .where(
            and_(
                usage_reservations.c.id == reservation_id,
                usage_reservations.c.state == "reserved",
            )
        )
        .values(
            state="expired",
            release_reason=_required_text(release_reason, "release_reason"),
            released_at=func.now(),
        )
        .returning(*usage_reservations.c)
    )
    persisted = _first_record(db.execute(stmt))
    if persisted is None:
        raise RuntimeError("Usage reservation state changed during expiry")
    return persisted


def create_credit_transaction(
    db: Session,
    *,
    billing_account_id: UUID,
    usage_period_id: UUID,
    reservation_id: UUID,
    request_id: str,
    operation_type: str,
    item_index: int,
    item_type: str,
    total_credits: int,
    pricing_version: str,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int = 0,
    normal_input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    output_tokens: int = 0,
    input_credits: int = 0,
    normal_input_credits: int = 0,
    cached_input_credits: int = 0,
    cache_write_credits: int = 0,
    output_credits: int = 0,
    fixed_credits: int = 0,
    provider_cost_usd: float = 0.0,
    uncached_equivalent_credits: int = 0,
    cache_savings_credits: int = 0,
    usage_estimated: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> BillingRecord:
    """Persist one immutable line item for a settled credit reservation."""
    if item_type not in {"model", "research", "adjustment"}:
        raise ValueError("Unsupported credit transaction item_type")
    numeric_values = {
        "item_index": item_index,
        "total_credits": total_credits,
        "input_tokens": input_tokens,
        "normal_input_tokens": normal_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "input_credits": input_credits,
        "normal_input_credits": normal_input_credits,
        "cached_input_credits": cached_input_credits,
        "cache_write_credits": cache_write_credits,
        "output_credits": output_credits,
        "fixed_credits": fixed_credits,
        "uncached_equivalent_credits": uncached_equivalent_credits,
        "cache_savings_credits": cache_savings_credits,
    }
    for label, value in numeric_values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a nonnegative integer")
    if provider_cost_usd < 0:
        raise ValueError("provider_cost_usd must be nonnegative")

    transactions = _table("credit_transactions")
    values = {
        "id": _new_id_for(transactions),
        "billing_account_id": billing_account_id,
        "usage_period_id": usage_period_id,
        "reservation_id": reservation_id,
        "request_id": _required_text(request_id, "request_id"),
        "operation_type": _required_text(operation_type, "operation_type").lower(),
        "item_index": item_index,
        "item_type": item_type,
        "provider": str(provider).strip().lower() if provider else None,
        "model": str(model).strip() if model else None,
        "input_tokens": input_tokens,
        "normal_input_tokens": normal_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_tokens": cache_write_tokens,
        "reasoning_tokens": reasoning_tokens,
        "output_tokens": output_tokens,
        "input_credits": input_credits,
        "normal_input_credits": normal_input_credits,
        "cached_input_credits": cached_input_credits,
        "cache_write_credits": cache_write_credits,
        "output_credits": output_credits,
        "fixed_credits": fixed_credits,
        "total_credits": total_credits,
        "uncached_equivalent_credits": uncached_equivalent_credits,
        "cache_savings_credits": cache_savings_credits,
        "provider_cost_usd": provider_cost_usd,
        "usage_estimated": bool(usage_estimated),
        "pricing_version": _required_text(pricing_version, "pricing_version"),
        "metadata": dict(metadata or {}),
    }
    stmt = (
        _dialect_insert(db, transactions)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["reservation_id", "item_index"])
        .returning(*transactions.c)
    )
    created = _first_record(db.execute(stmt))
    if created is not None:
        return created
    existing = _first_record(
        db.execute(
            select(transactions).where(
                and_(
                    transactions.c.reservation_id == reservation_id,
                    transactions.c.item_index == item_index,
                )
            )
        )
    )
    if existing is None:
        raise RuntimeError("Credit transaction conflict could not be resolved")
    comparable = (
        "request_id",
        "operation_type",
        "item_type",
        "provider",
        "model",
        "input_tokens",
        "normal_input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "output_tokens",
        "input_credits",
        "normal_input_credits",
        "cached_input_credits",
        "cache_write_credits",
        "output_credits",
        "fixed_credits",
        "total_credits",
        "uncached_equivalent_credits",
        "cache_savings_credits",
        "usage_estimated",
        "pricing_version",
        "metadata",
    )
    if any(existing.get(key) != values[key] for key in comparable):
        raise ValueError("Credit transaction item was already recorded differently")
    if Decimal(str(existing.get("provider_cost_usd") or 0)) != Decimal(
        str(values["provider_cost_usd"])
    ):
        raise ValueError("Credit transaction item was already recorded differently")
    return existing


def list_credit_transactions(
    db: Session,
    *,
    billing_account_id: UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[BillingRecord]:
    if limit < 1 or limit > 1000 or offset < 0:
        raise ValueError("Invalid credit transaction pagination")
    transactions = _table("credit_transactions")
    stmt = (
        select(transactions)
        .where(transactions.c.billing_account_id == billing_account_id)
        .order_by(transactions.c.created_at.desc(), transactions.c.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [dict(row) for row in db.execute(stmt).mappings().all()]


def _validate_payload_hash(payload_hash: str) -> str:
    normalized = _required_text(payload_hash, "payload_hash").lower()
    if len(normalized) != 64:
        raise ValueError("payload_hash must be a SHA-256 hex digest")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise ValueError("payload_hash must be a SHA-256 hex digest") from exc
    return normalized


def create_webhook_event_if_absent(
    db: Session,
    *,
    provider: str,
    provider_event_id: str,
    event_type: str,
    payload_hash: str,
) -> tuple[BillingRecord, bool]:
    """Create an idempotency record and report whether this call inserted it."""
    billing_webhook_events = _table("billing_webhook_events")
    provider_name = _normalized_provider(provider)
    external_id = _required_text(provider_event_id, "provider_event_id")
    digest = _validate_payload_hash(payload_hash)
    stmt = (
        _dialect_insert(db, billing_webhook_events)
        .values(
            id=_new_id_for(billing_webhook_events),
            provider=provider_name,
            provider_event_id=external_id,
            event_type=_required_text(event_type, "event_type"),
            payload_hash=digest,
            processing_status="received",
        )
        .on_conflict_do_nothing(index_elements=["provider", "provider_event_id"])
        .returning(*billing_webhook_events.c)
    )
    created = _first_record(db.execute(stmt))
    if created is not None:
        return created, True

    existing_stmt = select(billing_webhook_events).where(
        and_(
            billing_webhook_events.c.provider == provider_name,
            billing_webhook_events.c.provider_event_id == external_id,
        )
    )
    existing = _first_record(db.execute(existing_stmt))
    if existing is None:
        raise RuntimeError("Webhook event conflict occurred but the event could not be read")
    if existing["payload_hash"] != digest:
        raise ValueError("Webhook event ID was reused with a different payload hash")
    return existing, False


def lock_webhook_event(
    db: Session,
    *,
    provider: str,
    provider_event_id: str,
) -> BillingRecord | None:
    """Serialize webhook retry/duplicate processing for one provider event."""
    billing_webhook_events = _table("billing_webhook_events")
    stmt = (
        select(billing_webhook_events)
        .where(
            and_(
                billing_webhook_events.c.provider == _normalized_provider(provider),
                billing_webhook_events.c.provider_event_id
                == _required_text(provider_event_id, "provider_event_id"),
            )
        )
        .with_for_update()
    )
    return _first_record(db.execute(stmt))


def mark_webhook_event_processed(
    db: Session,
    event_id: UUID,
    *,
    ignored: bool = False,
) -> BillingRecord | None:
    billing_webhook_events = _table("billing_webhook_events")
    stmt = (
        update(billing_webhook_events)
        .where(billing_webhook_events.c.id == event_id)
        .values(
            processing_status="ignored" if ignored else "processed",
            processed_at=func.now(),
            error_message=None,
        )
        .returning(*billing_webhook_events.c)
    )
    return _first_record(db.execute(stmt))


def mark_webhook_event_failed(
    db: Session,
    event_id: UUID,
    *,
    error_message: str,
) -> BillingRecord | None:
    billing_webhook_events = _table("billing_webhook_events")
    stmt = (
        update(billing_webhook_events)
        .where(billing_webhook_events.c.id == event_id)
        .values(
            processing_status="failed",
            processed_at=func.now(),
            error_message=_required_text(error_message, "error_message"),
        )
        .returning(*billing_webhook_events.c)
    )
    return _first_record(db.execute(stmt))
