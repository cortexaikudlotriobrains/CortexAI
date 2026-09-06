"""Manage Cortex-issued plan access using an operator-controlled DB connection."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from db.session import SessionLocal
from server.billing.grant_service import (
    inspect_subscription_grant,
    issue_subscription_grant,
    resolve_grant_user,
    revoke_subscription_grant,
)
from server.billing.schema_preflight import validate_billing_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("grant", "change", "revoke", "inspect"):
        command = commands.add_parser(name)
        identity = command.add_mutually_exclusive_group(required=True)
        identity.add_argument("--email")
        identity.add_argument("--user-id", type=UUID)
        if name != "inspect":
            command.add_argument("--actor", required=True, help="Accountable operator identity")
            command.add_argument("--reason", required=True)
        if name in {"grant", "change"}:
            command.add_argument("--plan", required=True, choices=("plus", "pro"))
            expiry = command.add_mutually_exclusive_group(required=True)
            expiry.add_argument("--days", type=int)
            expiry.add_argument(
                "--expires-at",
                type=datetime.fromisoformat,
                help="ISO-8601 timestamp with timezone, e.g. 2026-12-05T12:00:00Z",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    load_dotenv(ROOT / ".env", override=False)
    now = datetime.now(UTC)
    try:
        expiry = None
        if args.command in {"grant", "change"}:
            if args.days is not None and args.days <= 0:
                raise ValueError("Days must be positive")
            expiry = now + timedelta(days=args.days) if args.days is not None else args.expires_at
        validate_billing_schema()
        with SessionLocal() as db, db.begin():
            user_id = resolve_grant_user(db, email=args.email, user_id=args.user_id)
            result: dict | None
            if args.command in {"grant", "change"}:
                if not isinstance(expiry, datetime):
                    raise ValueError("Expiry is required")
                result = issue_subscription_grant(
                    db,
                    user_id,
                    plan_code=args.plan,
                    expires_at=expiry,
                    granted_by=args.actor,
                    reason=args.reason,
                    now=now,
                    change=args.command == "change",
                )
            elif args.command == "revoke":
                result = revoke_subscription_grant(
                    db, user_id, revoked_by=args.actor, reason=args.reason, now=now
                )
            else:
                result = inspect_subscription_grant(db, user_id)
        print(
            json.dumps(
                {"operation": args.command, "user_id": str(user_id), "result": result},
                default=str,
                indent=2,
            )
        )
        return 0
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))
    except Exception:
        # Database exception strings can contain connection details and PII.
        print(
            "Grant operation failed; transaction rolled back. Check database connectivity, schema and operator permissions.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
