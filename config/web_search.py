"""Runtime configuration for provider-native web search."""

from __future__ import annotations

import os
from dataclasses import dataclass


SUPPORTED_NATIVE_WEB_PROVIDERS = frozenset(
    {"openai", "claude", "gemini", "grok", "deepseek"}
)


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.getenv(name, "") or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _positive_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


@dataclass(frozen=True)
class NativeWebSearchConfig:
    rollout_mode: str
    providers: frozenset[str]
    compare_enabled: bool
    deepseek_agentic_enabled: bool
    max_operations: int
    max_display_sources: int
    deepseek_max_results: int

    @property
    def enabled(self) -> bool:
        return self.rollout_mode == "enabled"

    @property
    def shadow(self) -> bool:
        return self.rollout_mode == "shadow"

    def provider_enabled(self, provider: str, *, compare: bool = False) -> bool:
        normalized = str(provider or "").strip().lower()
        if not self.enabled or normalized not in self.providers:
            return False
        if compare and not self.compare_enabled:
            return False
        if normalized == "deepseek" and not self.deepseek_agentic_enabled:
            return False
        return True


def load_native_web_search_config() -> NativeWebSearchConfig:
    rollout_mode = str(os.getenv("NATIVE_WEB_SEARCH_MODE", "enabled") or "enabled")
    rollout_mode = rollout_mode.strip().lower()
    if rollout_mode not in {"off", "shadow", "enabled"}:
        rollout_mode = "off"

    raw_providers = str(
        os.getenv(
            "NATIVE_WEB_SEARCH_PROVIDERS",
            "openai,claude,gemini,grok,deepseek",
        )
        or ""
    )
    providers = frozenset(
        provider
        for provider in (part.strip().lower() for part in raw_providers.split(","))
        if provider in SUPPORTED_NATIVE_WEB_PROVIDERS
    )
    return NativeWebSearchConfig(
        rollout_mode=rollout_mode,
        providers=providers,
        compare_enabled=_env_bool("COMPARE_NATIVE_WEB_SEARCH_ENABLED", True),
        deepseek_agentic_enabled=_env_bool("DEEPSEEK_AGENTIC_SEARCH_ENABLED", True),
        max_operations=min(3, _positive_int("WEB_SEARCH_MAX_OPERATIONS", 3)),
        max_display_sources=min(8, _positive_int("WEB_SEARCH_MAX_DISPLAY_SOURCES", 8)),
        deepseek_max_results=min(5, _positive_int("DEEPSEEK_WEB_SEARCH_MAX_RESULTS", 5)),
    )
