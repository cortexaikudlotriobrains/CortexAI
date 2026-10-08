"""Financial behavior, history, sync safety, and production-model parity."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    JSON,
    MetaData,
    Numeric,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.orm import sessionmaker

from config.model_pricing import load_pricing_policy
from config.pricing import ModelPricing, RegistryPricing
from db.pricing_repository import PricingRepository
from models.unified_response import TokenUsage, UnifiedResponse
from pricing.engine import calculate
from pricing.identity import ModelIdentityMap
from pricing.models import NormalizedLLMUsage, PricingUnavailableError, utc, validate_rates
from pricing.service import clear_cache, select_card, snapshot
from pricing.sources import CortexManualPricingSource, LiteLLMPricingSource
from pricing.sync import (
    approve_candidate,
    manual_override,
    remove_override,
    review_change,
    seed_legacy,
    synchronize,
)
from utils.cost_calculator import CostCalculator

AT = utc("2026-10-01T00:00:00Z")


def rates(input_rate="2", output_rate="10", cached="0.2", write="2.5"):
    return validate_rates(
        {
            "tokens": {
                "input": input_rate,
                "output": output_rate,
                "cached_input": cached,
                "cache_write": write,
            }
        }
    )


def add(repo, values=None, **kwargs):
    return repo.add(
        provider="openai",
        model="gpt-4o-mini",
        source="LEGACY_CORTEX",
        rates=values or rates(),
        at=AT,
        reference="fixture",
        version="v1",
        **kwargs,
    )


@pytest.fixture
def pricing_db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    meta = MetaData()
    cards = Table(
        "model_rate_cards",
        meta,
        *(
            Column(name, String, primary_key=name == "id")
            for name in (
                "id",
                "provider",
                "canonical_model_id",
                "provider_model_id",
                "processing_mode",
                "source",
                "source_reference",
                "source_version",
                "fingerprint",
                "currency",
                "status",
                "review_reason",
                "override_actor",
                "override_reason",
            )
        ),
        Column("pricing_components", JSON),
        Column("input_price_per_million", Numeric(30, 15)),
        Column("output_price_per_million", Numeric(30, 15)),
        *(
            Column(name, DateTime(timezone=True))
            for name in ("effective_from", "effective_to", "created_at", "last_verified_at")
        ),
    )
    Index(
        "one_open",
        cards.c.provider,
        cards.c.canonical_model_id,
        cards.c.processing_mode,
        cards.c.source,
        unique=True,
        sqlite_where=(cards.c.status == "approved") & cards.c.effective_to.is_(None),
    )
    observations = Table(
        "pricing_catalog_observations",
        meta,
        Column("source", String, primary_key=True),
        Column("source_model_id", String, primary_key=True),
        *(Column(name, String) for name in ("provider", "canonical_model_id", "source_version")),
        Column("last_seen_at", DateTime),
        Column("missing_since", DateTime),
        Column("pricing_available", Boolean),
        Column("metadata", JSON),
    )
    runs = Table(
        "pricing_sync_runs",
        meta,
        Column("id", String, primary_key=True),
        Column("source", String),
        Column("source_version", String),
        Column("started_at", DateTime),
        Column("completed_at", DateTime),
        Column("status", String),
        Column("summary", JSON),
    )
    responses = Table(
        "llm_responses",
        meta,
        Column("id", String, primary_key=True, default=lambda: str(uuid4())),
        Column("llm_request_id", String),
        Column("text", String),
        Column("finish_reason", String),
        Column("latency_ms", Numeric),
        Column("prompt_tokens", Numeric),
        Column("completion_tokens", Numeric),
        Column("total_tokens", Numeric),
        Column("estimated_cost", Numeric),
        Column("error_type", String),
        Column("error_message", String),
        Column("rate_card_id", String),
        Column("usage_calculated_provider_cost_usd", Numeric(30, 15)),
        Column("pricing_snapshot", JSON),
    )
    meta.create_all(engine)
    tables = {table.name: table for table in (cards, observations, runs, responses)}
    monkeypatch.setattr("db.pricing_repository.get_table", tables.__getitem__)
    monkeypatch.setattr("db.tables.get_table", tables.__getitem__)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("pricing.service.SessionLocal", factory)
    monkeypatch.setattr("pricing.sync.SessionLocal", factory)
    monkeypatch.setenv("MODEL_PRICING_MODE", "legacy")
    clear_cache()
    with factory() as db:
        yield db, PricingRepository(db)
    clear_cache()
    engine.dispose()


@pytest.mark.parametrize(
    "usage,expected",
    [
        (NormalizedLLMUsage(input_tokens=1_000_000), "2"),
        (NormalizedLLMUsage(1_000_000, 500_000), "7"),
        (NormalizedLLMUsage(1_000_000, 0, 600_000, 100_000), "0.97"),
        (NormalizedLLMUsage(), "0"),
        (NormalizedLLMUsage(1, 1), "0.000012"),
        (NormalizedLLMUsage(10**12, 10**12), "12000000"),
        (NormalizedLLMUsage(0, 1_000_000, reasoning_tokens=600_000), "10"),
    ],
)
def test_decimal_calculations(pricing_db, usage, expected):
    _, repo = pricing_db
    card = add(repo)
    result = calculate(usage, snapshot(card))
    assert result.total == Decimal(expected)
    assert result.audit()["usage_calculated_provider_cost_usd"] == str(result.total)


def test_invalid_partitions_clamp_without_double_charging():
    usage = NormalizedLLMUsage(100, 50, 200, 100, 80)
    assert (usage.cached_input_tokens, usage.cache_write_tokens, usage.reasoning_tokens) == (
        100,
        0,
        50,
    )


@pytest.mark.parametrize("value", [None, -1, True, "NaN", "Infinity", "-Infinity"])
def test_bad_rates_are_rejected(value):
    with pytest.raises(ValueError):
        rates(input_rate=value)


def test_unknown_dimensions_and_missing_price_fail():
    with pytest.raises(ValueError):
        validate_rates({"tokens": {"input": 2}})
    with pytest.raises(ValueError):
        validate_rates({"tokens": {"input": 2, "output": 10}, "units": {"arbitrary_charge": 1}})
    with pytest.raises(PricingUnavailableError):
        calculate(
            NormalizedLLMUsage(units={"web_search_count": 1}),
            {"input": 1, "output": 1, "cached_input": 1, "cache_write": 1},
        )


def test_reasoning_rate_replaces_output_rate_without_double_billing(pricing_db):
    _, repo = pricing_db
    value = rates()
    value["tokens"]["reasoning"] = "12"
    result = calculate(
        NormalizedLLMUsage(output_tokens=1_000_000, reasoning_tokens=600_000),
        snapshot(add(repo, value)),
    )
    assert result.total == Decimal("11.2")
    assert result.components["output_cost"] == Decimal("4")
    assert result.components["reasoning_cost"] == Decimal("7.2")


def test_mixed_cache_write_durations_keep_distinct_rates(pricing_db):
    _, repo = pricing_db
    value = rates()
    value["tokens"]["cache_write_1h"] = "4"
    usage = NormalizedLLMUsage(
        input_tokens=1_000_000, cache_write_tokens=1_000_000, cache_write_1h_tokens=600_000
    )
    result = calculate(usage, snapshot(add(repo, value)))
    assert result.components["cache_write_cost"] == Decimal("1")
    assert result.components["cache_write_1h_cost"] == Decimal("2.4")
    assert result.total == Decimal("3.4")


def test_deepseek_source_cache_alias_is_normalized():
    result = LiteLLMPricingSource.parse(
        json.dumps(
            {
                "deepseek-chat": {
                    "litellm_provider": "deepseek",
                    "input_cost_per_token": "0.00000014",
                    "output_cost_per_token": "0.00000028",
                    "input_cost_per_token_cache_hit": "0.000000014",
                }
            }
        ).encode()
    )
    assert result.entries["deepseek-chat"]["rates"]["tokens"]["cached_input"] == "0.014"


def catalog(input_rate=Decimal("0.000002"), output_rate=Decimal("0.00001"), extra=None):
    entry = {
        "litellm_provider": "openai",
        "mode": "chat",
        "input_cost_per_token": str(input_rate),
        "output_cost_per_token": str(output_rate),
        "cache_read_input_token_cost": "0.0000002",
        "cache_creation_input_token_cost": "0.0000025",
    }
    return LiteLLMPricingSource.parse(json.dumps({"gpt-4o-mini": entry, **(extra or {})}).encode())


def auto_policy():
    return replace(load_pricing_policy(), auto_activate=True)


def test_rate_change_is_idempotent_and_preserves_history(pricing_db):
    db, repo = pricing_db
    add(repo)
    first = synchronize(db, catalog(), at=AT + timedelta(days=1), policy=auto_policy())
    assert first["added"] == 1
    same = synchronize(db, catalog(), at=AT + timedelta(days=2), policy=auto_policy())
    assert same["unchanged"] == 1
    old = select_card(repo.list_cards("openai", "gpt-4o-mini"), AT + timedelta(days=2))
    result = synchronize(
        db,
        catalog(Decimal("0.0000015"), Decimal("0.000008")),
        at=AT + timedelta(days=3),
        policy=auto_policy(),
    )
    assert result["changed"] == 1
    cards = repo.list_cards("openai", "gpt-4o-mini")
    assert len(cards) == 3
    assert select_card(cards, AT + timedelta(days=2))["id"] == old["id"]
    assert snapshot(select_card(cards, AT + timedelta(days=3)))["input"] == "1.5"
    assert utc(next(c for c in cards if c["id"] == old["id"])["effective_to"]) == AT + timedelta(
        days=3
    )
    assert select_card(cards, AT - timedelta(seconds=1)) is None


def test_manual_override_survives_sync_and_removal_preserves_history(pricing_db):
    db, repo = pricing_db
    add(repo)
    override = manual_override(
        db,
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "processing_mode": "standard",
            "rates": rates("3"),
        },
        actor="operator",
        reason="provider contract",
        at=AT + timedelta(days=1),
    )
    synchronize(db, catalog(), at=AT + timedelta(days=2), policy=auto_policy())
    assert (
        select_card(repo.list_cards("openai", "gpt-4o-mini"), AT + timedelta(days=2))["id"]
        == override["id"]
    )
    remove_override(db, "openai", "gpt-4o-mini", actor="operator", reason="contract ended")
    assert (
        select_card(repo.list_cards("openai", "gpt-4o-mini"), AT + timedelta(days=2))["id"]
        == override["id"]
    )
    assert select_card(repo.list_cards("openai", "gpt-4o-mini"), utc())["source"] == "LITELLM"


def test_suspicious_rates_quarantined_and_candidate_approval_audited(pricing_db):
    db, repo = pricing_db
    old = add(repo)
    huge = catalog(Decimal("0.0002"))
    for day in (1, 2):
        result = synchronize(db, huge, at=AT + timedelta(days=day), policy=auto_policy())
        assert result["candidate"] == 1
        assert (
            select_card(repo.list_cards("openai", "gpt-4o-mini"), AT + timedelta(days=day))["id"]
            == old["id"]
        )
    candidates = (
        db.execute(select(repo.cards).where(repo.cards.c.status == "candidate")).mappings().all()
    )
    assert len(candidates) == 1
    approve_candidate(db, candidates[0]["id"], actor="reviewer", reason="verified official price")
    active = select_card(repo.list_cards("openai", "gpt-4o-mini"), utc())
    assert active["source"] == "LITELLM"
    assert active["override_actor"] == "reviewer"


def test_default_sync_requires_review_and_dry_run_is_read_only(pricing_db):
    db, repo = pricing_db
    add(repo)
    before = db.execute(select(repo.cards)).all()
    result = synchronize(db, catalog(), dry_run=True, at=AT + timedelta(days=1))
    assert result["candidate"] == 1
    assert db.execute(select(repo.cards)).all() == before
    assert not db.execute(select(repo.observations)).all()
    assert not db.execute(select(repo.runs)).all()


def test_rate_representation_changes_do_not_create_a_new_version(pricing_db):
    db, repo = pricing_db
    add(repo)
    synchronize(db, catalog(), at=AT + timedelta(days=1), policy=auto_policy())
    result = synchronize(
        db,
        catalog(Decimal("0.0000020"), Decimal("0.0000100")),
        at=AT + timedelta(days=2),
        policy=auto_policy(),
    )
    assert result["unchanged"] == 1
    assert len(repo.list_cards("openai", "gpt-4o-mini")) == 2


def test_missing_database_rate_is_denied_before_reserving(pricing_db, monkeypatch):
    from orchestrator.model_registry import ModelRegistry
    from server.billing.credit_estimator import estimate_model_credits

    candidate = ModelRegistry.from_yaml().find_model("openai", "gpt-4o-mini")
    monkeypatch.setenv("MODEL_PRICING_MODE", "database")
    with pytest.raises(PricingUnavailableError, match="before reservation"):
        estimate_model_credits(candidate, input_text="question", max_output_tokens=100)


def test_discovery_and_missing_observations_never_enable_or_retire(pricing_db):
    db, repo = pricing_db
    extra = {
        "new-unapproved-model": {
            "litellm_provider": "openai",
            "input_cost_per_token": "0.000002",
            "output_cost_per_token": "0.00001",
        }
    }
    synchronize(db, catalog(extra=extra), at=AT)
    observation = (
        db.execute(
            select(repo.observations).where(
                repo.observations.c.source_model_id == "new-unapproved-model"
            )
        )
        .mappings()
        .one()
    )
    assert observation["canonical_model_id"] is None
    assert observation["metadata"]["cortex_enabled"] is False
    assert not repo.list_cards("openai", "new-unapproved-model")
    synchronize(db, catalog(), at=AT + timedelta(days=1))
    observation = (
        db.execute(
            select(repo.observations).where(
                repo.observations.c.source_model_id == "new-unapproved-model"
            )
        )
        .mappings()
        .one()
    )
    assert observation["missing_since"] is not None


def test_failed_activation_rolls_back_whole_card(pricing_db, monkeypatch):
    db, repo = pricing_db
    add(repo)
    synchronize(db, catalog(), at=AT + timedelta(days=1), policy=auto_policy())
    db.commit()
    old = repo.list_cards("openai", "gpt-4o-mini")
    db.rollback()

    def fail(*args, **kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(PricingRepository, "add", fail)
    with pytest.raises(RuntimeError), db.begin():
        synchronize(
            db, catalog(Decimal("0.0000015")), at=AT + timedelta(days=2), policy=auto_policy()
        )
    assert repo.list_cards("openai", "gpt-4o-mini") == old


def test_source_parser_exact_mapping_and_long_context():
    result = LiteLLMPricingSource.parse(
        json.dumps(
            {
                "anthropic/claude-sonnet-4-6": {
                    "litellm_provider": "anthropic",
                    "input_cost_per_token": "0.000003",
                    "output_cost_per_token": "0.000015",
                    "cache_creation_input_token_cost_above_1hr": "0.000006",
                    "input_cost_per_token_above_200k_tokens": "0.000006",
                }
            }
        ).encode()
    )
    entry = result.entries["anthropic/claude-sonnet-4-6"]
    assert entry["rates"]["tokens"]["input"] == "3"
    assert entry["rates"]["bands"][0]["minimum_input_tokens"] == 200001
    assert (
        ModelIdentityMap().resolve("claude", "anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"
    )
    assert ModelIdentityMap().resolve("openai", "unlisted-snapshot-2026-10-01") is None


@pytest.mark.parametrize("mode", ["legacy", "database"])
def test_mini_served_snapshot_uses_canonical_rates(pricing_db, monkeypatch, mode):
    db, _ = pricing_db
    seed_legacy(db)
    monkeypatch.setenv("MODEL_PRICING_MODE", mode)
    result = CostCalculator("openai", "gpt-5.4-mini-2026-03-17").calculate_cost(19885, 3523)
    assert result["pricing_model"] == "gpt-5.4-mini"
    assert result["cost_audit"]["usage_calculated_provider_cost_usd"] == "0.03076725"
    assert not result["pricing_unknown"]
    assert ModelIdentityMap().resolve("openai", "gpt-5.4-mini-2026-03-17") == "gpt-5.4-mini"


def scheduled_pro_rates():
    rule = ModelIdentityMap().models["deepseek", "deepseek-v4-pro"]["pricing_rules"][-1]
    return validate_rates(
        {
            "tokens": {
                key: rule[key]
                for key in ("input", "output", "cached_input", "cache_write", "cache_write_1h")
            },
            "schedule": rule["schedule"],
        }
    )


@pytest.mark.parametrize(
    "at,tier,input_rate,output_rate",
    [
        ("2026-10-08T00:59:59Z", "off_peak", "0.66", "1.98"),
        ("2026-10-08T01:00:00Z", "peak", "1.32", "3.96"),
        ("2026-10-08T03:59:59Z", "peak", "1.32", "3.96"),
        ("2026-10-08T04:00:00Z", "off_peak", "0.66", "1.98"),
        ("2026-10-08T06:00:00Z", "peak", "1.32", "3.96"),
        ("2026-10-08T10:00:00Z", "off_peak", "0.66", "1.98"),
        ("2026-10-10T02:00:00Z", "off_peak", "0.66", "1.98"),
        ("2026-10-07T02:00:00Z", "off_peak", "0.66", "1.98"),
        ("2026-10-07T21:00:00-04:00", "peak", "1.32", "3.96"),
    ],
)
def test_deepseek_schedule_boundaries_weekends_and_holidays(
    pricing_db, at, tier, input_rate, output_rate
):
    _, repo = pricing_db
    card = add(repo, scheduled_pro_rates())
    selected = snapshot(card, at=at)
    assert selected["pricing_tier"] == tier
    assert selected["input"] == input_rate
    assert selected["output"] == output_rate
    expected = Decimal(input_rate) + Decimal(output_rate)
    assert calculate(NormalizedLLMUsage(1_000_000, 1_000_000), selected).total == expected


def test_schedule_rejects_unverified_year_and_overlap(pricing_db):
    _, repo = pricing_db
    with pytest.raises(PricingUnavailableError, match="calendar"):
        snapshot(add(repo, scheduled_pro_rates()), at="2027-01-04T02:00:00Z")
    value = scheduled_pro_rates()
    value["schedule"]["peak_windows"] = [["01:00", "04:00"], ["03:00", "05:00"]]
    with pytest.raises(ValueError, match="Overlapping"):
        validate_rates(value)


def test_source_preserves_service_charges_without_model_double_billing(pricing_db):
    db, repo = pricing_db
    add(repo)
    source = catalog()
    row = {
        "litellm_provider": "openai",
        "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.00001",
        "cache_read_input_token_cost": "0.0000002",
        "cache_creation_input_token_cost": "0.0000025",
        "search_context_cost_per_query": {
            "search_context_size_low": "0.01",
            "search_context_size_medium": "0.01",
            "search_context_size_high": "0.01",
        },
    }
    source = LiteLLMPricingSource.parse(json.dumps({"gpt-4o-mini": row}).encode())
    entry = source.entries["gpt-4o-mini"]
    assert entry["rates"]["units"] == {}
    result = synchronize(db, source, at=AT, policy=auto_policy())
    assert result["candidate"] == 1
    assert result["changes"][0]["reason"] == "separate_service_charges_require_review"
    observed = db.execute(select(repo.observations)).mappings().one()
    assert observed["metadata"]["separate_charges"] == entry["separate_charges"]


@pytest.mark.parametrize("image_rate,accepted", [("0.000002", True), ("0.000004", False)])
def test_image_token_source_requires_equal_aggregate_input_rate(image_rate, accepted):
    row = {
        "litellm_provider": "xai",
        "input_cost_per_token": "0.000002",
        "output_cost_per_token": "0.000006",
        "input_cost_per_image_token": image_rate,
    }
    entry = LiteLLMPricingSource.parse(json.dumps({"xai/grok-4.5": row}).encode()).entries[
        "xai/grok-4.5"
    ]
    assert bool(entry["rates"]) is accepted
    if accepted:
        assert entry["included_input_dimensions"] == ["image"]
        assert entry["rates"]["units"] == {}


def test_seed_upgrade_closes_old_interval_and_rejects_peak_only_candidate(pricing_db):
    db, repo = pricing_db
    old = repo.add(
        provider="deepseek",
        model="deepseek-v4-pro",
        source="LEGACY_CORTEX",
        rates=validate_rates(
            {"tokens": {"input": "0.435", "output": "0.87", "cached_input": "0.003625"}}
        ),
        at="2026-04-24T00:00:00Z",
        reference="old-registry",
        version="old",
    )
    seed_legacy(db)
    cards = repo.list_cards("deepseek", "deepseek-v4-pro")
    retained = next(card for card in cards if str(card["id"]) == str(old["id"]))
    assert retained["effective_to"] == utc("2026-10-08T00:00:00Z").replace(tzinfo=None)
    assert retained["fingerprint"] == old["fingerprint"]
    incoming = LiteLLMPricingSource.parse(
        json.dumps(
            {
                "deepseek-v4-pro": {
                    "litellm_provider": "deepseek",
                    "input_cost_per_token": "0.00000132",
                    "output_cost_per_token": "0.00000396",
                }
            }
        ).encode()
    )
    result = synchronize(db, incoming, at="2026-10-08T02:00:00Z", policy=auto_policy())
    assert result["invalid"] == 1
    assert result["changes"][0]["reason"] == "incomplete_billing_schedule"
    candidate = repo.add(
        provider="deepseek",
        model="deepseek-v4-pro",
        source="LITELLM",
        rates=incoming.entries["deepseek-v4-pro"]["rates"],
        at=AT,
        reference="source",
        version="v1",
        status="candidate",
    )
    with pytest.raises(ValueError, match="omits"):
        approve_candidate(db, candidate["id"], actor="test", reason="test")


@pytest.mark.parametrize("payload", [b"[]", b"{}", b'{"x":NaN}', b'{"x":{},"x":{}}', b"malformed"])
def test_malformed_source_fails_safely(payload):
    with pytest.raises((ValueError, TypeError)):
        LiteLLMPricingSource.parse(payload)


def test_null_zero_unknown_charge_and_ambiguous_alias_rejected(pricing_db):
    db, repo = pricing_db
    add(repo)
    for invalid in (None, 0, -1):
        result = catalog(input_rate=invalid)
        assert result.entries["gpt-4o-mini"]["rates"] is None
        assert synchronize(db, result, at=AT + timedelta(days=1))["invalid"] == 1
    valid = catalog().entries["gpt-4o-mini"]
    conflicting = catalog(Decimal("0.000003")).entries["gpt-4o-mini"]
    result = replace(catalog(), entries={"gpt-4o-mini": valid, "openai/gpt-4o-mini": conflicting})
    assert synchronize(db, result, at=AT + timedelta(days=1), policy=auto_policy())["invalid"] == 1


@pytest.mark.parametrize(
    "timestamp",
    ["2026-10-07T00:00:00Z", "2026-10-08T02:00:00Z", "2026-10-08T04:00:00Z"],
)
def test_all_enabled_models_seed_parity_and_idempotency(pricing_db, timestamp):
    db, repo = pricing_db
    seeded = seed_legacy(db)
    assert seeded["added"] > 0
    again = seed_legacy(db)
    assert again == {"added": 0, "unchanged": seeded["added"]}
    now = utc(timestamp)
    for (provider, model), record in ModelIdentityMap().models.items():
        if not record.get("enabled", True):
            continue
        identity = RegistryPricing.resolve_model_identity(provider, model, at=now)
        for prompt, output, cached, writes in ((1000, 500, 0, 0), (500000, 10000, 100000, 30000)):
            for ttl in ("5m", "1h"):
                legacy = RegistryPricing.get_pricing_snapshot(
                    provider, model, at=now, prompt_tokens=prompt, cache_write_ttl=ttl
                )
                selected = snapshot(
                    select_card(repo.list_cards(provider, identity["pricing_model"]), now),
                    at=now,
                    prompt_tokens=prompt,
                    cache_write_ttl=ttl,
                )
                old = (
                    (prompt - cached - writes) * legacy["input"]
                    + cached * legacy["cached_input"]
                    + writes * legacy["cache_write"]
                    + output * legacy["output"]
                ) / 1000000
                new = calculate(NormalizedLLMUsage(prompt, output, cached, writes), selected)
                assert float(new.total) == pytest.approx(old, abs=1e-12), (provider, model, ttl)


def test_database_facade_persistence_and_unknown_never_zero(pricing_db, monkeypatch):
    from api.base_client import BaseAIClient
    from db.repository import create_llm_response

    db, repo = pricing_db
    add(repo)
    db.commit()
    monkeypatch.setenv("MODEL_PRICING_MODE", "database")
    cost = CostCalculator("openai", "gpt-4o-mini").calculate_cost(1000, 500, request_at=AT)
    client = object.__new__(
        type(
            "TestClient",
            (BaseAIClient,),
            {
                "get_completion": lambda *a, **kw: None,
                "list_available_models": lambda *a, **kw: None,
            },
        )
    )
    client.requested_model_name = "gpt-4o-mini"
    client.model_name = "gpt-4o-mini"
    client.model_identity = {}
    audit = client._response_audit_fields(served_model="gpt-4o-mini", cost=cost)
    response = UnifiedResponse(
        "req",
        "answer",
        "openai",
        "gpt-4o-mini",
        1,
        TokenUsage(1000, 500),
        cost["total_cost"],
        **audit,
    )
    create_llm_response(db, "request-row", response)
    stored = (
        db.execute(
            select(__import__("db.tables", fromlist=["get_table"]).get_table("llm_responses"))
        )
        .mappings()
        .one()
    )
    assert stored["rate_card_id"] == audit["pricing_snapshot"]["rate_card_id"]
    assert stored["pricing_snapshot"]["cost_components_usd"]["input_cost"] == "0.002"
    assert stored["usage_calculated_provider_cost_usd"] == Decimal("0.007")
    with pytest.raises(PricingUnavailableError):
        CostCalculator("openai", "unknown-model").calculate_cost(1, 1, request_at=AT)


def test_database_failure_does_not_fall_back_to_yaml(pricing_db, monkeypatch):
    monkeypatch.setenv("MODEL_PRICING_MODE", "database")

    def unavailable(*a, **kw):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("pricing.service.SessionLocal", unavailable)
    with pytest.raises(RuntimeError, match="database unavailable"):
        ModelPricing.get_pricing_snapshot("openai", "gpt-4o-mini")


def test_cached_cards_refresh_after_bounded_lifetime(pricing_db, monkeypatch):
    from pricing.service import get_snapshot

    db, repo = pricing_db
    initial = add(repo)
    db.commit()
    clock = [1000.0]
    monkeypatch.setattr("pricing.service.monotonic", lambda: clock[0])
    monkeypatch.setenv("MODEL_PRICING_CACHE_SECONDS", "60")
    at = AT + timedelta(days=2)
    cached = get_snapshot("openai", "gpt-4o-mini", at=at)
    cached["input"] = "999"
    override = manual_override(
        db,
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "processing_mode": "standard",
            "rates": rates(input_rate="3"),
        },
        actor="operator",
        reason="Verified contract",
        at=AT + timedelta(days=1),
    )
    db.commit()
    clock[0] += 59
    retained = get_snapshot("openai", "gpt-4o-mini", at=at)
    assert retained["rate_card_id"] == str(initial["id"])
    assert retained["input"] == "2"
    clock[0] += 2
    refreshed = get_snapshot("openai", "gpt-4o-mini", at=at)
    assert refreshed["rate_card_id"] == str(override["id"])
    assert refreshed["input"] == "3"


def test_stale_approved_pricing_retained_with_warning(pricing_db, monkeypatch, caplog):
    from pricing.service import get_snapshot

    db, repo = pricing_db
    approved = add(repo)
    db.commit()
    monkeypatch.setenv("MODEL_PRICING_STALE_HOURS", "48")
    monkeypatch.setattr(
        "pricing.service.utc", lambda value=None: utc(value) if value else AT + timedelta(hours=49)
    )
    selected = get_snapshot("openai", "gpt-4o-mini", at=AT)
    assert selected["rate_card_id"] == str(approved["id"])
    assert selected["pricing_stale"] is True
    assert selected["input"] == "2"
    assert "pricing_stale" in caplog.text


def test_external_source_failure_retains_approved_cards(pricing_db):
    from pricing.sync import run_sync

    db, repo = pricing_db
    initial = add(repo)
    db.commit()

    class BrokenSource:
        def fetch(self):
            raise RuntimeError("source unavailable")

    with pytest.raises(RuntimeError, match="source unavailable"):
        run_sync(BrokenSource())
    assert repo.list_cards("openai", "gpt-4o-mini")[0]["id"] == initial["id"]
    assert db.execute(select(repo.runs)).mappings().one()["status"] == "failed"


def test_manual_source_validation():
    data = CortexManualPricingSource.parse(
        json.dumps(
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "processing_mode": "standard",
                "rates": rates(),
            }
        )
    )
    assert data["rates"]["tokens"]["input"] == "2"
    with pytest.raises(ValueError):
        CortexManualPricingSource.parse('{"provider":"unknown"}')


def test_currency_and_context_changes_require_review():
    long = rates()
    long["bands"] = [{"minimum_input_tokens": 200001, "tokens": {"input": "4"}}]
    assert review_change(rates(), long, auto_policy()) == "long_context_structure_changed"
    with pytest.raises(ValueError):
        calculate(NormalizedLLMUsage(), {"currency": "EUR"})


def test_migration_history_protection_contract():
    sql = Path("db/migrations/20261007_add_versioned_model_rate_cards.sql").read_text()
    assert "uq_model_rate_cards_open" in sql
    assert "pg_advisory_xact_lock" not in sql  # Writers take the transaction lock.
    assert "protect_approved_rate_card" in sql
    assert "UPDATE public.llm_responses" not in sql
