"""Apply approved proposals: add models, hide what they replace, move defaults."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import yaml

from tools.model_catalog.checks import load_registry, model_index, write_lock
from tools.model_catalog.frontend import write_frontend_defaults
from tools.model_catalog.ladder import load_ladder
from tools.model_catalog.registry_text import (
    PROVIDERS_PATH,
    REGISTRY_PATH,
    hide_entry,
    insert_entry,
    remove_field,
    render_entry,
    replace_provider_default,
    replace_quoted_in_section,
    set_field,
)

_REASONING_FIELDS = (
    "reasoning_modes",
    "default_reasoning_mode",
    "reasoning_efforts",
    "reasoning_disable_supported",
    "reasoning_counts_against_output",
)
_ATTACHMENT_FIELDS = (
    "supports_image_input",
    "supported_attachment_mime_types",
    "max_attachment_bytes",
    "max_attachments_per_request",
)


_IMAGE_PREFIX = "image/"


def derive_attachments(
    pred: dict[str, Any] | None, defaults: dict[str, Any], flags: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Attachment settings for a successor: the predecessor's, adjusted by LiteLLM flags.

    Each field comes from the predecessor when it sets one, else from the provider
    defaults (what the predecessor effectively uses). LiteLLM flags only remove
    types the new model cannot read; limits and new types are never invented, so a
    gap is returned as a review note instead.
    """

    settings: dict[str, Any] = {}
    for field in _ATTACHMENT_FIELDS:
        value = (pred or {}).get(field)
        settings[field] = defaults.get(field) if value is None else value
    types = [str(item) for item in settings.get("supported_attachment_mime_types") or []]
    notes: list[str] = []
    if flags.get("supports_vision") is False:
        types = [item for item in types if not item.startswith(_IMAGE_PREFIX)]
    if flags.get("supports_pdf_input") is False:
        types = [item for item in types if item != "application/pdf"]
    if flags.get("supports_vision") and not any(item.startswith(_IMAGE_PREFIX) for item in types):
        notes.append(
            "LiteLLM marks the model vision-capable, but the predecessor accepts no images; "
            "add image types and limits under `attachments` once the provider client supports them."
        )
    settings["supported_attachment_mime_types"] = types
    settings["supports_image_input"] = any(item.startswith(_IMAGE_PREFIX) for item in types)
    if not types:
        settings["max_attachments_per_request"] = 0
    elif not settings.get("max_attachments_per_request") or not settings.get(
        "max_attachment_bytes"
    ):
        notes.append("Attachment limits are missing; set them under `attachments`.")
    return settings, notes


_ID = re.compile(r"^[a-z][a-z0-9]*:[a-z0-9][a-z0-9.\-]*$")
_RATE_KEYS = {"input", "output", "cached_input", "cache_write", "cache_write_1h", "reasoning"}
_PROPOSAL_KEYS = {
    "id",
    "litellm_id",
    "replaces",
    "based_on",
    "also_replaces",
    "display_name",
    "description",
    "tier",
    "credits",
    "billing_class",
    "credit_usage_label",
    "plans",
    "price",
    "long_context",
    "change_per_turn",
    "context_limit",
    "max_output_tokens",
    "promo",
    "flags",
    "source_url",
    "effective_from",
    "reasoning",
    "tags",
    "aliases",
    "attachments",
    "notes",
}


