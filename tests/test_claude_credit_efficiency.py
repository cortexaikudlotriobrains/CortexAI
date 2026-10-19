"""Provider payload and usage regressions for Claude credit efficiency."""

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from api.claude_client import ClaudeClient
from models.provider_stream import ProviderStreamObserver


def provider_response():
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="Preserved answer")],
        usage=SimpleNamespace(
            input_tokens=20,
            output_tokens=30,
            cache_read_input_tokens=7000,
            cache_creation_input_tokens=1000,
            cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=0),
            server_tool_use=SimpleNamespace(web_search_requests=0),
        ),
        model="claude-opus-5-5",
        stop_reason="end_turn",
    )


def provider_client(monkeypatch):
    fake = Mock()
    response = provider_response()
    fake.messages.create.return_value = response
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.__iter__.return_value = iter([])
    stream.get_final_message.return_value = response
    fake.messages.stream.return_value = stream
    monkeypatch.setattr("api.claude_client.anthropic.Anthropic", lambda **_: fake)
    return fake


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "enabled,secret,expected",
    [(True, "private-test-secret", True), (False, "private-test-secret", False), (True, "", False)],
)
def test_cache_preserves_full_context_reasoning_and_stream_usage(
    monkeypatch, streamed, enabled, secret, expected
):
    monkeypatch.setenv("CLAUDE_PROMPT_CACHE_ENABLED", str(enabled).lower())
    monkeypatch.setenv("CACHE_KEY_SECRET", secret)
    monkeypatch.setenv("NATIVE_WEB_SEARCH_MODE", "off")
    fake = provider_client(monkeypatch)
    messages = [
        {"role": "system", "content": "Keep all factual constraints."},
        {"role": "user", "content": "Our budget is $500. Preserve this exact fact."},
        {"role": "assistant", "content": "Previous complete response."},
        {"role": "user", "content": "Continue with the same constraints."},
    ]
    observer = (
        ProviderStreamObserver(
            provider="claude",
            model="claude-opus-5-5",
            activity_callback=lambda _: None,
            text_callback=lambda _: None,
        )
        if streamed
        else None
    )
    result = ClaudeClient(api_key="test", model_name="claude-opus-5-5").get_completion(
        messages=messages,
        max_tokens=12288,
        reasoning_mode="adaptive",
        reasoning_effort="high",
        _stream_observer=observer,
        _cache_scope={
            "scope_id": "test-session",
            "mode": "compare",
            "stable_context_hash": "prefix",
        },
    )
    assert result.is_success
    assert result.text == "Preserved answer"
    payload = (fake.messages.stream if streamed else fake.messages.create).call_args.kwargs
    assert ("cache_control" in payload) is expected
    if expected:
        assert payload["cache_control"] == {"type": "ephemeral"}
    assert payload["system"] == messages[0]["content"]
    assert [m["content"][0]["text"] for m in payload["messages"]] == [
        m["content"] for m in messages[1:]
    ]
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert payload["max_tokens"] == 12288
    assert result.token_usage.prompt_tokens == 8020
    assert result.token_usage.cached_input_tokens == 7000
    assert result.token_usage.cache_write_tokens == 1000
    assert result.token_usage.completion_tokens == 30


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "model,enabled,tool",
    [
        ("claude-opus-5-5", True, "web_search_20260318"),
        ("claude-sonnet-5-5-20260928", True, "web_search_20260318"),
        ("claude-opus-5-5", False, "web_search_20250305"),
        ("claude-sonnet-4-5", True, "web_search_20250305"),
        ("claude-haiku-5-5", True, "web_search_20250305"),
    ],
)
def test_filtered_search_preserves_citations_cap_and_usage(
    monkeypatch, streamed, model, enabled, tool
):
    monkeypatch.setenv("CLAUDE_FILTERED_WEB_SEARCH_ENABLED", str(enabled).lower())
    monkeypatch.setenv("NATIVE_WEB_SEARCH_MODE", "enabled")
    monkeypatch.setenv("NATIVE_WEB_SEARCH_PROVIDERS", "claude")
    fake = provider_client(monkeypatch)
    response = provider_response()
    response.model = model
    response.content = [
        SimpleNamespace(type="server_tool_use", name="code_execution", id="code-1"),
        SimpleNamespace(
            type="server_tool_use",
            name="web_search",
            id="search-1",
            caller={"type": "code_execution_20260120", "tool_id": "code-1"},
        ),
        SimpleNamespace(
            type="web_search_tool_result",
            tool_use_id="search-1",
            caller={"type": "code_execution_20260120", "tool_id": "code-1"},
            content=[{"url": "https://example.com/evidence", "title": "Evidence"}],
        ),
        SimpleNamespace(
            type="text",
            text="Grounded result",
            citations=[{"url": "https://example.com/evidence", "title": "Evidence"}],
        ),
    ]
    response.usage.server_tool_use.web_search_requests = 2
    fake.messages.create.return_value = response
    fake.messages.stream.return_value.__enter__.return_value.get_final_message.return_value = (
        response
    )
    observer = (
        ProviderStreamObserver(
            provider="claude",
            model=model,
            activity_callback=lambda _: None,
            text_callback=lambda _: None,
        )
        if streamed
        else None
    )
    result = ClaudeClient(api_key="test", model_name=model).get_completion(
        "Find current evidence",
        web_search_policy={"mode": "required", "max_operations": 3},
        reasoning_mode="adaptive",
        reasoning_effort="high",
        max_tokens=12288,
        _stream_observer=observer,
    )
    assert result.is_success
    assert result.text == "Grounded result[1]"
    assert result.metadata["web_source_items"] == [
        {"url": "https://example.com/evidence", "title": "Evidence"}
    ]
    assert result.metadata["web_search"]["operations"] == 2
    assert result.metadata["web_search"]["fixed_credits"] == 20000
    assert result.token_usage.cached_input_tokens == 7000
    call = fake.messages.stream if streamed else fake.messages.create
    call.assert_called_once()
    payload = call.call_args.kwargs
    assert payload["tools"] == [{"type": tool, "name": "web_search", "max_uses": 3}]
    if tool == "web_search_20260318":
        assert payload["tool_choice"] == {"type": "auto"}
        assert "must use web_search before answering" in payload["system"]
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}
    assert payload["max_tokens"] == 12288
