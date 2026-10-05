from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.claude_client import ClaudeClient
from api.google_gemini_client import GeminiClient
from api.grok_client import GrokClient
from api.openai_client import OpenAIClient
from config.provider_streaming import provider_live_streaming_enabled
from models.cortex_activity import (
    CortexActivitySignal,
    CortexActivityType,
    ProviderEventNormalizer,
)
from models.provider_stream import (
    ProviderStreamCancelled,
    ProviderStreamObserver,
    ProviderTextDelta,
)


class _StreamManager:
    def __init__(self, events, final, *, final_method: str):
        self.events = list(events)
        self.final = final
        self.final_method = final_method

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def __iter__(self):
        return iter(self.events)

    def get_final_response(self):
        assert self.final_method == "response"
        return self.final

    def get_final_completion(self):
        assert self.final_method == "completion"
        return self.final

    def get_final_message(self):
        assert self.final_method == "message"
        return self.final


def _observer(
    provider: str = "openai",
) -> tuple[ProviderStreamObserver, list[CortexActivitySignal], list[ProviderTextDelta]]:
    activities: list[CortexActivitySignal] = []
    deltas: list[ProviderTextDelta] = []
    observer = ProviderStreamObserver(
        provider=provider,
        model="model-test",
        activity_callback=activities.append,
        text_callback=deltas.append,
    )
    return observer, activities, deltas


def test_observer_emits_answer_activity_once_and_preserves_delta_whitespace():
    observer, activities, deltas = _observer()

    observer.emit_text("Hello")
    observer.emit_text(" world\n")

    assert [signal.event_type for signal in activities] == [
        CortexActivityType.ANSWER_STARTED,
        CortexActivityType.ANSWER_DELTA,
    ]
    assert [delta.text for delta in deltas] == ["Hello", " world\n"]
    assert observer.has_emitted_text is True


def test_observer_cancellation_stops_provider_worker_at_next_event():
    observer, _, _ = _observer()
    observer.cancel()

    with pytest.raises(ProviderStreamCancelled):
        observer.emit_text("late")


def test_terminal_provider_events_do_not_own_public_request_completion():
    cases = [
        ("openai", {"type": "response.completed"}),
        ("claude", {"type": "message_stop"}),
        ("gemini", {"event_type": "interaction.completed"}),
    ]

    for provider, raw_event in cases:
        assert ProviderEventNormalizer(provider).normalize(raw_event) == []


def test_gemini_step_lifecycle_is_statefully_normalized():
    normalizer = ProviderEventNormalizer("gemini", model="gemini-test")

    started = normalizer.normalize(
        {
            "event_type": "step.start",
            "index": 2,
            "step": {"type": "google_search_call"},
        }
    )
    completed = normalizer.normalize({"event_type": "step.stop", "index": 2})

    assert [signal.event_type for signal in started] == [CortexActivityType.SEARCH_STARTED]
    assert [signal.event_type for signal in completed] == [
        CortexActivityType.SEARCH_COMPLETED
    ]


def test_openai_responses_stream_returns_final_response_and_live_deltas():
    final = SimpleNamespace(output_text="Hello world")
    events = [
        SimpleNamespace(type="response.created"),
        SimpleNamespace(type="response.web_search_call.searching"),
        SimpleNamespace(type="response.output_text.delta", delta="Hello"),
        SimpleNamespace(type="response.output_text.delta", delta=" world"),
        SimpleNamespace(type="response.completed"),
    ]
    manager = _StreamManager(events, final, final_method="response")
    fake_responses = SimpleNamespace(stream=lambda **kwargs: manager)
    client = OpenAIClient.__new__(OpenAIClient)
    client.client = SimpleNamespace(responses=fake_responses)
    observer, activities, deltas = _observer("openai")

    result = client._create_responses_completion({"model": "gpt-test"}, observer)

    assert result is final
    assert "".join(delta.text for delta in deltas) == "Hello world"
    event_types = [signal.event_type for signal in activities]
    assert CortexActivityType.SEARCH_STARTED in event_types
    assert CortexActivityType.REQUEST_COMPLETED not in event_types


