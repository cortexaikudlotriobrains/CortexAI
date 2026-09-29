"""Normalize model-specific reasoning controls for routing and UI clients."""

from __future__ import annotations

from typing import Any

UI_REASONING_LEVELS = ("low", "medium", "high", "max")
_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")


def _values(candidate: Any, name: str) -> list[str]:
    return [
        str(value or "").strip().lower()
        for value in (getattr(candidate, name, None) or [])
        if str(value or "").strip()
    ]


def model_reasoning_efforts(candidate: Any) -> list[str]:
    """Return the provider efforts that can be explicitly requested."""

    efforts = _values(candidate, "reasoning_efforts")
    if efforts:
        return efforts

    # xAI historically represented effort values in reasoning_modes. Keep the
    # compatibility path centralized instead of teaching UI clients about it.
    if str(getattr(candidate, "provider", "")).strip().lower() == "grok":
        return [mode for mode in _values(candidate, "reasoning_modes") if mode in _EFFORT_ORDER]
    return []


def model_supports_reasoning(candidate: Any) -> bool:
    modes = _values(candidate, "reasoning_modes")
    return bool(model_reasoning_efforts(candidate)) and any(mode != "none" for mode in modes)


def normalized_reasoning_levels(candidate: Any) -> list[str]:
    """Expose one stable product scale while preserving provider truth."""

    efforts = set(model_reasoning_efforts(candidate))
    if not efforts:
        return []

    levels: list[str] = []
    if efforts.intersection({"minimal", "low"}):
        levels.append("low")
    if "medium" in efforts:
        levels.append("medium")
    if "high" in efforts:
        levels.append("high")
    if efforts.intersection({"xhigh", "max"}):
        levels.append("max")
    return levels


def default_reasoning_level(candidate: Any) -> str | None:
    levels = normalized_reasoning_levels(candidate)
    return levels[0] if levels else None


def effective_effort_for_request(candidate: Any, requested_effort: str) -> str | None:
    """Map a public effort to an exact supported provider value.

    Manual requests never jump between Low, Medium, and High. The only alias is
    the product's Maximum level, which maps to provider ``max`` when available
    and otherwise to ``xhigh``.
    """

    requested = str(requested_effort or "").strip().lower()
    efforts = model_reasoning_efforts(candidate)
    if requested == "max":
        if "max" in efforts:
            return "max"
        if "xhigh" in efforts:
            return "xhigh"
        return None
    if requested == "low":
        if "low" in efforts:
            return "low"
        if "minimal" in efforts:
            return "minimal"
        return None
    return requested if requested in efforts else None


def candidate_supports_reasoning_request(
    candidate: Any,
    *,
    mode: str | None,
    effort: str | None,
) -> bool:
    requested_mode = str(mode or "auto").strip().lower()
    requested_effort = str(effort or "auto").strip().lower()

    if requested_mode == "off":
        return bool(getattr(candidate, "reasoning_disable_supported", True))
    if requested_mode == "on" and not model_supports_reasoning(candidate):
        return False
    if requested_effort == "auto":
        return True
    return effective_effort_for_request(candidate, requested_effort) is not None
