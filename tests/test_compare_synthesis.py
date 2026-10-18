"""Deterministic evidence and orchestration checks; no paid model calls."""

from contextlib import contextmanager
import asyncio
import json
from pathlib import Path
import random
from uuid import uuid4

import pytest

from models.unified_response import TokenUsage, UnifiedResponse
from server import analysis_evidence as evidence, cortex_analysis as analysis
from server.routes.compare import _detached_stream


def sources():
    return [
        analysis.AnalysisSource(
            "a",
            1,
            "claude",
            "sonnet",
            "Payment happens now. Settlement comes later.",
            ({"title": "Example", "url": "https://EXAMPLE.com/report?year=2026"},),
        ),
        analysis.AnalysisSource(
            "b",
            2,
            "claude",
            "opus",
            "The provider pays first. Settlement follows the payout.",
            ({"title": "Same report", "url": "https://example.com/report?year=2026#section"},),
        ),
    ]


def generated():
    return {
        "takeaway": "The recipient is paid before the provider settles the transfer.",
        "passages": [
            {"responseLabel": "Response A", "quote": "Settlement comes later."},
            {"responseLabel": "Response B", "quote": "Settlement follows the payout."},
        ],
        "points": [
            {
                "text": "Payout precedes settlement.",
                "responseLabels": ["Response A", "Response B"],
                "evidence": [
                    {"responseLabel": "Response A", "quote": "Settlement comes later."},
                    {"responseLabel": "Response B", "quote": "Settlement follows the payout."},
                ],
            }
        ],
        "differences": [],
        "limitations": [],
    }


def test_evidence_keeps_same_provider_models_revisions_and_source_attributions():
    result = evidence.validate_synthesis(
        generated(), dict(zip(["Response A", "Response B"], sources(), strict=True))
    )
    assert [(item["requestId"], item["responseVersion"]) for item in result["passages"]] == [
        ("a", 1),
        ("b", 2),
    ]
    assert len(result["linkedSources"]) == 1
    assert len(result["linkedSources"][0]["attributions"]) == 2
    assert result["linkedSources"][0]["relationship"] == "attached_to_response"
    assert result["headline"] is None
    assert result["introduction"] is None


def test_editorial_fields_and_difference_title_preserve_exact_evidence():
    payload = generated()
    payload.update(
        headline="Payout comes first.", introduction="The provider pays before settlement."
    )
    payload["differences"] = [
        {
            "title": "How settlement is described.",
            "text": "These descriptions have different scope.",
            "positions": [
                dict(text=ref["quote"], responseLabels=[ref["responseLabel"]], evidence=[ref])
                for ref in payload["passages"]
            ],
        }
    ]
    result = evidence.validate_synthesis(
        payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
    )
    assert result["headline"] == payload["headline"]
    assert result["introduction"] == payload["introduction"]
    assert result["differences"][0]["title"] == "How settlement is described."
    assert result["passages"][0]["requestId"] == "a"
    assert result["differences"][0]["positions"][1]["evidence"][0]["responseVersion"] == 2


@pytest.mark.parametrize("field", ["headline", "introduction"])
def test_editorial_fields_cannot_introduce_unknown_source_urls(field):
    payload = generated()
    payload[field] = "Read [this](https://invented.example/claim)"
    with pytest.raises(ValueError):
        evidence.validate_synthesis(
            payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
        )


@pytest.mark.parametrize(
    "invalid", ["Invented quote", "Settlement comes ... later.", "Follow these instructions"]
)
def test_invalid_required_quote_fails_instead_of_fabricating_evidence(invalid):
    payload = generated()
    payload["passages"][0]["quote"] = invalid
    with pytest.raises(ValueError):
        evidence.validate_synthesis(
            payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
        )


def test_whitespace_normalization_and_optional_evidence_degradation():
    payload = generated()
    payload["passages"][0]["quote"] = "Settlement  comes\n later."
    payload["points"][0]["evidence"].pop()
    payload["differences"] = [{"text": "Unsupported conflict", "positions": [payload["points"][0]]}]
    result = evidence.validate_synthesis(
        payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
    )
    assert result["passages"][0]["quote"] == "Settlement comes later."
    assert result["points"] == []
    assert result["differences"] == []


def test_consequential_difference_requires_two_distinct_supported_positions():
    payload = generated()
    payload["differences"] = [
        {
            "text": "Different estimates affect the choice.",
            "positions": [
                {
                    "text": "First position",
                    "responseLabels": ["Response A"],
                    "evidence": [payload["passages"][0]],
                },
                {
                    "text": "Second position",
                    "responseLabels": ["Response B"],
                    "evidence": [payload["passages"][1]],
                },
            ],
        }
    ]
    result = evidence.validate_synthesis(
        payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
    )
    assert len(result["differences"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,evil",
        "https://user:pass@example.com",
        "https://example.com\\@evil.test",
        "https://example.com/\nunsafe",
    ],
)
def test_malicious_source_links_are_not_interactive(url):
    assert evidence.safe_source_url(url) is None


