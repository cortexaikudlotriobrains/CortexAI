"""Conservative preflight estimates and missing-usage fallbacks."""

from __future__ import annotations

from dataclasses import dataclass

from config.pricing import ModelPricing
from config.model_pricing import database_pricing_enabled
from pricing.models import PricingUnavailableError
from orchestrator.routing_types import ModelCandidate
from server.billing.credit_calculator import (
    ADVANCED_WEB_SEARCH_CREDITS,
    CreditCharge,
    calculate_credit_charge,
    resolve_cache_credit_multipliers,
)
from orchestrator.generation_policy import LEGACY_PROFILE, load_generation_policy
from utils.token_estimation import estimate_tokens


@dataclass(frozen=True)
class CreditEstimate:
    charge: CreditCharge
    input_tokens: int
    output_tokens: int


def estimate_text_tokens(text: str) -> int:
    """Backward-compatible wrapper around the shared estimator."""

    return estimate_tokens(text)


def estimate_model_credits(
    candidate: ModelCandidate,
    *,
    input_text: str,
    max_output_tokens: int | None,
    include_research: bool = False,
) -> CreditEstimate:
    output_tokens = max_output_tokens
    if output_tokens is None:
        output_tokens = int(
            load_generation_policy()["profiles"][LEGACY_PROFILE]["max_output_tokens"]
        )
    if isinstance(output_tokens, bool) or output_tokens <= 0:
        raise ValueError("max_output_tokens must be positive")
    input_tokens = estimate_text_tokens(input_text)
    pricing_snapshot = ModelPricing.get_pricing_snapshot(
        candidate.provider,
        candidate.model_name,
        prompt_tokens=input_tokens,
    )
    if pricing_snapshot is None and database_pricing_enabled():
        raise PricingUnavailableError(
            f"PRICING_UNKNOWN before reservation for {candidate.provider}:{candidate.model_name}"
        )
    _cached_multiplier, cache_write_multiplier = resolve_cache_credit_multipliers(
        input_credit_multiplier=candidate.input_credit_multiplier,
        pricing_snapshot=pricing_snapshot,
    )
    reservation_input_multiplier = max(
        candidate.input_credit_multiplier,
        float(cache_write_multiplier),
    )
    charge = calculate_credit_charge(
        input_tokens=input_tokens,
        output_tokens=int(output_tokens),
        input_multiplier=reservation_input_multiplier,
        output_multiplier=candidate.output_credit_multiplier,
        fixed_credits=ADVANCED_WEB_SEARCH_CREDITS if include_research else 0,
        estimated=True,
    )
    return CreditEstimate(
        charge=charge,
        input_tokens=input_tokens,
        output_tokens=int(output_tokens),
    )


def fallback_actual_tokens(
    *,
    input_text: str,
    output_text: str,
) -> tuple[int, int]:
    """Conservatively estimate actual tokens when a provider omits usage."""
    return estimate_text_tokens(input_text), estimate_text_tokens(output_text)
