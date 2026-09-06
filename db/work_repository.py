"""Short-transaction persistence for CortexAI Work.

Provider, MCP, OAuth, and object-storage I/O must happen outside these helpers.
Every user-facing lookup joins through the owning Work session so an opaque UUID
is never sufficient authorization.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import UUID

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from db.tables import get_table

ACTIVE_RUN_STATUSES = frozenset({"created", "planning", "running", "waiting_for_approval"})
TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "budget_exhausted", "output_limit_reached"}
)


def _row(result: Any) -> dict[str, Any] | None:
    return dict(result._mapping) if result is not None else None


def create_work_session(
    db: Session,
    *,
    session_id: UUID,
    user_id: UUID,
    agent_provider: str,
    provider_agent_id: str | None = None,
    provider_environment_id: str | None = None,
    default_tool_policy: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    table = get_table("work_sessions")
    result = db.execute(
        insert(table)
        .values(
            session_id=session_id,
            user_id=user_id,
            status="idle",
            agent_provider=agent_provider,
            provider_agent_id=provider_agent_id,
            provider_environment_id=provider_environment_id,
            default_tool_policy=dict(default_tool_policy or {}),
            metadata=dict(metadata or {}),
        )
        .returning(table)
    ).first()
    return _row(result) or {}


def get_work_session(db: Session, work_session_id: UUID) -> dict[str, Any] | None:
    table = get_table("work_sessions")
    return _row(db.execute(select(table).where(table.c.id == work_session_id)).first())


def get_work_session_for_user(
    db: Session, work_session_id: UUID, user_id: UUID
) -> dict[str, Any] | None:
    table = get_table("work_sessions")
    return _row(
        db.execute(
            select(table).where(
                table.c.id == work_session_id,
                table.c.user_id == user_id,
            )
        ).first()
    )


def list_work_sessions_for_user(
    db: Session, user_id: UUID, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    work_sessions = get_table("work_sessions")
    sessions = get_table("sessions")
    latest_status = (
        select(get_table("work_runs").c.status)
        .where(get_table("work_runs").c.work_session_id == work_sessions.c.id)
        .order_by(get_table("work_runs").c.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        select(
            work_sessions,
            sessions.c.title.label("title"),
            sessions.c.created_at.label("session_created_at"),
            latest_status.label("latest_run_status"),
        )
        .join(sessions, sessions.c.id == work_sessions.c.session_id)
        .where(work_sessions.c.user_id == user_id)
        .order_by(work_sessions.c.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def update_work_session(
    db: Session,
    work_session_id: UUID,
    *,
    status: str | None = None,
    provider_session_id: str | None = None,
    default_tool_policy: Mapping[str, object] | None = None,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, Any] | None:
    table = get_table("work_sessions")
    values: dict[str, object] = {"updated_at": func.now()}
    if status is not None:
        values["status"] = status
    if provider_session_id is not None:
        values["provider_session_id"] = provider_session_id
    if default_tool_policy is not None:
        values["default_tool_policy"] = dict(default_tool_policy)
    if metadata is not None:
        values["metadata"] = dict(metadata)
    result = db.execute(
        update(table).where(table.c.id == work_session_id).values(**values).returning(table)
    ).first()
    return _row(result)


def create_work_run(
    db: Session,
    *,
    work_session_id: UUID,
    request_id: str,
    instruction: str,
    provider: str,
    max_credit_budget: int,
    max_output_tokens: int,
    reserved_credits: int = 0,
    billing_reservation_id: UUID | None = None,
    configuration_snapshot: Mapping[str, object] | None = None,
) -> tuple[dict[str, Any], bool]:
    table = get_table("work_runs")
    values = {
        "work_session_id": work_session_id,
        "request_id": request_id,
        "instruction": instruction,
        "status": "created",
        "provider": provider,
        "max_credit_budget": max_credit_budget,
        "max_output_tokens": max_output_tokens,
        "reserved_credits": reserved_credits,
        "billing_reservation_id": billing_reservation_id,
        "configuration_snapshot": dict(configuration_snapshot or {}),
    }
    result = db.execute(
        pg_insert(table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=[table.c.work_session_id, table.c.request_id])
        .returning(table)
    ).first()
    if result is not None:
        return dict(result._mapping), True
    existing = db.execute(
        select(table).where(
            table.c.work_session_id == work_session_id,
            table.c.request_id == request_id,
        )
    ).first()
    if existing is None:  # pragma: no cover - defensive against impossible concurrent delete
        raise RuntimeError("Work run idempotency lookup failed")
    return dict(existing._mapping), False


def get_work_run(db: Session, work_run_id: UUID) -> dict[str, Any] | None:
    table = get_table("work_runs")
    return _row(db.execute(select(table).where(table.c.id == work_run_id)).first())


def get_work_run_for_user(db: Session, work_run_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(runs)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(runs.c.id == work_run_id, work_sessions.c.user_id == user_id)
    )
    return _row(db.execute(stmt).first())


def get_work_run_by_request_for_user(
    db: Session, request_id: str, user_id: UUID
) -> dict[str, Any] | None:
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    return _row(
        db.execute(
            select(runs)
            .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
            .where(runs.c.request_id == request_id, work_sessions.c.user_id == user_id)
        ).first()
    )


def list_work_runs_for_session(
    db: Session, work_session_id: UUID, user_id: UUID
) -> list[dict[str, Any]]:
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(runs)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(
            runs.c.work_session_id == work_session_id,
            work_sessions.c.user_id == user_id,
        )
        .order_by(runs.c.created_at)
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def count_active_work_runs(db: Session, user_id: UUID) -> int:
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(func.count())
        .select_from(runs.join(work_sessions, work_sessions.c.id == runs.c.work_session_id))
        .where(
            work_sessions.c.user_id == user_id,
            runs.c.status.in_(ACTIVE_RUN_STATUSES),
        )
    )
    return int(db.execute(stmt).scalar_one())


def list_reconcilable_work_runs(db: Session, *, limit: int = 100) -> list[dict[str, Any]]:
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(runs.c.id, work_sessions.c.user_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(
            runs.c.status.in_(ACTIVE_RUN_STATUSES),
            or_(
                runs.c.provider_run_id.is_not(None),
                work_sessions.c.provider_session_id.is_not(None),
            ),
        )
        .order_by(runs.c.updated_at)
        .limit(max(1, limit))
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def update_work_run(
    db: Session,
    work_run_id: UUID,
    *,
    status: str | None = None,
    provider_run_id: str | None = None,
    reserved_credits: int | None = None,
    actual_credits: int | None = None,
    actual_output_tokens: int | None = None,
    provider_model_id: str | None = None,
    billing_model_id: str | None = None,
    billing_model_source: str | None = None,
    provider_agent_id: str | None = None,
    provider_agent_version: int | None = None,
    output_finalize_requested: bool = False,
    output_limit_interrupt_requested: bool = False,
    billing_reservation_id: UUID | None = None,
    configuration_snapshot: Mapping[str, object] | None = None,
    usage_snapshot: Mapping[str, object] | None = None,
    provider_cost_snapshot: Mapping[str, object] | None = None,
    provider_cursor: str | None = None,
    stop_reason: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    started: bool = False,
    completed: bool = False,
) -> dict[str, Any] | None:
    table = get_table("work_runs")
    values: dict[str, object] = {"updated_at": func.now()}
    for key, value in {
        "status": status,
        "provider_run_id": provider_run_id,
        "reserved_credits": reserved_credits,
        "actual_credits": actual_credits,
        "actual_output_tokens": actual_output_tokens,
        "provider_model_id": provider_model_id,
        "billing_model_id": billing_model_id,
        "billing_model_source": billing_model_source,
        "provider_agent_id": provider_agent_id,
        "provider_agent_version": provider_agent_version,
        "billing_reservation_id": billing_reservation_id,
        "configuration_snapshot": (
            dict(configuration_snapshot) if configuration_snapshot is not None else None
        ),
        "usage_snapshot": dict(usage_snapshot) if usage_snapshot is not None else None,
        "provider_cost_snapshot": (
            dict(provider_cost_snapshot) if provider_cost_snapshot is not None else None
        ),
        "provider_cursor": provider_cursor,
        "stop_reason": stop_reason,
        "error_code": error_code,
        "error_message": error_message,
    }.items():
        if value is not None:
            values[key] = value
    if started:
        values["started_at"] = func.coalesce(table.c.started_at, func.now())
    if completed:
        values["completed_at"] = func.coalesce(table.c.completed_at, func.now())
    if output_finalize_requested:
        values["output_finalize_requested_at"] = func.coalesce(
            table.c.output_finalize_requested_at, func.now()
        )
    if output_limit_interrupt_requested:
        values["output_limit_interrupt_requested_at"] = func.coalesce(
            table.c.output_limit_interrupt_requested_at, func.now()
        )
    result = db.execute(
        update(table).where(table.c.id == work_run_id).values(**values).returning(table)
    ).first()
    return _row(result)


def clear_work_output_interrupt_request(db: Session, work_run_id: UUID) -> None:
    table = get_table("work_runs")
    db.execute(
        update(table)
        .where(table.c.id == work_run_id)
        .values(output_limit_interrupt_requested_at=None, updated_at=func.now())
    )


def clear_work_output_finalize_request(db: Session, work_run_id: UUID) -> None:
    table = get_table("work_runs")
    db.execute(
        update(table)
        .where(table.c.id == work_run_id)
        .values(output_finalize_requested_at=None, updated_at=func.now())
    )


def append_work_event(
    db: Session,
    *,
    work_run_id: UUID,
    event_type: str,
    display_message: str | None = None,
    payload: Mapping[str, object] | None = None,
    provider_event_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append one ordered event, returning an existing provider duplicate unchanged."""
    runs = get_table("work_runs")
    events = get_table("work_events")
    run = db.execute(
        select(runs.c.next_event_sequence).where(runs.c.id == work_run_id).with_for_update()
    ).first()
    if run is None:
        raise KeyError("work_run_not_found")
    if provider_event_id:
        existing = db.execute(
            select(events).where(
                events.c.work_run_id == work_run_id,
                events.c.provider_event_id == provider_event_id,
            )
        ).first()
        if existing is not None:
            return dict(existing._mapping), False
    sequence = int(run.next_event_sequence)
    inserted = db.execute(
        insert(events)
        .values(
            work_run_id=work_run_id,
            sequence_number=sequence,
            event_type=event_type,
            display_message=display_message,
            payload=dict(payload or {}),
            provider_event_id=provider_event_id,
        )
        .returning(events)
    ).first()
    if inserted is None:  # pragma: no cover - INSERT RETURNING contract
        raise RuntimeError("Work event insert returned no row")
    db.execute(
        update(runs)
        .where(runs.c.id == work_run_id)
        .values(next_event_sequence=sequence + 1, updated_at=func.now())
    )
    return dict(inserted._mapping), True


