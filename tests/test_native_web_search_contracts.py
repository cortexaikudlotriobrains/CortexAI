from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone

import pytest

from api.claude_client import ClaudeClient
from api.deepseek_client import DeepSeekClient
from api.google_gemini_client import GeminiClient
from api.grok_client import GrokClient
from api.openai_client import OpenAIClient
from config.web_search import load_native_web_search_config
from server.billing.web_search_billing import web_search_reservation_override
from tools.web.native_policy import (
    normalize_requested_web_mode,
    orchestrator_research_mode,
    resolve_web_search_policy,
)
from tools.web.provider_metadata import (
    build_web_search_metadata,
    normalize_web_sources,
    web_search_fixed_credits,
)


def test_new_web_mode_takes_precedence_over_legacy_boolean():
    assert normalize_requested_web_mode("auto", legacy_research_mode=False) == "auto"
    assert normalize_requested_web_mode("on", legacy_research_mode=False) == "required"


def test_legacy_web_boolean_remains_compatible():
    assert normalize_requested_web_mode(None, legacy_research_mode=True) == "required"
    assert normalize_requested_web_mode(None, legacy_research_mode=False) == "off"
    assert normalize_requested_web_mode(None, legacy_research_mode=None) == "auto"


def test_auto_policy_requires_search_for_current_information():
    policy = resolve_web_search_policy(
        "What is the latest Bank of Canada rate?",
        requested_mode="auto",
    )
    assert policy.effective_mode == "required"
    assert policy.reason == "current_information_required"


def test_prompt_can_explicitly_disable_automatic_search():
    policy = resolve_web_search_policy(
        "Without browsing, explain how DNS works.",
        requested_mode="auto",
    )
    assert policy.effective_mode == "off"


def test_ordinary_auto_prompt_leaves_decision_to_provider():
    policy = resolve_web_search_policy("Explain DNS caching.", requested_mode="auto")
    assert policy.effective_mode == "auto"
    assert policy.reason == "provider_decides"


def test_context_only_followup_does_not_force_search():
    policy = resolve_web_search_policy("Continue from where you left.", requested_mode="auto")
    assert policy.effective_mode == "auto"
    assert policy.reason == "provider_decides"


def test_required_policy_maps_to_legacy_orchestrator_on_value():
    policy = resolve_web_search_policy(
        "What is the latest Bank of Canada rate?",
        requested_mode="auto",
    )
    assert (
        orchestrator_research_mode(
            policy,
            requested_mode="auto",
            legacy_research_mode=None,
        )
        == "on"
    )


def test_legacy_disabled_web_request_stays_off_for_orchestrator():
    policy = resolve_web_search_policy(
        "What is the latest Bank of Canada rate?",
        requested_mode=None,
        legacy_research_mode=False,
    )
    assert (
        orchestrator_research_mode(
            policy,
            requested_mode=None,
            legacy_research_mode=False,
        )
        == "off"
    )


def test_source_normalization_deduplicates_and_caps():
    sources = normalize_web_sources(
        [
            {"title": "A", "url": "https://example.com/a"},
            {"title": "Duplicate", "url": "https://example.com/a/"},
            {"name": "B", "uri": "https://example.com/b"},
        ],
        limit=2,
    )
    assert sources == [
        {"title": "A", "url": "https://example.com/a"},
        {"title": "B", "url": "https://example.com/b"},
    ]


def test_provider_search_metadata_prices_actual_operations_only():
    metadata = build_web_search_metadata(
        provider="gemini",
        backend="google_search",
        requested_mode="auto",
        effective_mode="required",
        operations=2,
        sources=[{"title": "Source", "url": "https://example.com"}],
    )
    assert web_search_fixed_credits(metadata) == 28_000
    assert metadata["research_provider_credits_used"] == 0
    assert metadata["web_search"]["provider_cost_usd"] == 0.028


def test_provider_search_metadata_never_bills_above_product_operation_cap():
    metadata = build_web_search_metadata(
        provider="gemini",
        backend="google_search",
        requested_mode="auto",
        effective_mode="auto",
        operations=5,
    )
    usage = metadata["web_search"]
    assert usage["operations"] == 3
    assert usage["provider_reported_operations"] == 5
    assert usage["operation_limit_exceeded"] is True
    assert usage["fixed_credits"] == 42_000
    assert usage["provider_cost_usd"] == 0.07


