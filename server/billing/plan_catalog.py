"""Load and validate the server-owned subscription plan catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from server.billing.models import (
    ALLOWED_MODEL_BILLING_CLASSES,
    PlanAllowances,
    PlanEntitlements,
    PlanLimits,
    SubscriptionPlan,
)

_ALLOWANCE_KEYS = frozenset(PlanAllowances.__dataclass_fields__)
_LIMIT_KEYS = frozenset(PlanLimits.__dataclass_fields__)
_ENTITLEMENT_KEYS = frozenset(PlanEntitlements.__dataclass_fields__)
_PLAN_KEYS = frozenset({"display_name", "rank", "price", "entitlements", "allowances", "limits"})
_PRICE_KEYS = frozenset({"monthly_usd", "stripe_price_env"})


@dataclass(frozen=True)
class PlanCatalog:
    version: int
    _plans: Mapping[str, SubscriptionPlan]

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "PlanCatalog":
        catalog_path = (
            Path(path)
            if path is not None
            else Path(__file__).resolve().parents[2] / "config" / "subscription_plans.yaml"
        )
        if not catalog_path.exists():
            raise ValueError(f"Subscription plan catalog not found at {catalog_path}")

        try:
            data = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid subscription plan catalog YAML: {exc}") from exc

        root = _require_mapping(data, "catalog")
        version = _require_non_negative_int(root.get("version"), "catalog.version")
        if version <= 0:
            raise ValueError("Invalid subscription plan catalog: version must be positive")

        raw_plans = _require_mapping(root.get("plans"), "catalog.plans")
        if "free" not in raw_plans:
            raise ValueError("Invalid subscription plan catalog: missing required 'free' plan")

        plans: dict[str, SubscriptionPlan] = {}
        ranks: dict[int, str] = {}
        for raw_code, raw_plan in raw_plans.items():
            code = str(raw_code or "").strip().lower()
            if not code:
                raise ValueError("Invalid subscription plan catalog: plan code is empty")
            if code in plans:
                raise ValueError(f"Invalid subscription plan catalog: duplicate plan code '{code}'")

            plan = _parse_plan(code, raw_plan)
            previous_code = ranks.get(plan.rank)
            if previous_code is not None:
                raise ValueError(
                    "Invalid subscription plan catalog: duplicate plan rank "
                    f"{plan.rank} for '{previous_code}' and '{code}'"
                )
            ranks[plan.rank] = code
            plans[code] = plan

        return cls(version=version, _plans=MappingProxyType(plans))

    def get(self, code: str) -> SubscriptionPlan | None:
        return self._plans.get(str(code or "").strip().lower())

    def require(self, code: str) -> SubscriptionPlan:
        plan = self.get(code)
        if plan is None:
            raise KeyError(f"Unknown subscription plan '{code}'")
        return plan

    def list_plans(self) -> tuple[SubscriptionPlan, ...]:
        return tuple(sorted(self._plans.values(), key=lambda plan: plan.rank))


def _parse_plan(code: str, raw_plan: Any) -> SubscriptionPlan:
    plan = _require_mapping(raw_plan, f"plans.{code}")
    _reject_unknown_keys(plan, _PLAN_KEYS, f"plans.{code}")
    _require_keys(plan, _PLAN_KEYS, f"plans.{code}")

    display_name = str(plan["display_name"] or "").strip()
    if not display_name:
        raise ValueError(f"Invalid subscription plan '{code}': display_name is required")
    rank = _require_non_negative_int(plan["rank"], f"plans.{code}.rank")

    price = _require_mapping(plan["price"], f"plans.{code}.price")
    _reject_unknown_keys(price, _PRICE_KEYS, f"plans.{code}.price")
    _require_keys(price, _PRICE_KEYS, f"plans.{code}.price")
    monthly_price_usd = _require_decimal(price["monthly_usd"], f"plans.{code}.price.monthly_usd")
    stripe_price_env_raw = price["stripe_price_env"]
    stripe_price_env = (
        str(stripe_price_env_raw).strip() if stripe_price_env_raw is not None else None
    )
    if stripe_price_env == "":
        stripe_price_env = None

    if code == "free":
        if monthly_price_usd != Decimal("0"):
            raise ValueError("Invalid subscription plan 'free': monthly price must equal zero")
    else:
        if monthly_price_usd <= Decimal("0"):
            raise ValueError(
                f"Invalid paid subscription plan '{code}': monthly price must be positive"
            )
        if stripe_price_env is None:
            raise ValueError(
                f"Invalid paid subscription plan '{code}': stripe_price_env is required"
            )

    allowances_raw = _require_mapping(plan["allowances"], f"plans.{code}.allowances")
    _reject_unknown_keys(allowances_raw, _ALLOWANCE_KEYS, f"plans.{code}.allowances")
    _require_keys(allowances_raw, _ALLOWANCE_KEYS, f"plans.{code}.allowances")
    allowances = PlanAllowances(
        **{
            key: _require_non_negative_int(allowances_raw[key], f"plans.{code}.allowances.{key}")
            for key in _ALLOWANCE_KEYS
        }
    )

    limits_raw = _require_mapping(plan["limits"], f"plans.{code}.limits")
    _reject_unknown_keys(limits_raw, _LIMIT_KEYS, f"plans.{code}.limits")
    _require_keys(limits_raw, _LIMIT_KEYS, f"plans.{code}.limits")
    limits = PlanLimits(
        **{
            key: _require_non_negative_int(limits_raw[key], f"plans.{code}.limits.{key}")
            for key in _LIMIT_KEYS
        }
    )

    entitlements_raw = _require_mapping(plan["entitlements"], f"plans.{code}.entitlements")
    _reject_unknown_keys(entitlements_raw, _ENTITLEMENT_KEYS, f"plans.{code}.entitlements")
    _require_keys(entitlements_raw, _ENTITLEMENT_KEYS, f"plans.{code}.entitlements")

    allowed_billing_classes_raw = entitlements_raw["allowed_billing_classes"]
    if not isinstance(allowed_billing_classes_raw, list):
        raise ValueError(
            f"Invalid subscription plan '{code}': allowed_billing_classes must be a list"
        )
    allowed_billing_classes = frozenset(
        str(value or "").strip().lower() for value in allowed_billing_classes_raw
    )
    unknown_billing_classes = allowed_billing_classes - ALLOWED_MODEL_BILLING_CLASSES
    if unknown_billing_classes:
        unknown_text = ", ".join(sorted(unknown_billing_classes))
        raise ValueError(
            f"Invalid subscription plan '{code}': unknown billing class(es): {unknown_text}"
        )

    compare_enabled = _require_bool(
        entitlements_raw["compare_enabled"], f"plans.{code}.entitlements.compare_enabled"
    )
    max_compare_models = _require_non_negative_int(
        entitlements_raw["max_compare_models"],
        f"plans.{code}.entitlements.max_compare_models",
    )
    if compare_enabled and max_compare_models < 2:
        raise ValueError(
            f"Invalid subscription plan '{code}': max_compare_models must be at least 2 "
            "when Compare is enabled"
        )

    entitlements = PlanEntitlements(
        compare_enabled=compare_enabled,
        max_compare_models=max_compare_models,
        research_enabled=_require_bool(
            entitlements_raw["research_enabled"],
            f"plans.{code}.entitlements.research_enabled",
        ),
        prompt_improvement_enabled=_require_bool(
            entitlements_raw["prompt_improvement_enabled"],
            f"plans.{code}.entitlements.prompt_improvement_enabled",
        ),
        file_analysis_enabled=_require_bool(
            entitlements_raw["file_analysis_enabled"],
            f"plans.{code}.entitlements.file_analysis_enabled",
        ),
        usage_export_enabled=_require_bool(
            entitlements_raw["usage_export_enabled"],
            f"plans.{code}.entitlements.usage_export_enabled",
        ),
        saved_history_enabled=_require_bool(
            entitlements_raw["saved_history_enabled"],
            f"plans.{code}.entitlements.saved_history_enabled",
        ),
        models_catalog_enabled=_require_bool(
            entitlements_raw["models_catalog_enabled"],
            f"plans.{code}.entitlements.models_catalog_enabled",
        ),
        work_enabled=_require_bool(
            entitlements_raw["work_enabled"],
            f"plans.{code}.entitlements.work_enabled",
        ),
        verified_connectors_enabled=_require_bool(
            entitlements_raw["verified_connectors_enabled"],
            f"plans.{code}.entitlements.verified_connectors_enabled",
        ),
        custom_mcp_enabled=_require_bool(
            entitlements_raw["custom_mcp_enabled"],
            f"plans.{code}.entitlements.custom_mcp_enabled",
        ),
        action_tools_enabled=_require_bool(
            entitlements_raw["action_tools_enabled"],
            f"plans.{code}.entitlements.action_tools_enabled",
        ),
        allowed_billing_classes=allowed_billing_classes,
    )

    return SubscriptionPlan(
        code=code,
        display_name=display_name,
        rank=rank,
        monthly_price_usd=monthly_price_usd,
        stripe_price_env=stripe_price_env,
        entitlements=entitlements,
        allowances=allowances,
        limits=limits,
    )


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid subscription plan catalog: {label} must be a mapping")
    return value


def _require_keys(value: Mapping[str, Any], required: frozenset[str], label: str) -> None:
    missing = required - set(value)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ValueError(f"Invalid subscription plan catalog: {label} missing: {missing_text}")


def _reject_unknown_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        unknown_text = ", ".join(sorted(unknown))
        raise ValueError(
            f"Invalid subscription plan catalog: {label} has unknown key(s): {unknown_text}"
        )


def _require_non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid subscription plan catalog: {label} must be an integer")
    if value < 0:
        raise ValueError(f"Invalid subscription plan catalog: {label} cannot be negative")
    return value


def _require_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"Invalid subscription plan catalog: {label} must be numeric")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid subscription plan catalog: {label} must be numeric") from exc
    if not amount.is_finite():
        raise ValueError(f"Invalid subscription plan catalog: {label} must be finite")
    return amount


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"Invalid subscription plan catalog: {label} must be a boolean")
    return value


@lru_cache(maxsize=1)
def get_plan_catalog() -> PlanCatalog:
    """Return the validated process-wide plan catalogue."""

    return PlanCatalog.from_yaml()