def list_work_events_after_sequence(
    db: Session,
    work_run_id: UUID,
    user_id: UUID,
    *,
    after_sequence: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    events = get_table("work_events")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(events)
        .join(runs, runs.c.id == events.c.work_run_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(
            events.c.work_run_id == work_run_id,
            events.c.sequence_number > after_sequence,
            work_sessions.c.user_id == user_id,
        )
        .order_by(events.c.sequence_number)
        .limit(limit)
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def get_latest_work_sequence(db: Session, work_run_id: UUID, user_id: UUID) -> int:
    events = list_work_events_after_sequence(
        db, work_run_id, user_id, after_sequence=0, limit=1_000_000
    )
    return int(events[-1]["sequence_number"]) if events else 0


def attach_work_file(
    db: Session,
    *,
    work_run_id: UUID,
    user_id: UUID,
    file_id: UUID,
    role: str,
    source: str,
    provider_file_id: str | None = None,
    artifact_type: str | None = None,
    metadata: Mapping[str, object] | None = None,
) -> tuple[dict[str, Any], bool]:
    files = get_table("uploaded_files")
    run_files = get_table("work_run_files")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    owned = db.execute(
        select(files.c.id)
        .select_from(
            files.join(work_sessions, work_sessions.c.user_id == files.c.user_id).join(
                runs, runs.c.work_session_id == work_sessions.c.id
            )
        )
        .where(
            files.c.id == file_id,
            files.c.user_id == user_id,
            files.c.status == "ready",
            runs.c.id == work_run_id,
            work_sessions.c.user_id == user_id,
        )
    ).scalar_one_or_none()
    if owned is None:
        raise PermissionError("work_file_not_owned_or_ready")
    result = db.execute(
        pg_insert(run_files)
        .values(
            work_run_id=work_run_id,
            file_id=file_id,
            role=role,
            source=source,
            provider_file_id=provider_file_id,
            artifact_type=artifact_type,
            metadata=dict(metadata or {}),
        )
        .on_conflict_do_nothing(
            index_elements=[run_files.c.work_run_id, run_files.c.file_id, run_files.c.role]
        )
        .returning(run_files)
    ).first()
    if result is not None:
        return dict(result._mapping), True
    existing = db.execute(
        select(run_files).where(
            run_files.c.work_run_id == work_run_id,
            run_files.c.file_id == file_id,
            run_files.c.role == role,
        )
    ).first()
    if existing is None:  # pragma: no cover - conflict target disappeared
        raise RuntimeError("Work run-file idempotency lookup failed")
    return dict(existing._mapping), False


def list_work_run_files(
    db: Session, work_run_id: UUID, user_id: UUID, *, role: str | None = None
) -> list[dict[str, Any]]:
    run_files = get_table("work_run_files")
    files = get_table("uploaded_files")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    conditions = [
        run_files.c.work_run_id == work_run_id,
        work_sessions.c.user_id == user_id,
        files.c.user_id == user_id,
    ]
    if role:
        conditions.append(run_files.c.role == role)
    stmt = (
        select(
            run_files,
            files.c.original_filename,
            files.c.mime_type,
            files.c.size_bytes,
            files.c.status.label("file_status"),
        )
        .join(files, files.c.id == run_files.c.file_id)
        .join(runs, runs.c.id == run_files.c.work_run_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(*conditions)
        .order_by(run_files.c.created_at)
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def list_work_session_files(
    db: Session,
    work_session_id: UUID,
    user_id: UUID,
    *,
    role: str | None = None,
) -> list[dict[str, Any]]:
    """List persisted Work files for provider-session recovery.

    A file can appear in more than one run. The most recent attachment wins so
    callers can remount one stable resource per Cortex-owned file.
    """
    run_files = get_table("work_run_files")
    files = get_table("uploaded_files")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    conditions = [
        runs.c.work_session_id == work_session_id,
        work_sessions.c.user_id == user_id,
        files.c.user_id == user_id,
    ]
    if role:
        conditions.append(run_files.c.role == role)
    stmt = (
        select(
            run_files,
            files.c.original_filename,
            files.c.mime_type,
            files.c.size_bytes,
            files.c.storage_bucket,
            files.c.storage_key,
        )
        .join(files, files.c.id == run_files.c.file_id)
        .join(runs, runs.c.id == run_files.c.work_run_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(*conditions)
        .order_by(run_files.c.created_at.desc())
    )
    by_file: dict[UUID, dict[str, Any]] = {}
    for item in db.execute(stmt):
        row = dict(item._mapping)
        by_file.setdefault(row["file_id"], row)
    return list(by_file.values())


def update_work_run_file_provider_id(
    db: Session,
    *,
    work_run_id: UUID,
    file_id: UUID,
    role: str,
    provider_file_id: str,
    metadata: Mapping[str, object] | None = None,
) -> dict[str, Any] | None:
    table = get_table("work_run_files")
    values: dict[str, object] = {"provider_file_id": provider_file_id}
    if metadata is not None:
        values["metadata"] = dict(metadata)
    return _row(
        db.execute(
            update(table)
            .where(
                table.c.work_run_id == work_run_id,
                table.c.file_id == file_id,
                table.c.role == role,
            )
            .values(**values)
            .returning(table)
        ).first()
    )


def find_work_artifact_by_provider_file(
    db: Session, *, user_id: UUID, provider_file_id: str
) -> dict[str, Any] | None:
    run_files = get_table("work_run_files")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    return _row(
        db.execute(
            select(run_files)
            .join(runs, runs.c.id == run_files.c.work_run_id)
            .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
            .where(
                run_files.c.provider_file_id == provider_file_id,
                run_files.c.role == "artifact",
                work_sessions.c.user_id == user_id,
            )
            .limit(1)
        ).first()
    )


def find_tool_call_by_provider_id(
    db: Session, *, work_run_id: UUID, provider_call_id: str
) -> dict[str, Any] | None:
    table = get_table("work_tool_calls")
    return _row(
        db.execute(
            select(table).where(
                table.c.work_run_id == work_run_id,
                table.c.provider_call_id == provider_call_id,
            )
        ).first()
    )


def get_tool_call(db: Session, tool_call_id: UUID) -> dict[str, Any] | None:
    table = get_table("work_tool_calls")
    return _row(db.execute(select(table).where(table.c.id == tool_call_id)).first())


def list_pending_approvals_for_run(
    db: Session, work_run_id: UUID, user_id: UUID
) -> list[dict[str, Any]]:
    approvals = get_table("work_approvals")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(approvals)
        .join(runs, runs.c.id == approvals.c.work_run_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(
            approvals.c.work_run_id == work_run_id,
            approvals.c.status == "pending",
            work_sessions.c.user_id == user_id,
        )
        .order_by(approvals.c.requested_at)
    )
    return [dict(item._mapping) for item in db.execute(stmt)]


def create_tool_connection(
    db: Session,
    *,
    user_id: UUID,
    connector_key: str,
    connection_type: str,
    display_name: str,
    server_url: str | None,
    auth_type: str,
    credential_reference: str | None = None,
    provider_vault_id: str | None = None,
    status: str = "pending",
    granted_scopes: Sequence[str] = (),
    metadata: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    table = get_table("tool_connections")
    result = db.execute(
        insert(table)
        .values(
            user_id=user_id,
            connector_key=connector_key,
            connection_type=connection_type,
            display_name=display_name,
            server_url=server_url,
            auth_type=auth_type,
            credential_reference=credential_reference,
            provider_vault_id=provider_vault_id,
            status=status,
            granted_scopes=list(granted_scopes),
            metadata=dict(metadata or {}),
        )
        .returning(table)
    ).first()
    return _row(result) or {}


def get_tool_connection_for_user(
    db: Session, connection_id: UUID, user_id: UUID
) -> dict[str, Any] | None:
    table = get_table("tool_connections")
    return _row(
        db.execute(
            select(table).where(table.c.id == connection_id, table.c.user_id == user_id)
        ).first()
    )


def list_tool_connections_for_user(db: Session, user_id: UUID) -> list[dict[str, Any]]:
    table = get_table("tool_connections")
    return [
        dict(item._mapping)
        for item in db.execute(
            select(table).where(table.c.user_id == user_id).order_by(table.c.updated_at.desc())
        )
    ]


def count_tool_connections(db: Session, user_id: UUID) -> int:
    table = get_table("tool_connections")
    return int(
        db.execute(
            select(func.count())
            .select_from(table)
            .where(
                table.c.user_id == user_id,
                table.c.status != "disabled",
            )
        ).scalar_one()
    )


def update_tool_connection(
    db: Session,
    connection_id: UUID,
    user_id: UUID,
    *,
    status: str,
    metadata: Mapping[str, object] | None = None,
    verified: bool = False,
) -> dict[str, Any] | None:
    table = get_table("tool_connections")
    values: dict[str, object] = {"status": status, "updated_at": func.now()}
    if metadata is not None:
        values["metadata"] = dict(metadata)
    if verified:
        values["last_verified_at"] = func.now()
    return _row(
        db.execute(
            update(table)
            .where(table.c.id == connection_id, table.c.user_id == user_id)
            .values(**values)
            .returning(table)
        ).first()
    )


def disable_tool_connection(db: Session, connection_id: UUID, user_id: UUID) -> bool:
    return update_tool_connection(db, connection_id, user_id, status="disabled") is not None


def snapshot_run_connection(
    db: Session,
    *,
    work_run_id: UUID,
    connection_id: UUID,
    configuration_snapshot: Mapping[str, object],
) -> None:
    table = get_table("work_run_connections")
    db.execute(
        pg_insert(table)
        .values(
            work_run_id=work_run_id,
            connection_id=connection_id,
            configuration_snapshot=dict(configuration_snapshot),
        )
        .on_conflict_do_nothing(index_elements=[table.c.work_run_id, table.c.connection_id])
    )


def list_run_connection_snapshots(db: Session, work_run_id: UUID) -> list[dict[str, Any]]:
    snapshots = get_table("work_run_connections")
    rows = db.execute(select(snapshots).where(snapshots.c.work_run_id == work_run_id))
    return [dict(item._mapping) for item in rows]


def create_tool_call_and_approval(
    db: Session,
    *,
    work_run_id: UUID,
    provider_call_id: str | None,
    connection_id: UUID | None,
    tool_source: str,
    tool_name: str,
    action_class: str,
    request_summary: Mapping[str, object],
    description: str,
    request_payload: Mapping[str, object],
) -> tuple[dict[str, Any], dict[str, Any]]:
    call_row = create_work_tool_call(
        db,
        work_run_id=work_run_id,
        provider_call_id=provider_call_id,
        connection_id=connection_id,
        tool_source=tool_source,
        tool_name=tool_name,
        action_class=action_class,
        request_summary=request_summary,
        status="awaiting_approval",
    )
    approval = create_approval_for_tool_call(
        db,
        work_run_id=work_run_id,
        tool_call_id=call_row["id"],
        connection_id=connection_id,
        action_class=action_class,
        tool_name=tool_name,
        description=description,
        request_payload=request_payload,
    )
    return call_row, approval


def create_work_tool_call(
    db: Session,
    *,
    work_run_id: UUID,
    provider_call_id: str | None,
    connection_id: UUID | None,
    tool_source: str,
    tool_name: str,
    action_class: str,
    request_summary: Mapping[str, object],
    status: str = "running",
) -> dict[str, Any]:
    calls = get_table("work_tool_calls")
    statement = pg_insert(calls).values(
        work_run_id=work_run_id,
        provider_call_id=provider_call_id,
        connection_id=connection_id,
        tool_source=tool_source,
        tool_name=tool_name,
        action_class=action_class,
        status=status,
        request_summary=dict(request_summary),
        started_at=func.now(),
    )
    if provider_call_id:
        statement = statement.on_conflict_do_nothing(
            index_elements=[calls.c.work_run_id, calls.c.provider_call_id],
            index_where=calls.c.provider_call_id.is_not(None),
        )
    call = db.execute(statement.returning(calls)).first()
    if call is not None:
        return dict(call._mapping)
    existing = find_tool_call_by_provider_id(
        db,
        work_run_id=work_run_id,
        provider_call_id=str(provider_call_id),
    )
    if existing is None:
        raise RuntimeError("Work tool-call idempotency lookup failed")
    return existing


def create_approval_for_tool_call(
    db: Session,
    *,
    work_run_id: UUID,
    tool_call_id: UUID,
    connection_id: UUID | None,
    action_class: str,
    tool_name: str,
    description: str,
    request_payload: Mapping[str, object],
) -> dict[str, Any]:
    calls = get_table("work_tool_calls")
    approvals = get_table("work_approvals")
    db.execute(update(calls).where(calls.c.id == tool_call_id).values(status="awaiting_approval"))
    approval = db.execute(
        insert(approvals)
        .values(
            work_run_id=work_run_id,
            tool_call_id=tool_call_id,
            connection_id=connection_id,
            action_type=action_class,
            tool_name=tool_name,
            description=description,
            request_payload=dict(request_payload),
        )
        .returning(approvals)
    ).first()
    return _row(approval) or {}


def update_work_tool_call_status_by_provider_id(
    db: Session,
    *,
    work_run_id: UUID,
    provider_call_id: str,
    status: str,
) -> None:
    calls = get_table("work_tool_calls")
    values: dict[str, object] = {"status": status}
    if status in {"succeeded", "failed", "denied"}:
        values["completed_at"] = func.now()
    db.execute(
        update(calls)
        .where(
            calls.c.work_run_id == work_run_id,
            calls.c.provider_call_id == provider_call_id,
        )
        .values(**values)
    )


def get_approval_for_user(
    db: Session, approval_id: UUID, user_id: UUID, *, lock: bool = False
) -> dict[str, Any] | None:
    approvals = get_table("work_approvals")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    stmt = (
        select(approvals)
        .join(runs, runs.c.id == approvals.c.work_run_id)
        .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
        .where(approvals.c.id == approval_id, work_sessions.c.user_id == user_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=approvals)
    return _row(db.execute(stmt).first())


def decide_approval(
    db: Session, *, approval_id: UUID, user_id: UUID, decision: str
) -> dict[str, Any] | None:
    approval = get_approval_for_user(db, approval_id, user_id, lock=True)
    if approval is None:
        return None
    if approval["status"] != "pending":
        raise ValueError("approval_already_resolved")
    approvals = get_table("work_approvals")
    calls = get_table("work_tool_calls")
    updated = db.execute(
        update(approvals)
        .where(approvals.c.id == approval_id, approvals.c.status == "pending")
        .values(status=decision, decided_at=func.now(), decided_by=user_id)
        .returning(approvals)
    ).first()
    db.execute(
        update(calls)
        .where(calls.c.id == approval["tool_call_id"])
        .values(status="running" if decision == "approved" else "denied")
    )
    return _row(updated)


def expire_pending_approvals_for_run(
    db: Session,
    *,
    work_run_id: UUID,
    user_id: UUID,
    requested_before: datetime,
) -> list[dict[str, Any]]:
    """Claim stale approvals for denial without performing provider I/O."""
    approvals = get_table("work_approvals")
    calls = get_table("work_tool_calls")
    runs = get_table("work_runs")
    work_sessions = get_table("work_sessions")
    pending = [
        dict(row._mapping)
        for row in db.execute(
            select(approvals)
            .join(runs, runs.c.id == approvals.c.work_run_id)
            .join(work_sessions, work_sessions.c.id == runs.c.work_session_id)
            .where(
                approvals.c.work_run_id == work_run_id,
                approvals.c.status == "pending",
                approvals.c.requested_at <= requested_before,
                work_sessions.c.user_id == user_id,
            )
            .order_by(approvals.c.requested_at)
            .with_for_update(of=approvals)
        )
    ]
    expired: list[dict[str, Any]] = []
    for approval in pending:
        updated = db.execute(
            update(approvals)
            .where(
                approvals.c.id == approval["id"],
                approvals.c.status == "pending",
            )
            .values(status="expired", decided_at=func.now(), decided_by=None)
            .returning(approvals)
        ).first()
        if updated is None:
            continue
        tool_call = get_tool_call(db, approval["tool_call_id"])
        db.execute(
            update(calls)
            .where(calls.c.id == approval["tool_call_id"])
            .values(status="denied", completed_at=func.now())
        )
        item = dict(updated._mapping)
        item["provider_call_id"] = tool_call.get("provider_call_id") if tool_call else None
        expired.append(item)
    return expired


def reopen_expired_approval_after_provider_failure(
    db: Session,
    *,
    approval_id: UUID,
) -> bool:
    """Restore an automatically expired approval if provider denial failed."""
    approvals = get_table("work_approvals")
    calls = get_table("work_tool_calls")
    row = db.execute(
        update(approvals)
        .where(
            approvals.c.id == approval_id,
            approvals.c.status == "expired",
            approvals.c.decided_by.is_(None),
        )
        .values(status="pending", decided_at=None)
        .returning(approvals.c.tool_call_id)
    ).first()
    if row is None:
        return False
    db.execute(
        update(calls)
        .where(calls.c.id == row[0])
        .values(status="awaiting_approval", completed_at=None)
    )
    return True


def reopen_approval_after_provider_failure(
    db: Session,
    *,
    approval_id: UUID,
    user_id: UUID,
    decision: str,
) -> bool:
    """Compensate a claimed decision only when provider confirmation failed."""
    approvals = get_table("work_approvals")
    calls = get_table("work_tool_calls")
    row = db.execute(
        update(approvals)
        .where(
            approvals.c.id == approval_id,
            approvals.c.status == decision,
            approvals.c.decided_by == user_id,
        )
        .values(status="pending", decided_at=None, decided_by=None)
        .returning(approvals.c.tool_call_id)
    ).first()
    if row is None:
        return False
    db.execute(update(calls).where(calls.c.id == row[0]).values(status="awaiting_approval"))
    return True


def create_oauth_state(
    db: Session,
    *,
    state_hash: str,
    user_id: UUID,
    connector_key: str,
    redirect_uri: str,
    expires_at: datetime,
) -> None:
    table = get_table("work_oauth_states")
    db.execute(
        insert(table).values(
            state_hash=state_hash,
            user_id=user_id,
            connector_key=connector_key,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )
    )


def consume_oauth_state(db: Session, *, state_hash: str, user_id: UUID) -> dict[str, Any] | None:
    table = get_table("work_oauth_states")
    now = datetime.now(UTC)
    row = db.execute(
        select(table)
        .where(
            table.c.state_hash == state_hash,
            table.c.user_id == user_id,
            table.c.consumed_at.is_(None),
            table.c.expires_at > now,
        )
        .with_for_update()
    ).first()
    if row is None:
        return None
    consumed = db.execute(
        update(table)
        .where(table.c.id == row.id, table.c.consumed_at.is_(None))
        .values(consumed_at=func.now())
        .returning(table)
    ).first()
    return _row(consumed)


def claim_sync_lease(
    db: Session,
    *,
    work_run_id: UUID,
    lease_owner: str,
    ttl_seconds: int = 30,
) -> bool:
    table = get_table("work_sync_leases")
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=max(5, ttl_seconds))
    result = db.execute(
        pg_insert(table)
        .values(
            work_run_id=work_run_id,
            lease_owner=lease_owner,
            lease_expires_at=expires,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[table.c.work_run_id],
            set_={
                "lease_owner": lease_owner,
                "lease_expires_at": expires,
                "updated_at": now,
            },
            where=or_(
                table.c.lease_expires_at <= now,
                table.c.lease_owner == lease_owner,
            ),
        )
        .returning(table.c.work_run_id)
    ).scalar_one_or_none()
    return result is not None


def release_sync_lease(db: Session, *, work_run_id: UUID, lease_owner: str) -> None:
    table = get_table("work_sync_leases")
    db.execute(
        delete(table).where(
            table.c.work_run_id == work_run_id,
            table.c.lease_owner == lease_owner,
        )
    )
