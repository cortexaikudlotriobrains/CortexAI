from __future__ import annotations

import csv
import io
import shutil
import tempfile
from contextlib import redirect_stdout, suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Uuid,
    create_engine,
    insert,
    select,
    text,
)

from models.unified_response import (
    MultiUnifiedResponse,
    NormalizedError,
    TokenUsage,
    UnifiedResponse,
)
from orchestrator.core import CortexOrchestrator
from orchestrator.routing_types import ModelCandidate, Tier, ValidationResult
from server import circuit_breaker as circuit_breaker_service
from server import dependencies as deps
from server import persistence as persistence_service
from server import rate_limit as rate_limit_service
from server.app import create_app


class InspectableOrchestrator:
    def __init__(self):
        self.ask_calls = 0
        self.compare_calls = 0
        self.last_ask_kwargs: dict = {}
        self.last_compare_kwargs: dict = {}
        self.force_ask_error = False
        self.ask_text = "normal response"

    def ask(
        self, prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        self.ask_calls += 1
        self.last_ask_kwargs = kwargs
        provider = model_type or "openai"
        model = kwargs.get("model_name") or kwargs.get("model") or "gpt-4o-mini"
        if self.force_ask_error:
            return UnifiedResponse(
                request_id=f"ask_{uuid4()}",
                text="",
                provider=provider,
                model=model,
                latency_ms=5,
                token_usage=TokenUsage(prompt_tokens=12, completion_tokens=0, total_tokens=12),
                estimated_cost=0.0,
                finish_reason="error",
                error=NormalizedError(
                    code="provider_error",
                    message="provider failed",
                    provider=provider,
                    retryable=True,
                    details={},
                ),
                metadata={
                    "routing": {
                        "mode": "explicit",
                        "attempt_count": 1,
                        "attempts": [
                            {
                                "attempt_number": 1,
                                "provider": provider,
                                "model": model,
                                "validation": "provider_error",
                            }
                        ],
                    }
                },
            )

        return UnifiedResponse(
            request_id=f"ask_{uuid4()}",
            text=self.ask_text,
            provider=provider,
            model=model,
            latency_ms=7,
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            estimated_cost=0.0003,
            finish_reason="stop",
            error=None,
            metadata={
                "routing": {
                    "mode": "explicit",
                    "attempt_count": 1,
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "provider": provider,
                            "model": model,
                            "validation": "ok",
                            "latency_ms": 7,
                        }
                    ],
                }
            },
        )

    def compare(self, prompt: str, models_list, context=None, **kwargs):
        self.compare_calls += 1
        self.last_compare_kwargs = kwargs
        request_group_id = str(uuid4())
        responses: list[UnifiedResponse | None] = []
        for index, item in enumerate(models_list):
            provider = item["provider"]
            model = item.get("model") or "unknown-model"
            if index == 0:
                responses.append(
                    UnifiedResponse(
                        request_id=f"cmp_{uuid4()}",
                        text="compare ok",
                        provider=provider,
                        model=model,
                        latency_ms=11,
                        token_usage=TokenUsage(
                            prompt_tokens=25, completion_tokens=15, total_tokens=40
                        ),
                        estimated_cost=0.0004,
                        finish_reason="stop",
                        error=None,
                        metadata={
                            "routing": {
                                "mode": "smart",
                                "attempt_count": 1,
                                "attempts": [
                                    {
                                        "attempt_number": 1,
                                        "provider": provider,
                                        "model": model,
                                        "validation": "ok",
                                        "latency_ms": 11,
                                    }
                                ],
                            }
                        },
                    )
                )
            else:
                responses.append(
                    UnifiedResponse(
                        request_id=f"cmp_{uuid4()}",
                        text="",
                        provider=provider,
                        model=model,
                        latency_ms=9,
                        token_usage=TokenUsage(
                            prompt_tokens=18, completion_tokens=0, total_tokens=18
                        ),
                        estimated_cost=0.0,
                        finish_reason="error",
                        error=NormalizedError(
                            code="rate_limit",
                            message="429 Too Many Requests",
                            provider=provider,
                            retryable=True,
                            details={},
                        ),
                        metadata={
                            "routing": {
                                "mode": "smart",
                                "attempt_count": 2,
                                "fallback_used": True,
                                "attempts": [
                                    {
                                        "attempt_number": 1,
                                        "provider": provider,
                                        "model": model,
                                        "validation": "rate_limit",
                                        "error_type": "rate_limit",
                                        "error_message": "429 Too Many Requests",
                                        "latency_ms": 9,
                                    },
                                    {
                                        "attempt_number": 2,
                                        "provider": provider,
                                        "model": model,
                                        "validation": "ok",
                                        "latency_ms": 8,
                                    },
                                ],
                            }
                        },
                    )
                )
        return MultiUnifiedResponse.from_responses(
            request_group_id=request_group_id,
            prompt=prompt,
            responses=responses,
        )


def _reset_db_runtime_state(*, schema_name: str = "public") -> None:
    import db.engine as db_engine
    import db.tables as db_tables

    with suppress(Exception):
        if db_engine._ENGINE is not None:
            db_engine._ENGINE.dispose()

    db_engine._ENGINE = None
    db_tables.DB_SCHEMA = schema_name
    db_tables._tables_cache.clear()
    db_tables.metadata.clear()


