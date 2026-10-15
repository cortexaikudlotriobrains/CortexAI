"""Credit ladder: the fixed customer price points for selectable models."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
LADDER_PATH = ROOT / "config" / "credit_ladder.yaml"
PLANS_PATH = ROOT / "config" / "subscription_plans.yaml"


@dataclass(frozen=True)
class Step:
    id: str
    input: Decimal
    output: Decimal
    billing_class: str
    label: str

    def covers(self, input_rate: Decimal, output_rate: Decimal) -> bool:
        return self.input >= input_rate and self.output >= output_rate

    def describe(self) -> str:
        return f"{_plain(self.input)}/{_plain(self.output)}"


@dataclass(frozen=True)
class Ladder:
    steps: tuple[Step, ...]
    legacy_exceptions: frozenset[str]

    def lowest_covering(self, input_rate: Any, output_rate: Any) -> Step | None:
        """Return the cheapest step at or above both provider rates (USD per 1M)."""

        rate_in, rate_out = Decimal(str(input_rate)), Decimal(str(output_rate))
        for step in self.steps:
            if step.covers(rate_in, rate_out):
                return step
        return None

    def step_for(self, input_multiplier: Any, output_multiplier: Any) -> Step | None:
        """Return the step whose multipliers equal the given pair, if any."""

        pair = (Decimal(str(input_multiplier)), Decimal(str(output_multiplier)))
        return next((step for step in self.steps if (step.input, step.output) == pair), None)


def load_ladder(path: Path = LADDER_PATH) -> Ladder:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = tuple(
        Step(
            id=str(item["id"]),
            input=Decimal(str(item["input"])),
            output=Decimal(str(item["output"])),
            billing_class=str(item["billing_class"]),
            label=str(item["label"]),
        )
        for item in data["steps"]
    )
    for lower, higher in pairwise(steps):
        if not (higher.input >= lower.input and higher.output >= lower.output):
            raise ValueError(f"Credit ladder steps must not decrease: {lower.id} -> {higher.id}")
    return Ladder(
        steps=steps,
        legacy_exceptions=frozenset(str(key) for key in data.get("legacy_exceptions") or []),
    )


def plans_by_class(path: Path = PLANS_PATH) -> dict[str, list[str]]:
    """Map each billing class to the plan display names that may use it."""

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for _code, plan in sorted(data["plans"].items(), key=lambda item: item[1]["rank"]):
        for billing_class in plan["entitlements"]["allowed_billing_classes"]:
            out.setdefault(str(billing_class), []).append(str(plan["display_name"]))
    return out


def _plain(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text
