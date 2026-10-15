"""Rules every registry change must pass. Run with `python scripts/models.py check`.

Errors fail CI. Warnings are printed and also appear in the weekly report.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from tools.model_catalog.ladder import Ladder, load_ladder, plans_by_class
from tools.model_catalog.registry_text import PROVIDERS_PATH, REGISTRY_PATH, ROOT

LOCK_PATH = ROOT / "config" / "known_model_ids.txt"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
CLASS_RANK = {"economical": 0, "standard": 1, "advanced": 2, "premium": 3}
NON_ACTIVE = {"DEPRECATED", "SCHEDULED_FOR_RETIREMENT", "RETIRED", "ALIAS_REDIRECTED"}
PROMO_WARNING_DAYS = 45
MAX_REPLACEMENT_HOPS = 3


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def iter_models(registry: dict[str, Any]):
    for provider, block in registry["providers"].items():
        for model in block.get("models") or []:
            yield provider, model


def model_index(registry: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(provider, str(model["name"])): model for provider, model in iter_models(registry)}


def lifecycle_of(model: dict[str, Any]) -> dict[str, Any]:
    lifecycle = model.get("lifecycle")
    return lifecycle if isinstance(lifecycle, dict) else {}


def lifecycle_status(model: dict[str, Any]) -> str:
    return str(lifecycle_of(model).get("status") or "ACTIVE").upper()


def is_selectable(model: dict[str, Any]) -> bool:
    return bool(model.get("enabled", True)) and bool(
        model.get("selectable", model.get("enabled", True))
    )


def known_ids(registry: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for provider, model in iter_models(registry):
        ids.add(f"{provider}:{model['name']}")
        ids.update(f"{provider}:{alias}" for alias in model.get("aliases") or [])
    return ids


def read_lock(path: Path = LOCK_PATH) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def write_lock(registry: dict[str, Any], path: Path = LOCK_PATH) -> list[str]:
    """Append IDs that are new; never remove any. Returns the added IDs."""

    existing = read_lock(path)
    added = sorted(known_ids(registry) - set(existing))
    if added or not path.exists():
        header = (
            "# Every model ID (name or alias) the registry has ever listed, one per line.\n"
            "# `scripts/models.py check` fails if one disappears: hide or retire models,\n"
            "# never delete them. Append new IDs with `scripts/models.py lock`.\n"
        )
        body = "\n".join(sorted(set(existing) | set(added)))
        path.write_text(header + body + "\n", encoding="utf-8")
    return added


def _current_rule(model: dict[str, Any], at: datetime) -> dict[str, Any] | None:
    applicable = [
        rule
        for rule in model.get("pricing_rules") or []
        if str(rule.get("processing_mode") or "standard") == "standard"
        and _utc(rule.get("effective_from") or "1970-01-01T00:00:00Z") <= at
        and (not rule.get("effective_until") or at < _utc(rule["effective_until"]))
    ]
    return max(
        applicable,
        key=lambda rule: _utc(rule.get("effective_from") or "1970-01-01T00:00:00Z"),
        default=None,
    )


def _peak_rates(rule: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """Highest standard-context input/output rate of a rule (peak for schedules)."""

    rate_in, rate_out = Decimal(str(rule["input"])), Decimal(str(rule["output"]))
    off_peak = (rule.get("schedule") or {}).get("off_peak_tokens") or {}
    if off_peak:
        rate_in = max(rate_in, Decimal(str(off_peak.get("input", rate_in))))
        rate_out = max(rate_out, Decimal(str(off_peak.get("output", rate_out))))
    return rate_in, rate_out


def _promotion_ended(model: dict[str, Any], at: datetime) -> bool:
    review = model.get("pricing_review") or {}
    until = review.get("effective_until")
    return bool(review.get("requires_expiry") and until and at >= _utc(until))


def _check_credits(
    key: str, model: dict[str, Any], ladder: Ladder, at: datetime, result: CheckResult
) -> None:
    in_mult = Decimal(str(model["input_credit_multiplier"]))
    out_mult = Decimal(str(model["output_credit_multiplier"]))
    step = ladder.step_for(in_mult, out_mult)
    if key in ladder.legacy_exceptions:
        result.warnings.append(
            f"{key}: credits {in_mult}/{out_mult} predate the credit ladder (legacy exception)"
        )
    elif step is None:
        result.errors.append(
            f"{key}: credits {in_mult}/{out_mult} are not a credit ladder step "
            f"({', '.join(s.describe() for s in ladder.steps)})"
        )
    else:
        if str(model.get("billing_class")) != step.billing_class:
            result.errors.append(
                f"{key}: billing_class {model.get('billing_class')} does not match ladder step "
                f"{step.id} ({step.billing_class})"
            )
        if str(model.get("credit_usage_label")) != step.label:
            result.errors.append(
                f"{key}: credit_usage_label '{model.get('credit_usage_label')}' should be "
                f"'{step.label}' for ladder step {step.id}"
            )
    rule = _current_rule(model, at)
    if rule is None:
        return
    rate_in, rate_out = _peak_rates(rule)
    if in_mult < rate_in or out_mult < rate_out:
        message = (
            f"{key}: credits {in_mult}/{out_mult} are below the provider price "
            f"{rate_in}/{rate_out}; the runtime floor charges the provider price"
        )
        if _promotion_ended(model, at):
            result.warnings.append(message + " (launch promotion ended; reprice)")
        else:
            result.errors.append(message)


def _check_replacements(index: dict[tuple[str, str], dict[str, Any]], result: CheckResult) -> None:
    for (provider, name), model in index.items():
        lifecycle = lifecycle_of(model)
        target = lifecycle.get("alias_target") or lifecycle.get("replacement_model")
        if not target:
            continue
        key = f"{provider}:{name}"
        seen = {name}
        current, hops = str(target), 0
        first = index.get((provider, current))
        if first is None:
            result.errors.append(f"{key}: replacement '{current}' is not a {provider} model")
            continue
        if CLASS_RANK.get(str(first.get("billing_class")), 9) > CLASS_RANK.get(
            str(model.get("billing_class")), -1
        ):
            result.errors.append(
                f"{key}: replacement {current} has a higher billing class "
                f"({first.get('billing_class')} > {model.get('billing_class')})"
            )
        while True:
            node = index.get((provider, current))
            if node is None:
                result.errors.append(f"{key}: replacement chain reaches unknown model {current}")
                break
            if is_selectable(node) and lifecycle_status(node) == "ACTIVE":
                break
            hops += 1
            if current in seen or hops > MAX_REPLACEMENT_HOPS:
                result.errors.append(
                    f"{key}: replacement chain does not reach a selectable model "
                    f"within {MAX_REPLACEMENT_HOPS} steps"
                )
                break
            seen.add(current)
            node_lifecycle = lifecycle_of(node)
            nxt = node_lifecycle.get("alias_target") or node_lifecycle.get("replacement_model")
            if not nxt:
                result.errors.append(
                    f"{key}: replacement {current} is hidden and names no replacement of its own"
                )
                break
            current = str(nxt)


def _check_defaults(
    registry: dict[str, Any],
    index: dict[tuple[str, str], dict[str, Any]],
    result: CheckResult,
) -> None:
    providers = yaml.safe_load(PROVIDERS_PATH.read_text(encoding="utf-8"))["providers"]
    for spec in providers:
        provider = spec["id"]
        if not any(is_selectable(m) for (p, _), m in index.items() if p == provider):
            result.errors.append(f"{provider}: no selectable model")
        for field_name in ("default_model", "compare_default_model"):
            name = spec.get(field_name)
            if not name:
                continue
            model = index.get((provider, str(name)))
            if model is None or not is_selectable(model) or lifecycle_status(model) != "ACTIVE":
                result.errors.append(
                    f"providers.yaml {provider}.{field_name}={name} is not a selectable ACTIVE model"
                )

    def selectable_key(value: str, where: str) -> dict[str, Any] | None:
        provider, _, name = str(value).partition(":")
        model = index.get((provider, name))
        if model is None or not is_selectable(model):
            result.errors.append(f"{where}: {value} is not a selectable model")
            return None
        return model

    ui = registry.get("ui_defaults") or {}
    plan_classes = yaml.safe_load((ROOT / "config" / "subscription_plans.yaml").read_text())[
        "plans"
    ]
    for plan, value in (ui.get("ask_by_plan") or {}).items():
        model = selectable_key(value, f"ui_defaults.ask_by_plan.{plan}")
        allowed = (
            plan_classes.get(plan, {}).get("entitlements", {}).get("allowed_billing_classes", [])
        )
        if model is not None and str(model.get("billing_class")) not in allowed:
            result.errors.append(
                f"ui_defaults.ask_by_plan.{plan}: {value} is {model.get('billing_class')}, "
                f"which the {plan} plan cannot use"
            )
    for value in ui.get("compare") or []:
        selectable_key(value, "ui_defaults.compare")
    if ui.get("compare_added"):
        selectable_key(ui["compare_added"], "ui_defaults.compare_added")
    for value in (registry.get("routing_defaults") or {}).get("coding_preference") or []:
        selectable_key(value, "routing_defaults.coding_preference")


def _check_env_example(index: dict[tuple[str, str], dict[str, Any]], result: CheckResult) -> None:
    if not ENV_EXAMPLE_PATH.exists():
        return
    env = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=([^#\s]*)", line.strip())
        if match:
            env[match[1]] = match[2]
    providers = yaml.safe_load(PROVIDERS_PATH.read_text(encoding="utf-8"))["providers"]
    # (provider, variable, warn when the model is hidden from the picker)
    checks = [(spec["id"], spec["default_model_env"], True) for spec in providers]
    checks += [
        ("openai", "CORTEX_ANALYSIS_MODEL", True),
        # Provisioning input for Work's Managed Agent; it never reaches the picker.
        ("claude", "ANTHROPIC_MANAGED_AGENT_MODEL", False),
        ("gemini", "PROMPT_OPTIMIZER_GEMINI_MODEL", True),
        (env.get("PROMPT_OPTIMIZER_PROVIDER", ""), "PROMPT_OPTIMIZER_MODEL", True),
    ]
    aliases = {
        (provider, str(alias)): model
        for (provider, _), model in index.items()
        for alias in model.get("aliases") or []
    }
    for provider, variable, warn_hidden in checks:
        value = env.get(variable)
        if not value or not provider:
            continue
        model = index.get((provider, value)) or aliases.get((provider, value))
        if model is None:
            result.errors.append(f".env.example {variable}={value} is not a {provider} model")
        elif not model.get("enabled", True):
            result.errors.append(f".env.example {variable}={value} is disabled")
        elif lifecycle_status(model) != "ACTIVE":
            result.warnings.append(f".env.example {variable}={value} is {lifecycle_status(model)}")
        elif warn_hidden and not is_selectable(model):
            result.warnings.append(f".env.example {variable}={value} is hidden from the picker")


def run_checks(*, at: datetime | None = None, check_frontend: bool = True) -> CheckResult:
    os.environ.setdefault("MODEL_PRICING_MODE", "legacy")
    now = at or datetime.now(timezone.utc)
    result = CheckResult()
    registry = load_registry()
    index = model_index(registry)
    ladder = load_ladder()

    # The runtime loaders must accept the file.
    try:
        from orchestrator.model_registry import ModelRegistry
        from pricing.identity import ModelIdentityMap

        ModelRegistry.from_yaml()
        ModelIdentityMap()
    except Exception as exc:  # reported as a check error, not raised
        result.errors.append(f"Registry does not load: {exc}")
        return result

    names: dict[tuple[str, str], int] = {}
    for provider, model in iter_models(registry):
        names[(provider, str(model["name"]))] = names.get((provider, str(model["name"])), 0) + 1
    for (provider, name), count in names.items():
        if count > 1:
            result.errors.append(f"{provider}:{name} is listed {count} times")

    missing = [key for key in read_lock() if key not in known_ids(registry)]
    for key in missing:
        result.errors.append(
            f"{key} was removed from the registry; hide or retire models instead of deleting them"
        )
    unlocked = sorted(known_ids(registry) - set(read_lock()))
    if unlocked:
        result.errors.append(
            "New model IDs are not in config/known_model_ids.txt; run `python scripts/models.py lock`: "
            + ", ".join(unlocked)
        )

    for (provider, name), model in index.items():
        key = f"{provider}:{name}"
        status = lifecycle_status(model)
        lifecycle = lifecycle_of(model)
        if status in NON_ACTIVE and is_selectable(model):
            result.errors.append(f"{key}: {status} models must not be selectable")
        if status == "SCHEDULED_FOR_RETIREMENT":
            if not lifecycle.get("retirement_date"):
                result.errors.append(f"{key}: SCHEDULED_FOR_RETIREMENT needs retirement_date")
            elif _utc(lifecycle["retirement_date"]) <= now:
                result.warnings.append(
                    f"{key}: retirement date {lifecycle['retirement_date']} has passed; mark it RETIRED"
                )
        if status in {"RETIRED", "ALIAS_REDIRECTED"} and not (
            lifecycle.get("replacement_model") or lifecycle.get("alias_target")
        ):
            result.errors.append(f"{key}: {status} models need a replacement_model")
        if not is_selectable(model):
            continue
        _check_credits(key, model, ladder, now, result)
        if str(model.get("release_status")) == "preview" and not lifecycle.get("replacement_model"):
            result.warnings.append(f"{key}: preview model has no replacement_model fallback")
        review = model.get("pricing_review") or {}
        until = review.get("effective_until")
        if review.get("requires_expiry") and until:
            ends = _utc(until)
            successor = any(
                rule.get("effective_from") and _utc(rule["effective_from"]) == ends
                for rule in model.get("pricing_rules") or []
            )
            if now <= ends <= now + timedelta(days=PROMO_WARNING_DAYS):
                result.warnings.append(
                    f"{key}: launch price ends {until}"
                    + ("" if successor else " with no successor pricing rule")
                )

    _check_replacements(index, result)
    _check_defaults(registry, index, result)
    _check_env_example(index, result)

    if check_frontend:
        from tools.model_catalog.frontend import GENERATED_PATH, render_frontend_defaults

        expected = render_frontend_defaults(registry)
        actual = GENERATED_PATH.read_text(encoding="utf-8") if GENERATED_PATH.exists() else ""
        if expected != actual:
            result.errors.append(
                f"{GENERATED_PATH.relative_to(ROOT)} is stale; run `python scripts/models.py sync-frontend`"
            )
    return result


def class_plans_text(billing_class: str) -> str:
    return ", ".join(plans_by_class().get(billing_class, [])) or "no plan"
