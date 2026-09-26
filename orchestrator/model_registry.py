from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from config.pricing import ModelPricing
from orchestrator.routing_types import ModelCandidate, RoutingConstraints, Tier
from orchestrator.reasoning_capabilities import candidate_supports_reasoning_request
from server.billing.models import ALLOWED_MODEL_BILLING_CLASSES, ModelBillingClass
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ModelRegistry:
    _providers: dict[str, list[ModelCandidate]]
    _routing_defaults: dict[str, Any]
    _catalog_path: str

    @classmethod
    def from_yaml(cls, path: str | None = None) -> "ModelRegistry":
        registry_path = (
            Path(path)
            if path
            else Path(__file__).resolve().parent.parent / "config" / "model_registry.yaml"
        )
        if not registry_path.exists():
            raise ValueError(f"Model registry not found at {registry_path}")

        data = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        if not data or "providers" not in data:
            raise ValueError("Invalid model registry: missing providers")

        providers: dict[str, list[ModelCandidate]] = {}
        for provider, pdata in data["providers"].items():
            provider_defaults = pdata.get("defaults", {})
            if not isinstance(provider_defaults, dict):
                provider_defaults = {}
            models = pdata.get("models", [])
            if not isinstance(models, list):
                raise ValueError(f"Invalid models list for provider {provider}")
            candidates: list[ModelCandidate] = []
            for model in models:
                required = [
                    "name",
                    "tier",
                    "billing_class",
                    "input_credit_multiplier",
                    "output_credit_multiplier",
                    "credit_usage_label",
                    "credit_pricing_version",
                    "context_limit",
                    "tags",
                ]
                if any(key not in model for key in required):
                    raise ValueError(f"Missing required fields in model for provider {provider}")
                tier = Tier(model["tier"])
                raw_billing_class = model.get("billing_class")
                if raw_billing_class in (None, ""):
                    raise ValueError(
                        f"Missing billing_class for {provider}:{model['name']}; "
                        "unclassified models are unavailable"
                    )
                else:
                    normalized_billing_class = str(raw_billing_class).strip().lower()
                    if normalized_billing_class not in ALLOWED_MODEL_BILLING_CLASSES:
                        allowed_text = ", ".join(sorted(ALLOWED_MODEL_BILLING_CLASSES))
                        raise ValueError(
                            "Invalid billing_class "
                            f"'{raw_billing_class}' for {provider}:{model['name']}; "
                            f"expected one of: {allowed_text}"
                        )
                    billing_class = ModelBillingClass(normalized_billing_class)
                supported_mime_types = model.get(
                    "supported_attachment_mime_types",
                    provider_defaults.get("supported_attachment_mime_types", []),
                )
                if not isinstance(supported_mime_types, list):
                    supported_mime_types = []
                max_attachment_bytes_raw = model.get(
                    "max_attachment_bytes",
                    provider_defaults.get("max_attachment_bytes"),
                )
                max_attachment_bytes = None
                if max_attachment_bytes_raw not in (None, ""):
                    max_attachment_bytes = int(max_attachment_bytes_raw)

                max_attachments_raw = model.get(
                    "max_attachments_per_request",
                    provider_defaults.get("max_attachments_per_request"),
                )
                max_attachments = None
                if max_attachments_raw not in (None, ""):
                    max_attachments = int(max_attachments_raw)

                pricing_snapshot: dict[str, Any] | None = None
                if "input_cost_per_1m" in model and "output_cost_per_1m" in model:
                    input_cost_per_1m = float(model["input_cost_per_1m"])
                    output_cost_per_1m = float(model["output_cost_per_1m"])
                else:
                    pricing_snapshot = ModelPricing.get_pricing_snapshot(
                        provider,
                        str(model["name"]),
                        catalog_path=registry_path,
                    )
                    if pricing_snapshot is None:
                        raise ValueError(
                            f"Missing effective pricing rule for {provider}:{model['name']}"
                        )
                    input_cost_per_1m = float(pricing_snapshot["input"])
                    output_cost_per_1m = float(pricing_snapshot["output"])

                lifecycle = model.get("lifecycle")
                if not isinstance(lifecycle, dict):
                    lifecycle = {}
                aliases = model.get("aliases", [])
                if not isinstance(aliases, list):
                    aliases = []
                reasoning_modes = model.get("reasoning_modes", [])
                if not isinstance(reasoning_modes, list):
                    reasoning_modes = []
                reasoning_efforts = model.get("reasoning_efforts", [])
                if not isinstance(reasoning_efforts, list):
                    reasoning_efforts = []
                pricing_source_url = str(
                    model.get("pricing_source_url")
                    or provider_defaults.get("pricing_source_url")
                    or ""
                ).strip()
                lifecycle_source_url = str(
                    model.get("lifecycle_source_url")
                    or provider_defaults.get("lifecycle_source_url")
                    or ""
                ).strip()
                source_verified_at = str(
                    model.get("source_verified_at")
                    or provider_defaults.get("source_verified_at")
                    or ""
                ).strip()

                candidates.append(
                    ModelCandidate(
                        provider=provider,
                        model_name=model["name"],
                        tier=tier,
                        input_cost_per_1m=input_cost_per_1m,
                        output_cost_per_1m=output_cost_per_1m,
                        context_limit=int(model["context_limit"]),
                        tags=list(model.get("tags", [])),
                        billing_class=billing_class,
                        input_credit_multiplier=_positive_float(
                            model["input_credit_multiplier"],
                            f"{provider}:{model['name']}.input_credit_multiplier",
                        ),
                        output_credit_multiplier=_positive_float(
                            model["output_credit_multiplier"],
                            f"{provider}:{model['name']}.output_credit_multiplier",
                        ),
                        credit_usage_label=_required_text(
                            model["credit_usage_label"],
                            f"{provider}:{model['name']}.credit_usage_label",
                        ),
                        credit_pricing_version=_required_text(
                            model["credit_pricing_version"],
                            f"{provider}:{model['name']}.credit_pricing_version",
                        ),
                        enabled=bool(model.get("enabled", True)),
                        selectable=bool(model.get("selectable", model.get("enabled", True))),
                        display_name=str(model.get("display_name") or model["name"]),
                        description=str(model.get("description") or ""),
                        release_status=str(model.get("release_status") or "unknown"),
                        lifecycle_status=str(lifecycle.get("status") or "ACTIVE").upper(),
                        replacement_model=(
                            str(lifecycle.get("replacement_model") or "").strip() or None
                        ),
                        retirement_date=(
                            str(lifecycle.get("retirement_date") or "").strip() or None
                        ),
                        migration_reason=(
                            str(lifecycle.get("migration_reason") or "").strip() or None
                        ),
                        pricing_model=(
                            str(model.get("pricing_model") or "").strip()
                            or (
                                str(pricing_snapshot.get("pricing_model") or "").strip()
                                if pricing_snapshot
                                else str(model["name"])
                            )
                        ),
                        pricing_rule_id=(
                            str(pricing_snapshot.get("pricing_rule_id") or "").strip() or None
                            if pricing_snapshot
                            else None
                        ),
                        pricing_effective_from=(
                            str(pricing_snapshot.get("effective_from") or "").strip() or None
                            if pricing_snapshot
                            else None
                        ),
                        pricing_effective_until=(
                            str(pricing_snapshot.get("effective_until") or "").strip() or None
                            if pricing_snapshot
                            else None
                        ),
                        long_context_threshold_tokens=(
                            int(pricing_snapshot["long_context_threshold_tokens"])
                            if pricing_snapshot
                            and pricing_snapshot.get("long_context_threshold_tokens")
                            else None
                        ),
                        cached_input_cost_per_1m=(
                            float(pricing_snapshot["cached_input"])
                            if pricing_snapshot is not None
                            else None
                        ),
                        cache_write_cost_per_1m=(
                            float(pricing_snapshot["cache_write"])
                            if pricing_snapshot is not None
                            else None
                        ),
                        max_output_tokens=(
                            int(model["max_output_tokens"])
                            if model.get("max_output_tokens") not in (None, "")
                            else None
                        ),
                        aliases=[str(alias).strip() for alias in aliases if str(alias).strip()],
                        reasoning_modes=[
                            str(mode).strip() for mode in reasoning_modes if str(mode).strip()
                        ],
                        default_reasoning_mode=(
                            str(model.get("default_reasoning_mode") or "").strip() or None
                        ),
                        reasoning_efforts=[
                            str(effort).strip()
                            for effort in reasoning_efforts
                            if str(effort).strip()
                        ],
                        reasoning_disable_supported=bool(
                            model.get("reasoning_disable_supported", True)
                        ),
                        reasoning_counts_against_output=bool(
                            model.get("reasoning_counts_against_output", True)
                        ),
                        pricing_source_url=pricing_source_url or None,
                        lifecycle_source_url=lifecycle_source_url or None,
                        source_verified_at=source_verified_at or None,
                        supports_image_input=bool(
                            model.get(
                                "supports_image_input",
                                provider_defaults.get("supports_image_input", False),
                            )
                        ),
                        supported_attachment_mime_types=[
                            str(mime).strip().lower()
                            for mime in supported_mime_types
                            if str(mime).strip()
                        ],
                        max_attachment_bytes=max_attachment_bytes,
                        max_attachments_per_request=max_attachments,
                    )
                )
            providers[provider] = candidates

        routing_defaults = data.get("routing_defaults", {})
        return cls(
            _providers=providers,
            _routing_defaults=routing_defaults,
            _catalog_path=str(registry_path),
        )

    def routing_defaults(self) -> dict[str, Any]:
        return self._routing_defaults

    def next_tier(self, tier: Tier) -> Tier | None:
        tier_order = self._routing_defaults.get("tier_order", ["T0", "T1", "T2", "T3"])
        if tier.value not in tier_order:
            raise ValueError(f"Invalid tier {tier}")
        idx = tier_order.index(tier.value)
        if idx + 1 >= len(tier_order):
            return None
        return Tier(tier_order[idx + 1])

    def get_candidates(
        self, tier: Tier, constraints: RoutingConstraints | None = None
    ) -> list[ModelCandidate]:
        if not isinstance(tier, Tier):
            raise ValueError(f"Invalid tier: {tier}")

        allowed = None
        if constraints and constraints.allowed_providers is not None:
            allowed = [p.lower() for p in constraints.allowed_providers]
        else:
            allow_defaults = self._routing_defaults.get("allow_providers")
            if allow_defaults:
                allowed = [p.lower() for p in allow_defaults]

        allowed_billing_classes = None
        if constraints and constraints.allowed_billing_classes is not None:
            allowed_billing_classes = {
                str(item or "").strip().lower()
                for item in constraints.allowed_billing_classes
                if str(item or "").strip()
            }
        allowed_models = None
        if constraints and constraints.allowed_models is not None:
            allowed_models = {
                str(item or "").strip().lower()
                for item in constraints.allowed_models
                if str(item or "").strip()
            }

        results: list[ModelCandidate] = []
        for provider, models in self._providers.items():
            if allowed and provider.lower() not in allowed:
                continue
            for candidate in models:
                if not candidate.enabled:
                    continue
                if not candidate.selectable:
                    continue
                if candidate.tier != tier:
                    continue
                if (
                    allowed_billing_classes is not None
                    and candidate.billing_class.value not in allowed_billing_classes
                ):
                    continue
                if (
                    allowed_models is not None
                    and f"{candidate.provider}:{candidate.model_name}".lower() not in allowed_models
                ):
                    continue
                if constraints and constraints.min_context_limit is not None:
                    if candidate.context_limit < constraints.min_context_limit:
                        continue
                if constraints and not candidate_supports_reasoning_request(
                    candidate,
                    mode=constraints.reasoning_mode,
                    effort=constraints.reasoning_effort,
                ):
                    continue
                results.append(candidate)

        return results

    def find_model(self, provider: str, model_name: str) -> ModelCandidate | None:
        provider_norm = (provider or "").lower().strip()
        model_norm = (model_name or "").strip()
        if not provider_norm or not model_norm:
            return None

        models = self._providers.get(provider_norm, [])
        for candidate in models:
            if candidate.model_name == model_norm or model_norm in candidate.aliases:
                return candidate
        return None

    def resolve_model_identity(self, provider: str, model_name: str) -> dict[str, Any] | None:
        return ModelPricing.resolve_model_identity(
            provider,
            model_name,
            catalog_path=self._catalog_path,
        )

    def is_enabled_model(self, provider: str, model_name: str) -> bool:
        candidate = self.find_model(provider, model_name)
        return bool(candidate and candidate.enabled)

    def list_enabled_models(self) -> list[ModelCandidate]:
        out: list[ModelCandidate] = []
        for candidates in self._providers.values():
            out.extend([c for c in candidates if c.enabled])
        return out

    def list_selectable_models(self, provider: str | None = None) -> list[ModelCandidate]:
        return [
            candidate
            for candidate in self.list_models(provider=provider, include_disabled=False)
            if candidate.selectable
        ]

    def list_models(
        self,
        provider: str | None = None,
        *,
        include_disabled: bool = True,
    ) -> list[ModelCandidate]:
        provider_norm = (provider or "").strip().lower()
        if provider_norm:
            candidates = list(self._providers.get(provider_norm, []))
        else:
            candidates = []
            for provider_candidates in self._providers.values():
                candidates.extend(provider_candidates)

        if include_disabled:
            return candidates
        return [candidate for candidate in candidates if candidate.enabled]


def _positive_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Invalid {label}: expected a positive number")
    if not isinstance(value, (str, int, float)):
        raise ValueError(f"Invalid {label}: expected a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {label}: expected a positive number") from exc
    if parsed <= 0:
        raise ValueError(f"Invalid {label}: expected a positive number")
    return parsed


def _required_text(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Invalid {label}: value is required")
    return normalized
