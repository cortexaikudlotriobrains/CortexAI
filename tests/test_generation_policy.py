import pytest
from types import SimpleNamespace
from pydantic import ValidationError

from models.unified_response import TokenUsage, UnifiedResponse
from orchestrator.completion_status import attach_generation_budget
from orchestrator.generation_policy import GenerationPolicyError, resolve_generation_budget
from server.generation_service import constrain_budget_to_reservation, resolve_request_budget
from server.schemas.requests import ChatRequest
from server.schemas.responses import ChatResponseDTO


def test_legacy_request_keeps_quick_profile():
    resolved = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
    )

    assert resolved.profile == "quick"
    assert resolved.effective_max_output_tokens == 1024
    assert resolved.effective_reasoning_mode == "thinking"
    assert resolved.effective_reasoning_effort == "low"


def test_explicit_balanced_profile_uses_fixed_4k_ceiling():
    resolved = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "balanced", "reasoning": {"mode": "auto"}},
    )

    assert resolved.effective_max_output_tokens == 4096
    assert resolved.retry_profile == "deep"


def test_auto_profile_keeps_a_simple_economical_model_at_4k():
    resolved = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "auto"},
        input_text="What is a Python virtual environment?",
    )

    assert resolved.profile == "auto"
    assert resolved.requested_max_output_tokens == 4096
    assert resolved.effective_max_output_tokens == 4096
    assert resolved.retry_profile == "deep"


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openai", "gpt-5.6-terra"),
        ("claude", "claude-sonnet-5"),
        ("claude", "claude-sonnet-4-6"),
    ],
)
def test_auto_profile_gives_advanced_reasoning_models_8k(provider, model):
    resolved = resolve_generation_budget(
        provider=provider,
        model=model,
        generation={"profile": "auto"},
        input_text="Explain dependency injection with a short example.",
    )

    assert resolved.requested_max_output_tokens == 8192
    assert resolved.effective_max_output_tokens == 8192
    assert resolved.effective_reasoning_effort == "medium"
    assert resolved.retry_profile == "deep"


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5", "claude-fable-5", "claude-opus-4-6", "claude-opus-4-5"],
)
def test_auto_profile_gives_premium_models_12k(model):
    resolved = resolve_generation_budget(
        provider="claude",
        model=model,
        generation={"profile": "auto"},
        input_text="Explain dependency injection with a short example.",
    )

    assert resolved.requested_max_output_tokens == 12288
    assert resolved.effective_max_output_tokens == 12288
    assert resolved.retry_profile == "extended"


def test_auto_profile_escalates_detailed_advanced_work_to_12k():
    resolved = resolve_generation_budget(
        provider="openai",
        model="gpt-5.6-terra",
        generation={"profile": "auto"},
        input_text="Create a comprehensive implementation plan for this production architecture.",
    )

    assert resolved.requested_max_output_tokens == 12288
    assert resolved.effective_reasoning_effort == "high"
    assert resolved.retry_profile == "extended"


def test_chat_request_accepts_auto_generation_profile():
    request = ChatRequest(
        prompt="Explain this",
        provider="openai",
        model="gpt-5.6-terra",
        generation={"profile": "auto"},
    )

    assert request.generation is not None
    assert request.generation.profile == "auto"


def test_profile_is_clamped_to_model_native_limit():
    resolved = resolve_generation_budget(
        provider="openai",
        model="gpt-4o-mini",
        generation={"profile": "extended"},
    )

    assert resolved.requested_max_output_tokens == 32768
    assert resolved.effective_max_output_tokens == 16384


def test_explicit_limit_above_model_limit_is_rejected_not_silently_clamped():
    with pytest.raises(GenerationPolicyError, match="exceeds the allowed maximum"):
        resolve_generation_budget(
            provider="openai",
            model="gpt-4o-mini",
            generation={"max_output_tokens": 20000},
        )


def test_fable_reasoning_cannot_be_disabled():
    with pytest.raises(GenerationPolicyError, match="does not allow reasoning to be disabled"):
        resolve_generation_budget(
            provider="claude",
            model="claude-fable-5",
            generation={"profile": "balanced", "reasoning": {"mode": "off"}},
        )


@pytest.mark.parametrize(
    "model",
    ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-5"],
)
def test_manual_thinking_only_claude_models_default_to_normal_generation(model):
    resolved = resolve_generation_budget(
        provider="claude",
        model=model,
        generation={"profile": "balanced"},
    )

    assert resolved.effective_reasoning_mode == "off"
    assert resolved.effective_reasoning_effort == "none"


def test_manual_thinking_only_claude_models_reject_reasoning_on_locally():
    with pytest.raises(GenerationPolicyError, match="does not support reasoning controls"):
        resolve_generation_budget(
            provider="claude",
            model="claude-haiku-4-5",
            generation={"profile": "balanced", "reasoning": {"mode": "on"}},
        )


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-opus-4-6"])
def test_claude_46_models_use_registry_declared_adaptive_thinking(model):
    resolved = resolve_generation_budget(
        provider="claude",
        model=model,
        generation={"profile": "balanced"},
    )

    assert resolved.effective_reasoning_mode == "adaptive"
    assert resolved.effective_reasoning_effort == "medium"


