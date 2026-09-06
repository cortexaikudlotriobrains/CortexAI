from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.schemas.work import WorkSessionCreateDTO
from server.work.anthropic_provider import (
    AnthropicManagedAgentProvider,
    normalize_anthropic_event,
)
from server.work.billing import calculate_work_credit_usage
from server.work.config import WorkConfig, load_work_config
from server.work.provider import ProviderMcpServer, ProviderResource
from server.work.security import classify_action, redact_mapping, validate_remote_https_url


def _config() -> WorkConfig:
    return WorkConfig(
        enabled=True,
        mcp_enabled=True,
        action_tools_enabled=True,
        artifact_import_enabled=True,
        web_enabled=True,
        provider="fake",
        agent_id="agent_123",
        environment_id="environment_123",
        default_credit_budget=1_000_000,
        default_output_token_limit=40_000,
        output_finalize_token_threshold=32_000,
        reconciler_enabled=True,
        reconciler_interval_seconds=2,
        event_sync_interval_seconds=1,
        sse_heartbeat_seconds=15,
        approval_timeout_seconds=3600,
    )


def test_anthropic_event_normalization_redacts_tool_secrets_and_thinking():
    tool = normalize_anthropic_event(
        {
            "id": "tool-1",
            "type": "agent.mcp_tool_use",
            "name": "send_email",
            "input": {"to": "person@example.com", "authorization": "secret"},
        }
    )
    assert tool.type == "tool_started"
    assert tool.payload["input_summary"] == {
        "to": "person@example.com",
        "authorization": "[redacted]",
    }
    thinking = normalize_anthropic_event(
        {
            "id": "thinking-1",
            "type": "agent.thinking",
            "content": [{"type": "text", "text": "private reasoning"}],
        }
    )
    assert thinking.display_message == "Reasoning about the next step"
    assert "private reasoning" not in str(thinking.payload)


def test_anthropic_idle_events_map_approval_budget_and_completion():
    approval = normalize_anthropic_event(
        {
            "id": "idle-approval",
            "type": "session.status_idle",
            "stop_reason": {"type": "requires_action", "event_ids": ["tool-1"]},
        }
    )
    budget = normalize_anthropic_event(
        {
            "id": "idle-budget",
            "type": "session.status_idle",
            "stop_reason": {"type": "budget_reached"},
        }
    )
    complete = normalize_anthropic_event(
        {"id": "idle-complete", "type": "session.status_idle", "stop_reason": {"type": "end_turn"}}
    )
    assert approval.type == "approval_required"
    assert approval.payload["blocking_event_ids"] == ["tool-1"]
    assert budget.terminal_status == "budget_exhausted"
    assert complete.terminal_status == "completed"


def test_anthropic_tool_confirmation_retains_replay_identity():
    confirmation = normalize_anthropic_event(
        {
            "id": "confirmation-1",
            "type": "user.tool_confirmation",
            "tool_use_id": "tool-1",
            "result": "allow",
        }
    )
    assert confirmation.payload == {
        "provider_type": "user.tool_confirmation",
        "tool_use_id": "tool-1",
        "result": "allow",
    }


def test_work_runtime_uses_session_model_without_a_billing_model_environment(monkeypatch):
    monkeypatch.setenv("CORTEX_WORK_AGENT_PROVIDER", "anthropic_managed_agents")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_MANAGED_AGENT_ID", "agent-1")
    monkeypatch.setenv("ANTHROPIC_MANAGED_ENVIRONMENT_ID", "environment-1")
    monkeypatch.delenv("ANTHROPIC_MANAGED_BILLING_MODEL", raising=False)
    monkeypatch.delenv("CORTEX_WORK_DEFAULT_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("CORTEX_WORK_OUTPUT_FINALIZE_TOKENS", raising=False)
    load_work_config.cache_clear()
    try:
        config = load_work_config()
        config.validate_provider()
        assert config.default_output_token_limit == 40_000
        assert config.output_finalize_token_threshold == 32_000
        assert not hasattr(config, "billing_model")
    finally:
        load_work_config.cache_clear()


def test_usage_delta_prevents_followup_double_charge_and_counts_runtime_web():
    baseline = {
        "input_tokens": 1_000,
        "output_tokens": 200,
        "active_seconds": 30,
        "server_tool_use": {"web_search_requests": 1},
    }
    current = {
        "input_tokens": 1_500,
        "output_tokens": 300,
        "active_seconds": 75,
        "server_tool_use": {"web_search_requests": 3},
    }
    usage = calculate_work_credit_usage(current, baseline, model="claude-haiku-4-5")
    assert usage.prompt_tokens == 500
    assert usage.output_tokens == 100
    assert usage.active_seconds == 45
    assert usage.web_searches == 2
    assert usage.runtime_credits == 1_000
    assert usage.web_credits == 20_000
    assert usage.total_credits == usage.model_credits + 21_000
    assert usage.provider_cost_usd > 0.02


def test_work_billing_counts_managed_agent_cache_partitions_independently():
    usage = calculate_work_credit_usage(
        {
            "list_cost": {"amount": "20", "currency": "USD"},
            "input_tokens": 30,
            "output_tokens": 7_668,
            "active_seconds": 184.073,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 29_115,
                "ephemeral_1h_input_tokens": 0,
            },
            "cache_read_input_tokens": 244_569,
            "server_tool_use": {
                "web_fetch_requests": 0,
                "web_search_requests": 0,
            },
        },
        {
            "list_cost": {"amount": "0", "currency": "USD"},
            "input_tokens": 0,
            "output_tokens": 0,
            "active_seconds": 0,
            "cache_read_input_tokens": 0,
            "server_tool_use": {
                "web_fetch_requests": 0,
                "web_search_requests": 0,
            },
        },
        model="claude-sonnet-5",
    )

    assert usage.prompt_tokens == 273_714
    assert usage.cached_input_tokens == 244_569
    assert usage.cache_write_tokens == 29_115
    assert usage.input_credits == 243_523
    assert usage.output_credits == 138_024
    assert usage.model_credits == 381_547
    assert usage.runtime_credits == 4_089
    assert usage.component_credits == 385_636
    assert usage.provider_floor_credits == 200_000
    assert usage.total_credits == 385_636
    assert usage.reported_provider_cost_usd == pytest.approx(0.20)
    assert usage.reconstructed_provider_cost_usd == pytest.approx(0.2025301889)
    assert usage.provider_cost_usd == pytest.approx(0.2025301889)


