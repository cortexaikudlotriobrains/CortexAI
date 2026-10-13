"""Read-only preflight: prove the full model catalog works with database pricing.

Run this on the API host before setting ``MODEL_PRICING_MODE=database`` for the
service. It switches database pricing on for this process only, then:

1. runs the same pricing readiness check the API runs at startup;
2. builds the complete ``ModelRegistry`` (what /v1/model-options, /v1/providers,
   /v1/history, Ask and Compare use) from approved database rate cards;
3. checks every provider and every selectable model is still present;
4. checks that rates missing from a card stay missing (never $0) in the catalog;
5. checks operator-supplied expected rates, card IDs and long-context starts;
6. warns about approved cards that end without an approved successor.

It never edits ``.env``, never writes to the database, never restarts the API,
never calls a model provider and never charges anyone. Logs go to stdout only.
Exit status 0 means no check failed (warnings allowed); 1 means do not switch.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import sys
import traceback
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EXPECTED_PROVIDERS = ("openai", "claude", "gemini", "grok", "deepseek")
OPTIONAL_RATES = (
    ("cached_input", "cached_input_cost_per_1m"),
    ("cache_write", "cache_write_cost_per_1m"),
)


@dataclass(frozen=True)
class Expectation:
    provider: str
    model: str
    input_rate: Decimal
    output_rate: Decimal
    rate_card_id: str | None


@dataclass
class Report:
    out: Callable[[str], None]
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.out(f"PASS {message}")

    def fail(self, message: str) -> None:
        self.failures.append(message)
        self.out(f"FAIL {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.out(f"WARN {message}")

    def info(self, message: str) -> None:
        self.out(f"     {message}")


def _model_ref(value: str) -> tuple[str, str]:
    provider, sep, model = value.partition(":")
    if not sep or not provider.strip() or not model.strip():
        raise argparse.ArgumentTypeError(f"expected PROVIDER:MODEL, got {value!r}")
    return provider.strip().lower(), model.strip()


def _expectation(value: str) -> Expectation:
    target, sep, spec = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(
            f"expected PROVIDER:MODEL=INPUT/OUTPUT[@RATE_CARD_ID], got {value!r}"
        )
    provider, model = _model_ref(target)
    rates, _, card_id = spec.partition("@")
    input_text, slash, output_text = rates.partition("/")
    try:
        if not slash:
            raise InvalidOperation
        input_rate, output_rate = Decimal(input_text), Decimal(output_text)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"invalid INPUT/OUTPUT rates in {value!r}") from exc
    return Expectation(provider, model, input_rate, output_rate, card_id.strip() or None)


def _long_context(value: str) -> tuple[str, str, int]:
    target, sep, threshold = value.partition("=")
    if not sep or not threshold.strip().isdigit() or int(threshold) <= 1:
        raise argparse.ArgumentTypeError(
            f"expected PROVIDER:MODEL=FIRST_LONG_CONTEXT_TOKEN, got {value!r}"
        )
    provider, model = _model_ref(target)
    return provider, model, int(threshold)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        type=_expectation,
        metavar="PROVIDER:MODEL=INPUT/OUTPUT[@RATE_CARD_ID]",
        help="Expected standard USD/1M input/output rates and, optionally, the approved card",
    )
    parser.add_argument(
        "--long-context",
        action="append",
        default=[],
        type=_long_context,
        metavar="PROVIDER:MODEL=FIRST_LONG_CONTEXT_TOKEN",
        help="Prompt size at which long-context pricing must start",
    )
    parser.add_argument(
        "--min-selectable",
        type=int,
        default=0,
        help="Fail if fewer selectable models are built (in addition to the registry parity check)",
    )
    parser.add_argument(
        "--expiry-warning-days",
        type=int,
        default=45,
        help="Warn about approved cards ending within this many days without a successor",
    )
    return parser


def _rate(value: Any) -> str:
    return "-" if value is None else str(value)


def _check_registry(report: Report) -> tuple[Any, Any] | None:
    """Build the legacy and database registries; return them or None on failure."""

    from orchestrator.model_registry import ModelRegistry
    from pricing.service import clear_cache, validate_readiness

    os.environ["MODEL_PRICING_MODE"] = "legacy"
    try:
        legacy = ModelRegistry.from_yaml()
    except Exception as exc:
        report.fail(f"legacy registry baseline failed to build: {exc!r}")
        return None

    os.environ["MODEL_PRICING_MODE"] = "database"
    clear_cache()
    try:
        validate_readiness()
        report.ok(
            "startup readiness: schema, enabled-model cards, search and runtime cards, "
            "registry build"
        )
    except Exception as exc:
        report.fail(f"startup readiness would fail: {exc!r}")

    try:
        registry = ModelRegistry.from_yaml()
    except Exception as exc:
        report.fail(f"model registry failed to build with database pricing: {exc!r}")
        report.out(traceback.format_exc().rstrip())
        return None
    report.ok(f"model registry built from database pricing ({len(registry.list_models())} models)")
    return legacy, registry


def _check_catalog(report: Report, legacy: Any, registry: Any, min_selectable: int) -> None:
    selectable = registry.list_selectable_models()
    pairs = {(c.provider, c.model_name) for c in selectable}
    legacy_pairs = {(c.provider, c.model_name) for c in legacy.list_selectable_models()}

    counts = {provider: 0 for provider in EXPECTED_PROVIDERS}
    for provider, _ in pairs:
        counts[provider] = counts.get(provider, 0) + 1
    summary = " ".join(f"{provider}={count}" for provider, count in sorted(counts.items()))
    missing_providers = [provider for provider in EXPECTED_PROVIDERS if not counts.get(provider)]
    if missing_providers:
        report.fail(f"providers without selectable models: {', '.join(missing_providers)}")
    else:
        report.ok(f"all providers have selectable models: {summary}")

    if pairs != legacy_pairs:
        lost = sorted(f"{p}:{m}" for p, m in legacy_pairs - pairs)
        extra = sorted(f"{p}:{m}" for p, m in pairs - legacy_pairs)
        report.fail(f"selectable models differ from the registry (lost={lost}, extra={extra})")
    else:
        report.ok(f"selectable models match the registry configuration ({len(pairs)})")
    if len(pairs) < min_selectable:
        report.fail(f"{len(pairs)} selectable models built; expected at least {min_selectable}")


def _check_rates(report: Report, registry: Any) -> dict[tuple[str, str], dict[str, Any]]:
    from config.pricing import ModelPricing

    snapshots: dict[tuple[str, str], dict[str, Any]] = {}
    mismatches: list[str] = []
    report.info(
        "provider  model                      source         rate_card_id                          "
        "input   output  cached  write   write1h  until"
    )
    for candidate in sorted(
        registry.list_selectable_models(), key=lambda c: (c.provider, c.model_name)
    ):
        key = (candidate.provider, candidate.model_name)
        snapshot = ModelPricing.get_pricing_snapshot(*key)
        if snapshot is None:
            report.fail(f"{key[0]}:{key[1]} has no approved card now")
            continue
        snapshots[key] = snapshot
        report.info(
            f"{key[0]:<9} {key[1]:<26} {_rate(snapshot.get('pricing_source')):<14} "
            f"{_rate(snapshot.get('rate_card_id')):<37} {_rate(snapshot.get('input')):<7} "
            f"{_rate(snapshot.get('output')):<7} {_rate(snapshot.get('cached_input')):<7} "
            f"{_rate(snapshot.get('cache_write')):<7} {_rate(snapshot.get('cache_write_1h')):<8} "
            f"{_rate(snapshot.get('effective_until'))}"
            + ("  STALE" if snapshot.get("pricing_stale") else "")
        )
        for rate_key, attribute in (
            ("input", "input_cost_per_1m"),
            ("output", "output_cost_per_1m"),
            *OPTIONAL_RATES,
        ):
            catalog_value = getattr(candidate, attribute)
            if rate_key in snapshot and snapshot[rate_key] is not None:
                if catalog_value is None or Decimal(str(catalog_value)) != Decimal(
                    str(snapshot[rate_key])
                ):
                    mismatches.append(f"{key[0]}:{key[1]} {rate_key}={catalog_value!r}")
            elif catalog_value is not None:
                mismatches.append(
                    f"{key[0]}:{key[1]} {rate_key} is missing on the card but "
                    f"catalog shows {catalog_value!r}"
                )
    if mismatches:
        report.fail("catalog rates disagree with approved cards: " + "; ".join(mismatches))
    else:
        report.ok("catalog rates match approved cards; missing rates stay missing, not $0")
    stale = sorted(f"{p}:{m}" for (p, m), s in snapshots.items() if s.get("pricing_stale"))
    if stale:
        report.warn(f"stale approved cards (last verification is old): {', '.join(stale)}")
    return snapshots


def _check_expectations(
    report: Report,
    snapshots: dict[tuple[str, str], dict[str, Any]],
    expectations: list[Expectation],
    long_context: list[tuple[str, str, int]],
) -> None:
    from config.pricing import ModelPricing

    for item in expectations:
        snapshot = snapshots.get((item.provider, item.model))
        label = f"{item.provider}:{item.model}"
        if snapshot is None:
            report.fail(f"expected {label} is not a selectable model with an approved card")
            continue
        actual = (Decimal(str(snapshot["input"])), Decimal(str(snapshot["output"])))
        problems = []
        if actual != (item.input_rate, item.output_rate):
            problems.append(
                f"rates {actual[0]}/{actual[1]} != {item.input_rate}/{item.output_rate}"
            )
        if item.rate_card_id and snapshot.get("rate_card_id") != item.rate_card_id:
            problems.append(f"card {snapshot.get('rate_card_id')} != {item.rate_card_id}")
        if problems:
            report.fail(f"{label}: " + "; ".join(problems))
        else:
            report.ok(
                f"{label} uses {snapshot.get('pricing_source')} card "
                f"{snapshot.get('rate_card_id')} at {actual[0]}/{actual[1]} per 1M"
            )

    for provider, model, threshold in long_context:
        label = f"{provider}:{model}"
        below = ModelPricing.get_pricing_snapshot(provider, model, prompt_tokens=threshold - 1)
        at = ModelPricing.get_pricing_snapshot(provider, model, prompt_tokens=threshold)
        if below is None or at is None:
            report.fail(f"{label}: no approved card for the long-context check")
        elif below.get("long_context_applied") or not at.get("long_context_applied"):
            report.fail(
                f"{label}: long-context pricing does not start at {threshold} tokens "
                f"(at {threshold - 1}: {below.get('long_context_applied')}, "
                f"at {threshold}: {at.get('long_context_applied')})"
            )
        else:
            report.ok(
                f"{label} long-context pricing starts at {threshold} tokens "
                f"(input {below['input']} -> {at['input']})"
            )


def _check_expiry(
    report: Report,
    snapshots: dict[tuple[str, str], dict[str, Any]],
    warning_days: int,
    now: datetime,
) -> None:
    from config.pricing import ModelPricing
    from pricing.models import utc

    for (provider, model), snapshot in sorted(snapshots.items()):
        until = snapshot.get("effective_until")
        if not until:
            continue
        ends = utc(until)
        successor = ModelPricing.get_pricing_snapshot(provider, model, at=ends)
        days = (ends - now).total_seconds() / 86400
        label = f"{provider}:{model} card {snapshot.get('rate_card_id')} ends {ends.isoformat()}"
        if successor is not None:
            report.info(f"{label}; successor card {successor.get('rate_card_id')} is approved")
        elif days <= warning_days:
            report.warn(
                f"{label} ({days:.0f} days) with no approved successor: the registry, catalog, "
                "Ask/Compare and startup readiness will fail then unless one is approved"
            )
        else:
            report.info(f"{label} ({days:.0f} days); no successor approved yet")


def run_preflight(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    now: datetime | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    report = Report(out)
    previous_mode = os.environ.get("MODEL_PRICING_MODE")
    try:
        built = _check_registry(report)
        if built is not None:
            legacy, registry = built
            _check_catalog(report, legacy, registry, args.min_selectable)
            snapshots = _check_rates(report, registry)
            _check_expectations(report, snapshots, args.expect, args.long_context)
            _check_expiry(
                report,
                snapshots,
                args.expiry_warning_days,
                now or datetime.now(timezone.utc),
            )
    finally:
        if previous_mode is None:
            os.environ.pop("MODEL_PRICING_MODE", None)
        else:
            os.environ["MODEL_PRICING_MODE"] = previous_mode
    if report.failures:
        out(f"RESULT: FAIL ({len(report.failures)} failed, {len(report.warnings)} warnings)")
        out("Do not set MODEL_PRICING_MODE=database for the service.")
        return 1
    out(f"RESULT: PASS ({len(report.warnings)} warnings)")
    return 0


def main() -> int:
    # Process-local only, and set before .env is read so they win over it:
    # logs to stdout (not the service's log files), and no automatic activation.
    os.environ["LOG_DESTINATION"] = "stdout"
    os.environ["LOG_LEVEL"] = "ERROR"
    os.environ["LOG_CONSOLE_LEVEL"] = "ERROR"
    os.environ["MODEL_PRICING_ALLOW_AUTO_ACTIVATION"] = "false"
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return run_preflight()


if __name__ == "__main__":
    raise SystemExit(main())
