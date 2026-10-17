"""Tests for the model catalog tool behind `python scripts/models.py` and its workflow."""

from __future__ import annotations

import json
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tools.model_catalog import apply as apply_module
from tools.model_catalog import registry_text as rt
from tools.model_catalog.checks import model_index, run_checks, write_lock
from tools.model_catalog.ladder import load_ladder
from tools.model_catalog.report import (
    build_report,
    display_name,
    family,
    fetch_catalog,
    parse_issue,
    render_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 14)


def _row(provider: str, input_rate: str, output_rate: str, **extra) -> dict:
    return {
        "litellm_provider": provider,
        "input_cost_per_token": input_rate,
        "output_cost_per_token": output_rate,
        "mode": "chat",
        **extra,
    }


@pytest.fixture
def catalog(tmp_path):
    rows = {
        # Known model, so the new model can inherit its context-window convention.
        "gpt-6-luna": _row("openai", "0.0000001", "0.0000005", max_input_tokens=922000),
        # A cheaper successor of GPT-6 Luna.
        "gpt-7-luna": _row(
            "openai",
            "0.00000008",
            "0.0000004",
            cache_read_input_token_cost="0.000000008",
            max_input_tokens=922000,
            supports_none_reasoning_effort=True,
        ),
        # Never proposed: dated snapshot, beta, and a dismissed older Grok.
        "gpt-7-luna-2027-01-15": _row("openai", "0.00000008", "0.0000004"),
        "gpt-7-luna-beta": _row("openai", "0.00000008", "0.0000004"),
        "xai/grok-4.20": _row("xai", "0.00000125", "0.0000025"),
        # Above the top credit step (P2 = 10/50).
        "gpt-9-ultra": _row("openai", "0.0001", "0.0004"),
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return fetch_catalog(str(path))


# --------------------------------------------------------------------------- repository


def test_repository_registry_passes_the_model_catalog_checks():
    result = run_checks(check_frontend=True)
    assert result.errors == []


def test_every_registry_id_is_locked():
    """IDs are never removed or reused: a hidden or retired model keeps its ID."""

    registry = yaml.safe_load((ROOT / "config" / "model_registry.yaml").read_text())
    lock = ROOT / "config" / "known_model_ids.txt"
    before = lock.read_text(encoding="utf-8")
    try:
        assert write_lock(registry) == []
    finally:
        lock.write_text(before, encoding="utf-8")


# --------------------------------------------------------------------------- ladder


@pytest.mark.parametrize(
    "price,step",
    [
        (("0.1", "0.5"), "E1"),
        (("0.3", "2.5"), "S1"),
        (("0.75", "3.75"), "S1"),
        (("2", "6"), "S2"),
        (("2", "10"), "A1"),
        (("4", "20"), "P1"),
        (("10", "50"), "P2"),
    ],
)
def test_ladder_picks_the_cheapest_covering_step(price, step):
    found = load_ladder().lowest_covering(*price)
    assert found is not None and found.id == step
    assert found.input >= Decimal(price[0]) and found.output >= Decimal(price[1])


def test_ladder_has_no_step_above_the_top_price():
    assert load_ladder().lowest_covering("100", "400") is None


def test_ladder_steps_never_decrease(tmp_path):
    bad = tmp_path / "ladder.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "price_source": "litellm_current",
                "steps": [
                    {"id": "A", "input": 2, "output": 8, "billing_class": "standard", "label": "x"},
                    {"id": "B", "input": 1, "output": 4, "billing_class": "standard", "label": "x"},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must not decrease"):
        load_ladder(bad)


# --------------------------------------------------------------------------- naming


@pytest.mark.parametrize(
    "provider,name,label",
    [
        ("openai", "gpt-6.1-sol", "GPT-6.1 Sol"),
        ("openai", "gpt-6-luna", "GPT-6 Luna"),
        ("claude", "claude-opus-5-5", "Claude Opus 5.5"),
        ("gemini", "gemini-3.8-flash", "Gemini 3.8 Flash"),
        ("deepseek", "deepseek-flash", "DeepSeek Flash"),
    ],
)
def test_display_names(provider, name, label):
    assert display_name(provider, name) == label


def test_family_groups_versions():
    assert family("claude-opus-5-5") == ("claude-opus-#", (5, 5))
    assert family("gpt-6.1-sol") == ("gpt-#-sol", (6, 1))


# --------------------------------------------------------------------------- registry text


SAMPLE = """\
providers:
  demo:
    models:
      - name: "demo-1"
        display_name: "Demo 1"
        selectable: true
        aliases: ["demo-latest"]
        lifecycle:
          status: "ACTIVE"
        pricing_rules:
          - id: "demo-1-standard"
            input: 1.0
            output: 2.0

ui_defaults:
  ask_by_plan:
    free: "demo:demo-1"
"""


def test_registry_text_edits_keep_valid_yaml():
    entry = rt.render_entry(
        {
            "name": "demo-2",
            "display_name": "Demo 2",
            "tags": ["fast", "cheap"],
            "lifecycle": {"status": "ACTIVE"},
            "pricing_rules": [{"id": "demo-2-standard", "input": 0.5, "output": 1.0}],
        }
    )
    text = rt.insert_entry(SAMPLE, "demo", entry, before="demo-1")
    text = rt.hide_entry(text, "demo", "demo-1", replacement="demo-2", reason="Replaced.")
    text = rt.remove_field(text, "demo", "demo-1", "aliases")
    text, moved = rt.replace_quoted_in_section(text, "ui_defaults", "demo:demo-1", "demo:demo-2")
    assert moved == 1

    data = yaml.safe_load(text)
    models = data["providers"]["demo"]["models"]
    assert [model["name"] for model in models] == ["demo-2", "demo-1"]
    assert models[0]["tags"] == ["fast", "cheap"]
    assert models[1]["selectable"] is False
    assert models[1]["lifecycle"] == {
        "status": "ACTIVE",
        "replacement_model": "demo-2",
        "migration_reason": "Replaced.",
    }
    assert "aliases" not in models[1]
    assert data["ui_defaults"]["ask_by_plan"]["free"] == "demo:demo-2"

    text = rt.update_lifecycle(text, "demo", "demo-1", migration_reason=None, status="DEPRECATED")
    assert yaml.safe_load(text)["providers"]["demo"]["models"][1]["lifecycle"] == {
        "status": "DEPRECATED",
        "replacement_model": "demo-2",
    }


def test_registry_text_rejects_unknown_models():
    with pytest.raises(rt.RegistryEditError):
        rt.set_field(SAMPLE, "demo", "missing", "selectable", False)


# --------------------------------------------------------------------------- report


def test_report_proposes_only_newer_models_on_a_ladder_step(catalog):
    report = build_report(catalog, today=TODAY)
    proposals = {item["id"]: item for item in report["proposals"]}

    assert set(proposals) == {"openai:gpt-7-luna"}
    luna = proposals["openai:gpt-7-luna"]
    assert luna["replaces"] == "openai:gpt-6-luna"
    assert luna["credits"] == "0.25/1"
    assert luna["billing_class"] == "economical"
    assert luna["plans"] == ["Free", "Plus", "Pro"]
    assert luna["change_per_turn"] == "\N{MINUS SIGN}20%"
    # Inherits the predecessor's wording and its whole-window context convention.
    assert (
        luna["description"] == "Lowest-cost GPT-7 model for high-volume and latency-sensitive work."
    )
    assert luna["context_limit"] == 1_050_000

    assert [item["id"] for item in report["above_ladder"]] == ["openai:gpt-9-ultra"]
    assert [item["id"] for item in report["dismissed"]] == ["grok:grok-4.20"]
    assert report["errors"] == []


def test_issue_round_trip_returns_ticked_proposals(catalog):
    body = render_markdown(build_report(catalog, today=TODAY))
    assert "- [ ] `openai:gpt-7-luna`" in body

    proposals, ticked = parse_issue(body)
    assert [item["id"] for item in proposals] == ["openai:gpt-7-luna"]
    assert ticked == set()

    edited = body.replace("- [ ] `openai:gpt-7-luna`", "- [x] `openai:gpt-7-luna`")
    _proposals, ticked = parse_issue(edited)
    assert ticked == {"openai:gpt-7-luna"}


# --------------------------------------------------------------------------- apply


@pytest.fixture
def config_copy(tmp_path, monkeypatch):
    registry = tmp_path / "model_registry.yaml"
    providers = tmp_path / "providers.yaml"
    lock = tmp_path / "known_model_ids.txt"
    shutil.copy(ROOT / "config" / "model_registry.yaml", registry)
    shutil.copy(ROOT / "config" / "providers.yaml", providers)
    shutil.copy(ROOT / "config" / "known_model_ids.txt", lock)
    frontend: dict = {}
    monkeypatch.setattr(apply_module, "REGISTRY_PATH", registry)
    monkeypatch.setattr(apply_module, "PROVIDERS_PATH", providers)
    monkeypatch.setattr(
        apply_module,
        "load_registry",
        lambda: yaml.safe_load(registry.read_text(encoding="utf-8")),
    )
    monkeypatch.setattr(apply_module, "write_lock", lambda data: write_lock(data, lock))
    monkeypatch.setattr(
        apply_module,
        "write_frontend_defaults",
        lambda data: frontend.setdefault("registry", data) is data,
    )
    return registry, providers, lock, frontend


def test_apply_adds_the_model_hides_its_predecessor_and_moves_defaults(catalog, config_copy):
    registry_path, providers_path, lock_path, frontend = config_copy
    proposals = build_report(catalog, today=TODAY)["proposals"]

    log = apply_module.apply_proposals(proposals, {"openai:gpt-7-luna"}, today=TODAY)

    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    index = model_index(registry)
    new = index[("openai", "gpt-7-luna")]
    old = index[("openai", "gpt-6-luna")]
    names = [model["name"] for model in registry["providers"]["openai"]["models"]]
    assert names.index("gpt-7-luna") == names.index("gpt-6-luna") - 1

    assert (new["input_credit_multiplier"], new["output_credit_multiplier"]) == (0.25, 1.0)
    assert new["billing_class"] == "economical"
    assert new["credit_usage_label"] == "Very low"
    assert new["selectable"] is True and new["lifecycle"]["status"] == "ACTIVE"
    rule = new["pricing_rules"][0]
    assert "effective_from" not in rule  # applies from the start, like most registry rules
    assert (rule["input"], rule["output"], rule["cached_input"]) == (0.08, 0.4, 0.008)
    assert new["source_verified_at"] == TODAY.isoformat()

    assert old["selectable"] is False
    assert old["lifecycle"]["status"] == "ACTIVE"
    assert old["lifecycle"]["replacement_model"] == "gpt-7-luna"

    assert registry["ui_defaults"]["ask_by_plan"]["free"] == "openai:gpt-7-luna"
    assert "openai:gpt-7-luna" in registry["ui_defaults"]["compare"]
    providers = yaml.safe_load(providers_path.read_text(encoding="utf-8"))
    openai = next(item for item in providers["providers"] if item["id"] == "openai")
    assert openai["compare_default_model"] == "gpt-7-luna"

    assert "openai:gpt-7-luna" in lock_path.read_text(encoding="utf-8").split()
    assert frontend["registry"] is not None
    assert any(line.startswith("Added openai:gpt-7-luna") for line in log)


def test_apply_skips_unticked_and_refuses_duplicates(catalog, config_copy):
    registry_path, *_ = config_copy
    proposals = build_report(catalog, today=TODAY)["proposals"]
    before = registry_path.read_text(encoding="utf-8")

    apply_module.apply_proposals(proposals, set(), today=TODAY)
    assert registry_path.read_text(encoding="utf-8") == before

    apply_module.apply_proposals(proposals, None, today=TODAY)
    with pytest.raises(ValueError, match="already in the registry"):
        apply_module.apply_proposals(proposals, None, today=TODAY)


def test_apply_writes_two_dated_rules_for_a_promotion(catalog, config_copy):
    registry_path, *_ = config_copy
    proposal = dict(build_report(catalog, today=TODAY)["proposals"][0])
    proposal["promo"] = {
        "until": "2027-03-01T00:00:00Z",
        "regular": {"input": 0.16, "output": 0.8, "cached_input": 0.016},
        "note": "Launch price through February 2027.",
    }

    apply_module.apply_proposals([proposal], None, today=TODAY)

    new = model_index(yaml.safe_load(registry_path.read_text(encoding="utf-8")))[
        ("openai", "gpt-7-luna")
    ]
    launch, regular = new["pricing_rules"]
    assert launch["effective_until"] == "2027-03-01T00:00:00Z"
    assert regular["effective_from"] == "2027-03-01T00:00:00Z"
    assert (regular["input"], regular["output"]) == (0.16, 0.8)
    assert new["pricing_review"]["requires_expiry"] is True


@pytest.mark.parametrize(
    "edit,message",
    [
        ({"id": 'openai:gpt-7-luna"\n  injected: true'}, "Invalid model key"),
        ({"surprise": 1}, "unknown proposal fields"),
        ({"price": {"input": 0.08, "output": 0.4, "bonus": 1}}, "unknown price rates"),
        ({"price": {"input": -1, "output": 0.4}}, "nonnegative"),
        ({"tier": "T9"}, "tier"),
        ({"aliases": ["ok", "Not Ok"]}, "Invalid alias"),
    ],
)
def test_apply_rejects_unsafe_issue_edits(catalog, config_copy, edit, message):
    registry_path, *_ = config_copy
    before = registry_path.read_text(encoding="utf-8")
    proposal = {**build_report(catalog, today=TODAY)["proposals"][0], **edit}

    with pytest.raises(ValueError, match=message):
        apply_module.apply_proposals([proposal], None, today=TODAY)
    assert registry_path.read_text(encoding="utf-8") == before


def test_apply_refuses_ticks_without_a_proposal(catalog, config_copy):
    proposals = build_report(catalog, today=TODAY)["proposals"]
    with pytest.raises(ValueError, match="no proposal"):
        apply_module.apply_proposals(proposals, {"openai:gpt-8-luna"}, today=TODAY)


def test_fingerprint_changes_only_with_the_findings(catalog, tmp_path):
    first = build_report(catalog, today=TODAY)
    later = build_report(catalog, today=date(2026, 10, 21))
    assert first["fingerprint"] == later["fingerprint"]
    assert f"model-catalog:fingerprint={first['fingerprint']}" in render_markdown(first)

    rows = json.loads(json.dumps(catalog.raw))
    rows["gpt-7-luna"]["output_cost_per_token"] = "0.0000003"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    assert (
        build_report(fetch_catalog(str(path)), today=TODAY)["fingerprint"] != first["fingerprint"]
    )


# --------------------------------------------------------------------------- successor fields


def _successor(catalog, **changes):
    proposal = dict(build_report(catalog, today=TODAY)["proposals"][0])
    proposal.update(changes)
    return proposal


def test_vision_successor_keeps_the_predecessors_attachment_settings(catalog, config_copy):
    """A DeepSeek Flash successor must not fall back to DeepSeek's no-attachment defaults."""

    registry_path, *_ = config_copy
    proposal = _successor(
        catalog,
        id="deepseek:deepseek-flash-2",
        replaces="deepseek:deepseek-flash",
        based_on="deepseek:deepseek-flash",
        reasoning=None,
        display_name="DeepSeek Flash 2",
        flags={"supports_vision": True},
        attachments=None,
    )
    apply_module.apply_proposals([proposal], None, today=TODAY)

    new = model_index(yaml.safe_load(registry_path.read_text(encoding="utf-8")))[
        ("deepseek", "deepseek-flash-2")
    ]
    assert new["supports_image_input"] is True
    assert new["supported_attachment_mime_types"] == [
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    ]
    assert new["max_attachment_bytes"] == 20971520
    assert new["max_attachments_per_request"] == 5


def test_successor_without_vision_drops_image_types_only():
    pred = {
        "supports_image_input": True,
        "supported_attachment_mime_types": ["image/png", "application/pdf"],
        "max_attachment_bytes": 1000,
        "max_attachments_per_request": 3,
    }
    settings, notes = apply_module.derive_attachments(pred, {}, {"supports_vision": False})
    assert settings == {
        "supports_image_input": False,
        "supported_attachment_mime_types": ["application/pdf"],
        "max_attachment_bytes": 1000,
        "max_attachments_per_request": 3,
    }
    assert notes == []


def test_vision_flag_without_predecessor_support_is_a_review_note_not_a_guess():
    defaults = {
        "supports_image_input": False,
        "supported_attachment_mime_types": [],
        "max_attachment_bytes": None,
        "max_attachments_per_request": 0,
    }
    settings, notes = apply_module.derive_attachments(None, defaults, {"supports_vision": True})
    assert settings["supports_image_input"] is False
    assert settings["max_attachments_per_request"] == 0
    assert notes and "vision-capable" in notes[0]


def test_inherited_default_attachments_are_not_written_to_the_entry(catalog, config_copy):
    registry_path, *_ = config_copy
    apply_module.apply_proposals(build_report(catalog, today=TODAY)["proposals"], None, today=TODAY)
    new = model_index(yaml.safe_load(registry_path.read_text(encoding="utf-8")))[
        ("openai", "gpt-7-luna")
    ]
    assert "supports_image_input" not in new
    assert "supported_attachment_mime_types" not in new


def _catalog_with(tmp_path, **rows):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return fetch_catalog(str(path))


def test_new_models_own_output_limit_wins_over_the_predecessors(tmp_path):
    catalog = _catalog_with(
        tmp_path,
        **{
            "gpt-6-luna": _row("openai", "0.0000001", "0.0000005", max_input_tokens=922000),
            "gpt-7-luna": _row(
                "openai",
                "0.00000008",
                "0.0000004",
                max_input_tokens=922000,
                max_output_tokens=32768,
            ),
        },
    )
    proposal = build_report(catalog, today=TODAY)["proposals"][0]
    assert proposal["max_output_tokens"] == 32768
    assert any("32768" in note and "128000" in note for note in proposal["notes"])
    assert "Check: Output limit 32768" in render_markdown(build_report(catalog, today=TODAY))


def test_output_limit_equal_to_the_context_window_is_not_trusted(tmp_path):
    catalog = _catalog_with(
        tmp_path,
        **{
            "xai/grok-4.7": _row("xai", "0.000002", "0.000006", max_input_tokens=500000),
            "xai/grok-4.9": _row(
                "xai",
                "0.000002",
                "0.000006",
                max_input_tokens=500000,
                max_output_tokens=500000,
            ),
        },
    )
    proposal = build_report(catalog, today=TODAY)["proposals"][0]
    assert proposal["id"] == "grok:grok-4.9"
    assert proposal["max_output_tokens"] == 128000
    assert any("equals the context window" in note for note in proposal["notes"])


def test_a_new_warning_changes_the_fingerprint(catalog, monkeypatch):
    from tools.model_catalog import report as report_module
    from tools.model_catalog.checks import CheckResult

    before = build_report(catalog, today=TODAY)["fingerprint"]
    real = report_module.run_checks

    def with_warning(**kwargs):
        result = real(**kwargs)
        return CheckResult(
            errors=result.errors,
            warnings=[*result.warnings, "gemini:gemini-4-argon: launch price ends soon"],
        )

    monkeypatch.setattr(report_module, "run_checks", with_warning)
    assert build_report(catalog, today=TODAY)["fingerprint"] != before


def test_dearer_successor_is_added_alongside_not_as_a_replacement(tmp_path):
    """GPT-5.5 (premium) must not be proposed as GPT-5.4's (advanced) replacement."""

    catalog = _catalog_with(
        tmp_path,
        **{
            "gpt-6-luna": _row("openai", "0.0000001", "0.0000005", max_input_tokens=922000),
            "gpt-7-luna": _row("openai", "0.000003", "0.000014", max_input_tokens=922000),
        },
    )
    report = build_report(catalog, today=TODAY)
    proposal = report["proposals"][0]
    assert proposal["id"] == "openai:gpt-7-luna"
    assert proposal["billing_class"] == "advanced"
    assert proposal["replaces"] is None
    assert proposal["description"] == "GPT-7 Luna model."
    assert any("Added alongside gpt-6-luna" in note for note in proposal["notes"])
    assert proposal["based_on"] == "openai:gpt-6-luna"
    assert "replaces nothing (added alongside openai:gpt-6-luna)" in render_markdown(report)


def _load_applied_registry(registry_path, monkeypatch):
    """Build the runtime ModelRegistry from an edited registry copy (legacy pricing)."""

    from config import pricing as config_pricing
    from orchestrator.model_registry import ModelRegistry

    monkeypatch.setenv("MODEL_PRICING_MODE", "legacy")
    monkeypatch.setattr(config_pricing, "_DEFAULT_CATALOG_PATH", registry_path)
    config_pricing._load_catalog.cache_clear()
    try:
        return ModelRegistry.from_yaml(str(registry_path))
    finally:
        config_pricing._load_catalog.cache_clear()


@pytest.mark.parametrize("price", [("0.00000008", "0.0000004"), ("0.000003", "0.000014")])
def test_report_apply_load_keeps_reasoning_for_replacements_and_additions(
    tmp_path, config_copy, monkeypatch, price
):
    """Full path for a cheaper replacement and for a dearer model added alongside."""

    from orchestrator.reasoning_capabilities import normalized_reasoning_levels
    from orchestrator.routing_types import RoutingConstraints, Tier

    registry_path, *_ = config_copy
    catalog = _catalog_with(
        tmp_path,
        **{
            "gpt-6-luna": _row("openai", "0.0000001", "0.0000005", max_input_tokens=922000),
            "gpt-7-luna": _row(
                "openai",
                *price,
                max_input_tokens=922000,
                supports_reasoning=True,
                supports_none_reasoning_effort=True,
                supports_max_reasoning_effort=True,
            ),
        },
    )
    proposal = build_report(catalog, today=TODAY)["proposals"][0]
    assert proposal["based_on"] == "openai:gpt-6-luna"
    assert proposal["reasoning"]["reasoning_modes"]

    apply_module.apply_proposals([proposal], None, today=TODAY)
    registry = _load_applied_registry(registry_path, monkeypatch)

    new = registry.find_model("openai", "gpt-7-luna")
    base = registry.find_model("openai", "gpt-6-luna")
    assert new is not None and new.selectable is True
    assert new.reasoning_modes == base.reasoning_modes
    assert normalized_reasoning_levels(new) == normalized_reasoning_levels(base)
    assert normalized_reasoning_levels(new)
    reasoning_max = RoutingConstraints(reasoning_mode="on", reasoning_effort="max")
    routable = {
        candidate.model_name
        for tier in Tier
        for candidate in registry.get_candidates(tier, reasoning_max)
    }
    assert "gpt-7-luna" in routable
    # Added alongside (dearer) keeps the family model selectable; a replacement hides it.
    assert base.selectable is (proposal["replaces"] is None)
