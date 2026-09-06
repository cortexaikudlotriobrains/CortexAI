from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
import pytest

from server.work.config import WorkConfig
from server.work import service
from server.work.billing import calculate_work_credit_usage
from server.work.prompt_policy import resolve_work_web_mode
from server.work.output_policy import resolve_output_guardrail
from server.work.provider import ProviderArtifact, ProviderEvent, ProviderSession


def _config(**overrides: Any) -> WorkConfig:
    base = WorkConfig(
        enabled=True,
        mcp_enabled=True,
        action_tools_enabled=True,
        artifact_import_enabled=True,
        web_enabled=True,
        provider="fake",
        agent_id="agent-1",
        environment_id="environment-1",
        default_credit_budget=1_000_000,
        default_output_token_limit=40_000,
        output_finalize_token_threshold=32_000,
        reconciler_enabled=True,
        reconciler_interval_seconds=2,
        event_sync_interval_seconds=1,
        sse_heartbeat_seconds=15,
        approval_timeout_seconds=3600,
    )
    return replace(base, **overrides)


def _effective(*, work: bool, active: int = 1, budget: int = 250_000, custom: bool = False):
    plan = SimpleNamespace(
        code="plus" if work else "free",
        display_name="Plus" if work else "Free",
        entitlements=SimpleNamespace(
            work_enabled=work,
            custom_mcp_enabled=custom,
        ),
        limits=SimpleNamespace(
            max_active_work_runs=active,
            max_work_credit_budget=budget,
            max_mcp_servers_per_run=3,
        ),
    )
    return SimpleNamespace(plan=plan)


def test_provider_session_reuse_keeps_context_when_only_web_or_mcp_tools_change():
    prior = {
        "connection_ids": ["connection-old"],
        "vault_ids": [],
        "web_enabled": True,
    }

    assert (
        service._can_reuse_provider_session(
            "session-1",
            prior,
            ["connection-new"],
            [],
        )
        is True
    )
    assert service._can_reuse_provider_session("", prior, [], []) is False
    assert service._can_reuse_provider_session("session-1", None, [], []) is False
    assert (
        service._can_reuse_provider_session(
            "session-1",
            prior,
            [],
            ["vault-new"],
        )
        is False
    )
    assert (
        service._can_reuse_provider_session(
            "session-1",
            {**prior, "vault_ids": ["vault-existing"]},
            [],
            ["vault-existing"],
        )
        is True
    )
    legacy = {"connection_ids": ["connection-old"], "web_enabled": False}
    assert (
        service._can_reuse_provider_session(
            "session-1",
            legacy,
            ["connection-old"],
            [],
        )
        is True
    )
    assert (
        service._can_reuse_provider_session(
            "session-1",
            legacy,
            [],
            [],
        )
        is False
    )


