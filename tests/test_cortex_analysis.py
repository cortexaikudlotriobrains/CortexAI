import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from models.unified_response import TokenUsage, UnifiedResponse
from server import cortex_analysis as analysis_service
from server.dependencies import AuthResult
from server.routes import cortex_analysis as analysis_route


class _FakeOpenAIClient:
    captured_messages = None
    captured_kwargs = None

    def __init__(self, api_key: str, model_name: str):
        assert api_key == "test-openai-key"
        assert model_name == "gpt-5.4-mini"

    def get_completion(self, *, messages, **kwargs):
        self.__class__.captured_messages = messages
        self.__class__.captured_kwargs = kwargs
        payload = json.loads(messages[1]["content"])
        first_label = payload["responses"][0]["label"]
        return UnifiedResponse(
            request_id="analysis-provider-request",
            text=json.dumps(
                {
                    "recommendedAnswer": ("Use Response A's recommendation as a starting point."),
                    "agreements": ["Responses A and B emphasize the same constraint."],
                    "disagreements": [
                        {
                            "who": "Response B",
                            "text": "Emphasizes outcomes more strongly than Response A.",
                        }
                    ],
                    "disagreementNote": "These are different priorities, not a verdict.",
                    "uniqueInsights": [
                        {
                            "responseLabel": first_label,
                            "text": ("Response B noticed an implementation dependency."),
                        }
                    ],
                    "confidence": {
                        "level": "moderate",
                        "reason": (
                            "Response A and Response B align, but external details "
                            "still need checking."
                        ),
                    },
                    "verify": ["Confirm Response B's implementation constraint."],
                    "highStakesDomain": None,
                }
            ),
            provider="openai",
            model="gpt-5.4-mini",
            latency_ms=20,
            token_usage=TokenUsage(
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            ),
            estimated_cost=0.001,
            finish_reason="stop",
            error=None,
        )


class _StrongDisagreementClient(_FakeOpenAIClient):
    def get_completion(self, *, messages, **kwargs):
        response = super().get_completion(messages=messages, **kwargs)
        return replace(
            response,
            text=json.dumps(
                {
                    "recommendedAnswer": "The verified winner scored 9/10, so use the first path.",
                    "agreements": [],
                    "disagreements": [
                        {
                            "who": "Response A",
                            "text": "Recommends a path incompatible with Response B.",
                        }
                    ],
                    "disagreementNote": None,
                    "uniqueInsights": [],
                    "confidence": {
                        "level": "limited",
                        "reason": "The responses disagree.",
                    },
                    "verify": [],
                    "highStakesDomain": None,
                }
            ),
        )


