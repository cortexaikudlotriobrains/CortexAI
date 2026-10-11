"""Deterministic AI-credit calculations.

Credits are an integer accounting unit. Input and output components are
calculated and rounded up separately before fixed charges are added.
Provider reasoning tokens belong to output tokens before they reach this
module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from tools.web.provider_metadata import SEARCH_CREDITS_PER_OPERATION

# Credit calibration ceiling: provider USD per one million raw credit units.
# One raw unit is 1/1,000 of a displayed AI credit, so this is also the provider
# USD covered by 1,000 displayed credits. Registry multipliers are calibrated to
# it for standard-context prices; the runtime floor below applies the same
# ceiling to whatever rate a request actually used.
DEFAULT_MAX_PROVIDER_USD_PER_MILLION_CREDITS = Decimal("1")
MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV = "CREDIT_MAX_PROVIDER_USD_PER_MILLION_CREDITS"

# One Tavily API credit at the USD 0.008 pay-as-you-go rate, converted at the
# calibration ceiling. Advanced search consumes two Tavily credits.
CORTEX_CREDITS_PER_TAVILY_CREDIT = 8_000
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
    pricing_snapshot: Mapping[str, Any] | None = None,
) -> CreditCharge:
    """Return the legacy uncached charge through the shared calculator.

    A supplied pricing snapshot only applies the provider-rate floor; every
    prompt token is still charged at the (floored) normal input multiplier.
    """

    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 0:
        raise ValueError("input_tokens must be a nonnegative integer")

    return calculate_model_credit_charge(
        prompt_tokens=input_tokens,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=output_tokens,
        input_credit_multiplier=input_multiplier,
        output_credit_multiplier=output_multiplier,
        pricing_snapshot=pricing_snapshot,
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
    multiplier and can never create an unsupported discount. Multipliers are
    floored at the provider rates the snapshot applied (see
    ``floor_credit_multipliers``) before cache ratios are derived.
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

    input_rate, output_rate = floor_credit_multipliers(
        input_credit_multiplier=input_credit_multiplier,
        output_credit_multiplier=output_credit_multiplier,
        pricing_snapshot=pricing_snapshot,
    )
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


def max_provider_usd_per_million_credits() -> Decimal:
    """Return the configured credit-calibration ceiling (default USD 1).

    Lower values raise the minimum credit charge for every request whose
    applied provider rate exceeds the ceiling; values above the default would
    weaken the registry calibration and are rejected.
    """

    raw = str(os.getenv(MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV, "") or "").strip()
    if not raw:
        return DEFAULT_MAX_PROVIDER_USD_PER_MILLION_CREDITS
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(
            f"{MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV} must be a decimal in (0, 1]"
        ) from exc
    if (
        not value.is_finite()
        or value <= 0
        or value > DEFAULT_MAX_PROVIDER_USD_PER_MILLION_CREDITS
    ):
        raise ValueError(
            f"{MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV} must be a decimal in (0, 1]"
        )
    return value


def floor_credit_multipliers(
    *,
    input_credit_multiplier: float | Decimal,
    output_credit_multiplier: float | Decimal,
    pricing_snapshot: Mapping[str, Any] | None,
) -> tuple[Decimal, Decimal]:
    """Raise registry multipliers to cover the provider rates a request used.

    Registry multipliers are calibrated against standard-context prices. A
    request can apply a higher rate, such as a whole-request long-context band
    or a newly approved rate-card version, so each multiplier is floored at
    ``applied rate / calibration ceiling``. An explicit reasoning rate counts
    toward the output floor. Missing or invalid evidence leaves the registry
    multiplier unchanged, and the floor never lowers a multiplier.
    """

    input_rate = _positive_decimal(input_credit_multiplier, "input_credit_multiplier")
    output_rate = _positive_decimal(output_credit_multiplier, "output_credit_multiplier")
    rates = _snapshot_rates(pricing_snapshot)
    applied_input = _nonnegative_decimal(rates.get("input"))
    applied_output = max(
        (
            rate
            for rate in (
                _nonnegative_decimal(rates.get("output")),
                _nonnegative_decimal(rates.get("reasoning")),
            )
            if rate is not None
        ),
        default=None,
    )
    if not applied_input and not applied_output:
        return input_rate, output_rate
    ceiling = max_provider_usd_per_million_credits()
    if applied_input:
        input_rate = max(input_rate, applied_input / ceiling)
    if applied_output:
        output_rate = max(output_rate, applied_output / ceiling)
    return input_rate, output_rate


def resolve_cache_credit_multipliers(
    *,
    input_credit_multiplier: float | Decimal,
    pricing_snapshot: Mapping[str, Any] | None,
) -> tuple[Decimal, Decimal]:
    """Resolve cache-read/write multipliers from effective provider prices."""

    normal_multiplier = _positive_decimal(
        input_credit_multiplier, "input_credit_multiplier"
    )
    rates = _snapshot_rates(pricing_snapshot)

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


def _snapshot_rates(pricing_snapshot: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return applied per-million rates from either snapshot shape."""

    if not isinstance(pricing_snapshot, Mapping):
        return {}
    nested = pricing_snapshot.get("rates_per_1m")
    return nested if isinstance(nested, Mapping) else pricing_snapshot


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
