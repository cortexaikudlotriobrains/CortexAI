"""Deterministic Work request policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
import re

from orchestrator.prompt_analyzer import PromptAnalyzer

WORK_WEB_MODES = frozenset({"auto", "on", "off"})

_CURRENT_INFORMATION_PATTERNS = (
    r"\bopening hours?\b",
    r"\bbusiness hours?\b",
    r"\bticket prices?\b",
    r"\bcurrent prices?\b",
    r"\blive (?:price|prices|availability|status|schedule)\b",
    r"\b(?:flight|train|event|store|service) schedules?\b",
    r"\bweather(?: forecast)?\b",
    r"\bverify (?:online|on the web|with sources)\b",
    r"\bsource links?\b",
    r"\bcurrent (?:law|laws|rule|rules|regulation|regulations)\b",
)


@dataclass(frozen=True)
class WorkWebDecision:
    requested_mode: str
    effective_enabled: bool
    current_information: bool
    reason: str


def needs_current_information(instruction: str) -> bool:
    text = instruction or ""
    if PromptAnalyzer().analyze(text, None).needs_latest_info:
        return True
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _CURRENT_INFORMATION_PATTERNS)


def resolve_work_web_mode(instruction: str, requested_mode: str) -> WorkWebDecision:
    mode = str(requested_mode or "auto").strip().lower()
    if mode not in WORK_WEB_MODES:
        raise ValueError(f"Unsupported Work web mode: {requested_mode}")
    current_information = needs_current_information(instruction)
    if mode == "on":
        return WorkWebDecision(mode, True, current_information, "explicit_on")
    if mode == "off":
        return WorkWebDecision(mode, False, current_information, "explicit_off")
    return WorkWebDecision(
        mode,
        current_information,
        current_information,
        "current_information" if current_information else "not_required",
    )
