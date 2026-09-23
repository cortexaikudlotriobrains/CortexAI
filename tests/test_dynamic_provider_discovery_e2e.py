from __future__ import annotations

from pathlib import Path
import shutil
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from config.provider_catalog import ProviderCatalog
from orchestrator.model_registry import ModelRegistry
from server.app import create_app


def _write_catalog_with_new_provider(tmp_path: Path) -> tuple[Path, Path, str]:
    provider_id = "zai"
    model_name = "zai-chat"

    providers_src = Path("config/providers.yaml")
    models_src = Path("config/model_registry.yaml")
    providers_data = yaml.safe_load(providers_src.read_text(encoding="utf-8"))
    models_data = yaml.safe_load(models_src.read_text(encoding="utf-8"))

    providers_list = list(providers_data.get("providers") or [])
    providers_list.append(
        {
            "id": provider_id,
            "label": "Z.AI",
            "api_key_env": "ZAI_API_KEY",
            "default_model_env": "DEFAULT_ZAI_MODEL",
            "default_model": model_name,
            "compare_default_model": model_name,
            "byok_supported": True,
            "capabilities": ["chat", "stream", "compare", "smart_routing", "prompt_optimization"],
            "ui": {
                "display_name": "Z.AI",
                "icon_token": "star",
                "color": "#2563EB",
                "sort_order": 99,
            },
        }
    )
    providers_data["providers"] = providers_list

    providers_block = dict(models_data.get("providers") or {})
    providers_block[provider_id] = {
        "models": [
            {
                "name": model_name,
                "tier": "T1",
                "input_cost_per_1m": 0.2,
                "output_cost_per_1m": 0.6,
                "context_limit": 128000,
                "tags": ["balanced", "general"],
                "enabled": True,
                "billing_class": "advanced",
                "access_category": "advanced",
                "input_credit_multiplier": 2.0,
                "output_credit_multiplier": 8.0,
                "credit_usage_label": "High",
                "credit_pricing_version": "2026-07-29",
            }
        ]
    }
    models_data["providers"] = providers_block

    providers_path = tmp_path / "providers.with-zai.yaml"
    models_path = tmp_path / "model_registry.with-zai.yaml"
    providers_path.write_text(yaml.safe_dump(providers_data, sort_keys=False), encoding="utf-8")
    models_path.write_text(yaml.safe_dump(models_data, sort_keys=False), encoding="utf-8")
    return providers_path, models_path, provider_id


@pytest.fixture()
def discovery_client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    monkeypatch.setenv("ENABLE_MODELS_CATALOG", "true")
    app = create_app()
    from server import dependencies as deps

    session_user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        deps,
        "parse_session",
        lambda cookie: session_user_id if cookie else None,
    )

    temp_dir = Path("tests/.tmp_provider_discovery") / uuid.uuid4().hex
    temp_dir.mkdir(parents=True, exist_ok=True)
    providers_path, models_path, provider_id = _write_catalog_with_new_provider(temp_dir)
    catalog = ProviderCatalog.from_yaml(str(providers_path))

    from server.routes import catalog as catalog_route

    monkeypatch.setattr(catalog_route, "get_provider_catalog", lambda: catalog)
    monkeypatch.setattr(catalog_route, "get_provider_ids", lambda: catalog.provider_ids())

    class _PatchedRegistry:
        @staticmethod
        def from_yaml():
            return ModelRegistry.from_yaml(str(models_path))

    monkeypatch.setattr(catalog_route, "ModelRegistry", _PatchedRegistry)

    try:
        yield TestClient(app), provider_id
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_new_provider_appears_in_discovery_endpoints(discovery_client):
    client, provider_id = discovery_client
    session_cookie = {"cortex_session": "test-session-cookie"}

    providers_resp = client.get("/v1/providers", cookies=session_cookie)
    assert providers_resp.status_code == 200
    providers_payload = providers_resp.json()
    provider_ids = {item["provider"] for item in providers_payload["providers"]}
    assert provider_id in provider_ids

    zai_provider = next(
        item for item in providers_payload["providers"] if item["provider"] == provider_id
    )
    assert zai_provider["label"] == "Z.AI"
    assert zai_provider["default_model"] == "zai-chat"
    assert zai_provider["enabled_model_count"] >= 1

    models_resp = client.get("/v1/models", cookies=session_cookie)
    assert models_resp.status_code == 200
    models_payload = models_resp.json()
    model_pairs = {(item["provider"], item["model"]) for item in models_payload["models"]}
    assert (provider_id, "zai-chat") in model_pairs
    zai_model = next(item for item in models_payload["models"] if item["provider"] == provider_id)
    assert zai_model["billing_class"] == "advanced"

    options_resp = client.get("/v1/model-options", cookies=session_cookie)
    assert options_resp.status_code == 200
    option_pairs = {
        (item["provider"], item["model"])
        for item in options_resp.json()["models"]
    }
    assert (provider_id, "zai-chat") in option_pairs

    filtered_resp = client.get(
        f"/v1/models?provider={provider_id}",
        cookies=session_cookie,
    )
    assert filtered_resp.status_code == 200
    filtered_payload = filtered_resp.json()
    assert filtered_payload["provider"] == provider_id
    assert any(item["model"] == "zai-chat" for item in filtered_payload["models"])