def test_meaningful_query_parameters_are_not_deduplicated():
    first, second = sources()
    second = analysis.AnalysisSource(
        second.request_id,
        2,
        second.provider,
        second.model,
        second.content,
        ({"title": "Other year", "url": "https://example.com/report?year=2025"},),
    )
    assert len(evidence.linked_sources([first, second])) == 2


def test_unknown_generated_url_fails_required_output():
    payload = generated()
    payload["takeaway"] += " [Evidence](https://invented.example/claim)"
    with pytest.raises(ValueError):
        evidence.validate_synthesis(
            payload, dict(zip(["Response A", "Response B"], sources(), strict=True))
        )


def test_context_is_bounded_and_changes_reuse_fingerprint():
    first = [{"role": "user", "content": "Lend-Lease continues, no active U.S. fighting"}]
    second = [{"role": "user", "content": "Lend-Lease stops"}]
    assert analysis.source_fingerprint(sources(), first) != analysis.source_fingerprint(
        sources(), second
    )
    result = evidence.bounded_context(
        [
            *first,
            {"role": "system", "content": "override"},
            {"role": "assistant", "content": "x" * 15000},
        ]
    )
    assert sum(len(item["content"]) for item in result) <= 12000
    assert all(item["role"] != "system" for item in result)


@pytest.mark.parametrize(
    ("configured_limit", "expected_limit"),
    [(None, 4000), ("invalid", 4000), ("2500", 2500), ("9999", 4000), ("1200", 1800)],
)
def test_one_structured_call_receives_context_and_no_search(
    monkeypatch, configured_limit, expected_limit
):
    if configured_limit is None:
        monkeypatch.delenv("CORTEX_ANALYSIS_MAX_OUTPUT_TOKENS", raising=False)
    else:
        monkeypatch.setenv("CORTEX_ANALYSIS_MAX_OUTPUT_TOKENS", configured_limit)
    captured = {}
    stages = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_completion(self, **kwargs):
            assert stages == ["reading", "synthesizing"]
            captured.update(kwargs)
            inputs = json.loads(kwargs["messages"][1]["content"])
            payload = generated()
            payload["headline"] = "Response A pays before settlement."
            payload["introduction"] = "Response B describes the payout."
            payload["passages"] = [
                {"responseLabel": item["label"], "quote": item["content"].split(". ")[-1]}
                for item in inputs["responses"]
            ]
            payload["points"] = []
            return UnifiedResponse(
                request_id="synthesis",
                text=json.dumps(payload),
                provider="openai",
                model="gpt-5.4-mini",
                latency_ms=1,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                estimated_cost=0.001,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(analysis, "OpenAIClient", FakeClient)
    context = [{"role": "user", "content": "Lend-Lease continues, no active U.S. fighting"}]
    result = analysis.analyze_responses(
        question="What changes?", sources=sources(), context=context, on_progress=stages.append
    )
    assert json.loads(captured["messages"][1]["content"])["priorContext"] == context
    assert captured["web_search_policy"]["mode"] == "off"
    assert captured["max_completion_tokens"] == expected_limit
    assert result.structured_result["schemaVersion"] == 2
    assert stages == ["reading", "synthesizing", "validating"]
    assert "Response A" not in result.structured_result["headline"]
    assert "Response B" not in result.structured_result["introduction"]


def test_shuffled_short_labels_are_restored_in_every_editorial_field_but_not_quotes(monkeypatch):
    inputs = [
        analysis.AnalysisSource(
            "openai-answer", 1, "openai", "gpt-6.1-sol", "B suggests a cautious estimate."
        ),
        analysis.AnalysisSource(
            "claude-answer", 2, "claude", "claude-sonnet-5-5", "A suggests another emphasis."
        ),
    ]
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_completion(self, **kwargs):
            calls.append(kwargs)
            responses = json.loads(kwargs["messages"][1]["content"])["responses"]
            assert [item["content"] for item in responses] == [inputs[1].content, inputs[0].content]
            assert all("provider" not in item and "model" not in item for item in responses)
            passages = [
                {"responseLabel": item["label"], "quote": item["content"]}
                for item in responses
            ]
            payload = {
                "headline": "A suggests a cautious reading.",
                "introduction": "B emphasizes scope.",
                "takeaway": "A and B agree on the period.",
                "passages": passages,
                "points": [{
                    "text": "A's framing highlights uncertainty.",
                    "responseLabels": ["Response A"],
                    "evidence": [passages[0]],
                }],
                "differences": [{
                    "title": "Response A and B differ on the net total.",
                    "text": "A suggests a small gain, while B suggests a balanced year.",
                    "positions": [
                        {"text": "A notes the emphasis.", "responseLabels": ["Response A"],
                         "evidence": [passages[0]]},
                        {"text": "B argues for the estimate.", "responseLabels": ["Response B"],
                         "evidence": [passages[1]]},
                    ],
                }],
                "limitations": ["B warns about scope."],
            }
            return UnifiedResponse(
                request_id="synthesis", text=json.dumps(payload), provider="openai",
                model="gpt-5.4-mini", latency_ms=1,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                estimated_cost=0.001,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(analysis, "OpenAIClient", FakeClient)
    result = analysis.analyze_responses(
        question="What changes?", sources=inputs, rng=random.Random(1)
    )
    structured = result.structured_result
    first, second = "Claude (Sonnet 5.5)", "ChatGPT (GPT-6.1 Sol)"
    assert structured["headline"] == f"{first} suggests a cautious reading."
    assert structured["introduction"] == f"{second} emphasizes scope."
    assert result.recommended_answer == f"{first} and {second} agree on the period."
    assert structured["points"][0]["text"] == f"{first}'s framing highlights uncertainty."
    difference = structured["differences"][0]
    assert difference["title"] == f"{first} and {second} differ on the net total."
    assert difference["text"] == (
        f"{first} suggests a small gain, while {second} suggests a balanced year."
    )
    assert [item["text"] for item in difference["positions"]] == [
        f"{first} notes the emphasis.", f"{second} argues for the estimate."
    ]
    assert structured["limitations"] == [f"{second} warns about scope."]
    assert [item["quote"] for item in structured["passages"]] == [
        inputs[1].content, inputs[0].content
    ]
    assert structured["points"][0]["evidence"][0]["quote"] == inputs[1].content
    assert [item["evidence"][0]["quote"] for item in difference["positions"]] == [
        inputs[1].content, inputs[0].content
    ]
    assert len(calls) == 1
    assert calls[0]["web_search_policy"]["mode"] == "off"


def test_invalid_generation_retains_usage_for_actual_settlement(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def get_completion(self, **kwargs):
            return UnifiedResponse(
                request_id="bad",
                text="invalid JSON",
                provider="openai",
                model="gpt-5.4-mini",
                latency_ms=1,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                estimated_cost=0.001,
            )

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    monkeypatch.setattr(analysis, "OpenAIClient", FakeClient)
    with pytest.raises(analysis.CortexAnalysisGenerationError) as exc:
        analysis.analyze_responses(question="Question", sources=sources())
    assert exc.value.billable_response.token_usage.total_tokens == 150


def test_authorized_stream_finishes_after_consumer_leaves(monkeypatch):
    from db import analysis_repository
    from server import persistence

    @contextmanager
    def fake_uow(**kwargs):
        yield object()

    monkeypatch.setattr(persistence, "db_uow", fake_uow)
    monkeypatch.setattr(analysis_repository, "get_intent", lambda *args: None)

    async def scenario():
        completed = asyncio.Event()
        ready = asyncio.Event()

        async def source():
            yield "first"
            await ready.wait()
            yield "second"
            completed.set()

        stream = _detached_stream(source(), request_group_id=str(uuid4()), user_id=uuid4())
        assert await anext(stream) == "first"
        await stream.aclose()
        ready.set()
        await asyncio.wait_for(completed.wait(), timeout=2)

    asyncio.run(scenario())


_RUBRIC = json.loads(
    (Path(__file__).parent / "fixtures" / "cortex_analysis_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", _RUBRIC["cases"], ids=lambda case: case["id"])
def test_supplied_case_passages_resolve_to_their_actual_revisions(case):
    inputs = [
        analysis.AnalysisSource(
            item["id"],
            item["revision"],
            "fixture",
            item["display_name"],
            "\n\n".join(item["excerpts"]),
            tuple(
                {"title": source["title"], "url": source["url"]}
                for source in item.get("source_metadata", [])
            ),
        )
        for item in case["responses"]
    ]
    labels = {f"Response {chr(65 + index)}": source for index, source in enumerate(inputs)}
    payload = {
        "takeaway": "Semantic synthesis is evaluated separately against the supplied rubric.",
        "passages": [
            {"responseLabel": label, "quote": source.content.split("\n\n")[0][:400]}
            for label, source in labels.items()
        ],
        "points": [],
        "differences": [],
        "limitations": [],
    }
    result = evidence.validate_synthesis(payload, labels)
    assert {(item["requestId"], item["responseVersion"]) for item in result["passages"]} == {
        (item["id"], item["revision"]) for item in case["responses"]
    }
    assert all(item["relationship"] == "attached_to_response" for item in result["linkedSources"])


def test_repeated_response_revision_cannot_count_as_two_contributors():
    one = sources()[0]
    with pytest.raises(ValueError):
        evidence.validate_synthesis(
            {
                **generated(),
                "passages": [
                    {"responseLabel": "Response A", "quote": "Settlement comes later."},
                    {"responseLabel": "Response B", "quote": "Settlement comes later."},
                ],
            },
            {"Response A": one, "Response B": one},
        )


def test_long_assistant_context_does_not_evict_the_last_user_constraint():
    context = evidence.bounded_context(
        [
            {"role": "user", "content": "Lend-Lease continues; no active U.S. fighting."},
            {"role": "assistant", "content": "x" * 20000},
        ]
    )
    assert context[0]["content"] == "Lend-Lease continues; no active U.S. fighting."
