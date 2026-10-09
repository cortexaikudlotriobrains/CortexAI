from decimal import Decimal

import pytest

from orchestrator.model_registry import ModelRegistry
from config.pricing import ModelPricing
from server.billing.credit_calculator import (
    ADVANCED_WEB_SEARCH_CREDITS,
    CORTEX_CREDITS_PER_TAVILY_CREDIT,
    MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV,
    TAVILY_ADVANCED_SEARCH_CREDIT_ESTIMATE,
    calculate_credit_charge,
    calculate_model_credit_charge,
    calculate_research_credit_charge,
    floor_credit_multipliers,
    max_provider_usd_per_million_credits,
    research_credit_usage_from_metadata,
)
from server.billing.credit_estimator import estimate_model_credits, estimate_text_tokens


def test_input_and_output_credits_use_independent_multipliers_and_round_up():
    charge = calculate_credit_charge(
        input_tokens=3,
        output_tokens=2,
        input_multiplier=Decimal("0.25"),
        output_multiplier=Decimal("1.5"),
        fixed_credits=7,
    )

    assert charge.input_credits == 1
    assert charge.output_credits == 3
    assert charge.fixed_credits == 7
    assert charge.total_credits == 11


def test_reasoning_tokens_are_billed_as_output_tokens_by_contract():
    # Provider adapters aggregate reasoning into completion_tokens; the credit
    # calculator deliberately has no discounted reasoning category.
    charge = calculate_credit_charge(
        input_tokens=100,
        output_tokens=400,
        input_multiplier=1,
        output_multiplier=4,
    )
    assert charge.input_credits == 100
    assert charge.output_credits == 1_600
    assert charge.total_credits == 1_700


def test_fractional_input_and_output_components_each_round_up():
    charge = calculate_credit_charge(
        input_tokens=1,
        output_tokens=1,
        input_multiplier=Decimal("0.25"),
        output_multiplier=Decimal("0.25"),
    )

    assert charge.input_credits == 1
    assert charge.output_credits == 1
    assert charge.total_credits == 2


def test_research_preflight_reserves_the_normal_advanced_search_cost():
    candidate = ModelRegistry.from_yaml().find_model("openai", "gpt-4.1-mini")
    assert candidate is not None
    without_search = estimate_model_credits(
        candidate,
        input_text="hello",
        max_output_tokens=100,
    )
    with_search = estimate_model_credits(
        candidate,
        input_text="hello",
        max_output_tokens=100,
        include_research=True,
    )
    assert (
        with_search.charge.total_credits - without_search.charge.total_credits
        == ADVANCED_WEB_SEARCH_CREDITS
    )
    assert ADVANCED_WEB_SEARCH_CREDITS == (
        TAVILY_ADVANCED_SEARCH_CREDIT_ESTIMATE
        * CORTEX_CREDITS_PER_TAVILY_CREDIT
    )


@pytest.mark.parametrize(
    ("provider_credits", "expected_cortex_credits"),
    [(0, 0), (1, 8_000), (2, 16_000), (3, 24_000)],
)
def test_research_charge_scales_with_provider_usage(
    provider_credits,
    expected_cortex_credits,
):
    assert calculate_research_credit_charge(provider_credits) == expected_cortex_credits


def test_research_metadata_uses_reported_usage_and_never_charges_reuse():
    fresh = research_credit_usage_from_metadata(
        {
            "research_used": True,
            "research_reused": False,
            "research_provider_credits_used": 3,
            "research_provider_credits_estimated": False,
        }
    )
    reused = research_credit_usage_from_metadata(
        {
            "research_used": True,
            "research_reused": True,
            "research_provider_credits_used": 3,
        }
    )

    assert (fresh.provider_credits_used, fresh.cortex_credits, fresh.estimated) == (
        3,
        24_000,
        False,
    )
    assert (reused.provider_credits_used, reused.cortex_credits) == (0, 0)


