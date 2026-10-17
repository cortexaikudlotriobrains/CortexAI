from datetime import date

from config.pricing import ModelPricing
from config.provider_catalog import get_provider_catalog
from orchestrator.model_registry import ModelRegistry
from utils.cost_calculator import CostCalculator


def test_all_enabled_registry_models_have_pricing():
    registry = ModelRegistry.from_yaml()
    missing: list[str] = []

    for candidate in registry.list_enabled_models():
        pricing = ModelPricing.get_model_pricing(candidate.provider, candidate.model_name)
        if pricing is None:
            missing.append(f"{candidate.provider}:{candidate.model_name}")

    assert missing == [], f"Missing pricing entries: {missing}"


def test_all_enabled_registry_model_prices_match_pricing_table():
    registry = ModelRegistry.from_yaml()
    mismatches: list[str] = []

    for candidate in registry.list_enabled_models():
        pricing = ModelPricing.get_model_pricing(candidate.provider, candidate.model_name)
        if pricing is None:
            mismatches.append(f"{candidate.provider}:{candidate.model_name}=missing")
            continue

        if (
            float(candidate.input_cost_per_1m) != float(pricing["input"])
            or float(candidate.output_cost_per_1m) != float(pricing["output"])
        ):
            mismatches.append(
                (
                    f"{candidate.provider}:{candidate.model_name} "
                    f"registry=({candidate.input_cost_per_1m},{candidate.output_cost_per_1m}) "
                    f"pricing=({pricing['input']},{pricing['output']})"
                )
            )

    assert mismatches == [], f"Registry/pricing mismatch: {mismatches}"


def test_unknown_model_pricing_uses_flagged_conservative_fallback():
    calc = CostCalculator(model_type="openai", model_name="unknown-model-name")
    costs = calc.calculate_cost(prompt_tokens=1000, completion_tokens=500)
    assert costs["total_cost"] > 0.0
    assert costs["pricing_unknown"] is True
    assert costs["pricing_rule_applied"] == "conservative-fallback:openai"


def _verified_on_a_past_date(value: str | None) -> bool:
    """Models are re-verified on different review dates; each must be a real, past date."""

    return bool(value) and date.fromisoformat(str(value)) <= date.today()


def test_provider_defaults_and_compare_defaults_are_current_selectable_models():
    registry = ModelRegistry.from_yaml()

    for spec in get_provider_catalog().provider_specs():
        for configured_model in (spec.default_model, spec.compare_default_model):
            candidate = registry.find_model(spec.provider_id, configured_model)
            assert candidate is not None, f"Missing default {spec.provider_id}:{configured_model}"
            assert candidate.enabled is True
            assert candidate.selectable is True
            assert candidate.lifecycle_status == "ACTIVE"
            assert candidate.pricing_source_url
            assert _verified_on_a_past_date(candidate.source_verified_at)


def test_every_selectable_model_has_official_evidence_and_nonzero_rates():
    registry = ModelRegistry.from_yaml()

    for candidate in registry.list_selectable_models():
        assert candidate.input_cost_per_1m > 0
        assert candidate.output_cost_per_1m > 0
        assert candidate.pricing_rule_id
        assert candidate.pricing_source_url and candidate.pricing_source_url.startswith("https://")
        assert candidate.lifecycle_source_url and candidate.lifecycle_source_url.startswith("https://")
        assert _verified_on_a_past_date(candidate.source_verified_at), candidate.model_name
