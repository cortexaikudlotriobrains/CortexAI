"""
Test Suite: FastAPI Contract & Guardrail Validation

Purpose
-------
This test module validates the *public API contract, guardrails, and safety guarantees*
of the CortexAI FastAPI application. These tests are intentionally designed to operate
WITHOUT calling real LLM providers, external APIs, or incurring token costs.

The goal is to ensure that:
- The FastAPI layer is stable, predictable, and safe
- API inputs are validated correctly
- Errors are normalized consistently
- The application never crashes with 5xx errors for user-controlled input
- DTO mappings remain compatible with orchestration-layer responses

What This Test Suite Covers
---------------------------
1. API Health & Availability
   - Confirms the service is reachable and responds correctly (`/health`)

2. Authentication & Guardrails
   - Ensures protected endpoints reject requests without valid credentials
   - Prevents misuse such as insufficient or excessive compare targets

3. Input Validation
   - Enforces minimum and maximum constraints on compare requests
   - Validates request structure before orchestration logic runs

4. Error Normalization
   - Verifies that low-level exceptions (timeouts, auth failures, rate limits, etc.)
     are converted into consistent, user-facing NormalizedError objects
   - Confirms retryable vs non-retryable errors are classified correctly

5. DTO Contract Stability
   - Ensures CompareResponseDTO correctly maps from a MultiUnifiedResponse-like object
   - Acts as an early-warning system for breaking changes between layers

6. Runtime Safety Guarantees
   - Confirms that the `/v1/compare` endpoint never returns HTTP 500 errors
     due to malformed input or orchestration-layer behavior

How These Tests Work
--------------------
- A FakeOrchestrator is injected using FastAPI dependency overrides
- All orchestration responses are deterministic and in-memory
- No external providers, tokens, or network calls are used
- Tests focus strictly on FastAPI behavior and response contracts

What This Test Suite Does NOT Cover
-----------------------------------
- LLM response quality or correctness
- Provider-specific adapter logic (OpenAI, Gemini, etc.)
- Token accounting accuracy across real providers
- Performance, load, or concurrency behavior
- End-to-end integrations with real APIs

Why This Matters
----------------
These tests protect the FastAPI layer as a *stable public interface*.
They ensure that internal refactors, routing changes, or provider updates
do not silently break API consumers or cause runtime crashes.

If these tests pass, the application guarantees:
- No unexpected 500s from user input
- Predictable error handling
- Stable API response shapes
- Safe orchestration boundaries
"""

import json
import logging
from contextlib import suppress
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Boolean, Column, MetaData, String, Table, Uuid, text

from config.provider_catalog import get_provider_catalog, get_provider_ids
from orchestrator.model_registry import ModelRegistry
from server.app import create_app
from models.unified_response import UnifiedResponse, TokenUsage, NormalizedError
from server.schemas.responses import ChatResponseDTO, CompareResponseDTO
from server.billing.enforcement_service import ReservedRequestUsage
from server.billing.entitlement_service import EntitlementDenial
from server.billing.errors import EntitlementDeniedError
from server.persistence import ApiKeyPersistenceResolution
from api.base_client import BaseAIClient

# -------------------------------------------------------------------
# Fake MultiUnifiedResponse (match what CompareResponseDTO expects)
# -------------------------------------------------------------------


@dataclass(frozen=True)
class FakeMultiUnifiedResponse:
    request_id: str
    request_group_id: str
    prompt: str
    responses: List[UnifiedResponse]

    success_count: int
    failure_count: int
    error_count: int
    total_tokens: int
    total_cost: float

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )


# -------------------------------------------------------------------
# Fake orchestrator (keeps tests offline & deterministic)
# -------------------------------------------------------------------


class FakeOrchestrator:
    def __init__(self):
        self.last_ask_prompt = None
        self.last_ask_model_type = None
        self.last_ask_context = None
        self.last_compare_prompt = None
        self.last_compare_context = None
        self.last_ask_kwargs = {}
        self.last_compare_kwargs = {}
        self.prepare_messages_calls = []

    @staticmethod
    def _metadata_for_research_mode(research_mode: str | None) -> dict[str, Any]:
        if str(research_mode or "").strip().lower() != "on":
            return {}
        return {
            "research_used": True,
            "research_reused": False,
            "sources": [{"title": "Example Source", "url": "https://example.com/report"}],
        }

    def ask(
        self, prompt: str, model_type: Optional[str] = None, context: Any = None, **kwargs
    ) -> UnifiedResponse:
        self.last_ask_prompt = prompt
        self.last_ask_model_type = model_type
        self.last_ask_context = context
        self.last_ask_kwargs = dict(kwargs)
        return UnifiedResponse(
            request_id="req_ask_1",
            text="OK",
            provider=model_type or "openai",
            model=kwargs.get("model_name") or kwargs.get("model") or "fake-model",
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata=self._metadata_for_research_mode(kwargs.get("research_mode")),
        )

    def prepare_messages_for_turn(
        self,
        *,
        prompt: str,
        context: Any = None,
        research_mode: str = "auto",
    ) -> dict[str, Any]:
        self.prepare_messages_calls.append(
            {
                "prompt": prompt,
                "context": context,
                "research_mode": research_mode,
            }
        )
        return {
            "prompt": prompt,
            "messages": [{"role": "user", "content": prompt}],
            "research_metadata": self._metadata_for_research_mode(research_mode),
            "optimization_metadata": {},
        }

    def compare(
        self, prompt: str, models_list: List[Dict[str, Any]], context: Any = None, **kwargs
    ) -> FakeMultiUnifiedResponse:
        self.last_compare_prompt = prompt
        self.last_compare_context = context
        self.last_compare_kwargs = dict(kwargs)
        shared_metadata = self._metadata_for_research_mode(kwargs.get("research_mode"))
        r1 = UnifiedResponse(
            request_id="req_cmp_1",
            text="A",
            provider=models_list[0]["provider"],
            model=models_list[0].get("model", "fake-a"),
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata=shared_metadata,
        )

        r2 = UnifiedResponse(
            request_id="req_cmp_2",
            text="B",
            provider=models_list[1]["provider"],
            model=models_list[1].get("model", "fake-b"),
            latency_ms=12,
            token_usage=TokenUsage(prompt_tokens=6, completion_tokens=4, total_tokens=10),
            estimated_cost=0.00002,
            finish_reason="stop",
            error=None,
            metadata=shared_metadata,
        )

        total_tokens = r1.token_usage.total_tokens + r2.token_usage.total_tokens
        total_cost = r1.estimated_cost + r2.estimated_cost

        return FakeMultiUnifiedResponse(
            request_id="req_compare_1",
            request_group_id="grp_1",
            prompt=prompt,
            responses=[r1, r2],
            success_count=2,
            failure_count=0,
            error_count=0,
            total_tokens=total_tokens,
            total_cost=total_cost,
        )


# -------------------------------------------------------------------
# Dummy client to access BaseAIClient helpers
# -------------------------------------------------------------------


class DummyClient(BaseAIClient):
    def __init__(self):
        # don't call BaseAIClient.__init__ (signature may vary)
        pass

    def get_completion(self, *args, **kwargs):
        raise NotImplementedError

    def list_available_models(self):
        return []


def _reset_db_runtime_state(monkeypatch, *, schema_name: str) -> None:
    import db.engine as db_engine
    import db.tables as db_tables

    with suppress(Exception):
        if db_engine._ENGINE is not None:
            db_engine._ENGINE.dispose()

    monkeypatch.setattr(db_engine, "_ENGINE", None)
    monkeypatch.setattr(db_tables, "DB_SCHEMA", schema_name)
    db_tables._tables_cache.clear()
    db_tables.metadata.clear()


def _bootstrap_dev_auth_sqlite_schema() -> None:
    import db.engine as db_engine

    engine = db_engine.get_engine()
    metadata = MetaData()
    Table(
        "users",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("email", String, unique=True),
        Column("display_name", String),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("auth_provider", String),
        Column("auth_subject", String),
        Column("auth_issuer", String),
    )
    metadata.create_all(engine)


# -------------------------------------------------------------------
# Pytest fixtures
# -------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch):
    """
    Build FastAPI app and override get_orchestrator dependency.
    """
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    monkeypatch.setenv("DB_SCHEMA", "main")
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "false")
    monkeypatch.setenv("ENABLE_MODELS_CATALOG", "true")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("FRONTEND_RUNTIME_API_BASE", raising=False)
    monkeypatch.delenv("FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN", raising=False)
    monkeypatch.delenv("FRONTEND_RUNTIME_DEV_SESSION_LOGIN_TOKEN", raising=False)
    _reset_db_runtime_state(monkeypatch, schema_name="main")
    _bootstrap_dev_auth_sqlite_schema()
    app = create_app()

    # Keep these tests DB-agnostic and deterministic.
    from server.routes import chat as chat_route
    from server.routes import compare as compare_route
    from server.routes import files as files_route
    from server.routes import history as history_route
    from server.routes import optimize as optimize_route

    chat_route.API_DB_ENABLED = False
    compare_route.API_DB_ENABLED = False
    files_route.API_DB_ENABLED = False
    history_route.API_DB_ENABLED = False
    optimize_route.API_DB_ENABLED = False

    from server import dependencies as deps

    session_user_id = UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        deps,
        "parse_session",
        lambda cookie: session_user_id if cookie else None,
    )

    # Clear singleton cache to avoid cross-test leakage
    if hasattr(deps.get_orchestrator, "_instance"):
        delattr(deps.get_orchestrator, "_instance")

    fake_orchestrator = FakeOrchestrator()
    app.state.fake_orchestrator = fake_orchestrator
    app.dependency_overrides[deps.get_orchestrator] = lambda: fake_orchestrator
    yield app
    _reset_db_runtime_state(monkeypatch, schema_name="main")


@pytest.fixture()
def client(app):
    return TestClient(app)


def _parse_runtime_config_payload(js_text: str) -> dict[str, Any]:
    prefix = "window.CORTEX_RUNTIME_CONFIG = "
    assert js_text.startswith(prefix)
    payload_text = js_text[len(prefix) :].strip()
    if payload_text.endswith(";"):
        payload_text = payload_text[:-1]
    return json.loads(payload_text)


# -------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")


