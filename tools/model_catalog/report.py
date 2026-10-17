"""Compare the LiteLLM price catalog with the registry and propose model changes.

Prices come from LiteLLM only (decided 2026-10-14). The registry is used to know
which models the app has, not as a price source for comparisons.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml

from pricing.identity import PROVIDERS as SOURCE_PROVIDERS
from pricing.identity import ModelIdentityMap
from pricing.sources import LITELLM_URL, MAX_CATALOG_BYTES, LiteLLMPricingSource
from tools.model_catalog.checks import (
    CLASS_RANK,
    is_selectable,
    lifecycle_status,
    load_registry,
    model_index,
    run_checks,
)
from tools.model_catalog.apply import derive_attachments, derive_reasoning
from tools.model_catalog.ladder import load_ladder, plans_by_class
from tools.model_catalog.registry_text import ROOT

IGNORE_PATH = ROOT / "config" / "model_catalog_ignore.yaml"
TURN_INPUT, TURN_OUTPUT = 1000, 500
PROPOSALS_START = "<!-- model-catalog:proposals -->"
PROPOSALS_END = "<!-- /model-catalog:proposals -->"
_SKIP_WORDS = (
    "audio",
    "realtime",
    "tts",
    "transcribe",
    "image",
    "embedding",
    "search",
    "computer-use",
    "live",
    "robotics",
    "deep-research",
    "moderation",
    "latest",
    "-exp",
    "codex",
    "cyber",
    "rosalind",
    "beta",
    "multi-agent",
    "-gv",
    "customtools",
    "mythos",  # restricted access, not callable with a standard API key
)
_SNAPSHOT = re.compile(r"(-\d{4}-\d{2}-\d{2}|-\d{8}|-\d{4})($|-)")


@dataclass(frozen=True)
class SourceCatalog:
    version: str
    entries: dict[str, dict[str, Any]]  # parsed by pricing.sources
    raw: dict[str, dict[str, Any]]  # original JSON rows (capability flags, dates)


def fetch_catalog(path: str | None = None) -> SourceCatalog:
    if path:
        payload = Path(path).read_bytes()
    else:
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
            with client.stream("GET", LITELLM_URL) as response:
                response.raise_for_status()
                buffer = bytearray()
                for chunk in response.iter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_CATALOG_BYTES:
                        raise ValueError("Pricing catalog exceeds size limit")
                payload = bytes(buffer)
    parsed = LiteLLMPricingSource.parse(payload)
    raw = json.loads(payload)
    return SourceCatalog(parsed.version, parsed.entries, raw)


# ----------------------------------------------------------------------------- naming


def family(name: str) -> tuple[str, tuple[int, ...]]:
    """`claude-opus-5-5` -> ("claude-opus-#", (5, 5)); `gpt-6.1-sol` -> ("gpt-#-sol", (6, 1))."""

    stem: list[str] = []
    version: list[int] = []
    for part in name.split("-"):
        if re.fullmatch(r"\d+(\.\d+)*", part):
            version.extend(int(piece) for piece in part.split("."))
            if not stem or stem[-1] != "#":
                stem.append("#")
        else:
            stem.append(part)
    return "-".join(stem), tuple(version)


def display_name(provider: str, name: str) -> str:
    """`gemini-3.8-flash` -> "Gemini 3.8 Flash"; `claude-opus-5-5` -> "Claude Opus 5.5"."""

    brand = {
        "gpt": "GPT",
        "claude": "Claude",
        "gemini": "Gemini",
        "grok": "Grok",
        "deepseek": "DeepSeek",
    }
    words: list[str] = []
    for part in name.split("-"):
        if re.fullmatch(r"\d+(\.\d+)*", part) and words and re.fullmatch(r"[\d.]+", words[-1]):
            words[-1] = f"{words[-1]}.{part}"
        elif re.fullmatch(r"\d+(\.\d+)*", part):
            words.append(part)
        else:
            words.append(brand.get(part, part.capitalize()))
    if len(words) > 1 and words[0] == "GPT" and re.fullmatch(r"[\d.]+", words[1]):
        words[:2] = [f"GPT-{words[1]}"]
    return " ".join(words)


def load_ignored(path: Path = IGNORE_PATH) -> dict[str, str]:
    """IDs a person dismissed for good, with the reason (config/model_catalog_ignore.yaml)."""

    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(item["id"]): str(item.get("reason") or "") for item in data.get("ignored") or []}


def _version_text(name: str) -> str:
    return ".".join(str(part) for part in family(name)[1])


def _carry_description(description: str | None, predecessor: str, name: str) -> str | None:
    """Reuse the predecessor's description with its version swapped for the new one."""

    if not description:
        return None
    old, new = _version_text(predecessor), _version_text(name)
    if old and new and old != new:
        description = re.sub(rf"(?<![\d.]){re.escape(old)}(?![\d.])", new, description)
    return description