def test_artifact_import_uses_original_provider_session_and_isolates_item_failures(monkeypatch):
    user_id = uuid4()
    work_run_id = uuid4()
    work_session_id = uuid4()
    database = object()
    downloaded: list[str] = []
    stored: list[str] = []
    linked: list[str] = []

    @contextmanager
    def fake_db_uow(*, commit_on_success=True):
        del commit_on_success
        yield database

    class FakeStorage:
        key_prefix = "attachments"
        bucket = "test-bucket"

        def put_bytes(self, *, key, payload, content_type, metadata):
            del payload, content_type, metadata
            stored.append(key)

        def delete_object(self, *, key):
            stored.remove(key)

    class FakeProvider:
        def list_artifacts(self, session_id):
            assert session_id == "provider-session-original"
            return [
                ProviderArtifact(
                    id="input-1",
                    filename="input.txt",
                    mime_type="text/plain",
                    size_bytes=5,
                    downloadable=True,
                ),
                ProviderArtifact(
                    id="preview-1",
                    filename="preview.txt",
                    mime_type="text/plain",
                    size_bytes=5,
                    downloadable=False,
                ),
                ProviderArtifact(
                    id="broken-1",
                    filename="broken.md",
                    mime_type="text/markdown",
                    size_bytes=10,
                    downloadable=True,
                ),
                ProviderArtifact(
                    id="report-1",
                    filename="security-report.md",
                    mime_type="text/markdown",
                    size_bytes=15,
                    downloadable=True,
                ),
            ]

        def download_artifact(self, artifact_id):
            downloaded.append(artifact_id)
            if artifact_id == "broken-1":
                raise RuntimeError("provider file unavailable")
            return b"security report"

    monkeypatch.setattr(service.persistence_service, "db_uow", fake_db_uow)
    monkeypatch.setattr(
        service.repository,
        "get_work_run_for_user",
        lambda *_: {
            "id": work_run_id,
            "work_session_id": work_session_id,
            "provider_run_id": "provider-session-original",
        },
    )
    monkeypatch.setattr(
        service.repository,
        "get_work_session",
        lambda *_: {"id": work_session_id, "provider_session_id": "provider-session-new"},
    )
    monkeypatch.setattr(
        service,
        "resolve_effective_subscription",
        lambda *_: SimpleNamespace(
            plan=SimpleNamespace(limits=SimpleNamespace(max_file_bytes=1_000_000))
        ),
    )
    monkeypatch.setattr(
        service.repository,
        "list_work_session_files",
        lambda *_args, **_kwargs: [{"provider_file_id": "input-1"}],
    )
    monkeypatch.setattr(
        service.repository, "find_work_artifact_by_provider_file", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(service, "get_object_storage", lambda: FakeStorage())
    monkeypatch.setattr(service, "create_uploaded_file", lambda *_args, **_kwargs: None)

    def attach_work_file(_db, **kwargs):
        linked.append(kwargs["provider_file_id"])
        return ({"provider_file_id": kwargs["provider_file_id"]}, True)

    monkeypatch.setattr(service.repository, "attach_work_file", attach_work_file)
    monkeypatch.setattr(service.repository, "append_work_event", lambda *_args, **_kwargs: None)

    imported = service.import_work_artifacts(
        user_id=user_id,
        work_run_id=work_run_id,
        provider=FakeProvider(),
    )

    assert downloaded == ["broken-1", "report-1"]
    assert linked == ["report-1"]
    assert imported == [{"provider_file_id": "report-1"}]
    assert len(stored) == 1


def test_feature_off_is_not_exposed_as_a_partially_configured_route():
    with pytest.raises(HTTPException) as caught:
        service.require_work_enabled(_config(enabled=False))
    assert caught.value.status_code == 404
    assert caught.value.detail["code"] == "work_disabled"


def test_web_auto_enables_current_information_without_overriding_explicit_off():
    current = resolve_work_web_mode(
        "Build an itinerary with current opening hours, ticket prices, and source links",
        "auto",
    )
    assert current.effective_enabled is True
    assert current.reason == "current_information"
    evergreen = resolve_work_web_mode("Summarize the attached contract", "auto")
    assert evergreen.effective_enabled is False
    explicit_off = resolve_work_web_mode("What is the latest exchange rate?", "off")
    assert explicit_off.effective_enabled is False
    assert explicit_off.current_information is True


def test_provider_billing_identity_uses_the_resolved_session_model():
    identity = service._provider_billing_identity(
        ProviderSession(
            id="session-1",
            status="running",
            model_id="claude-haiku-4-5-20251001",
            agent_id="agent-1",
            agent_version=4,
        ),
        provider_name="anthropic_managed_agents",
    )
    assert identity["provider_model_id"] == "claude-haiku-4-5-20251001"
    assert identity["billing_model_id"] == "claude-haiku-4-5"
    assert identity["billing_model_source"] == ("anthropic_managed_agents_session_agent_snapshot")

    with pytest.raises(ValueError, match="multi-model"):
        service._provider_billing_identity(
            ProviderSession(
                id="session-2",
                status="running",
                model_id="claude-haiku-4-5",
                additional_model_ids=("claude-sonnet-5",),
            ),
            provider_name="anthropic_managed_agents",
        )


def test_output_guardrail_finalizes_then_interrupts_once_per_run():
    soft = resolve_output_guardrail(
        output_tokens=32_000,
        max_output_tokens=40_000,
        finalize_threshold=32_000,
        provider_interruptible=True,
        finalize_already_requested=False,
        interrupt_already_requested=False,
    )
    assert soft.finalize is True
    assert soft.interrupt is False
    hard = resolve_output_guardrail(
        output_tokens=40_100,
        max_output_tokens=40_000,
        finalize_threshold=32_000,
        provider_interruptible=True,
        finalize_already_requested=True,
        interrupt_already_requested=False,
    )
    assert hard.limit_reached is True
    assert hard.interrupt is True
    repeated = resolve_output_guardrail(
        output_tokens=41_000,
        max_output_tokens=40_000,
        finalize_threshold=32_000,
        provider_interruptible=True,
        finalize_already_requested=True,
        interrupt_already_requested=True,
    )
    assert repeated.interrupt is False


def test_free_is_denied_and_plus_budget_boundaries_are_enforced(monkeypatch):
    monkeypatch.setattr(service.repository, "count_active_work_runs", lambda *_: 0)
    monkeypatch.setattr(
        service, "resolve_effective_subscription", lambda *_: _effective(work=False)
    )
    with pytest.raises(HTTPException) as denied:
        service._plan_and_budget(object(), uuid4(), 25_000, _config())
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "work_not_in_plan"

    monkeypatch.setattr(service, "resolve_effective_subscription", lambda *_: _effective(work=True))
    _, default_budget = service._plan_and_budget(object(), uuid4(), None, _config())
    assert default_budget == 250_000
    with pytest.raises(HTTPException) as too_large:
        service._plan_and_budget(object(), uuid4(), 250_001, _config())
    assert too_large.value.status_code == 422
    assert too_large.value.detail["code"] == "invalid_work_credit_budget"


def test_active_run_limit_and_custom_mcp_entitlement(monkeypatch):
    monkeypatch.setattr(service, "resolve_effective_subscription", lambda *_: _effective(work=True))
    monkeypatch.setattr(service.repository, "count_active_work_runs", lambda *_: 1)
    with pytest.raises(HTTPException) as active_limit:
        service._plan_and_budget(object(), uuid4(), 25_000, _config())
    assert active_limit.value.status_code == 409
    assert active_limit.value.detail["code"] == "active_work_run_limit"

    connection_id = uuid4()
    monkeypatch.setattr(
        service.repository,
        "get_tool_connection_for_user",
        lambda *_: {
            "id": connection_id,
            "status": "connected",
            "connection_type": "mcp_remote",
            "connector_key": "custom_mcp",
        },
    )
    with pytest.raises(HTTPException) as custom_denied:
        service._load_connections(
            object(),
            user_id=uuid4(),
            connection_ids=[connection_id],
            plan=_effective(work=True, custom=False).plan,
            config=_config(),
        )
    assert custom_denied.value.status_code == 403
    allowed = service._load_connections(
        object(),
        user_id=uuid4(),
        connection_ids=[connection_id],
        plan=_effective(work=True, custom=True).plan,
        config=_config(),
    )
    assert allowed[0]["id"] == connection_id


def test_saved_write_policy_is_exactly_scoped_and_sensitive_actions_still_ask():
    connection_id = uuid4()
    session = {
        "default_tool_policy": {
            "allowed_write_tools": [
                {
                    "connection_id": str(connection_id),
                    "tool_name": "open_pull_request",
                }
            ]
        }
    }
    assert service._has_saved_write_grant(
        session,
        connection_id=connection_id,
        tool_name="open_pull_request",
    )
    assert not service._has_saved_write_grant(
        session,
        connection_id=uuid4(),
        tool_name="open_pull_request",
    )
    assert service.classify_action("open_pull_request") == "WRITE"
    assert service.classify_action("merge_pull_request") == "DESTRUCTIVE"


def test_provider_interrupt_and_followup_event_guards():
    assert service._provider_session_is_interruptible("running")
    assert service._provider_session_is_interruptible("rescheduling")
    assert not service._provider_session_is_interruptible("idle")
    assert not service._provider_session_is_interruptible("terminated")

    old = ProviderEvent(
        id="old-budget",
        type="budget_exhausted",
        display_message="Budget reached",
        payload={"provider_type": "session.status_idle"},
        terminal_status="budget_exhausted",
        stop_reason="budget_reached",
    )
    new = ProviderEvent(
        id="new-progress",
        type="progress",
        display_message="Work resumed",
        payload={"provider_type": "session.status_running"},
    )
    assert service._latest_provider_stop_reason([old]) == "budget_reached"
    filtered = service._provider_events_for_run(
        [old, new],
        {"configuration_snapshot": {"provider_event_baseline_ids": [old.id]}},
    )
    assert filtered == [new]

    confirmation = ProviderEvent(
        id="confirmation-1",
        type="progress",
        display_message=None,
        payload={
            "provider_type": "user.tool_confirmation",
            "tool_use_id": "tool-1",
            "result": "allow",
        },
    )
    assert service._provider_confirmed_tool_ids([old, confirmation]) == {"tool-1"}


def test_work_settlement_persists_full_cache_partition_and_provider_floor(monkeypatch):
    usage = calculate_work_credit_usage(
        {
            "list_cost": {"amount": "20", "currency": "USD"},
            "input_tokens": 30,
            "output_tokens": 7_668,
            "active_seconds": 184.073,
            "cache_creation": {"ephemeral_5m_input_tokens": 29_115},
            "cache_read_input_tokens": 244_569,
        },
        {"list_cost": {"amount": "0", "currency": "USD"}},
        model="claude-sonnet-5",
    )
    reservation_id = uuid4()
    work_session_id = uuid4()
    user_id = uuid4()
    transaction: dict[str, object] = {}
    settlement = SimpleNamespace(
        billed_quantity=usage.total_credits,
        reservation=SimpleNamespace(
            id=reservation_id,
            billing_account_id=uuid4(),
            usage_period_id=uuid4(),
        ),
    )
    monkeypatch.setattr(
        service.repository,
        "get_work_session",
        lambda *_: {"user_id": user_id},
    )
    monkeypatch.setattr(
        service,
        "resolve_effective_subscription",
        lambda *_: SimpleNamespace(
            plan=SimpleNamespace(allowances=SimpleNamespace(ai_credits=1_000_000))
        ),
    )
    monkeypatch.setattr(
        service, "settle_usage_with_supplement", lambda *_args, **_kwargs: settlement
    )
    monkeypatch.setattr(
        service.billing_repository,
        "create_credit_transaction",
        lambda _db, **kwargs: transaction.update(kwargs),
    )

    billed = service._settle_work_billing(
        object(),
        {
            "billing_reservation_id": reservation_id,
            "work_session_id": work_session_id,
            "request_id": "work-cache-incident",
            "instruction": "Analyze this repository",
        },
        usage,
    )

    assert billed == 385_636
    assert transaction["input_tokens"] == 273_714
    assert transaction["normal_input_tokens"] == 30
    assert transaction["cached_input_tokens"] == 244_569
    assert transaction["cache_write_tokens"] == 29_115
    assert transaction["input_credits"] == 243_523
    assert transaction["output_credits"] == 138_024
    assert transaction["fixed_credits"] == 4_089
    assert transaction["total_credits"] == 385_636
    assert transaction["provider_cost_usd"] == pytest.approx(0.2025301889)
    metadata = transaction["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["managed_component_credits"] == 385_636
    assert metadata["managed_provider_floor_credits"] == 200_000
    assert metadata["managed_reported_provider_cost_usd"] == pytest.approx(0.20)