def test_config_enforces_product_caps(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_MAX_OPERATIONS", "99")
    monkeypatch.setenv("WEB_SEARCH_MAX_DISPLAY_SOURCES", "99")
    monkeypatch.setenv("DEEPSEEK_WEB_SEARCH_MAX_RESULTS", "99")
    config = load_native_web_search_config()
    assert config.max_operations == 3
    assert config.max_display_sources == 8
    assert config.deepseek_max_results == 5


def test_legacy_rollout_keeps_default_tavily_reservation():
    assert (
        web_search_reservation_override(
            ("openai",),
            native_enabled=False,
            search_enabled=True,
            per_target=False,
        )
        is None
    )


def test_native_rollout_can_explicitly_reserve_zero_when_search_is_off():
    assert (
        web_search_reservation_override(
            ("openai",),
            native_enabled=True,
            search_enabled=False,
            per_target=False,
        )
        == 0
    )


def _policy(mode="auto"):
    return {
        "requested_mode": "auto",
        "mode": mode,
        "max_operations": 3,
        "max_display_sources": 8,
    }


def test_openai_native_search_uses_responses_and_returns_citations(monkeypatch):
    fake_client = Mock()
    fake_client.responses.create.return_value = SimpleNamespace(
        output_text="Latest update",
        output=[
            SimpleNamespace(
                type="web_search_call",
                action=SimpleNamespace(
                    sources=[SimpleNamespace(url="https://example.com/a", title="A")]
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        text="Latest update",
                        annotations=[
                            SimpleNamespace(
                                url_citation=SimpleNamespace(
                                    url="https://example.com/a",
                                    title="A",
                                    end_index=13,
                                )
                            )
                        ],
                    )
                ],
            ),
        ],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        status="completed",
        model="gpt-4o",
    )
    monkeypatch.setattr("api.openai_client.openai.OpenAI", lambda **_: fake_client)

    response = OpenAIClient(api_key="test", model_name="gpt-4o").get_completion(
        "What is new?",
        web_search_policy=_policy("required"),
    )

    assert response.is_success
    assert response.text == "Latest update[1]"
    assert response.metadata["web_search"]["operations"] == 1
    assert response.metadata["web_source_items"][0]["url"] == "https://example.com/a"
    payload = fake_client.responses.create.call_args.kwargs
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["tool_choice"] == "required"
    assert payload["max_tool_calls"] == 3


def test_claude_native_search_caps_uses_and_preserves_sources(monkeypatch):
    fake_client = Mock()
    fake_client.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(type="server_tool_use", name="web_search"),
            SimpleNamespace(
                type="web_search_tool_result",
                content=[SimpleNamespace(url="https://example.com/b", title="B")],
            ),
            SimpleNamespace(
                type="text",
                text="Claude result",
                citations=[SimpleNamespace(url="https://example.com/b", title="B")],
            ),
        ],
        usage=SimpleNamespace(
            input_tokens=9,
            output_tokens=4,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=1),
        ),
        model="claude-sonnet-4-5",
        stop_reason="end_turn",
    )
    monkeypatch.setattr("api.claude_client.anthropic.Anthropic", lambda **_: fake_client)

    response = ClaudeClient(api_key="test").get_completion(
        "Find the latest update",
        web_search_policy=_policy("auto"),
    )

    assert response.is_success
    assert response.text == "Claude result[1]"
    assert response.metadata["web_search"]["operations"] == 1
    payload = fake_client.messages.create.call_args.kwargs
    assert payload["tools"][0]["max_uses"] == 3
    assert payload["tool_choice"] == {"type": "auto"}


