from __future__ import annotations

import asyncio

from models.cortex_activity import (
    CortexActivitySignal,
    CortexActivityType,
    activity_display_message,
    normalize_provider_event,
    safe_tool_activity_message,
    sanitize_activity_metadata,
)
from server.activity_stream import ActivityStream


def test_openai_web_search_event_normalizes_to_search_started():
    event = normalize_provider_event(
        "openai",
        {"type": "response.web_search_call.searching", "call_id": "secret-call"},
    )

    assert event is not None
    assert event.event_type == CortexActivityType.SEARCH_STARTED
    assert event.display_message == "Searching the web…"
    assert "secret-call" not in str(event.metadata)


def test_anthropic_thinking_block_never_copies_reasoning_content():
    event = normalize_provider_event(
        "anthropic",
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "private chain of thought"},
        },
    )

    assert event is not None
    assert event.event_type == CortexActivityType.THINKING_STARTED
    assert "private chain of thought" not in str(event.metadata)


def test_gemini_google_search_call_normalizes_to_search_started():
    event = normalize_provider_event("gemini", {"type": "google_search_call"})

    assert event is not None
    assert event.event_type == CortexActivityType.SEARCH_STARTED


def test_deepseek_reasoning_is_only_a_safe_progress_signal():
    event = normalize_provider_event(
        "deepseek",
        {"reasoning_content": "hidden reasoning", "content": None},
    )

    assert event is not None
    assert event.event_type == CortexActivityType.THINKING_STARTED
    assert event.metadata == {}


def test_malformed_or_unknown_provider_event_is_ignored():
    assert normalize_provider_event("unknown", {"type": "private.event"}) is None
    assert normalize_provider_event("openai", {"payload": "missing type"}) is None


def test_activity_metadata_allowlist_drops_provider_internals():
    assert sanitize_activity_metadata(
        {
            "source_count": 6,
            "success": True,
            "tool_call_id": "private",
            "arguments": {"token": "secret"},
        }
    ) == {"source_count": 6, "success": True}


def test_activity_metadata_reduces_tool_names_to_safe_categories():
    assert sanitize_activity_metadata(
        {"tool_kind": "mcp__github__get_file_contents", "reason": "provider secret"}
    ) == {"tool_kind": "file_read"}


def test_search_failure_uses_truthful_recoverable_copy():
    assert (
        activity_display_message(
            CortexActivityType.SEARCH_COMPLETED,
            {"source_count": 0, "success": False},
        )
        == "Search unavailable; continuing…"
    )


def test_tool_display_names_are_safe_and_human_readable():
    assert safe_tool_activity_message("mcp__github__get_file_contents", mcp=True) == (
        "Using a connected service…"
    )
    assert safe_tool_activity_message("web_search") == "Searching the web…"


def test_activity_stream_sequences_events_and_deduplicates_replays():
    async def exercise() -> None:
        stream = ActivityStream(
            request_id="request-1",
            conversation_id="conversation-1",
            mode="compare",
            loop=asyncio.get_running_loop(),
        )
        signal = CortexActivitySignal(
            CortexActivityType.REQUEST_IN_PROGRESS,
            provider="openai",
            model="gpt-test",
        )

        first = stream.serialize(signal, index=1)
        duplicate = stream.serialize(signal, index=1)
        other_target = stream.serialize(signal, index=2)

        assert first is not None
        assert first["index"] == 1
        assert first["activity"]["sequence_number"] == 1
        assert duplicate is None
        assert other_target is not None
        assert other_target["activity"]["sequence_number"] == 2

    asyncio.run(exercise())
