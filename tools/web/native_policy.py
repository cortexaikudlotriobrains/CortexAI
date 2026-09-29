"""Provider-neutral request policy for Ask and Compare web search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

WebSearchMode = Literal["off", "auto", "required"]

_NO_WEB_PATTERNS = (
    re.compile(r"\b(?:do\s+not|don't|dont|without|no)\s+(?:use\s+)?(?:the\s+)?(?:web|internet|online search|browsing)\b", re.I),
    re.compile(r"\b(?:offline|from your existing knowledge|without searching)\b", re.I),
)

_CURRENT_INFO_PATTERNS = (
    re.compile(r"\b(?:latest|current|today|tonight|right now|up[- ]to[- ]date|recent)\b", re.I),
    re.compile(r"\bthis\s+(?:week|month|year)\b", re.I),
    re.compile(r"\b(?:live|real[- ]time)\s+(?:price|score|status|availability|schedule|weather)\b", re.I),
    re.compile(r"\b(?:opening|business)\s+hours\b", re.I),
    re.compile(r"\b(?:law|laws|rule|rules|regulation|regulations)\s+(?:now|today|currently|in effect)\b", re.I),
    re.compile(r"\b(?:verify|check|confirm)\s+(?:online|on the web|with sources)\b", re.I),
)

_EXPLICIT_WEB_PATTERNS = (
    re.compile(
        r"\b(?:search|browse|check|look)\s+(?:the\s+)?(?:web|internet|online)\b",
        re.I,
    ),
    re.compile(r"\blook\s+(?:it|this|that)\s+up\b", re.I),
    re.compile(r"\b(?:web|internet|online)\s+search\b", re.I),
    re.compile(r"\bsearch\s+(?:for|using|with)\b", re.I),
)


@dataclass(frozen=True)
class WebSearchPolicy:
    requested_mode: WebSearchMode
    effective_mode: WebSearchMode
    reason: str
    max_operations: int = 3

    @property
    def enabled(self) -> bool:
        return self.effective_mode != "off"

    @property
    def required(self) -> bool:
        return self.effective_mode == "required"

    def to_provider_dict(self) -> dict[str, object]:
        return {
            "requested_mode": self.requested_mode,
            "mode": self.effective_mode,
            "reason": self.reason,
            "max_operations": self.max_operations,
        }


def normalize_requested_web_mode(
    web_mode: str | None,
    *,
    legacy_research_mode: bool | None = None,
) -> WebSearchMode:
    """Resolve new tri-state requests while preserving legacy Boolean clients."""
    raw = str(web_mode or "").strip().lower()
    if raw == "on":
        raw = "required"
    if raw in {"off", "auto", "required"}:
        return raw  # type: ignore[return-value]
    if legacy_research_mode is True:
        return "required"
    if legacy_research_mode is False:
        return "off"
    return "auto"


def resolve_web_search_policy(
    prompt: str,
    *,
    requested_mode: str | None,
    legacy_research_mode: bool | None = None,
    max_operations: int = 3,
) -> WebSearchPolicy:
    normalized = normalize_requested_web_mode(
        requested_mode,
        legacy_research_mode=legacy_research_mode,
    )
    bounded_operations = max(1, min(3, int(max_operations or 3)))
    text = str(prompt or "").strip()

    if normalized == "off":
        return WebSearchPolicy(normalized, "off", "request_disabled", bounded_operations)
    if any(pattern.search(text) for pattern in _NO_WEB_PATTERNS):
        return WebSearchPolicy(normalized, "off", "prompt_explicitly_disabled", bounded_operations)
    if normalized == "required":
        return WebSearchPolicy(normalized, "required", "request_required", bounded_operations)
    if any(pattern.search(text) for pattern in _CURRENT_INFO_PATTERNS):
        return WebSearchPolicy(normalized, "required", "current_information_required", bounded_operations)
    if any(pattern.search(text) for pattern in _EXPLICIT_WEB_PATTERNS):
        return WebSearchPolicy(normalized, "required", "prompt_explicitly_requested", bounded_operations)
    return WebSearchPolicy(normalized, "auto", "provider_decides", bounded_operations)


def orchestrator_research_mode(
    policy: WebSearchPolicy,
    *,
    requested_mode: str | None,
    legacy_research_mode: bool | None,
) -> str:
    """Translate native policy into the legacy ``off|auto|on`` contract."""
    if requested_mode is None and legacy_research_mode is not None:
        return "on" if legacy_research_mode else "off"
    if policy.required:
        return "on"
    return policy.effective_mode
