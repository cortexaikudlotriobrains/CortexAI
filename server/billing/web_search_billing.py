"""Reservation and settlement helpers for provider-native web search."""

from __future__ import annotations

from collections.abc import Iterable

from config.web_search import load_native_web_search_config
from server.billing.credit_calculator import web_search_credit_usage_from_metadata
from server.billing.enforcement_service import BillableWebSearchUsage
from tools.web.provider_metadata import SEARCH_CREDITS_PER_OPERATION


def web_search_reservation_credits(
    providers: Iterable[str],
    *,
    enabled: bool,
    per_target: bool,
) -> int:
    if not enabled:
        return 0
    config = load_native_web_search_config()
    rates = [
        SEARCH_CREDITS_PER_OPERATION.get(str(provider or "").strip().lower(), 0)
        for provider in providers
        if config.provider_enabled(str(provider or "").strip().lower())
    ]
    if not rates:
        return 0
    per_provider = [rate * config.max_operations for rate in rates]
    return sum(per_provider) if per_target else max(per_provider)


def web_search_reservation_override(
    providers: Iterable[str],
    *,
    native_enabled: bool,
    search_enabled: bool,
    per_target: bool,
) -> int | None:
    """Return ``None`` only when billing must retain the legacy Tavily default."""
    if not native_enabled:
        return None
    return web_search_reservation_credits(
        providers,
        enabled=search_enabled,
        per_target=per_target,
    )


def billable_web_search_usages(
    responses: Iterable[object],
) -> tuple[BillableWebSearchUsage, ...]:
    usages: list[BillableWebSearchUsage] = []
    for response in responses:
        metadata = getattr(response, "metadata", None)
        usage = web_search_credit_usage_from_metadata(metadata)
        if usage.operations <= 0 or usage.cortex_credits <= 0:
            continue
        usages.append(
            BillableWebSearchUsage(
                provider=usage.provider,
                backend=usage.backend,
                operations=usage.operations,
                fixed_credits=usage.cortex_credits,
                provider_cost_usd=usage.provider_cost_usd,
                usage_estimated=usage.estimated,
            )
        )
    return tuple(usages)
