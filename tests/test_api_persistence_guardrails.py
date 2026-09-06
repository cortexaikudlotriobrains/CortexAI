from contextlib import contextmanager
from contextlib import suppress
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    Index,
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
from sqlalchemy.orm import sessionmaker

import db.repository as repo
from models.unified_response import MultiUnifiedResponse, TokenUsage, UnifiedResponse
from server import dependencies as deps
from server import cortex_analysis as cortex_analysis_service
from server import persistence as persistence_service
from server import rate_limit as rate_limit_service
from server.app import create_app
from server.routes import chat as chat_route
from server.routes import compare as compare_route
from server.routes import cortex_analysis as cortex_analysis_route
from server.routes import history as history_route
from server.utils import redact_sensitive_headers


class FakeOrchestrator:
    def __init__(self):
        self.ask_calls = 0
        self.compare_calls = 0

    @staticmethod
    def _metadata_for_research_mode(research_mode: str | None) -> dict:
        if str(research_mode or "").strip().lower() != "on":
            return {
                "routing": {
                    "mode": "smart",
                    "initial_tier": "T1",
                    "final_tier": "T1",
                    "attempt_count": 1,
                    "fallback_used": False,
                    "attempts": [],
                }
            }
        return {
            "routing": {
                "mode": "smart",
                "initial_tier": "T1",
                "final_tier": "T1",
                "attempt_count": 1,
                "fallback_used": False,
                "attempts": [],
            },
            "research_used": True,
            "research_reused": False,
            "sources": [{"title": "Example Source", "url": "https://example.com/report"}],
        }

    def ask(
        self, prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        self.ask_calls += 1
        return UnifiedResponse(
            request_id="req_guardrail_ask_1",
            text="ok",
            provider=model_type or "openai",
            model=kwargs.get("model_name") or kwargs.get("model") or "gpt-4o-mini",
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata={
                **self._metadata_for_research_mode(kwargs.get("research_mode")),
                "routing": {
                    "mode": "smart",
                    "initial_tier": "T1",
                    "final_tier": "T1",
                    "attempt_count": 1,
                    "fallback_used": False,
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "tier": "T1",
                            "provider": model_type or "openai",
                            "model": kwargs.get("model_name")
                            or kwargs.get("model")
                            or "gpt-4o-mini",
                            "validation": "ok",
                            "latency_ms": 10,
                        }
                    ],
                },
            },
        )

    def compare(self, prompt: str, models_list, context=None, **kwargs):
        self.compare_calls += 1
        r1 = UnifiedResponse(
            request_id="req_guardrail_cmp_1",
            text="A",
            provider=models_list[0]["provider"],
            model=models_list[0].get("model") or "gpt-4o-mini",
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata={"routing": {"mode": "explicit", "attempts": []}},
        )
        r2 = UnifiedResponse(
            request_id="req_guardrail_cmp_2",
            text="B",
            provider=models_list[1]["provider"],
            model=models_list[1].get("model") or "gemini-2.5-flash-lite",
            latency_ms=12,
            token_usage=TokenUsage(prompt_tokens=6, completion_tokens=4, total_tokens=10),
            estimated_cost=0.00002,
            finish_reason="stop",
            error=None,
            metadata={"routing": {"mode": "explicit", "attempts": []}},
        )
        return MultiUnifiedResponse.from_responses(
            request_group_id="11111111-1111-1111-1111-111111111111",
            prompt=prompt,
            responses=[r1, r2],
        )


@dataclass
class DummySession:
    committed: int = 0
    rolled_back: int = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _fake_db_uow_factory(session_obj):
    @contextmanager
    def _fake_db_uow(*, commit_on_success=True):
        yield session_obj

    return _fake_db_uow


@pytest.fixture()
def fastapi_client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    rate_limit_service.reset_rate_limit_state()

    app = create_app()
    chat_route.API_DB_ENABLED = False
    compare_route.API_DB_ENABLED = False
    cortex_analysis_route.API_DB_ENABLED = False
    history_route.API_DB_ENABLED = False
    subscription_reservation = SimpleNamespace(
        allowed_billing_classes=frozenset({"standard", "advanced"}),
    )
    monkeypatch.setattr(
        chat_route,
        "_reserve_chat_usage",
        lambda **_kwargs: subscription_reservation,
    )
    monkeypatch.setattr(
        compare_route,
        "_reserve_compare_usage",
        lambda **_kwargs: subscription_reservation,
    )
    monkeypatch.setattr(chat_route, "_finalize_subscription_usage", lambda **_kwargs: None)
    monkeypatch.setattr(compare_route, "_finalize_subscription_usage", lambda **_kwargs: None)
    monkeypatch.setattr(chat_route, "_release_subscription_usage", lambda **_kwargs: None)
    monkeypatch.setattr(compare_route, "_release_subscription_usage", lambda **_kwargs: None)
    if hasattr(deps.get_orchestrator, "_instance"):
        delattr(deps.get_orchestrator, "_instance")
    session_user_id = UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        deps,
        "parse_session",
        lambda cookie: session_user_id if cookie else None,
    )

    fake_orch = FakeOrchestrator()
    app.dependency_overrides[deps.get_orchestrator] = lambda: fake_orch
    client = TestClient(app)
    client.cookies.set("cortex_session", "test-session-cookie")
    return client, fake_orch


