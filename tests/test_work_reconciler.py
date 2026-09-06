from __future__ import annotations

from contextlib import contextmanager
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient

from server.app import create_app
from server.work.config import WorkConfig
from server.work.config import load_work_config
from server.work.reconciler import run_reconciliation_cycle


def _config(*, enabled: bool = True, reconciler_enabled: bool = True) -> WorkConfig:
    return WorkConfig(
        enabled=enabled,
        mcp_enabled=False,
        action_tools_enabled=False,
        artifact_import_enabled=False,
        web_enabled=True,
        provider="fake",
        agent_id=None,
        environment_id=None,
        default_credit_budget=1_000_000,
        default_output_token_limit=40_000,
        output_finalize_token_threshold=32_000,
        reconciler_enabled=reconciler_enabled,
        reconciler_interval_seconds=2,
        event_sync_interval_seconds=2,
        sse_heartbeat_seconds=15,
        approval_timeout_seconds=3_600,
    )


def test_reconciliation_cycle_is_inert_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "server.work.reconciler.persistence_service.db_uow",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("database should not be opened")),
    )

    assert run_reconciliation_cycle(config=_config(enabled=False)) == {
        "examined": 0,
        "reconciled": 0,
        "errors": 0,
    }
    assert run_reconciliation_cycle(config=_config(reconciler_enabled=False)) == {
        "examined": 0,
        "reconciled": 0,
        "errors": 0,
    }


def test_reconciliation_cycle_processes_each_active_run_and_isolates_failures(monkeypatch):
    first_run = uuid4()
    first_user = uuid4()
    second_run = uuid4()
    second_user = uuid4()
    database = object()
    provider = object()
    calls: list[tuple[object, object, object]] = []

    @contextmanager
    def fake_db_uow(*, commit_on_success=True):
        assert commit_on_success is False
        yield database

    def fake_list(db, *, limit):
        assert db is database
        assert limit == 25
        return [
            {"id": first_run, "user_id": first_user},
            {"id": second_run, "user_id": second_user},
        ]

    def fake_reconcile(*, user_id, work_run_id, provider, config):
        calls.append((user_id, work_run_id, provider))
        assert config.reconciler_enabled is True
        if work_run_id == second_run:
            raise RuntimeError("provider temporarily unavailable")

    monkeypatch.setattr("server.work.reconciler.persistence_service.db_uow", fake_db_uow)
    monkeypatch.setattr("server.work.reconciler.repository.list_reconcilable_work_runs", fake_list)
    monkeypatch.setattr("server.work.reconciler.get_agent_provider", lambda: provider)
    monkeypatch.setattr("server.work.reconciler.reconcile_work_run", fake_reconcile)

    assert run_reconciliation_cycle(config=_config(), limit=25) == {
        "examined": 2,
        "reconciled": 1,
        "errors": 1,
    }
    assert calls == [
        (first_user, first_run, provider),
        (second_user, second_run, provider),
    ]


def test_postgres_lifespan_runs_work_reconciliation_without_a_browser(monkeypatch):
    from server.billing import schema_preflight as billing_schema_preflight
    from server.work import reconciler, schema_preflight as work_schema_preflight

    cycle_started = Event()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    monkeypatch.setenv("CORTEX_WORK_ENABLED", "true")
    monkeypatch.setenv("CORTEX_WORK_AGENT_PROVIDER", "fake")
    monkeypatch.setenv("CORTEX_WORK_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("CORTEX_WORK_RECONCILER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("ENABLE_ATTACHMENTS_CLEANUP_WORKER", "false")
    monkeypatch.setenv("ENABLE_BILLING_RESERVATION_CLEANUP_WORKER", "false")
    monkeypatch.setattr(billing_schema_preflight, "validate_billing_schema", lambda: None)
    monkeypatch.setattr(work_schema_preflight, "validate_work_schema", lambda: None)
    monkeypatch.setattr(
        reconciler,
        "run_reconciliation_cycle",
        lambda *, config: (cycle_started.set() or {"examined": 0, "reconciled": 0, "errors": 0}),
    )
    load_work_config.cache_clear()
    try:
        with TestClient(create_app()):
            assert cycle_started.wait(timeout=2)
    finally:
        load_work_config.cache_clear()
