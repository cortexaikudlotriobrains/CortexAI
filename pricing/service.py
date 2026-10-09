"""Local rate selection, bounded process cache, and startup readiness checks."""

from copy import deepcopy
from threading import Lock
from time import monotonic
from typing import Any

from config.model_pricing import load_pricing_policy
from db.pricing_repository import PricingRepository
from db.session import SessionLocal
from pricing.models import PricingUnavailableError, utc, validate_rates
from utils.logger import get_logger

logger = get_logger(__name__)
_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
_lock = Lock()
SOURCE_PRIORITY = {"LEGACY_CORTEX": 0, "LITELLM": 1, "CORTEX_MANUAL": 2}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def select_card(cards: list[dict[str, Any]], at: Any) -> dict[str, Any] | None:
    timestamp = utc(at)
    applicable = [
        card
        for card in cards
        if card["status"] == "approved"
        and utc(card["effective_from"]) <= timestamp
        and (card.get("effective_to") is None or timestamp < utc(card["effective_to"]))
    ]
    return max(
        applicable,
        key=lambda card: (SOURCE_PRIORITY[card["source"]], utc(card["effective_from"])),
        default=None,
    )


def snapshot(
    card: dict[str, Any], *, prompt_tokens: int = 0, cache_write_ttl: str = "5m", at: Any = None
) -> dict[str, Any]:
    if cache_write_ttl not in {"5m", "1h"}:
        raise ValueError("Unsupported cache TTL")
    rates = validate_rates(card["pricing_components"])
    tokens = dict(rates["tokens"])
    tier = "standard"
    if "schedule" in rates:
        from pricing.schedules import scheduled_tokens

        selected, tier = scheduled_tokens(rates["schedule"], at)
        tokens.update(selected)
    threshold = None
    for band in rates["bands"]:
        if prompt_tokens >= band["minimum_input_tokens"]:
            tokens.update(band["tokens"])
            threshold = band["minimum_input_tokens"]
    if cache_write_ttl == "1h":
        tokens["cache_write"] = tokens["cache_write_1h"]
    return {
        **tokens,
        "unit_rates": rates["units"],
        "provider": card["provider"],
        "pricing_model": card["canonical_model_id"],
        "rate_card_id": str(card["id"]),
        "pricing_rule_id": str(card["id"]),
        "pricing_version": f"rate-card:{card['id']}",
        "pricing_source": card["source"],
        "source_version": card["source_version"],
        "source_url": card["source_reference"],
        "source_verified_at": utc(card["last_verified_at"]).isoformat(),
        "effective_from": utc(card["effective_from"]).isoformat(),
        "effective_until": (
            utc(card["effective_to"]).isoformat() if card.get("effective_to") else None
        ),
        "currency": "USD",
        "unit": "per_1m_tokens",
        "processing_mode": card["processing_mode"],
        "pricing_tier": tier,
        "schedule_calendar_year": (
            rates["schedule"]["holiday_calendar"]["year"] if "schedule" in rates else None
        ),
        "long_context_applied": threshold is not None,
        "long_context_threshold_tokens": threshold,
        "cache_write_ttl": cache_write_ttl,
        "selection_reason": f"approved_{card['source'].lower()}_at_request_time",
    }


def get_snapshot(
    provider: str,
    model: str,
    *,
    at: Any = None,
    prompt_tokens: int = 0,
    mode: str = "standard",
    cache_write_ttl: str = "5m",
) -> dict[str, Any] | None:
    policy = load_pricing_policy()
    key = (provider, model, mode)
    now = monotonic()
    with _lock:
        cached = _cache.get(key)
    if cached and now - cached[0] < policy.cache_seconds:
        cards = cached[1]
    else:
        with SessionLocal() as db:
            cards = PricingRepository(db).list_cards(provider, model, mode)
        with _lock:
            if len(_cache) >= 512:
                _cache.clear()
            _cache[key] = (now, cards)
    card = select_card(cards, at)
    if card is None:
        logger.error(
            "pricing_unknown_model",
            extra={
                "extra_fields": {
                    "event": "pricing_unknown_model",
                    "provider": provider,
                    "model": model,
                }
            },
        )
        return None
    result = snapshot(card, prompt_tokens=prompt_tokens, cache_write_ttl=cache_write_ttl, at=at)
    result["pricing_stale"] = (
        utc() - utc(card["last_verified_at"])
    ).total_seconds() > policy.stale_hours * 3600
    if result["pricing_stale"]:
        logger.warning(
            "pricing_stale",
            extra={"extra_fields": {"event": "pricing_stale", "rate_card_id": str(card["id"])}},
        )
    return deepcopy(result)


def validate_readiness() -> None:
    from config.pricing import ModelPricing, _catalog
    from db.engine import get_engine
    from db.tables import DB_SCHEMA
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    expected = {
        "model_rate_cards": {
            "id",
            "pricing_components",
            "effective_from",
            "effective_to",
            "last_verified_at",
        },
        "pricing_catalog_observations": {"source_model_id", "missing_since"},
        "pricing_sync_runs": {"id", "status", "summary"},
        "llm_responses": {"rate_card_id", "usage_calculated_provider_cost_usd"},
    }
    for name, required in expected.items():
        if not inspector.has_table(name, schema=DB_SCHEMA) or not required <= {
            col["name"] for col in inspector.get_columns(name, schema=DB_SCHEMA)
        }:
            raise PricingUnavailableError(
                "Apply 20261007_add_versioned_model_rate_cards.sql before database pricing"
            )
    for provider, block in _catalog()["providers"].items():
        for model in block["models"]:
            if (
                model.get("enabled", True)
                and ModelPricing.get_pricing_snapshot(provider, model["name"]) is None
            ):
                raise PricingUnavailableError(
                    f"Seed approved pricing for enabled model {provider}:{model['name']}"
                )
        if get_snapshot(provider, "__web_search__") is None:
            raise PricingUnavailableError(f"Seed search pricing for {provider}")
    if get_snapshot("claude", "__managed_runtime__") is None:
        raise PricingUnavailableError("Seed Managed Agent runtime pricing")