def _sqlite_repo_fixture(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    metadata = MetaData()

    users = Table(
        "users",
        metadata,
        Column("id", String, primary_key=True, default=lambda: str(uuid4())),
        Column("email", String, unique=True),
        Column("display_name", String),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("auth_provider", String),
        Column("auth_subject", String),
        Column("auth_issuer", String),
    )

    api_keys = Table(
        "api_keys",
        metadata,
        Column("id", String, primary_key=True, default=lambda: str(uuid4())),
        Column("user_id", String, nullable=False),
        Column("key_hash", String, nullable=False, unique=True),
        Column("label", String),
        Column("is_active", Boolean, nullable=False, default=True),
        Column("key_prefix", String),
        Column("key_last4", String),
    )

    metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db_session = SessionLocal()

    import db.tables as db_tables

    def _fake_get_table(name: str):
        if name == "users":
            return users
        if name == "api_keys":
            return api_keys
        raise ValueError(name)

    monkeypatch.setattr(db_tables, "get_table", _fake_get_table)
    return db_session, users, api_keys


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


def _bootstrap_api_db_schema(database_url: str) -> None:
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
        Column("response_revision_root_id", Uuid),
        Column("response_revision", Integer, nullable=False, default=1),
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
        "cortex_analysis_runs",
        metadata,
        Column("id", Uuid, primary_key=True, nullable=False),
        Column("user_id", Uuid, nullable=False),
        Column("session_id", Uuid, nullable=False),
        Column("request_group_id", Uuid, nullable=False),
        Column("model", String, nullable=False),
        Column("source_fingerprint", String, nullable=False),
        Column("source_snapshot", JSON, nullable=False, default=list),
        Column("recommended_answer", String, nullable=False),
        Column("agreements", JSON, nullable=False, default=list),
        Column("disagreements", JSON, nullable=False, default=list),
        Column("disagreement_note", String),
        Column("unique_insights", JSON, nullable=False, default=list),
        Column("confidence_level", String, nullable=False),
        Column("confidence_reason", String, nullable=False),
        Column("verify_items", JSON, nullable=False, default=list),
        Column("high_stakes_domain", String),
        Column("combined_response_count", Integer, nullable=False),
        Column("failed_response_count", Integer, nullable=False, default=0),
        Column("prompt_tokens", Integer, nullable=False, default=0),
        Column("cached_input_tokens", BigInteger, nullable=False, default=0),
        Column("cache_write_tokens", BigInteger, nullable=False, default=0),
        Column("reasoning_tokens", BigInteger, nullable=False, default=0),
        Column("completion_tokens", Integer, nullable=False, default=0),
        Column("total_tokens", Integer, nullable=False, default=0),
        Column("estimated_cost", Float, nullable=False, default=0.0),
        Column("pricing_snapshot", JSON, nullable=False, default=dict),
        Column("pricing_version", String),
        Column(
            "analysis_policy_version",
            String,
            nullable=False,
            default="cortex-analysis-v1",
        ),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
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
        "billing_accounts",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("owner_type", String(32), nullable=False),
        Column("owner_id", Uuid, nullable=False),
        Column("stripe_customer_id", String(255), unique=True),
        Column("currency", String(3), nullable=False, default="USD"),
        Column("country", String(2)),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        CheckConstraint("owner_type IN ('user', 'organization')"),
        Index("uq_billing_accounts_owner", "owner_type", "owner_id", unique=True),
    )

    Table(
        "subscriptions",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, nullable=False),
        Column("provider", String(32), nullable=False, default="stripe"),
        Column("provider_subscription_id", String(255)),
        Column("provider_price_id", String(255)),
        Column("plan_code", String(64), nullable=False),
        Column("status", String(64), nullable=False),
        Column("current_period_start", DateTime),
        Column("current_period_end", DateTime),
        Column("cancel_at_period_end", Boolean, nullable=False, default=False),
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
    )

    Table(
        "usage_periods",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("billing_account_id", Uuid, nullable=False),
        Column("subscription_id", Uuid),
        Column("plan_code", String(64), nullable=False),
        Column("starts_at", DateTime, nullable=False),
        Column("ends_at", DateTime, nullable=False),
        Column("status", String(32), nullable=False, default="active"),
        Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("updated_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        CheckConstraint("ends_at > starts_at"),
        Index(
            "uq_usage_period_account_start",
            "billing_account_id",
            "starts_at",
            unique=True,
        ),
    )

    Table(
        "usage_counters",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("usage_period_id", Uuid, nullable=False),
        Column("meter_key", String(64), nullable=False),
        Column("used_quantity", BigInteger, nullable=False, default=0),
        Column("reserved_quantity", BigInteger, nullable=False, default=0),
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
        Column("id", Uuid, primary_key=True),
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
        CheckConstraint("state IN ('reserved', 'settled', 'released', 'expired')"),
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
        Column("id", Uuid, primary_key=True),
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
        "cache_reuse_events",
        metadata,
        Column(
            "id",
            Uuid,
            primary_key=True,
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

    Table(
        "billing_webhook_events",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("provider", String(32), nullable=False, default="stripe"),
        Column("provider_event_id", String(255), nullable=False),
        Column("event_type", String(255), nullable=False),
        Column("payload_hash", String(64), nullable=False),
        Column("processing_status", String(32), nullable=False),
        Column("received_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
        Column("processed_at", DateTime),
        Column("error_message", String),
        CheckConstraint("processing_status IN ('received', 'processed', 'ignored', 'failed')"),
        Index(
            "uq_billing_webhook_provider_event",
            "provider",
            "provider_event_id",
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
                email="session-user@example.com",
                display_name="Session User",
                is_active=True,
                auth_provider="session",
                auth_subject="test-session-user",
                auth_issuer="cortexai",
            )
        )
    import db.tables as db_tables

    db_tables._tables_cache.update({table.name: table for table in metadata.tables.values()})
    engine.dispose()


class DBModeCompareOrchestrator:
    def ask(
        self, prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        metadata = {}
        if str(kwargs.get("research_mode") or "").strip().lower() == "on":
            metadata = {
                "research_used": True,
                "research_reused": False,
                "sources": [{"title": "Example Source", "url": "https://example.com/report"}],
            }
        return UnifiedResponse(
            request_id=f"ask_{uuid4()}",
            text="ok",
            provider=model_type or "openai",
            model=kwargs.get("model_name") or kwargs.get("model") or "gpt-4o-mini",
            latency_ms=5,
            token_usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata=metadata,
        )

    def compare(self, prompt: str, models_list, context=None, **kwargs):
        request_group_id = str(uuid4())
        first_model = models_list[0].get("model") or "gpt-4o-mini"
        second_model = models_list[1].get("model") or "gemini-2.5-flash"

        r1 = UnifiedResponse(
            request_id=f"cmp_{uuid4()}",
            text="A",
            provider=models_list[0]["provider"],
            model=first_model,
            latency_ms=10,
            token_usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            estimated_cost=0.00001,
            finish_reason="stop",
            error=None,
            metadata={
                "routing": {
                    "mode": "smart",
                    "initial_tier": "T1",
                    "final_tier": "T1",
                    "attempt_count": 1,
                    "fallback_used": False,
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "tier": "T1",
                            "provider": models_list[0]["provider"],
                            "model": first_model,
                            "validation": "ok",
                            "latency_ms": 10,
                        }
                    ],
                }
            },
        )

        r2 = UnifiedResponse(
            request_id=f"cmp_{uuid4()}",
            text="B",
            provider=models_list[1]["provider"],
            model=second_model,
            latency_ms=12,
            token_usage=TokenUsage(prompt_tokens=6, completion_tokens=4, total_tokens=10),
            estimated_cost=0.00002,
            finish_reason="stop",
            error=None,
            metadata={
                "routing": {
                    "mode": "smart",
                    "initial_tier": "T1",
                    "final_tier": "T2",
                    "attempt_count": 2,
                    "fallback_used": True,
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "tier": "T1",
                            "provider": "openai",
                            "model": "gpt-4o-mini",
                            "validation": "rate_limit",
                            "error_type": "rate_limit",
                            "error_message": "429 Too Many Requests",
                            "latency_ms": 9,
                        },
                        {
                            "attempt_number": 2,
                            "tier": "T2",
                            "provider": models_list[1]["provider"],
                            "model": second_model,
                            "validation": "ok",
                            "latency_ms": 11,
                        },
                    ],
                }
            },
        )

        return MultiUnifiedResponse.from_responses(
            request_group_id=request_group_id,
            prompt=prompt,
            responses=[r1, r2],
        )


@pytest.fixture()
def db_mode_fastapi_client(monkeypatch):
    from server.routes import admin as admin_route

    db_file = Path(tempfile.gettempdir()) / f"api_compare_group_{uuid4().hex}.sqlite"
    with suppress(FileNotFoundError):
        db_file.unlink()
    db_url = f"sqlite+pysqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ALLOW_NON_POSTGRES_DATABASE_URL", "true")
    monkeypatch.setenv("DB_SCHEMA", "main")
    monkeypatch.setenv("API_KEYS", "dev-key-1")
    monkeypatch.setenv("AUTO_REGISTER_UNMAPPED_API_KEYS", "true")
    monkeypatch.delenv("DAILY_TOKEN_CAP", raising=False)
    monkeypatch.delenv("DAILY_COST_CAP", raising=False)
    monkeypatch.delenv("DAILY_CAP_SCOPE", raising=False)

    _reset_db_runtime_state(schema_name="main")
    rate_limit_service.reset_rate_limit_state()
    _bootstrap_api_db_schema(db_url)

    old_persistence_db = persistence_service.API_DB_ENABLED
    old_chat_db = chat_route.API_DB_ENABLED
    old_compare_db = compare_route.API_DB_ENABLED
    old_cortex_analysis_db = cortex_analysis_route.API_DB_ENABLED
    old_history_db = history_route.API_DB_ENABLED
    old_admin_db = admin_route.API_DB_ENABLED

    persistence_service.API_DB_ENABLED = True
    chat_route.API_DB_ENABLED = True
    compare_route.API_DB_ENABLED = True
    cortex_analysis_route.API_DB_ENABLED = True
    history_route.API_DB_ENABLED = True
    admin_route.API_DB_ENABLED = True

    app = create_app()
    if hasattr(deps.get_orchestrator, "_instance"):
        delattr(deps.get_orchestrator, "_instance")
    session_user_id = UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(
        deps,
        "parse_session",
        lambda cookie: session_user_id if cookie else None,
    )

    fake_orch = DBModeCompareOrchestrator()
    app.dependency_overrides[deps.get_orchestrator] = lambda: fake_orch
    client = TestClient(app)
    client.cookies.set("cortex_session", "test-session-cookie")

    try:
        yield client, fake_orch
    finally:
        client.close()
        persistence_service.API_DB_ENABLED = old_persistence_db
        chat_route.API_DB_ENABLED = old_chat_db
        compare_route.API_DB_ENABLED = old_compare_db
        cortex_analysis_route.API_DB_ENABLED = old_cortex_analysis_db
        history_route.API_DB_ENABLED = old_history_db
        admin_route.API_DB_ENABLED = old_admin_db
        rate_limit_service.reset_rate_limit_state()
        _reset_db_runtime_state(schema_name="public")
        with suppress(FileNotFoundError):
            db_file.unlink()


