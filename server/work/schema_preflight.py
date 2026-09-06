"""Fail-fast Work schema validation when the feature is enabled."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from db.engine import get_engine
from db.tables import DB_SCHEMA


@dataclass
class WorkSchemaPreflightError(RuntimeError):
    missing: tuple[str, ...]

    def __str__(self) -> str:
        return "Database schema is missing required CortexAI Work objects: " + ", ".join(
            self.missing
        )


REQUIRED_WORK_SCHEMA: Mapping[str, frozenset[str]] = {
    "work_sessions": frozenset(
        {"id", "session_id", "user_id", "status", "agent_provider", "provider_session_id"}
    ),
    "work_runs": frozenset(
        {
            "id",
            "work_session_id",
            "request_id",
            "status",
            "max_credit_budget",
            "max_output_tokens",
            "actual_output_tokens",
            "provider_model_id",
            "billing_model_id",
            "billing_model_source",
            "provider_agent_id",
            "provider_agent_version",
            "output_finalize_requested_at",
            "output_limit_interrupt_requested_at",
            "billing_reservation_id",
            "next_event_sequence",
        }
    ),
    "work_events": frozenset(
        {"id", "work_run_id", "sequence_number", "event_type", "provider_event_id"}
    ),
    "work_run_files": frozenset({"id", "work_run_id", "file_id", "role", "provider_file_id"}),
    "tool_connections": frozenset(
        {"id", "user_id", "connector_key", "credential_reference", "provider_vault_id"}
    ),
    "work_run_connections": frozenset({"work_run_id", "connection_id", "configuration_snapshot"}),
    "work_tool_calls": frozenset(
        {"id", "work_run_id", "provider_call_id", "action_class", "status"}
    ),
    "work_approvals": frozenset({"id", "work_run_id", "tool_call_id", "status", "decided_by"}),
    "work_oauth_states": frozenset({"state_hash", "user_id", "expires_at", "consumed_at"}),
    "work_sync_leases": frozenset({"work_run_id", "lease_owner", "lease_expires_at"}),
}


def validate_work_schema(*, engine: Engine | None = None, schema: str | None = None) -> None:
    target = engine or get_engine()
    target_schema = schema or DB_SCHEMA
    inspector = inspect(target)
    tables = set(inspector.get_table_names(schema=target_schema))
    missing: list[str] = []
    for name, columns in REQUIRED_WORK_SCHEMA.items():
        if name not in tables:
            missing.append(f"table {target_schema}.{name}")
            continue
        available = {
            str(item["name"]) for item in inspector.get_columns(name, schema=target_schema)
        }
        missing.extend(
            f"column {target_schema}.{name}.{column}" for column in sorted(columns - available)
        )
    if missing:
        raise WorkSchemaPreflightError(tuple(missing))