_FLAG_KEYS = (
    "supports_vision",
    "supports_pdf_input",
    "supports_reasoning",
    "thinking_always_on",
    "supports_none_reasoning_effort",
    "supports_minimal_reasoning_effort",
    "supports_xhigh_reasoning_effort",
    "supports_max_reasoning_effort",
    "supports_web_search",
    "deprecation_date",
)


def _max_output_tokens(
    raw: dict[str, Any], pred_model: dict[str, Any] | None, notes: list[str]
) -> int | None:
    """The new model's own output limit from LiteLLM, else the predecessor's.

    Some LiteLLM rows repeat the context window as `max_output_tokens` (xAI); a value
    equal to `max_input_tokens` is treated as unknown rather than as an output limit.
    """

    source = int(raw.get("max_output_tokens") or 0) or None
    inherited = int((pred_model or {}).get("max_output_tokens") or 0) or None
    if source and source == int(raw.get("max_input_tokens") or 0):
        if inherited:
            notes.append(
                f"LiteLLM's max_output_tokens ({source}) equals the context window; "
                f"kept the predecessor's {inherited}. Check the provider's limit."
            )
        return inherited
    if source and inherited and source != inherited:
        notes.append(f"Output limit {source} from LiteLLM; the predecessor allows {inherited}.")
    return source or inherited


def _short(provider: str, external: str) -> str:
    prefix = next((src for src, ours in SOURCE_PROVIDERS.items() if ours == provider), provider)
    return external.removeprefix(f"{prefix}/")


# ----------------------------------------------------------------------------- prices


def _rates(entry: dict[str, Any]) -> dict[str, Decimal] | None:
    if not entry.get("rates"):
        return None
    tokens = entry["rates"]["tokens"]
    return {key: Decimal(str(value)) for key, value in tokens.items()}


def _turn_cost(rates: dict[str, Decimal]) -> Decimal:
    return (rates["input"] * TURN_INPUT + rates["output"] * TURN_OUTPUT) / Decimal(1_000_000)


def _pct(new: Decimal, old: Decimal) -> str:
    if old == 0:
        return "n/a"
    change = (new / old - 1) * 100
    return f"{change:+.0f}%".replace("-", "\u2212")  # typographic minus in the issue


def _litellm_for(
    catalog: SourceCatalog, identities: ModelIdentityMap, provider: str, canonical: str
) -> dict[str, Any] | None:
    for external, entry in catalog.entries.items():
        if entry["provider"] == provider and identities.resolve(provider, external) == canonical:
            if entry.get("rates"):
                return entry
    return None


def _raw_for(
    catalog: SourceCatalog, identities: ModelIdentityMap, provider: str, canonical: str
) -> dict[str, Any]:
    for external, entry in catalog.entries.items():
        if entry["provider"] == provider and identities.resolve(provider, external) == canonical:
            if entry.get("rates"):
                return catalog.raw.get(external, {})
    return {}