@pytest.mark.unit
def test_cortex_analysis_runs_persist_and_regeneration_marks_them_stale(
    db_mode_fastapi_client,
    monkeypatch,
):
    client, _ = db_mode_fastapi_client
    generated = cortex_analysis_service.AnalysisResult(
        recommended_answer="Use a staged rollout.",
        agreements=["Both responses favor a staged rollout."],
        disagreements=[],
        disagreement_note=None,
        unique_insights=[],
        confidence_level="moderate",
        confidence_reason="The responses align on the main tradeoff.",
        verify_items=["Confirm the rollout constraint."],
        high_stakes_domain=None,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        estimated_cost=0.001,
    )
    monkeypatch.setattr(
        cortex_analysis_service,
        "analyze_responses",
        lambda **kwargs: generated,
    )

    compare = client.post(
        "/v1/compare",
        json={
            "prompt": "Compare rollout approaches",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash"},
            ],
            "routing": {"smart_mode": False, "research_mode": False},
        },
    )
    assert compare.status_code == 200
    compare_payload = compare.json()
    group_id = compare_payload["request_group_id"]
    session_id = compare_payload["session_id"]

    first = client.post(f"/v1/compare/{group_id}/analysis", json={})
    second = client.post(f"/v1/compare/{group_id}/analysis", json={})
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["analysisId"] != second.json()["analysisId"]

    saved = client.get(
        "/v1/compare/analysis-runs",
        params={"session_id": session_id},
    )
    assert saved.status_code == 200
    assert len(saved.json()) == 2
    assert saved.json()[0]["isStale"] is False

    source = compare_payload["responses"][0]
    regenerated = client.post(
        "/v1/chat",
        json={
            "prompt": "Compare rollout approaches",
            "provider": source["provider"],
            "model": source["model"],
            "routing": {"smart_mode": False, "research_mode": False},
            "context": {"session_id": session_id, "new_session": False},
            "regeneration": {"source_request_id": source["request_id"]},
        },
    )
    assert regenerated.status_code == 200
    assert regenerated.json()["response_version"] == 2

    history = client.get(
        "/v1/history",
        params={"session_id": session_id, "limit": 20},
    )
    assert history.status_code == 200
    compare_rows = [item for item in history.json() if item["request_group_id"] == group_id]
    assert len(compare_rows) == 2
    assert any(item["response_version"] == 2 for item in compare_rows)

    stale_runs = client.get(
        "/v1/compare/analysis-runs",
        params={"request_group_id": group_id},
    )
    assert stale_runs.status_code == 200
    assert len(stale_runs.json()) == 2
    assert all(item["isStale"] is True for item in stale_runs.json())