def test_openai_chat_stream_keeps_final_usage_and_requests_usage_chunk():
    final = SimpleNamespace(
        id="chatcmpl-test",
        object="chat.completion",
        created=1,
        model="gpt-4o-mini",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(role="assistant", content="Hello"),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=4,
            completion_tokens=2,
            total_tokens=6,
        ),
    )
    manager = _StreamManager(
        [SimpleNamespace(type="content.delta", delta="Hello")],
        final,
        final_method="completion",
    )
    calls = []

    def stream(**kwargs):
        calls.append(kwargs)
        return manager

    client = OpenAIClient(api_key="test", model_name="gpt-4o-mini")
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(stream=stream))
    )
    observer, _, deltas = _observer("openai")

    response = client.get_completion(
        messages=[{"role": "user", "content": "Hi"}],
        max_tokens=32,
        _stream_observer=observer,
    )

    assert response.error is None
    assert response.text == "Hello"
    assert response.token_usage.total_tokens == 6
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert [delta.text for delta in deltas] == ["Hello"]


def test_claude_message_stream_returns_final_message_and_live_deltas():
    final = SimpleNamespace(content=[])
    events = [
        SimpleNamespace(type="message_start"),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Claude live"),
        ),
        SimpleNamespace(type="message_stop"),
    ]
    manager = _StreamManager(events, final, final_method="message")
    fake_messages = SimpleNamespace(stream=lambda **kwargs: manager)
    client = ClaudeClient.__new__(ClaudeClient)
    client.client = SimpleNamespace(messages=fake_messages)
    observer, activities, deltas = _observer("claude")

    result = client._create_message({"model": "claude-test"}, observer)

    assert result is final
    assert [delta.text for delta in deltas] == ["Claude live"]
    assert CortexActivityType.REQUEST_COMPLETED not in {
        signal.event_type for signal in activities
    }


def test_gemini_interactions_stream_returns_completed_interaction():
    final = SimpleNamespace(status="completed", steps=[])
    events = [
        SimpleNamespace(event_type="interaction.created"),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="text", text="Gemini live"),
        ),
        SimpleNamespace(event_type="interaction.completed", interaction=final),
    ]
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return iter(events)

    client = GeminiClient.__new__(GeminiClient)
    client.client = SimpleNamespace(interactions=SimpleNamespace(create=create))
    observer, activities, deltas = _observer("gemini")

    result = client._create_interaction({"model": "gemini-test"}, observer)

    assert result is final
    assert calls == [{"model": "gemini-test", "stream": True}]
    assert [delta.text for delta in deltas] == ["Gemini live"]
    assert CortexActivityType.REQUEST_COMPLETED not in {
        signal.event_type for signal in activities
    }


def test_grok_chat_stream_uses_openai_compatible_live_events():
    final = SimpleNamespace(choices=[])
    events = [
        SimpleNamespace(type="content.delta", delta="Grok live"),
        SimpleNamespace(
            type="chunk",
            chunk=SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=" live"))]
            ),
        ),
    ]
    manager = _StreamManager(events, final, final_method="completion")
    fake_completions = SimpleNamespace(stream=lambda **kwargs: manager)
    client = GrokClient.__new__(GrokClient)
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=fake_completions)
    )
    observer, _, deltas = _observer("grok")

    result = client._create_chat_completion({"model": "grok-test"}, observer)

    assert result is final
    assert "".join(delta.text for delta in deltas) == "Grok live"


def test_provider_streaming_rollout_defaults_exclude_deepseek(monkeypatch):
    monkeypatch.delenv("PROVIDER_LIVE_STREAMING_PROVIDERS", raising=False)

    assert provider_live_streaming_enabled("openai") is True
    assert provider_live_streaming_enabled("anthropic") is True
    assert provider_live_streaming_enabled("google") is True
    assert provider_live_streaming_enabled("xai") is True
    assert provider_live_streaming_enabled("deepseek") is False


def test_provider_streaming_rollout_can_be_disabled(monkeypatch):
    monkeypatch.setenv("PROVIDER_LIVE_STREAMING_PROVIDERS", "off")

    assert provider_live_streaming_enabled("openai") is False
