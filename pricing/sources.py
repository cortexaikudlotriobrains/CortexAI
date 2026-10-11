"""Synchronization-only sources; inference never calls these adapters."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Protocol

import httpx

from pricing.identity import PROVIDERS
from pricing.models import amount, validate_rates

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
MAX_CATALOG_BYTES = 16 * 1024 * 1024
FIELDS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cached_input": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
    "cache_write_1h": "cache_creation_input_token_cost_above_1hr",
    "reasoning": "output_cost_per_reasoning_token",
}
SERVICE_FIELDS = {"search_context_cost_per_query", "google_maps_grounding_cost_per_query"}


def service_charge(field: str, value: Any) -> Any:
    """Retain USD/unit service evidence outside model-token cards."""
    if field == "search_context_cost_per_query":
        if not isinstance(value, dict) or set(value) != {
            "search_context_size_low",
            "search_context_size_medium",
            "search_context_size_high",
        }:
            raise ValueError("Unsupported search-context charge dimensions")
        return {key: str(amount(rate)) for key, rate in sorted(value.items())}
    return str(amount(value))


@dataclass(frozen=True)
class Catalog:
    version: str
    reference: str
    entries: dict[str, dict[str, Any]]


class PricingSource(Protocol):
    def fetch(self) -> Catalog: ...


class LiteLLMPricingSource:
    def fetch(self) -> Catalog:
        # Fixed HTTPS URL, TLS verification, no redirects, bounded size/time.
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
            with client.stream("GET", LITELLM_URL) as response:
                response.raise_for_status()
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_CATALOG_BYTES:
                        raise ValueError("Pricing catalog exceeds size limit")
        return self.parse(bytes(payload))

    @staticmethod
    def parse(payload: bytes) -> Catalog:
        if len(payload) > MAX_CATALOG_BYTES:
            raise ValueError("Pricing catalog exceeds size limit")

        def reject_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON numeric constant: {value}")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON pricing key")
                result[key] = value
            return result

        data = json.loads(
            payload,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
        if not isinstance(data, dict) or not data or len(data) > 50000:
            raise ValueError("Invalid pricing catalog object")
        entries = {}
        for external, row in data.items():
            if external in {"sample_spec", "fallback_generalizations"}:
                continue
            if not isinstance(row, dict) or not isinstance(external, str) or len(external) > 300:
                raise ValueError("Malformed pricing entry")
            provider = PROVIDERS.get(str(row.get("litellm_provider", "")))
            if not provider or row.get("mode", "chat") not in {"chat", "responses"}:
                continue
            entry: dict[str, Any] = {
                "provider": provider,
                "rates": None,
                "error": None,
                "separate_charges": {},
                "included_input_dimensions": [],
            }
            try:
                if row.get("currency", "USD") != "USD":
                    raise ValueError("Unsupported currency")
                tokens = {
                    key: str(amount(row[field]) * 1_000_000)
                    for key, field in FIELDS.items()
                    if field in row
                }
                if not {"input", "output"} <= set(tokens) or any(
                    amount(tokens[k]) == 0 for k in ("input", "output")
                ):
                    raise ValueError("Missing or unexpectedly zero core token prices")
                if "input_cost_per_token_cache_hit" in row:
                    cached = str(amount(row["input_cost_per_token_cache_hit"]) * 1_000_000)
                    if "cached_input" in tokens and amount(tokens["cached_input"]) != amount(
                        cached
                    ):
                        raise ValueError("Conflicting cache-read rates")
                    tokens["cached_input"] = cached
                bands: dict[int, dict[str, str]] = {}
                supported_fields = set(FIELDS.values()) | {"input_cost_per_token_cache_hit"}
                for field in SERVICE_FIELDS & row.keys():
                    entry["separate_charges"][field] = service_charge(field, row[field])
                    supported_fields.add(field)
                if "input_cost_per_image_token" in row:
                    if amount(row["input_cost_per_image_token"]) != amount(
                        row["input_cost_per_token"]
                    ):
                        raise ValueError(
                            "Image-token rate requires a distinct normalized usage partition"
                        )
                    supported_fields.add("input_cost_per_image_token")
                    entry["included_input_dimensions"].append("image")
                for field, value in row.items():
                    match = re.fullmatch(r"(.+)_above_(\d+)k_tokens", field)
                    if match and match[1] in FIELDS.values():
                        key = next(key for key, name in FIELDS.items() if name == match[1])
                        minimum = int(match[2]) * 1000 + 1  # LiteLLM says "above", not >=.
                        bands.setdefault(minimum, {})[key] = str(amount(value) * 1_000_000)
                        supported_fields.add(field)
                # Tier/batch fields are outside this standard-only adapter. Unknown
                # standard billable dimensions must be reviewed, not dropped.
                unknown = [
                    key
                    for key, value in row.items()
                    if "cost" in key
                    and key not in supported_fields
                    and not key.endswith(("_batches", "_priority", "_flex", "_ultrafast"))
                    and value not in (None, 0, "0")
                ]
                if unknown:
                    raise ValueError(
                        "Unsupported standard charge dimensions: " + ",".join(sorted(unknown))
                    )
                entry["rates"] = validate_rates(
                    {
                        "tokens": tokens,
                        "units": {},
                        "bands": [
                            {"minimum_input_tokens": minimum, "tokens": vals}
                            for minimum, vals in bands.items()
                        ],
                    }
                )
            except (ValueError, TypeError) as exc:
                entry["error"] = str(exc)
            entries[external] = entry
        if not entries:
            raise ValueError("Catalog contains no supported provider models")
        return Catalog(hashlib.sha256(payload).hexdigest(), LITELLM_URL, entries)


class CortexManualPricingSource:
    """Trusted operator input; approval identity is supplied separately by CLI."""

    @staticmethod
    def parse(payload: str) -> dict[str, Any]:
        data = json.loads(payload, parse_float=Decimal)
        if not isinstance(data, dict) or set(data) != {
            "provider",
            "model",
            "processing_mode",
            "rates",
        }:
            raise ValueError("Manual card requires provider, model, processing_mode, rates")
        if data["provider"] not in set(PROVIDERS.values()) or data["processing_mode"] != "standard":
            raise ValueError("Unsupported manual provider or mode")
        data["rates"] = validate_rates(data["rates"])
        return data