@pytest.mark.unit
def test_create_api_key_returns_existing_owner_when_user_differs(monkeypatch):
    db_session, _users, api_keys = _sqlite_repo_fixture(monkeypatch)

    owner_user_id = str(uuid4())
    requested_user_id = str(uuid4())
    key_hash = repo.compute_api_key_hash("shared-dev-key")
    existing_api_key_id = str(uuid4())

    db_session.execute(
        insert(api_keys).values(
            id=existing_api_key_id,
            user_id=owner_user_id,
            key_hash=key_hash,
            label="existing",
            is_active=True,
            key_prefix="shared-dev-k",
            key_last4="-key",
        )
    )

    api_key_id, resolved_owner_user_id = repo.create_api_key(
        db_session,
        user_id=requested_user_id,
        raw_api_key="shared-dev-key",
        label="new-attempt",
    )

    assert api_key_id == existing_api_key_id
    assert resolved_owner_user_id == owner_user_id


@pytest.mark.unit
def test_get_or_create_api_session_reuses_user_owned_session_across_modes(monkeypatch):
    user_id = uuid4()
    session_id = uuid4()

    monkeypatch.setattr(
        persistence_service,
        "get_session_by_id",
        lambda *_args, **_kwargs: {"id": session_id, "mode": "ask"},
    )
    monkeypatch.setattr(
        persistence_service,
        "verify_session_belongs_to_user",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        persistence_service,
        "get_active_session",
        lambda *_args, **_kwargs: pytest.fail("should not query fallback active session"),
    )
    monkeypatch.setattr(
        persistence_service,
        "create_session",
        lambda *_args, **_kwargs: pytest.fail("should not create a new session"),
    )

    resolved = persistence_service.get_or_create_api_session(
        object(),
        user_id,
        str(session_id),
        mode="compare",
        title="API Compare",
    )

    assert resolved == session_id


@pytest.mark.unit
def test_get_or_create_api_session_reuses_latest_session_without_mode_partition(monkeypatch):
    user_id = uuid4()
    session_id = uuid4()
    observed_modes = []

    monkeypatch.setattr(persistence_service, "get_session_by_id", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        persistence_service,
        "get_active_session",
        lambda _db, _user_id, mode=None: observed_modes.append(mode) or session_id,
    )
    monkeypatch.setattr(
        persistence_service,
        "create_session",
        lambda *_args, **_kwargs: pytest.fail("should reuse the existing active session"),
    )

    resolved = persistence_service.get_or_create_api_session(
        object(),
        user_id,
        None,
        mode="compare",
        title="API Compare",
    )

    assert resolved == session_id
    assert observed_modes == [None]


@pytest.mark.unit
def test_get_or_create_service_user_uses_dedicated_identity(monkeypatch):
    db_session, users, _api_keys = _sqlite_repo_fixture(monkeypatch)

    cli_user_id = str(uuid4())
    db_session.execute(
        insert(users).values(
            id=cli_user_id,
            email="cli@cortexai.local",
            display_name="CLI User",
            is_active=True,
            auth_provider="cli",
            auth_subject="local-cli",
            auth_issuer="cortexai",
        )
    )

    service_user_id = repo.get_or_create_service_user(
        db_session,
        email="api@cortexai.local",
        display_name="API Service User",
    )
    service_user_id_2 = repo.get_or_create_service_user(
        db_session,
        email="api@cortexai.local",
        display_name="API Service User",
    )

    service_row = db_session.execute(
        select(users.c.auth_provider, users.c.auth_subject, users.c.auth_issuer).where(
            users.c.id == service_user_id
        )
    ).first()

    assert service_user_id == service_user_id_2
    assert service_user_id != cli_user_id
    assert service_row is not None
    assert service_row.auth_provider == "service"
    assert service_row.auth_subject == "api-service"
    assert service_row.auth_issuer == "cortexai"
    db_session.close()