# ----------------------------------------------------------------------------- build


def build_report(catalog: SourceCatalog, *, today: date | None = None) -> dict[str, Any]:
    today = today or datetime.now(timezone.utc).date()
    registry = load_registry()
    index = model_index(registry)
    identities = ModelIdentityMap()
    ladder = load_ladder()
    plans = plans_by_class()

    selectable = {key: model for key, model in index.items() if is_selectable(model)}
    by_family: dict[tuple[str, str], list[tuple[tuple[int, ...], str]]] = {}
    newest_major: dict[str, int] = {}
    for (provider, name), _model in selectable.items():
        stem, version = family(name)
        by_family.setdefault((provider, stem), []).append((version, name))
        if version:
            newest_major[provider] = max(newest_major.get(provider, 0), version[0])

    ignored = load_ignored()
    proposals: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    above_ladder: list[dict[str, Any]] = []
    dismissed: list[dict[str, str]] = []
    for external, entry in sorted(catalog.entries.items()):
        provider = entry["provider"]
        if identities.resolve(provider, external):
            continue
        name = _short(provider, external)
        raw = catalog.raw.get(external, {})
        if f"{provider}:{name}" in ignored:
            dismissed.append({"id": f"{provider}:{name}", "reason": ignored[f"{provider}:{name}"]})
            continue
        if (
            "/" in name
            or any(word in name for word in _SKIP_WORDS)
            or _SNAPSHOT.search(name)
            or (raw.get("deprecation_date") and str(raw["deprecation_date"]) <= today.isoformat())
        ):
            continue
        stem, version = family(name)
        siblings = sorted(by_family.get((provider, stem), []))
        older = [item for item in siblings if item[0] < version]
        if siblings and version <= siblings[-1][0]:
            continue  # not newer than the newest model of this family in the app
        if not siblings and (not version or version[0] < newest_major.get(provider, 0)):
            continue  # an old family the app never used
        if not entry.get("rates"):
            rejected.append({"id": f"{provider}:{name}", "error": entry.get("error")})
            continue
        rates = _rates(entry) or {}
        predecessor = older[-1][1] if older else None
        step = ladder.lowest_covering(rates["input"], rates["output"])
        if step is None:
            above_ladder.append(
                {"id": f"{provider}:{name}", "price": f"{rates['input']}/{rates['output']}"}
            )
            continue
        pred_entry = (
            _litellm_for(catalog, identities, provider, predecessor) if predecessor else None
        )
        pred_rates = _rates(pred_entry) if pred_entry else None
        pred_model = index.get((provider, predecessor)) if predecessor else None
        bands = entry["rates"].get("bands") or []
        context_limit = int(raw.get("max_input_tokens") or 0) or None
        pred_raw = _raw_for(catalog, identities, provider, predecessor) if predecessor else {}
        if (
            context_limit
            and pred_model
            and pred_model.get("context_limit")
            and pred_raw.get("max_input_tokens")
        ):
            # Keep the predecessor's convention: OpenAI's registry limit is the whole
            # window (input + output) while LiteLLM's max_input_tokens is input only.
            context_limit += int(pred_model["context_limit"]) - int(pred_raw["max_input_tokens"])
        flags = {key: raw[key] for key in _FLAG_KEYS if key in raw}
        notes: list[str] = []
        replaces = f"{provider}:{predecessor}" if predecessor else None
        if pred_model and CLASS_RANK.get(step.billing_class, 9) > CLASS_RANK.get(
            str(pred_model.get("billing_class")), -1
        ):
            # A replacement must never move users to a dearer plan class
            # (`scripts/models.py check` rejects it), so propose it alongside.
            notes.append(
                f"Added alongside {predecessor}, not replacing it: {step.billing_class} costs "
                f"more than {pred_model.get('billing_class')}, and a replacement may only move "
                "users to the same or a cheaper class. Write a description for it."
            )
            replaces = None
        max_output_tokens = _max_output_tokens(raw, pred_model, notes)
        attachments, attachment_notes = derive_attachments(
            pred_model, (registry["providers"].get(provider) or {}).get("defaults") or {}, flags
        )
        notes += attachment_notes
        proposals.append(
            {
                "id": f"{provider}:{name}",
                "litellm_id": external,
                "replaces": replaces,
                "based_on": f"{provider}:{predecessor}" if predecessor else None,
                "display_name": display_name(provider, name),
                "description": _carry_description(
                    (pred_model or {}).get("description") if replaces else None,
                    predecessor or "",
                    name,
                )
                or f"{display_name(provider, name)} model.",
                "tier": (pred_model or {}).get("tier") or "T2",
                "credits": step.describe(),
                "billing_class": step.billing_class,
                "credit_usage_label": step.label,
                "plans": list(plans.get(step.billing_class, [])),
                "price": {key: float(value) for key, value in rates.items()},
                "long_context": (
                    {
                        "threshold_tokens": int(bands[0]["minimum_input_tokens"]),
                        **{key: float(value) for key, value in bands[0]["tokens"].items()},
                    }
                    if bands
                    else None
                ),
                "change_per_turn": (
                    _pct(_turn_cost(rates), _turn_cost(pred_rates)) if pred_rates else None
                ),
                "context_limit": context_limit,
                "max_output_tokens": max_output_tokens,
                "promo": None,
                "flags": flags,
                # Explicit so a reviewer sees (and can edit) the controls the model gets.
                "reasoning": {
                    key: value
                    for key, value in derive_reasoning(pred_model, flags).items()
                    if value is not None
                }
                or None,
                "attachments": attachments,
                "source_url": raw.get("source") or None,
                **({"notes": notes} if notes else {}),
            }
        )

    deprecations = []
    for (provider, name), model in sorted(index.items()):
        source_row: dict[str, Any] | None = None
        for external, candidate in catalog.entries.items():
            if candidate["provider"] == provider and identities.resolve(provider, external) == name:
                source_row = catalog.raw.get(external, {})
                if source_row.get("deprecation_date"):
                    break
        if source_row and source_row.get("deprecation_date"):
            deprecations.append(
                {
                    "id": f"{provider}:{name}",
                    "date": str(source_row["deprecation_date"]),
                    "status": lifecycle_status(model),
                    "selectable": is_selectable(model),
                }
            )

    below_price = []
    for (provider, name), model in sorted(selectable.items()):
        current = _litellm_for(catalog, identities, provider, name)
        current_rates = _rates(current) if current else None
        if not current_rates:
            continue
        in_mult = Decimal(str(model["input_credit_multiplier"]))
        out_mult = Decimal(str(model["output_credit_multiplier"]))
        if in_mult < current_rates["input"] or out_mult < current_rates["output"]:
            below_price.append(
                {
                    "id": f"{provider}:{name}",
                    "credits": f"{in_mult}/{out_mult}",
                    "price": f"{current_rates['input']}/{current_rates['output']}",
                }
            )

    checks = run_checks(check_frontend=False)
    findings = {
        "proposals": proposals,
        "rejected": rejected,
        "above_ladder": above_ladder,
        "deprecations": deprecations,
        "credits_below_price": below_price,
        "errors": checks.errors,
        "warnings": checks.warnings,
    }
    # Same findings, same fingerprint: the workflow keeps last week's issue (and any
    # ticks on it) instead of opening a duplicate.
    fingerprint = hashlib.sha256(
        json.dumps(findings, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    return {
        "generated_on": today.isoformat(),
        "fingerprint": fingerprint,
        "litellm_version": catalog.version,
        "proposals": proposals,
        "rejected": rejected,
        "above_ladder": above_ladder,
        "dismissed": dismissed,
        "deprecations": deprecations,
        "credits_below_price": below_price,
        "warnings": checks.warnings,
        "errors": checks.errors,
    }


# ----------------------------------------------------------------------------- render


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"<!-- model-catalog:fingerprint={report.get('fingerprint', '')} -->",
        f"## Model catalog report, {report['generated_on']}",
        "",
        f"LiteLLM catalog `{report['litellm_version'][:12]}` compared with `config/model_registry.yaml`.",
        "Prices are LiteLLM's current prices. A typical turn is 1,000 input plus 500 output tokens.",
        "",
    ]
    proposals = report["proposals"]
    if proposals:
        lines += [
            "### New models",
            "",
            "Tick the models to add, edit the YAML block below if a field is wrong (for example",
            "`replaces`, `description`, `promo`, `attachments` or `max_output_tokens`), then approve the waiting **Apply model updates**",
            "job in Actions. Unticked models are reported again next week.",
            "",
        ]
        for item in proposals:
            change = item["change_per_turn"] or "new family"
            replaces = item["replaces"] or (
                f"nothing (added alongside {item['based_on']})"
                if item.get("based_on")
                else "nothing (new family)"
            )
            lines.append(
                f"- [ ] `{item['id']}` replaces {replaces}; {change} per turn; credits "
                f"{item['credits']} ({item['billing_class']}: {', '.join(item['plans'])})"
            )
            lines += [f"  - Check: {note}" for note in item.get("notes") or []]
        lines += [
            "",
            PROPOSALS_START,
            "```yaml",
            yaml.safe_dump(proposals, sort_keys=False, allow_unicode=True).rstrip(),
            "```",
            PROPOSALS_END,
            "",
        ]
    else:
        lines += ["No new models since the last registry change.", ""]
    if report.get("dismissed"):
        lines += [
            f"{len(report['dismissed'])} catalog entries are dismissed in "
            "`config/model_catalog_ignore.yaml` and not proposed.",
            "",
        ]
    if report["above_ladder"]:
        lines += ["### Priced above the top credit step (add a step first)", ""]
        lines += [f"- `{item['id']}`: {item['price']} per 1M" for item in report["above_ladder"]]
        lines.append("")
    if report["rejected"]:
        lines += ["### LiteLLM entries our parser rejects", ""]
        lines += [f"- `{item['id']}`: {item['error']}" for item in report["rejected"]]
        lines.append("")
    if report["deprecations"]:
        lines += ["### Provider deprecation dates for models in the registry", ""]
        lines += [
            f"- `{item['id']}`: {item['date']} (ours: {item['status']}"
            + (", in the picker)" if item["selectable"] else ", hidden)")
            for item in report["deprecations"]
        ]
        lines.append("")
    if report["credits_below_price"]:
        lines += ["### Credits below the LiteLLM price (the runtime floor is charging)", ""]
        lines += [
            f"- `{item['id']}`: credits {item['credits']}, price {item['price']}"
            for item in report["credits_below_price"]
        ]
        lines.append("")
    if report["errors"] or report["warnings"]:
        lines += ["### Registry checks", ""]
        lines += [f"- Error: {message}" for message in report["errors"]]
        lines += [f"- Warning: {message}" for message in report["warnings"]]
        lines.append("")
    lines += [
        "Code a new model may still need (a person writes it): provider client behaviour such as",
        "API surface, sampling or thinking parameters. Run the live check before enabling.",
    ]
    return "\n".join(lines) + "\n"


def parse_issue(body: str) -> tuple[list[dict[str, Any]], set[str]]:
    """Return (proposals, ticked ids) from a report issue body edited by a reviewer."""

    ticked = set(re.findall(r"^- \[[xX]\] `([^`]+)`", body, flags=re.MULTILINE))
    block = re.search(
        re.escape(PROPOSALS_START) + r"\s*```yaml\n(.*?)```\s*" + re.escape(PROPOSALS_END),
        body,
        flags=re.DOTALL,
    )
    proposals = yaml.safe_load(block[1]) if block else []
    return list(proposals or []), ticked