def test_legacy_research_metadata_uses_marked_two_credit_fallback():
    usage = research_credit_usage_from_metadata(
        {"research_used": True, "research_reused": False}
    )
    assert usage.provider_credits_used == 2
    assert usage.cortex_credits == 16_000
    assert usage.estimated is True


def test_provider_usage_is_billable_even_when_no_sources_were_usable():
    usage = research_credit_usage_from_metadata(
        {
            "research_used": False,
            "research_reused": False,
            "research_provider_credits_used": 2,
            "research_provider_credits_estimated": False,
        }
    )
    assert usage.provider_credits_used == 2
    assert usage.cortex_credits == 16_000
    assert usage.estimated is False


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_research_calculator_rejects_invalid_provider_usage(value):
    with pytest.raises(ValueError, match="provider_credits_used"):
        calculate_research_credit_charge(value)


def test_text_estimator_is_deterministic_and_conservative():
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcdef") == 2
    assert estimate_text_tokens("abcdef") == estimate_text_tokens("abcdef")


def test_every_enabled_model_is_calibrated_below_one_dollar_per_million_credits():
    for candidate in ModelRegistry.from_yaml().list_models(include_disabled=False):
        assert (
            candidate.input_cost_per_1m / candidate.input_credit_multiplier <= 1
        ), candidate.model_name
        assert (
            candidate.output_cost_per_1m / candidate.output_credit_multiplier <= 1
        ), candidate.model_name


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "fixed_credits"],
)
def test_credit_calculator_rejects_negative_accounting_inputs(field):
    values = {
        "input_tokens": 1,
        "output_tokens": 1,
        "fixed_credits": 0,
    }
    values[field] = -1
    with pytest.raises(ValueError, match=field):
        calculate_credit_charge(
            **values,
            input_multiplier=1,
            output_multiplier=1,
        )


# Rates stay within the USD 1 per million credits calibration for the unit
# multiplier used below, so these cases isolate cache ratios from the
# provider-rate floor (cached reads at 10% and cache writes at 125% of input).
CACHE_PRICING = {
    "input": 1.0,
    "cached_input": 0.1,
    "cache_write": 1.25,
}


def _cache_charge(*, cached: int = 0, cache_write: int = 0):
    return calculate_model_credit_charge(
        prompt_tokens=10_000,
        cached_input_tokens=cached,
        cache_write_tokens=cache_write,
        output_tokens=100,
        input_credit_multiplier=1,
        output_credit_multiplier=4,
        pricing_snapshot=CACHE_PRICING,
    )


def test_uncached_cache_aware_charge_equals_legacy_total():
    cache_aware = _cache_charge()
    legacy = calculate_credit_charge(
        input_tokens=10_000,
        output_tokens=100,
        input_multiplier=1,
        output_multiplier=4,
    )
    assert cache_aware.total_credits == legacy.total_credits
    assert cache_aware.normal_input_tokens == 10_000


def test_partial_and_full_cache_hits_only_discount_reported_tokens():
    partial = _cache_charge(cached=8_000)
    full = _cache_charge(cached=10_000)
    assert (partial.normal_input_tokens, partial.cached_input_tokens) == (2_000, 8_000)
    assert (partial.normal_input_credits, partial.cached_input_credits) == (2_000, 800)
    assert partial.cache_savings_credits == 7_200
    assert (full.normal_input_tokens, full.cached_input_tokens) == (0, 10_000)
    assert full.cached_input_credits == 1_000


def test_cache_creation_uses_write_ratio_and_never_reports_negative_savings():
    charge = _cache_charge(cache_write=8_000)
    assert (charge.normal_input_tokens, charge.cache_write_tokens) == (2_000, 8_000)
    assert charge.cache_write_credits == 10_000
    assert charge.cache_savings_credits == 0