@pytest.mark.integration
def test_chat_persists_core_artifacts_in_db_mode(fastapi_client, monkeypatch):
    client, _fake_orch = fastapi_client

    owner_user_id = uuid4()
    api_key_id = uuid4()
    session_id = uuid4()
    llm_request_pk = uuid4()

    chat_route.API_DB_ENABLED = True

    monkeypatch.setattr(
        chat_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: chat_route.ApiKeyPersistenceResolution(
            user_id=owner_user_id,
            api_key_id=api_key_id,
            decision_path="mapped",
        ),
    )

    dummy = DummySession()
    monkeypatch.setattr(persistence_service, "db_uow", _fake_db_uow_factory(dummy))
    monkeypatch.setattr(
        persistence_service, "get_or_create_api_session", lambda *_args, **_kwargs: session_id
    )

    calls: dict[str, list] = {
        "save_message": [],
        "create_llm_request": [],
        "create_llm_response": [],
        "routing": [],
        "usage": [],
    }

    monkeypatch.setattr(
        persistence_service,
        "save_message",
        lambda _db, _session_id, role, content: calls["save_message"].append((role, content)),
    )

    def _capture_create_llm_request(_db, **kwargs):
        calls["create_llm_request"].append(kwargs)
        return llm_request_pk

    monkeypatch.setattr(persistence_service, "create_llm_request", _capture_create_llm_request)
    monkeypatch.setattr(
        persistence_service,
        "create_llm_response",
        lambda _db, _llm_request_id, _response: calls["create_llm_response"].append(
            _llm_request_id
        ),
    )
    monkeypatch.setattr(
        persistence_service,
        "persist_routing_telemetry",
        lambda *_args, **_kwargs: calls["routing"].append(True),
    )
    monkeypatch.setattr(
        persistence_service, "update_session_timestamp", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        persistence_service,
        "upsert_usage_daily",
        lambda _db, **kwargs: calls["usage"].append(kwargs),
    )
    monkeypatch.setattr(
        persistence_service, "update_api_key_last_used", lambda *_args, **_kwargs: None
    )

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-4o-mini"},
    )

    assert response.status_code == 200
    assert len(calls["create_llm_request"]) == 1
    assert len(calls["create_llm_response"]) == 1
    assert len(calls["routing"]) == 1
    assert len(calls["usage"]) == 1
    assert len(calls["save_message"]) >= 1
    req_kwargs = calls["create_llm_request"][0]
    assert req_kwargs["user_id"] == owner_user_id
    assert req_kwargs["api_key_id"] == api_key_id
    assert req_kwargs["session_id"] == session_id
    assert req_kwargs["route_mode"] == "ask"