def test_work_billing_uses_cache_deltas_and_provider_cost_floor_for_followups():
    baseline = {
        "list_cost": {"amount": "20", "currency": "USD"},
        "input_tokens": 30,
        "output_tokens": 7_668,
        "active_seconds": 184.073,
        "cache_creation": {"ephemeral_5m_input_tokens": 29_115},
        "cache_read_input_tokens": 244_569,
    }
    current = {
        "list_cost": {"amount": "21", "currency": "USD"},
        "input_tokens": 35,
        "output_tokens": 8_000,
        "active_seconds": 190,
        "cache_creation": {"ephemeral_5m_input_tokens": 30_000},
        "cache_read_input_tokens": 250_000,
    }

    usage = calculate_work_credit_usage(current, baseline, model="claude-sonnet-5")

    assert usage.prompt_tokens == 6_321
    assert usage.cached_input_tokens == 5_431
    assert usage.cache_write_tokens == 885
    assert usage.output_tokens == 332
    assert usage.active_seconds == 6
    assert usage.component_credits == 12_728
    assert usage.provider_floor_credits == 10_000
    assert usage.total_credits == 12_728
    assert usage.reported_provider_cost_usd == pytest.approx(0.01)
    assert usage.reconstructed_provider_cost_usd == pytest.approx(0.0067620333)
    assert usage.provider_cost_usd == pytest.approx(0.01)


def test_work_billing_never_falls_below_reported_provider_list_cost():
    usage = calculate_work_credit_usage(
        {"list_cost": {"amount": "5", "currency": "USD"}},
        {"list_cost": {"amount": "0", "currency": "USD"}},
        model="claude-sonnet-5",
    )

    assert usage.component_credits == 0
    assert usage.provider_floor_credits == 50_000
    assert usage.total_credits == 50_000
    assert usage.provider_cost_usd == pytest.approx(0.05)


@pytest.mark.parametrize(
    "list_cost",
    [
        {"amount": "not-cents", "currency": "USD"},
        {"amount": -1, "currency": "USD"},
        {"amount": 1, "currency": "EUR"},
    ],
)
def test_work_billing_rejects_invalid_reported_provider_list_cost(list_cost):
    with pytest.raises(ValueError, match="Managed Agent"):
        calculate_work_credit_usage(
            {"list_cost": list_cost},
            {},
            model="claude-sonnet-5",
        )