def test_chat_requires_auth(client):
    payload = {
        "prompt": "hello",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    r = client.post("/v1/chat", json=payload)
    assert r.status_code in (401, 403)


def test_compare_requires_auth(client):
    payload = {
        "prompt": "hello",
        "targets": [{"provider": "openai"}, {"provider": "gemini"}],
    }
    r = client.post("/v1/compare", json=payload)
    assert r.status_code in (401, 403)


def test_chat_rejects_api_key_only_auth(client):
    payload = {
        "prompt": "hello",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    r = client.post("/v1/chat", json=payload, headers={"X-API-Key": "dev-key-1"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_auth_required"


def test_compare_rejects_api_key_only_auth(client):
    payload = {
        "prompt": "hello",
        "targets": [{"provider": "openai"}, {"provider": "gemini"}],
    }
    r = client.post("/v1/compare", json=payload, headers={"X-API-Key": "dev-key-1"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_auth_required"


def test_dev_login_is_disabled_by_default(client):
    r = client.post("/v1/auth/dev-login")
    assert r.status_code == 404


def test_dev_login_sets_session_cookie_when_enabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "true")
    r = client.post("/v1/auth/dev-login")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["email"] == "cli@cortexai.local"
    assert "cortex_session=" in (r.headers.get("set-cookie") or "")

    # TestClient persists cookies between requests, so /me should resolve.
    me = client.get("/v1/auth/me")
    assert me.status_code == 200


def test_dev_login_token_guard_when_configured(client, monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "true")
    monkeypatch.setenv("DEV_SESSION_LOGIN_TOKEN", "local-dev-token")

    unauthorized = client.post("/v1/auth/dev-login")
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/v1/auth/dev-login",
        headers={"X-Dev-Login-Token": "local-dev-token"},
    )
    assert authorized.status_code == 200


def test_dev_login_rejected_in_production_runtime(client, monkeypatch):
    monkeypatch.setenv("ENABLE_DEV_SESSION_LOGIN", "true")
    monkeypatch.setenv("APP_ENV", "production")

    r = client.post("/v1/auth/dev-login")
    assert r.status_code == 403


def test_auth_success_redirect_marks_fresh_login():
    from server.routes import auth as auth_route

    assert (
        auth_route._with_query_param("/index.html", "fresh_login", "1")
        == "/index.html?fresh_login=1"
    )
    assert (
        auth_route._with_query_param("/index.html?foo=bar&fresh_login=0", "fresh_login", "1")
        == "/index.html?foo=bar&fresh_login=1"
    )


def test_runtime_config_js_defaults_to_request_origin(client, monkeypatch):
    monkeypatch.delenv("ENABLE_MODELS_CATALOG", raising=False)
    r = client.get("/runtime-config.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "no-store" in (r.headers.get("cache-control") or "")
    payload = _parse_runtime_config_payload(r.text)
    assert payload["apiBase"] == "http://testserver"
    assert payload["enableDevSessionLogin"] is False
    assert payload["directAttachmentUploads"] is False
    assert payload["legacyAttachmentUploads"] is True
    assert payload["modelsCatalogEnabled"] is False


def test_runtime_config_js_honors_frontend_env_overrides(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("FRONTEND_RUNTIME_API_BASE", "https://kudlo.triobrain.com/")
    monkeypatch.setenv("FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN", "true")
    monkeypatch.setenv("FRONTEND_RUNTIME_DEV_SESSION_LOGIN_TOKEN", "local-token")
    monkeypatch.setenv("ATTACHMENTS_DIRECT_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("ATTACHMENTS_LEGACY_PROXY_UPLOAD_ENABLED", "false")
    monkeypatch.setenv("ENABLE_MODELS_CATALOG", "true")

    r = client.get("/runtime-config.js")
    assert r.status_code == 200
    payload = _parse_runtime_config_payload(r.text)
    assert payload["apiBase"] == "https://kudlo.triobrain.com"
    assert payload["enableDevSessionLogin"] is True
    assert payload["devSessionLoginToken"] == "local-token"
    assert payload["directAttachmentUploads"] is True
    assert payload["legacyAttachmentUploads"] is False
    assert payload["modelsCatalogEnabled"] is True


def test_runtime_config_js_disables_dev_session_login_in_production(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN", "true")

    r = client.get("/runtime-config.js")
    assert r.status_code == 200
    payload = _parse_runtime_config_payload(r.text)
    assert payload["enableDevSessionLogin"] is False


def test_client_diagnostics_accepts_lifecycle_events_without_auth(client):
    payload = {
        "source": "test",
        "reason": "unit",
        "page_instance_id": "page-test",
        "events": [
            {
                "event": "boot",
                "timestamp": "2026-07-04T00:00:00.000Z",
                "page_instance_id": "page-test",
                "details": {
                    "navigationType": "reload",
                    "wasDiscarded": False,
                },
            }
        ],
    }

    r = client.post("/v1/client-diagnostics", json=payload)

    assert r.status_code == 204
    assert r.text == ""


def test_chat_with_attachments_requires_db_mode(client):
    payload = {
        "prompt": "describe this file",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "attachments": [
            {
                "file_id": "11111111-1111-1111-1111-111111111111",
                "usage_role": "primary",
                "transform_mode": "auto",
            }
        ],
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 501
    assert r.json()["detail"]["code"] == "attachments_require_db"


def test_compare_with_attachments_requires_db_mode(client):
    payload = {
        "prompt": "compare this file",
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
        "attachments": [
            {
                "file_id": "22222222-2222-2222-2222-222222222222",
                "usage_role": "primary",
                "transform_mode": "auto",
            }
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 501
    assert r.json()["detail"]["code"] == "attachments_require_db"


def test_providers_catalog_requires_auth(client):
    r = client.get("/v1/providers")
    assert r.status_code in (401, 403)


def test_models_catalog_requires_auth(client):
    r = client.get("/v1/models")
    assert r.status_code in (401, 403)


def test_providers_catalog_rejects_api_key_only_auth(client):
    r = client.get("/v1/providers", headers={"X-API-Key": "dev-key-1"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_auth_required"


def test_models_catalog_rejects_api_key_only_auth(client):
    r = client.get("/v1/models", headers={"X-API-Key": "dev-key-1"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_auth_required"


def test_model_options_require_session_auth(client):
    unauthenticated = client.get("/v1/model-options")
    api_key_only = client.get("/v1/model-options", headers={"X-API-Key": "dev-key-1"})

    assert unauthenticated.status_code in (401, 403)
    assert api_key_only.status_code == 403
    assert api_key_only.json()["detail"]["code"] == "session_auth_required"


def test_models_catalog_returns_not_found_when_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_MODELS_CATALOG", "false")

    authenticated = client.get(
        "/v1/models?enabled_only=true",
        cookies={"cortex_session": "test-session-cookie"},
    )
    unauthenticated = client.get("/v1/models?enabled_only=true")

    assert authenticated.status_code == 404
    assert authenticated.json()["detail"] == "Not Found"
    assert unauthenticated.status_code == 404
    assert unauthenticated.json()["detail"] == "Not Found"


def test_model_options_remain_complete_when_rich_catalog_is_disabled(client, monkeypatch):
    monkeypatch.setenv("ENABLE_MODELS_CATALOG", "false")

    response = client.get(
        "/v1/model-options",
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] is None
    assert body["enabled_only"] is True
    assert body["total"] == len(body["models"])
    assert all(item["enabled"] is True for item in body["models"])
    assert all(item["selectable"] is True for item in body["models"])

    registry = ModelRegistry.from_yaml()
    expected_pairs = {
        (candidate.provider, candidate.model_name)
        for candidate in registry.list_selectable_models()
    }
    actual_pairs = {(item["provider"], item["model"]) for item in body["models"]}
    actual_providers = {provider for provider, _model in actual_pairs}

    assert actual_pairs == expected_pairs
    assert {"gemini", "grok"}.issubset(actual_providers)


def test_providers_catalog_returns_catalog_and_model_counts(client):
    r = client.get("/v1/providers", cookies={"cortex_session": "test-session-cookie"})
    assert r.status_code == 200

    body = r.json()
    assert body["total"] == len(body["providers"])
    assert isinstance(body.get("timestamp"), str) and body["timestamp"]

    catalog = get_provider_catalog()
    registry = ModelRegistry.from_yaml()
    by_provider = {item["provider"]: item for item in body["providers"]}
    assert set(by_provider.keys()) == set(catalog.provider_ids())

    for spec in catalog.provider_specs():
        item = by_provider[spec.provider_id]
        assert item["label"] == spec.label
        assert item["api_key_env"] == spec.api_key_env
        assert item["default_model_env"] == spec.default_model_env
        assert item["default_model"] == spec.default_model
        assert item["byok_supported"] == spec.byok_supported
        assert item["capabilities"] == list(spec.capabilities)
        assert item["ui"] == dict(spec.ui)

        expected_all = registry.list_models(spec.provider_id, include_disabled=True)
        expected_enabled = registry.list_selectable_models(spec.provider_id)
        assert item["model_count"] == len(expected_all)
        assert item["enabled_model_count"] == len(expected_enabled)


def test_models_catalog_lists_enabled_models_by_default(client):
    r = client.get("/v1/models", cookies={"cortex_session": "test-session-cookie"})
    assert r.status_code == 200

    body = r.json()
    assert body["provider"] is None
    assert body["enabled_only"] is True
    assert body["total"] == len(body["models"])
    assert isinstance(body.get("timestamp"), str) and body["timestamp"]
    assert all(item["enabled"] is True for item in body["models"])
    assert all(item["selectable"] is True for item in body["models"])

    required_keys = {
        "provider",
        "model",
        "tier",
        "billing_class",
        "access_category",
        "input_credit_multiplier",
        "output_credit_multiplier",
        "credit_usage_label",
        "credit_pricing_version",
        "input_cost_per_1m",
        "output_cost_per_1m",
        "cached_input_cost_per_1m",
        "cache_write_cost_per_1m",
        "context_limit",
        "max_output_tokens",
        "tags",
        "enabled",
        "selectable",
        "display_name",
        "description",
        "release_status",
        "lifecycle_status",
        "pricing_source_url",
        "source_verified_at",
        "supports_image_input",
        "supported_attachment_mime_types",
        "max_attachment_bytes",
        "max_attachments_per_request",
    }
    for item in body["models"]:
        assert required_keys.issubset(item.keys())
        assert item["billing_class"] in {
            "economical",
            "standard",
            "advanced",
            "premium",
        }

    registry = ModelRegistry.from_yaml()
    expected_pairs = {
        (candidate.provider, candidate.model_name)
        for candidate in registry.list_selectable_models()
    }
    actual_pairs = {(item["provider"], item["model"]) for item in body["models"]}
    assert actual_pairs == expected_pairs

    claude_by_model = {
        item["model"]: item for item in body["models"] if item["provider"] == "claude"
    }
    assert claude_by_model["claude-sonnet-4-6"]["billing_class"] == "advanced"
    assert claude_by_model["claude-opus-4-6"]["billing_class"] == "premium"
    assert claude_by_model["claude-opus-4-5"]["billing_class"] == "premium"


def test_models_catalog_filters_by_provider_case_insensitive(client):
    provider = get_provider_ids()[0]
    r = client.get(
        f"/v1/models?provider={provider.upper()}",
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200

    body = r.json()
    assert body["provider"] == provider
    assert body["enabled_only"] is True
    assert all(item["provider"] == provider for item in body["models"])

    registry = ModelRegistry.from_yaml()
    expected_names = sorted(
        candidate.model_name
        for candidate in registry.list_selectable_models(provider=provider)
    )
    actual_names = sorted(item["model"] for item in body["models"])
    assert actual_names == expected_names


def test_models_catalog_can_include_disabled(client):
    provider = get_provider_ids()[0]
    r_enabled = client.get(
        f"/v1/models?provider={provider}",
        cookies={"cortex_session": "test-session-cookie"},
    )
    r_all = client.get(
        f"/v1/models?provider={provider}&enabled_only=false",
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r_enabled.status_code == 200
    assert r_all.status_code == 200

    enabled_body = r_enabled.json()
    all_body = r_all.json()
    assert enabled_body["enabled_only"] is True
    assert all_body["enabled_only"] is False

    enabled_pairs = {(item["provider"], item["model"]) for item in enabled_body["models"]}
    all_pairs = {(item["provider"], item["model"]) for item in all_body["models"]}
    assert enabled_pairs.issubset(all_pairs)
    assert len(all_pairs) >= len(enabled_pairs)


def test_models_catalog_rejects_unsupported_provider(client):
    r = client.get(
        "/v1/models?provider=not-a-provider",
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "Unsupported provider 'not-a-provider'" in detail
    for provider in get_provider_ids():
        assert provider in detail


def test_optimize_rejects_api_key_only_auth(client):
    r = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        headers={"X-API-Key": "dev-key-1"},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "session_auth_required"


def test_optimize_accepts_session_cookie_auth(client, monkeypatch):
    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "false")

    r = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["original_prompt"] == "make this prompt better"
    assert body["optimization_status"] == "disabled"
    assert body["fallback_reason"] == "optimization_disabled"


def test_optimize_is_subject_to_plan_request_rate_limit(client, monkeypatch):
    from server.routes import optimize as optimize_route

    optimizer_called = False

    class FakeOptimizer:
        def optimize_prompt(self, _payload):
            nonlocal optimizer_called
            optimizer_called = True
            return {"optimized_prompt": "should not run"}

    def rate_limited(**_kwargs):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Rate limit exceeded (5 requests/minute)",
                "retry_after_seconds": 30,
            },
        )

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_resolve_and_enforce_caps", rate_limited)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    response = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "rate_limited"
    assert optimizer_called is False


def test_optimize_uses_schema_optimizer_when_enabled(client, monkeypatch):
    from server.routes import optimize as optimize_route

    class FakeOptimizer:
        def optimize_prompt(self, payload):
            assert payload["prompt"] == "make this prompt better"
            assert payload["max_retries"] == 2
            assert isinstance(payload["deadline_at"], float)
            assert "context_hint" not in payload
            return {
                "optimized_prompt": "Rewrite this prompt to be more specific and actionable.",
                "steps": ["clarified intent"],
                "prompt_quality": "weak",
                "attempt_count": 1,
                "retry_reasons": [],
                "unchanged_retry_used": False,
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.delenv("PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES", raising=False)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    r = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["optimized_prompt"] == "Rewrite this prompt to be more specific and actionable."
    assert body["was_optimized"] is True
    assert body["server_optimization_enabled"] is True
    assert body["optimization_status"] == "optimized"
    assert body["fallback_reason"] is None


def test_optimize_passes_compact_context_when_enabled(client, monkeypatch):
    from server.routes import optimize as optimize_route

    seen_payload = {}

    class FakeOptimizer:
        def optimize_prompt(self, payload):
            seen_payload.update(payload)
            return {
                "optimized_prompt": "Explain why humans mine asteroids.",
                "steps": ["resolved follow-up reference"],
                "prompt_quality": "reference_dependent",
                "attempt_count": 1,
                "retry_reasons": [],
                "unchanged_retry_used": False,
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setenv("PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES", "2")
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    r = client.post(
        "/v1/optimize",
        json={
            "prompt": "I was talking about mining the asteroids",
            "context_hint": "Recent topic: asteroid mining.",
            "context": {
                "session_id": "session-1",
                "conversation_history": [
                    {"role": "user", "content": "List asteroid mining candidates."}
                ],
            },
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    assert seen_payload["prompt"] == "I was talking about mining the asteroids"
    assert seen_payload["max_retries"] == 2
    assert isinstance(seen_payload["deadline_at"], float)
    assert seen_payload["context_hint"] == "Recent topic: asteroid mining."
    assert seen_payload["context"]["session_id"] == "session-1"
    assert seen_payload["context"]["conversation_history"][0]["role"] == "user"
    body = r.json()
    assert body["optimization_status"] == "optimized"


def test_optimize_accepts_four_thousand_character_context_hint(client, monkeypatch):
    from server.routes import optimize as optimize_route

    seen_payload = {}

    class FakeOptimizer:
        def optimize_prompt(self, payload):
            seen_payload.update(payload)
            return {
                "optimized_prompt": payload["prompt"],
                "prompt_quality": "weak",
                "attempt_count": 1,
                "retry_reasons": [],
                "unchanged_retry_used": False,
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    context_hint = "h" * 4000
    r = client.post(
        "/v1/optimize",
        json={"prompt": "Rewrite it", "context_hint": context_hint},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    assert seen_payload["context_hint"] == context_hint


def test_optimize_falls_back_when_schema_optimizer_rejects_output(client, monkeypatch):
    from server.routes import optimize as optimize_route

    class FakeOptimizer:
        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": payload["prompt"],
                "error": {
                    "code": "optimization_failed",
                    "message": "Optimizer response appears to answer the prompt instead of rewriting it",
                },
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    r = client.post(
        "/v1/optimize",
        json={"prompt": "how osama was killed"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["optimized_prompt"] == "how osama was killed"
    assert body["was_optimized"] is False
    assert body["server_optimization_enabled"] is True
    assert body["optimization_status"] == "rejected"
    assert body["fallback_reason"] == "optimizer_output_rejected"


def test_optimize_preserves_unchanged_after_retry_fallback_reason(client, monkeypatch):
    from server.routes import optimize as optimize_route

    class FakeOptimizer:
        provider = "openai"
        model = "gpt-4.1-nano"

        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": payload["prompt"],
                "fallback_reason": "unchanged_after_retry",
                "prompt_quality": "weak",
                "attempt_count": 2,
                "retry_reasons": ["unchanged_weak_prompt"],
                "unchanged_retry_used": True,
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.delenv("PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES", raising=False)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())

    r = client.post(
        "/v1/optimize",
        json={"prompt": "write something about ai"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["optimized_prompt"] == "write something about ai"
    assert body["was_optimized"] is False
    assert body["optimization_status"] == "kept_original"
    assert body["fallback_reason"] == "unchanged_after_retry"


def test_optimize_times_out_to_original_prompt(client, monkeypatch):
    from server.routes import optimize as optimize_route

    reservation = object()
    released: list[dict[str, Any]] = []

    class SlowOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            time.sleep(0.05)
            return {"optimized_prompt": "This should arrive too late."}

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setenv("PROMPT_OPTIMIZER_TIMEOUT_MS", "1")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: SlowOptimizer())
    monkeypatch.setattr(
        optimize_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        optimize_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )

    r = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["optimized_prompt"] == "make this prompt better"
    assert body["was_optimized"] is False
    assert body["server_optimization_enabled"] is True
    assert body["optimization_status"] == "timeout"
    assert body["fallback_reason"] == "timeout"
    time.sleep(0.1)
    assert released == [
        {
            "reservation": reservation,
            "reason": "optimizer_timeout_no_billable_output",
        }
    ]


def test_optimize_timeout_settles_late_billable_provider_usage(client, monkeypatch):
    from server.routes import optimize as optimize_route

    reservation = object()
    finalized: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []

    class SlowBillableOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            time.sleep(0.03)
            return {
                "optimized_prompt": "This result completed after the HTTP timeout.",
                "_billable_usages": [
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "output_text": "This result completed after the HTTP timeout.",
                        "provider_cost_usd": 0.0001,
                    }
                ],
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setenv("PROMPT_OPTIMIZER_TIMEOUT_MS", "1")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: SlowBillableOptimizer())
    monkeypatch.setattr(
        optimize_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        optimize_route,
        "_finalize_subscription_usage",
        lambda **kwargs: finalized.append(kwargs),
    )
    monkeypatch.setattr(
        optimize_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )

    response = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert response.json()["optimization_status"] == "timeout"
    time.sleep(0.1)
    assert len(finalized) == 1
    assert len(finalized[0]["model_usages"]) == 1
    assert released == []


@pytest.mark.parametrize("attempt_count", [1, 2, 3])
def test_optimize_reserves_and_settles_every_billable_attempt(
    client,
    monkeypatch,
    attempt_count,
):
    from server.routes import optimize as optimize_route

    reservation = object()
    reserve_calls: list[dict[str, Any]] = []
    finalize_calls: list[dict[str, Any]] = []

    class RetryingOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": "Write a precise, testable implementation plan.",
                "attempt_count": attempt_count,
                "_billable_usages": [
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "input_tokens": 20 + index,
                        "output_tokens": 10 + index,
                        "output_text": f"attempt {index + 1}",
                        "provider_cost_usd": 0.0001,
                    }
                    for index in range(attempt_count)
                ],
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setenv("PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES", str(attempt_count))
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: RetryingOptimizer())

    def reserve(**kwargs):
        reserve_calls.append(kwargs)
        return reservation

    monkeypatch.setattr(optimize_route, "_reserve_subscription_usage", reserve)
    monkeypatch.setattr(
        optimize_route,
        "_finalize_subscription_usage",
        lambda **kwargs: finalize_calls.append(kwargs),
    )

    response = client.post(
        "/v1/optimize",
        json={
            "prompt": "make this prompt better",
            "credit_activity_id": "activity-optimize",
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert reserve_calls[0]["model_attempt_count"] == attempt_count
    assert reserve_calls[0]["max_output_tokens"] == 1800
    assert reserve_calls[0]["initial_query"] == "make this prompt better"
    assert reserve_calls[0]["credit_activity_id"] == "activity-optimize"
    assert len(finalize_calls) == 1
    assert len(finalize_calls[0]["model_usages"]) == attempt_count


def test_optimize_provider_failure_releases_reserved_credits(client, monkeypatch):
    from server.routes import optimize as optimize_route

    reservation = object()
    released: list[dict[str, Any]] = []

    class FailedOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": payload["prompt"],
                "attempt_count": 1,
                "_billable_usages": [],
                "error": {
                    "code": "optimization_failed",
                    "message": "Provider failed before returning usage.",
                },
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FailedOptimizer())
    monkeypatch.setattr(
        optimize_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        optimize_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )

    response = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert response.json()["optimization_status"] == "failed"
    assert released == [
        {
            "reservation": reservation,
            "reason": "optimizer_no_billable_output",
        }
    ]


def test_optimize_releases_reservation_after_settlement_failure(client, monkeypatch):
    from server.routes import optimize as optimize_route

    reservation = object()
    released: list[dict[str, Any]] = []

    class SuccessfulOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": "A more precise prompt.",
                "_billable_usages": [
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "input_tokens": 20,
                        "output_tokens": 10,
                        "output_text": "A more precise prompt.",
                        "provider_cost_usd": 0.0001,
                    }
                ],
            }

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: SuccessfulOptimizer())
    monkeypatch.setattr(
        optimize_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        optimize_route,
        "_finalize_subscription_usage",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("settlement failed")),
    )
    monkeypatch.setattr(
        optimize_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )

    response = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 500
    assert released == [
        {
            "reservation": reservation,
            "reason": "optimizer_settlement_failed",
        }
    ]


def test_optimize_setup_failure_does_not_leave_a_reservation(client, monkeypatch):
    from server.routes import optimize as optimize_route

    reservation = object()
    released: list[dict[str, Any]] = []

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(
        optimize_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        optimize_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )
    monkeypatch.setattr(
        optimize_route,
        "_get_optimizer",
        lambda: (_ for _ in ()).throw(RuntimeError("optimizer setup failed")),
    )

    with pytest.raises(RuntimeError, match="optimizer setup failed"):
        client.post(
            "/v1/optimize",
            json={"prompt": "make this prompt better"},
            cookies={"cortex_session": "test-session-cookie"},
        )

    assert released == []


def test_optimize_entitlement_denial_prevents_optimizer_invocation(client, monkeypatch):
    from server.routes import optimize as optimize_route

    optimizer_called = False

    class FakeOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            del payload
            nonlocal optimizer_called
            optimizer_called = True
            return {"optimized_prompt": "should not run"}

    def deny_usage(**_kwargs):
        raise EntitlementDeniedError(
            EntitlementDenial(
                code="insufficient_credits",
                message="This request needs 4,096 AI credits, but only 0 remain.",
                meter="ai_credits",
                current_plan="free",
                recommended_plan="plus",
                remaining=0,
            )
        )

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FakeOptimizer())
    monkeypatch.setattr(optimize_route, "_reserve_subscription_usage", deny_usage)

    response = client.post(
        "/v1/optimize",
        json={"prompt": "make this prompt better"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["meter"] == "ai_credits"
    assert optimizer_called is False


def test_explicit_improve_settles_fallback_once_and_chat_does_not_recount(
    client,
    monkeypatch,
):
    from server.routes import optimize as optimize_route

    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    optimize_reservation = ReservedRequestUsage(
        reservation_id=uuid4(),
        request_id="test-optimize",
        operation_type="optimize",
        requested_quantities={"ai_credits": 4096},
        allowed_billing_classes=frozenset({"standard", "advanced"}),
        current_plan="free",
        reset_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    optimize_reserves: list[dict[str, Any]] = []
    optimize_settles: list[dict[str, Any]] = []
    chat_reserves: list[dict[str, Any]] = []

    class FallbackOptimizer:
        provider = "openai"
        model = "gpt-4.1-mini"
        max_output_tokens = 1800

        def optimize_prompt(self, payload):
            return {
                "optimized_prompt": payload["prompt"],
                "fallback_reason": "already_clear",
                "_billable_usages": [
                    {
                        "provider": self.provider,
                        "model": self.model,
                        "input_tokens": 12,
                        "output_tokens": 4,
                        "output_text": payload["prompt"],
                        "provider_cost_usd": 0.0001,
                    }
                ],
            }

    def reserve_optimize(**kwargs):
        optimize_reserves.append(kwargs)
        return optimize_reservation

    def reserve_chat(**kwargs):
        chat_reserves.append(kwargs)
        return _test_reservation("ask")

    monkeypatch.setenv("ENABLE_PROMPT_OPTIMIZATION", "true")
    monkeypatch.setattr(optimize_route, "API_DB_ENABLED", True)
    monkeypatch.setattr(optimize_route, "_get_optimizer", lambda: FallbackOptimizer())
    monkeypatch.setattr(optimize_route, "_reserve_subscription_usage", reserve_optimize)
    monkeypatch.setattr(
        optimize_route,
        "_finalize_subscription_usage",
        lambda **kwargs: optimize_settles.append(kwargs),
    )
    monkeypatch.setattr(chat_route, "_reserve_subscription_usage", reserve_chat)
    monkeypatch.setattr(chat_route, "_finalize_subscription_usage", lambda **_kwargs: None)

    optimize_response = client.post(
        "/v1/optimize",
        json={"prompt": "already clear prompt"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    chat_response = client.post(
        "/v1/chat",
        json={
            "prompt": optimize_response.json()["optimized_prompt"],
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert optimize_response.status_code == 200
    assert optimize_response.json()["optimization_status"] == "kept_original"
    assert chat_response.status_code == 200
    assert optimize_reserves[0]["optimization_enabled"] is True
    assert optimize_reserves[0]["model_attempt_count"] == 2
    assert optimize_reserves[0]["max_output_tokens"] == 1800
    assert optimize_settles[0]["optimization_performed"] is True
    assert "optimization_enabled" not in chat_reserves[0]


def test_compare_rejects_too_many_targets(client):
    payload = {
        "prompt": "hello",
        "targets": [
            {"provider": "openai"},
            {"provider": "gemini"},
            {"provider": "deepseek"},
            {"provider": "grok"},
            {"provider": "openai"},
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code in (400, 422)


def test_compare_requires_min_two_targets(client):
    payload = {
        "prompt": "hello",
        "targets": [{"provider": "openai"}],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code in (400, 422)


@pytest.mark.parametrize(
    "exc, expected_code, expected_retryable",
    [
        (TimeoutError("timed out"), "timeout", True),
        (Exception("401 Unauthorized"), "auth", False),
        (Exception("429 Too Many Requests"), "rate_limit", True),
        (Exception("400 Bad Request"), "bad_request", False),
        (Exception("503 Service Unavailable"), "provider_error", True),
    ],
)
def test_error_normalization(exc, expected_code, expected_retryable):
    dummy = DummyClient()
    err: NormalizedError = dummy._normalize_error(exc, provider="test")  # type: ignore
    assert err.code == expected_code
    assert err.retryable == expected_retryable


def test_provider_503_high_demand_error_is_classified_as_transient_capacity():
    dummy = DummyClient()
    err: NormalizedError = dummy._normalize_error(
        Exception(
            "503 UNAVAILABLE. {'error': {'message': 'This model is currently "
            "experiencing high demand. Please try again later.'}}"
        ),
        provider="gemini",
    )

    assert err.code == "provider_error"
    assert err.retryable is True
    assert (
        err.message
        == "This model is temporarily busy. Try again shortly or switch to another model."
    )
    assert err.details["kind"] == "transient_capacity"
    assert err.details["status_code"] == 503


def test_compare_dto_mapping_smoke():
    """
    Validates CompareResponseDTO mapping works against a MultiUnifiedResponse-like object.
    (We make the fake match the DTO-required attributes.)
    """
    r1 = UnifiedResponse(
        request_id="r1",
        text="A",
        provider="openai",
        model="gpt-4o-mini",
        latency_ms=10,
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        estimated_cost=0.00001,
        finish_reason="stop",
        error=None,
        metadata={},
    )
    r2 = UnifiedResponse(
        request_id="r2",
        text="B",
        provider="gemini",
        model="gemini-2.5-flash-lite",
        latency_ms=11,
        token_usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        estimated_cost=0.00002,
        finish_reason="stop",
        error=None,
        metadata={},
    )

    mur = FakeMultiUnifiedResponse(
        request_id="mur_1",
        request_group_id="grp_1",
        prompt="hello",
        responses=[r1, r2],
        success_count=2,
        failure_count=0,
        error_count=0,
        total_tokens=6,
        total_cost=0.00003,
    )

    dto = CompareResponseDTO.from_multi_unified_response(mur)
    assert dto is not None


def test_chat_dto_uses_requested_model_for_versioned_served_model_credits():
    response = UnifiedResponse(
        request_id="versioned-openai-chat-response",
        text="OpenAI answer",
        provider="openai",
        model="gpt-4o-mini-2024-07-18",
        requested_model="gpt-4o-mini",
        served_model="gpt-4o-mini-2024-07-18",
        pricing_model="gpt-4o-mini-2024-07-18",
        latency_ms=10,
        token_usage=TokenUsage(
            prompt_tokens=144,
            completion_tokens=422,
            total_tokens=566,
        ),
        estimated_cost=0.0,
        finish_reason="stop",
        error=None,
        metadata={},
    )

    dto = ChatResponseDTO.from_unified_response(response)

    assert dto.requested_model == "gpt-4o-mini"
    assert dto.served_model == "gpt-4o-mini-2024-07-18"
    assert dto.pricing_model == "gpt-4o-mini-2024-07-18"
    assert dto.ai_credits == 916
    assert dto.credit_usage_estimated is False


def test_compare_dto_uses_requested_model_for_versioned_served_model_credits():
    responses = [
        UnifiedResponse(
            request_id="versioned-openai-response",
            text="OpenAI answer",
            provider="openai",
            model="gpt-4o-mini-2024-07-18",
            requested_model="gpt-4o-mini",
            served_model="gpt-4o-mini-2024-07-18",
            pricing_model="gpt-4o-mini-2024-07-18",
            latency_ms=10,
            token_usage=TokenUsage(
                prompt_tokens=144,
                completion_tokens=422,
                total_tokens=566,
            ),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        ),
        UnifiedResponse(
            request_id="deepseek-response",
            text="DeepSeek answer",
            provider="deepseek",
            model="deepseek-v4-flash",
            requested_model="deepseek-v4-flash",
            served_model="deepseek-v4-flash",
            pricing_model="deepseek-v4-flash",
            latency_ms=11,
            token_usage=TokenUsage(
                prompt_tokens=222,
                completion_tokens=757,
                total_tokens=979,
            ),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        ),
    ]
    mur = FakeMultiUnifiedResponse(
        request_id="versioned-compare",
        request_group_id="versioned-compare-group",
        prompt="Compare these answers",
        responses=responses,
        success_count=2,
        failure_count=0,
        error_count=0,
        total_tokens=1545,
        total_cost=0.0,
    )

    dto = CompareResponseDTO.from_multi_unified_response(mur)

    assert [item.ai_credits for item in dto.responses] == [916, 868]
    assert [item.credit_usage_estimated for item in dto.responses] == [False, False]
    assert dto.total_ai_credits == 1784


def test_chat_dto_adds_formula_based_research_credits():
    base = UnifiedResponse(
        request_id="research-chat",
        text="Answer",
        provider="openai",
        model="gpt-4o-mini",
        latency_ms=10,
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        estimated_cost=0.00001,
        finish_reason="stop",
        error=None,
        metadata={},
    )
    fresh = replace(
        base,
        metadata={
            "research_used": True,
            "research_reused": False,
            "research_provider_credits_used": 3,
        },
    )

    base_dto = ChatResponseDTO.from_unified_response(base)
    fresh_dto = ChatResponseDTO.from_unified_response(fresh)

    assert fresh_dto.ai_credits - base_dto.ai_credits == 15_000


def test_compare_dto_adds_shared_research_charge_once():
    metadata = {
        "research_used": True,
        "research_reused": False,
        "research_provider_credits_used": 3,
    }
    responses = [
        UnifiedResponse(
            request_id=f"research-compare-{index}",
            text="Answer",
            provider="openai",
            model="gpt-4o-mini",
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata=metadata,
        )
        for index in range(2)
    ]
    mur = FakeMultiUnifiedResponse(
        request_id="research-compare",
        request_group_id="research-compare-group",
        prompt="latest facts",
        responses=responses,
        success_count=2,
        failure_count=0,
        error_count=0,
        total_tokens=6,
        total_cost=0.00002,
    )

    dto = CompareResponseDTO.from_multi_unified_response(mur)

    assert dto.total_ai_credits - sum(item.ai_credits for item in dto.responses) == 15_000


def test_compare_never_returns_500(client):
    payload = {
        "prompt": "Explain async/await",
        "targets": [{"provider": "openai"}, {"provider": "gemini"}],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code < 500


def test_chat_stream_returns_ndjson_events(client, caplog):
    caplog.set_level(logging.INFO, logger="server.stream_observability")
    payload = {
        "prompt": "hello",
        "provider": "openai",
        "model": "gpt-4o-mini",
    }
    r = client.post(
        "/v1/chat/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")

    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    event_types = [e.get("type") for e in events]

    assert "start" in event_types
    assert "response_done" in event_types
    assert "done" in event_types
    log_events = [getattr(record, "extra_fields", {}).get("event") for record in caplog.records]
    assert "chat.stream.opened" in log_events
    assert "chat.stream.start_event_sent" in log_events
    assert "chat.stream.provider_call_started" in log_events
    assert "chat.stream.provider_call_completed" in log_events
    assert "chat.stream.response_done_sent" in log_events
    assert "chat.stream.done_sent" in log_events


def test_context_guardrail_soft_trims_oversized_history():
    from server.schemas.requests import ConversationHistoryItem, UserContextRequest
    from server.utils import MAX_CONTEXT_CHARS, validate_and_trim_context

    context = UserContextRequest(
        conversation_history=[
            ConversationHistoryItem(
                role="user" if index % 2 == 0 else "assistant",
                content=f"message-{index}-" + ("x" * 5000),
            )
            for index in range(12)
        ]
    )

    result = validate_and_trim_context(context)

    assert result is context
    assert context.conversation_history
    retained_content = [item.content for item in context.conversation_history]
    assert sum(len(content) for content in retained_content) <= MAX_CONTEXT_CHARS
    assert any("message-11-" in content for content in retained_content)
    assert all("message-0-" not in content for content in retained_content)


def test_chat_stream_soft_trims_oversized_context_instead_of_rejecting(client, app):
    from server.utils import MAX_CONTEXT_CHARS

    payload = {
        "prompt": "continue",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "context": {
            "session_id": "long-chat-session",
            "conversation_history": [
                {
                    "role": "user" if index % 2 == 0 else "assistant",
                    "content": f"long-context-message-{index}-" + ("x" * 5000),
                }
                for index in range(12)
            ],
        },
    }

    r = client.post(
        "/v1/chat/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    sent_context = app.state.fake_orchestrator.last_ask_context
    assert sent_context is not None
    retained_content = [item["content"] for item in sent_context.conversation_history]
    assert sum(len(content) for content in retained_content) <= MAX_CONTEXT_CHARS
    assert any("long-context-message-11-" in content for content in retained_content)


def test_chat_preserves_empty_length_response_as_incomplete(client, app):
    def _empty_success(*_args, **_kwargs):
        return UnifiedResponse(
            request_id="req_empty_success_chat",
            text="",
            provider="openai",
            model="gpt-5.1",
            latency_ms=8,
            token_usage=TokenUsage(prompt_tokens=200, completion_tokens=500, total_tokens=700),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )

    app.state.fake_orchestrator.ask = _empty_success

    payload = {
        "prompt": "Long answer please",
        "provider": "openai",
        "model": "gpt-5.1",
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200

    body = r.json()
    assert body["text"] == ""
    assert body["finish_reason"] == "length"
    assert body["error"] is None
    assert body["completion_status"] == "incomplete"
    assert body["stop_cause"] == "token_limit"
    assert body["retry_with_more_room"]["available"] is True


def test_chat_sanitizes_raw_transient_provider_error(client, app):
    raw_message = (
        "Provider error: 503 UNAVAILABLE. {'error': {'code': 503, 'message': "
        "'This model is currently experiencing high demand. Spikes in demand are usually temporary.'}}"
    )

    def _transient_error(*_args, **_kwargs):
        return UnifiedResponse(
            request_id="req_transient_chat",
            text="",
            provider="gemini",
            model="gemini-2.5-flash",
            latency_ms=20,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message=raw_message,
                provider="gemini",
                retryable=True,
            ),
            metadata={},
        )

    app.state.fake_orchestrator.ask = _transient_error

    r = client.post(
        "/v1/chat",
        json={"prompt": "hello", "provider": "gemini", "model": "gemini-2.5-flash"},
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200

    body = r.json()
    assert (
        body["error"]["message"]
        == "This model is temporarily busy. Try again shortly or switch to another model."
    )
    assert body["error"]["details"]["kind"] == "transient_capacity"
    assert "UNAVAILABLE" not in body["error"]["message"]


def test_chat_stream_preserves_empty_length_response_as_incomplete(client, app):
    def _empty_success(*_args, **_kwargs):
        return UnifiedResponse(
            request_id="req_empty_success_chat_stream",
            text="",
            provider="openai",
            model="gpt-5.1",
            latency_ms=8,
            token_usage=TokenUsage(prompt_tokens=200, completion_tokens=500, total_tokens=700),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )

    app.state.fake_orchestrator.ask = _empty_success

    payload = {
        "prompt": "Long answer please",
        "provider": "openai",
        "model": "gpt-5.1",
    }
    r = client.post(
        "/v1/chat/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]

    line_events = [event for event in events if event.get("type") == "line"]
    assert line_events == []

    done = next((event for event in events if event.get("type") == "response_done"), None)
    assert done is not None
    payload_json = done["response"]
    assert payload_json["text"] == ""
    assert payload_json["finish_reason"] == "length"
    assert payload_json["error"] is None
    assert payload_json["completion_status"] == "incomplete"
    assert payload_json["stop_cause"] == "token_limit"


def test_chat_stream_sanitizes_raw_transient_provider_error(client, app):
    raw_message = (
        "Provider error: 503 UNAVAILABLE. {'error': {'message': "
        "'This model is currently experiencing high demand. Please try again later.'}}"
    )

    def _transient_error(*_args, **_kwargs):
        return UnifiedResponse(
            request_id="req_transient_chat_stream",
            text="",
            provider="gemini",
            model="gemini-2.5-flash",
            latency_ms=20,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message=raw_message,
                provider="gemini",
                retryable=True,
            ),
            metadata={},
        )

    app.state.fake_orchestrator.ask = _transient_error

    r = client.post(
        "/v1/chat/stream",
        json={"prompt": "hello", "provider": "gemini", "model": "gemini-2.5-flash"},
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    rendered_text = "\n".join(
        str(event.get("text", "")) for event in events if event.get("type") == "line"
    )
    assert (
        rendered_text
        == "This model is temporarily busy. Try again shortly or switch to another model."
    )
    assert "UNAVAILABLE" not in rendered_text
    assert "high demand" not in rendered_text


def test_compare_preserves_empty_length_response_as_incomplete(client, app):
    def _compare_with_blank_success(
        prompt: str, models_list: List[Dict[str, Any]], context: Any = None, **kwargs
    ):
        empty = UnifiedResponse(
            request_id="req_cmp_empty",
            text="",
            provider=models_list[0]["provider"],
            model=models_list[0].get("model", "gpt-5.1"),
            latency_ms=7,
            token_usage=TokenUsage(prompt_tokens=120, completion_tokens=500, total_tokens=620),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )
        ok = UnifiedResponse(
            request_id="req_cmp_ok",
            text="second model answer",
            provider=models_list[1]["provider"],
            model=models_list[1].get("model", "gemini-2.5-flash"),
            latency_ms=9,
            token_usage=TokenUsage(prompt_tokens=12, completion_tokens=18, total_tokens=30),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )
        return FakeMultiUnifiedResponse(
            request_id="req_compare_empty_shape",
            request_group_id="grp_compare_empty_shape",
            prompt=prompt,
            responses=[empty, ok],
            success_count=2,
            failure_count=0,
            error_count=0,
            total_tokens=650,
            total_cost=0.0001,
        )

    app.state.fake_orchestrator.compare = _compare_with_blank_success

    payload = {
        "prompt": "compare this",
        "targets": [
            {"provider": "openai", "model": "gpt-5.1"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200

    body = r.json()
    assert body["success_count"] == 2
    assert body["error_count"] == 0
    first = body["responses"][0]
    assert first["text"] == ""
    assert first["finish_reason"] == "length"
    assert first["error"] is None
    assert first["completion_status"] == "incomplete"
    assert first["stop_cause"] == "token_limit"


def test_compare_sanitizes_raw_transient_provider_error(client, app):
    raw_message = (
        "Provider error: 503 UNAVAILABLE. {'error': {'message': "
        "'This model is currently experiencing high demand. Please try again later.'}}"
    )

    def _compare_with_transient_error(
        prompt: str,
        models_list: List[Dict[str, Any]],
        context: Any = None,
        **kwargs,
    ):
        failed = UnifiedResponse(
            request_id="req_cmp_transient",
            text="",
            provider=models_list[0]["provider"],
            model=models_list[0].get("model", "gemini-2.5-flash"),
            latency_ms=15,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message=raw_message,
                provider=models_list[0]["provider"],
                retryable=True,
            ),
            metadata={},
        )
        ok = UnifiedResponse(
            request_id="req_cmp_still_ok",
            text="second model answer",
            provider=models_list[1]["provider"],
            model=models_list[1].get("model", "openai-model"),
            latency_ms=9,
            token_usage=TokenUsage(prompt_tokens=12, completion_tokens=18, total_tokens=30),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )
        return FakeMultiUnifiedResponse(
            request_id="req_compare_transient_shape",
            request_group_id="grp_compare_transient_shape",
            prompt=prompt,
            responses=[failed, ok],
            success_count=1,
            failure_count=1,
            error_count=1,
            total_tokens=30,
            total_cost=0.0001,
        )

    app.state.fake_orchestrator.compare = _compare_with_transient_error

    r = client.post(
        "/v1/compare",
        json={
            "prompt": "compare",
            "targets": [
                {"provider": "gemini", "model": "gemini-2.5-flash"},
                {"provider": "openai", "model": "gpt-4o-mini"},
            ],
        },
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    body = r.json()
    first = body["responses"][0]
    assert (
        first["error"]["message"]
        == "This model is temporarily busy. Try again shortly or switch to another model."
    )
    assert first["error"]["details"]["kind"] == "transient_capacity"
    assert "UNAVAILABLE" not in first["error"]["message"]
    assert body["success_count"] == 1
    assert body["error_count"] == 1


def test_compare_stream_returns_ndjson_events(client, caplog):
    caplog.set_level(logging.INFO, logger="server.stream_observability")
    payload = {
        "prompt": "hello",
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")

    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    event_types = [e.get("type") for e in events]

    assert "start" in event_types
    assert event_types.count("response_done") >= 2
    assert "done" in event_types
    log_events = [getattr(record, "extra_fields", {}).get("event") for record in caplog.records]
    assert "compare.stream.opened" in log_events
    assert "compare.stream.start_event_sent" in log_events
    assert "compare.stream.provider_call_started" in log_events
    assert "compare.stream.provider_call_completed" in log_events
    assert "compare.stream.response_done_sent" in log_events
    assert "compare.stream.done_sent" in log_events


def test_compare_stream_preserves_empty_length_response_as_incomplete(client, app):
    def _ask_with_one_blank(
        prompt: str, model_type: Optional[str] = None, context: Any = None, **kwargs
    ):
        provider = model_type or "openai"
        model = kwargs.get("model_name") or kwargs.get("model") or "unknown"
        if provider == "openai":
            return UnifiedResponse(
                request_id="req_cmp_stream_empty",
                text="",
                provider=provider,
                model=model,
                latency_ms=6,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=500, total_tokens=600),
                estimated_cost=0.0,
                finish_reason="length",
                error=None,
                metadata={"endpoint": "chat.completions"},
            )

        return UnifiedResponse(
            request_id="req_cmp_stream_ok",
            text="healthy fallback answer",
            provider=provider,
            model=model,
            latency_ms=6,
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    app.state.fake_orchestrator.ask = _ask_with_one_blank

    payload = {
        "prompt": "compare stream",
        "targets": [
            {"provider": "openai", "model": "gpt-5.1"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]

    response_done_events = [event for event in events if event.get("type") == "response_done"]
    assert len(response_done_events) >= 2
    openai_done = next(
        (
            event
            for event in response_done_events
            if event.get("response", {}).get("provider") == "openai"
        ),
        None,
    )
    assert openai_done is not None
    openai_resp = openai_done["response"]
    assert openai_resp["text"] == ""
    assert openai_resp["finish_reason"] == "length"
    assert openai_resp["error"] is None
    assert openai_resp["completion_status"] == "incomplete"
    assert openai_resp["stop_cause"] == "token_limit"


def test_compare_stream_done_payload_counts_incomplete_successes_and_uses_profile(client, app):
    observed_kwargs: dict[str, Any] = {}

    def _ask_with_one_blank(
        prompt: str, model_type: Optional[str] = None, context: Any = None, **kwargs
    ):
        observed_kwargs.clear()
        observed_kwargs.update(kwargs)
        provider = model_type or "openai"
        model = kwargs.get("model_name") or kwargs.get("model") or "unknown"
        if provider == "openai":
            return UnifiedResponse(
                request_id="req_cmp_stream_empty_count",
                text="",
                provider=provider,
                model=model,
                latency_ms=6,
                token_usage=TokenUsage(prompt_tokens=100, completion_tokens=500, total_tokens=600),
                estimated_cost=0.0,
                finish_reason="length",
                error=None,
                metadata={"endpoint": "chat.completions"},
            )

        return UnifiedResponse(
            request_id="req_cmp_stream_ok_count",
            text="healthy fallback answer",
            provider=provider,
            model=model,
            latency_ms=6,
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    app.state.fake_orchestrator.ask = _ask_with_one_blank

    payload = {
        "prompt": "compare stream aggregate",
        "generation": {"profile": "balanced"},
        "targets": [
            {"provider": "openai", "model": "gpt-5.1"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert observed_kwargs.get("max_tokens") == 4096

    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    done = next((event for event in events if event.get("type") == "done"), None)
    assert done is not None
    compare_payload = done.get("compare", {})
    assert compare_payload.get("success_count") == 2
    assert compare_payload.get("error_count") == 0


def test_compare_stream_allows_context_with_three_targets(client):
    payload = {
        "prompt": "Where is it most discussed in Canada?",
        "context": {
            "session_id": "session-compare-1",
            "new_session": False,
            "conversation_history": [
                {"role": "user", "content": "Tell me about Donald Trump"},
                {"role": "assistant", "content": "Trump is a former U.S. president."},
            ],
        },
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
            {"provider": "deepseek", "model": "deepseek-chat"},
        ],
    }
    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")

    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    event_types = [e.get("type") for e in events]
    assert event_types.count("response_done") >= 3
    assert "done" in event_types


def test_chat_accepts_auto_routing_without_provider(client, app):
    payload = {
        "prompt": "Give me a quick summary of async await",
        "routing": {"smart_mode": True, "research_mode": False},
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("provider") in set(get_provider_ids())
    assert isinstance(body.get("model"), str)
    assert app.state.fake_orchestrator.last_ask_model_type is None
    assert app.state.fake_orchestrator.last_ask_kwargs.get("routing_mode") == "smart"


def test_chat_rejects_model_without_provider(client):
    payload = {
        "prompt": "hello",
        "model": "gpt-4o-mini",
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 422


def test_chat_rejects_explicit_limit_above_model_cap(client, app):
    payload = {
        "prompt": "Give me a summary",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "max_tokens": 99999,
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_generation_budget"


def test_compare_rejects_explicit_limit_above_model_cap(client, app):
    payload = {
        "prompt": "compare",
        "max_tokens": 77777,
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_generation_budget"


def test_chat_uses_same_profile_limit_for_billing_and_provider(
    client,
    app,
    monkeypatch,
):
    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    reservations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_route,
        "_reserve_subscription_usage",
        lambda **kwargs: reservations.append(kwargs) or _test_reservation("ask"),
    )
    monkeypatch.setattr(
        chat_route,
        "_finalize_subscription_usage",
        lambda **_kwargs: None,
    )

    response = client.post(
        "/v1/chat",
        json={
            "prompt": "Give me a concise, structured summary",
            "initial_query": "Give me a summary",
            "credit_activity_id": "activity-chat",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "generation": {"profile": "balanced"},
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert reservations[0]["max_output_tokens"] == 4096
    assert reservations[0]["initial_query"] == "Give me a summary"
    assert reservations[0]["credit_activity_id"] == "activity-chat"
    assert app.state.fake_orchestrator.last_ask_kwargs["max_tokens"] == 4096


def test_compare_uses_same_profile_limit_for_billing_and_provider(
    client,
    app,
    monkeypatch,
):
    _, compare_route = _enable_subscription_route_test_mode(monkeypatch)
    reservations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        compare_route,
        "_reserve_subscription_usage",
        lambda **kwargs: reservations.append(kwargs) or _test_reservation("compare"),
    )
    monkeypatch.setattr(
        compare_route,
        "_finalize_subscription_usage",
        lambda **_kwargs: None,
    )

    response = client.post(
        "/v1/compare",
        json={
            "prompt": "Compare these options in a table",
            "initial_query": "Compare these",
            "credit_activity_id": "activity-compare",
            "generation": {"profile": "balanced"},
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
            ],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert reservations[0]["max_output_tokens_by_target"] == {
        "openai:gpt-4o-mini": 4096,
        "gemini:gemini-2.5-flash": 4096,
    }
    assert reservations[0]["initial_query"] == "Compare these"
    assert reservations[0]["credit_activity_id"] == "activity-compare"
    assert app.state.fake_orchestrator.last_compare_kwargs["_per_client_generation"] == {
        "openai:gpt-4o-mini": {
            "max_tokens": 4096,
            "reasoning_mode": "off",
            "reasoning_effort": "none",
        },
        "gemini:gemini-2.5-flash": {
            "max_tokens": 4096,
            "reasoning_mode": "off",
            "reasoning_effort": "none",
        },
    }


def test_chat_stream_auto_routing_includes_selected_target(client, app):
    payload = {
        "prompt": "Write a small Python function",
        "routing": {"smart_mode": True},
    }
    r = client.post(
        "/v1/chat/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    start_event = next((e for e in events if e.get("type") == "start"), None)
    assert start_event is not None
    assert start_event.get("provider") in set(get_provider_ids())
    assert isinstance(start_event.get("model"), str)
    assert app.state.fake_orchestrator.last_ask_model_type is None
    assert app.state.fake_orchestrator.last_ask_kwargs.get("routing_mode") == "smart"


def test_chat_web_mode_keeps_prompt_clean_and_returns_sources_from_orchestrator(client, app):
    payload = {
        "prompt": "Latest AI policy updates",
        "routing": {"smart_mode": True, "research_mode": True},
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    payload_json = r.json()
    assert app.state.fake_orchestrator.last_ask_prompt == "Latest AI policy updates"
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "on"
    assert payload_json["web_source_items"] == [
        {"title": "Example Source", "url": "https://example.com/report"}
    ]


def test_chat_web_toggle_off_after_on_only_returns_sources_for_web_turn(client, app):
    first_payload = {
        "prompt": "Latest AI policy updates",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "context": {"session_id": "session-toggle-chat", "new_session": False},
        "routing": {"smart_mode": True, "research_mode": True},
    }
    first_response = client.post(
        "/v1/chat",
        json=first_payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert first_response.status_code == 200
    assert app.state.fake_orchestrator.last_ask_prompt == "Latest AI policy updates"
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "on"
    assert first_response.json()["web_source_items"] == [
        {"title": "Example Source", "url": "https://example.com/report"}
    ]

    second_payload = {
        "prompt": "Explain photosynthesis in one sentence.",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "context": {"session_id": "session-toggle-chat", "new_session": False},
        "routing": {"smart_mode": True, "research_mode": False},
    }
    second_response = client.post(
        "/v1/chat",
        json=second_payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert second_response.status_code == 200
    assert app.state.fake_orchestrator.last_ask_prompt == "Explain photosynthesis in one sentence."
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "off"
    assert second_response.json()["web_source_items"] == []


def test_compare_web_mode_keeps_prompt_clean_and_returns_sources_from_orchestrator(client, app):
    payload = {
        "prompt": "Recent market updates",
        "routing": {"research_mode": True},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    payload_json = r.json()
    assert app.state.fake_orchestrator.last_compare_prompt == "Recent market updates"
    assert payload_json["responses"][0]["web_source_items"] == [
        {"title": "Example Source", "url": "https://example.com/report"}
    ]


def test_chat_passes_research_mode_off_to_orchestrator_when_web_toggle_off(client, app):
    payload = {
        "prompt": "Can humans survive a nuclear war?",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "routing": {"smart_mode": True, "research_mode": False},
    }
    r = client.post(
        "/v1/chat",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "off"


def test_chat_stream_passes_research_mode_off_to_orchestrator_when_web_toggle_off(client, app):
    payload = {
        "prompt": "Can humans survive a nuclear war?",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "routing": {"smart_mode": True, "research_mode": False},
    }
    r = client.post(
        "/v1/chat/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "off"


def test_compare_passes_research_mode_to_orchestrator(client, app):
    payload = {
        "prompt": "Recent market updates",
        "routing": {"research_mode": True},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert app.state.fake_orchestrator.last_compare_kwargs.get("research_mode") == "on"
    assert "routing_mode" not in app.state.fake_orchestrator.last_compare_kwargs


def test_compare_stream_passes_research_mode_off_to_orchestrator(client, app):
    payload = {
        "prompt": "Recent market updates",
        "routing": {"smart_mode": True, "research_mode": False},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert r.status_code == 200
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "off"
    assert "routing_mode" not in app.state.fake_orchestrator.last_ask_kwargs


def test_compare_stream_prepares_shared_research_context_once(client, app):
    payload = {
        "prompt": (
            "List the top-grossing or most popular films of the 1990s, specifying "
            "the criteria used for ranking."
        ),
        "context": {
            "session_id": "session-bollywood-followup",
            "new_session": False,
            "conversation_history": [
                {
                    "role": "user",
                    "content": "how has bollywood transitioned over the years in term of movies",
                },
                {
                    "role": "assistant",
                    "content": "Bollywood shifted across eras and became romance-heavy in the 1990s.",
                },
            ],
        },
        "routing": {"research_mode": True},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
            {"provider": "grok", "model": "grok-4-1-fast-reasoning"},
        ],
    }

    r = client.post(
        "/v1/compare/stream",
        json=payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert r.status_code == 200
    assert len(app.state.fake_orchestrator.prepare_messages_calls) == 1
    prepared = app.state.fake_orchestrator.prepare_messages_calls[0]
    assert prepared["prompt"] == payload["prompt"]
    assert prepared["research_mode"] == "on"
    assert prepared["context"].conversation_history[0]["content"].startswith("how has bollywood")
    assert "_prepared_messages" in app.state.fake_orchestrator.last_ask_kwargs


def test_compare_stream_web_toggle_off_after_on_only_returns_sources_for_web_turn(client, app):
    first_payload = {
        "prompt": "Recent market updates",
        "context": {"session_id": "session-toggle-compare-stream", "new_session": False},
        "routing": {"research_mode": True},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    first_response = client.post(
        "/v1/compare/stream",
        json=first_payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert first_response.status_code == 200
    first_events = [json.loads(line) for line in first_response.text.splitlines() if line.strip()]
    first_start = next((event for event in first_events if event.get("type") == "start"), None)
    first_done = next(
        (event for event in first_events if event.get("type") == "response_done"), None
    )
    assert first_start is not None
    assert first_done is not None
    assert first_start.get("research_mode") is True
    assert first_start.get("web_sources") == 0
    assert first_done.get("response", {}).get("web_source_items") == [
        {"title": "Example Source", "url": "https://example.com/report"}
    ]
    assert app.state.fake_orchestrator.last_ask_prompt == "Recent market updates"
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "on"

    second_payload = {
        "prompt": "Explain photosynthesis in one sentence.",
        "context": {"session_id": "session-toggle-compare-stream", "new_session": False},
        "routing": {"research_mode": False},
        "targets": [
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash"},
        ],
    }
    second_response = client.post(
        "/v1/compare/stream",
        json=second_payload,
        headers={"X-API-Key": "dev-key-1"},
        cookies={"cortex_session": "test-session-cookie"},
    )
    assert second_response.status_code == 200
    second_events = [json.loads(line) for line in second_response.text.splitlines() if line.strip()]
    second_start = next((event for event in second_events if event.get("type") == "start"), None)
    second_done = next(
        (event for event in second_events if event.get("type") == "response_done"), None
    )
    assert second_start is not None
    assert second_done is not None
    assert second_start.get("research_mode") is False
    assert second_start.get("web_sources") == 0
    assert second_done.get("response", {}).get("web_source_items") == []
    assert app.state.fake_orchestrator.last_ask_prompt == "Explain photosynthesis in one sentence."
    assert app.state.fake_orchestrator.last_ask_kwargs.get("research_mode") == "off"


def _enable_subscription_route_test_mode(monkeypatch):
    from server.routes import chat as chat_route
    from server.routes import compare as compare_route

    resolution = ApiKeyPersistenceResolution(
        user_id=UUID("11111111-1111-1111-1111-111111111111"),
        api_key_id=None,
        decision_path="test",
    )
    for route in (chat_route, compare_route):
        monkeypatch.setattr(route, "API_DB_ENABLED", True)
        monkeypatch.setattr(
            route,
            "_resolve_and_enforce_caps",
            lambda **_kwargs: resolution,
        )
        monkeypatch.setattr(
            route,
            "_resolve_runtime_byok_provider_keys",
            lambda **_kwargs: {},
        )
    monkeypatch.setattr(
        chat_route,
        "_persist_chat_interaction",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        compare_route,
        "_persist_compare_interaction",
        lambda **_kwargs: None,
    )
    return chat_route, compare_route


def _test_reservation(
    operation_type: str,
    *,
    allowed=frozenset({"economical", "standard", "advanced"}),
):
    return ReservedRequestUsage(
        reservation_id=uuid4(),
        request_id=f"test-{operation_type}",
        operation_type=operation_type,
        requested_quantities={"ai_credits": 4096 if operation_type == "ask" else 8192},
        allowed_billing_classes=allowed,
        current_plan="free",
        reset_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def _resolved_attachment(*, size_bytes: int = 1024):
    from server.attachments import ResolvedAttachment

    return ResolvedAttachment(
        file_id=uuid4(),
        usage_role="primary",
        transform_mode="auto",
        order_index=0,
        original_filename="notes.txt",
        mime_type="text/plain",
        size_bytes=size_bytes,
        status="ready",
        storage_bucket="test-bucket",
        storage_key="test-key",
    )


def test_chat_with_attachment_reserves_file_facts_and_settles_one_analysis_turn(
    client,
    monkeypatch,
):
    from server import attachments as attachments_service

    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    attachment = _resolved_attachment(size_bytes=2048)
    reserved: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []

    monkeypatch.setattr(
        attachments_service,
        "resolve_request_attachments",
        lambda **_kwargs: [attachment],
    )
    monkeypatch.setattr(
        attachments_service,
        "compatible_providers_for_attachments",
        lambda _attachments: ["openai"],
    )
    monkeypatch.setattr(
        attachments_service,
        "enforce_model_attachment_compatibility",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        attachments_service,
        "materialize_inference_attachments",
        lambda _attachments, **_kwargs: [
            {"mime_type": "text/plain", "extracted_text": "materialized file text"}
        ],
    )
    monkeypatch.setattr(
        attachments_service,
        "build_request_attachment_persistence_items",
        lambda **_kwargs: [],
    )

    def reserve(**kwargs):
        reserved.append(kwargs)
        return _test_reservation("ask")

    monkeypatch.setattr(chat_route, "_reserve_subscription_usage", reserve)
    monkeypatch.setattr(
        chat_route,
        "_finalize_subscription_usage",
        lambda **kwargs: settled.append(kwargs),
    )

    response = client.post(
        "/v1/chat",
        json={
            "prompt": "summarize this",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "attachments": [{"file_id": str(attachment.file_id)}],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert reserved[0]["attachment_count"] == 1
    assert reserved[0]["total_attachment_bytes"] == 2048
    assert reserved[0]["attachment_sizes"] == (2048,)
    assert "materialized file text" in reserved[0]["input_text"]
    assert settled[0]["file_analysis_performed"] is True


def test_attachment_provider_compatibility_error_remains_distinct_from_plan_denial(
    client,
    app,
    monkeypatch,
):
    from fastapi import HTTPException
    from server import attachments as attachments_service

    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    attachment = _resolved_attachment()
    reserve_called = False

    monkeypatch.setattr(
        attachments_service,
        "resolve_request_attachments",
        lambda **_kwargs: [attachment],
    )
    monkeypatch.setattr(
        attachments_service,
        "compatible_providers_for_attachments",
        lambda _attachments: ["openai"],
    )

    def reject_compatibility(**_kwargs):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "attachment_model_incompatible",
                "message": "The selected model does not support attachment inputs.",
            },
        )

    def reserve(**_kwargs):
        nonlocal reserve_called
        reserve_called = True
        return _test_reservation("ask")

    monkeypatch.setattr(
        attachments_service,
        "enforce_model_attachment_compatibility",
        reject_compatibility,
    )
    monkeypatch.setattr(chat_route, "_reserve_subscription_usage", reserve)

    response = client.post(
        "/v1/chat",
        json={
            "prompt": "summarize this",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "attachments": [{"file_id": str(attachment.file_id)}],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "attachment_model_incompatible"
    assert reserve_called is False
    assert app.state.fake_orchestrator.last_ask_prompt is None


def test_chat_entitlement_denial_prevents_provider_execution(client, app, monkeypatch):
    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)

    def deny_usage(**_kwargs):
        raise EntitlementDeniedError(
            EntitlementDenial(
                code="insufficient_credits",
                message="This request needs 4,096 AI credits, but only 0 remain.",
                meter="ai_credits",
                current_plan="free",
                recommended_plan="plus",
                remaining=0,
            )
        )

    monkeypatch.setattr(chat_route, "_reserve_subscription_usage", deny_usage)
    response = client.post(
        "/v1/chat",
        json={
            "prompt": "hello",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "insufficient_credits"
    assert app.state.fake_orchestrator.last_ask_prompt is None


def test_free_plan_three_target_compare_denial_prevents_provider_execution(
    client,
    app,
    monkeypatch,
):
    _, compare_route = _enable_subscription_route_test_mode(monkeypatch)

    def deny_usage(**_kwargs):
        raise EntitlementDeniedError(
            EntitlementDenial(
                code="feature_not_in_plan",
                message="The Free plan supports up to 2 Compare models.",
                feature="compare_model_count",
                current_plan="free",
                recommended_plan="pro",
            )
        )

    monkeypatch.setattr(compare_route, "_reserve_subscription_usage", deny_usage)
    response = client.post(
        "/v1/compare",
        json={
            "prompt": "compare",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
                {"provider": "deepseek", "model": "deepseek-chat"},
            ],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["feature"] == "compare_model_count"
    assert app.state.fake_orchestrator.last_compare_prompt is None


def test_compare_partial_success_settles_only_successful_target(client, app, monkeypatch):
    _, compare_route = _enable_subscription_route_test_mode(monkeypatch)
    reservation = _test_reservation("compare")
    settled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        compare_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        compare_route,
        "_finalize_subscription_usage",
        lambda **kwargs: settled.append(kwargs),
    )

    def partial_compare(prompt, models_list, context=None, **kwargs):
        del context
        success = UnifiedResponse(
            request_id="partial-success",
            text="ok",
            provider="openai",
            model="gpt-4o-mini",
            latency_ms=1,
            token_usage=TokenUsage(1, 1, 2),
            estimated_cost=0.0,
            finish_reason="stop",
            metadata={"research_used": True},
        )
        failure = UnifiedResponse(
            request_id="partial-failure",
            text="",
            provider="gemini",
            model="gemini-2.5-flash",
            latency_ms=1,
            token_usage=TokenUsage(0, 0, 0),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message="provider failed",
                provider="gemini",
                retryable=True,
            ),
            metadata={"research_used": True},
        )
        return FakeMultiUnifiedResponse(
            request_id="partial",
            request_group_id="partial-group",
            prompt=prompt,
            responses=[success, failure],
            success_count=1,
            failure_count=1,
            error_count=1,
            total_tokens=2,
            total_cost=0.0,
        )

    app.state.fake_orchestrator.compare = partial_compare
    response = client.post(
        "/v1/compare",
        json={
            "prompt": "compare",
            "routing": {"research_mode": True},
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
            ],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert len(settled) == 1
    assert [(item.provider, item.model) for item in settled[0]["successful_targets"]] == [
        ("openai", "gpt-4o-mini")
    ]
    assert settled[0]["research_provider_credits_used"] == 2
    assert settled[0]["research_usage_estimated"] is True


def test_compare_stream_partial_success_uses_aggregate_settlement(client, app, monkeypatch):
    _, compare_route = _enable_subscription_route_test_mode(monkeypatch)
    reservation = _test_reservation("compare")
    settled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        compare_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        compare_route,
        "_finalize_subscription_usage",
        lambda **kwargs: settled.append(kwargs),
    )

    def partial_ask(prompt, model_type=None, context=None, **kwargs):
        del prompt, context
        model = kwargs.get("model_name") or "unknown"
        if model_type == "gemini":
            return UnifiedResponse(
                request_id="stream-failure",
                text="",
                provider="gemini",
                model=model,
                latency_ms=1,
                token_usage=TokenUsage(0, 0, 0),
                estimated_cost=0.0,
                finish_reason="error",
                error=NormalizedError(
                    code="provider_error",
                    message="provider failed",
                    provider="gemini",
                    retryable=True,
                ),
            )
        return UnifiedResponse(
            request_id="stream-success",
            text="ok",
            provider=model_type or "openai",
            model=model,
            latency_ms=1,
            token_usage=TokenUsage(1, 1, 2),
            estimated_cost=0.0,
            finish_reason="stop",
        )

    app.state.fake_orchestrator.ask = partial_ask
    response = client.post(
        "/v1/compare/stream",
        json={
            "prompt": "compare",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
            ],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert len(settled) == 1
    assert [(item.provider, item.model) for item in settled[0]["successful_targets"]] == [
        ("openai", "gpt-4o-mini")
    ]


def test_compare_stream_settles_versioned_served_model_with_requested_identity(
    client,
    app,
    monkeypatch,
):
    _, compare_route = _enable_subscription_route_test_mode(monkeypatch)
    reservation = _test_reservation("compare")
    settled: list[dict[str, Any]] = []
    released: list[dict[str, Any]] = []
    monkeypatch.setattr(
        compare_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        compare_route,
        "_finalize_subscription_usage",
        lambda **kwargs: settled.append(kwargs),
    )
    monkeypatch.setattr(
        compare_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )

    original_ask = app.state.fake_orchestrator.ask

    def versioned_openai_ask(prompt, model_type=None, context=None, **kwargs):
        response = original_ask(prompt, model_type=model_type, context=context, **kwargs)
        if model_type != "openai":
            return response
        requested_model = kwargs.get("model_name") or "gpt-5.4-mini"
        return replace(
            response,
            model="gpt-5.4-mini-2026-03-17",
            requested_model=requested_model,
            served_model="gpt-5.4-mini-2026-03-17",
            pricing_model="gpt-5.4-mini-2026-03-17",
        )

    app.state.fake_orchestrator.ask = versioned_openai_ask
    response = client.post(
        "/v1/compare/stream",
        json={
            "prompt": "Compare Selenium reliability practices",
            "targets": [
                {"provider": "openai", "model": "gpt-5.4-mini"},
                {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
                {"provider": "deepseek", "model": "deepseek-v4-flash"},
            ],
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert response.status_code == 200
    assert events[-1]["type"] == "done"
    assert len(settled) == 1
    assert released == []
    assert [(item.provider, item.model) for item in settled[0]["successful_targets"]] == [
        ("openai", "gpt-5.4-mini"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("deepseek", "deepseek-v4-flash"),
    ]
    assert [(item.provider, item.model) for item in settled[0]["model_usages"]] == [
        ("openai", "gpt-5.4-mini"),
        ("gemini", "gemini-3.5-flash-lite"),
        ("deepseek", "deepseek-v4-flash"),
    ]


def test_compare_finalization_failure_releases_reservation(monkeypatch):
    from server.routes import compare as compare_route

    reservation = _test_reservation("compare")
    released: list[dict[str, Any]] = []
    monkeypatch.setattr(
        compare_route,
        "_finalize_subscription_usage",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("settlement unavailable")),
    )
    monkeypatch.setattr(
        compare_route,
        "_release_subscription_usage",
        lambda **kwargs: released.append(kwargs),
    )
    response = UnifiedResponse(
        request_id="compare-settlement-failure",
        text="visible answer",
        provider="openai",
        model="gpt-5.4-mini",
        latency_ms=1,
        token_usage=TokenUsage(3, 2, 5),
        estimated_cost=0.0,
        finish_reason="stop",
    )

    with pytest.raises(HTTPException):
        compare_route._finalize_compare_usage(
            reservation=reservation,
            responses=[response],
            orchestrator=object(),
        )

    assert released == [
        {
            "reservation": reservation,
            "reason": "billing_finalization_failed",
        }
    ]


def test_chat_settlement_uses_requested_model_for_versioned_served_identity(monkeypatch):
    from server.routes import chat as chat_route

    reservation = _test_reservation("ask")
    settled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_route,
        "_finalize_subscription_usage",
        lambda **kwargs: settled.append(kwargs),
    )
    response = UnifiedResponse(
        request_id="chat-versioned-model",
        text="answer",
        provider="openai",
        model="gpt-5.4-mini-2026-03-17",
        requested_model="gpt-5.4-mini",
        served_model="gpt-5.4-mini-2026-03-17",
        pricing_model="gpt-5.4-mini-2026-03-17",
        latency_ms=1,
        token_usage=TokenUsage(3, 2, 5),
        estimated_cost=0.0,
        finish_reason="stop",
    )

    chat_route._finalize_chat_usage(
        reservation=reservation,
        response=response,
        orchestrator=object(),
    )

    assert [(item.provider, item.model) for item in settled[0]["successful_targets"]] == [
        ("openai", "gpt-5.4-mini")
    ]
    assert [(item.provider, item.model) for item in settled[0]["model_usages"]] == [
        ("openai", "gpt-5.4-mini")
    ]


def test_compare_stream_partial_settlement_uses_only_emitted_successes():
    from server.routes import compare as compare_route

    emitted = UnifiedResponse(
        request_id="emitted",
        text="visible",
        provider="openai",
        model="gpt-4o-mini",
        latency_ms=1,
        token_usage=TokenUsage(1, 1, 2),
        estimated_cost=0.0,
        finish_reason="stop",
    )
    completed_but_unemitted = UnifiedResponse(
        request_id="unemitted",
        text="not visible",
        provider="gemini",
        model="gemini-2.5-flash",
        latency_ms=1,
        token_usage=TokenUsage(1, 1, 2),
        estimated_cost=0.0,
        finish_reason="stop",
    )

    assert compare_route._billable_stream_responses(
        [emitted, completed_but_unemitted],
        {0},
    ) == [emitted]


def test_chat_stream_finalizes_success_after_output_before_terminal_event(client, monkeypatch):
    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    reservation = _test_reservation("ask")
    settled: list[dict[str, Any]] = []
    event_order: list[str] = []
    original_to_ndjson = chat_route._to_ndjson
    monkeypatch.setattr(
        chat_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )

    def record_settlement(**kwargs):
        event_order.append("finalize")
        settled.append(kwargs)

    def record_event(event):
        event_order.append(event["type"])
        return original_to_ndjson(event)

    monkeypatch.setattr(
        chat_route,
        "_finalize_subscription_usage",
        record_settlement,
    )
    monkeypatch.setattr(
        chat_route,
        "_to_ndjson",
        record_event,
    )

    response = client.post(
        "/v1/chat/stream",
        json={
            "prompt": "hello",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert response.status_code == 200
    assert events[-1]["type"] == "done"
    assert len(settled) == 1
    assert event_order.index("line") < event_order.index("finalize")
    assert event_order.index("finalize") < event_order.index("done")
    assert [(item.provider, item.model) for item in settled[0]["successful_targets"]] == [
        ("openai", "gpt-4o-mini")
    ]


def test_smart_chat_passes_plan_billing_classes_as_routing_constraint(
    client,
    app,
    monkeypatch,
):
    chat_route, _ = _enable_subscription_route_test_mode(monkeypatch)
    reservation = _test_reservation("ask", allowed=frozenset({"standard"}))
    observed_constraints: list[dict[str, Any] | None] = []
    monkeypatch.setattr(
        chat_route,
        "_reserve_subscription_usage",
        lambda **_kwargs: reservation,
    )
    monkeypatch.setattr(
        chat_route,
        "_finalize_subscription_usage",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(chat_route, "_finalize_chat_usage", lambda **_kwargs: None)
    app.state.fake_orchestrator.model_billing_class = lambda _provider, model: (
        "standard" if model == "gpt-4.1-mini" else "economical"
    )

    def preview_smart_target(**kwargs):
        observed_constraints.append(kwargs.get("routing_constraints"))
        return "openai", "gpt-4.1-mini"

    app.state.fake_orchestrator.preview_smart_target = preview_smart_target
    response = client.post(
        "/v1/chat",
        json={
            "prompt": "route this",
            "routing": {"smart_mode": True, "research_mode": False},
        },
        cookies={"cortex_session": "test-session-cookie"},
    )

    assert response.status_code == 200
    assert observed_constraints[-1]["allowed_billing_classes"] == ["standard"]
    assert app.state.fake_orchestrator.last_ask_kwargs["routing_constraints"][
        "allowed_billing_classes"
    ] == ["standard"]