@pytest.mark.unit
def test_analysis_shuffles_anonymous_labels_and_restores_attribution(
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(analysis_service, "OpenAIClient", _FakeOpenAIClient)
    sources = [
        analysis_service.AnalysisSource(
            request_id="openai-response",
            response_version=1,
            provider="openai",
            model="gpt-5.1",
            content="First neutral response.",
        ),
        analysis_service.AnalysisSource(
            request_id="claude-response",
            response_version=1,
            provider="claude",
            model="claude-sonnet-4-5",
            content="Second neutral response.",
        ),
    ]

    result = analysis_service.analyze_responses(
        question="Which approach should I use?",
        sources=sources,
        rng=__import__("random").Random(4),
    )

    sent_payload = json.loads(_FakeOpenAIClient.captured_messages[1]["content"])
    assert set(sent_payload["responses"][0]) == {"label", "content"}
    assert {item["label"] for item in sent_payload["responses"]} == {"Response A", "Response B"}
    assert "openai" not in _FakeOpenAIClient.captured_messages[1]["content"].lower()
    assert "claude" not in _FakeOpenAIClient.captured_messages[1]["content"].lower()
    assert _FakeOpenAIClient.captured_kwargs["model"] == "gpt-5.4-mini"
    assert _FakeOpenAIClient.captured_kwargs["response_format"]["type"] == "json_schema"
    response_schema = _FakeOpenAIClient.captured_kwargs["response_format"]["json_schema"]["schema"]
    assert response_schema["properties"]["disagreements"]["items"]["required"] == [
        "who",
        "text",
    ]
    assert "disagreementNote" in response_schema["required"]

    provider_by_label = {
        item["label"]: (
            "ChatGPT (GPT-5.1)" if item["content"].startswith("First") else "Claude (Sonnet 4.5)"
        )
        for item in sent_payload["responses"]
    }
    expected_name = provider_by_label[sent_payload["responses"][0]["label"]]
    assert result.unique_insights == [
        {
            "responseName": expected_name,
            "text": (f"{provider_by_label['Response B']} noticed an implementation " "dependency."),
        }
    ]
    assert result.recommended_answer == (
        f"Use {provider_by_label['Response A']}'s recommendation as a starting point."
    )
    assert result.agreements == [
        (
            f"{provider_by_label['Response A']} and "
            f"{provider_by_label['Response B']} emphasize the same constraint."
        )
    ]
    assert result.disagreements == [
        {
            "who": provider_by_label["Response B"],
            "text": (
                "Emphasizes outcomes more strongly than "
                f"{provider_by_label['Response A']}."
            ),
        }
    ]
    assert result.disagreement_note == "These are different priorities, not a verdict."
    assert result.confidence_reason.startswith(
        f"{provider_by_label['Response A']} and {provider_by_label['Response B']} align"
    )
    assert result.verify_items == [
        f"Confirm {provider_by_label['Response B']}'s implementation constraint."
    ]
    all_visible_copy = " ".join(
        [
            result.recommended_answer,
            *result.agreements,
            *(item["who"] for item in result.disagreements),
            *(item["text"] for item in result.disagreements),
            result.disagreement_note or "",
            *(item["text"] for item in result.unique_insights),
            result.confidence_reason,
            *result.verify_items,
        ]
    )
    assert "Response A" not in all_visible_copy
    assert "Response B" not in all_visible_copy
    assert result.total_tokens == 150


@pytest.mark.unit
def test_analysis_enforces_strong_disagreement_and_trust_copy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        analysis_service,
        "OpenAIClient",
        _StrongDisagreementClient,
    )
    sources = [
        analysis_service.AnalysisSource(
            request_id="response-a",
            response_version=1,
            provider="openai",
            model="gpt-5.1",
            content="Use path A.",
        ),
        analysis_service.AnalysisSource(
            request_id="response-b",
            response_version=1,
            provider="claude",
            model="claude-sonnet-4-5",
            content="Use path B.",
        ),
    ]

    result = analysis_service.analyze_responses(
        question="Which path?",
        sources=sources,
    )

    assert result.recommended_answer.startswith(
        "The responses differ too much for Cortex to choose for you."
    )
    normalized = result.recommended_answer.lower()
    assert "verified" not in normalized
    assert "winner" not in normalized
    assert "9/10" not in normalized


@pytest.mark.unit
def test_source_fingerprint_tracks_response_versions():
    sources = [
        analysis_service.AnalysisSource(
            request_id="response-a",
            response_version=1,
            provider="openai",
            model="gpt-5.1",
            content="A",
        ),
        analysis_service.AnalysisSource(
            request_id="response-b",
            response_version=1,
            provider="claude",
            model="claude-sonnet-4-5",
            content="B",
        ),
    ]
    original = analysis_service.source_fingerprint(sources)
    regenerated = analysis_service.source_fingerprint(
        [
            sources[0],
            analysis_service.AnalysisSource(**{**sources[1].__dict__, "response_version": 2}),
        ]
    )
    reordered = analysis_service.source_fingerprint(list(reversed(sources)))

    assert original != regenerated
    assert original == reordered


@pytest.mark.unit
def test_anonymous_response_references_are_restored_for_three_providers():
    label_map = {
        "Response A": analysis_service.AnalysisSource(
            request_id="response-a",
            response_version=1,
            provider="openai",
            model="gpt-5.4",
            content="A",
        ),
        "Response B": analysis_service.AnalysisSource(
            request_id="response-b",
            response_version=1,
            provider="claude",
            model="claude-sonnet-4-5",
            content="B",
        ),
        "Response C": analysis_service.AnalysisSource(
            request_id="response-c",
            response_version=1,
            provider="gemini",
            model="gemini-2.5-pro",
            content="C",
        ),
    }

    restored = analysis_service._restore_response_labels(
        (
            "Response B and Response C emphasize specific outcomes more strongly "
            "than Response A. Responses A, B, and C use different evidence."
        ),
        label_map,
    )

    assert restored == (
        "Claude (Sonnet 4.5) and Gemini (2.5 Pro) emphasize specific outcomes more "
        "strongly than ChatGPT (GPT-5.4). ChatGPT (GPT-5.4), Claude (Sonnet 4.5), "
        "and Gemini (2.5 Pro) use different evidence."
    )


