"""Cortex AI-credit accounting for cumulative Managed Agent session usage."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Mapping

from orchestrator.model_registry import ModelRegistry
from server.billing.credit_calculator import calculate_model_credit_charge

WORK_PRICING_VERSION = "managed-agents-2026-08-20"
MANAGED_RUNTIME_USD_PER_HOUR = Decimal("0.08")
CORTEX_CREDITS_PER_USD = Decimal("1000000")
ANTHROPIC_WEB_SEARCH_USD = Decimal("0.01")
USD_CENTS_PER_DOLLAR = Decimal("100")


class WorkBillingIdentityError(ValueError):
    """The provider session did not expose one supported billing identity."""


def _int(mapping: Mapping[str, object], key: str) -> int:
    raw = mapping.get(key, 0)
    if not isinstance(raw, (str, int, float, bool)):
        return 0
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _nested(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    return value if isinstance(value, Mapping) else {}


def _list_cost_cents(mapping: Mapping[str, object]) -> int:
    value = _nested(mapping, "list_cost")
    if not value:
        return 0
    currency = str(value.get("currency") or "USD").strip().upper()
    if currency != "USD":
        raise ValueError(f"Unsupported Managed Agent usage currency: {currency}")
    raw_amount = value.get("amount", 0)
    if isinstance(raw_amount, bool) or not isinstance(raw_amount, (str, int)):
        raise ValueError("Invalid Managed Agent list-cost amount")
    try:
        amount = int(raw_amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid Managed Agent list-cost amount") from exc
    if amount < 0:
        raise ValueError("Invalid Managed Agent list-cost amount")
    return amount


def _delta(current: int, baseline: int) -> int:
    return max(0, current - baseline)


@dataclass(frozen=True)
class WorkCreditUsage:
    total_credits: int
    model_credits: int
    input_credits: int
    output_credits: int
    runtime_credits: int
    web_credits: int
    prompt_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    active_seconds: int
    web_searches: int
    provider_cost_usd: float
    reconstructed_provider_cost_usd: float
    reported_provider_cost_usd: float
    provider_floor_credits: int
    component_credits: int
    model: str
    pricing_version: str = WORK_PRICING_VERSION


def calculate_work_credit_usage(
    current: Mapping[str, object],
    baseline: Mapping[str, object],
    *,
    model: str,
) -> WorkCreditUsage:
    candidate = ModelRegistry.from_yaml().find_model("claude", model)
    if candidate is None or not candidate.enabled:
        raise WorkBillingIdentityError(
            f"Managed Agent billing model is absent or disabled: {model}"
        )

    canonical_model = candidate.model_name
    cache_current = _nested(current, "cache_creation")
    cache_baseline = _nested(baseline, "cache_creation")
    normal_input = _delta(_int(current, "input_tokens"), _int(baseline, "input_tokens"))
    output_tokens = _delta(_int(current, "output_tokens"), _int(baseline, "output_tokens"))
    cached = _delta(
        _int(current, "cache_read_input_tokens"),
        _int(baseline, "cache_read_input_tokens"),
    )
    cache_write = _delta(
        _int(cache_current, "ephemeral_5m_input_tokens")
        + _int(cache_current, "ephemeral_1h_input_tokens"),
        _int(cache_baseline, "ephemeral_5m_input_tokens")
        + _int(cache_baseline, "ephemeral_1h_input_tokens"),
    )
    active_seconds = _delta(_int(current, "active_seconds"), _int(baseline, "active_seconds"))
    tool_current = _nested(current, "server_tool_use")
    tool_baseline = _nested(baseline, "server_tool_use")
    web_searches = _delta(
        _int(tool_current, "web_search_requests"),
        _int(tool_baseline, "web_search_requests"),
    )
    prompt_tokens = normal_input + cached + cache_write

    charge = calculate_model_credit_charge(
        prompt_tokens=prompt_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        input_credit_multiplier=candidate.input_credit_multiplier,
        output_credit_multiplier=candidate.output_credit_multiplier,
        pricing_snapshot={
            "input": candidate.input_cost_per_1m,
            "cached_input": candidate.cached_input_cost_per_1m,
            "cache_write": candidate.cache_write_cost_per_1m,
            "pricing_version": candidate.credit_pricing_version,
        },
    )
    runtime_usd = (Decimal(active_seconds) / Decimal(3600)) * MANAGED_RUNTIME_USD_PER_HOUR
    web_usd = Decimal(web_searches) * ANTHROPIC_WEB_SEARCH_USD
    cached_rate = candidate.cached_input_cost_per_1m or candidate.input_cost_per_1m
    cache_write_rate = candidate.cache_write_cost_per_1m or candidate.input_cost_per_1m
    model_usd = (
        Decimal(normal_input) * Decimal(str(candidate.input_cost_per_1m))
        + Decimal(cached) * Decimal(str(cached_rate))
        + Decimal(cache_write) * Decimal(str(cache_write_rate))
        + Decimal(output_tokens) * Decimal(str(candidate.output_cost_per_1m))
    ) / Decimal(1_000_000)
    runtime_credits = int(
        (runtime_usd * CORTEX_CREDITS_PER_USD).to_integral_value(rounding=ROUND_CEILING)
    )
    web_credits = int((web_usd * CORTEX_CREDITS_PER_USD).to_integral_value(rounding=ROUND_CEILING))
    reconstructed_provider_cost_usd = model_usd + runtime_usd + web_usd
    reported_cost_cents = _delta(_list_cost_cents(current), _list_cost_cents(baseline))
    reported_provider_cost_usd = Decimal(reported_cost_cents) / USD_CENTS_PER_DOLLAR
    provider_floor_credits = int(
        (reported_provider_cost_usd * CORTEX_CREDITS_PER_USD).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    component_credits = charge.total_credits + runtime_credits + web_credits
    return WorkCreditUsage(
        total_credits=max(component_credits, provider_floor_credits),
        model_credits=charge.total_credits,
        input_credits=charge.input_credits,
        output_credits=charge.output_credits,
        runtime_credits=runtime_credits,
        web_credits=web_credits,
        prompt_tokens=prompt_tokens,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        active_seconds=active_seconds,
        web_searches=web_searches,
        provider_cost_usd=float(max(reconstructed_provider_cost_usd, reported_provider_cost_usd)),
        reconstructed_provider_cost_usd=float(reconstructed_provider_cost_usd),
        reported_provider_cost_usd=float(reported_provider_cost_usd),
        provider_floor_credits=provider_floor_credits,
        component_credits=component_credits,
        model=canonical_model,
    )


def resolve_work_billing_model(model: str) -> str:
    """Return the canonical enabled registry model for a provider-reported ID."""

    candidate = ModelRegistry.from_yaml().find_model("claude", str(model or "").strip())
    if candidate is None or not candidate.enabled:
        raise WorkBillingIdentityError(
            f"Managed Agent billing model is absent or disabled: {model}"
        )
    return candidate.model_name
