from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from server.app import create_app
from server.billing.plan_catalog import PlanCatalog, get_plan_catalog

CATALOG_PATH = Path("config/subscription_plans.yaml")


def _catalog_data() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


def _write_catalog(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "subscription_plans.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_default_plan_catalog_loads_as_immutable_decimal_backed_types():
    catalog = PlanCatalog.from_yaml()

    assert catalog.version == 3
    assert [plan.code for plan in catalog.list_plans()] == ["free", "plus", "pro"]
    assert catalog.require("plus").monthly_price_usd == Decimal("6.99")
    assert catalog.require("pro").entitlements.allowed_billing_classes == frozenset(
        {"economical", "standard", "advanced", "premium"}
    )
    assert catalog.require("free").allowances.ai_credits == 100_000
    assert catalog.require("plus").allowances.ai_credits == 1_000_000
    assert catalog.require("pro").allowances.ai_credits == 3_000_000
    assert [plan.limits.requests_per_minute for plan in catalog.list_plans()] == [5, 15, 30]
    assert catalog.require("free").entitlements.usage_export_enabled is False
    assert catalog.require("free").entitlements.work_enabled is False
    assert catalog.require("plus").entitlements.work_enabled is True
    assert catalog.require("plus").limits.max_active_work_runs == 1
    assert catalog.require("plus").limits.max_work_credit_budget == 250_000
    assert catalog.require("pro").entitlements.custom_mcp_enabled is True
    assert catalog.require("pro").limits.max_tool_connections == 10
    assert catalog.require("pro").limits.max_work_credit_budget == 1_000_000
    with pytest.raises(FrozenInstanceError):
        catalog.require("free").rank = 99


def test_default_plan_catalog_is_process_cached():
    get_plan_catalog.cache_clear()

    first = get_plan_catalog()
    second = get_plan_catalog()

    assert first is second


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["plans"].pop("free"), "missing required 'free' plan"),
        (
            lambda data: data["plans"]["plus"].update({"rank": data["plans"]["free"]["rank"]}),
            "duplicate plan rank",
        ),
        (
            lambda data: data["plans"]["free"]["allowances"].update({"ai_credits": -1}),
            "cannot be negative",
        ),
        (
            lambda data: data["plans"]["free"]["entitlements"].update({"unknown_feature": True}),
            "unknown key",
        ),
        (
            lambda data: data["plans"]["free"]["entitlements"].update(
                {"allowed_billing_classes": ["standard", "vip"]}
            ),
            "unknown billing class",
        ),
        (
            lambda data: data["plans"]["plus"]["price"].update({"stripe_price_env": None}),
            "stripe_price_env is required",
        ),
        (
            lambda data: data["plans"]["free"]["entitlements"].update({"max_compare_models": 1}),
            "max_compare_models must be at least 2",
        ),
        (
            lambda data: data["plans"]["free"]["price"].update({"monthly_usd": 1}),
            "monthly price must equal zero",
        ),
        (
            lambda data: data["plans"]["plus"]["price"].update({"monthly_usd": 0}),
            "monthly price must be positive",
        ),
    ],
)
def test_invalid_plan_catalog_fails_clearly(tmp_path, mutate, message):
    data = _catalog_data()
    mutate(data)

    with pytest.raises(ValueError, match=message):
        PlanCatalog.from_yaml(_write_catalog(tmp_path, data))


def test_application_startup_validates_plan_catalog(monkeypatch):
    monkeypatch.setattr(
        "server.app.get_plan_catalog",
        lambda: (_ for _ in ()).throw(ValueError("invalid plans at startup")),
    )

    with pytest.raises(ValueError, match="invalid plans at startup"):
        with TestClient(create_app()):
            pass