def test_invalid_provider_cache_partitions_are_clamped_to_prompt_tokens():
    overreported = _cache_charge(cached=15_000)
    mixed = _cache_charge(cached=8_000, cache_write=8_000)
    assert (overreported.normal_input_tokens, overreported.cached_input_tokens) == (0, 10_000)
    assert (
        mixed.normal_input_tokens,
        mixed.cached_input_tokens,
        mixed.cache_write_tokens,
    ) == (0, 8_000, 2_000)


def test_missing_pricing_snapshot_never_grants_cache_discount():
    charge = calculate_model_credit_charge(
        prompt_tokens=10_000,
        cached_input_tokens=8_000,
        cache_write_tokens=2_000,
        output_tokens=0,
        input_credit_multiplier=1,
        output_credit_multiplier=4,
        pricing_snapshot=None,
    )
    assert charge.input_credits == 10_000
    assert charge.cache_savings_credits == 0


def test_long_context_effective_pricing_snapshot_drives_cache_ratios():
    snapshot = ModelPricing.get_pricing_snapshot(
        "openai",
        "gpt-5.6-luna",
        prompt_tokens=300_000,
    )
    assert snapshot is not None
    assert snapshot["long_context_applied"] is True
    charge = calculate_model_credit_charge(
        prompt_tokens=300_000,
        cached_input_tokens=200_000,
        output_tokens=0,
        input_credit_multiplier=1,
        output_credit_multiplier=4,
        pricing_snapshot=snapshot,
    )
    assert charge.cached_input_credits == 20_000
    assert charge.normal_input_credits == 100_000


def test_reservation_uses_expensive_cache_write_multiplier():
    candidate = ModelRegistry.from_yaml().find_model("openai", "gpt-5.6-luna")
    assert candidate is not None
    estimate = estimate_model_credits(
        candidate,
        input_text="x" * 300,
        max_output_tokens=1,
    )
    normal_only = calculate_credit_charge(
        input_tokens=estimate.input_tokens,
        output_tokens=1,
        input_multiplier=candidate.input_credit_multiplier,
        output_multiplier=candidate.output_credit_multiplier,
    )
    assert estimate.charge.input_credits > normal_only.input_credits


# Gemini 3.1 Pro Preview's whole-request band above 200K prompt tokens, against
# its 3 / 14 registry multipliers.
LONG_CONTEXT_RATES = {
    "long_context_applied": True,
    "rates_per_1m": {
        "input": "4.0",
        "cached_input": "0.4",
        "cache_write": "4.0",
        "output": "18.0",
    },
}


def test_default_credit_calibration_ceiling_is_one_dollar(monkeypatch):
    monkeypatch.delenv(MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV, raising=False)
    assert max_provider_usd_per_million_credits() == Decimal("1")


def test_long_context_rates_floor_registry_multipliers_at_the_ceiling():
    charge = calculate_model_credit_charge(
        prompt_tokens=250_000,
        output_tokens=1_000,
        input_credit_multiplier=3,
        output_credit_multiplier=14,
        pricing_snapshot=LONG_CONTEXT_RATES,
    )
    # USD 4 and USD 18 per million at USD 1 per million credits replace 3 and 14.
    assert charge.input_credits == 1_000_000
    assert charge.output_credits == 18_000
    assert charge.total_credits == 1_018_000


def test_rates_within_the_ceiling_keep_registry_multipliers():
    charge = calculate_model_credit_charge(
        prompt_tokens=4_000,
        output_tokens=800,
        input_credit_multiplier=3,
        output_credit_multiplier=14,
        pricing_snapshot={"input": 2.0, "cached_input": 0.2, "output": 12.0},
    )
    assert (charge.input_credits, charge.output_credits) == (12_000, 11_200)


def test_floor_raises_cache_multipliers_with_the_input_floor():
    charge = calculate_model_credit_charge(
        prompt_tokens=250_000,
        cached_input_tokens=200_000,
        output_tokens=0,
        input_credit_multiplier=3,
        output_credit_multiplier=14,
        pricing_snapshot=LONG_CONTEXT_RATES,
    )
    assert charge.normal_input_credits == 200_000
    assert charge.cached_input_credits == 80_000
    assert charge.uncached_equivalent_credits == 1_000_000