@pytest.mark.integration
def test_chat_persists_incomplete_when_orchestrator_returns_blank_length_success(
    fastapi_client, monkeypatch
):
    client, fake_orch = fastapi_client

    def _blank_success(
        prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        return UnifiedResponse(
            request_id="req_blank_success",
            text="",
            provider=model_type or "openai",
            model=kwargs.get("model_name") or kwargs.get("model") or "gpt-5.1",
            latency_ms=4,
            token_usage=TokenUsage(prompt_tokens=120, completion_tokens=500, total_tokens=620),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )

    fake_orch.ask = _blank_success

    owner_user_id = uuid4()
    api_key_id = uuid4()
    session_id = uuid4()
    llm_request_pk = uuid4()
    persisted_responses: list[UnifiedResponse] = []

    chat_route.API_DB_ENABLED = True

    monkeypatch.setattr(
        chat_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: chat_route.ApiKeyPersistenceResolution(
            user_id=owner_user_id,
            api_key_id=api_key_id,
            decision_path="mapped",
        ),
    )

    dummy = DummySession()
    monkeypatch.setattr(persistence_service, "db_uow", _fake_db_uow_factory(dummy))
    monkeypatch.setattr(
        persistence_service, "get_or_create_api_session", lambda *_args, **_kwargs: session_id
    )
    monkeypatch.setattr(persistence_service, "save_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        persistence_service,
        "create_llm_request",
        lambda _db, **_kwargs: llm_request_pk,
    )
    monkeypatch.setattr(
        persistence_service,
        "create_llm_response",
        lambda _db, _llm_request_id, _response: persisted_responses.append(_response),
    )
    monkeypatch.setattr(
        persistence_service, "persist_routing_telemetry", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        persistence_service, "update_session_timestamp", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(persistence_service, "upsert_usage_daily", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        persistence_service, "update_api_key_last_used", lambda *_args, **_kwargs: None
    )

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-5.1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == ""
    assert body["finish_reason"] == "length"
    assert body["error"] is None
    assert body["completion_status"] == "incomplete"
    assert body["stop_cause"] == "token_limit"

    assert len(persisted_responses) == 1
    persisted = persisted_responses[0]
    assert persisted.error is None
    assert persisted.finish_reason == "length"
    assert persisted.metadata["completion_status"] == "incomplete"
    assert persisted.metadata["stop_cause"] == "token_limit"


@pytest.mark.integration
def test_compare_persists_grouped_rows_and_usage_in_db_mode(fastapi_client, monkeypatch):
    client, _fake_orch = fastapi_client

    owner_user_id = uuid4()
    api_key_id = uuid4()
    session_id = uuid4()

    compare_route.API_DB_ENABLED = True

    monkeypatch.setattr(
        compare_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: compare_route.ApiKeyPersistenceResolution(
            user_id=owner_user_id,
            api_key_id=api_key_id,
            decision_path="mapped",
        ),
    )

    dummy = DummySession()
    monkeypatch.setattr(persistence_service, "db_uow", _fake_db_uow_factory(dummy))
    monkeypatch.setattr(
        persistence_service, "get_or_create_api_session", lambda *_args, **_kwargs: session_id
    )

    llm_req_calls = []
    usage_calls = []

    monkeypatch.setattr(persistence_service, "save_message", lambda *_args, **_kwargs: None)

    def _capture_llm_req(_db, **kwargs):
        llm_req_calls.append(kwargs)
        return uuid4()

    monkeypatch.setattr(persistence_service, "create_llm_request", _capture_llm_req)
    monkeypatch.setattr(persistence_service, "create_llm_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        persistence_service, "persist_routing_telemetry", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(persistence_service, "save_compare_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        persistence_service, "update_session_timestamp", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        persistence_service,
        "upsert_usage_daily",
        lambda _db, **kwargs: usage_calls.append(kwargs),
    )
    monkeypatch.setattr(
        persistence_service, "update_api_key_last_used", lambda *_args, **_kwargs: None
    )

    response = client.post(
        "/v1/compare",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Compare these models",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()

    assert len(llm_req_calls) == 2
    assert all(call["user_id"] == owner_user_id for call in llm_req_calls)
    assert all(call["api_key_id"] == api_key_id for call in llm_req_calls)
    assert all(call["route_mode"] == "compare" for call in llm_req_calls)
    assert all(str(call["request_group_id"]) == body["request_group_id"] for call in llm_req_calls)

    assert len(usage_calls) == 1
    assert usage_calls[0]["total_tokens"] == body["total_tokens"]
    assert usage_calls[0]["estimated_cost"] == body["total_cost"]


@pytest.mark.integration
def test_compare_request_group_id_matches_response_and_persisted_rows(db_mode_fastapi_client):
    client, _fake_orch = db_mode_fastapi_client

    response = client.post(
        "/v1/compare",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Compare persistence group id consistency",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    request_group_id = body["request_group_id"]
    request_ids = {item["request_id"] for item in body["responses"]}

    from db.session import SessionLocal
    from db.tables import get_table

    db_session = SessionLocal()
    try:
        llm_requests = get_table("llm_requests")
        rows = db_session.execute(
            select(llm_requests.c.request_id, llm_requests.c.request_group_id).where(
                llm_requests.c.request_id.in_(request_ids)
            )
        ).fetchall()
    finally:
        db_session.close()

    assert len(rows) == len(body["responses"])
    persisted_request_ids = {row[0] for row in rows}
    persisted_group_ids = {str(UUID(str(row[1]))) for row in rows}

    assert persisted_request_ids == request_ids
    assert persisted_group_ids == {str(UUID(request_group_id))}


@pytest.mark.integration
def test_admin_failed_attempts_view_by_request_group_id(db_mode_fastapi_client):
    client, _fake_orch = db_mode_fastapi_client

    compare_response = client.post(
        "/v1/compare",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Compare and inspect failed attempts",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert compare_response.status_code == 200
    request_group_id = compare_response.json()["request_group_id"]

    debug_response = client.get(
        f"/v1/admin/request-groups/{request_group_id}/failed-attempts",
        headers={"X-API-Key": "dev-key-1"},
    )
    assert debug_response.status_code == 200

    payload = debug_response.json()
    assert payload["request_group_id"] == request_group_id
    assert payload["count"] >= 1
    assert len(payload["items"]) == payload["count"]
    assert all(item["request_group_id"] == request_group_id for item in payload["items"])
    assert any(
        (item.get("validation") not in (None, "ok"))
        or item.get("error_type")
        or item.get("error_message")
        for item in payload["items"]
    )


@pytest.mark.integration
def test_daily_caps_enforced_in_api_db_mode(fastapi_client, monkeypatch):
    client, fake_orch = fastapi_client

    chat_route.API_DB_ENABLED = True
    monkeypatch.setenv("DAILY_TOKEN_CAP", "1")
    monkeypatch.delenv("DAILY_COST_CAP", raising=False)
    monkeypatch.setenv("DAILY_CAP_SCOPE", "user")

    user_id = uuid4()
    api_key_id = uuid4()

    class _CapSession:
        pass

    monkeypatch.setattr(persistence_service, "db_uow", _fake_db_uow_factory(_CapSession()))

    monkeypatch.setattr(
        persistence_service,
        "check_usage_limit",
        lambda *_args, **_kwargs: {
            "allowed": False,
            "current_tokens": 100,
            "current_cost": 5.0,
            "reason": "Daily token limit exceeded (100/1)",
        },
    )

    monkeypatch.setattr(
        chat_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: (
            persistence_service.enforce_usage_caps(user_id=user_id, api_key_id=api_key_id),
            chat_route.ApiKeyPersistenceResolution(
                user_id=user_id,
                api_key_id=api_key_id,
                decision_path="mapped",
            ),
        )[1],
    )

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-4o-mini"},
    )

    assert response.status_code == 429
    assert fake_orch.ask_calls == 0


@pytest.mark.integration
def test_stream_routes_persist_once_in_db_mode(fastapi_client, monkeypatch):
    client, _fake_orch = fastapi_client

    chat_route.API_DB_ENABLED = True
    compare_route.API_DB_ENABLED = True

    monkeypatch.setattr(
        chat_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: chat_route.ApiKeyPersistenceResolution(
            user_id=uuid4(),
            api_key_id=uuid4(),
            decision_path="mapped",
        ),
    )
    chat_persist_calls = []
    monkeypatch.setattr(
        chat_route,
        "_persist_chat_interaction",
        lambda **kwargs: chat_persist_calls.append(kwargs),
    )

    monkeypatch.setattr(
        compare_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: compare_route.ApiKeyPersistenceResolution(
            user_id=uuid4(),
            api_key_id=uuid4(),
            decision_path="mapped",
        ),
    )
    compare_persist_calls = []
    monkeypatch.setattr(
        compare_route,
        "_persist_compare_interaction",
        lambda **kwargs: compare_persist_calls.append(kwargs),
    )

    chat_resp = client.post(
        "/v1/chat/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-4o-mini"},
    )
    assert chat_resp.status_code == 200
    assert len(chat_persist_calls) == 1

    cmp_resp = client.post(
        "/v1/compare/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "hello",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert cmp_resp.status_code == 200
    assert len(compare_persist_calls) == 1


@pytest.mark.integration
def test_chat_stream_persists_incomplete_for_blank_length_success(fastapi_client, monkeypatch):
    client, fake_orch = fastapi_client

    def _blank_success(
        prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        return UnifiedResponse(
            request_id="req_stream_blank_success",
            text="",
            provider=model_type or "openai",
            model=kwargs.get("model_name") or kwargs.get("model") or "gpt-5.1",
            latency_ms=4,
            token_usage=TokenUsage(prompt_tokens=120, completion_tokens=500, total_tokens=620),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )

    fake_orch.ask = _blank_success

    chat_route.API_DB_ENABLED = True
    monkeypatch.setattr(
        chat_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: chat_route.ApiKeyPersistenceResolution(
            user_id=uuid4(),
            api_key_id=uuid4(),
            decision_path="mapped",
        ),
    )

    persisted: list[UnifiedResponse] = []
    monkeypatch.setattr(
        chat_route,
        "_persist_chat_interaction",
        lambda **kwargs: persisted.append(kwargs["response"]),
    )

    response = client.post(
        "/v1/chat/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={"prompt": "hello", "provider": "openai", "model": "gpt-5.1"},
    )
    assert response.status_code == 200
    assert len(persisted) == 1
    assert persisted[0].error is None
    assert persisted[0].finish_reason == "length"
    assert persisted[0].metadata["completion_status"] == "incomplete"
    assert persisted[0].metadata["stop_cause"] == "token_limit"


@pytest.mark.integration
def test_compare_stream_persists_incomplete_for_blank_length_target(fastapi_client, monkeypatch):
    client, fake_orch = fastapi_client

    def _ask_with_one_blank(
        prompt: str, model_type: str | None = None, context=None, **kwargs
    ) -> UnifiedResponse:
        provider = model_type or "openai"
        model = kwargs.get("model_name") or kwargs.get("model") or "unknown"
        if provider == "openai":
            return UnifiedResponse(
                request_id="req_cmp_stream_blank_target",
                text="",
                provider=provider,
                model=model,
                latency_ms=5,
                token_usage=TokenUsage(prompt_tokens=140, completion_tokens=500, total_tokens=640),
                estimated_cost=0.0,
                finish_reason="length",
                error=None,
                metadata={"endpoint": "chat.completions"},
            )
        return UnifiedResponse(
            request_id="req_cmp_stream_ok_target",
            text="ok",
            provider=provider,
            model=model,
            latency_ms=5,
            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    fake_orch.ask = _ask_with_one_blank

    compare_route.API_DB_ENABLED = True
    monkeypatch.setattr(
        compare_route,
        "_resolve_and_enforce_caps",
        lambda **_kwargs: compare_route.ApiKeyPersistenceResolution(
            user_id=uuid4(),
            api_key_id=uuid4(),
            decision_path="mapped",
        ),
    )

    persisted_batches: list[list[UnifiedResponse]] = []
    monkeypatch.setattr(
        compare_route,
        "_persist_compare_interaction",
        lambda **kwargs: persisted_batches.append(kwargs["responses"]),
    )

    response = client.post(
        "/v1/compare/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "hello",
            "targets": [
                {"provider": "openai", "model": "gpt-5.1"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert response.status_code == 200
    assert len(persisted_batches) == 1
    persisted = persisted_batches[0]
    assert len(persisted) == 2
    incomplete = next(item for item in persisted if item.finish_reason == "length")
    assert incomplete.error is None
    assert incomplete.metadata["completion_status"] == "incomplete"
    assert incomplete.metadata["stop_cause"] == "token_limit"


@pytest.mark.integration
def test_non_stream_chat_and_compare_share_one_session_id_in_db_mode(db_mode_fastapi_client):
    client, _fake_orch = db_mode_fastapi_client

    chat_response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Start a shared thread",
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
    )
    assert chat_response.status_code == 200
    chat_body = chat_response.json()
    session_id = chat_body.get("session_id")
    assert session_id

    compare_response = client.post(
        "/v1/compare",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Compare in the same thread",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert compare_response.status_code == 200
    compare_body = compare_response.json()
    assert compare_body["session_id"] == session_id

    history_response = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_id, "limit": 200},
    )
    assert history_response.status_code == 200
    history_payload = history_response.json()
    assert len(history_payload) == 3
    assert {item["mode"] for item in history_payload} == {"chat", "compare"}
    compare_history = [item for item in history_payload if item["mode"] == "compare"]
    assert len(compare_history) == 2
    assert {item["request_group_id"] for item in compare_history} == {
        compare_body["request_group_id"]
    }
    chat_history = [item for item in history_payload if item["mode"] == "chat"]
    assert chat_history[0]["request_group_id"] is None

    renamed = client.patch(
        f"/v1/history/session/{session_id}",
        headers={"X-API-Key": "dev-key-1"},
        json={"title": "Shared rollout plan"},
    )
    assert renamed.status_code == 200
    assert renamed.json() == {
        "session_id": session_id,
        "title": "Shared rollout plan",
    }

    renamed_history = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_id, "limit": 200},
    )
    assert renamed_history.status_code == 200
    assert {item["session_title"] for item in renamed_history.json()} == {"Shared rollout plan"}


@pytest.mark.integration
def test_stream_chat_and_compare_share_one_session_id_in_done_events(db_mode_fastapi_client):
    client, _fake_orch = db_mode_fastapi_client

    chat_response = client.post(
        "/v1/chat/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Stream a shared thread",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "routing": {"research_mode": True},
        },
    )
    assert chat_response.status_code == 200
    chat_events = [json.loads(line) for line in chat_response.text.splitlines() if line.strip()]
    chat_card = next(event["response"] for event in chat_events if event.get("type") == "response_done")
    chat_done = next(event for event in chat_events if event.get("type") == "done")
    session_id = chat_done.get("session_id")
    assert session_id

    compare_response = client.post(
        "/v1/compare/stream",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Stream compare in the same thread",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert compare_response.status_code == 200
    compare_events = [
        json.loads(line) for line in compare_response.text.splitlines() if line.strip()
    ]
    compare_cards = [
        event["response"] for event in compare_events if event.get("type") == "response_done"
    ]
    compare_done = next(event for event in compare_events if event.get("type") == "done")
    assert compare_done["compare"]["session_id"] == session_id

    history_response = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_id, "limit": 20},
    )
    assert history_response.status_code == 200
    history_by_request_id = {item["request_id"]: item for item in history_response.json()}

    restored_chat = history_by_request_id[chat_card["request_id"]]
    assert restored_chat["ai_credits"] == chat_card["ai_credits"]
    assert restored_chat["credit_usage_estimated"] == chat_card["credit_usage_estimated"]
    assert restored_chat["research_ai_credits"] == 10_000
    assert restored_chat["prompt_tokens"] == 2
    assert restored_chat["completion_tokens"] == 3

    for card in compare_cards:
        restored_card = history_by_request_id[card["request_id"]]
        assert restored_card["ai_credits"] == card["ai_credits"]
        assert restored_card["credit_usage_estimated"] == card["credit_usage_estimated"]
        assert restored_card["research_ai_credits"] == 0


@pytest.mark.integration
def test_history_can_filter_by_session_id_and_new_session_flag(db_mode_fastapi_client):
    client, _fake_orch = db_mode_fastapi_client

    session_a = str(uuid4())
    session_b = str(uuid4())

    first = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "session A first",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "context": {
                "session_id": session_a,
                "new_session": True,
                "conversation_history": [],
            },
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "session A second",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "context": {
                "session_id": session_a,
                "new_session": False,
                "conversation_history": [],
            },
        },
    )
    assert second.status_code == 200

    third = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "session B first",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "context": {
                "session_id": session_b,
                "new_session": True,
                "conversation_history": [],
            },
        },
    )
    assert third.status_code == 200

    only_a = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_a, "limit": 200},
    )
    assert only_a.status_code == 200
    payload_a = only_a.json()
    assert len(payload_a) == 2
    assert {item["session_id"] for item in payload_a} == {session_a}

    only_b = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_b, "limit": 200},
    )
    assert only_b.status_code == 200
    payload_b = only_b.json()
    assert len(payload_b) == 1
    assert payload_b[0]["session_id"] == session_b

    cleared_a = client.delete(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_a},
    )
    assert cleared_a.status_code == 204

    only_a_after_clear = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_a, "limit": 200},
    )
    assert only_a_after_clear.status_code == 200
    assert only_a_after_clear.json() == []

    only_b_after_clear = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"session_id": session_b, "limit": 200},
    )
    assert only_b_after_clear.status_code == 200
    payload_b_after_clear = only_b_after_clear.json()
    assert len(payload_b_after_clear) == 1
    assert payload_b_after_clear[0]["session_id"] == session_b


