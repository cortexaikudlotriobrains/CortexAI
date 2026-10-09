"""Validated usage and rate dimensions. Monetary values serialize as strings."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

TOKEN_KEYS = frozenset(
    {"input", "output", "cached_input", "cache_write", "cache_write_1h", "reasoning"}
)
UNIT_KEYS = frozenset(
    {
        "web_search_count",
        "request_count",
        "active_seconds",
        "image_input_units",
        "image_output_units",
        "audio_input_units",
        "audio_output_units",
    }
)
CALCULATION_VERSION = "rate-card-v1"


class PricingUnavailableError(RuntimeError):
    """PRICING_UNKNOWN: no approved applicable rate; never a zero fallback."""


def utc(value: datetime | str | None = None) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if value
            else datetime.now(timezone.utc)
        )
    )
    return (
        result.replace(tzinfo=timezone.utc)
        if result.tzinfo is None
        else result.astimezone(timezone.utc)
    )


def amount(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing or invalid price")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid price") from exc
    if not result.is_finite() or result < 0 or result > Decimal("1000000000"):
        raise ValueError("Price must be finite and nonnegative within supported range")
    if result != 0 and result < Decimal("1e-30"):
        raise ValueError("Price exceeds supported decimal precision")
    return result


def rate_string(value: Any) -> str:
    parsed = amount(value)
    if parsed == 0:
        return "0"
    result = format(parsed, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def validate_rates(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"tokens", "units", "bands", "schedule"}:
        raise ValueError("Unsupported rate-card fields")
    tokens = value.get("tokens", {})
    units = value.get("units", {})
    if not isinstance(tokens, Mapping) or not isinstance(units, Mapping):
        raise ValueError("Invalid pricing components")
    if set(tokens) - TOKEN_KEYS or set(units) - UNIT_KEYS or not {"input", "output"} <= set(tokens):
        raise ValueError("Unknown or missing pricing dimensions")
    normalized = {key: rate_string(val) for key, val in tokens.items()}
    normalized.setdefault("cached_input", normalized["input"])
    normalized.setdefault("cache_write", normalized["input"])
    normalized.setdefault("cache_write_1h", normalized["cache_write"])
    bands = []
    for band in value.get("bands", []):
        if not isinstance(band, Mapping) or set(band) != {"minimum_input_tokens", "tokens"}:
            raise ValueError("Invalid long-context band")
        threshold = band["minimum_input_tokens"]
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
            raise ValueError("Invalid long-context threshold")
        band_tokens = band["tokens"]
        if not isinstance(band_tokens, Mapping) or set(band_tokens) - TOKEN_KEYS:
            raise ValueError("Unsupported long-context dimension")
        bands.append(
            {
                "minimum_input_tokens": threshold,
                "tokens": {key: rate_string(val) for key, val in band_tokens.items()},
            }
        )
    bands.sort(key=lambda band: band["minimum_input_tokens"])
    if len({band["minimum_input_tokens"] for band in bands}) != len(bands):
        raise ValueError("Duplicate long-context threshold")
    result = {
        "tokens": normalized,
        "units": {key: rate_string(val) for key, val in units.items()},
        "bands": bands,
    }
    if "schedule" in value:
        from pricing.schedules import validate_schedule

        if bands:
            raise ValueError("Combined context and clock tiers require explicit support")
        result["schedule"] = validate_schedule(value["schedule"], normalized)
    return result


@dataclass(frozen=True)
class NormalizedLLMUsage:
    # Input includes cache reads/writes; output includes reasoning. No double billing.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    units: Mapping[str, int] = field(default_factory=dict)
    estimated: bool = False
    cache_write_1h_tokens: int = 0

    def __post_init__(self) -> None:
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "cache_write_1h_tokens",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid usage: {key}")
        if set(self.units) - UNIT_KEYS or any(
            isinstance(val, bool) or not isinstance(val, int) or val < 0
            for val in self.units.values()
        ):
            raise ValueError("Unsupported billable usage units")
        cached = min(self.input_tokens, self.cached_input_tokens)
        object.__setattr__(self, "cached_input_tokens", cached)
        object.__setattr__(
            self, "cache_write_tokens", min(self.input_tokens - cached, self.cache_write_tokens)
        )
        object.__setattr__(self, "reasoning_tokens", min(self.output_tokens, self.reasoning_tokens))
        object.__setattr__(
            self, "cache_write_1h_tokens", min(self.cache_write_tokens, self.cache_write_1h_tokens)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CostCalculationResult:
    usage: NormalizedLLMUsage
    snapshot: Mapping[str, Any]
    components: Mapping[str, Decimal]
    total: Decimal

    def audit(self) -> dict[str, Any]:
        return {
            "normalized_usage": self.usage.to_dict(),
            "cost_components_usd": {key: str(val) for key, val in self.components.items()},
            "usage_calculated_provider_cost_usd": str(self.total),
            "calculation_version": CALCULATION_VERSION,
            "cost_kind": (
                "estimated_provider_cost"
                if self.usage.estimated
                else "usage_calculated_provider_cost"
            ),
        }
