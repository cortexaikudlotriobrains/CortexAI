"""Provider-cost calculation with auditable pricing-rule snapshots."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from config.pricing import ModelPricing
from pricing.engine import calculate
from pricing.models import NormalizedLLMUsage, PricingUnavailableError
from utils.logger import get_logger

logger = get_logger(__name__)


class CostCalculator:
    """Calculate provider cost independently from CortexAI credit charging."""

    def __init__(
        self,
        model_type: str,
        model_name: str,
        *,
        catalog_path: str | Path | None = None,
    ):
        self.model_type = model_type.lower()
        self.model_name = model_name
        self.catalog_path = catalog_path
        self.pricing = ModelPricing.get_model_pricing(
            self.model_type,
            self.model_name,
            catalog_path=self.catalog_path,
        )

        self.total_input_cost = Decimal(0)
        self.total_output_cost = Decimal(0)
        self.total_cost = Decimal(0)

    def calculate_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        processing_mode: str = "standard",
        request_at: datetime | str | None = None,
        cache_write_ttl: str = "5m",
        unit_usage: dict[str, int] | None = None,
        usage_estimated: bool = False,
        cache_write_1h_tokens: int = 0,
    ) -> dict[str, Any]:
        """Calculate one call and return both amounts and pricing evidence.

        ``prompt_tokens`` is normalized to include uncached, cache-read, and
        cache-write input. ``completion_tokens`` is normalized to include any
        provider-reported reasoning tokens, so reasoning is recorded separately
        but never double billed.
        """

        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        cached = min(prompt, max(0, int(cached_input_tokens or 0)))
        cache_write = min(
            max(0, prompt - cached),
            max(0, int(cache_write_tokens or 0)),
        )
        normal_input = max(0, prompt - cached - cache_write)
        reasoning = min(completion, max(0, int(reasoning_tokens or 0)))
        usage = NormalizedLLMUsage(
            prompt,
            completion,
            cached,
            cache_write,
            reasoning,
            unit_usage or {},
            usage_estimated,
            max(0, int(cache_write_1h_tokens or 0)),
        )

        pricing = ModelPricing.get_pricing_snapshot(
            self.model_type,
            self.model_name,
            at=request_at,
            prompt_tokens=prompt,
            processing_mode=processing_mode,
            cache_write_ttl=cache_write_ttl,
            catalog_path=self.catalog_path,
        )
        pricing_unknown = pricing is None
        if pricing is None:
            pricing = ModelPricing.conservative_fallback(
                self.model_type,
                at=request_at,
                prompt_tokens=prompt,
                processing_mode=processing_mode,
                catalog_path=self.catalog_path,
            )
            if pricing is not None:
                logger.warning(
                    "Exact provider pricing unavailable; applying conservative fallback",
                    extra={
                        "extra_fields": {
                            "provider": self.model_type,
                            "served_model": self.model_name,
                            "pricing_rule_applied": pricing.get("pricing_rule_id"),
                            "prompt_tokens": prompt,
                            "completion_tokens": completion,
                        }
                    },
                )
        if pricing is None:
            logger.error(
                "cost_calculation_failure",
                extra={
                    "extra_fields": {
                        "event": "cost_calculation_failure",
                        "provider": self.model_type,
                        "model": self.model_name,
                        "status": "PRICING_UNKNOWN",
                    }
                },
            )
            raise PricingUnavailableError(
                f"No pricing or conservative fallback for {self.model_type}:{self.model_name}"
            )

        if usage.cache_write_1h_tokens and "cache_write_1h" not in pricing:
            hour_snapshot = ModelPricing.get_pricing_snapshot(
                self.model_type,
                self.model_name,
                at=request_at,
                prompt_tokens=prompt,
                processing_mode=processing_mode,
                cache_write_ttl="1h",
                catalog_path=self.catalog_path,
            )
            if hour_snapshot is None:
                raise PricingUnavailableError("PRICING_UNKNOWN for one-hour cache writes")
            pricing["cache_write_1h"] = hour_snapshot["cache_write"]
        result = calculate(usage, pricing)
        audit = {
            **result.audit(),
            **{
                key: pricing.get(key)
                for key in (
                    "rate_card_id",
                    "pricing_source",
                    "source_version",
                    "effective_from",
                    "effective_until",
                    "selection_reason",
                    "pricing_stale",
                    "cache_write_ttl",
                    "pricing_tier",
                    "schedule_calendar_year",
                )
            },
            "currency": "USD",
            "token_rates": {
                key: str(pricing[key])
                for key in (
                    "input",
                    "output",
                    "cached_input",
                    "cache_write",
                    "cache_write_1h",
                    "reasoning",
                )
                if key in pricing
            },
            "unit_rates": pricing.get("unit_rates", {}),
        }

        return {
            **{key: float(value) for key, value in result.components.items()},
            "total_cost": float(result.total),  # Existing display/API compatibility only.
            "cost_audit": audit,
            "normal_input_tokens": normal_input,
            "cached_input_tokens": cached,
            "cache_write_tokens": cache_write,
            "completion_tokens": completion,
            "reasoning_tokens": reasoning,
            "pricing_unknown": pricing_unknown,
            "pricing_rule_applied": pricing["pricing_rule_id"],
            "pricing_version": pricing["pricing_version"],
            "pricing_model": pricing.get("pricing_model") or self.model_name,
            "processing_mode": pricing["processing_mode"],
            "long_context_applied": bool(pricing["long_context_applied"]),
            "source_url": pricing.get("source_url"),
            "source_verified_at": pricing.get("source_verified_at"),
            "rates_per_1m": {
                "input": str(pricing["input"]),
                "cached_input": str(pricing["cached_input"]),
                "cache_write": str(pricing["cache_write"]),
                "output": str(pricing["output"]),
            },
        }

    def calculate_calls(self, calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Sum individually priced provider calls without losing exact evidence."""
        if not calls:
            raise ValueError("Provider call usage required")
        results = []
        evidence = []
        for call in calls:
            parameters = dict(call)
            model = str(parameters.pop("model"))
            calculator = (
                self
                if model == self.model_name
                else CostCalculator(self.model_type, model, catalog_path=self.catalog_path)
            )
            result = calculator.calculate_cost(**parameters)
            results.append(result)
            evidence.append(
                {
                    "served_model": model,
                    "request_started_at": str(call["request_at"]),
                    "pricing_model": result["pricing_model"],
                    **result["cost_audit"],
                }
            )
        combined = dict(results[0])
        audit = dict(combined["cost_audit"])
        keys = {key for result in results for key in result["cost_audit"]["cost_components_usd"]}
        with localcontext() as context:
            context.prec = 40
            components = {
                key: sum(
                    (
                        Decimal(result["cost_audit"]["cost_components_usd"].get(key, "0"))
                        for result in results
                    ),
                    Decimal(0),
                )
                for key in keys
            }
            total = sum(components.values(), Decimal(0))
        combined.update({key: float(value) for key, value in components.items()})
        combined["total_cost"] = float(total)
        combined["pricing_unknown"] = any(result["pricing_unknown"] for result in results)
        for key in (
            "normal_input_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "completion_tokens",
            "reasoning_tokens",
        ):
            combined[key] = sum(result[key] for result in results)
        normalized = dict(audit["normalized_usage"])
        for key in normalized:
            if key == "estimated":
                normalized[key] = any(
                    result["cost_audit"]["normalized_usage"][key] for result in results
                )
            elif key == "units":
                units = {
                    unit
                    for result in results
                    for unit in result["cost_audit"]["normalized_usage"][key]
                }
                normalized[key] = {
                    unit: sum(
                        result["cost_audit"]["normalized_usage"][key].get(unit, 0)
                        for result in results
                    )
                    for unit in units
                }
            else:
                normalized[key] = sum(
                    result["cost_audit"]["normalized_usage"][key] for result in results
                )
        tiers = {result["cost_audit"].get("pricing_tier", "standard") for result in results}
        audit.update(
            {
                "cost_components_usd": {key: str(value) for key, value in components.items()},
                "usage_calculated_provider_cost_usd": str(total),
                "normalized_usage": normalized,
                "pricing_tier": next(iter(tiers)) if len(tiers) == 1 else "mixed",
                "provider_calls": evidence,
                "rate_application": "per_provider_call",
            }
        )
        if normalized["estimated"]:
            audit["cost_kind"] = "estimated_provider_cost"
        if len({item.get("rate_card_id") for item in evidence}) > 1:
            audit["rate_card_id"] = None
            audit["selection_reason"] = "multiple_approved_cards_at_provider_call_times"
        combined["cost_audit"] = audit
        return combined

    def update_cumulative_cost(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        **kwargs: Any,
    ) -> None:
        costs = self.calculate_cost(prompt_tokens, completion_tokens, **kwargs)
        components = costs["cost_audit"]["cost_components_usd"]
        self.total_input_cost += sum(
            (
                Decimal(components[key])
                for key in ("input_cost", "cached_input_cost", "cache_write_cost")
            ),
            Decimal(0),
        )
        self.total_output_cost += Decimal(components["output_cost"])
        self.total_input_cost += Decimal(components.get("cache_write_1h_cost", "0"))
        self.total_output_cost += Decimal(components.get("reasoning_cost", "0"))
        self.total_cost += Decimal(costs["cost_audit"]["usage_calculated_provider_cost_usd"])

    def get_cumulative_cost(self) -> dict[str, float]:
        return {
            "total_input_cost": float(self.total_input_cost),
            "total_output_cost": float(self.total_output_cost),
            "total_cost": float(self.total_cost),
        }

    def format_cost(self, cost: float | Decimal, currency: str = "USD") -> str:
        if currency == "USD":
            return f"${cost:.6f}"
        return f"{cost:.6f} {currency}"

    def get_pricing_info(self) -> dict[str, Any]:
        snapshot = ModelPricing.get_pricing_snapshot(
            self.model_type,
            self.model_name,
            catalog_path=self.catalog_path,
        )
        if snapshot is None:
            return {
                "model_type": self.model_type,
                "model_name": self.model_name,
                "pricing_available": False,
                "message": "Exact pricing is unavailable; calls use a flagged conservative fallback",
            }
        return {
            "model_type": self.model_type,
            "model_name": self.model_name,
            "pricing_available": True,
            "input_price_per_million": snapshot["input"],
            "cached_input_price_per_million": snapshot["cached_input"],
            "cache_write_price_per_million": snapshot["cache_write"],
            "output_price_per_million": snapshot["output"],
            "pricing_rule_applied": snapshot["pricing_rule_id"],
            "pricing_version": snapshot["pricing_version"],
            "source_url": snapshot.get("source_url"),
            "source_verified_at": snapshot.get("source_verified_at"),
        }

    def format_summary(self) -> str:
        return (
            f"Input cost: {self.format_cost(self.total_input_cost)}\n"
            f"Output cost: {self.format_cost(self.total_output_cost)}\n"
            f"Total cost: {self.format_cost(self.total_cost)}"
        )

    def reset(self) -> None:
        self.total_input_cost = Decimal(0)
        self.total_output_cost = Decimal(0)
        self.total_cost = Decimal(0)