@pytest.mark.unit
def test_anonymous_response_references_distinguish_models_from_same_provider():
    label_map = {
        "Response A": analysis_service.AnalysisSource(
            request_id="sonnet-response",
            response_version=1,
            provider="claude",
            model="claude-sonnet-4-5",
            content="A",
        ),
        "Response B": analysis_service.AnalysisSource(
            request_id="opus-response",
            response_version=1,
            provider="claude",
            model="claude-opus-4-6",
            content="B",
        ),
    }

    restored = analysis_service._restore_response_labels(
        "Response A emphasizes speed; Response B emphasizes depth. Responses A and B agree.",
        label_map,
    )

    assert restored == (
        "Claude (Sonnet 4.5) emphasizes speed; Claude (Opus 4.6) emphasizes depth. "
        "Claude (Sonnet 4.5) and Claude (Opus 4.6) agree."
    )
    assert [
        item["responseName"] for item in analysis_service.source_snapshot(list(label_map.values()))
    ] == [
        "Claude (Sonnet 4.5)",
        "Claude (Opus 4.6)",
    ]


@pytest.mark.unit
def test_create_analysis_route_appends_and_returns_saved_run(monkeypatch):
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    session_id = UUID("22222222-2222-2222-2222-222222222222")
    group_id = UUID("33333333-3333-3333-3333-333333333333")
    resolution = SimpleNamespace(user_id=user_id, api_key_id=None)
    source_payload = {
        "session_id": session_id,
        "question": "Choose an approach",
        "responses": [
            {
                "request_id": "response-a",
                "response_version": 1,
                "provider": "openai",
                "model": "gpt-5.1",
                "content": "A",
                "error_message": None,
            },
            {
                "request_id": "response-b",
                "response_version": 1,
                "provider": "claude",
                "model": "claude-sonnet-4-5",
                "content": "B",
                "error_message": None,
            },
        ],
    }
    generated = analysis_service.AnalysisResult(
        recommended_answer="Use the lower-risk approach.",
        agreements=["Both responses prefer a staged rollout."],
        disagreements=[
            {
                "who": "ChatGPT (GPT-5.1)",
                "text": "Leans toward the lower-risk approach.",
            }
        ],
        disagreement_note="These are different risk preferences.",
        unique_insights=[],
        confidence_level="moderate",
        confidence_reason="The responses align on the main tradeoff.",
        verify_items=["Confirm the rollout constraint."],
        high_stakes_domain=None,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        estimated_cost=0.001,
    )
    saved_run = {
        "analysis_id": "44444444-4444-4444-4444-444444444444",
        "request_group_id": str(group_id),
        "session_id": str(session_id),
        "model": "gpt-5.4-mini",
        "source_fingerprint": analysis_service.source_fingerprint(
            analysis_service.normalize_analysis_sources(source_payload["responses"])[0]
        ),
        "source_snapshot": [
            {
                "requestId": "response-a",
                "responseVersion": 1,
                "responseName": "ChatGPT (GPT-5.1)",
            },
            {
                "requestId": "response-b",
                "responseVersion": 1,
                "responseName": "Claude (Sonnet 4.5)",
            },
        ],
        "recommended_answer": generated.recommended_answer,
        "agreements": generated.agreements,
        "disagreements": generated.disagreements,
        "disagreement_note": generated.disagreement_note,
        "unique_insights": [],
        "confidence_level": "moderate",
        "confidence_reason": generated.confidence_reason,
        "verify_items": generated.verify_items,
        "high_stakes_domain": None,
        "combined_response_count": 2,
        "failed_response_count": 0,
        "prompt_tokens": generated.prompt_tokens,
        "completion_tokens": generated.completion_tokens,
        "total_tokens": generated.total_tokens,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "pricing_snapshot": {},
        "created_at": "2026-07-27T12:00:00Z",
    }
    created = {}

    @contextmanager
    def fake_uow(*, commit_on_success=True):
        yield object()

    monkeypatch.setattr(analysis_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(
        analysis_route,
        "_require_analysis_schema",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(analysis_route, "_db_uow", fake_uow)
    monkeypatch.setattr(analysis_route, "_resolve_identity", lambda **kwargs: resolution)
    monkeypatch.setattr(
        analysis_route,
        "get_compare_analysis_sources",
        lambda *args, **kwargs: source_payload,
    )
    monkeypatch.setattr(
        analysis_route.analysis_service,
        "analyze_responses",
        lambda **kwargs: generated,
    )
    billing_reservation = SimpleNamespace()
    reserved = {}
    finalized = {}

    def fake_reserve(**kwargs):
        reserved.update(kwargs)
        return billing_reservation

    monkeypatch.setattr(
        analysis_route.persistence_service,
        "reserve_subscription_usage",
        fake_reserve,
    )
    monkeypatch.setattr(
        analysis_route.persistence_service,
        "finalize_subscription_usage",
        lambda **kwargs: finalized.update(kwargs),
    )

    def fake_create(*args, **kwargs):
        created.update(kwargs)
        return UUID(saved_run["analysis_id"])

    monkeypatch.setattr(analysis_route, "create_cortex_analysis_run", fake_create)
    monkeypatch.setattr(
        analysis_route,
        "list_cortex_analysis_runs",
        lambda *args, **kwargs: [saved_run],
    )
    request = SimpleNamespace(state=SimpleNamespace(request_id="route-request"))
    auth = AuthResult(api_key=None, cognito_claims=None, user_id=user_id)

    result = asyncio.run(
        analysis_route.create_analysis_run(
            request_group_id=group_id,
            request=request,
            auth=auth,
        )
    )

    assert result.analysis_id == saved_run["analysis_id"]
    assert result.is_stale is False
    assert created["recommended_answer"] == generated.recommended_answer
    assert created["disagreements"] == generated.disagreements
    assert created["disagreement_note"] == generated.disagreement_note
    assert result.disagreements[0].who == "ChatGPT (GPT-5.1)"
    assert result.disagreement_note == "These are different risk preferences."
    assert result.ai_credits == 180
    assert result.credit_usage_estimated is False
    assert created["combined_response_count"] == 2
    assert reserved["operation_type"] == "cortex_analysis"
    assert reserved["research_enabled"] is False
    assert reserved["initial_query"] == "Choose an approach"
    assert reserved["max_output_tokens"] == 1800
    assert "Choose an approach" in reserved["input_text"]
    assert "\nA\nB" in reserved["input_text"]
    assert finalized["reservation"] is billing_reservation
    assert finalized["research_provider_credits_used"] == 0
    assert finalized["model_usages"][0].input_tokens == 10
    assert finalized["model_usages"][0].output_tokens == 20


@pytest.mark.unit
def test_run_dto_keeps_legacy_flat_disagreements_readable():
    dto = analysis_route._run_to_dto(
        {
            "analysis_id": "44444444-4444-4444-4444-444444444444",
            "request_group_id": "33333333-3333-3333-3333-333333333333",
            "session_id": "22222222-2222-2222-2222-222222222222",
            "model": "gpt-5.4-mini",
            "recommended_answer": "Use a staged approach.",
            "agreements": [],
            "disagreements": ["A legacy saved position."],
            "disagreement_note": None,
            "unique_insights": [],
            "confidence_level": "limited",
            "confidence_reason": "The saved responses differ.",
            "verify_items": [],
            "high_stakes_domain": None,
            "source_fingerprint": "fingerprint",
            "source_snapshot": [],
            "combined_response_count": 2,
            "failed_response_count": 0,
            "created_at": "2026-07-27T12:00:00Z",
        },
        current_fingerprint="fingerprint",
    )

    assert dto.disagreements[0].who == "One response"
    assert dto.disagreements[0].text == "A legacy saved position."


@pytest.mark.unit
def test_identical_analysis_reuse_skips_provider_and_credit_reservation(monkeypatch):
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    session_id = UUID("22222222-2222-2222-2222-222222222222")
    group_id = UUID("33333333-3333-3333-3333-333333333333")
    source_payload = {
        "session_id": session_id,
        "question": "Choose an approach",
        "responses": [
            {
                "request_id": "a",
                "response_version": 1,
                "provider": "openai",
                "model": "gpt-5.1",
                "content": "A",
                "error_message": None,
            },
            {
                "request_id": "b",
                "response_version": 1,
                "provider": "claude",
                "model": "claude-sonnet-4-5",
                "content": "B",
                "error_message": None,
            },
        ],
    }
    sources, _ = analysis_service.normalize_analysis_sources(source_payload["responses"])
    reusable = {
        "analysis_id": "44444444-4444-4444-4444-444444444444",
        "request_group_id": str(group_id),
        "session_id": str(session_id),
        "model": analysis_service.configured_analysis_model(),
        "source_fingerprint": analysis_service.source_fingerprint(sources),
        "analysis_policy_version": "cortex-analysis-v1",
        "source_snapshot": [],
        "recommended_answer": "Reuse this.",
        "agreements": [],
        "disagreements": [],
        "unique_insights": [],
        "confidence_level": "moderate",
        "confidence_reason": "Existing evidence.",
        "verify_items": [],
        "combined_response_count": 2,
        "failed_response_count": 0,
        "created_at": "2026-08-07T12:00:00Z",
    }

    @contextmanager
    def fake_uow(*, commit_on_success=True):
        yield object()

    monkeypatch.setenv("CORTEX_ANALYSIS_REUSE_ENABLED", "true")
    monkeypatch.setattr(analysis_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(analysis_route, "_require_analysis_schema", lambda **kwargs: None)
    monkeypatch.setattr(analysis_route, "_db_uow", fake_uow)
    monkeypatch.setattr(
        analysis_route,
        "_resolve_identity",
        lambda **kwargs: SimpleNamespace(user_id=user_id, api_key_id=None),
    )
    monkeypatch.setattr(
        analysis_route, "get_compare_analysis_sources", lambda *args, **kwargs: source_payload
    )
    monkeypatch.setattr(
        analysis_route, "list_cortex_analysis_runs", lambda *args, **kwargs: [reusable]
    )
    monkeypatch.setattr(
        analysis_route.analysis_service,
        "analyze_responses",
        lambda **kwargs: pytest.fail("reused analysis must not call the provider"),
    )
    monkeypatch.setattr(
        analysis_route.persistence_service,
        "reserve_subscription_usage",
        lambda **kwargs: pytest.fail("reused analysis must consume zero new credits"),
    )

    result = asyncio.run(
        analysis_route.create_analysis_run(
            request_group_id=group_id,
            request=SimpleNamespace(state=SimpleNamespace(request_id="reuse")),
            regenerate=False,
            auth=AuthResult(api_key=None, cognito_claims=None, user_id=user_id),
        )
    )
    assert result.analysis_id == reusable["analysis_id"]
    assert result.recommended_answer == "Reuse this."


@pytest.mark.unit
def test_list_analysis_runs_returns_503_when_migration_is_missing(monkeypatch):
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    session_id = UUID("22222222-2222-2222-2222-222222222222")
    request = SimpleNamespace(state=SimpleNamespace(request_id="schema-check"))
    auth = AuthResult(api_key=None, cognito_claims=None, user_id=user_id)

    def missing_schema():
        raise analysis_route.CortexAnalysisSchemaUnavailable("cortex_analysis_runs is missing")

    monkeypatch.setattr(analysis_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(
        analysis_route,
        "require_cortex_analysis_schema",
        missing_schema,
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            analysis_route.list_analysis_runs(
                request=request,
                session_id=session_id,
                request_group_id=None,
                auth=auth,
            )
        )

    assert captured.value.status_code == 503
    assert captured.value.detail == {
        "code": "cortex_analysis_schema_unavailable",
        "message": (
            "Cortex Analysis is temporarily unavailable because its database "
            "setup is incomplete."
        ),
    }


@pytest.mark.unit
def test_create_analysis_checks_schema_before_provider_work(monkeypatch):
    user_id = UUID("11111111-1111-1111-1111-111111111111")
    group_id = UUID("33333333-3333-3333-3333-333333333333")
    request = SimpleNamespace(state=SimpleNamespace(request_id="schema-check"))
    auth = AuthResult(api_key=None, cognito_claims=None, user_id=user_id)
    provider_called = False

    def missing_schema():
        raise analysis_route.CortexAnalysisSchemaUnavailable("cortex_analysis_runs is missing")

    def unexpected_provider_call(**kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("Provider work must not start without persistence schema")

    monkeypatch.setattr(analysis_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(
        analysis_route,
        "require_cortex_analysis_schema",
        missing_schema,
    )
    monkeypatch.setattr(
        analysis_route.analysis_service,
        "analyze_responses",
        unexpected_provider_call,
    )

    with pytest.raises(HTTPException) as captured:
        asyncio.run(
            analysis_route.create_analysis_run(
                request_group_id=group_id,
                request=request,
                auth=auth,
            )
        )

    assert captured.value.status_code == 503
    assert provider_called is False
