#!/usr/bin/env python3
"""Model catalog maintenance.

  python scripts/models.py check                       # registry rules (CI runs this)
  python scripts/models.py report [--catalog FILE]     # LiteLLM vs registry; writes Markdown/YAML
  python scripts/models.py apply --proposals FILE [--select provider:model ...]
  python scripts/models.py apply --issue-body FILE     # ticked models from a report issue
  python scripts/models.py sync-frontend               # regenerate the React defaults file
  python scripts/models.py lock                        # append new IDs to config/known_model_ids.txt

See docs/runbooks/model-updates.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MODEL_PRICING_MODE", "legacy")


def _check(_args: argparse.Namespace) -> int:
    from tools.model_catalog.checks import run_checks

    result = run_checks()
    for message in result.warnings:
        print(f"warning: {message}")
    for message in result.errors:
        print(f"error: {message}")
    print(f"{len(result.errors)} error(s), {len(result.warnings)} warning(s)")
    return 0 if result.ok else 1


def _report(args: argparse.Namespace) -> int:
    import yaml

    from tools.model_catalog.report import build_report, fetch_catalog, render_markdown

    report = build_report(fetch_catalog(args.catalog))
    markdown = render_markdown(report)
    if args.markdown:
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    else:
        print(markdown)
    if args.proposals:
        Path(args.proposals).write_text(
            yaml.safe_dump(report["proposals"], sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    if args.summary_json:
        Path(args.summary_json).write_text(
            json.dumps(
                {
                    "fingerprint": report["fingerprint"],
                    "proposals": len(report["proposals"]),
                    "deprecations": len(report["deprecations"]),
                    "credits_below_price": len(report["credits_below_price"]),
                    "errors": len(report["errors"]),
                }
            ),
            encoding="utf-8",
        )
    return 0


def _apply(args: argparse.Namespace) -> int:
    import yaml

    from tools.model_catalog.apply import apply_proposals
    from tools.model_catalog.report import parse_issue

    selected: set[str] | None
    if args.issue_body:
        proposals, ticked = parse_issue(Path(args.issue_body).read_text(encoding="utf-8"))
        selected = ticked
    else:
        proposals = yaml.safe_load(Path(args.proposals).read_text(encoding="utf-8")) or []
        selected = set(args.select) if args.select else None
    if selected is not None and not selected:
        print("No models were ticked; nothing to apply.")
        return 0
    for line in apply_proposals(proposals, selected):
        print(line)
    return _check(args)


def _sync_frontend(_args: argparse.Namespace) -> int:
    from tools.model_catalog.checks import load_registry
    from tools.model_catalog.frontend import write_frontend_defaults

    changed = write_frontend_defaults(load_registry())
    print("Regenerated" if changed else "Already up to date")
    return 0


def _lock(_args: argparse.Namespace) -> int:
    from tools.model_catalog.checks import load_registry, write_lock

    added = write_lock(load_registry())
    print("Added: " + ", ".join(added) if added else "No new IDs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Validate registry rules").set_defaults(func=_check)
    report = sub.add_parser("report", help="Compare LiteLLM with the registry")
    report.add_argument("--catalog", help="Local LiteLLM JSON instead of downloading it")
    report.add_argument("--markdown", help="Write the issue body here")
    report.add_argument("--proposals", help="Write proposals YAML here")
    report.add_argument("--summary-json", help="Write counts for the workflow here")
    report.set_defaults(func=_report)
    apply = sub.add_parser("apply", help="Apply approved proposals")
    source = apply.add_mutually_exclusive_group(required=True)
    source.add_argument("--proposals", help="Proposals YAML")
    source.add_argument("--issue-body", help="Report issue body with ticked models")
    apply.add_argument("--select", nargs="*", help="Only these provider:model IDs")
    apply.set_defaults(func=_apply)
    sub.add_parser("sync-frontend", help="Regenerate React defaults").set_defaults(
        func=_sync_frontend
    )
    sub.add_parser("lock", help="Append new IDs to the ID lock").set_defaults(func=_lock)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
