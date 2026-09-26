from pathlib import Path

import pytest
import yaml

from orchestrator.model_registry import ModelRegistry
from orchestrator.reasoning_capabilities import (
    default_reasoning_level,
    normalized_reasoning_levels,
)
from orchestrator.routing_types import RoutingConstraints, Tier
from server.billing.models import ModelBillingClass
from server.billing.plan_catalog import get_plan_catalog


def test_registry_exposes_attachment_capabilities():
    registry = ModelRegistry.from_yaml()

    openai_candidate = registry.find_model("openai", "gpt-4o-mini")
    assert openai_candidate is not None
    assert openai_candidate.supports_image_input is True
    assert "application/pdf" in openai_candidate.supported_attachment_mime_types
    assert openai_candidate.max_attachment_bytes == 20971520
    assert openai_candidate.max_attachments_per_request == 5

    deepseek_candidate = registry.find_model("deepseek", "deepseek-chat")
    assert deepseek_candidate is not None
    assert deepseek_candidate.supports_image_input is False
    assert deepseek_candidate.supported_attachment_mime_types == []
    assert deepseek_candidate.max_attachments_per_request == 0


def test_registry_exposes_explicit_billing_classes_separate_from_routing_tiers():
    registry = ModelRegistry.from_yaml()

    standard = registry.find_model("openai", "gpt-5.4-mini")
    advanced = registry.find_model("openai", "gpt-5.4")
    premium = registry.find_model("claude", "claude-opus-4-6")

    assert standard is not None
    assert standard.tier.value == "T2"
    assert standard.billing_class is ModelBillingClass.STANDARD
    assert advanced is not None
    assert advanced.billing_class is ModelBillingClass.ADVANCED
    assert premium is not None
    assert premium.billing_class is ModelBillingClass.PREMIUM
    assert premium.input_credit_multiplier == 6
    assert premium.output_credit_multiplier == 30
    assert premium.credit_usage_label == "Premium"
    assert premium.credit_pricing_version


def test_registry_missing_billing_class_fails_closed(tmp_path):
    data = yaml.safe_load(Path("config/model_registry.yaml").read_text(encoding="utf-8"))
    model = data["providers"]["openai"]["models"][0]
    model.pop("billing_class")
    path = tmp_path / "model_registry.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Missing required fields"):
        ModelRegistry.from_yaml(str(path))


def test_registry_missing_credit_economics_fails_closed(tmp_path):
    data = yaml.safe_load(Path("config/model_registry.yaml").read_text(encoding="utf-8"))
    data["providers"]["openai"]["models"][0].pop("input_credit_multiplier")
    path = tmp_path / "model_registry.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Missing required fields"):
        ModelRegistry.from_yaml(str(path))


def test_registry_rejects_unknown_billing_class(tmp_path):
    data = yaml.safe_load(Path("config/model_registry.yaml").read_text(encoding="utf-8"))
    data["providers"]["openai"]["models"][0]["billing_class"] = "vip"
    path = tmp_path / "model_registry.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid billing_class 'vip'"):
        ModelRegistry.from_yaml(str(path))


def test_registry_separates_selectable_models_from_compatibility_entries():
    registry = ModelRegistry.from_yaml()
    selectable = {
        (candidate.provider, candidate.model_name)
        for candidate in registry.list_selectable_models()
    }

    assert ("openai", "gpt-5.6-sol") in selectable
    assert ("deepseek", "deepseek-v4-flash") in selectable
    assert ("claude", "claude-sonnet-4-6") in selectable
    assert ("claude", "claude-opus-4-6") in selectable
    assert ("claude", "claude-opus-4-5") in selectable
    assert ("openai", "gpt-5.2-codex") not in selectable
    assert ("grok", "grok-4-1-fast-reasoning") not in selectable


def test_every_claude_model_declares_its_thinking_mode_explicitly():
    registry = ModelRegistry.from_yaml()

    for candidate in registry.list_models(provider="claude"):
        assert candidate.reasoning_modes
        assert candidate.default_reasoning_mode in candidate.reasoning_modes


