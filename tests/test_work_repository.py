from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    create_engine,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from db import work_repository as repository


@pytest.fixture()
def work_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()
    users = Table(
        "users",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
    )
    sessions = Table(
        "sessions",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
        Column("mode", String(16), nullable=False),
        Column("title", String(200)),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    work_sessions = Table(
        "work_sessions",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("session_id", Uuid, ForeignKey("sessions.id"), nullable=False),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
        Column("status", String(32), nullable=False),
        Column("agent_provider", String(64), nullable=False),
        Column("provider_agent_id", String(255)),
        Column("provider_environment_id", String(255)),
        Column("provider_session_id", String(255)),
        Column("default_tool_policy", JSON, nullable=False, default=dict),
        Column("metadata", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    work_runs = Table(
        "work_runs",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("work_session_id", Uuid, ForeignKey("work_sessions.id"), nullable=False),
        Column("request_id", String(255), nullable=False),
        Column("instruction", Text, nullable=False),
        Column("status", String(32), nullable=False),
        Column("provider", String(64), nullable=False),
        Column("provider_run_id", String(255)),
        Column("provider_cursor", String(255)),
        Column("max_credit_budget", BigInteger, nullable=False),
        Column("max_output_tokens", BigInteger, nullable=False, default=40_000),
        Column("actual_output_tokens", BigInteger, nullable=False, default=0),
        Column("reserved_credits", BigInteger, nullable=False, default=0),
        Column("actual_credits", BigInteger, nullable=False, default=0),
        Column("provider_model_id", String(255)),
        Column("billing_model_id", String(255)),
        Column("billing_model_source", String(255)),
        Column("provider_agent_id", String(255)),
        Column("provider_agent_version", BigInteger),
        Column("output_finalize_requested_at", DateTime(timezone=True)),
        Column("output_limit_interrupt_requested_at", DateTime(timezone=True)),
        Column("billing_reservation_id", Uuid),
        Column("configuration_snapshot", JSON, nullable=False, default=dict),
        Column("usage_snapshot", JSON, nullable=False, default=dict),
        Column("provider_cost_snapshot", JSON, nullable=False, default=dict),
        Column("next_event_sequence", BigInteger, nullable=False, default=1),
        Column("stop_reason", String(255)),
        Column("error_code", String(255)),
        Column("error_message", Text),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Index(
            "uq_work_runs_session_request",
            "work_session_id",
            "request_id",
            unique=True,
        ),
    )
    work_events = Table(
        "work_events",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), nullable=False),
        Column("sequence_number", BigInteger, nullable=False),
        Column("provider_event_id", String(255)),
        Column("event_type", String(64), nullable=False),
        Column("display_message", Text),
        Column("payload", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Index("uq_work_events_run_sequence", "work_run_id", "sequence_number", unique=True),
        Index("uq_work_events_provider", "work_run_id", "provider_event_id", unique=True),
    )
    uploaded_files = Table(
        "uploaded_files",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
        Column("original_filename", String(255), nullable=False),
        Column("mime_type", String(255), nullable=False),
        Column("size_bytes", BigInteger, nullable=False),
        Column("storage_bucket", String(255), nullable=False),
        Column("storage_key", String(1024), nullable=False),
        Column("status", String(32), nullable=False),
    )
    work_run_files = Table(
        "work_run_files",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), nullable=False),
        Column("file_id", Uuid, ForeignKey("uploaded_files.id"), nullable=False),
        Column("role", String(16), nullable=False),
        Column("source", String(16), nullable=False),
        Column("provider_file_id", String(255)),
        Column("artifact_type", String(255)),
        Column("metadata", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Index("uq_work_run_files_role", "work_run_id", "file_id", "role", unique=True),
    )
    tool_connections = Table(
        "tool_connections",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
        Column("connector_key", String(64), nullable=False),
        Column("connection_type", String(32), nullable=False),
        Column("display_name", String(120), nullable=False),
        Column("server_url", String(2048)),
        Column("auth_type", String(32), nullable=False),
        Column("credential_reference", String(1024)),
        Column("provider_vault_id", String(255)),
        Column("status", String(32), nullable=False),
        Column("granted_scopes", JSON, nullable=False, default=list),
        Column("metadata", JSON, nullable=False, default=dict),
        Column("last_verified_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    work_run_connections = Table(
        "work_run_connections",
        metadata,
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), primary_key=True),
        Column("connection_id", Uuid, ForeignKey("tool_connections.id"), primary_key=True),
        Column("configuration_snapshot", JSON, nullable=False, default=dict),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    work_tool_calls = Table(
        "work_tool_calls",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), nullable=False),
        Column("provider_call_id", String(255)),
        Column("connection_id", Uuid, ForeignKey("tool_connections.id")),
        Column("tool_source", String(32), nullable=False),
        Column("tool_name", String(255), nullable=False),
        Column("action_class", String(64), nullable=False),
        Column("status", String(32), nullable=False),
        Column("request_summary", JSON, nullable=False, default=dict),
        Column("started_at", DateTime(timezone=True)),
        Column("completed_at", DateTime(timezone=True)),
        Index(
            "uq_work_tool_calls_provider_call",
            "work_run_id",
            "provider_call_id",
            unique=True,
            sqlite_where=sql_text("provider_call_id IS NOT NULL"),
            postgresql_where=sql_text("provider_call_id IS NOT NULL"),
        ),
    )
    work_approvals = Table(
        "work_approvals",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), nullable=False),
        Column("tool_call_id", Uuid, ForeignKey("work_tool_calls.id"), nullable=False),
        Column("connection_id", Uuid, ForeignKey("tool_connections.id")),
        Column("action_type", String(64), nullable=False),
        Column("tool_name", String(255), nullable=False),
        Column("description", Text, nullable=False),
        Column("request_payload", JSON, nullable=False, default=dict),
        Column("status", String(32), nullable=False, default="pending"),
        Column("requested_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
        Column("decided_at", DateTime(timezone=True)),
        Column("decided_by", Uuid),
    )
    work_oauth_states = Table(
        "work_oauth_states",
        metadata,
        Column("id", Uuid, primary_key=True, default=uuid4),
        Column("state_hash", String(64), nullable=False, unique=True),
        Column("user_id", Uuid, ForeignKey("users.id"), nullable=False),
        Column("connector_key", String(64), nullable=False),
        Column("redirect_uri", String(500), nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("consumed_at", DateTime(timezone=True)),
        Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    )
    work_sync_leases = Table(
        "work_sync_leases",
        metadata,
        Column("work_run_id", Uuid, ForeignKey("work_runs.id"), primary_key=True),
        Column("lease_owner", String(255), nullable=False),
        Column("lease_expires_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    tables = {
        table.name: table
        for table in (
            users,
            sessions,
            work_sessions,
            work_runs,
            work_events,
            uploaded_files,
            work_run_files,
            tool_connections,
            work_run_connections,
            work_tool_calls,
            work_approvals,
            work_oauth_states,
            work_sync_leases,
        )
    }
    metadata.create_all(engine)
    monkeypatch.setattr(repository, "get_table", tables.__getitem__)
    monkeypatch.setattr(repository, "pg_insert", sqlite_insert)
    db = sessionmaker(bind=engine)()
    try:
        yield db, tables
    finally:
        db.close()
        engine.dispose()


def _owned_session(db, tables):
    user_id = uuid4()
    session_id = uuid4()
    db.execute(tables["users"].insert().values(id=user_id))
    db.execute(
        tables["sessions"]
        .insert()
        .values(
            id=session_id,
            user_id=user_id,
            mode="work",
            title="Quarterly analysis",
        )
    )
    work_session = repository.create_work_session(
        db,
        session_id=session_id,
        user_id=user_id,
        agent_provider="fake",
    )
    return user_id, work_session


def _run(db, work_session_id, request_id="work-request"):
    return repository.create_work_run(
        db,
        work_session_id=work_session_id,
        request_id=request_id,
        instruction="Prepare the report",
        provider="fake",
        max_credit_budget=100_000,
        max_output_tokens=40_000,
        reserved_credits=100_000,
    )[0]


def test_work_session_ownership_and_lifecycle_are_scoped(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    assert repository.get_work_session_for_user(db, session["id"], user_id) is not None
    assert repository.get_work_session_for_user(db, session["id"], uuid4()) is None
    updated = repository.update_work_session(
        db,
        session["id"],
        status="running",
        provider_session_id="provider-session",
    )
    assert updated["status"] == "running"
    assert updated["provider_session_id"] == "provider-session"


def test_duplicate_request_id_returns_the_original_run(work_db):
    db, tables = work_db
    _, session = _owned_session(db, tables)
    first, created = repository.create_work_run(
        db,
        work_session_id=session["id"],
        request_id="same-request",
        instruction="Prepare the report",
        provider="fake",
        max_credit_budget=25_000,
        max_output_tokens=40_000,
    )
    repeated, repeated_created = repository.create_work_run(
        db,
        work_session_id=session["id"],
        request_id="same-request",
        instruction="Prepare the report",
        provider="fake",
        max_credit_budget=25_000,
        max_output_tokens=40_000,
    )
    assert created is True
    assert repeated_created is False
    assert repeated["id"] == first["id"]


def test_request_idempotency_is_scoped_to_the_owned_work_session(work_db):
    db, tables = work_db
    _, first_session = _owned_session(db, tables)
    _, second_session = _owned_session(db, tables)
    first = _run(db, first_session["id"], request_id="shared-client-key")
    second = _run(db, second_session["id"], request_id="shared-client-key")
    assert first["id"] != second["id"]


def test_event_sequence_duplicate_provider_event_and_replay(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    first, created = repository.append_work_event(
        db,
        work_run_id=run["id"],
        event_type="planning",
        display_message="Creating a plan",
        provider_event_id="provider-1",
    )
    duplicate, duplicate_created = repository.append_work_event(
        db,
        work_run_id=run["id"],
        event_type="planning",
        display_message="Duplicate",
        provider_event_id="provider-1",
    )
    second, _ = repository.append_work_event(
        db,
        work_run_id=run["id"],
        event_type="progress",
        display_message="Reading files",
        provider_event_id="provider-2",
    )
    assert created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]
    assert [first["sequence_number"], second["sequence_number"]] == [1, 2]
    replay = repository.list_work_events_after_sequence(db, run["id"], user_id, after_sequence=1)
    assert [event["sequence_number"] for event in replay] == [2]
    assert (
        repository.list_work_events_after_sequence(db, run["id"], uuid4(), after_sequence=0) == []
    )


def test_run_lifecycle_and_active_limit_count(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    assert repository.count_active_work_runs(db, user_id) == 1
    updated = repository.update_work_run(
        db,
        run["id"],
        status="completed",
        actual_credits=12_345,
        completed=True,
    )
    assert updated["status"] == "completed"
    assert updated["actual_credits"] == 12_345
    assert updated["completed_at"] is not None
    assert repository.count_active_work_runs(db, user_id) == 0


def test_work_file_and_artifact_ownership_is_enforced(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    owned_file = uuid4()
    db.execute(
        tables["uploaded_files"]
        .insert()
        .values(
            id=owned_file,
            user_id=user_id,
            original_filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=1200,
            storage_bucket="private",
            storage_key=f"users/{user_id}/report.pdf",
            status="ready",
        )
    )
    attached, created = repository.attach_work_file(
        db,
        work_run_id=run["id"],
        user_id=user_id,
        file_id=owned_file,
        role="artifact",
        source="agent",
        provider_file_id="provider-file",
    )
    assert created is True
    assert attached["file_id"] == owned_file
    artifacts = repository.list_work_run_files(db, run["id"], user_id, role="artifact")
    assert [item["original_filename"] for item in artifacts] == ["report.pdf"]
    with pytest.raises(PermissionError):
        repository.attach_work_file(
            db,
            work_run_id=run["id"],
            user_id=uuid4(),
            file_id=owned_file,
            role="input",
            source="user",
        )


def test_tool_connection_ownership_and_limits_are_queryable(work_db):
    db, tables = work_db
    user_id, _ = _owned_session(db, tables)
    connection = repository.create_tool_connection(
        db,
        user_id=user_id,
        connector_key="custom_mcp",
        connection_type="mcp_remote",
        display_name="Research tools",
        server_url="https://mcp.example.com/mcp",
        auth_type="none",
        status="connected",
    )
    assert repository.get_tool_connection_for_user(db, connection["id"], user_id) is not None
    assert repository.get_tool_connection_for_user(db, connection["id"], uuid4()) is None
    assert repository.count_tool_connections(db, user_id) == 1
    assert repository.disable_tool_connection(db, connection["id"], user_id) is True
    assert repository.count_tool_connections(db, user_id) == 0


def test_run_connection_snapshot_and_tool_audit_are_idempotent(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    connection = repository.create_tool_connection(
        db,
        user_id=user_id,
        connector_key="github",
        connection_type="mcp_remote",
        display_name="GitHub",
        server_url="https://mcp.example.com/mcp",
        auth_type="oauth2",
        status="connected",
    )
    repository.snapshot_run_connection(
        db,
        work_run_id=run["id"],
        connection_id=connection["id"],
        configuration_snapshot={"connector_key": "github"},
    )
    assert (
        repository.list_run_connection_snapshots(db, run["id"])[0]["connection_id"]
        == connection["id"]
    )
    first = repository.create_work_tool_call(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-read-1",
        connection_id=connection["id"],
        tool_source="mcp",
        tool_name="list_issues",
        action_class="READ",
        request_summary={"input_keys": ["repo"]},
    )
    repeated = repository.create_work_tool_call(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-read-1",
        connection_id=connection["id"],
        tool_source="mcp",
        tool_name="list_issues",
        action_class="READ",
        request_summary={"input_keys": ["repo"]},
    )
    assert repeated["id"] == first["id"]
    repository.update_work_tool_call_status_by_provider_id(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-read-1",
        status="succeeded",
    )
    assert repository.get_tool_call(db, first["id"])["status"] == "succeeded"


def test_approval_audit_decision_and_replay_rejection(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    tool_call, approval = repository.create_tool_call_and_approval(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-tool-1",
        connection_id=None,
        tool_source="mcp",
        tool_name="send_email",
        action_class="EXTERNAL_COMMUNICATION",
        request_summary={"input_keys": ["to"]},
        description="Allow Cortex to send this email?",
        request_payload={"to": "person@example.com"},
    )
    assert (
        repository.find_tool_call_by_provider_id(
            db, work_run_id=run["id"], provider_call_id="provider-tool-1"
        )["id"]
        == tool_call["id"]
    )
    assert [
        item["id"] for item in repository.list_pending_approvals_for_run(db, run["id"], user_id)
    ] == [approval["id"]]
    decided = repository.decide_approval(
        db,
        approval_id=approval["id"],
        user_id=user_id,
        decision="approved",
    )
    assert decided["status"] == "approved"
    assert repository.get_tool_call(db, tool_call["id"])["status"] == "running"
    with pytest.raises(ValueError, match="already_resolved"):
        repository.decide_approval(
            db,
            approval_id=approval["id"],
            user_id=user_id,
            decision="denied",
        )


def test_approval_claim_can_be_reopened_only_after_matching_provider_failure(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    tool_call, approval = repository.create_tool_call_and_approval(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-tool-retry",
        connection_id=None,
        tool_source="mcp",
        tool_name="create_issue",
        action_class="WRITE",
        request_summary={},
        description="Allow issue creation?",
        request_payload={},
    )
    repository.decide_approval(
        db,
        approval_id=approval["id"],
        user_id=user_id,
        decision="approved",
    )
    assert not repository.reopen_approval_after_provider_failure(
        db,
        approval_id=approval["id"],
        user_id=uuid4(),
        decision="approved",
    )
    assert repository.reopen_approval_after_provider_failure(
        db,
        approval_id=approval["id"],
        user_id=user_id,
        decision="approved",
    )
    assert repository.get_approval_for_user(db, approval["id"], user_id)["status"] == "pending"
    assert repository.get_tool_call(db, tool_call["id"])["status"] == "awaiting_approval"


def test_stale_approval_expires_for_its_owner_and_reopens_on_provider_failure(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    tool_call, approval = repository.create_tool_call_and_approval(
        db,
        work_run_id=run["id"],
        provider_call_id="provider-tool-expiry",
        connection_id=None,
        tool_source="mcp",
        tool_name="send_email",
        action_class="EXTERNAL_COMMUNICATION",
        request_summary={},
        description="Allow email?",
        request_payload={},
    )
    db.execute(
        tables["work_approvals"]
        .update()
        .where(tables["work_approvals"].c.id == approval["id"])
        .values(requested_at=datetime.now(UTC) - timedelta(hours=2))
    )

    assert (
        repository.expire_pending_approvals_for_run(
            db,
            work_run_id=run["id"],
            user_id=uuid4(),
            requested_before=datetime.now(UTC),
        )
        == []
    )
    expired = repository.expire_pending_approvals_for_run(
        db,
        work_run_id=run["id"],
        user_id=user_id,
        requested_before=datetime.now(UTC) - timedelta(hours=1),
    )
    assert expired[0]["status"] == "expired"
    assert expired[0]["provider_call_id"] == "provider-tool-expiry"
    assert repository.get_tool_call(db, tool_call["id"])["status"] == "denied"
    assert repository.reopen_expired_approval_after_provider_failure(
        db,
        approval_id=approval["id"],
    )
    assert repository.get_approval_for_user(db, approval["id"], user_id)["status"] == "pending"
    assert repository.get_tool_call(db, tool_call["id"])["status"] == "awaiting_approval"


def test_oauth_state_is_owned_expiring_and_single_use(work_db):
    db, tables = work_db
    user_id, _ = _owned_session(db, tables)
    repository.create_oauth_state(
        db,
        state_hash="a" * 64,
        user_id=user_id,
        connector_key="github",
        redirect_uri="/work",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert repository.consume_oauth_state(db, state_hash="a" * 64, user_id=uuid4()) is None
    consumed = repository.consume_oauth_state(db, state_hash="a" * 64, user_id=user_id)
    assert consumed["redirect_uri"] == "/work"
    assert repository.consume_oauth_state(db, state_hash="a" * 64, user_id=user_id) is None


def test_sync_lease_allows_one_owner_until_release(work_db):
    db, tables = work_db
    _, session = _owned_session(db, tables)
    run = _run(db, session["id"])
    assert repository.claim_sync_lease(db, work_run_id=run["id"], lease_owner="worker-a") is True
    assert repository.claim_sync_lease(db, work_run_id=run["id"], lease_owner="worker-b") is False
    repository.release_sync_lease(db, work_run_id=run["id"], lease_owner="worker-a")
    assert repository.claim_sync_lease(db, work_run_id=run["id"], lease_owner="worker-b") is True


def test_failed_output_guardrail_requests_can_be_retried(work_db):
    db, tables = work_db
    _, session = _owned_session(db, tables)
    run = _run(db, session["id"], request_id="guardrail-retry")
    repository.update_work_run(
        db,
        run["id"],
        output_finalize_requested=True,
        output_limit_interrupt_requested=True,
    )

    repository.clear_work_output_finalize_request(db, run["id"])
    repository.clear_work_output_interrupt_request(db, run["id"])

    cleared = repository.get_work_run(db, run["id"])
    assert cleared is not None
    assert cleared["output_finalize_requested_at"] is None
    assert cleared["output_limit_interrupt_requested_at"] is None


def test_background_reconciliation_waits_for_a_persisted_provider_session(work_db):
    db, tables = work_db
    user_id, session = _owned_session(db, tables)
    run = _run(db, session["id"], request_id="reconciler-ready")
    assert repository.list_reconcilable_work_runs(db) == []

    repository.update_work_run(db, run["id"], provider_run_id="provider-session-1")

    assert repository.list_reconcilable_work_runs(db) == [{"id": run["id"], "user_id": user_id}]
