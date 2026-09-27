"""Deterministic AI-credit calculations.

Credits are an integer accounting unit. Input and output components are
calculated and rounded up separately before fixed charges are added.
Provider reasoning tokens belong to output tokens before they reach this
module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any

from tools.web.provider_metadata import SEARCH_CREDITS_PER_OPERATION

CORTEX_CREDITS_PER_TAVILY_CREDIT = 5_000
TAVILY_ADVANCED_SEARCH_CREDIT_ESTIMATE = 2
# Research preflight reserves the normal Advanced Search cost. Settlement uses
# the provider-reported usage instead of treating this as a flat fee.
ADVANCED_WEB_SEARCH_CREDITS = (
    CORTEX_CREDITS_PER_TAVILY_CREDIT * TAVILY_ADVANCED_SEARCH_CREDIT_ESTIMATE
)


@dataclass(frozen=True)
class CreditCharge:
    prompt_tokens: int
    normal_input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    normal_input_credits: int
    cached_input_credits: int
    cache_write_credits: int
    input_credits: int
    output_credits: int
    fixed_credits: int
    total_credits: int
    uncached_equivalent_credits: int
    cache_savings_credits: int
    estimated: bool = False

    @property
    def input_tokens(self) -> int:
        """Backward-compatible alias for total provider prompt tokens."""

        return self.prompt_tokens


@dataclass(frozen=True)
class ResearchCreditUsage:
    provider_credits_used: int
    cortex_credits: int
    estimated: bool = False


@dataclass(frozen=True)
class WebSearchCreditUsage:
    provider: str
    backend: str
    operations: int
    cortex_credits: int
    provider_cost_usd: float
    estimated: bool = False


def web_search_credit_usage_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> WebSearchCreditUsage:
    if not isinstance(metadata, Mapping):
        return WebSearchCreditUsage("", "", 0, 0, 0.0, False)
    raw = metadata.get("web_search")
    if not isinstance(raw, Mapping):
        return WebSearchCreditUsage("", "", 0, 0, 0.0, False)
    provider = str(raw.get("provider") or "").strip().lower()
    backend = str(raw.get("backend") or provider).strip().lower()
    try:
        operations = max(0, int(raw.get("operations") or 0))
    except (TypeError, ValueError):
        operations = 0
    rate = SEARCH_CREDITS_PER_OPERATION.get(provider, 0)
    try:
        provider_cost_usd = max(0.0, float(raw.get("provider_cost_usd") or 0.0))
    except (TypeError, ValueError):
        provider_cost_usd = 0.0
    return WebSearchCreditUsage(
        provider=provider,
        backend=backend,
        operations=operations,
        cortex_credits=operations * rate,
        provider_cost_usd=provider_cost_usd,
        estimated=bool(raw.get("usage_estimated")),
    )


def calculate_research_credit_charge(provider_credits_used: int) -> int:
    """Convert Tavily API credits into Cortex AI credits."""
    if (
        isinstance(provider_credits_used, bool)
        or not isinstance(provider_credits_used, int)
        or provider_credits_used < 0
    ):
        raise ValueError("provider_credits_used must be a nonnegative integer")
    return provider_credits_used * CORTEX_CREDITS_PER_TAVILY_CREDIT


def research_credit_usage_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> ResearchCreditUsage:
    """Resolve one turn's research charge from orchestration metadata.

    Older responses that only expose ``research_used`` retain a conservative
    two-credit fallback. Cached/reused research is always free for the turn.
    """
    if not isinstance(metadata, Mapping) or bool(metadata.get("research_reused")):
        return ResearchCreditUsage(0, 0, False)

    raw_credits = metadata.get("research_provider_credits_used")
    estimated = bool(metadata.get("research_provider_credits_estimated"))
    if isinstance(raw_credits, bool) or not isinstance(raw_credits, int) or raw_credits < 0:
        if not bool(metadata.get("research_used")):
            return ResearchCreditUsage(0, 0, False)
        raw_credits = TAVILY_ADVANCED_SEARCH_CREDIT_ESTIMATE
        estimated = True

    return ResearchCreditUsage(
        provider_credits_used=raw_credits,
        cortex_credits=calculate_research_credit_charge(raw_credits),
        estimated=estimated,
    )


def calculate_credit_charge(
    *,
    input_tokens: int,
    output_tokens: int,
    input_multiplier: float | Decimal,
    output_multiplier: float | Decimal,
    fixed_credits: int = 0,
    estimated: bool = False,
) -> CreditCharge:
    """Return the legacy uncached charge through the shared calculator."""

    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("input_tokens must be a nonnegative integer")

    return calculate_model_credit_charge(
        prompt_tokens=input_tokens,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=output_tokens,
        input_credit_multiplier=input_multiplier,
        output_credit_multiplier=output_multiplier,
        fixed_credits=fixed_credits,
        estimated=estimated,
    )


def calculate_model_credit_charge(
    *,
    prompt_tokens: int,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    output_tokens: int,
    input_credit_multiplier: float | Decimal,
    output_credit_multiplier: float | Decimal,
    pricing_snapshot: Mapping[str, Any] | None = None,
    fixed_credits: int = 0,
    estimated: bool = False,
) -> CreditCharge:
    """Calculate one authoritative cache-aware model credit charge.

    Provider cache quantities are clamped into a disjoint partition of prompt
    tokens. Missing or invalid pricing evidence falls back to the full input
    multiplier and can never create an unsupported discount.
    """

    for label, value in (
        ("prompt_tokens", prompt_tokens),
        ("cached_input_tokens", cached_input_tokens),
        ("cache_write_tokens", cache_write_tokens),
        ("output_tokens", output_tokens),
        ("fixed_credits", fixed_credits),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a nonnegative integer")

    input_rate = _positive_decimal(input_credit_multiplier, "input_credit_multiplier")
    output_rate = _positive_decimal(output_credit_multiplier, "output_credit_multiplier")
    cached_tokens = min(prompt_tokens, cached_input_tokens)
    cache_write = min(max(0, prompt_tokens - cached_tokens), cache_write_tokens)
    normal_tokens = prompt_tokens - cached_tokens - cache_write
    cached_rate, cache_write_rate = resolve_cache_credit_multipliers(
        input_credit_multiplier=input_rate,
        pricing_snapshot=pricing_snapshot,
    )

    raw_normal_input = Decimal(normal_tokens) * input_rate
    raw_cached_input = Decimal(cached_tokens) * cached_rate
    raw_cache_write = Decimal(cache_write) * cache_write_rate
    raw_output = Decimal(output_tokens) * output_rate
    normal_input_credits = int(raw_normal_input.to_integral_value(rounding=ROUND_CEILING))
    cached_input_credits = int(raw_cached_input.to_integral_value(rounding=ROUND_CEILING))
    cache_write_credits = int(raw_cache_write.to_integral_value(rounding=ROUND_CEILING))
    input_credits = normal_input_credits + cached_input_credits + cache_write_credits
    output_credits = int(raw_output.to_integral_value(rounding=ROUND_CEILING))
    total = input_credits + output_credits + fixed_credits
    uncached_input_credits = int(
        (Decimal(prompt_tokens) * input_rate).to_integral_value(rounding=ROUND_CEILING)
    )
    uncached_equivalent = uncached_input_credits + output_credits + fixed_credits
    return CreditCharge(
        prompt_tokens=prompt_tokens,
        normal_input_tokens=normal_tokens,
        cached_input_tokens=cached_tokens,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        normal_input_credits=normal_input_credits,
        cached_input_credits=cached_input_credits,
        cache_write_credits=cache_write_credits,
        input_credits=input_credits,
        output_credits=output_credits,
        fixed_credits=fixed_credits,
        total_credits=total,
        uncached_equivalent_credits=uncached_equivalent,
        cache_savings_credits=max(0, uncached_equivalent - total),
        estimated=estimated,
    )


def resolve_cache_credit_multipliers(
    *,
    input_credit_multiplier: float | Decimal,
    pricing_snapshot: Mapping[str, Any] | None,
) -> tuple[Decimal, Decimal]:
    """Resolve cache-read/write multipliers from effective provider prices."""

    normal_multiplier = _positive_decimal(
        input_credit_multiplier, "input_credit_multiplier"
    )
    rates: Mapping[str, Any] = {}
    if isinstance(pricing_snapshot, Mapping):
        nested = pricing_snapshot.get("rates_per_1m")
        rates = nested if isinstance(nested, Mapping) else pricing_snapshot

    normal_price = _nonnegative_decimal(rates.get("input"))
    cached_price = _nonnegative_decimal(rates.get("cached_input"))
    cache_write_price = _nonnegative_decimal(rates.get("cache_write"))
    if normal_price is None or normal_price <= 0:
        return normal_multiplier, normal_multiplier

    cached_multiplier = (
        normal_multiplier * cached_price / normal_price
        if cached_price is not None and cached_price > 0
        else normal_multiplier
    )
    cache_write_multiplier = (
        normal_multiplier * cache_write_price / normal_price
        if cache_write_price is not None and cache_write_price > 0
        else normal_multiplier
    )
    return cached_multiplier, cache_write_multiplier


def _positive_decimal(value: float | Decimal, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive number")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{label} must be a positive number") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be a positive number")
    return result


def _nonnegative_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    if not result.is_finite() or result < 0:
        return None
    return result
