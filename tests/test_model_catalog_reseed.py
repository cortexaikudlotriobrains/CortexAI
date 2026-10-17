"""A model update must re-seed cleanly over the price cards seeded before it.

Every change to config/model_registry.yaml makes the production deploy run
`scripts/sync_model_pricing.py --seed-legacy` over cards seeded from the previous
registry, and the deploy stops if a retained rule's rates differ ("Legacy seed
differs from retained history"). These tests replay that: seed a base registry
into an empty pricing store, then seed the current registry the way the deploy
does (a dry run, the write, and a repeat that must add nothing).

The Model catalog workflow sets MODEL_CATALOG_BASE_REGISTRY to develop's registry
before it applies a report, so a model update that would break the deploy never
becomes a pull request. The other tests check the replay itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from pricing import sync
from tests.test_model_rate_cards import pricing_db as _pricing_db

pricing_db = _pricing_db

REGISTRY = Path(__file__).resolve().parents[1] / "config" / "model_registry.yaml"
BASE_REGISTRY = os.environ.get("MODEL_CATALOG_BASE_REGISTRY", "").strip()
# Set where the replay is a gate (CI on pull requests, the model-catalog apply job):
# a missing base registry then fails instead of silently skipping the replay.
REQUIRE_REPLAY = os.environ.get("MODEL_CATALOG_REQUIRE_REPLAY", "").strip() == "1"


def _reseed(db, monkeypatch, base: Path) -> dict[str, dict[str, int]]:
    current_identities = sync.ModelIdentityMap
    monkeypatch.setattr(sync, "ModelIdentityMap", lambda *_a, **_k: current_identities(base))
    seeded = sync.seed_legacy(db)
    monkeypatch.setattr(sync, "ModelIdentityMap", current_identities)
    dry_run = sync.seed_legacy(db, dry_run=True)
    written = sync.seed_legacy(db)
    repeat = sync.seed_legacy(db)
    return {"seeded": seeded, "dry_run": dry_run, "written": written, "repeat": repeat}


def test_current_registry_reseeds_over_the_base_registry(pricing_db, monkeypatch):
    if not BASE_REGISTRY:
        if REQUIRE_REPLAY:
            pytest.fail("MODEL_CATALOG_REQUIRE_REPLAY=1 but MODEL_CATALOG_BASE_REGISTRY is not set")
        pytest.skip("MODEL_CATALOG_BASE_REGISTRY names the registry before the change")
    base = Path(BASE_REGISTRY)
    assert base.is_file() and base.stat().st_size > 0, f"base registry {base} is missing or empty"
    db, _ = pricing_db
    result = _reseed(db, monkeypatch, base)
    print(f"reseed summary: {result}")
    assert result["written"] == result["dry_run"]
    assert result["repeat"]["added"] == 0


def test_unchanged_registry_reseeds_without_adding_cards(pricing_db, monkeypatch):
    db, _ = pricing_db
    result = _reseed(db, monkeypatch, REGISTRY)
    assert result["seeded"]["added"] > 0
    assert result["written"]["added"] == 0
    assert result["repeat"]["added"] == 0


def test_a_changed_rate_on_a_seeded_rule_is_caught(pricing_db, monkeypatch, tmp_path):
    """Rewriting an existing rule's price would stop the production deploy."""

    db, _ = pricing_db
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    model = next(
        entry for entry in data["providers"]["openai"]["models"] if entry["name"] == "gpt-4o-mini"
    )
    model["pricing_rules"][0]["input"] = float(model["pricing_rules"][0]["input"]) + 0.05
    base = tmp_path / "base_registry.yaml"
    base.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Legacy seed differs from retained history"):
        _reseed(db, monkeypatch, base)
