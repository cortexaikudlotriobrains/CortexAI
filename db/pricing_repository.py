"""Transaction-neutral persistence for rate cards and catalog observations."""

import hashlib
import json
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from db.tables import get_table
from pricing.models import utc, validate_rates


def fingerprint(rates: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(rates, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PricingRepository:
    def __init__(self, db: Session):
        self.db = db
        self.cards = get_table("model_rate_cards")
        self.observations = get_table("pricing_catalog_observations")
        self.runs = get_table("pricing_sync_runs")

    def lock(self) -> None:
        # All activation/seed/override writers use this transaction-scoped lock.
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(text("SELECT pg_advisory_xact_lock(6100701)"))

    def list_cards(self, provider: str, model: str, mode: str = "standard") -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(self.cards).where(
                self.cards.c.provider == provider,
                self.cards.c.canonical_model_id == model,
                self.cards.c.processing_mode == mode,
                self.cards.c.status == "approved",
            )
        ).mappings()
        return [dict(row) for row in rows]

    def add(
        self,
        *,
        provider: str,
        model: str,
        source: str,
        rates: dict[str, Any],
        at: Any,
        reference: str,
        version: str,
        status: str = "approved",
        mode: str = "standard",
        until: Any = None,
        actor: str | None = None,
        reason: str | None = None,
        verified_at: Any = None,
    ) -> dict[str, Any]:
        rates = validate_rates(rates)
        if status == "approved":
            for existing in self.list_cards(provider, model, mode):
                if (
                    existing["source"] == source
                    and (until is None or utc(existing["effective_from"]) < utc(until))
                    and (
                        existing["effective_to"] is None or utc(existing["effective_to"]) > utc(at)
                    )
                ):
                    raise ValueError("Approved source rate-card intervals must not overlap")
        if source == "CORTEX_MANUAL" and (not actor or not reason):
            raise ValueError("Manual pricing requires actor and reason")
        value = {
            "id": str(uuid4()),
            "provider": provider,
            "canonical_model_id": model,
            "provider_model_id": model,
            "processing_mode": mode,
            "source": source,
            "source_reference": reference,
            "source_version": version,
            "currency": "USD",
            "fingerprint": fingerprint(rates),
            "pricing_components": rates,
            "input_price_per_million": rates["tokens"]["input"],
            "output_price_per_million": rates["tokens"]["output"],
            "effective_from": utc(at),
            "effective_to": utc(until) if until else None,
            "status": status,
            "created_at": utc(),
            "last_verified_at": utc(verified_at or at),
            "override_actor": actor,
            "override_reason": reason if source == "CORTEX_MANUAL" else None,
            "review_reason": reason if status == "candidate" else None,
        }
        self.db.execute(insert(self.cards).values(**value))
        return value

    def candidate_exists(
        self, provider: str, model: str, source: str, rates: dict[str, Any], mode: str
    ) -> bool:
        return (
            self.db.execute(
                select(self.cards.c.id).where(
                    self.cards.c.provider == provider,
                    self.cards.c.canonical_model_id == model,
                    self.cards.c.source == source,
                    self.cards.c.processing_mode == mode,
                    self.cards.c.status == "candidate",
                    self.cards.c.fingerprint == fingerprint(rates),
                )
            ).first()
            is not None
        )

    def close(self, card: dict[str, Any], at: Any) -> None:
        if utc(at) <= utc(card["effective_from"]):
            raise ValueError("Activation must follow the previous effective_from")
        self.db.execute(
            update(self.cards).where(self.cards.c.id == card["id"]).values(effective_to=utc(at))
        )

    def verified(self, card: dict[str, Any], at: Any) -> None:
        self.db.execute(
            update(self.cards).where(self.cards.c.id == card["id"]).values(last_verified_at=utc(at))
        )

    def observe(
        self,
        *,
        external: str,
        provider: str,
        model: str | None,
        version: str,
        at: Any,
        available: bool,
        metadata: dict[str, Any],
    ) -> None:
        values = {
            "provider": provider,
            "canonical_model_id": model,
            "source_version": version,
            "last_seen_at": utc(at),
            "missing_since": None,
            "pricing_available": available,
            "metadata": metadata,
        }
        where = (
            self.observations.c.source == "LITELLM",
            self.observations.c.source_model_id == external,
        )
        if self.db.execute(select(self.observations.c.source).where(*where)).first():
            self.db.execute(update(self.observations).where(*where).values(**values))
        else:
            self.db.execute(
                insert(self.observations).values(
                    source="LITELLM", source_model_id=external, **values
                )
            )

    def mark_missing(
        self, seen: set[str], at: Any, provider: str | None = None, model: str | None = None
    ) -> int:
        query = update(self.observations).where(
            self.observations.c.source == "LITELLM", self.observations.c.missing_since.is_(None)
        )
        if seen:
            query = query.where(self.observations.c.source_model_id.not_in(seen))
        if provider:
            query = query.where(self.observations.c.provider == provider)
        if model:
            query = query.where(self.observations.c.canonical_model_id == model)
        result = self.db.execute(query.values(missing_since=utc(at)))
        return cast(CursorResult[Any], result).rowcount

    def record_run(
        self, source: str, version: str | None, started: Any, status: str, summary: dict[str, Any]
    ) -> None:
        self.db.execute(
            insert(self.runs).values(
                id=str(uuid4()),
                source=source,
                source_version=version,
                started_at=utc(started),
                completed_at=utc(),
                status=status,
                summary=summary,
            )
        )