@pytest.mark.integration
def test_history_includes_web_source_items_when_research_is_used(
    db_mode_fastapi_client,
):
    client, _fake_orch = db_mode_fastapi_client

    response = client.post(
        "/v1/chat",
        headers={"X-API-Key": "dev-key-1"},
        json={
            "prompt": "Use web research and keep citations",
            "provider": "openai",
            "model": "gpt-4o-mini",
            "routing": {"research_mode": True, "smart_mode": True},
        },
    )
    assert response.status_code == 200

    history_response = client.get(
        "/v1/history",
        headers={"X-API-Key": "dev-key-1"},
        params={"limit": 20},
    )
    assert history_response.status_code == 200
    payload = history_response.json()
    assert payload
    assert payload[0]["web_source_items"] == [
        {"title": "Example Source", "url": "https://example.com/report"}
    ]


@pytest.mark.integration
def test_api_works_without_db_and_history_endpoints_require_db(fastapi_client):
    client, fake_orch = fastapi_client

    chat_route.API_DB_ENABLED = False
    compare_route.API_DB_ENABLED = False
    history_route.API_DB_ENABLED = False

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
            "prompt": "hello",
            "targets": [
                {"provider": "openai", "model": "gpt-4o-mini"},
                {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            ],
        },
    )
    assert compare_response.status_code == 200

    history_list = client.get("/v1/history", headers={"X-API-Key": "dev-key-1"})
    assert history_list.status_code == 501

    history_delete = client.delete("/v1/history/1", headers={"X-API-Key": "dev-key-1"})
    assert history_delete.status_code == 501

    history_clear = client.delete("/v1/history", headers={"X-API-Key": "dev-key-1"})
    assert history_clear.status_code == 501

    assert fake_orch.ask_calls >= 1
    assert fake_orch.compare_calls >= 1