def test_legacy_charge_applies_the_floor_only_with_a_snapshot():
    floored = calculate_credit_charge(
        input_tokens=250_000,
        output_tokens=1_000,
        input_multiplier=3,
        output_multiplier=14,
        pricing_snapshot=LONG_CONTEXT_RATES,
    )
    registry_only = calculate_credit_charge(
        input_tokens=250_000,
        output_tokens=1_000,
        input_multiplier=3,
        output_multiplier=14,
    )
    assert floored.total_credits == 1_018_000
    assert registry_only.total_credits == 764_000


def test_explicit_reasoning_rate_counts_toward_the_output_floor():
    assert floor_credit_multipliers(
        input_credit_multiplier=1,
        output_credit_multiplier=4,
        pricing_snapshot={"input": 1.0, "output": 4.0, "reasoning": 6.0},
    ) == (Decimal("1"), Decimal("6"))


def test_floor_never_lowers_a_multiplier_and_ignores_missing_evidence():
    assert floor_credit_multipliers(
        input_credit_multiplier=6,
        output_credit_multiplier=30,
        pricing_snapshot={"rates_per_1m": {"input": "5", "output": "25"}},
    ) == (Decimal("6"), Decimal("30"))
    for snapshot in (None, {}, {"rates_per_1m": {"input": None, "output": "bad"}}):
        assert floor_credit_multipliers(
            input_credit_multiplier=3,
            output_credit_multiplier=14,
            pricing_snapshot=snapshot,
        ) == (Decimal("3"), Decimal("14"))


def test_configured_ceiling_scales_the_floor(monkeypatch):
    monkeypatch.setenv(MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV, "0.8")
    assert floor_credit_multipliers(
        input_credit_multiplier=3,
        output_credit_multiplier=14,
        pricing_snapshot=LONG_CONTEXT_RATES,
    ) == (Decimal("5"), Decimal("22.5"))


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", "NaN", "Infinity"])
def test_invalid_credit_calibration_ceiling_is_rejected(monkeypatch, value):
    monkeypatch.setenv(MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV, value)
    with pytest.raises(ValueError, match=MAX_PROVIDER_USD_PER_MILLION_CREDITS_ENV):
        max_provider_usd_per_million_credits()


def test_reservation_covers_the_long_context_floor():
    candidate = ModelRegistry.from_yaml().find_model("gemini", "gemini-3.1-pro-preview")
    assert candidate is not None
    estimate = estimate_model_credits(candidate, input_text="x" * 700_000, max_output_tokens=1)
    snapshot = ModelPricing.get_pricing_snapshot(
        "gemini",
        "gemini-3.1-pro-preview",
        prompt_tokens=estimate.input_tokens,
    )
    assert snapshot is not None and snapshot["long_context_applied"] is True
    assert estimate.charge.input_credits == estimate.input_tokens * 4


def test_every_enabled_long_context_band_is_covered_by_the_floor():
    checked = 0
    for candidate in ModelRegistry.from_yaml().list_models(include_disabled=False):
        snapshot = ModelPricing.get_pricing_snapshot(
            candidate.provider,
            candidate.model_name,
            prompt_tokens=2_000_000,
        )
        if not snapshot or not snapshot.get("long_context_applied"):
            continue
        input_rate, output_rate = floor_credit_multipliers(
            input_credit_multiplier=candidate.input_credit_multiplier,
            output_credit_multiplier=candidate.output_credit_multiplier,
            pricing_snapshot=snapshot,
        )
        assert Decimal(str(snapshot["input"])) / input_rate <= 1, candidate.model_name
        assert Decimal(str(snapshot["output"])) / output_rate <= 1, candidate.model_name
        checked += 1
    assert checked > 0