def test_remote_mcp_ssrf_and_action_classification(monkeypatch):
    monkeypatch.setattr(
        "server.work.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    assert validate_remote_https_url("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"
    monkeypatch.setattr(
        "server.work.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    with pytest.raises(ValueError, match="private or reserved"):
        validate_remote_https_url("https://mcp.example.com/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        validate_remote_https_url("http://mcp.example.com/mcp")
    assert classify_action("list_issues") == "READ"
    assert classify_action("send_email") == "EXTERNAL_COMMUNICATION"
    assert classify_action("delete_repository") == "DESTRUCTIVE"
    assert redact_mapping({"api_key": "secret", "value": "safe"}) == {
        "api_key": "[redacted]",
        "value": "safe",
    }


def test_work_session_title_normalizes_control_and_format_characters():
    request = WorkSessionCreateDTO(title="Run this command:\n\npwd\tgit\u200b--version")

    assert request.title == "Run this command: pwd git --version"
    assert WorkSessionCreateDTO(title="\n\t\u200b").title is None


def test_provider_create_session_uses_beta_budget_permissions_resources_mcp_and_safe_title():
    calls: dict[str, object] = {}

    class Events:
        def send(self, session_id, **kwargs):
            calls["send"] = (session_id, kwargs)

        def list(self, session_id, **kwargs):
            calls["list"] = (session_id, kwargs)
            return []

    class Resources:
        def add(self, session_id, **kwargs):
            calls["resource"] = (session_id, kwargs)

    class Sessions:
        events = Events()
        resources = Resources()

        def create(self, **kwargs):
            calls["create"] = kwargs
            return {
                "id": "session-1",
                "status": "idle",
                "usage": {},
                "agent": {
                    "id": "agent-1",
                    "version": 7,
                    "model": {
                        "id": "claude-haiku-4-5-20251001",
                        "effort": {"type": "high"},
                        "speed": "standard",
                    },
                },
            }

        def retrieve(self, session_id, **kwargs):
            return {
                "id": session_id,
                "status": "running",
                "usage": {},
                "agent": {
                    "id": "agent-1",
                    "version": 7,
                    "model": {"id": "claude-haiku-4-5-20251001"},
                },
            }

        def update(self, session_id, **kwargs):
            calls["update"] = (session_id, kwargs)
            return {"id": session_id, "status": "running", "usage": {}}

    client = SimpleNamespace(
        beta=SimpleNamespace(
            sessions=Sessions(),
            files=SimpleNamespace(
                list=lambda **_kwargs: [
                    {
                        "id": "artifact-1",
                        "filename": "report.md",
                        "mime_type": "text/markdown",
                        "size_bytes": 12,
                        "downloadable": True,
                    },
                    {
                        "id": "input-1",
                        "filename": "input.txt",
                        "mime_type": "text/plain",
                        "size_bytes": 4,
                        "downloadable": False,
                    },
                ],
                download=lambda _id, **_kwargs: b"artifact data",
            ),
        ),
        files=SimpleNamespace(
            upload=lambda **_kwargs: {"id": "file-1"}, download=lambda _id: b"data"
        ),
    )
    provider = AnthropicManagedAgentProvider(_config(), client=client)
    created = provider.create_session(
        title="Prepare\u200breport\nfor\tthe team",
        resources=[ProviderResource("file-1", "/report.csv")],
        mcp_servers=[ProviderMcpServer("github", "https://mcp.example.com", ("list_issues",))],
        vault_ids=["vault-1"],
        web_enabled=False,
        max_credit_budget=100_000,
    )
    assert created.id == "session-1"
    assert created.model_id == "claude-haiku-4-5-20251001"
    assert created.agent_id == "agent-1"
    assert created.agent_version == 7
    assert created.effort == "high"
    assert created.speed == "standard"
    request = calls["create"]
    assert isinstance(request, dict)
    assert request["title"] == "Prepare report for the team"
    assert request["betas"] == ["managed-agents-2026-04-01"]
    assert request["budget"] == {
        "type": "limit",
        "max_list_cost": {"amount": "10", "currency": "USD"},
    }
    assert request["resources"] == [
        {"type": "file", "file_id": "file-1", "mount_path": "/report.csv"}
    ]
    agent = request["agent"]
    assert isinstance(agent, dict)
    assert agent["type"] == "agent_with_overrides"
    assert agent["mcp_servers"] == [
        {"type": "url", "name": "github", "url": "https://mcp.example.com"}
    ]
    builtins = agent["tools"][0]
    assert builtins["default_config"]["permission_policy"] == {"type": "always_ask"}
    builtin_configs = {config["name"]: config for config in builtins["configs"]}
    for name in ("read", "glob", "grep"):
        assert builtin_configs[name]["permission_policy"] == {"type": "always_allow"}
    for name in ("web_search", "web_fetch"):
        assert builtin_configs[name]["enabled"] is False
        assert builtin_configs[name]["permission_policy"] == {"type": "always_allow"}
    provider.list_events("session-1")
    assert calls["list"][1]["limit"] == 1000
    provider.update_session_tools(
        "session-1",
        mcp_servers=[],
        web_enabled=True,
    )
    tool_update = calls["update"]
    assert tool_update[0] == "session-1"
    assert set(tool_update[1]["agent"]) == {"mcp_servers", "tools"}
    assert tool_update[1]["agent"]["tools"][0]["configs"][-1]["enabled"] is True
    provider.extend_budget(
        "session-1",
        25_000,
        current_usage={"list_cost": {"amount": "12", "currency": "USD"}},
    )
    updated = calls["update"]
    assert updated[0] == "session-1"
    assert updated[1]["budget"] == {
        "type": "limit",
        "max_list_cost": {"amount": "15", "currency": "USD"},
    }
    provider.confirm_tool("session-1", "tool-1", allow=False, deny_message="No")
    sent = calls["send"]
    assert sent[1]["events"][0]["type"] == "user.tool_confirmation"
    artifacts = provider.list_artifacts("session-1")
    assert [(item.id, item.downloadable) for item in artifacts] == [
        ("artifact-1", True),
        ("input-1", False),
    ]
    assert provider.download_artifact("artifact-1") == b"artifact data"