def _bootstrap_b2b_schema(database_url: str) -> None:
    engine = create_engine(database_url)
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

    Table(
        "billing_accounts",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("owner_type", String(32), nullable=False),
        Column("owner_id", Uuid, nullable=False),
        Column("stripe_customer_id", String(255), unique=True),
        Column("currency", String(3), nullable=False, server_default="USD"),
        Column("country", String(2)),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Index("uq_billing_accounts_owner", "owner_type", "owner_id", unique=True),
    )

    Table(
        "subscriptions",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("provider", String(32), nullable=False, server_default="stripe"),
        Column("provider_subscription_id", String(255)),
        Column("provider_price_id", String(255)),
        Column("plan_code", String(64), nullable=False),
        Column("status", String(64), nullable=False),
        Column("current_period_start", DateTime),
        Column("current_period_end", DateTime),
        Column("cancel_at_period_end", Boolean, nullable=False, server_default="0"),
        Column("canceled_at", DateTime),
        Column("trial_end", DateTime),
        Column("grace_until", DateTime),
        Column("latest_invoice_id", String(255)),
        Column("last_provider_event_at", DateTime),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Index(
            "uq_subscriptions_provider_id",
            "provider",
            "provider_subscription_id",
            unique=True,
        ),
        Index(
            "uq_subscriptions_one_live_per_account",
            "billing_account_id",
            unique=True,
            sqlite_where=text(
                "status IN ('trialing','active','past_due','unpaid','paused','incomplete')"
            ),
        ),
    )

    Table(
        "usage_periods",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("billing_account_id", Uuid, ForeignKey("billing_accounts.id"), nullable=False),
        Column("subscription_id", Uuid, ForeignKey("subscriptions.id")),
        Column("plan_code", String(64), nullable=False),
        Column("starts_at", DateTime, nullable=False),
        Column("ends_at", DateTime, nullable=False),
        Column("status", String(32), nullable=False, server_default="active"),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        CheckConstraint("ends_at > starts_at"),
        Index("uq_usage_period_account_start", "billing_account_id", "starts_at", unique=True),
    )

    Table(
        "usage_counters",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("usage_period_id", Uuid, ForeignKey("usage_periods.id"), nullable=False),
        Column("meter_key", String(64), nullable=False),
        Column("used_quantity", BigInteger, nullable=False, server_default="0"),
        Column("reserved_quantity", BigInteger, nullable=False, server_default="0"),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        CheckConstraint("used_quantity >= 0 AND reserved_quantity >= 0"),
        Index(
            "uq_usage_counter_period_meter",
            "usage_period_id",
            "meter_key",
            unique=True,
        ),
    )

    Table(
        "usage_reservations",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("billing_account_id", Uuid, nullable=False),
        Column("usage_period_id", Uuid, nullable=False),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("state", String(32), nullable=False),
        Column("requested_quantities", JSON, nullable=False),
        Column("settled_quantities", JSON),
        Column("release_reason", String(255)),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("settled_at", DateTime),
        Column("released_at", DateTime),
        Index(
            "uq_usage_reservations_request",
            "billing_account_id",
            "request_id",
            unique=True,
        ),
    )

    Table(
        "credit_transactions",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("billing_account_id", Uuid, nullable=False),
        Column("usage_period_id", Uuid, nullable=False),
        Column("reservation_id", Uuid),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("item_index", Integer, nullable=False, default=0),
        Column("item_type", String(32), nullable=False),
        Column("provider", String(64)),
        Column("model", String(255)),
        Column("input_tokens", BigInteger, nullable=False, default=0),
        Column("normal_input_tokens", BigInteger, nullable=False, default=0),
        Column("cached_input_tokens", BigInteger, nullable=False, default=0),
        Column("cache_write_tokens", BigInteger, nullable=False, default=0),
        Column("reasoning_tokens", BigInteger, nullable=False, default=0),
        Column("output_tokens", BigInteger, nullable=False, default=0),
        Column("input_credits", BigInteger, nullable=False, default=0),
        Column("normal_input_credits", BigInteger, nullable=False, default=0),
        Column("cached_input_credits", BigInteger, nullable=False, default=0),
        Column("cache_write_credits", BigInteger, nullable=False, default=0),
        Column("output_credits", BigInteger, nullable=False, default=0),
        Column("fixed_credits", BigInteger, nullable=False, default=0),
        Column("total_credits", BigInteger, nullable=False),
        Column("uncached_equivalent_credits", BigInteger, nullable=False, default=0),
        Column("cache_savings_credits", BigInteger, nullable=False, default=0),
        Column("provider_cost_usd", Float, nullable=False, default=0.0),
        Column("usage_estimated", Boolean, nullable=False, default=False),
        Column("pricing_version", String(64), nullable=False),
        Column("metadata", JSON, nullable=False, default=dict),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Index(
            "uq_credit_transactions_reservation_item",
            "reservation_id",
            "item_index",
            unique=True,
        ),
    )

    Table(
        "api_keys",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("user_id", Uuid, nullable=False),
        Column("key_hash", String, nullable=False, unique=True),
        Column("label", String),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("key_prefix", String),
        Column("key_last4", String),
        Column("last_used_at", DateTime),
    )

    Table(
        "api_key_settings",
        metadata,
        Column("api_key_id", Uuid, primary_key=True, nullable=False),
        Column("baseline_provider", String),
        Column("baseline_model", String),
        Column("requests_per_minute", Integer),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "byok_provider_keys",
        metadata,
        Column("api_key_id", Uuid, nullable=False),
        Column("provider", String, nullable=False),
        Column("encrypted_key", String, nullable=False),
        Column("key_fingerprint", String, nullable=False),
        Column("key_last4", String),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        PrimaryKeyConstraint("api_key_id", "provider"),
    )

    Table(
        "sessions",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("user_id", Uuid, nullable=False),
        Column("title", String),
        Column("mode", String, nullable=False, default="ask"),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "messages",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("session_id", Uuid, nullable=False),
        Column("role", String, nullable=False),
        Column("content", String, nullable=False),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "llm_requests",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("user_id", Uuid, nullable=False),
        Column("session_id", Uuid),
        Column("request_id", String, nullable=False),
        Column("route_mode", String, nullable=False),
        Column("provider", String, nullable=False),
        Column("model", String, nullable=False),
        Column("prompt_sha256", String, nullable=False),
        Column("prompt_stored", Boolean, nullable=False, default=False),
        Column("prompt_text", String),
        Column("input_tokens_est", Integer),
        Column("api_key_id", Uuid),
        Column("request_group_id", Uuid),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "llm_responses",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("llm_request_id", Uuid, nullable=False),
        Column("text", String),
        Column("finish_reason", String),
        Column("latency_ms", Integer),
        Column("prompt_tokens", Integer, nullable=False, default=0),
        Column("completion_tokens", Integer, nullable=False, default=0),
        Column("total_tokens", Integer, nullable=False, default=0),
        Column("estimated_cost", Float, nullable=False, default=0.0),
        Column("error_type", String),
        Column("error_message", String),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "llm_savings",
        metadata,
        Column("llm_request_id", Uuid, primary_key=True, nullable=False),
        Column("user_id", Uuid, nullable=False),
        Column("api_key_id", Uuid),
        Column("usage_date", Date, nullable=False, server_default=text("CURRENT_DATE")),
        Column("provider", String, nullable=False),
        Column("model", String, nullable=False),
        Column("actual_cost", Float, nullable=False, default=0.0),
        Column("baseline_provider", String, nullable=False),
        Column("baseline_model", String, nullable=False),
        Column("baseline_cost", Float, nullable=False, default=0.0),
        Column("savings_amount", Float, nullable=False, default=0.0),
        Column("savings_pct", Float, nullable=False, default=0.0),
        Column("response_status", String, nullable=False, default="success"),
        Column("error_code", String),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    )

    Table(
        "usage_daily",
        metadata,
        Column("user_id", Uuid, nullable=False),
        Column("usage_date", Date, nullable=False),
        Column("total_requests", Integer, nullable=False, default=0),
        Column("total_tokens", Integer, nullable=False, default=0),
        Column("total_cost", Float, nullable=False, default=0.0),
        PrimaryKeyConstraint("user_id", "usage_date"),
    )

    Table(
        "routing_decisions",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("llm_request_id", Uuid, nullable=False, unique=True),
        Column("prompt_category", String, nullable=False),
        Column("research_mode", String, nullable=False),
        Column("routing_mode", String, nullable=False),
        Column("initial_tier", String),
        Column("final_tier", String),
        Column("attempt_count", Integer, nullable=False, default=1),
        Column("fallback_used", Boolean, nullable=False, default=False),
        Column("decision_reasons", JSON),
        Column("features", JSON),
        Column("trace", JSON),
    )

    Table(
        "routing_attempts",
        metadata,
        Column("routing_decision_id", Uuid, nullable=False),
        Column("attempt_number", Integer, nullable=False),
        Column("tier", String),
        Column("provider", String),
        Column("model", String),
        Column("validation", String),
        Column("latency_ms", Integer),
        Column("error_type", String),
        Column("error_message", String),
        PrimaryKeyConstraint("routing_decision_id", "attempt_number"),
    )

    Table(
        "cache_reuse_events",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
            nullable=False,
            server_default=text("(lower(hex(randomblob(16))))"),
        ),
        Column("user_id", Uuid, nullable=False),
        Column("request_id", String(255), nullable=False),
        Column("operation_type", String(64), nullable=False),
        Column("reused", Boolean, nullable=False),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Index(
            "uq_cache_reuse_events_user_operation_request",
            "user_id",
            "operation_type",
            "request_id",
            unique=True,
        ),
    )

    from tests.billing_schema_helpers import add_subscription_grant_schema

    add_subscription_grant_schema(metadata)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(metadata.tables["users"]).values(
                id=UUID("11111111-1111-1111-1111-111111111111"),
                email="test-session@example.com",
                display_name="Test Session User",
                is_active=True,
                auth_provider="cognito",
                auth_subject="test-session-user",
                auth_issuer="test",
            )
        )
    engine.dispose()