def test_selectable_models_expose_normalized_reasoning_capabilities():
    registry = ModelRegistry.from_yaml()
    expected = {
        ("openai", "gpt-5.6-sol"): ["low", "medium", "high", "max"],
        ("gemini", "gemini-3.1-pro-preview"): ["low", "medium", "high"],
        ("deepseek", "deepseek-v4-pro"): ["low", "high", "max"],
        ("grok", "grok-4.3"): ["low", "medium", "high", "max"],
        ("grok", "grok-4.5"): ["low", "medium", "high"],
        ("claude", "claude-sonnet-5"): ["low", "medium", "high", "max"],
        ("openai", "gpt-4o-mini"): [],
    }

    for key, levels in expected.items():
        candidate = registry.find_model(*key)
        assert candidate is not None
        assert normalized_reasoning_levels(candidate) == levels
        assert default_reasoning_level(candidate) == (levels[0] if levels else None)


def test_smart_routing_filters_models_by_requested_reasoning_level():
    registry = ModelRegistry.from_yaml()
    constraints = RoutingConstraints(reasoning_mode="on", reasoning_effort="max")

    candidates = [
        *registry.get_candidates(Tier.T0, constraints),
        *registry.get_candidates(Tier.T1, constraints),
        *registry.get_candidates(Tier.T2, constraints),
        *registry.get_candidates(Tier.T3, constraints),
    ]
    pairs = {(candidate.provider, candidate.model_name) for candidate in candidates}

    assert ("openai", "gpt-5.6-sol") in pairs
    assert ("deepseek", "deepseek-v4-pro") in pairs
    assert ("grok", "grok-4.3") in pairs
    assert ("claude", "claude-opus-5") in pairs
    assert ("gemini", "gemini-3.1-pro-preview") not in pairs
    assert ("grok", "grok-4.5") not in pairs
    assert ("openai", "gpt-4o-mini") not in pairs


def test_smart_reasoning_on_excludes_models_without_reasoning_controls():
    registry = ModelRegistry.from_yaml()
    candidates = registry.get_candidates(
        Tier.T1,
        RoutingConstraints(reasoning_mode="on", reasoning_effort="auto"),
    )

    assert ("openai", "gpt-4o-mini") not in {
        (candidate.provider, candidate.model_name) for candidate in candidates
    }


def test_active_claude_4_models_use_the_requested_subscription_classes():
    registry = ModelRegistry.from_yaml()
    catalog = get_plan_catalog()
    expected = {
        "claude-sonnet-4-6": (
            ModelBillingClass.ADVANCED,
            {"plus", "pro"},
            3.0,
            15.0,
        ),
        "claude-opus-4-6": (
            ModelBillingClass.PREMIUM,
            {"pro"},
            5.0,
            25.0,
        ),
        "claude-opus-4-5": (
            ModelBillingClass.PREMIUM,
            {"pro"},
            5.0,
            25.0,
        ),
    }

    for model_name, (
        billing_class,
        allowed_plans,
        input_rate,
        output_rate,
    ) in expected.items():
        candidate = registry.find_model("claude", model_name)
        assert candidate is not None
        assert candidate.enabled is True
        assert candidate.selectable is True
        assert candidate.lifecycle_status == "ACTIVE"
        assert candidate.billing_class is billing_class
        assert candidate.input_cost_per_1m == input_rate
        assert candidate.output_cost_per_1m == output_rate

        actual_plans = {
            plan.code
            for plan in catalog.list_plans()
            if billing_class.value in plan.entitlements.allowed_billing_classes
        }
        assert actual_plans == allowed_plans


def test_registry_resolves_retired_and_redirected_ids_without_losing_request_identity():
    registry = ModelRegistry.from_yaml()

    codex = registry.resolve_model_identity("openai", "gpt-5.2-codex")
    grok = registry.resolve_model_identity("grok", "grok-4-1-fast-reasoning")

    assert codex is not None
    assert codex["requested_model"] == "gpt-5.2-codex"
    assert codex["runtime_model"] == "gpt-5.6-sol"
    assert codex["lifecycle_status"] == "RETIRED"
    assert codex["alias_redirected"] is True
    assert codex["replacement_model"] == "gpt-5.6-sol"
    assert "removed" in codex["migration_reason"].lower()
    assert grok is not None
    assert grok["runtime_model"] == "grok-4.3"
    assert grok["lifecycle_status"] == "ALIAS_REDIRECTED"
