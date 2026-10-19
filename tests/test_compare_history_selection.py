"""Selective history must preserve user constraints and cross-answer references."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from models.unified_response import TokenUsage, UnifiedResponse
from orchestrator.compare_history import select_compare_messages
from orchestrator.core import CortexOrchestrator
from orchestrator.multi_orchestrator import MultiModelOrchestrator
from server.schemas.requests import ConversationHistoryItem, UserContextRequest
from server.utils import validate_and_trim_context


def messages():
    variants = [
        {
            "provider": "openai",
            "model": "gpt-6-astra",
            "content": "OpenAI full answer: use plan A.",
        },
        {
            "provider": "gemini",
            "model": "gemini-4-argon",
            "content": "Gemini full answer: use plan B.",
        },
        {
            "provider": "claude",
            "model": "claude-opus-5-5",
            "content": "Claude full answer: use plan C.",
        },
    ]
    return [
        {"role": "system", "content": "Preserve evidence and constraints."},
        {"role": "user", "content": "Budget $500, deadline October 31. Launch in Canada."},
        {
            "role": "assistant",
            "content": "\n\n".join(v["content"] for v in variants),
            "response_variants": variants,
        },
        {"role": "user", "content": "Expand the marketing strategy."},
    ]


@pytest.mark.parametrize(
    "provider,model,index",
    [
        ("openai", "gpt-6-astra", 0),
        ("gemini", "gemini-4-argon", 1),
        ("claude", "claude-opus-5-5", 2),
    ],
)
def test_each_target_receives_own_complete_answer_and_all_user_constraints(
    monkeypatch, provider, model, index
):
    monkeypatch.setenv("COMPARE_SELECTIVE_HISTORY_ENABLED", "true")
    original = messages()
    before = deepcopy(original)
    selected = select_compare_messages(
        original, prompt=original[-1]["content"], provider=provider, model=model
    )
    assert selected[0] == original[0]
    assert selected[1] == original[1]
    assert selected[-1] == original[-1]
    assert selected[2] == {
        "role": "assistant",
        "content": original[2]["response_variants"][index]["content"],
    }
    assert original == before
    assert all("response_variants" not in item for item in selected)


@pytest.mark.parametrize(
    "prompt",
    [
        "Compare the recommendations.",
        "Explain the second one.",
        "Which answer is correct?",
        "Use what Gemini suggested.",
        "Combine their plans.",
        "Improve that.",
        "What about the $500 in the previous reply?",
        "Explain option 2.",
        "请继续分析",
        "What did Opus recommend?",
        "Expand the other plan.",
        "Which is better?",
        "A vs B",
    ],
)
def test_ambiguous_or_cross_model_references_keep_all_answers(prompt):
    original = messages()
    selected = select_compare_messages(
        original, prompt=prompt, provider="claude", model="claude-opus-5-5"
    )
    assert selected[2]["content"] == original[2]["content"]
    assert "response_variants" not in selected[2]


@pytest.mark.parametrize(
    "provider,model", [("claude", "new-model"), ("openai", "claude-opus-5-5"), ("", "")]
)
def test_changed_or_unknown_model_keeps_shared_history(provider, model):
    original = messages()
    selected = select_compare_messages(
        original, prompt="Expand marketing", provider=provider, model=model
    )
    assert selected[2]["content"] == original[2]["content"]


def test_rollback_and_legacy_clients_keep_shared_context(monkeypatch):
    original = messages()
    monkeypatch.setenv("COMPARE_SELECTIVE_HISTORY_ENABLED", "false")
    selected = select_compare_messages(
        original, prompt="Expand marketing", provider="claude", model="claude-opus-5-5"
    )
    assert selected[2]["content"] == original[2]["content"]
    monkeypatch.setenv("COMPARE_SELECTIVE_HISTORY_ENABLED", "true")
    legacy = [{k: v for k, v in m.items() if k != "response_variants"} for m in original]
    assert (
        select_compare_messages(
            legacy, prompt="Expand marketing", provider="claude", model="claude-opus-5-5"
        )
        == legacy
    )


def test_provenance_cannot_add_hidden_or_unbounded_history():
    bad = messages()[2]
    bad["content"] = "Only the visible bounded excerpt"
    with pytest.raises(ValidationError, match="reproduce"):
        ConversationHistoryItem.model_validate(bad)
    # Direct callers also cannot replace the visible content from mismatched variants.
    assert (
        select_compare_messages(
            [bad], prompt="Expand marketing", provider="claude", model="claude-opus-5-5"
        )[0]["content"]
        == bad["content"]
    )


def test_trimming_cannot_restore_large_variant_text(monkeypatch):
    monkeypatch.setenv("CONTEXT_COMPACTION_ENABLED", "false")
    variants = [
        {"provider": "claude", "model": "claude-opus-5-5", "content": "x" * 21000},
        {"provider": "openai", "model": "gpt-6-astra", "content": "Other"},
    ]
    context = UserContextRequest(
        conversation_history=[
            ConversationHistoryItem(
                role="assistant",
                content="\n\n".join(v["content"] for v in variants),
                response_variants=variants,
            )
        ]
    )
    trimmed = validate_and_trim_context(context).conversation_history[0]
    assert len(trimmed.content) <= 20000
    assert trimmed.response_variants is None


def response(provider="claude", model="claude-opus-5-5"):
    return UnifiedResponse(
        request_id="test-response",
        latency_ms=1,
        provider=provider,
        model=model,
        text="Complete result",
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        estimated_cost=0.001,
    )


@pytest.mark.parametrize("prepared", [False, True])
def test_streaming_target_or_regeneration_path_selects_before_provider_call(monkeypatch, prepared):
    from models.user_context import UserContext
    from server.routes.chat import _build_user_context as chat_context
    from server.routes.compare import _build_user_context as compare_context

    history = messages()[1:-1]
    request_context = UserContextRequest(conversation_history=history)
    for builder in (chat_context, compare_context):
        assert builder(request_context).conversation_history[1]["response_variants"]
    orchestrator = object.__new__(CortexOrchestrator)
    client = Mock()
    client.get_completion.return_value = response()
    monkeypatch.setattr(orchestrator, "_validate_explicit_model_selection", lambda *_: (True, None))
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_, **__: client)
    context = UserContext(conversation_history=history)
    prepared_messages = messages()
    monkeypatch.setattr(
        orchestrator,
        "prepare_messages_for_turn",
        lambda **_: {
            "messages": prepared_messages,
            "prompt": "Expand marketing",
            "research_metadata": {},
            "optimization_metadata": {},
        },
    )
    kwargs = (
        {"_prepared_messages": prepared_messages, "_prepared_prompt": "Expand marketing"}
        if prepared
        else {}
    )
    result = orchestrator.ask(
        "Expand marketing",
        model_type="claude",
        model_name="claude-opus-5-5",
        context=context,
        **kwargs,
    )
    assert result.is_success
    seen = client.get_completion.call_args.kwargs["messages"]
    assert seen[2]["content"] == "Claude full answer: use plan C."
    assert all("response_variants" not in item for item in seen)


def test_nonstream_compare_selects_independently_for_each_target():
    clients = []
    for provider, model in [("claude", "claude-opus-5-5"), ("gemini", "gemini-4-argon")]:
        client = Mock(provider_name=provider, model_name=model, requested_model_name=None)
        client.get_completion.return_value = response(provider, model)
        clients.append(client)
    result = MultiModelOrchestrator().get_comparisons_sync(
        "Expand marketing", clients, messages=messages()
    )
    assert result.success_count == 2
    assert (
        clients[0].get_completion.call_args.kwargs["messages"][2]["content"]
        == "Claude full answer: use plan C."
    )
    assert (
        clients[1].get_completion.call_args.kwargs["messages"][2]["content"]
        == "Gemini full answer: use plan B."
    )