@pytest.fixture()
def b2b_db_client(monkeypatch):
    from server.routes import admin as admin_route
    from server.routes import byok as byok_route
    from server.routes import chat as chat_route
    from server.routes import compare as compare_route
    from server.routes import entitlements as entitlements_route
    from server.routes import history as history_route
    from server.routes import reporting as reporting_route
    from server.routes import whoami as whoami_route

    db_file = Path(tempfile.gettempdir()) / f"b2b_launch_{uuid4().hex}.sqlite"
    with suppress(FileNotFoundError):
        db_file.unlink()
    db_url = f"sqlite+pysqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    monkeypatch.setenv("DB_SCHEMA", "main")
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("AUTO_REGISTER_UNMAPPED_API_KEYS", "true")
    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("MASTER_KEY", "test-master-key-for-byok")
    monkeypatch.setenv("BASELINE_PROVIDER", "openai")
    monkeypatch.setenv("BASELINE_MODEL", "gpt-4o")
    monkeypatch.setenv("REQUESTS_PER_MINUTE", "60")
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("CIRCUIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "120")

    _reset_db_runtime_state(schema_name="main")
    _bootstrap_b2b_schema(db_url)
    rate_limit_service.reset_rate_limit_state()
    circuit_breaker_service.reset_circuit_breakers()

    old_persistence_db = persistence_service.API_DB_ENABLED
    old_chat_db = chat_route.API_DB_ENABLED
    old_compare_db = compare_route.API_DB_ENABLED
    old_history_db = history_route.API_DB_ENABLED
    old_admin_db = admin_route.API_DB_ENABLED
    old_reporting_db = reporting_route.API_DB_ENABLED
    old_byok_db = byok_route.API_DB_ENABLED
    old_whoami_db = whoami_route.API_DB_ENABLED
    old_entitlements_db = entitlements_route.API_DB_ENABLED

    persistence_service.API_DB_ENABLED = True
    chat_route.API_DB_ENABLED = True
    compare_route.API_DB_ENABLED = True
    history_route.API_DB_ENABLED = True
    admin_route.API_DB_ENABLED = True
    reporting_route.API_DB_ENABLED = True
    byok_route.API_DB_ENABLED = True
    whoami_route.API_DB_ENABLED = True
    entitlements_route.API_DB_ENABLED = True

    app = create_app()
    if hasattr(deps.get_orchestrator, "_instance"):
        delattr(deps.get_orchestrator, "_instance")
    allow_api_key_route = SimpleNamespace(require=lambda **_kwargs: None)
    monkeypatch.setattr(chat_route, "_SESSION_AUTH_GUARD", allow_api_key_route)
    monkeypatch.setattr(compare_route, "_SESSION_AUTH_GUARD", allow_api_key_route)

    fake_orch = InspectableOrchestrator()
    app.dependency_overrides[deps.get_orchestrator] = lambda: fake_orch
    client = TestClient(app)

    try:
        yield client, fake_orch
    finally:
        client.close()
        persistence_service.API_DB_ENABLED = old_persistence_db
        chat_route.API_DB_ENABLED = old_chat_db
        compare_route.API_DB_ENABLED = old_compare_db
        history_route.API_DB_ENABLED = old_history_db
        admin_route.API_DB_ENABLED = old_admin_db
        reporting_route.API_DB_ENABLED = old_reporting_db
        byok_route.API_DB_ENABLED = old_byok_db
        whoami_route.API_DB_ENABLED = old_whoami_db
        entitlements_route.API_DB_ENABLED = old_entitlements_db
        rate_limit_service.reset_rate_limit_state()
        circuit_breaker_service.reset_circuit_breakers()
        _reset_db_runtime_state(schema_name="public")
        with suppress(FileNotFoundError):
            db_file.unlink()


@pytest.mark.integration
def test_savings_computed_for_chat_compare_and_error(b2b_db_client):
    client, fake_orch = b2b_db_client

    chat_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert chat_response.status_code == 200

    compare_response = client.post(
        "/v1/compare",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "compare",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert compare_response.status_code == 200

    fake_orch.force_ask_error = True
    error_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "force error", "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert error_response.status_code == 200

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        llm_savings = get_table("llm_savings")
        rows = db.execute(select(llm_savings)).fetchall()
    finally:
        db.close()

    assert len(rows) >= 4
    saw_error_row = False
    for row in rows:
        payload = row._mapping
        baseline_cost = float(payload["baseline_cost"] or 0.0)
        actual_cost = float(payload["actual_cost"] or 0.0)
        savings_amount = float(payload["savings_amount"] or 0.0)
        expected = baseline_cost - actual_cost
        assert savings_amount == pytest.approx(expected, rel=1e-6, abs=1e-9)

        if str(payload.get("response_status", "")).lower() == "error":
            saw_error_row = True
            assert payload.get("error_code")
            assert baseline_cost == pytest.approx(0.0, abs=1e-9)
            assert savings_amount == pytest.approx(0.0, abs=1e-9)

    assert saw_error_row


def _insert_usage_summary_row(
    db,
    *,
    user_id: str,
    session_id: str,
    route_mode: str,
    provider: str,
    model: str,
    created_at: datetime,
    total_tokens: int,
    estimated_cost: float,
    latency_ms: int,
    routing_mode: str | None,
    request_group_id: str | None = None,
) -> str:
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")
    routing_decisions = get_table("routing_decisions")

    request_uuid = uuid4()
    request_id = request_uuid.hex
    db.execute(
        insert(llm_requests).values(
            id=request_id,
            user_id=user_id,
            session_id=session_id,
            request_id=f"test-{request_uuid}",
            route_mode=route_mode,
            provider=provider,
            model=model,
            prompt_sha256=f"hash-{request_id}",
            prompt_stored=False,
            prompt_text=None,
            input_tokens_est=None,
            api_key_id=None,
            request_group_id=request_group_id,
            created_at=created_at,
        )
    )
    db.execute(
        insert(llm_responses).values(
            id=uuid4().hex,
            llm_request_id=request_id,
            text="ok",
            finish_reason="stop",
            latency_ms=latency_ms,
            prompt_tokens=total_tokens // 2,
            completion_tokens=total_tokens - (total_tokens // 2),
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
            error_type=None,
            error_message=None,
            created_at=created_at,
        )
    )
    if routing_mode is not None:
        db.execute(
            insert(routing_decisions).values(
                id=uuid4().hex,
                llm_request_id=request_id,
                prompt_category="general",
                research_mode="off",
                routing_mode=routing_mode,
                initial_tier=None,
                final_tier=None,
                attempt_count=1,
                fallback_used=False,
                decision_reasons=[],
                features={},
                trace={"mode": routing_mode},
            )
        )
    return request_id


def _create_reporting_api_key_owner(db, *, label: str) -> str:
    from db import create_api_key, get_or_create_service_user

    user_id = get_or_create_service_user(db)
    _api_key_id, owner_user_id = create_api_key(
        db,
        user_id=user_id,
        raw_api_key="dev-key-1",
        label=label,
    )
    return str(owner_user_id)


@pytest.mark.integration
def test_usage_and_savings_reporting_endpoints_and_csv_export(b2b_db_client):
    client, _fake_orch = b2b_db_client

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        user_id = _create_reporting_api_key_owner(db, label="usage-savings-report-test")
        billing_accounts = get_table("billing_accounts")
        subscriptions = get_table("subscriptions")
        billing_account_id = uuid4().hex
        subscription_now = datetime.utcnow()
        db.execute(
            insert(billing_accounts).values(
                id=billing_account_id,
                owner_type="user",
                owner_id=user_id,
                currency="USD",
            )
        )
        db.execute(
            insert(subscriptions).values(
                id=uuid4().hex,
                billing_account_id=billing_account_id,
                provider="stripe",
                provider_subscription_id=f"sub_{uuid4().hex}",
                plan_code="plus",
                status="active",
                current_period_start=subscription_now - timedelta(days=1),
                current_period_end=subscription_now + timedelta(days=30),
                cancel_at_period_end=False,
            )
        )
        request_ids = [
            _insert_usage_summary_row(
                db,
                user_id=user_id,
                session_id=uuid4().hex,
                route_mode="ask",
                provider="openai",
                model="gpt-4o-mini",
                created_at=datetime(2026, 6, 10, 12, 0, 0),
                total_tokens=30,
                estimated_cost=0.0003,
                latency_ms=7,
                routing_mode="explicit",
            )
            for _ in range(2)
        ]
        llm_savings = get_table("llm_savings")
        for request_id in request_ids:
            db.execute(
                insert(llm_savings).values(
                    llm_request_id=request_id,
                    user_id=user_id,
                    api_key_id=None,
                    usage_date=date(2026, 6, 10),
                    provider="openai",
                    model="gpt-4o-mini",
                    actual_cost=0.0003,
                    baseline_provider="openai",
                    baseline_model="gpt-4o",
                    baseline_cost=0.0005,
                    savings_amount=0.0002,
                    savings_pct=0.4,
                    response_status="success",
                    error_code=None,
                    created_at=datetime(2026, 6, 10, 12, 0, 0),
                )
            )
        db.commit()
    finally:
        db.close()

    client.cookies.clear()
    usage = client.get(
        "/v1/usage",
        headers={"X-API-Key": "dev-key-1"},
        params={"group_by": "provider"},
    )
    assert usage.status_code == 200
    usage_body = usage.json()
    assert usage_body["totals"]["requests"] >= 2
    assert usage_body["totals"]["tokens"] > 0
    assert usage_body["totals"]["cost"] >= 0

    usage_csv = client.get(
        "/v1/usage/export",
        headers={"X-API-Key": "dev-key-1"},
        params={"format": "csv", "group_by": "provider"},
    )
    assert usage_csv.status_code == 200
    assert "text/csv" in usage_csv.headers.get("content-type", "")
    assert usage_csv.headers.get("x-export-columns") == "bucket,requests,tokens,cost"
    usage_lines = usage_csv.text.splitlines()
    assert usage_lines[0] == "bucket,requests,tokens,cost"
    usage_reader = csv.DictReader(io.StringIO(usage_csv.text))
    usage_rows = list(usage_reader)
    assert usage_rows
    assert usage_reader.fieldnames == ["bucket", "requests", "tokens", "cost"]

    savings = client.get(
        "/v1/savings",
        headers={"X-API-Key": "dev-key-1"},
        params={"group_by": "day"},
    )
    assert savings.status_code == 200
    savings_body = savings.json()
    assert savings_body["totals"]["requests"] >= 2
    assert savings_body["totals"]["successful_requests"] >= 0
    assert savings_body["totals"]["failed_requests"] >= 0
    assert "savings_amount" in savings_body["totals"]

    savings_csv = client.get(
        "/v1/savings/export",
        headers={"X-API-Key": "dev-key-1"},
        params={"format": "csv", "group_by": "day"},
    )
    assert savings_csv.status_code == 200
    assert "text/csv" in savings_csv.headers.get("content-type", "")
    assert (
        savings_csv.headers.get("x-export-columns")
        == "bucket,requests,actual_cost,baseline_cost,savings_amount,savings_pct"
    )
    savings_lines = savings_csv.text.splitlines()
    assert (
        savings_lines[0] == "bucket,requests,actual_cost,baseline_cost,savings_amount,savings_pct"
    )
    savings_reader = csv.DictReader(io.StringIO(savings_csv.text))
    savings_rows = list(savings_reader)
    assert savings_rows
    assert savings_reader.fieldnames == [
        "bucket",
        "requests",
        "actual_cost",
        "baseline_cost",
        "savings_amount",
        "savings_pct",
    ]


@pytest.mark.integration
def test_usage_summary_contract_from_audit_tables(b2b_db_client):
    client, _fake_orch = b2b_db_client

    from db.session import SessionLocal

    mixed_session = uuid4().hex
    compare_session = uuid4().hex
    ask_session = uuid4().hex
    previous_session = uuid4().hex
    compare_group = uuid4().hex
    db = SessionLocal()
    try:
        from db.tables import get_table

        user_id = _create_reporting_api_key_owner(db, label="usage-summary-test")

        _insert_usage_summary_row(
            db,
            user_id=user_id,
            session_id=mixed_session,
            route_mode="ask",
            provider="openai",
            model="gpt-5.4-mini",
            created_at=datetime(2026, 6, 10, 12, 0, 0),
            total_tokens=100,
            estimated_cost=0.01,
            latency_ms=1000,
            routing_mode="smart",
        )
        _insert_usage_summary_row(
            db,
            user_id=user_id,
            session_id=mixed_session,
            route_mode="compare",
            provider="claude",
            model="claude-sonnet-4-5",
            created_at=datetime(2026, 6, 11, 12, 0, 0),
            total_tokens=200,
            estimated_cost=0.02,
            latency_ms=3000,
            routing_mode="explicit",
            request_group_id=compare_group,
        )
        _insert_usage_summary_row(
            db,
            user_id=user_id,
            session_id=compare_session,
            route_mode="compare",
            provider="gemini",
            model="gemini-2.5-flash-lite",
            created_at=datetime(2026, 6, 12, 12, 0, 0),
            total_tokens=300,
            estimated_cost=0.03,
            latency_ms=5000,
            routing_mode="explicit",
            request_group_id=compare_group,
        )
        _insert_usage_summary_row(
            db,
            user_id=user_id,
            session_id=ask_session,
            route_mode="ask",
            provider="deepseek",
            model="deepseek-chat",
            created_at=datetime(2026, 6, 12, 14, 0, 0),
            total_tokens=50,
            estimated_cost=0.005,
            latency_ms=2000,
            routing_mode="explicit",
        )
        reuse_events = get_table("cache_reuse_events")
        for operation_type, reused, index in (
            ("research", False, 1),
            ("research", True, 2),
            ("prompt_optimization", True, 3),
            ("cortex_analysis", False, 4),
            ("cortex_analysis", True, 5),
        ):
            db.execute(
                insert(reuse_events).values(
                    id=uuid4().hex,
                    user_id=user_id,
                    request_id=f"reuse-{index}",
                    operation_type=operation_type,
                    reused=reused,
                    created_at=datetime(2026, 6, 12, 15, index, 0),
                )
            )
        _insert_usage_summary_row(
            db,
            user_id=user_id,
            session_id=previous_session,
            route_mode="ask",
            provider="openai",
            model="gpt-5.4-mini",
            created_at=datetime(2026, 5, 20, 12, 0, 0),
            total_tokens=325,
            estimated_cost=0.0325,
            latency_ms=1500,
            routing_mode="explicit",
        )
        db.commit()
    finally:
        db.close()

    client.cookies.clear()
    response = client.get(
        "/v1/usage/summary",
        headers={"X-API-Key": "dev-key-1"},
        params={"from": "2026-06-01", "to": "2026-06-14"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["period"] == {
        "from": "2026-06-01",
        "to": "2026-06-14",
        "label": "2026-06-01 to 2026-06-14",
    }
    assert body["totalTokens"] == 650
    assert body["totalRequests"] == 4
    assert body["totalSessions"] == 3
    assert body["avgLatencyMs"] == pytest.approx(2750.0)
    assert body["p95LatencyMs"] == pytest.approx(4700.0)
    assert body["minLatencyMs"] == pytest.approx(1000.0)
    assert body["totalSpend"] == pytest.approx(0.065)
    assert body["avgCostPerRequest"] == pytest.approx(0.01625)
    assert body["tokensDeltaPct"] == pytest.approx(100.0)
    assert body["smartRoutedTotal"] == 1
    assert body["sessionModes"] == {"askOnly": 1, "compareOnly": 1, "mixed": 1}
    assert body["switchedMidSession"] == 1
    assert body["researchRequests"] == 2
    assert body["researchReuseRate"] == pytest.approx(0.5)
    assert body["promptOptimizationReuseRate"] == pytest.approx(1.0)
    assert body["cortexAnalysisReuseRate"] == pytest.approx(0.5)

    models_by_id = {item["modelId"]: item for item in body["models"]}
    assert models_by_id["gpt-5.4-mini"] == {
        "provider": "openai",
        "modelId": "gpt-5.4-mini",
        "displayName": "GPT-5.4 Mini",
        "replies": 1,
        "viaSmart": 1,
    }
    assert models_by_id["claude-sonnet-4-5"]["displayName"] == "Claude Sonnet 4.5"
    assert models_by_id["deepseek-chat"]["displayName"] == "DeepSeek Chat"

    assert len(body["activityDaily"]) == 14
    assert body["activityDaily"][0]["date"] == "2026-06-01"
    assert body["activityDaily"][-1]["date"] == "2026-06-14"
    activity_by_date = {item["date"]: item["tokens"] for item in body["activityDaily"]}
    assert activity_by_date["2026-06-10"] == 100
    assert activity_by_date["2026-06-11"] == 200
    assert activity_by_date["2026-06-12"] == 350
    assert activity_by_date["2026-06-14"] == 0


@pytest.mark.integration
def test_usage_summary_empty_period_returns_zeroed_contract(b2b_db_client):
    client, _fake_orch = b2b_db_client

    response = client.get(
        "/v1/usage/summary",
        headers={"X-API-Key": "dev-key-1"},
        params={"from": "2026-06-01", "to": "2026-06-14"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["totalTokens"] == 0
    assert body["totalRequests"] == 0
    assert body["totalSessions"] == 0
    assert body["avgLatencyMs"] == 0.0
    assert body["p95LatencyMs"] == 0.0
    assert body["minLatencyMs"] == 0.0
    assert body["avgCostPerRequest"] == 0.0
    assert body["totalSpend"] == 0.0
    assert body["tokensDeltaPct"] == 0.0
    assert body["smartRoutedTotal"] == 0
    assert body["models"] == []
    assert body["sessionModes"] == {"askOnly": 0, "compareOnly": 0, "mixed": 0}
    assert body["switchedMidSession"] == 0
    assert len(body["activityDaily"]) == 14


@pytest.mark.integration
def test_reporting_returns_501_when_db_mode_off(monkeypatch):
    from server.routes import chat as chat_route
    from server.routes import compare as compare_route
    from server.routes import history as history_route
    from server.routes import reporting as reporting_route
    from server.routes import whoami as whoami_route

    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    app = create_app()
    reporting_route.API_DB_ENABLED = False
    chat_route.API_DB_ENABLED = False
    compare_route.API_DB_ENABLED = False
    history_route.API_DB_ENABLED = False
    whoami_route.API_DB_ENABLED = False
    client = TestClient(app)
    try:
        response0 = client.get("/v1/usage/summary", headers={"X-API-Key": "dev-key-1"})
        assert response0.status_code == 501
        response = client.get("/v1/usage", headers={"X-API-Key": "dev-key-1"})
        assert response.status_code == 501
        response2 = client.get("/v1/savings", headers={"X-API-Key": "dev-key-1"})
        assert response2.status_code == 501
        response3 = client.get("/v1/whoami", headers={"X-API-Key": "dev-key-1"})
        assert response3.status_code == 200
        body = response3.json()
        assert body["api_key_id"] is None
        assert body["user_id"] is None
    finally:
        client.close()


@pytest.mark.integration
def test_reporting_large_range_is_rejected_quickly(monkeypatch, b2b_db_client):
    client, _fake_orch = b2b_db_client
    monkeypatch.setenv("REPORT_MAX_RANGE_DAYS", "31")

    response = client.get(
        "/v1/usage/export",
        headers={"X-API-Key": "dev-key-1"},
        params={"format": "csv", "from": "2020-01-01", "to": "2026-12-31", "group_by": "day"},
    )
    assert response.status_code == 400
    assert "Date range too large" in response.text


@pytest.mark.integration
def test_byok_set_status_delete_and_runtime_usage(b2b_db_client):
    client, fake_orch = b2b_db_client

    set_response = client.post(
        "/v1/byok",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "provider_keys": {
                "openai": "sk-openai-tenant-123456",
                "gemini": "gk-gemini-tenant-654321",
            },
            "baseline_provider": "openai",
            "baseline_model": "gpt-4o",
            "requests_per_minute": 99,
        },
    )
    assert set_response.status_code == 200
    assert set(set_response.json()["updated_providers"]) == {"openai", "gemini"}

    status_response = client.get("/v1/byok/status", headers={"X-API-Key": "dev-key-1"})
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["baseline_provider"] == "openai"
    assert payload["baseline_model"] == "gpt-4o"
    assert payload["requests_per_minute"] == 99
    providers = {item["provider"]: item for item in payload["providers"]}
    assert providers["openai"]["configured"] is True
    assert "encrypted_key" not in str(payload)

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        byok_keys = get_table("byok_provider_keys")
        rows = db.execute(select(byok_keys.c.provider, byok_keys.c.encrypted_key)).fetchall()
    finally:
        db.close()

    encrypted_map = {row[0]: row[1] for row in rows}
    assert "openai" in encrypted_map
    assert "sk-openai-tenant-123456" not in str(encrypted_map["openai"])
    assert encrypted_map["openai"] != "sk-openai-tenant-123456"

    chat_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "use byok", "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert chat_response.status_code == 200
    provider_api_keys = fake_orch.last_ask_kwargs.get("provider_api_keys", {})
    assert provider_api_keys.get("openai") == "sk-openai-tenant-123456"

    delete_one = client.delete(
        "/v1/byok", headers={"X-API-Key": "dev-key-1"}, params={"provider": "openai"}
    )
    assert delete_one.status_code == 200
    assert delete_one.json()["deleted_count"] >= 1

    delete_all = client.delete("/v1/byok", headers={"X-API-Key": "dev-key-1"})
    assert delete_all.status_code == 200
    assert delete_all.json()["deleted_count"] >= 1


@pytest.mark.integration
def test_byok_requires_master_key_for_secret_storage(monkeypatch, b2b_db_client):
    client, _fake_orch = b2b_db_client
    monkeypatch.delenv("MASTER_KEY", raising=False)

    response = client.post(
        "/v1/byok",
        headers={"X-API-Key": "dev-key-1"},
        json={"provider_keys": {"openai": "sk-no-master-key-1234"}},
    )
    assert response.status_code == 500
    assert "MASTER_KEY" in response.text


@pytest.mark.integration
def test_whoami_snapshot_in_db_mode(monkeypatch, b2b_db_client):
    client, _fake_orch = b2b_db_client
    monkeypatch.setenv("DAILY_CAP_SCOPE", "api_key")
    monkeypatch.setenv("DAILY_TOKEN_CAP", "12345")
    monkeypatch.setenv("DAILY_COST_CAP", "4.56")

    set_response = client.post(
        "/v1/byok",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "provider_keys": {},
            "baseline_provider": "openai",
            "baseline_model": "gpt-4o-mini",
            "requests_per_minute": 77,
        },
    )
    assert set_response.status_code == 200

    response = client.get("/v1/whoami", headers={"X-API-Key": "dev-key-1"})
    assert response.status_code == 200

    payload = response.json()
    assert payload["api_key_id"] is not None
    assert payload["user_id"] is not None
    assert payload["plan_tier"] == "Free"
    assert payload["billing"] == {"plan_code": "free", "status": "free"}
    assert payload["storage_policy"] in {"metadata", "full"}
    assert payload["baseline"]["provider"] == "openai"
    assert payload["baseline"]["model"] == "gpt-4o-mini"
    assert payload["baseline"]["source"] == "api_key"
    assert payload["rate_limits"]["requests_per_minute"] == 77
    assert payload["rate_limits"]["daily_cap_scope"] == "api_key"
    assert payload["rate_limits"]["daily_token_cap"] == 12345
    assert float(payload["rate_limits"]["daily_cost_cap"]) == pytest.approx(4.56, rel=1e-6)
    assert payload["breakers"]["scope"] == "global_provider_model"
    assert payload["breakers"]["failure_threshold"] >= 1

    entitlements_response = client.get(
        "/v1/entitlements",
        headers={"X-API-Key": "dev-key-1"},
    )
    assert entitlements_response.status_code == 200
    entitlements_payload = entitlements_response.json()
    assert entitlements_payload["plan"]["code"] == "free"
    assert set(entitlements_payload["allowances"]) == {"ai_credits"}


@pytest.mark.integration
def test_onboard_tenant_script_does_not_print_byok_secret(b2b_db_client):
    from scripts import onboard_tenant

    _client, _fake_orch = b2b_db_client
    byok_secret = f"sk-super-secret-byok-{uuid4().hex}"
    raw_api_key = f"tenant-key-{uuid4().hex[:10]}"

    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = onboard_tenant.main(
            [
                "--email",
                f"tenant-{uuid4().hex[:8]}@example.com",
                "--name",
                "Onboard Test Tenant",
                "--api-key",
                raw_api_key,
                "--baseline-model-id",
                "openai:gpt-4o-mini",
                "--byok-openai",
                byok_secret,
            ]
        )

    assert exit_code == 0
    rendered = output.getvalue()
    assert byok_secret not in rendered
    assert "byok_providers:" in rendered
    assert "X-API-Key:" in rendered


@pytest.mark.integration
def test_proof_pack_script_metadata_mode_excludes_raw_prompt(monkeypatch, b2b_db_client):
    from scripts import generate_proof_pack

    client, _fake_orch = b2b_db_client
    monkeypatch.setenv("STORAGE_POLICY", "metadata")

    unique_prompt = f"DO_NOT_LEAK_PROMPT_{uuid4().hex}"
    chat_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": unique_prompt, "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert chat_response.status_code == 200

    whoami_response = client.get("/v1/whoami", headers={"X-API-Key": "dev-key-1"})
    assert whoami_response.status_code == 200
    user_id = whoami_response.json()["user_id"]
    assert user_id

    out_root = Path(tempfile.gettempdir()) / f"proof_pack_{uuid4().hex}"
    out_root.mkdir(parents=True, exist_ok=True)
    output = io.StringIO()
    try:
        with redirect_stdout(output):
            exit_code = generate_proof_pack.main(
                [
                    "--user-id",
                    str(user_id),
                    "--month",
                    date.today().strftime("%Y-%m"),
                    "--out-dir",
                    str(out_root),
                    "--tenant-label",
                    "metadata-check",
                ]
            )
        assert exit_code == 0

        pack_dirs = [path for path in out_root.iterdir() if path.is_dir()]
        assert len(pack_dirs) == 1
        pack_dir = pack_dirs[0]

        for name in ("summary.csv", "top_models.csv", "proof_pack.md", "proof_pack.html"):
            content = (pack_dir / name).read_text(encoding="utf-8")
            assert unique_prompt not in content
    finally:
        with suppress(Exception):
            shutil.rmtree(out_root, ignore_errors=True)


@pytest.mark.integration
def test_savings_uses_api_key_baseline_setting(b2b_db_client):
    client, _fake_orch = b2b_db_client

    baseline_response = client.post(
        "/v1/byok",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "provider_keys": {},
            "baseline_provider": "openai",
            "baseline_model": "gpt-4o-mini",
        },
    )
    assert baseline_response.status_code == 200

    chat_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "baseline check", "provider": "gemini", "model": "gemini-2.5-flash-lite"},
    )
    assert chat_response.status_code == 200

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        llm_savings = get_table("llm_savings")
        row = db.execute(
            select(llm_savings.c.baseline_provider, llm_savings.c.baseline_model)
            .order_by(llm_savings.c.created_at.desc())
            .limit(1)
        ).first()
    finally:
        db.close()

    assert row is not None
    assert row[0] == "openai"
    assert row[1] == "gpt-4o-mini"


@pytest.mark.integration
def test_rate_limit_returns_429(b2b_db_client):
    client, fake_orch = b2b_db_client
    rate_limit_service.reset_rate_limit_state()

    responses = [
        client.post(
            "/v1/chat",
            headers={"X-API-Key": "dev-key-1"},
            json={"prompt": f"request {index}", "provider": "openai", "model": "gpt-4o-mini"},
        )
        for index in range(6)
    ]
    assert all(response.status_code == 200 for response in responses[:5])
    assert responses[5].status_code == 429
    assert responses[5].json()["detail"]["code"] == "rate_limited"
    assert fake_orch.ask_calls == 5


@pytest.mark.unit
def test_circuit_breaker_opens_and_smart_fallback_skips_open_model(monkeypatch):
    monkeypatch.setenv("CIRCUIT_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("CIRCUIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("CIRCUIT_COOLDOWN_SECONDS", "120")
    circuit_breaker_service.reset_circuit_breakers()
    circuit_breaker_service.record_failure("openai", "gpt-4o-mini")

    orch = CortexOrchestrator()

    candidate_open = ModelCandidate(
        provider="openai",
        model_name="gpt-4o-mini",
        tier=Tier.T1,
        input_cost_per_1m=0.15,
        output_cost_per_1m=0.60,
        context_limit=8000,
        tags=[],
        enabled=True,
    )
    candidate_fallback = ModelCandidate(
        provider="gemini",
        model_name="gemini-2.5-flash-lite",
        tier=Tier.T1,
        input_cost_per_1m=0.05,
        output_cost_per_1m=0.20,
        context_limit=8000,
        tags=[],
        enabled=True,
    )

    def _fake_route_once_plan(**_kwargs):
        return (
            object(),
            Tier.T1,
            [candidate_open, candidate_fallback],
            {
                "mode": "smart",
                "attempts": [],
                "candidate_plan": [
                    {"provider": "openai", "model": "gpt-4o-mini", "status": "pending", "order": 1},
                    {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash-lite",
                        "status": "pending",
                        "order": 2,
                    },
                ],
                "selected_sequence": [],
                "decision_reasons": [],
                "fallback_used": False,
                "initial_tier": "T1",
                "final_tier": "T1",
            },
        )

    class _FakeClient:
        def get_completion(self, **kwargs):
            return UnifiedResponse(
                request_id=f"cb_{uuid4()}",
                text="fallback ok",
                provider="gemini",
                model="gemini-2.5-flash-lite",
                latency_ms=4,
                token_usage=TokenUsage(prompt_tokens=4, completion_tokens=3, total_tokens=7),
                estimated_cost=0.00002,
                finish_reason="stop",
                error=None,
                metadata={},
            )

    monkeypatch.setattr(orch._smart_router, "route_once_plan", _fake_route_once_plan)
    monkeypatch.setattr(
        orch._validator,
        "validate",
        lambda _features, _constraints, response: ValidationResult(
            ok=not response.is_error,
            reason="ok" if not response.is_error else "provider_error",
            severity="none",
        ),
    )
    monkeypatch.setattr(orch._model_registry, "next_tier", lambda _tier: None)
    monkeypatch.setattr(orch, "_get_client", lambda *args, **kwargs: _FakeClient())
    monkeypatch.setattr(
        orch,
        "available_providers",
        lambda **_kwargs: ["openai", "gemini"],
    )

    response = orch.ask(
        prompt="trigger smart fallback",
        routing_mode="smart",
        model_type=None,
    )

    assert response.is_success
    assert response.provider == "gemini"
    assert response.model == "gemini-2.5-flash-lite"


@pytest.mark.integration
def test_storage_policy_metadata_only_avoids_raw_prompt_and_response(monkeypatch, b2b_db_client):
    client, fake_orch = b2b_db_client
    monkeypatch.setenv("STORAGE_POLICY", "metadata")
    monkeypatch.setenv("REDACT_PII", "false")
    fake_orch.ask_text = "assistant raw text should not persist"

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "user@example.com raw prompt",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        messages = get_table("messages")
        llm_responses = get_table("llm_responses")
        credit_transactions = get_table("credit_transactions")
        latest_messages = db.execute(
            select(messages.c.content).order_by(messages.c.created_at.desc()).limit(2)
        ).fetchall()
        latest_response_text = db.execute(
            select(llm_responses.c.text).order_by(llm_responses.c.created_at.desc()).limit(1)
        ).scalar_one()
        latest_credit_metadata = db.execute(
            select(credit_transactions.c.metadata)
            .order_by(credit_transactions.c.created_at.desc())
            .limit(1)
        ).scalar_one()
    finally:
        db.close()

    joined_messages = " ".join(row[0] for row in latest_messages if row[0])
    assert "metadata-only" in joined_messages
    assert "user@example.com" not in joined_messages
    assert latest_response_text in ("", None)
    assert "initial_query" not in (latest_credit_metadata or {})


@pytest.mark.integration
def test_redaction_masks_email_phone_and_card_in_stored_content(monkeypatch, b2b_db_client):
    client, fake_orch = b2b_db_client
    monkeypatch.setenv("STORAGE_POLICY", "full")
    monkeypatch.setenv("REDACT_PII", "true")
    fake_orch.ask_text = "Email john@example.com phone 212-555-1212 card 4111 1111 1111 1111"

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Reach me at jane@example.com or 415-555-0000 and card 4242 4242 4242 4242",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
    )
    assert response.status_code == 200

    from db.session import SessionLocal
    from db.tables import get_table

    db = SessionLocal()
    try:
        messages = get_table("messages")
        llm_responses = get_table("llm_responses")
        credit_transactions = get_table("credit_transactions")
        rows = db.execute(
            select(messages.c.content).order_by(messages.c.created_at.desc()).limit(2)
        ).fetchall()
        response_text = db.execute(
            select(llm_responses.c.text).order_by(llm_responses.c.created_at.desc()).limit(1)
        ).scalar_one()
        credit_metadata = db.execute(
            select(credit_transactions.c.metadata)
            .order_by(credit_transactions.c.created_at.desc())
            .limit(1)
        ).scalar_one()
    finally:
        db.close()

    combined = (
        " ".join(row[0] for row in rows if row[0])
        + " "
        + str(response_text or "")
        + " "
        + str(credit_metadata or {})
    )
    assert "[REDACTED_EMAIL]" in combined
    assert "[REDACTED_PHONE]" in combined
    assert "[REDACTED_CARD]" in combined
    assert "jane@example.com" not in combined
    assert "john@example.com" not in combined