@pytest.mark.unit
def test_trigger_migration_contains_owner_guard():
    migration_path = Path("db/migrations/20260218_llm_requests_api_key_owner_guard.sql")
    sql = migration_path.read_text(encoding="utf-8")

    assert "enforce_llm_request_api_key_owner_match" in sql
    assert "CREATE TRIGGER trg_llm_requests_enforce_api_key_owner" in sql
    assert "llm_requests.user_id" in sql
    assert "api_keys.user_id" in sql


@pytest.mark.unit
def test_request_group_id_migration_exists_and_has_indexes():
    migration_path = Path("db/migrations/20260218_add_request_group_id_to_llm_requests.sql")
    sql = migration_path.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS request_group_id uuid" in sql
    assert "idx_llm_requests_request_group_id" in sql
    assert "idx_llm_requests_user_request_group_id" in sql


@pytest.mark.unit
def test_go_live_hardening_migration_has_savings_status_and_reporting_indexes():
    migration_path = Path("db/migrations/20260222_go_live_hardening.sql")
    sql = migration_path.read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS response_status" in sql
    assert "ADD COLUMN IF NOT EXISTS error_code" in sql
    assert "idx_llm_requests_user_created_at" in sql
    assert "idx_llm_requests_api_key_created_at" in sql
    assert "idx_llm_savings_user_created_at" in sql
    assert "idx_llm_savings_api_key_created_at" in sql


@pytest.mark.unit
def test_redact_sensitive_headers_masks_auth_values():
    headers = {
        "X-API-Key": "dev-key-1",
        "Authorization": "Bearer abc",
        "Content-Type": "application/json",
    }

    redacted = redact_sensitive_headers(headers)

    assert redacted["X-API-Key"] == "[REDACTED]"
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["Content-Type"] == "application/json"
