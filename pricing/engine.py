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
        components = {
            "input_cost": Decimal(normal) * amount(snapshot["input"]) / million,
            "cached_input_cost": Decimal(usage.cached_input_tokens)
            * amount(snapshot["cached_input"])
            / million,
            "cache_write_cost": Decimal(usage.cache_write_tokens - usage.cache_write_1h_tokens)
            * amount(snapshot["cache_write"])
            / million,
            "output_cost": Decimal(
                usage.output_tokens - (usage.reasoning_tokens if "reasoning" in snapshot else 0)
            )
            * amount(snapshot["output"])
            / million,
        }
        if usage.cache_write_1h_tokens:
            if "cache_write_1h" not in snapshot:
                raise PricingUnavailableError("PRICING_UNKNOWN for one-hour cache writes")
            components["cache_write_1h_cost"] = (
                Decimal(usage.cache_write_1h_tokens) * amount(snapshot["cache_write_1h"]) / million
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