def validate_proposal(proposal: dict[str, Any]) -> None:
    """Reject edits the registry renderer must never see.

    Proposals come from an issue body that people with write access can edit, so
    only known keys, IDs and rate names reach config/model_registry.yaml.
    """

    if not isinstance(proposal, dict):
        raise ValueError("Each proposal must be a mapping")
    unknown = set(proposal) - _PROPOSAL_KEYS
    if unknown:
        raise ValueError(f"{proposal.get('id')}: unknown proposal fields {sorted(unknown)}")
    ids = [
        proposal.get("id"),
        proposal.get("replaces"),
        proposal.get("based_on"),
        *(proposal.get("also_replaces") or []),
    ]
    for key in [value for value in ids if value]:
        if not _ID.match(str(key)):
            raise ValueError(f"Invalid model key {key!r}")
    for alias in proposal.get("aliases") or []:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", str(alias)):
            raise ValueError(f"Invalid alias {alias!r}")
    if proposal.get("tier") not in {"T0", "T1", "T2", "T3"}:
        raise ValueError(f"{proposal['id']}: tier must be T0-T3")
    promo = proposal.get("promo") or {}
    for label, rates in (
        ("price", proposal.get("price") or {}),
        ("long_context", proposal.get("long_context") or {}),
        ("promo.regular", promo.get("regular") or {}),
    ):
        bad = set(rates) - _RATE_KEYS - {"threshold_tokens"}
        if bad:
            raise ValueError(f"{proposal['id']}: unknown {label} rates {sorted(bad)}")
        for key, value in rates.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{proposal['id']}: {label}.{key} must be a nonnegative number")
    if set(proposal.get("reasoning") or {}) - set(_REASONING_FIELDS):
        raise ValueError(f"{proposal['id']}: unknown reasoning fields")
    attachments = proposal.get("attachments") or {}
    if set(attachments) - set(_ATTACHMENT_FIELDS):
        raise ValueError(f"{proposal['id']}: unknown attachment fields")
    for mime in attachments.get("supported_attachment_mime_types") or []:
        if not re.fullmatch(r"[a-z]+/[a-z0-9.+\-]+", str(mime)):
            raise ValueError(f"{proposal['id']}: invalid attachment type {mime!r}")


def _split(key: str) -> tuple[str, str]:
    provider, _, name = str(key).partition(":")
    if not provider or not name:
        raise ValueError(f"Model keys look like provider:model, got {key!r}")
    return provider, name


def _token_fields(provider: str, rates: dict[str, Any]) -> dict[str, Any]:
    """Map LiteLLM token rates to registry rule keys for this provider."""

    out: dict[str, Any] = {"input": rates["input"]}
    if rates.get("cached_input") is not None:
        out["cached_input"] = rates["cached_input"]
    if provider == "claude":
        if rates.get("cache_write") is not None:
            out["cache_write_5m"] = rates["cache_write"]
        if rates.get("cache_write_1h") is not None:
            out["cache_write_1h"] = rates["cache_write_1h"]
    elif rates.get("cache_write") is not None:
        out["cache_write"] = rates["cache_write"]
    out["output"] = rates["output"]
    return out


def _rule(
    provider: str,
    name: str,
    *,
    starts: str | None,
    ends: str | None,
    rates: dict[str, Any],
    long_context: dict[str, Any] | None,
    source_url: str | None,
    verified: str,
) -> dict[str, Any]:
    day = (starts or verified)[:10].replace("-", "")
    stem = name if name.startswith(f"{provider}-") else f"{provider}-{name}"
    rule: dict[str, Any] = {
        "id": f"{stem}-standard-{day}",
        "processing_mode": "standard",
        "effective_from": starts,
        "effective_until": ends,
        "source_url": source_url,
        "source_verified_at": verified,
    }
    rule.update(_token_fields(provider, rates))
    if long_context:
        band = {key: value for key, value in long_context.items() if key != "threshold_tokens"}
        rule["long_context"] = {
            "threshold_tokens": int(long_context["threshold_tokens"]),
            "applies_to_entire_request": True,
            **_token_fields(provider, band),
        }
    return rule


def derive_reasoning(pred: dict[str, Any] | None, flags: dict[str, Any]) -> dict[str, Any]:
    """The predecessor's reasoning settings, adjusted by LiteLLM's capability flags."""

    reasoning = {field: (pred or {}).get(field) for field in _REASONING_FIELDS}
    modes = list(reasoning.get("reasoning_modes") or [])
    efforts = list(reasoning.get("reasoning_efforts") or [])
    disable = reasoning.get("reasoning_disable_supported")
    if flags.get("thinking_always_on") or flags.get("supports_none_reasoning_effort") is False:
        modes = [mode for mode in modes if mode != "none"]
        efforts = [effort for effort in efforts if effort != "none"]
        disable = False
    if flags.get("supports_minimal_reasoning_effort") is False:
        efforts = [effort for effort in efforts if effort != "minimal"]
    for flag, effort in (
        ("supports_xhigh_reasoning_effort", "xhigh"),
        ("supports_max_reasoning_effort", "max"),
    ):
        if efforts and flags.get(flag) and effort not in efforts:
            efforts.append(effort)
    default = reasoning.get("default_reasoning_mode")
    if modes and default not in modes:
        default = modes[0]
    return {
        "reasoning_modes": modes or None,
        "default_reasoning_mode": default,
        "reasoning_efforts": efforts or None,
        "reasoning_disable_supported": disable,
        "reasoning_counts_against_output": reasoning.get("reasoning_counts_against_output"),
    }


