"""Opt-in PostgreSQL DDL, history-protection and concurrent sync checks.

Use an isolated test database via CORTEX_TEST_PRICING_DATABASE_URL. Each test
uses a disposable schema, never the application's tables.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from config.model_pricing import load_pricing_policy
from db.pricing_repository import PricingRepository
from pricing.models import utc, validate_rates
from pricing.sources import LiteLLMPricingSource
from pricing.sync import seed_legacy, synchronize


@pytest.fixture
def postgres_pricing(monkeypatch):
    url = os.getenv("CORTEX_TEST_PRICING_DATABASE_URL")
    if not url:
        pytest.skip("Isolated PostgreSQL pricing test URL not configured")
    engine = create_engine(url)
    schema = "pricing_test_" + uuid4().hex
    sql = (
        Path("db/migrations/20261007_add_versioned_model_rate_cards.sql")
        .read_text()
        .replace("public.", f"{schema}.")
    )
    with engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'CREATE TABLE "{schema}".llm_responses (id uuid PRIMARY KEY)'))
        conn.commit()
        conn.exec_driver_sql(sql)
        conn.commit()
        conn.exec_driver_sql(sql)  # DDL reapplication must be safe.
        conn.commit()
    meta = MetaData()
    tables = {
        name: Table(name, meta, schema=schema, autoload_with=engine)
        for name in ("model_rate_cards", "pricing_catalog_observations", "pricing_sync_runs")
    }
    monkeypatch.setattr("db.pricing_repository.get_table", tables.__getitem__)
    monkeypatch.setenv("MODEL_PRICING_MODE", "legacy")
    factory = sessionmaker(bind=engine)
    try:
        yield factory, tables
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_postgres_seed_and_approved_history_is_immutable(postgres_pricing):
    factory, tables = postgres_pricing
    with factory.begin() as db:
        seeded = seed_legacy(db)
        assert seeded["added"] > 0
        assert seed_legacy(db)["added"] == 0
        card = db.execute(select(tables["model_rate_cards"])).mappings().first()
        card_id = card["id"]
    with pytest.raises(DBAPIError, match="immutable"), factory.begin() as db:
        db.execute(
            tables["model_rate_cards"]
            .update()
            .where(tables["model_rate_cards"].c.id == card_id)
            .values(input_price_per_million=99)
        )
    with pytest.raises(DBAPIError, match="cannot be deleted"), factory.begin() as db:
        db.execute(
            tables["model_rate_cards"].delete().where(tables["model_rate_cards"].c.id == card_id)
        )
    with factory.begin() as db:
        db.execute(
            tables["model_rate_cards"]
            .update()
            .where(tables["model_rate_cards"].c.id == card_id)
            .values(last_verified_at=utc())
        )


def test_postgres_seed_upgrade_closes_history_without_changing_financials(postgres_pricing):
    factory, _tables = postgres_pricing
    with factory.begin() as db:
        repo = PricingRepository(db)
        old = repo.add(
            provider="deepseek",
            model="deepseek-v4-pro",
            source="LEGACY_CORTEX",
            rates=validate_rates(
                {"tokens": {"input": "0.435", "output": "0.87", "cached_input": "0.003625"}}
            ),
            at="2026-04-24T00:00:00Z",
            reference="previous-registry",
            version="previous",
        )
        seed_legacy(db)
        cards = repo.list_cards("deepseek", "deepseek-v4-pro")
        retained = next(card for card in cards if str(card["id"]) == str(old["id"]))
        assert retained["effective_to"] == utc("2026-10-08T00:00:00Z")
        for key in ("fingerprint", "pricing_components", "source_reference", "source_version"):
            assert retained[key] == old[key]
        current = next(card for card in cards if card["effective_to"] is None)
        assert current["pricing_components"]["schedule"]["holiday_calendar"]["year"] == 2026


def test_postgres_concurrent_sync_has_one_active_version(postgres_pricing):
    factory, tables = postgres_pricing
    at = utc("2026-10-01T00:00:00Z")
    with factory.begin() as db:
        PricingRepository(db).add(
            provider="openai",
            model="gpt-4o-mini",
            source="LEGACY_CORTEX",
            rates=validate_rates({"tokens": {"input": "2", "output": "10"}}),
            at=at,
            reference="test",
            version="test",
        )
    catalog = LiteLLMPricingSource.parse(
        json.dumps(
            {
                "gpt-4o-mini": {
                    "litellm_provider": "openai",
                    "input_cost_per_token": "0.000002",
                    "output_cost_per_token": "0.00001",
                }
            }
        ).encode()
    )
    policy = replace(load_pricing_policy(), auto_activate=True)

    def sync_once():
        with factory.begin() as db:
            return synchronize(db, catalog, at=at + timedelta(days=1), policy=policy)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sync_once(), range(2)))
    assert sum(result["added"] for result in results) == 1
    assert sum(result["unchanged"] for result in results) == 1
    with factory() as db:
        rows = (
            db.execute(
                select(tables["model_rate_cards"]).where(
                    tables["model_rate_cards"].c.source == "LITELLM"
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["input_price_per_million"] == Decimal("2")
