"""Regression: the full model registry and catalog must build under database pricing.

Incident (2026-10-13): production switched to ``MODEL_PRICING_MODE=database``.
``/health`` and the startup pricing readiness check passed, but
``ModelRegistry.from_yaml()`` raised ``KeyError: 'cache_write'`` because the
approved LiteLLM card for ``gemini-3.6-flash`` legitimately has no cache-write
rate. Every route that builds the registry (``/v1/model-options``,
``/v1/providers``, ``/v1/history``, Ask, Compare) failed, so the React app fell
back to its bundled OpenAI/Claude/DeepSeek catalog and stopped refreshing
history.

These tests use an isolated in-memory SQLite rate-card store seeded the same
way production was: ``seed_legacy`` for every enabled model, then the four
approved LiteLLM cards, re-keyed to their production IDs. Time is pinned so
card expiry dates cannot make the suite time-dependent.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from config.pricing import ModelPricing
from orchestrator.model_registry import ModelRegistry
from pricing.models import PricingUnavailableError, utc
from pricing.service import clear_cache, validate_model_registry, validate_readiness
from pricing.sources import LiteLLMPricingSource
from pricing.sync import seed_legacy
from server.dependencies import AuthResult, get_auth
from tests.test_model_rate_cards import pricing_db as _pricing_db
from utils.cost_calculator import CostCalculator

pricing_db = _pricing_db

NOW = utc("2026-10-14T12:00:00Z")
APPROVED_AT = utc("2026-10-13T00:00:00Z")
GEMINI_UNTIL = utc("2027-01-01T00:00:00Z")
SOL_UNTIL = utc("2026-11-21T00:00:00Z")

PROVIDERS = {"openai", "claude", "gemini", "grok", "deepseek"}

# The 21 models selectable in production on 2026-10-13. Asserted as a subset so
# adding a model to the registry does not break this test, but losing one does.
PRODUCTION_SELECTABLE_MODELS = {
    ("openai", "gpt-5.6-luna"),
    ("openai", "gpt-5.4-nano"),
    ("openai", "gpt-4o-mini"),
    ("openai", "gpt-5.4-mini"),
    ("openai", "gpt-5.6-terra"),
    ("openai", "gpt-5.4"),
    ("openai", "gpt-5.6-sol"),
    ("gemini", "gemini-3.5-flash-lite"),
    ("gemini", "gemini-3.6-flash"),
    ("gemini", "gemini-3.1-pro-preview"),
    ("deepseek", "deepseek-v4-flash"),
    ("deepseek", "deepseek-v4-pro"),
    ("grok", "grok-4.3"),
    ("grok", "grok-4.5"),
    ("claude", "claude-haiku-4-5"),
    ("claude", "claude-sonnet-5"),
    ("claude", "claude-opus-5"),
    ("claude", "claude-fable-5"),
    ("claude", "claude-sonnet-4-6"),
    ("claude", "claude-opus-4-6"),
    ("claude", "claude-opus-4-5"),
}

# Approved LiteLLM cards in production PostgreSQL (rates per LiteLLM token units).
APPROVED_LITELLM_CARDS = {
    ("claude", "claude-haiku-4-5"): {
        "id": "6884812d-54ab-49ec-9313-b64cf2c825ae",
        "until": None,
        "row": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": "0.000001",
            "output_cost_per_token": "0.000005",
            "cache_read_input_token_cost": "0.0000001",
            "cache_creation_input_token_cost": "0.00000125",
            "cache_creation_input_token_cost_above_1hr": "0.000002",
        },
    },
    ("claude", "claude-sonnet-5"): {
        "id": "7ea64bd6-380d-4d6e-9d60-9b0831eb3ae1",
        "until": None,
        "row": {
            "litellm_provider": "anthropic",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.00001",
            "cache_read_input_token_cost": "0.0000002",
            "cache_creation_input_token_cost": "0.0000025",
            "cache_creation_input_token_cost_above_1hr": "0.000004",
        },
    },
    ("gemini", "gemini-3.6-flash"): {
        "id": "f6021121-63be-40e5-b434-9cdaa12251ef",
        "until": GEMINI_UNTIL,
        "row": {
            "litellm_provider": "gemini",
            "input_cost_per_token": "0.00000075",
            "output_cost_per_token": "0.00000375",
            "cache_read_input_token_cost": "0.000000075",
            # No cache_creation_input_token_cost: LiteLLM publishes none.
        },
    },
    ("openai", "gpt-5.6-sol"): {
        "id": "7d77e71c-8395-46cb-9746-0e3acb19f408",
        "until": SOL_UNTIL,
        "row": {
            "litellm_provider": "openai",
            "input_cost_per_token": "0.000004",
            "output_cost_per_token": "0.000020",
            "cache_read_input_token_cost": "0.0000004",
            "cache_creation_input_token_cost": "0.000005",
            "cache_creation_input_token_cost_above_1hr": "0.000005",
            "input_cost_per_token_above_272k_tokens": "0.000008",
            "output_cost_per_token_above_272k_tokens": "0.000030",
            "cache_read_input_token_cost_above_272k_tokens": "0.0000008",
            "cache_creation_input_token_cost_above_272k_tokens": "0.000010",
        },
    },
}

EXPECTED_STANDARD_RATES = {
    ("claude", "claude-haiku-4-5"): (1.0, 5.0),
    ("claude", "claude-sonnet-5"): (2.0, 10.0),
    ("gemini", "gemini-3.6-flash"): (0.75, 3.75),
    ("openai", "gpt-5.6-sol"): (4.0, 20.0),
}


def _clear_registry_caches() -> None:
    from server.billing import enforcement_service, response_credit_service
    from server import cortex_analysis

    clear_cache()
    response_credit_service._credit_registry.cache_clear()
    enforcement_service._default_model_registry.cache_clear()
    cortex_analysis._model_registry.cache_clear()


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(
        "pricing.service.utc", lambda value=None: utc(value) if value is not None else NOW
    )
    monkeypatch.setattr("config.pricing._utc_now", lambda: NOW)
    return NOW


@pytest.fixture
def production_rate_cards(pricing_db, frozen_now, monkeypatch):
    """Legacy seeds for every enabled model plus the four approved LiteLLM cards."""

    db, repo = pricing_db
    seed_legacy(db)
    catalog = LiteLLMPricingSource.parse(
        json.dumps(
            {model: card["row"] for (_provider, model), card in APPROVED_LITELLM_CARDS.items()}
        ).encode()
    )
    for (provider, model), card in APPROVED_LITELLM_CARDS.items():
        entry = catalog.entries[model]
        assert entry["error"] is None, (model, entry["error"])
        assert entry["provider"] == provider
        added = repo.add(
            provider=provider,
            model=model,
            source="LITELLM",
            rates=entry["rates"],
            at=APPROVED_AT,
            until=card["until"],
            reference=catalog.reference,
            version=catalog.version,
            verified_at=APPROVED_AT,
        )
        db.execute(update(repo.cards).where(repo.cards.c.id == added["id"]).values(id=card["id"]))
    db.commit()
    _clear_registry_caches()
    monkeypatch.setenv("MODEL_PRICING_MODE", "database")
    monkeypatch.setenv("MODEL_PRICING_ALLOW_AUTO_ACTIVATION", "false")
    yield db, repo
    _clear_registry_caches()


def _legacy_registry_hard_index(snapshot, key):
    """The pre-fix contract: every optional rate was assumed present."""
    return float(snapshot[key]) if snapshot is not None else None


# ---------------------------------------------------------------------------
# Reproduction
# ---------------------------------------------------------------------------


def test_pre_fix_registry_contract_reproduces_production_keyerror(
    production_rate_cards, monkeypatch
):
    monkeypatch.setattr("orchestrator.model_registry._optional_rate", _legacy_registry_hard_index)
    with pytest.raises(KeyError, match="cache_write"):
        ModelRegistry.from_yaml()


def test_per_model_price_checks_alone_do_not_prove_catalog_health(
    production_rate_cards, monkeypatch
):
    """The old readiness loop passed in production while the registry could not build."""

    from config.pricing import _catalog
    from pricing.service import get_snapshot

    # Every check the pre-incident readiness gate made still passes ...
    for provider, block in _catalog()["providers"].items():
        for model in block["models"]:
            if model.get("enabled", True):
                assert ModelPricing.get_pricing_snapshot(provider, model["name"]) is not None
    for provider in PROVIDERS:
        assert get_snapshot(provider, "__web_search__") is not None
    assert get_snapshot("claude", "__managed_runtime__") is not None

    # ... yet the registry the routes depend on could not be built.
    monkeypatch.setattr("orchestrator.model_registry._optional_rate", _legacy_registry_hard_index)
    with pytest.raises(PricingUnavailableError, match="Model registry failed to build"):
        validate_model_registry()


def test_startup_readiness_now_builds_the_registry(production_rate_cards, monkeypatch):
    db, _ = production_rate_cards
    monkeypatch.setattr("db.engine.get_engine", lambda: db.get_bind())
    monkeypatch.setattr("db.tables.DB_SCHEMA", "main")

    validate_readiness()

    monkeypatch.setattr("orchestrator.model_registry._optional_rate", _legacy_registry_hard_index)
    with pytest.raises(PricingUnavailableError, match="KeyError"):
        validate_readiness()


# ---------------------------------------------------------------------------
# Registry and catalog in database mode
# ---------------------------------------------------------------------------


def test_registry_builds_with_every_provider_and_selectable_model(
    production_rate_cards, monkeypatch
):
    registry = ModelRegistry.from_yaml()
    selectable = {(c.provider, c.model_name) for c in registry.list_selectable_models()}
    enabled = {(c.provider, c.model_name) for c in registry.list_enabled_models()}

    assert {provider for provider, _ in selectable} == PROVIDERS
    assert PRODUCTION_SELECTABLE_MODELS <= selectable

    # Database pricing must not change which models exist, only their rates.
    monkeypatch.setenv("MODEL_PRICING_MODE", "legacy")
    legacy = ModelRegistry.from_yaml()
    assert selectable == {(c.provider, c.model_name) for c in legacy.list_selectable_models()}
    assert enabled == {(c.provider, c.model_name) for c in legacy.list_enabled_models()}


def test_approved_cards_drive_the_catalog_rates(production_rate_cards):
    registry = ModelRegistry.from_yaml()
    for (provider, model), (input_rate, output_rate) in EXPECTED_STANDARD_RATES.items():
        candidate = registry.find_model(provider, model)
        assert candidate is not None
        assert candidate.input_cost_per_1m == input_rate
        assert candidate.output_cost_per_1m == output_rate
        assert candidate.pricing_rule_id == APPROVED_LITELLM_CARDS[(provider, model)]["id"]

    gemini = registry.find_model("gemini", "gemini-3.6-flash")
    assert gemini.pricing_effective_until == GEMINI_UNTIL.isoformat()
    sol = registry.find_model("openai", "gpt-5.6-sol")
    assert sol.pricing_effective_until == SOL_UNTIL.isoformat()


def test_gemini_missing_cache_write_stays_missing_not_zero(production_rate_cards):
    gemini = ModelRegistry.from_yaml().find_model("gemini", "gemini-3.6-flash")

    assert gemini.cached_input_cost_per_1m == 0.075
    assert gemini.cache_write_cost_per_1m is None

    snapshot = ModelPricing.get_pricing_snapshot("gemini", "gemini-3.6-flash")
    assert "cache_write" not in snapshot
    assert "cache_write_1h" not in snapshot


def test_optional_rates_mirror_card_presence_for_every_model(production_rate_cards):
    """Absent dimension -> None; explicit zero (DeepSeek cache writes) -> 0.0."""

    registry = ModelRegistry.from_yaml()
    for candidate in registry.list_enabled_models():
        snapshot = ModelPricing.get_pricing_snapshot(candidate.provider, candidate.model_name)
        for key, value in (
            ("cached_input", candidate.cached_input_cost_per_1m),
            ("cache_write", candidate.cache_write_cost_per_1m),
        ):
            if key in snapshot:
                assert value == float(snapshot[key]), (candidate.model_name, key)
            else:
                assert value is None, (candidate.model_name, key)

    deepseek = registry.find_model("deepseek", "deepseek-v4-flash")
    assert deepseek.cache_write_cost_per_1m == 0.0


def test_legacy_mode_registry_is_unchanged(pricing_db, frozen_now):
    registry = ModelRegistry.from_yaml()
    gemini = registry.find_model("gemini", "gemini-3.6-flash")
    assert (gemini.input_cost_per_1m, gemini.output_cost_per_1m) == (1.5, 7.5)
    assert gemini.cache_write_cost_per_1m == 1.5
    assert gemini.pricing_rule_id == "gemini-3.6-flash-standard-20260731"
    sol = registry.find_model("openai", "gpt-5.6-sol")
    assert sol.long_context_threshold_tokens == 272000


# ---------------------------------------------------------------------------
# Billing behaviour is unchanged
# ---------------------------------------------------------------------------


def test_gemini_cost_uses_card_and_fails_only_when_cache_write_is_billed(
    production_rate_cards,
):
    calculator = CostCalculator("gemini", "gemini-3.6-flash")

    cost = calculator.calculate_cost(1000, 100, cached_input_tokens=400, request_at=NOW)
    assert Decimal(cost["cost_audit"]["usage_calculated_provider_cost_usd"]) == Decimal(
        "0.000855"
    )  # (600 * 0.75 + 400 * 0.075 + 100 * 3.75) / 1M
    assert (
        cost["cost_audit"]["rate_card_id"]
        == APPROVED_LITELLM_CARDS[("gemini", "gemini-3.6-flash")]["id"]
    )
    assert cost["pricing_unknown"] is False
    assert "cache_write" not in cost["rates_per_1m"]

    with pytest.raises(PricingUnavailableError, match="cache_write"):
        calculator.calculate_cost(1000, 100, cache_write_tokens=10, request_at=NOW)
    with pytest.raises(PricingUnavailableError):
        calculator.calculate_cost(
            1000, 100, cache_write_tokens=10, cache_write_1h_tokens=10, request_at=NOW
        )
    assert ModelPricing.conservative_fallback("gemini") is None


@pytest.mark.parametrize(
    "prompt,expected_input,applied", [(272000, "4", False), (272001, "8", True)]
)
def test_sol_long_context_band_starts_at_272001(
    production_rate_cards, prompt, expected_input, applied
):
    snapshot = ModelPricing.get_pricing_snapshot("openai", "gpt-5.6-sol", prompt_tokens=prompt)
    assert snapshot["input"] == expected_input
    assert snapshot["long_context_applied"] is applied
    cost = CostCalculator("openai", "gpt-5.6-sol").calculate_cost(
        prompt, 1000, cached_input_tokens=1000, cache_write_tokens=100, request_at=NOW
    )
    expected = (
        Decimal(prompt - 1100) * Decimal(expected_input)
        + Decimal(1000) * Decimal("0.8" if applied else "0.4")
        + Decimal(100) * Decimal("10" if applied else "5")
        + Decimal(1000) * Decimal("30" if applied else "20")
    ) / Decimal(1_000_000)
    assert Decimal(cost["cost_audit"]["usage_calculated_provider_cost_usd"]) == expected


def test_sol_long_context_hour_cache_writes_stay_unknown(production_rate_cards):
    with pytest.raises(PricingUnavailableError, match="cache_write_1h"):
        ModelPricing.get_pricing_snapshot(
            "openai", "gpt-5.6-sol", prompt_tokens=272001, cache_write_ttl="1h"
        )


def test_claude_hour_cache_writes_and_reasoning_rates_are_preserved(production_rate_cards):
    hour = ModelPricing.get_pricing_snapshot("claude", "claude-sonnet-5", cache_write_ttl="1h")
    assert hour["cache_write"] == "4"
    cost = CostCalculator("claude", "claude-sonnet-5").calculate_cost(
        1000,
        1000,
        cache_write_tokens=200,
        cache_write_1h_tokens=50,
        reasoning_tokens=400,
        request_at=NOW,
    )
    components = cost["cost_audit"]["cost_components_usd"]
    assert Decimal(components["cache_write_cost"]) == Decimal(150) * Decimal("2.5") / 1_000_000
    assert Decimal(components["cache_write_1h_cost"]) == Decimal(50) * Decimal("4") / 1_000_000
    # No separate reasoning rate on the card: reasoning bills at output, once.
    assert "reasoning_cost" not in components
    assert Decimal(components["output_cost"]) == Decimal(1000) * Decimal("10") / 1_000_000


def test_card_expiry_boundaries_are_unchanged(production_rate_cards):
    gemini_before = ModelPricing.get_pricing_snapshot(
        "gemini", "gemini-3.6-flash", at=GEMINI_UNTIL - timedelta(microseconds=1)
    )
    assert (
        gemini_before["rate_card_id"]
        == APPROVED_LITELLM_CARDS[("gemini", "gemini-3.6-flash")]["id"]
    )
    # An expired verified card never falls back to legacy or zero pricing.
    assert ModelPricing.get_pricing_snapshot("gemini", "gemini-3.6-flash", at=GEMINI_UNTIL) is None
    assert ModelPricing.get_pricing_snapshot("openai", "gpt-5.6-sol", at=SOL_UNTIL) is None
    with pytest.raises(PricingUnavailableError):
        CostCalculator("openai", "gpt-5.6-sol").calculate_cost(10, 10, request_at=SOL_UNTIL)


def test_registry_cannot_build_once_a_card_ends_without_successor(
    production_rate_cards, monkeypatch
):
    """Current fail-closed behaviour that the preflight's expiry warning describes.

    Billing must never price an expired card, so an enabled model with no
    applicable card stops the whole registry from building. Approve a successor
    card before ``effective_until``.
    """

    monkeypatch.setattr(
        "pricing.service.utc", lambda value=None: utc(value) if value is not None else SOL_UNTIL
    )
    monkeypatch.setattr("config.pricing._utc_now", lambda: SOL_UNTIL)
    with pytest.raises(
        ValueError, match=re.escape("Missing effective pricing rule for openai:gpt-5.6-sol")
    ):
        ModelRegistry.from_yaml()


# ---------------------------------------------------------------------------
# HTTP surfaces the frontend depends on
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_client(production_rate_cards, monkeypatch):
    from server.routes import catalog

    # TestClient serves requests on another thread, and each thread sees its own
    # empty in-memory SQLite database. Load every card into the bounded pricing
    # cache on this thread first; the route then reads the same approved cards.
    monkeypatch.setenv("MODEL_PRICING_CACHE_SECONDS", "300")
    ModelRegistry.from_yaml()

    app = FastAPI()
    app.include_router(catalog.router)
    app.dependency_overrides[get_auth] = lambda: AuthResult(
        api_key=None, cognito_claims=None, user_id=uuid4()
    )
    return TestClient(app)


def test_model_options_endpoint_returns_full_catalog_in_database_mode(catalog_client):
    response = catalog_client.get("/v1/model-options")
    assert response.status_code == 200
    body = response.json()
    pairs = {(item["provider"], item["model"]) for item in body["models"]}

    assert {provider for provider, _ in pairs} == PROVIDERS
    assert PRODUCTION_SELECTABLE_MODELS <= pairs
    assert body["total"] == len(body["models"])

    by_pair = {(item["provider"], item["model"]): item for item in body["models"]}
    gemini = by_pair[("gemini", "gemini-3.6-flash")]
    assert gemini["input_cost_per_1m"] == 0.75
    assert gemini["output_cost_per_1m"] == 3.75
    assert gemini["cached_input_cost_per_1m"] == 0.075
    assert gemini["cache_write_cost_per_1m"] is None
    assert gemini["pricing_rule_id"] == APPROVED_LITELLM_CARDS[("gemini", "gemini-3.6-flash")]["id"]


def test_providers_endpoint_counts_models_for_every_provider(catalog_client):
    response = catalog_client.get("/v1/providers")
    assert response.status_code == 200
    counts = {
        item["provider"]: item["enabled_model_count"] for item in response.json()["providers"]
    }
    assert set(counts) >= PROVIDERS
    assert all(counts[provider] > 0 for provider in PROVIDERS)


def test_history_credit_usage_builds_registry_in_database_mode(production_rate_cards):
    from server.billing.response_credit_service import calculate_response_credit_usage

    usage = calculate_response_credit_usage(
        provider="gemini",
        model="gemini-3.6-flash",
        input_tokens=1000,
        cached_input_tokens=400,
        output_tokens=100,
        output_text="answer",
        include_research_charge=False,
    )
    assert usage.ai_credits > 0
    assert usage.credit_usage_estimated is False


# ---------------------------------------------------------------------------
# Operator preflight (scripts/preflight_database_pricing.py)
# ---------------------------------------------------------------------------

PREFLIGHT_ARGS = [
    "--min-selectable",
    "21",
    "--expect",
    "claude:claude-haiku-4-5=1/5@6884812d-54ab-49ec-9313-b64cf2c825ae",
    "--expect",
    "claude:claude-sonnet-5=2/10@7ea64bd6-380d-4d6e-9d60-9b0831eb3ae1",
    "--expect",
    "gemini:gemini-3.6-flash=0.75/3.75@f6021121-63be-40e5-b434-9cdaa12251ef",
    "--expect",
    "openai:gpt-5.6-sol=4/20@7d77e71c-8395-46cb-9746-0e3acb19f408",
    "--long-context",
    "openai:gpt-5.6-sol=272001",
]


@pytest.fixture
def preflight(production_rate_cards, monkeypatch):
    from scripts import preflight_database_pricing

    db, _ = production_rate_cards
    monkeypatch.setattr("db.engine.get_engine", lambda: db.get_bind())
    monkeypatch.setattr("db.tables.DB_SCHEMA", "main")

    def run(args):
        lines: list[str] = []
        status = preflight_database_pricing.run_preflight(args, out=lines.append, now=NOW)
        return status, "\n".join(lines)

    return run


def test_preflight_passes_and_flags_cards_ending_without_successor(preflight):
    status, output = preflight(PREFLIGHT_ARGS)

    assert status == 0, output
    assert "FAIL" not in output
    assert "all providers have selectable models" in output
    assert "selectable models match the registry configuration (21)" in output
    assert "missing rates stay missing, not $0" in output
    assert "long-context pricing starts at 272001 tokens (input 4 -> 8)" in output
    # GPT-5.6 Sol's approved card ends 2026-11-21 with no successor approved.
    assert "WARN openai:gpt-5.6-sol card 7d77e71c" in output
    # Gemini ends 2027-01-01, outside the default 45-day warning window.
    assert "WARN gemini" not in output
    assert "RESULT: PASS" in output


def test_preflight_catches_the_production_registry_failure(preflight, monkeypatch):
    monkeypatch.setattr("orchestrator.model_registry._optional_rate", _legacy_registry_hard_index)
    status, output = preflight(PREFLIGHT_ARGS)

    assert status == 1
    assert "FAIL startup readiness would fail" in output
    assert (
        "FAIL model registry failed to build with database pricing: KeyError('cache_write')"
        in output
    )
    assert "RESULT: FAIL" in output


def test_preflight_fails_on_unexpected_rates_or_card(preflight):
    status, output = preflight(
        [
            "--expect",
            "gemini:gemini-3.6-flash=1.5/7.5",
            "--expect",
            "claude:claude-sonnet-5=2/10@00000000-0000-0000-0000-000000000000",
        ]
    )
    assert status == 1
    assert "FAIL gemini:gemini-3.6-flash: rates 0.75/3.75 != 1.5/7.5" in output
    assert "FAIL claude:claude-sonnet-5: card 7ea64bd6" in output


def test_preflight_restores_the_process_pricing_mode(preflight):
    import os

    assert os.environ["MODEL_PRICING_MODE"] == "database"
    preflight([])
    assert os.environ["MODEL_PRICING_MODE"] == "database"
