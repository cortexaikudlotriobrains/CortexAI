"""Rollout controls for provider-native live answer streaming."""

from __future__ import annotations

import os


DEFAULT_LIVE_STREAMING_PROVIDERS = frozenset({"openai", "claude", "gemini", "grok"})


def live_streaming_providers() -> frozenset[str]:
    raw = os.getenv("PROVIDER_LIVE_STREAMING_PROVIDERS")
    if raw is None:
        return DEFAULT_LIVE_STREAMING_PROVIDERS
    normalized = raw.strip().lower()
    if normalized in {"", "none", "off", "false", "0"}:
        return frozenset()
    aliases = {"anthropic": "claude", "google": "gemini", "xai": "grok"}
    return frozenset(
        aliases.get(item.strip().lower(), item.strip().lower())
        for item in raw.split(",")
        if item.strip()
    )


def provider_live_streaming_enabled(provider: str) -> bool:
    normalized = str(provider or "").strip().lower()
    normalized = {"anthropic": "claude", "google": "gemini", "xai": "grok"}.get(
        normalized,
        normalized,
    )
    return normalized in live_streaming_providers()
