import pytest

from config.pricing import ModelPricing
from utils.cost_calculator import CostCalculator


@pytest.mark.parametrize(
    ("provider", "model", "expected_input", "expected_output"),
    [
        ("openai", "gpt-5.1", 1.25, 10.00),
        ("openai", "gpt-5.6-luna", 0.20, 1.20),
        ("openai", "gpt-5.2-codex", 5.00, 30.00),
        ("gemini", "gemini-2.5-flash-lite", 0.10, 0.40),
        ("gemini", "gemini-2.5-pro", 1.25, 10.00),
        ("deepseek", "deepseek-v4-flash", 0.14, 0.28),
        ("deepseek", "deepseek-chat", 0.14, 0.28),
        ("grok", "grok-4-1-fast-non-reasoning", 1.25, 2.50),
        ("claude", "claude-sonnet-4-5", 3.00, 15.00),
        ("claude", "claude-sonnet-4-6", 3.00, 15.00),
        ("claude", "claude-opus-4-5", 5.00, 25.00),
        ("claude", "claude-opus-4-6", 5.00, 25.00),
    ],
)
def test_key_model_pricing_values(provider, model, expected_input, expected_output):
    pricing = ModelPricing.get_model_pricing(provider, model, at="2026-10-07T00:00:00Z")
    assert pricing is not None
    assert pricing["input"] == pytest.approx(expected_input)
    assert pricing["output"] == pytest.approx(expected_output)


@pytest.mark.parametrize(
    (
        "model",
        "expected_input",
        "expected_cached",
        "expected_write_5m",
        "expected_write_1h",
        "expected_output",
    ),
    [
        ("claude-sonnet-4-6", 3.0, 0.3, 3.75, 6.0, 15.0),
        ("claude-opus-4-5", 5.0, 0.5, 6.25, 10.0, 25.0),
        ("claude-opus-4-6", 5.0, 0.5, 6.25, 10.0, 25.0),
    ],
)
def test_active_claude_4_models_use_official_cache_rates(
    model,
    expected_input,
    expected_cached,
    expected_write_5m,
    expected_write_1h,
    expected_output,
):
    five_minute = ModelPricing.get_pricing_snapshot("claude", model)
    one_hour = ModelPricing.get_pricing_snapshot(
        "claude",
        model,
        cache_write_ttl="1h",
    )

    assert five_minute is not None and one_hour is not None
    assert five_minute["input"] == expected_input
    assert five_minute["cached_input"] == expected_cached
    assert five_minute["cache_write"] == expected_write_5m
    assert five_minute["output"] == expected_output
    assert one_hour["cache_write"] == expected_write_1h
    assert five_minute["source_url"] == ("https://platform.claude.com/docs/en/about-claude/pricing")


def test_get_model_pricing_provider_case_insensitive():
    upper = ModelPricing.get_model_pricing("OPENAI", "gpt-4o-mini")
    lower = ModelPricing.get_model_pricing("openai", "gpt-4o-mini")
    assert upper == lower
    assert upper == {"input": 0.15, "output": 0.60}


def test_all_pricing_entries_have_required_shape_and_non_negative_values():
    all_pricing = ModelPricing.list_all_pricing()
    assert set(all_pricing.keys()) == {"openai", "gemini", "deepseek", "grok", "claude"}

    for provider, models in all_pricing.items():
        assert models, f"{provider} pricing should not be empty"
        for model_name, prices in models.items():
            assert set(prices.keys()) == {"input", "output"}, f"Invalid keys for {model_name}"
            assert isinstance(prices["input"], int | float)
            assert isinstance(prices["output"], int | float)
            assert prices["input"] >= 0.0
            assert prices["output"] >= 0.0


def test_list_all_pricing_filtering_behavior():
    openai_only = ModelPricing.list_all_pricing("openai")
    assert set(openai_only.keys()) == {"openai"}
    assert "gpt-4o-mini" in openai_only["openai"]

    unknown = ModelPricing.list_all_pricing("unknown-provider")
    assert unknown == {"unknown-provider": {}}


def test_cost_calculator_calculates_expected_cost_from_pricing_table():
    calc = CostCalculator(model_type="openai", model_name="gpt-5.1")
    costs = calc.calculate_cost(prompt_tokens=1_000_000, completion_tokens=500_000)
    assert costs["input_cost"] == pytest.approx(1.25)
    assert costs["output_cost"] == pytest.approx(5.00)
    assert costs["total_cost"] == pytest.approx(6.25)


def test_cost_calculator_cumulative_tracking_is_consistent():
    calc = CostCalculator(model_type="gemini", model_name="gemini-2.5-flash-lite")
    calc.update_cumulative_cost(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    calc.update_cumulative_cost(prompt_tokens=500_000, completion_tokens=0)

    totals = calc.get_cumulative_cost()
    assert totals["total_input_cost"] == pytest.approx(0.15)
    assert totals["total_output_cost"] == pytest.approx(0.40)
    assert totals["total_cost"] == pytest.approx(0.55)


def test_long_context_and_cached_token_pricing_is_applied_to_the_whole_request():
    calc = CostCalculator(model_type="openai", model_name="gpt-5.6-sol")
    costs = calc.calculate_cost(
        prompt_tokens=300_000,
        completion_tokens=10_000,
        cached_input_tokens=100_000,
    )

    assert costs["long_context_applied"] is True
    assert costs["input_cost"] == pytest.approx(2.0)
    assert costs["cached_input_cost"] == pytest.approx(0.1)
    assert costs["output_cost"] == pytest.approx(0.45)
    assert costs["pricing_rule_applied"] == "openai-gpt-5.6-sol-standard-20260731"
    assert costs["source_url"] == "https://developers.openai.com/api/docs/pricing"


def test_effective_dated_promotional_pricing_does_not_rewrite_future_costs():
    promo = ModelPricing.get_pricing_snapshot(
        "claude",
        "claude-sonnet-5",
        at="2026-08-31T23:59:59Z",
    )
    standard = ModelPricing.get_pricing_snapshot(
        "claude",
        "claude-sonnet-5",
        at="2026-09-01T00:00:00Z",
    )

    assert promo is not None and standard is not None
    assert (promo["input"], promo["output"]) == (2.0, 10.0)
    assert (standard["input"], standard["output"]) == (3.0, 15.0)
    assert promo["pricing_rule_id"] != standard["pricing_rule_id"]
