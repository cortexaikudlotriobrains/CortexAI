"""Shared additive grant schema for the existing isolated billing fixtures."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Uuid,
    func,
)


def add_subscription_grant_schema(metadata):
    grants = Table(
        "subscription_grants",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("plan_code", String, nullable=False),
        Column("status", String, nullable=False, server_default="active"),
        Column("starts_at", DateTime(timezone=True), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("granted_by", String, nullable=False),
        Column("reason", String, nullable=False),
        Column("revoked_at", DateTime(timezone=True)),
        Column("revoked_by", String),
        Column("revocation_reason", String),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        CheckConstraint("plan_code IN ('plus', 'pro')"),
        CheckConstraint("status IN ('active', 'expired', 'revoked')"),
        CheckConstraint("expires_at > starts_at"),
        CheckConstraint("trim(granted_by) <> '' AND trim(reason) <> ''"),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND trim(revoked_by) <> '' AND revocation_reason IS NOT NULL "
            "AND trim(revocation_reason) <> '') OR "
            "(status <> 'revoked' AND revoked_at IS NULL AND revoked_by IS NULL "
            "AND revocation_reason IS NULL)"
        ),
    )
    Index(
        "uq_subscription_grants_one_active_per_account",
        grants.c.billing_account_id,
        unique=True,
        sqlite_where=grants.c.status == "active",
    )
    periods = metadata.tables["usage_periods"]
    periods.append_column(
        Column("subscription_grant_id", Uuid, ForeignKey("subscription_grants.id"))
    )
    periods.append_constraint(
        CheckConstraint("subscription_id IS NULL OR subscription_grant_id IS NULL")
    )
    for index in list(periods.indexes):
        if index.name == "uq_usage_period_account_start":
            periods.indexes.remove(index)
    Index(
        "uq_usage_period_account_start",
        periods.c.billing_account_id,
        periods.c.starts_at,
        unique=True,
        sqlite_where=periods.c.subscription_grant_id.is_(None),
    )
    Index(
        "uq_usage_period_grant_start",
        periods.c.subscription_grant_id,
        periods.c.starts_at,
        unique=True,
        sqlite_where=periods.c.subscription_grant_id.is_not(None),
    )
    return grants
