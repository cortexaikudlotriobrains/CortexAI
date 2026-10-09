"""Resolve customer-facing AI-credit usage for response payloads and history."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from config.cache_optimization import (
    cache_aware_credit_calculation_enabled,
    cache_aware_credit_settlement_enabled,
)
from orchestrator.model_registry import ModelRegistry
from server.billing.credit_calculator import (
    calculate_credit_charge,
    calculate_model_credit_charge,
    research_credit_usage_from_metadata,
    web_search_credit_usage_from_metadata,
)
from server.billing.credit_estimator import fallback_actual_tokens


@dataclass(frozen=True)
class ResponseCreditUsage:
    ai_credits: int
    credit_usage_estimated: bool
    research_ai_credits: int
    research_credit_usage_estimated: bool
    web_search_ai_credits: int = 0
    web_search_credit_usage_estimated: bool = False
    cache_hit: bool = False
    cache_hit_ratio: float = 0.0
    cache_savings_ai_credits: int = 0
    uncached_equivalent_ai_credits: int = 0


@lru_cache(maxsize=1)
def _credit_registry() -> ModelRegistry:
    return ModelRegistry.from_yaml()


def calculate_response_credit_usage(
    *,
    provider: str,
    model: str,
    requested_model: str = "",
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    output_tokens: int = 0,
    output_text: str = "",
    is_error: bool = False,
    metadata: Mapping[str, Any] | None = None,
    pricing_snapshot: Mapping[str, Any] | None = None,
    include_research_charge: bool = True,
) -> ResponseCreditUsage:
    """Calculate the exact response-card snapshot from persisted response inputs."""
    model_credits = 0
    cache_hit = False
    cache_hit_ratio = 0.0
    cache_savings = 0
    uncached_equivalent = 0
    model_usage_estimated = False
    if not is_error:
        candidate = _credit_registry().find_model(provider, requested_model or model)
        if candidate is None and requested_model and requested_model != model:
            candidate = _credit_registry().find_model(provider, model)
        if candidate is None:
            model_usage_estimated = True
        else:
            resolved_input_tokens = max(0, int(input_tokens or 0))
            resolved_output_tokens = max(0, int(output_tokens or 0))
            model_usage_estimated = resolved_input_tokens == 0 and resolved_output_tokens == 0
            if model_usage_estimated:
                resolved_input_tokens, resolved_output_tokens = fallback_actual_tokens(
                    input_text="",
                    output_text=output_text,
                )
            legacy_charge = calculate_credit_charge(
                input_tokens=resolved_input_tokens,
                output_tokens=resolved_output_tokens,
                input_multiplier=candidate.input_credit_multiplier,
                output_multiplier=candidate.output_credit_multiplier,
                estimated=model_usage_estimated,
                pricing_snapshot=pricing_snapshot,
            )
            cache_charge = calculate_model_credit_charge(
                prompt_tokens=resolved_input_tokens,
                cached_input_tokens=max(0, int(cached_input_tokens or 0)),
                cache_write_tokens=max(0, int(cache_write_tokens or 0)),
                output_tokens=resolved_output_tokens,
                input_credit_multiplier=candidate.input_credit_multiplier,
                output_credit_multiplier=candidate.output_credit_multiplier,
                pricing_snapshot=pricing_snapshot,
                estimated=model_usage_estimated,
            )
            settlement_enabled = (
                cache_aware_credit_calculation_enabled()
                and cache_aware_credit_settlement_enabled()
            )
            charge = cache_charge if settlement_enabled else legacy_charge
            model_credits = charge.total_credits
            cache_hit = cache_charge.cached_input_tokens > 0
            cache_hit_ratio = (
                cache_charge.cached_input_tokens / cache_charge.prompt_tokens
                if cache_charge.prompt_tokens
                else 0.0
            )
            cache_savings = cache_charge.cache_savings_credits if settlement_enabled else 0
            uncached_equivalent = cache_charge.uncached_equivalent_credits

    research_usage = research_credit_usage_from_metadata(metadata)
    web_search_usage = web_search_credit_usage_from_metadata(metadata)
    displayed_credits = model_credits + web_search_usage.cortex_credits
    if include_research_charge:
        displayed_credits += research_usage.cortex_credits

    return ResponseCreditUsage(
        ai_credits=displayed_credits,
        credit_usage_estimated=model_usage_estimated,
        research_ai_credits=research_usage.cortex_credits,
        research_credit_usage_estimated=research_usage.estimated,
        web_search_ai_credits=web_search_usage.cortex_credits,
        web_search_credit_usage_estimated=web_search_usage.estimated,
        cache_hit=cache_hit,
        cache_hit_ratio=cache_hit_ratio,
        cache_savings_ai_credits=cache_savings,
        uncached_equivalent_ai_credits=uncached_equivalent,
    )


def resolve_response_credit_usage(
    response: object,
    *,
    include_research_charge: bool = True,
) -> ResponseCreditUsage:
    """Resolve a response-card snapshot from a UnifiedResponse-like object."""
    token_usage = getattr(response, "token_usage", None)
    metadata = getattr(response, "metadata", None)
    return calculate_response_credit_usage(
        provider=str(getattr(response, "provider", "") or ""),
        model=str(getattr(response, "model", "") or ""),
        requested_model=str(getattr(response, "requested_model", "") or ""),
        input_tokens=max(0, int(getattr(token_usage, "prompt_tokens", 0) or 0)),
        cached_input_tokens=max(
            0, int(getattr(token_usage, "cached_input_tokens", 0) or 0)
        ),
        cache_write_tokens=max(0, int(getattr(token_usage, "cache_write_tokens", 0) or 0)),
        reasoning_tokens=max(0, int(getattr(token_usage, "reasoning_tokens", 0) or 0)),
        output_tokens=max(0, int(getattr(token_usage, "completion_tokens", 0) or 0)),
        output_text=str(getattr(response, "text", "") or ""),
        is_error=bool(getattr(response, "is_error", False)),
        metadata=metadata if isinstance(metadata, Mapping) else None,
        pricing_snapshot=(
            getattr(response, "pricing_snapshot", None)
            if isinstance(getattr(response, "pricing_snapshot", None), Mapping)
            else None
        ),
        include_research_charge=include_research_charge,
    )