def build_entry(
    proposal: dict[str, Any],
    pred: dict[str, Any] | None,
    today: date,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    provider, name = _split(proposal["id"])
    ladder = load_ladder()
    credits_in, credits_out = (Decimal(part) for part in str(proposal["credits"]).split("/"))
    step = ladder.step_for(credits_in, credits_out)
    if step is None:
        raise ValueError(f"{proposal['id']}: credits {proposal['credits']} are not a ladder step")
    verified = today.isoformat()
    # Without an explicit start the first rule applies from the beginning, like most
    # registry rules: a model cannot be called before it exists, and tests or
    # history lookups at earlier timestamps still find a price.
    starts = str(proposal["effective_from"]) if proposal.get("effective_from") else None
    promo = proposal.get("promo") or None
    rules = []
    if promo:
        rules.append(
            _rule(
                provider,
                name,
                starts=starts,
                ends=str(promo["until"]),
                rates=proposal["price"],
                long_context=proposal.get("long_context"),
                source_url=proposal.get("source_url"),
                verified=verified,
            )
        )
        rules.append(
            _rule(
                provider,
                name,
                starts=str(promo["until"]),
                ends=None,
                rates=promo["regular"],
                long_context=promo.get("regular_long_context"),
                source_url=proposal.get("source_url"),
                verified=verified,
            )
        )
    else:
        rules.append(
            _rule(
                provider,
                name,
                starts=starts,
                ends=None,
                rates=proposal["price"],
                long_context=proposal.get("long_context"),
                source_url=proposal.get("source_url"),
                verified=verified,
            )
        )
    reasoning = proposal.get("reasoning") or derive_reasoning(pred, proposal.get("flags") or {})
    release_status = "preview" if "preview" in name else "generally_available"
    inherited_only = {"preview", "retired", "compatibility"} - (
        {"preview"} if release_status == "preview" else set()
    )
    tags = [
        tag
        for tag in (proposal.get("tags") or (pred or {}).get("tags") or [])
        if proposal.get("tags") or tag not in inherited_only
    ]
    if (proposal.get("flags") or {}).get("supports_reasoning") and "reasoning" not in tags:
        tags.append("reasoning")
    if int(proposal.get("context_limit") or 0) >= 500_000 and "long_context" not in tags:
        tags.append("long_context")
    defaults = defaults or {}
    derived, _notes = derive_attachments(pred, defaults, proposal.get("flags") or {})
    attachments = {**derived, **(proposal.get("attachments") or {})}
    # Write the fields only when they differ from the provider defaults (house style).
    if all(attachments.get(field) == defaults.get(field) for field in _ATTACHMENT_FIELDS):
        attachments = {}
    entry: dict[str, Any] = {
        "name": name,
        "aliases": proposal.get("aliases") or None,
        "pricing_review": (
            {
                "requires_expiry": True,
                "effective_until": str(promo["until"]),
                "source_url": proposal.get("source_url"),
                "verified_at": verified,
                "note": promo.get("note")
                or "Launch price; the regular price applies from effective_until.",
            }
            if promo
            else None
        ),
        "display_name": proposal["display_name"],
        "description": proposal["description"],
        "tier": proposal["tier"],
        "billing_class": step.billing_class,
        "input_credit_multiplier": credits_in,
        "output_credit_multiplier": credits_out,
        "credit_usage_label": step.label,
        "credit_pricing_version": verified,
        "context_limit": int(proposal["context_limit"]),
        "max_output_tokens": int(proposal["max_output_tokens"]),
        **{field: reasoning.get(field) for field in _REASONING_FIELDS},
        "tags": tags,
        **{field: attachments.get(field) for field in _ATTACHMENT_FIELDS},
        "enabled": True,
        "selectable": True,
        "release_status": release_status,
        "source_verified_at": verified,
        "lifecycle": {"status": "ACTIVE"},
        "pricing_rules": rules,
    }
    return entry


def apply_proposals(
    proposals: list[dict[str, Any]],
    selected: set[str] | None = None,
    *,
    today: date | None = None,
) -> list[str]:
    """Edit the registry, providers.yaml, the ID lock and the frontend defaults."""

    today = today or datetime.now(timezone.utc).date()
    registry_text = REGISTRY_PATH.read_text(encoding="utf-8")
    providers_text = PROVIDERS_PATH.read_text(encoding="utf-8")
    log: list[str] = []
    chosen = [p for p in proposals if selected is None or p.get("id") in selected]
    missing = (selected or set()) - {p.get("id") for p in proposals}
    if missing:
        raise ValueError(f"Ticked models have no proposal in the YAML block: {sorted(missing)}")
    for proposal in chosen:
        validate_proposal(proposal)
    for proposal in chosen:
        provider, name = _split(proposal["id"])
        current = yaml.safe_load(registry_text)
        index = model_index(current)
        defaults = (current["providers"].get(provider) or {}).get("defaults") or {}
        if (provider, name) in index:
            raise ValueError(f"{proposal['id']} is already in the registry")
        replaces = proposal.get("replaces")
        if replaces and index.get(_split(replaces)) is None:
            raise ValueError(f"{proposal['id']}: replaced model {replaces} is not in the registry")
        # Capabilities (reasoning, tags, attachments) come from `based_on`, which is
        # independent of hiding: a dearer newcomer is added alongside its family but
        # still inherits from it. Without `based_on`, the replaced model is the source.
        based_on = proposal.get("based_on") or replaces
        pred = index.get(_split(based_on)) if based_on else None
        if based_on and pred is None:
            raise ValueError(f"{proposal['id']}: based_on model {based_on} is not in the registry")
        if based_on and _split(based_on)[0] != provider:
            raise ValueError(f"{proposal['id']}: based_on must be a {provider} model")
        entry = build_entry(proposal, pred, today, defaults)
        before = _split(based_on)[1] if based_on else None
        registry_text = insert_entry(registry_text, provider, render_entry(entry), before=before)
        log.append(
            f"Added {proposal['id']} ({entry['billing_class']}, credits {proposal['credits']})"
        )

        hidden = [key for key in [replaces, *(proposal.get("also_replaces") or [])] if key]
        for old_key in hidden:
            old_provider, old_name = _split(old_key)
            if old_provider != provider:
                raise ValueError(f"{proposal['id']} cannot replace a {old_provider} model")
            registry_text = hide_entry(
                registry_text,
                old_provider,
                old_name,
                replacement=name,
                reason=f"Replaced in the picker by {proposal['display_name']} on {today.isoformat()}.",
            )
            for alias in proposal.get("aliases") or []:
                old_aliases = list((index.get((old_provider, old_name)) or {}).get("aliases") or [])
                if alias in old_aliases:
                    old_aliases.remove(alias)
                    registry_text = (
                        set_field(registry_text, old_provider, old_name, "aliases", old_aliases)
                        if old_aliases
                        else remove_field(registry_text, old_provider, old_name, "aliases")
                    )
            new_key = f"{provider}:{name}"
            for section in ("ui_defaults", "routing_defaults"):
                registry_text, moved = replace_quoted_in_section(
                    registry_text, section, old_key, new_key
                )
                if moved:
                    log.append(f"Moved {section} {old_key} -> {new_key}")
            providers_text, moved = replace_provider_default(
                providers_text, provider, old_name, name
            )
            if moved:
                log.append(f"Moved providers.yaml {provider} default {old_name} -> {name}")
            log.append(f"Hid {old_key}; replacement {name}")

    REGISTRY_PATH.write_text(registry_text, encoding="utf-8")
    PROVIDERS_PATH.write_text(providers_text, encoding="utf-8")
    registry = load_registry()
    added = write_lock(registry)
    if added:
        log.append("Locked IDs: " + ", ".join(added))
    if write_frontend_defaults(registry):
        log.append("Regenerated frontend-react/src/config/generated/registryDefaults.ts")
    return log
