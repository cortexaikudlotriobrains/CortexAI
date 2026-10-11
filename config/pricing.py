"""Effective-dated provider pricing with a staged local rate-card facade.

All token rates are USD per million. The registry supplies product identities
and legacy bootstrap/rollback rates; database mode uses approved local versions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CATALOG_PATH = Path(__file__).resolve().parent / "model_registry.yaml"
_RUNTIME_MIGRATION_STATES = {"RETIRED", "ALIAS_REDIRECTED"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_utc(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    return _coerce_utc(str(value))


@lru_cache(maxsize=8)
def _load_catalog(catalog_path: str) -> dict[str, Any]:
    path = Path(catalog_path)
    if not path.exists():
        raise ValueError(f"Model catalogue not found at {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        raise ValueError("Invalid model catalogue: missing providers")
    return data


def _catalog(catalog_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(catalog_path) if catalog_path else _DEFAULT_CATALOG_PATH
    return _load_catalog(str(path.resolve()))


def _provider_block(
    provider: str,
    *,
    catalog_path: str | Path | None = None,
) -> dict[str, Any] | None:
    providers = _catalog(catalog_path).get("providers", {})
    block = providers.get(str(provider or "").strip().lower())
    return block if isinstance(block, dict) else None


def _find_model_record(
    provider: str,
    model_name: str,
    *,
    catalog_path: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
    provider_data = _provider_block(provider, catalog_path=catalog_path)
    if provider_data is None:
        return None
    requested = str(model_name or "").strip()
    for raw_model in provider_data.get("models", []):
        if not isinstance(raw_model, dict):
            continue
        canonical = str(raw_model.get("name") or "").strip()
        aliases = {
            str(alias).strip() for alias in (raw_model.get("aliases") or []) if str(alias).strip()
        }
        if requested == canonical:
            return raw_model, provider_data, False
        if requested in aliases:
            return raw_model, provider_data, True
    return None


def _rule_is_effective(rule: dict[str, Any], at: datetime) -> bool:
    effective_from = _optional_utc(rule.get("effective_from"))
    effective_until = _optional_utc(rule.get("effective_until"))
    if effective_from is not None and at < effective_from:
        return False
    if effective_until is not None and at >= effective_until:
        return False
    return True


def _select_rule(
    record: dict[str, Any],
    *,
    at: datetime,
    processing_mode: str,
) -> dict[str, Any] | None:
    effective: list[dict[str, Any]] = []
    for raw_rule in record.get("pricing_rules", []):
        if not isinstance(raw_rule, dict):
            continue
        mode = str(raw_rule.get("processing_mode") or "standard").strip().lower()
        if mode != processing_mode:
            continue
        if _rule_is_effective(raw_rule, at):
            effective.append(raw_rule)
    if not effective:
        return None
    effective.sort(
        key=lambda rule: _optional_utc(rule.get("effective_from"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return effective[0]


def _number(value: Any, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    return float(value)


class RegistryPricing:
    """Read current and historical pricing from ``model_registry.yaml``."""

    @classmethod
    def catalogue_version(cls, *, catalog_path: str | Path | None = None) -> str:
        return str(_catalog(catalog_path).get("catalog_version") or "unknown")

    @classmethod
    def resolve_model_identity(
        cls,
        model_type: str,
        model_name: str,
        *,
        at: datetime | str | None = None,
        catalog_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Resolve requested, provider-runtime, and pricing identities.

        Retired local compatibility IDs are migrated to their replacement.  A
        future scheduled retirement starts migrating only when its timestamp is
        reached.  Provider aliases declared in ``aliases`` resolve to the
        canonical catalogue ID without losing the originally requested value.
        """

        provider = str(model_type or "").strip().lower()
        requested_model = str(model_name or "").strip()
        found = _find_model_record(provider, requested_model, catalog_path=catalog_path)
        if found is None:
            return None
        record, provider_data, matched_alias = found
        canonical_model = str(record.get("name") or requested_model).strip()
        raw_lifecycle = record.get("lifecycle")
        lifecycle: dict[str, Any] = raw_lifecycle if isinstance(raw_lifecycle, dict) else {}
        lifecycle_status = str(lifecycle.get("status") or "ACTIVE").strip().upper()
        retirement_at = _optional_utc(lifecycle.get("retirement_date"))
        request_at = _coerce_utc(at)
        should_migrate = lifecycle_status in _RUNTIME_MIGRATION_STATES or (
            lifecycle_status == "SCHEDULED_FOR_RETIREMENT"
            and retirement_at is not None
            and request_at >= retirement_at
        )
        target = str(
            lifecycle.get("alias_target") or lifecycle.get("replacement_model") or canonical_model
        ).strip()
        runtime_model = target if should_migrate else canonical_model
        pricing_model = str(record.get("pricing_model") or runtime_model).strip()
        raw_defaults = provider_data.get("defaults")
        defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
        return {
            "provider": provider,
            "requested_model": requested_model,
            "catalog_model": canonical_model,
            "runtime_model": runtime_model,
            "pricing_model": pricing_model,
            "lifecycle_status": lifecycle_status,
            "release_status": str(record.get("release_status") or "unknown"),
            "default_reasoning_mode": (
                str(record.get("default_reasoning_mode") or "").strip() or None
            ),
            "alias_redirected": bool(matched_alias or runtime_model != canonical_model),
            "replacement_model": str(lifecycle.get("replacement_model") or "") or None,
            "retirement_date": str(lifecycle.get("retirement_date") or "") or None,
            "migration_reason": str(lifecycle.get("migration_reason") or "") or None,
            "source_verified_at": str(
                record.get("source_verified_at") or defaults.get("source_verified_at") or ""
            )
            or None,
            "lifecycle_source_url": str(
                record.get("lifecycle_source_url") or defaults.get("lifecycle_source_url") or ""
            )
            or None,
        }

    @classmethod
    def get_pricing_snapshot(
        cls,
        model_type: str,
        model_name: str,
        *,
        at: datetime | str | None = None,
        prompt_tokens: int = 0,
        processing_mode: str = "standard",
        cache_write_ttl: str = "5m",
        catalog_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Return the effective immutable pricing rule for one request."""

        provider = str(model_type or "").strip().lower()
        identity = cls.resolve_model_identity(
            provider,
            model_name,
            at=at,
            catalog_path=catalog_path,
        )
        if identity is None:
            return None
        pricing_model = str(identity["pricing_model"])
        found = _find_model_record(provider, pricing_model, catalog_path=catalog_path)
        if found is None:
            return None
        record, provider_data, _ = found
        request_at = _coerce_utc(at)
        mode = str(processing_mode or "standard").strip().lower()
        rule = _select_rule(record, at=request_at, processing_mode=mode)
        if rule is None:
            return None

        rates: dict[str, Any] = dict(rule)
        tier = "standard"
        schedule = rule.get("schedule")
        if schedule is not None:
            from pricing.models import validate_rates
            from pricing.schedules import scheduled_tokens

            schedule_rates = validate_rates(
                {
                    "tokens": {
                        key: rule[key]
                        for key in (
                            "input",
                            "output",
                            "cached_input",
                            "cache_write",
                            "cache_write_1h",
                        )
                        if key in rule
                    },
                    "schedule": schedule,
                }
            )
            selected, tier = scheduled_tokens(schedule_rates["schedule"], request_at)
            rates.update(selected)
        long_context = rule.get("long_context")
        long_context_applied = False
        if isinstance(long_context, dict):
            threshold = int(long_context.get("threshold_tokens") or 0)
            if threshold > 0 and max(0, int(prompt_tokens)) >= threshold:
                rates.update(long_context)
                long_context_applied = True

        cache_write_key = "cache_write_1h" if cache_write_ttl == "1h" else "cache_write_5m"
        cache_write_rate = rates.get("cache_write")
        if cache_write_rate in (None, ""):
            cache_write_rate = rates.get(cache_write_key, rates.get("input", 0.0))

        raw_defaults = provider_data.get("defaults")
        defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
        catalog = _catalog(catalog_path)
        return {
            "provider": provider,
            "requested_model": identity["requested_model"],
            "pricing_model": str(record.get("name") or pricing_model),
            "runtime_model": identity["runtime_model"],
            "lifecycle_status": identity["lifecycle_status"],
            "alias_redirected": identity["alias_redirected"],
            "input": _number(rates.get("input")),
            "cached_input": _number(rates.get("cached_input"), default=_number(rates.get("input"))),
            "cache_write": _number(cache_write_rate, default=_number(rates.get("input"))),
            "output": _number(rates.get("output")),
            "currency": str(catalog.get("currency") or "USD"),
            "unit": str(catalog.get("pricing_unit") or "per_1m_tokens"),
            "pricing_rule_id": str(rule.get("id") or "unknown"),
            "pricing_version": (
                f"{catalog.get('catalog_version') or 'unknown'}:{rule.get('id') or 'unknown'}"
            ),
            "processing_mode": mode,
            "pricing_tier": tier,
            "schedule_calendar_year": schedule["holiday_calendar"]["year"] if schedule else None,
            "effective_from": str(rule.get("effective_from") or "") or None,
            "effective_until": str(rule.get("effective_until") or "") or None,
            "long_context_applied": long_context_applied,
            "long_context_threshold_tokens": (
                int(long_context.get("threshold_tokens") or 0)
                if isinstance(long_context, dict)
                else None
            ),
            "source_url": str(
                rule.get("source_url")
                or record.get("pricing_source_url")
                or defaults.get("pricing_source_url")
                or ""
            )
            or None,
            "source_verified_at": str(
                rule.get("source_verified_at")
                or record.get("source_verified_at")
                or defaults.get("source_verified_at")
                or ""
            )
            or None,
        }

    @classmethod
    def get_model_pricing(
        cls,
        model_type: str,
        model_name: str,
        *,
        at: datetime | str | None = None,
        catalog_path: str | Path | None = None,
    ) -> dict[str, float] | None:
        """Compatibility view containing only standard input/output rates."""

        snapshot = cls.get_pricing_snapshot(
            model_type,
            model_name,
            at=at,
            catalog_path=catalog_path,
        )
        if snapshot is None:
            return None
        return {"input": float(snapshot["input"]), "output": float(snapshot["output"])}

    @classmethod
    def conservative_fallback(
        cls,
        model_type: str,
        *,
        at: datetime | str | None = None,
        prompt_tokens: int = 0,
        processing_mode: str = "standard",
        catalog_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        """Return the highest effective rates, never a silent zero-price rule."""

        providers = _catalog(catalog_path).get("providers", {})
        requested_provider = str(model_type or "").strip().lower()
        provider_names = (
            [requested_provider] if requested_provider in providers else list(providers)
        )
        snapshots: list[dict[str, Any]] = []
        for provider in provider_names:
            block = providers.get(provider)
            if not isinstance(block, dict):
                continue
            for record in block.get("models", []):
                if not isinstance(record, dict) or not record.get("enabled", True):
                    continue
                name = str(record.get("name") or "").strip()
                snapshot = cls.get_pricing_snapshot(
                    provider,
                    name,
                    at=at,
                    prompt_tokens=prompt_tokens,
                    processing_mode=processing_mode,
                    catalog_path=catalog_path,
                )
                if snapshot is not None:
                    snapshots.append(snapshot)
        if not snapshots:
            return None
        fallback = {
            "provider": requested_provider or "unknown",
            "requested_model": None,
            "pricing_model": None,
            "runtime_model": None,
            "lifecycle_status": "UNKNOWN",
            "alias_redirected": False,
            "input": max(float(item["input"]) for item in snapshots),
            "cached_input": max(float(item["cached_input"]) for item in snapshots),
            "cache_write": max(float(item["cache_write"]) for item in snapshots),
            "output": max(float(item["output"]) for item in snapshots),
            "currency": "USD",
            "unit": "per_1m_tokens",
            "pricing_rule_id": f"conservative-fallback:{requested_provider or 'all'}",
            "pricing_version": f"{cls.catalogue_version(catalog_path=catalog_path)}:fallback",
            "processing_mode": processing_mode,
            "effective_from": None,
            "effective_until": None,
            "long_context_applied": False,
            "long_context_threshold_tokens": None,
            "source_url": None,
            "source_verified_at": None,
        }
        return fallback

    @classmethod
    def list_all_pricing(
        cls,
        model_type: str | None = None,
        *,
        at: datetime | str | None = None,
        catalog_path: str | Path | None = None,
    ) -> dict[str, dict[str, dict[str, float]]]:
        """List the current compatibility pricing view for catalogue models."""

        providers = _catalog(catalog_path).get("providers", {})
        if model_type:
            normalized = str(model_type).strip().lower()
            provider_names = [normalized]
        else:
            provider_names = list(providers)
        result: dict[str, dict[str, dict[str, float]]] = {}
        for provider in provider_names:
            result[provider] = {}
            block = providers.get(provider)
            if not isinstance(block, dict):
                continue
            for record in block.get("models", []):
                if not isinstance(record, dict):
                    continue
                name = str(record.get("name") or "").strip()
                pricing = cls.get_model_pricing(
                    provider,
                    name,
                    at=at,
                    catalog_path=catalog_path,
                )
                if pricing is not None:
                    result[provider][name] = pricing
        return result


class ModelPricing(RegistryPricing):
    """Compatibility facade: local DB rate cards after the staged rollout switch.

    The registry retains identities/lifecycle and supplies the explicit legacy
    mode. A database outage or missing rate never falls through to registry
    pricing in database mode.
    """

    @classmethod
    def get_pricing_snapshot(
        cls,
        model_type: str,
        model_name: str,
        *,
        at: datetime | str | None = None,
        prompt_tokens: int = 0,
        processing_mode: str = "standard",
        cache_write_ttl: str = "5m",
        catalog_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        from config.model_pricing import database_pricing_enabled

        custom_catalog = (
            catalog_path is not None
            and Path(catalog_path).resolve() != _DEFAULT_CATALOG_PATH.resolve()
        )
        if custom_catalog or not database_pricing_enabled():
            return RegistryPricing.get_pricing_snapshot(
                model_type,
                model_name,
                at=at,
                prompt_tokens=prompt_tokens,
                processing_mode=processing_mode,
                cache_write_ttl=cache_write_ttl,
                catalog_path=catalog_path,
            )
        from pricing.service import get_snapshot

        identity = cls.resolve_model_identity(model_type, model_name, at=at)
        canonical = str(identity["pricing_model"]) if identity else model_name
        return get_snapshot(
            model_type.lower(),
            canonical,
            at=at,
            prompt_tokens=prompt_tokens,
            mode=processing_mode,
            cache_write_ttl=cache_write_ttl,
        )

    @classmethod
    def conservative_fallback(cls, model_type: str, **kwargs: Any) -> dict[str, Any] | None:
        from config.model_pricing import database_pricing_enabled

        catalog_path = kwargs.get("catalog_path")
        custom_catalog = (
            catalog_path is not None
            and Path(catalog_path).resolve() != _DEFAULT_CATALOG_PATH.resolve()
        )
        if not custom_catalog and database_pricing_enabled():
            return None  # Only a model-specific approved persisted fallback is allowed.
        return RegistryPricing.conservative_fallback(model_type, **kwargs)