def test_profile_effort_uses_model_supported_maximum():
    resolved = resolve_generation_budget(
        provider="claude",
        model="claude-sonnet-5",
        generation={"profile": "extended"},
    )

    assert resolved.effective_reasoning_effort == "max"


def test_explicit_maximum_reasoning_is_supported_for_claude_5():
    resolved = resolve_generation_budget(
        provider="claude",
        model="claude-sonnet-5",
        generation={
            "profile": "extended",
            "reasoning": {"mode": "on", "effort": "max"},
        },
    )

    assert resolved.effective_reasoning_mode == "adaptive"
    assert resolved.effective_reasoning_effort == "max"


def test_explicit_unsupported_reasoning_effort_is_rejected():
    with pytest.raises(GenerationPolicyError, match="unsupported"):
        resolve_generation_budget(
            provider="deepseek",
            model="deepseek-v4-pro",
            generation={
                "profile": "extended",
                "reasoning": {"mode": "on", "effort": "medium"},
            },
        )


@pytest.mark.parametrize(
    ("provider", "model", "requested", "expected"),
    [
        ("openai", "gpt-5.4", "max", "xhigh"),
        ("grok", "grok-4.3", "max", "xhigh"),
        ("gemini", "gemini-3.5-flash-lite", "low", "low"),
        ("deepseek", "deepseek-v4-flash", "low", "low"),
    ],
)
def test_public_reasoning_levels_map_to_provider_efforts(
    provider,
    model,
    requested,
    expected,
):
    resolved = resolve_generation_budget(
        provider=provider,
        model=model,
        generation={
            "profile": "balanced",
            "reasoning": {"mode": "on", "effort": requested},
        },
    )

    assert resolved.effective_reasoning_effort == expected


def test_non_reasoning_model_rejects_manual_low_instead_of_silently_disabling():
    with pytest.raises(GenerationPolicyError, match="does not support reasoning controls"):
        resolve_generation_budget(
            provider="openai",
            model="gpt-4o-mini",
            generation={
                "profile": "balanced",
                "reasoning": {"mode": "on", "effort": "low"},
            },
        )


def test_legacy_and_new_generation_fields_conflict():
    with pytest.raises(ValidationError, match="generation and max_tokens"):
        ChatRequest(
            prompt="hello",
            provider="deepseek",
            model="deepseek-v4-flash",
            max_tokens=1000,
            generation={"profile": "balanced"},
        )


def test_length_response_is_exposed_as_incomplete_with_retry_profile():
    budget = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "balanced"},
    )
    response = UnifiedResponse(
        request_id="req-length",
        text="Partial answer",
        provider="deepseek",
        model="deepseek-v4-flash",
        latency_ms=10,
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=4096, total_tokens=4106),
        estimated_cost=0.0,
        finish_reason="length",
    )
    annotated = attach_generation_budget(response, budget)
    dto = ChatResponseDTO.from_unified_response(annotated)

    assert dto.completion_status == "incomplete"
    assert dto.stop_cause == "token_limit"
    assert dto.generation_budget is not None
    assert dto.generation_budget.effective_max_output_tokens == 4096
    assert dto.retry_with_more_room.available is True
    assert dto.retry_with_more_room.recommended_profile == "deep"


def test_empty_reasoning_length_response_stays_billable_and_incomplete():
    budget = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "balanced"},
    )
    response = UnifiedResponse(
        request_id="req-reasoning-length",
        text="",
        provider="deepseek",
        model="deepseek-v4-flash",
        latency_ms=10,
        token_usage=TokenUsage(
            prompt_tokens=10,
            completion_tokens=4096,
            total_tokens=4106,
            reasoning_tokens=4096,
        ),
        estimated_cost=0.0,
        finish_reason="length",
    )

    annotated = attach_generation_budget(response, budget)
    assert annotated.is_success
    assert annotated.metadata["reasoning_budget_exhausted"] is True


def test_rollout_switch_restores_quick_budget(monkeypatch):
    monkeypatch.setenv("GENERATION_BUDGET_POLICY_ENABLED", "false")

    resolved = resolve_request_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "extended"},
        legacy_max_tokens=None,
        input_text="hello",
    )

    assert resolved.profile == "quick"
    assert resolved.effective_max_output_tokens == 2048


def test_affordable_reservation_ceiling_is_used_for_provider_execution(monkeypatch):
    monkeypatch.setenv("CREDIT_AWARE_GENERATION_BUDGET_ENABLED", "true")
    resolved = resolve_generation_budget(
        provider="deepseek",
        model="deepseek-v4-flash",
        generation={"profile": "balanced"},
    )
    reservation = SimpleNamespace(
        model_estimates=(
            SimpleNamespace(
                provider="deepseek",
                model="deepseek-v4-flash",
                output_tokens=900,
            ),
        )
    )
    constrained = constrain_budget_to_reservation(
        resolved,
        reservation,
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    assert constrained.requested_max_output_tokens == 4096
    assert constrained.effective_max_output_tokens == 900
    assert constrained.provider_kwargs()["max_tokens"] == 900
