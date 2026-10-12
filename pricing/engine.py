"""Pure Decimal cost calculation; contains no provider or customer prices."""

from decimal import Decimal, localcontext
from typing import Any, Mapping

from pricing.models import (
    CostCalculationResult,
    NormalizedLLMUsage,
    PricingUnavailableError,
    amount,
)


def calculate(usage: NormalizedLLMUsage, snapshot: Mapping[str, Any]) -> CostCalculationResult:
    if (
        snapshot.get("currency", "USD") != "USD"
        or snapshot.get("unit", "per_1m_tokens") != "per_1m_tokens"
    ):
        raise ValueError("Unsupported currency or token pricing unit")
    normal = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
    with localcontext() as context:
        context.prec = 40
        million = Decimal(1_000_000)

        def token_cost(key: str, quantity: int) -> Decimal:
            if not quantity:
                return Decimal(0)
            if key not in snapshot or snapshot[key] is None:
                raise PricingUnavailableError(f"PRICING_UNKNOWN for dimension {key}")
            return Decimal(quantity) * amount(snapshot[key]) / million

        components = {
            "input_cost": token_cost("input", normal),
            "cached_input_cost": token_cost("cached_input", usage.cached_input_tokens),
            "cache_write_cost": token_cost(
                "cache_write", usage.cache_write_tokens - usage.cache_write_1h_tokens
            ),
            "output_cost": token_cost(
                "output",
                usage.output_tokens - (usage.reasoning_tokens if "reasoning" in snapshot else 0),
            ),
        }
        if usage.cache_write_1h_tokens:
            components["cache_write_1h_cost"] = token_cost(
                "cache_write_1h", usage.cache_write_1h_tokens
            )
        if "reasoning" in snapshot and usage.reasoning_tokens:
            components["reasoning_cost"] = (
                Decimal(usage.reasoning_tokens) * amount(snapshot["reasoning"]) / million
            )
        for key, quantity in usage.units.items():
            if quantity and key not in snapshot.get("unit_rates", {}):
                raise PricingUnavailableError(f"PRICING_UNKNOWN for dimension {key}")
            if quantity:
                components[key] = Decimal(quantity) * amount(snapshot["unit_rates"][key])
        return CostCalculationResult(
            usage, snapshot, components, sum(components.values(), Decimal(0))
        )