def test_gemini_interactions_search_extracts_result_items(monkeypatch):
    fake_client = Mock()
    fake_client.interactions.create.return_value = SimpleNamespace(
        steps=[
            SimpleNamespace(
                type="google_search_call",
                arguments=SimpleNamespace(queries=["latest rate"]),
            ),
            SimpleNamespace(
                type="google_search_result",
                result=[SimpleNamespace(url="https://example.com/c", title="C")],
            ),
            SimpleNamespace(
                type="model_output",
                content=[
                    SimpleNamespace(
                        type="text",
                        text="Gemini result",
                        annotations=[SimpleNamespace(source="https://example.com/c", end_index=13)],
                    )
                ],
            ),
        ],
        usage=SimpleNamespace(
            total_input_tokens=8,
            total_output_tokens=6,
            total_tokens=16,
            total_cached_tokens=0,
            total_thought_tokens=2,
            grounding_tool_count=[SimpleNamespace(type="google_search", count=1)],
        ),
        model="gemini-2.5-flash",
        status="completed",
        output_text="Gemini result",
    )
    monkeypatch.setattr("api.google_gemini_client.genai.Client", lambda **_: fake_client)

    response = GeminiClient(api_key="test", model_name="gemini-2.5-flash").get_completion(
        "What is the latest rate?",
        web_search_policy=_policy("required"),
    )

    assert response.is_success
    assert response.text == "Gemini result[1]"
    assert response.metadata["web_search"]["operations"] == 1
    assert response.token_usage.reasoning_tokens == 2
    assert response.token_usage.completion_tokens == 8
    assert response.token_usage.total_tokens == 16
    payload = fake_client.interactions.create.call_args.kwargs
    assert payload["tools"] == [{"type": "google_search"}]
    assert payload["input"] == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "What is the latest rate?"}],
        }
    ]
    assert payload["generation_config"]["temperature"] == 0.7
    assert "tool_choice" not in payload["generation_config"]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("reported_total", [0, 2675])
def test_gemini_interactions_bills_answer_and_thought_tokens(monkeypatch, streamed, reported_total):
    final = SimpleNamespace(
        steps=[],
        output_text="Answer",
        status="completed",
        model="gemini-3.1-pro-preview",
        usage=SimpleNamespace(
            total_input_tokens=255,
            total_output_tokens=1519,
            total_thought_tokens=901,
            total_tokens=reported_total,
        ),
    )
    fake_client = Mock()
    fake_client.interactions.create.return_value = (
        iter([SimpleNamespace(event_type="interaction.completed", interaction=final)])
        if streamed
        else final
    )
    monkeypatch.setattr("api.google_gemini_client.genai.Client", lambda **_: fake_client)
    observer = Mock(has_emitted=False) if streamed else None
    response = GeminiClient(api_key="test", model_name="gemini-3.1-pro-preview").get_completion(
        "Answer this",
        web_search_policy=_policy("required"),
        _stream_observer=observer,
    )
    assert response.is_success
    assert response.token_usage.completion_tokens == 2420
    assert response.token_usage.reasoning_tokens == 901
    assert response.token_usage.total_tokens == 2675
    assert response.estimated_cost == pytest.approx(0.02955)


def test_gemini_interactions_history_uses_v2_step_roles():
    payload = GeminiClient._build_interactions_input(
        [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Follow-up"},
        ],
        attachments=[],
    )

    assert payload == [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "First question"}],
        },
        {
            "type": "model_output",
            "content": [{"type": "text", "text": "First answer"}],
        },
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "Follow-up"}],
        },
    ]


def test_gemini_3_5_native_search_omits_unsupported_temperature(monkeypatch):
    fake_client = Mock()

    def create_interaction(**kwargs):
        assert "temperature" not in kwargs["generation_config"]
        return SimpleNamespace(
            steps=[
                SimpleNamespace(
                    type="model_output",
                    content=[SimpleNamespace(type="text", text="Gemini result", annotations=[])],
                )
            ],
            usage=SimpleNamespace(
                total_input_tokens=8,
                total_output_tokens=6,
                total_tokens=14,
                total_cached_tokens=0,
                total_reasoning_tokens=0,
            ),
            model="gemini-3.5-flash-lite",
            status="completed",
            output_text="Gemini result",
        )

    fake_client.interactions.create.side_effect = create_interaction
    monkeypatch.setattr("api.google_gemini_client.genai.Client", lambda **_: fake_client)

    response = GeminiClient(
        api_key="test",
        model_name="gemini-3.5-flash-lite",
    ).get_completion(
        "What is the latest rate?",
        temperature=0.7,
        reasoning_effort="medium",
        web_search_policy=_policy("required"),
    )

    assert response.is_success
    payload = fake_client.interactions.create.call_args.kwargs
    assert payload["tools"] == [{"type": "google_search"}]
    assert payload["generation_config"]["max_output_tokens"] == 2048
    assert payload["generation_config"]["thinking_level"] == "medium"
    assert "tool_choice" not in payload["generation_config"]


