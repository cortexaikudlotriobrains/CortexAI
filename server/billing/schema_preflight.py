"""Fail-fast validation for the subscription and unified-credit schema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from db.engine import get_engine
from db.tables import DB_SCHEMA


@dataclass
class BillingSchemaPreflightError(RuntimeError):
    """Raised when required subscription schema has not been migrated."""

    missing: tuple[str, ...]

    def __str__(self) -> str:
        details = ", ".join(self.missing)
        return (
            "Database schema is missing required CortexAI billing objects: "
            f"{details}. Apply db migrations in filename order before starting the API."
        )


REQUIRED_BILLING_SCHEMA: Mapping[str, frozenset[str]] = {
    "llm_requests": frozenset(
        {
            "id",
            "model",
            "requested_model",
            "generation_profile",
            "requested_max_output_tokens",
            "effective_max_output_tokens",
            "requested_reasoning_mode",
            "effective_reasoning_mode",
            "requested_reasoning_effort",
            "effective_reasoning_effort",
            "generation_policy_version",
        }
    ),
    "llm_responses": frozenset(
        {
            "id",
            "served_model",
            "pricing_model",
            "model_lifecycle_status",
            "replacement_model",
            "model_migration_reason",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "pricing_rule_applied",
            "pricing_version",
            "pricing_unknown",
            "pricing_snapshot",
            "completion_status",
            "stop_cause",
        }
    ),
    "billing_accounts": frozenset({"id", "owner_type", "owner_id"}),
    "subscriptions": frozenset(
        {
            "id",
            "billing_account_id",
            "plan_code",
            "status",
            "current_period_start",
            "current_period_end",
        }
    ),
    "subscription_grants": frozenset(
        {
            "id",
            "billing_account_id",
            "plan_code",
            "status",
            "starts_at",
            "expires_at",
            "granted_by",
            "reason",
            "revoked_at",
            "revoked_by",
            "revocation_reason",
            "created_at",
            "updated_at",
        }
    ),
    "usage_periods": frozenset(
        {
            "id",
            "billing_account_id",
            "subscription_id",
            "subscription_grant_id",
            "plan_code",
            "starts_at",
            "ends_at",
        }
    ),
    "usage_counters": frozenset(
        {"id", "usage_period_id", "meter_key", "used_quantity", "reserved_quantity"}
    ),
    "usage_reservations": frozenset(
        {
            "id",
            "billing_account_id",
            "usage_period_id",
            "request_id",
            "operation_type",
            "state",
            "requested_quantities",
            "settled_quantities",
            "last_activity_at",
        }
    ),
    "credit_transactions": frozenset(
        {
            "id",
            "billing_account_id",
            "usage_period_id",
            "reservation_id",
            "request_id",
            "operation_type",
            "item_index",
            "item_type",
            "normal_input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "normal_input_credits",
            "cached_input_credits",
            "cache_write_credits",
            "total_credits",
            "uncached_equivalent_credits",
            "cache_savings_credits",
            "metadata",
        }
    ),
    "billing_webhook_events": frozenset(
        {"id", "provider", "provider_event_id", "processing_status"}
    ),
    "cortex_analysis_runs": frozenset(
        {
            "id",
            "user_id",
            "session_id",
            "request_group_id",
            "source_fingerprint",
            "model",
            "analysis_policy_version",
            "recommended_answer",
            "created_at",
        }
    ),
    "cache_reuse_events": frozenset(
        {"id", "user_id", "request_id", "operation_type", "reused", "created_at"}
    ),
}


def validate_billing_schema(
    *,
    engine: Engine | None = None,
    schema: str | None = None,
) -> None:
    """Validate all tables/columns needed before any provider call can run."""

    target_engine = engine or get_engine()
    target_schema = schema or DB_SCHEMA
    inspector = inspect(target_engine)
    available_tables = set(inspector.get_table_names(schema=target_schema))
    missing: list[str] = []

    for table_name, required_columns in REQUIRED_BILLING_SCHEMA.items():
        if table_name not in available_tables:
            missing.append(f"table {target_schema}.{table_name}")
            continue
        available_columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name, schema=target_schema)
        }
        for column_name in sorted(required_columns - available_columns):
            missing.append(f"column {target_schema}.{table_name}.{column_name}")

    if missing:
        raise BillingSchemaPreflightError(tuple(missing))
