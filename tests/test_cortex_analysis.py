import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

from server import cortex_analysis as analysis_service
from server.dependencies import AuthResult
from server.routes import cortex_analysis as analysis_route


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
            "than Response A. Responses A, B, and C use different evidence. "
            "C offers another perspective."
        ),
        label_map,
    )

    assert restored == (
        "Claude (Sonnet 4.5) and Gemini (2.5 Pro) emphasize specific outcomes more "
        "strongly than ChatGPT (GPT-5.4). ChatGPT (GPT-5.4), Claude (Sonnet 4.5), "
        "and Gemini (2.5 Pro) use different evidence. Gemini (2.5 Pro) offers another perspective."
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
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "This changes the emphasis: A suggests a small cumulative advantage, "
            "while B suggests a more balanced year.",
            "This changes the emphasis: ChatGPT (GPT-6.1 Sol) suggests a small "
            "cumulative advantage, while Claude (Sonnet 5.5) suggests a more balanced year.",
        ),
        (
            "A argues for caution; B emphasizes uncertainty. A and B agree on scope.",
            "ChatGPT (GPT-6.1 Sol) argues for caution; Claude (Sonnet 5.5) emphasizes "
            "uncertainty. ChatGPT (GPT-6.1 Sol) and Claude (Sonnet 5.5) agree on scope.",
        ),
        (
            "A's estimate is higher, whereas B\u2019s estimate is lower.",
            "ChatGPT (GPT-6.1 Sol)'s estimate is higher, whereas "
            "Claude (Sonnet 5.5)\u2019s estimate is lower.",
        ),
        (
            "Response A and B disagree on the net total.",
            "ChatGPT (GPT-6.1 Sol) and Claude (Sonnet 5.5) disagree on the net total.",
        ),
        (
            "A cautious answer mentions vitamin A and B, a Type C connector and plan B. "
            "Grade A suggests high quality. C suggests an unlisted position.",
            "A cautious answer mentions vitamin A and B, a Type C connector and plan B. "
            "Grade A suggests high quality. C suggests an unlisted position.",
        ),
    ],
)
def test_short_response_references_use_model_names_without_replacing_ordinary_letters(
    text, expected
):
    label_map = {
        "Response A": analysis_service.AnalysisSource(
            "openai-response", 1, "openai", "gpt-6.1-sol", "An answer."
        ),
        "Response B": analysis_service.AnalysisSource(
            "claude-response", 1, "claude", "claude-sonnet-5-5", "Another answer."
        ),
    }
    assert analysis_service._restore_response_labels(text, label_map) == expected


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