def test_grok_native_search_uses_usage_and_normalizes_inline_citations(monkeypatch):
    fake_client = Mock()
    fake_client.responses.create.return_value = SimpleNamespace(
        output_text="Grok result[[1]](https://example.com/d)",
        output=[SimpleNamespace(type="web_search_call", action={})],
        citations=["https://example.com/d"],
        usage=SimpleNamespace(
            input_tokens=7,
            output_tokens=5,
            total_tokens=12,
            server_side_tool_usage_details=SimpleNamespace(web_search_calls=1),
        ),
        model="grok-4-latest",
        finish_reason="stop",
    )
    monkeypatch.setattr("api.grok_client.openai.OpenAI", lambda **_: fake_client)

    response = GrokClient(api_key="test").get_completion(
        "What is new?",
        web_search_policy=_policy("auto"),
    )

    assert response.is_success
    assert response.text == "Grok result[1]"
    assert response.metadata["web_search"]["operations"] == 1
    payload = fake_client.responses.create.call_args.kwargs
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["max_tool_calls"] == 3
    assert payload["parallel_tool_calls"] is False
    assert payload["extra_body"] == {"max_turns": 3}
    assert "include" not in payload


def test_deepseek_search_loop_requires_only_first_call(monkeypatch):
    call_times = iter(
        [
            datetime(2026, 10, 8, 3, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 10, 8, 4, 0, 0, tzinfo=timezone.utc),
        ]
    )

    class ProviderClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(call_times)

    monkeypatch.setattr("api.deepseek_client.datetime", ProviderClock)
    fake_client = Mock()
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="search_web", arguments='{"query":"latest rate"}'),
    )
    fake_client.chat.completions.create.side_effect = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call]),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            model="deepseek-chat",
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="DeepSeek result [1]", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4, total_tokens=12),
            model="deepseek-chat",
        ),
    ]
    monkeypatch.setattr("api.deepseek_client.openai.OpenAI", lambda **_: fake_client)

    response = DeepSeekClient(api_key="test").get_completion(
        "What is the latest rate?",
        web_search_policy=_policy("required"),
        web_search_executor=lambda query: {
            "ok": True,
            "query": query,
            "content": "1. Rate source",
            "sources": [{"title": "D", "url": "https://example.com/e"}],
            "provider_credits_used": 2,
            "provider_credits_estimated": False,
        },
    )

    assert response.is_success
    assert response.token_usage.total_tokens == 19
    assert response.metadata["web_search"]["operations"] == 1
    assert response.estimated_cost == pytest.approx(0.0000075)
    audit = response.pricing_snapshot
    assert audit["pricing_tier"] == "mixed"
    assert [item["pricing_tier"] for item in audit["provider_calls"]] == ["peak", "off_peak"]
    assert audit["normalized_usage"]["input_tokens"] == 13
    calls = fake_client.chat.completions.create.call_args_list
    assert calls[0].kwargs["tool_choice"] == "required"
    assert calls[1].kwargs["tool_choice"] == "auto"


def test_deepseek_preserves_search_usage_when_later_model_turn_fails(monkeypatch):
    fake_client = Mock()
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="search_web", arguments='{"query":"latest rate"}'),
    )
    fake_client.chat.completions.create.side_effect = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=None, tool_calls=[tool_call]),
                    finish_reason="tool_calls",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            model="deepseek-chat",
        ),
        RuntimeError("provider failed after tool result"),
    ]
    monkeypatch.setattr("api.deepseek_client.openai.OpenAI", lambda **_: fake_client)

    response = DeepSeekClient(api_key="test").get_completion(
        "What is the latest rate?",
        web_search_policy=_policy("required"),
        web_search_executor=lambda query: {
            "ok": True,
            "query": query,
            "content": "1. Rate source",
            "sources": [{"title": "D", "url": "https://example.com/e"}],
            "provider_credits_used": 2,
            "provider_credits_estimated": False,
        },
    )

    assert response.is_error
    assert response.token_usage.total_tokens == 7
    assert response.metadata["web_search"]["status"] == "error"
    assert response.metadata["web_search"]["operations"] == 1
    assert response.metadata["web_source_items"] == [{"title": "D", "url": "https://example.com/e"}]
