"""Rate-card rollout and synchronization policy (no provider prices)."""

from dataclasses import dataclass
from decimal import Decimal
import os


def database_pricing_enabled() -> bool:
    mode = os.getenv("MODEL_PRICING_MODE", "legacy").strip().lower()
    if mode not in {"legacy", "database"}:
        raise ValueError("MODEL_PRICING_MODE must be legacy or database")
    return mode == "database"


@dataclass(frozen=True)
class PricingPolicy:
    auto_activate: bool
    max_increase: Decimal
    max_decrease: Decimal
    stale_hours: int
    cache_seconds: int


def load_pricing_policy() -> PricingPolicy:
    policy = PricingPolicy(
        auto_activate=os.getenv("MODEL_PRICING_ALLOW_AUTO_ACTIVATION", "false").lower()
        in {"1", "true", "yes", "on"},
        max_increase=Decimal(os.getenv("MODEL_PRICING_MAX_INCREASE_RATIO", "2")),
        max_decrease=Decimal(os.getenv("MODEL_PRICING_MAX_DECREASE_RATIO", "0.5")),
        stale_hours=int(os.getenv("MODEL_PRICING_STALE_HOURS", "48")),
        cache_seconds=int(os.getenv("MODEL_PRICING_CACHE_SECONDS", "60")),
    )
    if (
        not policy.max_increase.is_finite()
        or not policy.max_decrease.is_finite()
        or policy.max_increase <= 1
        or not 0 < policy.max_decrease < 1
        or policy.stale_hours <= 0
        or not 0 <= policy.cache_seconds <= 300
    ):
        raise ValueError("Invalid model pricing policy")
    return policy
