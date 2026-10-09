"""Atomic version activation with conservative review and audited overrides."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from config.model_pricing import PricingPolicy, load_pricing_policy
from db.pricing_repository import PricingRepository, fingerprint
from db.session import SessionLocal
from pricing.identity import ModelIdentityMap
from pricing.models import amount, utc, validate_rates
from pricing.service import clear_cache, select_card
from pricing.sources import Catalog, PricingSource
from utils.logger import get_logger

logger = get_logger(__name__)


def event(name: str, **fields: Any) -> None:
    logger.info(name, extra={"extra_fields": {"event": name, **fields}})


def review_change(
    old: dict[str, Any] | None, new: dict[str, Any], policy: PricingPolicy
) -> str | None:
    if old is None:
        return "no_known_baseline"
    old = validate_rates(old)
    new = validate_rates(new)
    if old.get("schedule") != new.get("schedule"):
        return "billing_schedule_changed"
    if len(old["bands"]) != len(new["bands"]) or any(
        a["minimum_input_tokens"] != b["minimum_input_tokens"]
        for a, b in zip(old["bands"], new["bands"], strict=True)
    ):
        return "long_context_structure_changed"
    groups = [(old["tokens"], new["tokens"]), (old["units"], new["units"])]
    groups.extend(
        (a["tokens"], b["tokens"]) for a, b in zip(old["bands"], new["bands"], strict=True)
    )
    for before, after in groups:
        if set(before) != set(after):
            return "charge_dimensions_changed"
        for key, value in before.items():
            previous, incoming = amount(value), amount(after[key])
            if incoming == previous:
                continue
            if incoming == 0 or previous == 0:
                return "zero_rate_change"
            ratio = incoming / previous
            if ratio > policy.max_increase or ratio < policy.max_decrease:
                return "large_price_change"
    return None


def synchronize(
    db: Session,
    catalog: Catalog,
    *,
    dry_run: bool = False,
    at: Any = None,
    provider: str | None = None,
    model: str | None = None,
    identities: ModelIdentityMap | None = None,
    policy: PricingPolicy | None = None,
) -> dict[str, Any]:
    repo = PricingRepository(db)
    if not dry_run:
        repo.lock()
    timestamp = utc(at)
    identities = identities or ModelIdentityMap()
    policy = policy or load_pricing_policy()
    summary: dict[str, Any] = {
        "seen": 0,
        "changed": 0,
        "added": 0,
        "unchanged": 0,
        "discovered": 0,
        "invalid": 0,
        "candidate": 0,
        "missing": 0,
        "changes": [],
    }
    seen = set()
    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for external, entry in catalog.entries.items():
        entry_provider = entry["provider"]
        canonical = identities.resolve(entry_provider, external)
        if (provider and provider != entry_provider) or (model and model != canonical):
            continue
        seen.add(external)
        summary["seen"] += 1
        if not dry_run:
            repo.observe(
                external=external,
                provider=entry_provider,
                model=canonical,
                version=catalog.version,
                at=timestamp,
                available=bool(entry["rates"]),
                metadata={
                    "error": entry["error"],
                    "rates": entry["rates"],
                    "separate_charges": entry.get("separate_charges", {}),
                    "included_input_dimensions": entry.get("included_input_dimensions", []),
                    "cortex_enabled": bool(
                        identities.models.get((entry_provider, canonical or ""), {}).get(
                            "enabled", False
                        )
                    ),
                },
            )
        if not canonical:
            summary["discovered"] += 1
            continue
        groups.setdefault((entry_provider, canonical), []).append((external, entry))
    for (entry_provider, canonical), entries in groups.items():
        valid = [entry for _, entry in entries if entry["rates"]]
        if len(valid) != len(entries) or len({fingerprint(entry["rates"]) for entry in valid}) != 1:
            summary["invalid"] += 1
            summary["changes"].append(
                {
                    "provider": entry_provider,
                    "model": canonical,
                    "action": "rejected",
                    "reason": "invalid_or_ambiguous_source_entries",
                }
            )
            continue
        rates = valid[0]["rates"]
        cards = repo.list_cards(entry_provider, canonical)
        external_card = select_card(
            [card for card in cards if card["source"] == "LITELLM"], timestamp
        )
        baseline = external_card or select_card(
            [card for card in cards if card["source"] == "LEGACY_CORTEX"], timestamp
        )
        if baseline and "schedule" in baseline["pricing_components"] and "schedule" not in rates:
            summary["invalid"] += 1
            summary["changes"].append(
                {
                    "provider": entry_provider,
                    "model": canonical,
                    "action": "rejected",
                    "reason": "incomplete_billing_schedule",
                    "source_model_ids": [name for name, _ in entries],
                }
            )
            continue
        if external_card and fingerprint(rates) == external_card["fingerprint"]:
            summary["unchanged"] += 1
            if not dry_run:
                repo.verified(external_card, timestamp)
            continue
        reason = review_change(baseline["pricing_components"] if baseline else None, rates, policy)
        if any(entry.get("separate_charges") for entry in valid):
            reason = reason or "separate_service_charges_require_review"
        if not policy.auto_activate:
            reason = reason or "auto_activation_disabled"
        status = "candidate" if reason else "approved"
        summary["candidate" if reason else ("changed" if external_card else "added")] += 1
        summary["changes"].append(
            {
                "provider": entry_provider,
                "model": canonical,
                "action": status,
                "reason": reason,
                "source_model_ids": [name for name, _ in entries],
                "rates": rates,
            }
        )
        if reason:
            event(
                (
                    "pricing_large_change_detected"
                    if reason != "auto_activation_disabled"
                    else "pricing_review_required"
                ),
                provider=entry_provider,
                model=canonical,
                reason=reason,
            )
        if not dry_run:
            if reason and repo.candidate_exists(
                entry_provider, canonical, "LITELLM", rates, "standard"
            ):
                continue
            if not reason and external_card:
                repo.close(external_card, timestamp)
            repo.add(
                provider=entry_provider,
                model=canonical,
                source="LITELLM",
                rates=rates,
                at=timestamp,
                reference=f"{catalog.reference}#{entries[0][0]}",
                version=catalog.version,
                status=status,
                reason=reason,
            )
    if not dry_run:
        summary["missing"] = repo.mark_missing(seen, timestamp, provider, model)
        repo.record_run("LITELLM", catalog.version, timestamp, "completed", summary)
    return summary


def run_sync(
    source: PricingSource,
    *,
    dry_run: bool = False,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    started = utc()
    event("pricing_sync_started", dry_run=dry_run)
    try:
        catalog = source.fetch()  # Never hold a DB transaction across network I/O.
        with SessionLocal() as db:
            with db.begin():
                result = synchronize(db, catalog, dry_run=dry_run, provider=provider, model=model)
        if not dry_run:
            clear_cache()
        event(
            "pricing_sync_completed",
            **{key: value for key, value in result.items() if key != "changes"},
        )
        return result
    except Exception:
        event("pricing_sync_failure")
        if not dry_run:
            with SessionLocal() as db:
                with db.begin():
                    PricingRepository(db).record_run(
                        "LITELLM", None, started, "failed", {"error": "sync_failed"}
                    )
        raise


def seed_legacy(db: Session, *, dry_run: bool = False) -> dict[str, int]:
    from tools.web.provider_metadata import web_search_seed_versions
    from server.work.billing import MANAGED_RUNTIME_USD_PER_HOUR

    identities = ModelIdentityMap()
    repo = PricingRepository(db)
    if not dry_run:
        repo.lock()
    added, unchanged = 0, 0
    rows = []
    for provider, block in identities.catalog["providers"].items():
        defaults = block.get("defaults", {})
        for model in block["models"]:
            for rule in model.get("pricing_rules", []):
                tokens = {
                    "input": rule["input"],
                    "output": rule["output"],
                    "cached_input": rule.get("cached_input", rule["input"]),
                    "cache_write": rule.get(
                        "cache_write", rule.get("cache_write_5m", rule["input"])
                    ),
                    "cache_write_1h": rule.get(
                        "cache_write", rule.get("cache_write_1h", rule["input"])
                    ),
                }
                bands = []
                long = rule.get("long_context")
                if long:
                    merged = {**rule, **long}
                    long_tokens = {
                        "input": merged["input"],
                        "output": merged["output"],
                        "cached_input": merged.get("cached_input", merged["input"]),
                        "cache_write": merged.get(
                            "cache_write", merged.get("cache_write_5m", merged["input"])
                        ),
                        "cache_write_1h": merged.get(
                            "cache_write", merged.get("cache_write_1h", merged["input"])
                        ),
                    }
                    bands = [
                        {
                            "minimum_input_tokens": int(long["threshold_tokens"]),
                            "tokens": long_tokens,
                        }
                    ]
                rows.append(
                    {
                        "provider": provider,
                        "model": model["name"],
                        "rates": validate_rates(
                            {
                                "tokens": tokens,
                                "bands": bands,
                                **({"schedule": rule["schedule"]} if "schedule" in rule else {}),
                            }
                        ),
                        "mode": rule.get("processing_mode", "standard"),
                        "at": rule.get("effective_from") or "1970-01-01T00:00:00Z",
                        "until": rule.get("effective_until"),
                        "reference": rule.get("source_url")
                        or model.get("pricing_source_url")
                        or defaults.get("pricing_source_url")
                        or "legacy-registry",
                        "version": f"{identities.catalog['catalog_version']}:{rule['id']}",
                        "verified_at": rule.get("source_verified_at")
                        or model.get("source_verified_at")
                        or defaults.get("source_verified_at"),
                    }
                )
    for search in web_search_seed_versions():
        rows.append(
            {
                "provider": search["provider"],
                "model": "__web_search__",
                "rates": validate_rates(
                    {
                        "tokens": {"input": "0", "output": "0"},
                        "units": {"web_search_count": search["rate"]},
                    }
                ),
                "at": search["at"],
                "until": search["until"],
                "reference": "legacy-native-web-search",
                "version": search["version"],
            }
        )
    rows.append(
        {
            "provider": "claude",
            "model": "__managed_runtime__",
            "rates": validate_rates(
                {
                    "tokens": {"input": "0", "output": "0"},
                    "units": {"active_seconds": str(MANAGED_RUNTIME_USD_PER_HOUR / Decimal(3600))},
                }
            ),
            "at": "1970-01-01T00:00:00Z",
            "reference": "legacy-managed-runtime",
            "version": "2026-08-20",
        }
    )
    for row in rows:
        cards = repo.list_cards(row["provider"], row["model"], row.get("mode", "standard"))
        seeded = next(
            (
                card
                for card in cards
                if card["source"] == "LEGACY_CORTEX"
                and utc(card["effective_from"]) == utc(row["at"])
            ),
            None,
        )
        if seeded:
            if seeded["fingerprint"] != fingerprint(row["rates"]):
                raise ValueError(
                    "Legacy seed differs from retained history; use an audited new rate version"
                )
            if row.get("until") and (
                seeded["effective_to"] is None or utc(seeded["effective_to"]) != utc(row["until"])
            ):
                if seeded["effective_to"] is not None:
                    raise ValueError("Retained seed interval cannot be rewritten")
                if not dry_run:
                    repo.close(seeded, row["until"])
            unchanged += 1
        else:
            added += 1
            if not dry_run:
                repo.add(source="LEGACY_CORTEX", **row)
    return {"added": added, "unchanged": unchanged}


def manual_override(
    db: Session, data: dict[str, Any], *, actor: str, reason: str, at: Any = None
) -> dict[str, Any]:
    repo = PricingRepository(db)
    repo.lock()
    timestamp = utc(at)
    rates = validate_rates(data["rates"])
    previous = select_card(
        [
            card
            for card in repo.list_cards(data["provider"], data["model"], data["processing_mode"])
            if card["source"] == "CORTEX_MANUAL"
        ],
        timestamp,
    )
    if previous:
        repo.close(previous, timestamp)
    result = repo.add(
        provider=data["provider"],
        model=data["model"],
        mode=data["processing_mode"],
        source="CORTEX_MANUAL",
        rates=rates,
        at=timestamp,
        reference="operator-override",
        version=timestamp.isoformat(),
        actor=actor,
        reason=reason,
    )
    event("pricing_override_created", rate_card_id=result["id"], actor=actor)
    return result


def remove_override(db: Session, provider: str, model: str, *, actor: str, reason: str) -> None:
    if not actor.strip() or not reason.strip():
        raise ValueError("Removing an override requires actor and reason")
    repo = PricingRepository(db)
    repo.lock()
    timestamp = utc()
    previous = select_card(
        [card for card in repo.list_cards(provider, model) if card["source"] == "CORTEX_MANUAL"],
        timestamp,
    )
    if previous:
        repo.close(previous, timestamp)
    repo.record_run(
        "CORTEX_MANUAL",
        None,
        timestamp,
        "completed",
        {
            "action": "remove_override",
            "actor": actor,
            "reason": reason,
            "rate_card_id": str(previous["id"]) if previous else None,
        },
    )


def approve_candidate(db: Session, card_id: str, *, actor: str, reason: str) -> None:
    if not actor.strip() or not reason.strip():
        raise ValueError("Approval requires actor and reason")
    repo = PricingRepository(db)
    repo.lock()
    candidate = (
        db.execute(
            select(repo.cards).where(repo.cards.c.id == card_id, repo.cards.c.status == "candidate")
        )
        .mappings()
        .one()
    )
    timestamp = datetime.now(timezone.utc)
    validate_rates(candidate["pricing_components"])
    approved = repo.list_cards(
        candidate["provider"], candidate["canonical_model_id"], candidate["processing_mode"]
    )
    baseline = select_card(approved, timestamp)
    if (
        baseline
        and "schedule" in baseline["pricing_components"]
        and "schedule" not in candidate["pricing_components"]
    ):
        raise ValueError(
            "Candidate omits the active billing schedule; use a verified scheduled card"
        )
    previous = select_card(
        [
            card
            for card in repo.list_cards(
                candidate["provider"], candidate["canonical_model_id"], candidate["processing_mode"]
            )
            if card["source"] == candidate["source"]
        ],
        timestamp,
    )
    if previous:
        repo.close(previous, timestamp)
    db.execute(
        update(repo.cards)
        .where(repo.cards.c.id == card_id)
        .values(
            status="approved", effective_from=timestamp, override_actor=actor, review_reason=reason
        )
    )
    repo.record_run(
        candidate["source"],
        candidate["source_version"],
        timestamp,
        "completed",
        {"action": "approve_candidate", "actor": actor, "reason": reason, "rate_card_id": card_id},
    )
