"""Trusted operator CLI for provider pricing; does not modify model enablement."""

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from sqlalchemy import select

from db.pricing_repository import PricingRepository
from db.session import SessionLocal
from pricing.service import clear_cache
from pricing.identity import ModelIdentityMap
from pricing.models import utc
from config.model_pricing import load_pricing_policy
from pricing.sources import CortexManualPricingSource, LiteLLMPricingSource
from pricing.sync import (
    approve_candidate,
    manual_override,
    remove_override,
    run_sync,
    seed_legacy,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--provider", choices=["openai", "claude", "gemini", "grok", "deepseek"])
    parser.add_argument("--model")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--seed-legacy", action="store_true")
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--override", type=Path)
    action.add_argument("--remove-override", action="store_true")
    action.add_argument("--approve", metavar="CANDIDATE_ID")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    mutation = args.override or args.remove_override or args.approve
    if mutation and (not args.actor or not args.reason):
        parser.error("Override/approval actions require --actor and --reason")
    if args.remove_override and (not args.provider or not args.model):
        parser.error("--remove-override requires --provider and --model")
    if args.seed_legacy and (args.provider or args.model):
        parser.error("Legacy seeding is a complete catalog operation")
    if args.model and not args.provider:
        parser.error("--model requires --provider")
    if not (args.seed_legacy or args.inspect or mutation):
        print(
            json.dumps(
                run_sync(
                    LiteLLMPricingSource(),
                    dry_run=args.dry_run,
                    provider=args.provider,
                    model=args.model,
                ),
                indent=2,
                default=str,
            )
        )
        return
    with SessionLocal() as db:
        with db.begin():
            result: Any
            repo = PricingRepository(db)
            if args.inspect:
                query = select(repo.cards).order_by(
                    repo.cards.c.provider,
                    repo.cards.c.canonical_model_id,
                    repo.cards.c.effective_from,
                )
                if args.provider:
                    query = query.where(repo.cards.c.provider == args.provider)
                if args.model:
                    query = query.where(repo.cards.c.canonical_model_id == args.model)
                result = [dict(row) for row in db.execute(query).mappings()]
                identities = ModelIdentityMap()
                for row in result:
                    product_model = identities.models.get(
                        (row["provider"], row["canonical_model_id"]), {}
                    )
                    row["lifecycle_status"] = product_model.get("lifecycle", {}).get(
                        "status", "UNKNOWN"
                    )
                    row["cortex_enabled"] = product_model.get("enabled", False)
                    row["manually_overridden"] = row["source"] == "CORTEX_MANUAL"
                    row["pricing_stale"] = (
                        utc() - utc(row["last_verified_at"])
                    ).total_seconds() > load_pricing_policy().stale_hours * 3600
            elif args.seed_legacy:
                result = seed_legacy(db, dry_run=args.dry_run)
            elif args.override:
                data = CortexManualPricingSource.parse(args.override.read_text(encoding="utf-8"))
                result = (
                    data
                    if args.dry_run
                    else manual_override(db, data, actor=args.actor, reason=args.reason)
                )
            elif args.remove_override:
                result = {
                    "action": "remove_override",
                    "provider": args.provider,
                    "model": args.model,
                }
                if not args.dry_run:
                    remove_override(
                        db, args.provider, args.model, actor=args.actor, reason=args.reason
                    )
            else:
                result = {"action": "approve", "rate_card_id": args.approve}
                if not args.dry_run:
                    approve_candidate(db, args.approve, actor=args.actor, reason=args.reason)
    if not args.dry_run:
        clear_cache()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
