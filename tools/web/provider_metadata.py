"""Normalize native-provider search evidence into the Cortex response contract."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any


SEARCH_CREDITS_PER_OPERATION = {
    "openai": 10_000,
    "claude": 10_000,
    "gemini": 14_000,
    "grok": 5_000,
    "deepseek": 10_000,
    "tavily": 10_000,
}

SEARCH_COST_USD_PER_OPERATION = {
    "openai": 0.010,
    "claude": 0.010,
    "gemini": 0.014,
    "grok": 0.005,
    "deepseek": 0.010,
    "tavily": 0.010,
}

MAX_BILLABLE_SEARCH_OPERATIONS = 3


def normalize_web_sources(
    candidates: Iterable[Mapping[str, Any]] | None,
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates or ():
        url = str(candidate.get("url") or candidate.get("uri") or "").strip()
        if not url:
            continue
        key = url.casefold().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        title = str(candidate.get("title") or candidate.get("name") or url).strip() or url
        sources.append({"title": title, "url": url})
        if len(sources) >= max(1, int(limit)):
            break
    return sources


def field_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def sequence_value(value: Any) -> list[Any] | tuple[Any, ...]:
    """Return provider collection fields without treating loose mocks/objects as lists."""
    return value if isinstance(value, (list, tuple)) else ()


def insert_numbered_citations(
    text: str,
    annotations: Iterable[Mapping[str, Any]],
    sources: list[dict[str, str]],
) -> str:
    """Insert stable ``[n]`` markers using provider character offsets."""
    base = str(text or "")
    if not base or not sources:
        return base
    source_index = {
        str(source["url"]).casefold().rstrip("/"): index + 1
        for index, source in enumerate(sources)
    }
    insertions: dict[int, set[int]] = {}
    for annotation in annotations:
        url = str(annotation.get("url") or "").strip()
        raw_end_index = annotation.get("end_index")
        if raw_end_index is None:
            continue
        try:
            end_index = int(raw_end_index)
        except (TypeError, ValueError):
            continue
        number = source_index.get(url.casefold().rstrip("/"))
        if number is None or end_index <= 0 or end_index > len(base):
            continue
        insertions.setdefault(end_index, set()).add(number)
    if not insertions:
        return base
    output = base
    for end_index in sorted(insertions, reverse=True):
        marker = "".join(f"[{number}]" for number in sorted(insertions[end_index]))
        if output[max(0, end_index - len(marker)) : end_index] == marker:
            continue
        output = f"{output[:end_index]}{marker}{output[end_index:]}"
    return output


def extract_responses_web_search(
    response: Any,
    *,
    text: str,
) -> tuple[str, int, list[dict[str, str]]]:
    """Read Responses-style search calls, consulted sources, and annotations."""
    operations = 0
    raw_sources: list[dict[str, str]] = []
    annotations: list[dict[str, Any]] = []
    for item in sequence_value(field_value(response, "output", [])):
        item_type = str(field_value(item, "type", "") or "").lower()
        if item_type in {"web_search_call", "web_search"}:
            operations += 1
            action = field_value(item, "action", {})
            for source in sequence_value(field_value(action, "sources", [])):
                raw_sources.append(
                    {
                        "url": str(field_value(source, "url", "") or ""),
                        "title": str(field_value(source, "title", "") or ""),
                    }
                )
        for part in sequence_value(field_value(item, "content", [])):
            for annotation in sequence_value(field_value(part, "annotations", [])):
                citation = field_value(annotation, "url_citation", annotation)
                url = str(field_value(citation, "url", "") or "")
                if not url:
                    continue
                title = str(field_value(citation, "title", "") or "")
                raw_sources.append({"url": url, "title": title})
                annotations.append(
                    {
                        "url": url,
                        "end_index": field_value(citation, "end_index"),
                    }
                )
    for citation in sequence_value(field_value(response, "citations", [])):
        if isinstance(citation, str):
            raw_sources.append({"url": citation, "title": citation})
        else:
            raw_sources.append(
                {
                    "url": str(
                        field_value(citation, "url", "")
                        or field_value(citation, "uri", "")
                        or ""
                    ),
                    "title": str(field_value(citation, "title", "") or ""),
                }
            )
    usage = field_value(response, "usage")
    usage_details = field_value(usage, "server_side_tool_usage_details", {})
    try:
        billed_operations = max(
            0, int(field_value(usage_details, "web_search_calls", 0) or 0)
        )
    except (TypeError, ValueError):
        billed_operations = 0
    operations = billed_operations or operations
    sources = normalize_web_sources(raw_sources, limit=8)
    cited_text = insert_numbered_citations(text, annotations, sources)
    cited_text = re.sub(r"\[\[(\d+)\]\]\(https?://[^\s)]+\)", r"[\1]", cited_text)
    return cited_text, operations, sources


def build_web_search_metadata(
    *,
    provider: str,
    backend: str,
    requested_mode: str,
    effective_mode: str,
    operations: int,
    sources: Iterable[Mapping[str, Any]] | None = None,
    status: str | None = None,
    usage_estimated: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    normalized_provider = str(provider or backend or "").strip().lower()
    provider_reported_operations = max(0, int(operations or 0))
    normalized_operations = min(
        provider_reported_operations,
        MAX_BILLABLE_SEARCH_OPERATIONS,
    )
    normalized_sources = normalize_web_sources(sources, limit=8)
    rate = SEARCH_CREDITS_PER_OPERATION.get(normalized_provider, 0)
    cost_rate = SEARCH_COST_USD_PER_OPERATION.get(normalized_provider, 0.0)
    resolved_status = status or ("executed" if normalized_operations else "not_used")
    usage = {
        "provider": normalized_provider,
        "backend": str(backend or normalized_provider).strip().lower(),
        "requested_mode": str(requested_mode or "auto").strip().lower(),
        "effective_mode": str(effective_mode or "auto").strip().lower(),
        "status": resolved_status,
        "operations": normalized_operations,
        "provider_reported_operations": provider_reported_operations,
        "operation_limit_exceeded": provider_reported_operations > normalized_operations,
        "fixed_credits": normalized_operations * rate,
        "provider_cost_usd": provider_reported_operations * cost_rate,
        "usage_estimated": bool(usage_estimated),
        "error": str(error).strip() if error else None,
        "sources": normalized_sources,
    }
    return {
        "web_search": usage,
        "web_source_items": normalized_sources,
        # Compatibility fields consumed by history, streaming, and existing analytics.
        "research_used": normalized_operations > 0,
        "research_reused": False,
        "research_provider_credits_used": 0,
        "research_provider_credits_estimated": False,
        "research_error": error if error else (None if normalized_operations else "not_performed"),
        "sources": normalized_sources,
    }


def web_search_usage_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    usage = metadata.get("web_search")
    return dict(usage) if isinstance(usage, Mapping) else {}


def web_search_fixed_credits(metadata: Mapping[str, Any] | None) -> int:
    usage = web_search_usage_from_metadata(metadata)
    try:
        return max(0, int(usage.get("fixed_credits") or 0))
    except (TypeError, ValueError):
        return 0
