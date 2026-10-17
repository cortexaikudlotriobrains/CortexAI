"""Golden credit charges for the models added in the 2026-10 model update.

Credits come from the credit ladder (config/credit_ladder.yaml): the cheapest
step at or above the LiteLLM standard price. Long-context bands and ended
promotions are covered at runtime by the provider-rate floor
(``floor_credit_multipliers``), so the charges below are pinned for a normal
turn, for each long-context band and for both sides of each promotion.

If a provider price or a ladder step changes, update the registry with
``python scripts/models.py`` and then these numbers, never the other way round.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal

import pytest

from config import pricing
from config.pricing import ModelPricing
from orchestrator.model_registry import ModelRegistry
from server.billing.credit_calculator import calculate_model_credit_charge
from server.billing.credit_estimator import estimate_model_credits
from server.billing.plan_catalog import get_plan_catalog

TODAY = datetime(2026, 10, 14, 12, tzinfo=UTC)

# (provider, model): (input/output credits per token, billing class, plans)
NEW_MODELS = {
    ("openai", "gpt-6-luna"): ("0.25/1", "economical", {"free", "plus", "pro"}),
    ("openai", "gpt-6.1-sol"): ("3/14", "advanced", {"plus", "pro"}),
    ("openai", "gpt-6-astra"): ("10/50", "premium", {"pro"}),
    ("claude", "claude-haiku-5-5"): ("0.25/1", "economical", {"free", "plus", "pro"}),
    ("claude", "claude-sonnet-5-5"): ("3/14", "advanced", {"plus", "pro"}),
    ("claude", "claude-opus-5-5"): ("6/30", "premium", {"pro"}),
    ("claude", "claude-fable-5-1"): ("10/50", "premium", {"pro"}),
    ("gemini", "gemini-3.8-flash"): ("1/4", "standard", {"free", "plus", "pro"}),
    ("gemini", "gemini-4-argon"): ("3/14", "advanced", {"plus", "pro"}),
    ("grok", "grok-4.7"): ("2/8", "standard", {"free", "plus", "pro"}),
}

# A normal turn: 10,000 prompt tokens of which 4,000 are cache reads, 1,000 output.
NORMAL_TURN_CREDITS = {
    ("openai", "gpt-6-luna"): 2_600,  # 6000*0.25 + 4000*0.025 + 1000*1
    ("openai", "gpt-6.1-sol"): 32_600,  # 6000*3 + 4000*0.15 + 1000*14
    ("openai", "gpt-6-astra"): 114_000,  # 6000*10 + 4000*1 + 1000*50
    ("claude", "claude-haiku-5-5"): 2_600,  # 6000*0.25 + 4000*0.025 + 1000*1
    ("claude", "claude-sonnet-5-5"): 32_600,  # 6000*3 + 4000*0.15 + 1000*14
    ("claude", "claude-opus-5-5"): 67_200,  # 6000*6 + 4000*0.3 + 1000*30
    ("claude", "claude-fable-5-1"): 111_000,  # 6000*10 + 4000*0.25 + 1000*50
    ("gemini", "gemini-3.8-flash"): 10_400,  # 6000*1 + 4000*0.1 + 1000*4
    ("gemini", "gemini-4-argon"): 32_600,  # 6000*3 + 4000*0.15 + 1000*14
    ("grok", "grok-4.7"): 22_000,  # 6000*2 + 4000*0.5 + 1000*8
}

# First prompt token of each long-context band, and the charge for a 300,000-token
# prompt with 1,000 output tokens (the floor lifts credits to the band's rates).
LONG_CONTEXT = {
    ("openai", "gpt-6-luna"): (272_001, 76_000),  # band 0.2/0.75 stays under 0.25/1
    ("openai", "gpt-6.1-sol"): (272_001, 1_215_000),  # floored to 4/15
    ("openai", "gpt-6-astra"): (272_001, 6_075_000),  # floored to 20/75
    ("claude", "claude-haiku-5-5"): (100_001, 152_500),  # floored to 0.5/2.5
    ("grok", "grok-4.7"): (200_001, 1_212_000),  # floored to 4/12
}
NO_LONG_CONTEXT_BAND = {
    ("claude", "claude-sonnet-5-5"),
    ("claude", "claude-opus-5-5"),
    ("claude", "claude-fable-5-1"),
    ("gemini", "gemini-3.8-flash"),
    ("gemini", "gemini-4-argon"),
}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(pricing, "_utc_now", lambda: TODAY)


@pytest.fixture(scope="module")
def registry():
    return ModelRegistry.from_yaml()


def _charge(candidate, *, prompt, cached=0, output, at=TODAY):
    snapshot = ModelPricing.get_pricing_snapshot(
        candidate.provider, candidate.model_name, prompt_tokens=prompt, at=at
    )
    assert snapshot is not None
    return snapshot, calculate_model_credit_charge(
        prompt_tokens=prompt,
        cached_input_tokens=cached,
        output_tokens=output,
        input_credit_multiplier=candidate.input_credit_multiplier,
        output_credit_multiplier=candidate.output_credit_multiplier,
        pricing_snapshot=snapshot,
    )


def test_golden_tables_cover_the_same_models():
    assert set(NORMAL_TURN_CREDITS) == set(NEW_MODELS)
    assert set(LONG_CONTEXT) | NO_LONG_CONTEXT_BAND == set(NEW_MODELS)


@pytest.mark.parametrize("key", sorted(NEW_MODELS))
def test_new_model_credits_class_and_plans(registry, key):
    credits, billing_class, plans = NEW_MODELS[key]
    candidate = registry.find_model(*key)
    assert candidate is not None
    assert candidate.selectable is True
    assert candidate.lifecycle_status == "ACTIVE"
    expected_in, expected_out = (Decimal(part) for part in credits.split("/"))
    assert Decimal(str(candidate.input_credit_multiplier)) == expected_in
    assert Decimal(str(candidate.output_credit_multiplier)) == expected_out
    assert candidate.billing_class.value == billing_class

    allowed = {
        plan.code
        for plan in get_plan_catalog().list_plans()
        if billing_class in plan.entitlements.allowed_billing_classes
    }
    assert allowed == plans


@pytest.mark.parametrize("key", sorted(NEW_MODELS))
def test_credits_cover_the_current_standard_price(registry, key):
    """Ladder rule: credits per token are never below USD per 1M tokens today."""

    candidate = registry.find_model(*key)
    snapshot = ModelPricing.get_pricing_snapshot(*key, prompt_tokens=10_000, at=TODAY)
    assert snapshot is not None and not snapshot.get("long_context_applied")
    assert Decimal(str(candidate.input_credit_multiplier)) >= Decimal(str(snapshot["input"]))
    assert Decimal(str(candidate.output_credit_multiplier)) >= Decimal(str(snapshot["output"]))


@pytest.mark.parametrize("key", sorted(NORMAL_TURN_CREDITS))
def test_normal_turn_golden_credits(registry, key):
    _snapshot, charge = _charge(
        registry.find_model(*key), prompt=10_000, cached=4_000, output=1_000
    )
    assert charge.total_credits == NORMAL_TURN_CREDITS[key]
    assert charge.cache_savings_credits > 0


@pytest.mark.parametrize("key", sorted(LONG_CONTEXT))
def test_long_context_band_boundary_and_golden_credits(registry, key):
    threshold, expected = LONG_CONTEXT[key]
    candidate = registry.find_model(*key)
    below = ModelPricing.get_pricing_snapshot(*key, prompt_tokens=threshold - 1, at=TODAY)
    at = ModelPricing.get_pricing_snapshot(*key, prompt_tokens=threshold, at=TODAY)
    assert below["long_context_applied"] is False
    assert at["long_context_applied"] is True
    assert candidate.long_context_threshold_tokens == threshold

    snapshot, charge = _charge(candidate, prompt=300_000, output=1_000)
    assert snapshot["long_context_applied"] is True
    assert charge.total_credits == expected


@pytest.mark.parametrize("key", sorted(NO_LONG_CONTEXT_BAND))
def test_models_without_a_band_keep_one_price(registry, key):
    snapshot = ModelPricing.get_pricing_snapshot(*key, prompt_tokens=900_000, at=TODAY)
    assert snapshot is not None
    assert not snapshot.get("long_context_applied")


def test_haiku_5_5_reservation_covers_the_100k_band(registry):
    candidate = registry.find_model("claude", "claude-haiku-5-5")
    estimate = estimate_model_credits(candidate, input_text="x" * 500_000, max_output_tokens=1)
    assert estimate.input_tokens >= 100_001
    # Band rates 0.5/2.5 floor the 0.25/1 credits; reservations price input at the
    # 5-minute cache-write ratio (1.25x) so a cache write can never exceed them.
    assert estimate.charge.input_credits == int(
        (Decimal(estimate.input_tokens) * Decimal("0.625")).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    assert estimate.charge.output_credits == 3  # 1 token * 2.5, rounded up


def test_haiku_5_5_small_reservation_keeps_ladder_credits(registry):
    candidate = registry.find_model("claude", "claude-haiku-5-5")
    estimate = estimate_model_credits(candidate, input_text="x" * 4_000, max_output_tokens=1_000)
    assert estimate.input_tokens < 100_001
    assert estimate.charge.output_credits == 1_000


@pytest.mark.parametrize(
    "key,promo_total,regular_total,ends",
    [
        # 0.75/3.75 launch price, then 1.5/7.5 floors the 1/4 credits:
        # 6000*1.5 + 4000*0.15 + 1000*7.5.
        (("gemini", "gemini-3.8-flash"), 10_400, 17_100, datetime(2027, 1, 1, tzinfo=UTC)),
        # 2/10 launch price, then 4/20 floors the 3/14 credits:
        # 6000*4 + 4000*0.2 + 1000*20.
        (("gemini", "gemini-4-argon"), 32_600, 44_800, datetime(2026, 12, 1, tzinfo=UTC)),
    ],
)
def test_promotions_end_without_a_pricing_gap(registry, key, promo_total, regular_total, ends):
    """Two dated registry rules: the regular price starts the instant the promo ends."""

    candidate = registry.find_model(*key)
    last_promo_instant = ends - timedelta(microseconds=1)
    before, promo = _charge(
        candidate, prompt=10_000, cached=4_000, output=1_000, at=last_promo_instant
    )
    after, regular = _charge(candidate, prompt=10_000, cached=4_000, output=1_000, at=ends)
    assert before["effective_until"] is not None
    assert after["effective_until"] is None
    assert before["pricing_rule_id"] != after["pricing_rule_id"]
    assert promo.total_credits == promo_total
    assert regular.total_credits == regular_total
