from datetime import datetime, timezone

from models.unified_response import NormalizedError, TokenUsage, UnifiedResponse
from models.user_context import UserContext
from orchestrator.core import CortexOrchestrator
from orchestrator.routing_types import ModelCandidate, PromptFeatures, Tier
from tools.web.intent import should_reuse_research
from tools.web.research_state import ResearchSource, ResearchState


class FakeClient:
    def __init__(self, provider: str, model: str):
        self.provider_name = provider
        self.model_name = model

    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id="req_fake",
            text="ok",
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        )


class StaticTextClient(FakeClient):
    def __init__(self, provider: str, model: str, text: str):
        super().__init__(provider, model)
        self.text = text

    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id=f"req_{self.provider_name}",
            text=self.text,
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=40, total_tokens=60),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        )


class SpyClient(FakeClient):
    def __init__(self, provider: str, model: str):
        super().__init__(provider, model)
        self.calls: list[list[dict[str, str]]] = []

    def get_completion(self, *args, **kwargs):
        messages = kwargs.get("messages") or []
        self.calls.append(messages)
        return super().get_completion(*args, **kwargs)


class EmptyResponseClient(FakeClient):
    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id="req_empty",
            text="",
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=0, total_tokens=1),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )


class EmptyLengthResponseClient(FakeClient):
    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id="req_empty_length",
            text="",
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=42, completion_tokens=500, total_tokens=542),
            estimated_cost=0.0,
            finish_reason="length",
            error=None,
            metadata={"endpoint": "chat.completions"},
        )


class LongSuccessClient(FakeClient):
    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id="req_long_success",
            text=(
                "This response is intentionally long enough to pass validator checks. "
                "It includes concrete steps, rationale, and a complete answer so fallback can stop."
            ),
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=5,
            token_usage=TokenUsage(prompt_tokens=30, completion_tokens=90, total_tokens=120),
            estimated_cost=0.0002,
            finish_reason="stop",
            error=None,
            metadata={},
        )


class TransientCapacityErrorClient(FakeClient):
    def get_completion(self, *args, **kwargs):
        return UnifiedResponse(
            request_id="req_transient_capacity",
            text="",
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=3,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message="This model is temporarily busy. Try again shortly or switch to another model.",
                provider=self.provider_name,
                retryable=True,
                details={"kind": "transient_capacity", "status_code": 503},
            ),
            metadata={},
        )


class FakeSourceDoc:
    def __init__(self, source_id: int, title: str, url: str, fetched_at: str, excerpt: str):
        self.id = source_id
        self.title = title
        self.url = url
        self.fetched_at = fetched_at
        self.excerpt = excerpt


class FakeResearchContext:
    def __init__(self, query: str):
        now = "2026-03-02T00:00:00+00:00"
        self.used = True
        self.injected_text = (
            "WEB RESEARCH SOURCES:\n"
            "[1] Example Source\n"
            "URL: https://example.com/source\n"
            "Snippet: example snippet"
        )
        self.sources = [
            FakeSourceDoc(
                source_id=1,
                title="Example Source",
                url="https://example.com/source",
                fetched_at=now,
                excerpt=f"excerpt for {query}",
            )
        ]
        self.cache_hit = False
        self.error = None
        self.provider_credits_used = 2
        self.provider_credits_estimated = False


class FakeResearchService:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def build(self, query: str, *, use_cache: bool = True):
        self.calls.append({"query": query, "use_cache": use_cache})
        return FakeResearchContext(query)


def test_direct_ask_still_works(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = FakeClient("openai", "gpt-4o-mini")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)
    resp = orchestrator.ask(
        prompt="hello",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
    )
    assert resp.text == "ok"
    assert resp.provider == "openai"


def test_direct_ask_normalizes_empty_output_to_provider_error(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = EmptyResponseClient("openai", "gpt-4o-mini")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)

    resp = orchestrator.ask(
        prompt="hello",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
    )
    assert resp.is_error is True
    assert resp.error is not None
    assert resp.error.code == "provider_error"
    assert "empty response" in resp.error.message.lower()


def test_direct_ask_preserves_empty_length_output_as_incomplete(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = EmptyLengthResponseClient("openai", "gpt-5.1")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)

    resp = orchestrator.ask(
        prompt="summarize this report",
        model_type="openai",
        model_name="gpt-5.1",
        routing_mode="legacy",
    )
    assert resp.is_success is True
    assert resp.error is None
    assert resp.finish_reason == "length"
    assert resp.metadata["completion_status"] == "incomplete"
    assert resp.metadata["stop_cause"] == "token_limit"


def test_research_metadata_attached(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = FakeClient("openai", "gpt-4o-mini")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)

    def _fake_apply(*, prompt, messages, research_mode, context):
        return messages, {
            "research_used": True,
            "research_reused": False,
            "research_topic": "test",
            "research_error": None,
            "sources": [],
        }

    orchestrator.research_service = object()
    monkeypatch.setattr(orchestrator, "_apply_research_if_needed", _fake_apply)

    resp = orchestrator.ask(
        prompt="hello",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
        research_mode="auto",
    )
    assert resp.metadata.get("research_used") is True


def test_direct_ask_preserves_provider_browse_disclaimer(monkeypatch):
    orchestrator = CortexOrchestrator()
    provider_text = (
        "I cannot browse independently, but I can answer using the web research context "
        "supplied with this request."
    )
    fake = StaticTextClient("openai", "gpt-4o-mini", provider_text)
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)
    orchestrator.research_service = FakeResearchService()

    response = orchestrator.ask(
        prompt="Search the web for the latest example.",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
        research_mode="on",
    )

    assert response.text == provider_text
    assert response.metadata["research_used"] is True
    assert response.metadata["research_error"] is None


def test_smart_mode_respects_explicit_model(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = FakeClient("openai", "gpt-4o-mini")
    smart_called = {"value": False}

    def _fake_run(*args, **kwargs):
        smart_called["value"] = True
        return UnifiedResponse(
            request_id="req_smart",
            text="smart",
            provider="openai",
            model="gpt-4o-mini",
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    monkeypatch.setattr(orchestrator, "_run_smart_attempt_loop", _fake_run)
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)

    resp = orchestrator.ask(
        prompt="hello",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="smart",
    )
    assert resp.text == "ok"
    assert smart_called["value"] is False


def test_explicit_unknown_model_rejected():
    orchestrator = CortexOrchestrator()
    resp = orchestrator.ask(
        prompt="hello",
        model_type="openai",
        model_name="definitely-not-a-real-model",
        routing_mode="smart",
    )
    assert resp.is_error is True
    assert resp.error.code == "bad_request"


def test_smart_retry_on_refusal_then_success(monkeypatch):
    orchestrator = CortexOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda **_kwargs: ["gemini", "openai"],
    )

    features = PromptFeatures(
        word_count=20,
        char_count=120,
        token_estimate=80,
        has_code=True,
        has_math=False,
        has_analysis=False,
        has_creative=False,
        has_factual=False,
        strict_format=False,
        has_logs_stacktrace=False,
        context_token_estimate=0,
        context_messages=0,
        is_follow_up=False,
        needs_latest_info=False,
        needs_accuracy=False,
        intent="code",
        has_strict_constraints=False,
    )

    candidates = [
        ModelCandidate(
            provider="gemini",
            model_name="gemini-2.5-pro",
            tier=Tier.T2,
            input_cost_per_1m=1.0,
            output_cost_per_1m=1.0,
            context_limit=128000,
            tags=["strong"],
            enabled=True,
        ),
        ModelCandidate(
            provider="openai",
            model_name="gpt-4o",
            tier=Tier.T2,
            input_cost_per_1m=2.0,
            output_cost_per_1m=2.0,
            context_limit=128000,
            tags=["strong"],
            enabled=True,
        ),
    ]

    routing_md = {
        "mode": "smart",
        "initial_tier": "T2",
        "final_tier": "T2",
        "attempt_count": 0,
        "fallback_used": False,
        "attempts": [],
        "decision_reasons": ["code_detected"],
    }

    monkeypatch.setattr(
        orchestrator._smart_router,
        "route_once_plan",
        lambda **kwargs: (features, Tier.T2, candidates, routing_md),
    )

    refusal_resp = UnifiedResponse(
        request_id="req_refusal",
        text="I'm sorry, but I can't assist with what appears to be an attempt to modify or override my system instructions.",
        provider="gemini",
        model="gemini-2.5-pro",
        latency_ms=10,
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        estimated_cost=0.0,
        finish_reason="stop",
        error=None,
        metadata={},
    )
    success_resp = UnifiedResponse(
        request_id="req_ok",
        text=(
            "Here is a safe refactor with unchanged behavior. "
            "I extracted duplicated branches, kept thread-safety intact, and preserved the "
            "existing mode-update flow while improving readability and naming."
        ),
        provider="openai",
        model="gpt-4o",
        latency_ms=10,
        token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        estimated_cost=0.0,
        finish_reason="stop",
        error=None,
        metadata={},
    )

    responses = {
        "gemini-2.5-pro": refusal_resp,
        "gpt-4o": success_resp,
    }
    monkeypatch.setattr(
        orchestrator,
        "_invoke_candidate",
        lambda candidate, messages, **kwargs: responses[candidate.model_name],
    )

    resp = orchestrator._run_smart_attempt_loop(
        prompt="Please refactor this code",
        context=None,
        messages=[{"role": "user", "content": "Please refactor this code"}],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert "safe refactor with unchanged behavior" in resp.text
    assert resp.metadata["routing"]["attempt_count"] == 2
    assert resp.metadata["routing"]["fallback_used"] is True


def test_smart_skips_unauthorized_expensive_candidate_before_provider_call(monkeypatch):
    orchestrator = CortexOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda **_kwargs: ["claude", "deepseek"],
    )
    features = PromptFeatures(
        word_count=20,
        char_count=120,
        token_estimate=80,
        has_code=True,
        has_math=False,
        has_analysis=True,
        has_creative=False,
        has_factual=False,
        strict_format=False,
        has_logs_stacktrace=False,
        context_token_estimate=0,
        context_messages=0,
        is_follow_up=False,
        needs_latest_info=False,
        needs_accuracy=True,
        intent="code",
        has_strict_constraints=False,
    )
    candidates = [
        ModelCandidate(
            provider="claude",
            model_name="claude-opus-4-6",
            tier=Tier.T3,
            input_cost_per_1m=5.0,
            output_cost_per_1m=25.0,
            context_limit=200000,
            tags=["flagship"],
            enabled=True,
        ),
        ModelCandidate(
            provider="deepseek",
            model_name="deepseek-chat",
            tier=Tier.T3,
            input_cost_per_1m=0.28,
            output_cost_per_1m=0.42,
            context_limit=128000,
            tags=["cheap"],
            enabled=True,
        ),
    ]
    routing_md = {
        "mode": "smart",
        "initial_tier": "T3",
        "final_tier": "T3",
        "attempt_count": 0,
        "fallback_used": False,
        "attempts": [],
        "decision_reasons": ["analysis_detected"],
    }
    monkeypatch.setattr(
        orchestrator._smart_router,
        "route_once_plan",
        lambda **_kwargs: (features, Tier.T3, candidates, routing_md),
    )
    invoked: list[str] = []

    def invoke(candidate, *_args, **_kwargs):
        invoked.append(candidate.model_name)
        return UnifiedResponse(
            request_id="req_affordable_fallback",
            text=(
                "Use an atomic transaction boundary, lock the shared counter, and retry "
                "only serialization failures. This preserves the credit invariant."
            ),
            provider=candidate.provider,
            model=candidate.model_name,
            latency_ms=5,
            token_usage=TokenUsage(prompt_tokens=20, completion_tokens=30, total_tokens=50),
            estimated_cost=0.0001,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    monkeypatch.setattr(orchestrator, "_invoke_candidate", invoke)
    authorization_checks: list[str] = []

    def authorize(_provider: str, model: str) -> bool:
        authorization_checks.append(model)
        return model == "deepseek-chat"

    response = orchestrator._run_smart_attempt_loop(
        prompt="Review this transaction design.",
        context=None,
        messages=[{"role": "user", "content": "Review this transaction design."}],
        routing_mode="smart",
        routing_constraints=None,
        candidate_authorizer=authorize,
    )

    assert authorization_checks[:2] == ["claude-opus-4-6", "deepseek-chat"]
    assert invoked == ["deepseek-chat"]
    assert response.provider == "deepseek"
    assert response.model == "deepseek-chat"


def test_smart_retry_on_empty_length_output_then_success(monkeypatch):
    orchestrator = CortexOrchestrator()

    features = PromptFeatures(
        word_count=20,
        char_count=120,
        token_estimate=80,
        has_code=False,
        has_math=False,
        has_analysis=True,
        has_creative=False,
        has_factual=True,
        strict_format=False,
        has_logs_stacktrace=False,
        context_token_estimate=0,
        context_messages=0,
        is_follow_up=False,
        needs_latest_info=False,
        needs_accuracy=False,
        intent="general",
        has_strict_constraints=False,
    )

    candidates = [
        ModelCandidate(
            provider="openai",
            model_name="gpt-5.1",
            tier=Tier.T2,
            input_cost_per_1m=2.0,
            output_cost_per_1m=8.0,
            context_limit=128000,
            tags=["reasoning"],
            enabled=True,
        ),
        ModelCandidate(
            provider="claude",
            model_name="claude-sonnet-4",
            tier=Tier.T2,
            input_cost_per_1m=3.0,
            output_cost_per_1m=15.0,
            context_limit=200000,
            tags=["reasoning"],
            enabled=True,
        ),
    ]

    routing_md = {
        "mode": "smart",
        "initial_tier": "T2",
        "final_tier": "T2",
        "attempt_count": 0,
        "fallback_used": False,
        "attempts": [],
        "decision_reasons": ["large_context_or_prompt"],
        "candidate_plan": [],
        "selected_sequence": [],
    }

    monkeypatch.setattr(
        orchestrator._smart_router,
        "route_once_plan",
        lambda **kwargs: (features, Tier.T2, candidates, routing_md),
    )
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda providers=None, provider_api_keys=None: ["openai", "claude"],
    )

    clients = {
        ("openai", "gpt-5.1"): EmptyLengthResponseClient("openai", "gpt-5.1"),
        ("claude", "claude-sonnet-4"): LongSuccessClient("claude", "claude-sonnet-4"),
    }

    def _fake_get_client(model_type, model_name, **_kwargs):
        return clients[(model_type, model_name)]

    monkeypatch.setattr(orchestrator, "_get_client", _fake_get_client)

    resp = orchestrator._run_smart_attempt_loop(
        prompt="give me a deeply detailed answer with citations format",
        context=None,
        messages=[
            {"role": "user", "content": "give me a deeply detailed answer with citations format"}
        ],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert resp.is_success
    assert resp.provider == "claude"
    assert "intentionally long enough" in resp.text
    routing = resp.metadata.get("routing", {})
    assert routing.get("attempt_count") == 2
    assert routing.get("fallback_used") is True
    attempts = routing.get("attempts", [])
    assert attempts[0]["validation"] == "truncated"
    assert attempts[1]["validation"] == "ok"


def test_smart_retry_on_transient_capacity_then_success(monkeypatch):
    orchestrator = CortexOrchestrator()

    features = PromptFeatures(
        word_count=8,
        char_count=60,
        token_estimate=40,
        has_code=False,
        has_math=False,
        has_analysis=False,
        has_creative=False,
        has_factual=False,
        strict_format=False,
        has_logs_stacktrace=False,
        context_token_estimate=0,
        context_messages=0,
        is_follow_up=False,
        needs_latest_info=False,
        needs_accuracy=False,
        intent="general",
        has_strict_constraints=False,
    )
    candidates = [
        ModelCandidate(
            provider="gemini",
            model_name="gemini-2.5-flash",
            tier=Tier.T1,
            input_cost_per_1m=0.3,
            output_cost_per_1m=2.5,
            context_limit=1000000,
            tags=["fast", "general"],
            enabled=True,
        ),
        ModelCandidate(
            provider="openai",
            model_name="gpt-4o-mini",
            tier=Tier.T1,
            input_cost_per_1m=0.15,
            output_cost_per_1m=0.6,
            context_limit=128000,
            tags=["fast", "general"],
            enabled=True,
        ),
    ]
    routing_md = {
        "mode": "smart",
        "initial_tier": "T1",
        "final_tier": "T1",
        "attempt_count": 0,
        "fallback_used": False,
        "attempts": [],
        "decision_reasons": ["general_prompt"],
        "candidate_plan": [],
        "selected_sequence": [],
    }
    monkeypatch.setattr(
        orchestrator._smart_router,
        "route_once_plan",
        lambda **kwargs: (features, Tier.T1, candidates, routing_md),
    )
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda providers=None, provider_api_keys=None: ["gemini", "openai"],
    )
    clients = {
        ("gemini", "gemini-2.5-flash"): TransientCapacityErrorClient("gemini", "gemini-2.5-flash"),
        ("openai", "gpt-4o-mini"): LongSuccessClient("openai", "gpt-4o-mini"),
    }
    monkeypatch.setattr(
        orchestrator,
        "_get_client",
        lambda model_type, model_name, **_kwargs: clients[(model_type, model_name)],
    )

    resp = orchestrator._run_smart_attempt_loop(
        prompt="quick summary please",
        context=None,
        messages=[{"role": "user", "content": "quick summary please"}],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert resp.is_success
    assert resp.provider == "openai"
    routing = resp.metadata.get("routing", {})
    assert routing.get("attempt_count") == 2
    assert routing.get("fallback_used") is True
    attempts = routing.get("attempts", [])
    assert attempts[0]["validation"] == "provider_error"
    assert "transient_capacity" in attempts[0]["why_failed"]
    assert "UNAVAILABLE" not in attempts[0]["why_failed"]


def test_smart_routing_records_latency_budget_recovery_reason(monkeypatch):
    orchestrator = CortexOrchestrator()

    features = PromptFeatures(
        word_count=12,
        char_count=90,
        token_estimate=60,
        has_code=False,
        has_math=False,
        has_analysis=True,
        has_creative=False,
        has_factual=False,
        strict_format=False,
        has_logs_stacktrace=False,
        context_token_estimate=0,
        context_messages=0,
        is_follow_up=False,
        needs_latest_info=False,
        needs_accuracy=False,
        intent="general",
        has_strict_constraints=False,
    )

    candidates = [
        ModelCandidate(
            provider="openai",
            model_name="gpt-5.1",
            tier=Tier.T2,
            input_cost_per_1m=2.0,
            output_cost_per_1m=8.0,
            context_limit=128000,
            tags=["reasoning"],
            enabled=True,
        ),
        ModelCandidate(
            provider="claude",
            model_name="claude-sonnet-4",
            tier=Tier.T2,
            input_cost_per_1m=3.0,
            output_cost_per_1m=15.0,
            context_limit=200000,
            tags=["reasoning"],
            enabled=True,
        ),
    ]
    routing_md = {
        "mode": "smart",
        "initial_tier": "T2",
        "final_tier": "T2",
        "attempt_count": 0,
        "fallback_used": False,
        "attempts": [],
        "decision_reasons": ["analysis_detected"],
        "candidate_plan": [],
        "selected_sequence": [],
    }

    monkeypatch.setattr(
        orchestrator._smart_router,
        "route_once_plan",
        lambda **kwargs: (features, Tier.T2, candidates, routing_md),
    )
    monkeypatch.setattr(
        orchestrator._model_registry,
        "routing_defaults",
        lambda: {"max_attempts": 3, "max_total_latency_ms": 0},
    )
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda providers=None, provider_api_keys=None: ["openai", "claude"],
    )

    clients = {
        ("openai", "gpt-5.1"): EmptyLengthResponseClient("openai", "gpt-5.1"),
        ("claude", "claude-sonnet-4"): LongSuccessClient("claude", "claude-sonnet-4"),
    }
    monkeypatch.setattr(
        orchestrator,
        "_get_client",
        lambda model_type, model_name, **_kwargs: clients[(model_type, model_name)],
    )

    resp = orchestrator._run_smart_attempt_loop(
        prompt="deep response please",
        context=None,
        messages=[{"role": "user", "content": "deep response please"}],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert resp.is_success
    routing = resp.metadata.get("routing", {})
    sequence = routing.get("selected_sequence", [])
    assert sequence
    assert sequence[0].get("next_action_reason") == "latency_budget_recovery"
    assert routing.get("fallback_used") is True


def test_smart_routing_filters_unroutable_providers_before_selection(monkeypatch):
    orchestrator = CortexOrchestrator()
    attempted: list[str] = []

    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda providers=None, provider_api_keys=None: ["openai"],
    )

    def _fake_invoke(candidate, messages, **kwargs):
        attempted.append(candidate.provider)
        return UnifiedResponse(
            request_id="req_openai",
            text="openai selected",
            provider=candidate.provider,
            model=candidate.model_name,
            latency_ms=1,
            token_usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            estimated_cost=0.0,
            finish_reason="stop",
            error=None,
            metadata={},
        )

    monkeypatch.setattr(orchestrator, "_invoke_candidate", _fake_invoke)

    resp = orchestrator._run_smart_attempt_loop(
        prompt="Write Python code to parse JSON safely",
        context=None,
        messages=[{"role": "user", "content": "Write Python code to parse JSON safely"}],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert resp.provider == "openai"
    assert attempted == ["openai"]


def test_smart_routing_returns_provider_error_when_no_provider_is_routable(monkeypatch):
    orchestrator = CortexOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "available_providers",
        lambda providers=None, provider_api_keys=None: [],
    )

    resp = orchestrator._run_smart_attempt_loop(
        prompt="Write Python code to parse JSON safely",
        context=None,
        messages=[{"role": "user", "content": "Write Python code to parse JSON safely"}],
        routing_mode="smart",
        routing_constraints=None,
    )

    assert resp.is_error is True
    assert resp.error is not None
    assert resp.error.code == "provider_error"
    assert "No routable providers are available" in resp.error.message


def test_research_mode_off_does_not_reuse_prior_web_context():
    orchestrator = CortexOrchestrator()
    session_id = "session-web-off-guard"
    now = "2026-03-02T00:00:00+00:00"

    prior_state = ResearchState(
        topic="pakistan nuclear program",
        query="pakistan nuclear program latest details",
        injected_text="WEB RESEARCH SOURCES:\n1) example source",
        sources=[
            ResearchSource(
                id=1,
                title="Example Source",
                url="https://example.com/source",
                fetched_at=now,
                excerpt="sample excerpt",
            )
        ],
        created_at=now,
        last_used_at=now,
        used=True,
        cache_hit=False,
        error=None,
        session_id=session_id,
        mode="on",
        ttl_seconds=900,
    )
    orchestrator._research_states[session_id] = prior_state

    messages = [{"role": "user", "content": "What is photosynthesis?"}]
    ctx = UserContext(session_id=session_id)

    updated_messages, metadata = orchestrator._apply_research_if_needed(
        prompt="What is photosynthesis?",
        messages=messages,
        research_mode="off",
        context=ctx,
    )

    assert updated_messages == messages
    assert metadata["research_used"] is False
    assert metadata["research_reused"] is False
    assert metadata["research_provider_credits_used"] == 0
    assert metadata["research_provider_credits_estimated"] is False
    assert metadata["sources"] == []


def test_research_mode_off_system_prompt_excludes_sources_only_phrase():
    orchestrator = CortexOrchestrator()
    messages = orchestrator._build_messages(
        prompt="What happened yesterday?",
        context=None,
        research_mode="off",
    )
    system_content = str(messages[0].get("content", ""))
    assert "CURRENT DATE:" in system_content
    assert "CURRENT DATE: January 17, 2026" not in system_content
    assert "Web research is disabled for this turn." in system_content
    assert "The provided sources don't contain that specific information." not in system_content


def test_system_prompt_uses_current_utc_date():
    orchestrator = CortexOrchestrator()
    messages = orchestrator._build_messages(
        prompt="quick answer",
        context=None,
        research_mode="off",
    )
    system_content = str(messages[0].get("content", ""))
    today = datetime.now(timezone.utc)
    expected_date = f"{today.strftime('%B')} {today.day}, {today.year}"
    assert f"CURRENT DATE: {expected_date}" in system_content


def test_ask_sequence_web_on_then_off_does_not_leak_prior_sources(monkeypatch):
    orchestrator = CortexOrchestrator()
    spy = SpyClient("openai", "gpt-4o-mini")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: spy)

    fake_research = FakeResearchService()
    orchestrator.research_service = fake_research

    context = UserContext(session_id="session-web-toggle-sequence")

    first_response = orchestrator.ask(
        prompt="What are the latest inflation numbers in Canada?",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
        research_mode="on",
        context=context,
    )
    second_response = orchestrator.ask(
        prompt="Explain photosynthesis in one sentence.",
        model_type="openai",
        model_name="gpt-4o-mini",
        routing_mode="legacy",
        research_mode="off",
        context=context,
    )

    assert first_response.metadata.get("research_used") is True
    assert second_response.metadata.get("research_used") is False
    assert second_response.metadata.get("research_reused") is False
    assert second_response.metadata.get("sources") == []

    # First turn can search; second turn must never search after user toggles web off.
    assert len(fake_research.calls) == 1
    assert fake_research.calls[0]["use_cache"] is False

    first_messages = spy.calls[0]
    second_messages = spy.calls[1]
    assert any("WEB RESEARCH SOURCES:" in str(msg.get("content", "")) for msg in first_messages)
    assert all(
        "WEB RESEARCH SOURCES:" not in str(msg.get("content", "")) for msg in second_messages
    )
    assert "Web research is disabled for this turn." in str(second_messages[0].get("content", ""))


def test_research_mode_off_skips_search_even_for_explicit_web_request():
    orchestrator = CortexOrchestrator()
    session_id = "session-explicit-web-off"
    now = "2026-03-02T00:00:00+00:00"

    prior_state = ResearchState(
        topic="inflation canada",
        query="latest inflation in canada",
        injected_text="WEB RESEARCH SOURCES:\n1) old source",
        sources=[
            ResearchSource(
                id=1,
                title="Old Source",
                url="https://example.com/old",
                fetched_at=now,
                excerpt="old excerpt",
            )
        ],
        created_at=now,
        last_used_at=now,
        used=True,
        cache_hit=False,
        error=None,
        session_id=session_id,
        mode="on",
        ttl_seconds=900,
    )
    orchestrator._research_states[session_id] = prior_state
    fake_research = FakeResearchService()
    orchestrator.research_service = fake_research

    original_messages = [{"role": "user", "content": "Can you search internet for inflation now?"}]
    updated_messages, metadata = orchestrator._apply_research_if_needed(
        prompt="Can you search internet for inflation now?",
        messages=original_messages,
        research_mode="off",
        context=UserContext(session_id=session_id),
    )

    assert updated_messages == original_messages
    assert metadata["research_used"] is False
    assert metadata["research_reused"] is False
    assert metadata["sources"] == []
    assert fake_research.calls == []


def test_should_reuse_research_returns_false_when_state_mode_is_off():
    now = "2026-03-02T00:00:00+00:00"
    state = ResearchState(
        topic="pakistan nuclear program",
        query="pakistan nuclear program latest",
        injected_text="WEB RESEARCH SOURCES:\n1) source",
        sources=[
            ResearchSource(
                id=1,
                title="Source",
                url="https://example.com/source",
                fetched_at=now,
                excerpt="excerpt",
            )
        ],
        created_at=now,
        last_used_at=now,
        used=True,
        cache_hit=False,
        error=None,
        session_id="session-intent-guard",
        mode="off",
        ttl_seconds=900,
    )

    assert should_reuse_research("can you check again", state) is False


def test_should_reuse_research_returns_false_when_current_mode_is_on():
    now = "2026-03-02T00:00:00+00:00"
    state = ResearchState(
        topic="inflation canada",
        query="latest inflation in canada",
        injected_text="WEB RESEARCH SOURCES:\n1) source",
        sources=[
            ResearchSource(
                id=1,
                title="Source",
                url="https://example.com/source",
                fetched_at=now,
                excerpt="excerpt",
            )
        ],
        created_at=now,
        last_used_at=now,
        used=True,
        cache_hit=False,
        error=None,
        session_id="session-intent-on",
        mode="auto",
        ttl_seconds=900,
    )

    assert should_reuse_research("can you check again", state, current_mode="on") is False


def test_research_mode_on_forces_fresh_search_instead_of_reuse():
    orchestrator = CortexOrchestrator()
    session_id = "session-web-on-fresh"
    now = "2026-03-02T00:00:00+00:00"

    prior_state = ResearchState(
        topic="inflation canada",
        query="latest inflation in canada",
        injected_text="WEB RESEARCH SOURCES:\n1) old source",
        sources=[
            ResearchSource(
                id=1,
                title="Old Source",
                url="https://example.com/old",
                fetched_at=now,
                excerpt="old excerpt",
            )
        ],
        created_at=now,
        last_used_at=now,
        used=True,
        cache_hit=False,
        error=None,
        session_id=session_id,
        mode="auto",
        ttl_seconds=900,
    )
    orchestrator._research_states[session_id] = prior_state
    fake_research = FakeResearchService()
    orchestrator.research_service = fake_research

    updated_messages, metadata = orchestrator._apply_research_if_needed(
        prompt="Can you verify the latest inflation numbers in Canada?",
        messages=[
            {"role": "user", "content": "Can you verify the latest inflation numbers in Canada?"}
        ],
        research_mode="on",
        context=UserContext(session_id=session_id),
    )

    assert len(fake_research.calls) == 1
    assert fake_research.calls[0]["use_cache"] is False
    assert metadata["research_used"] is True
    assert metadata["research_reused"] is False
    assert metadata["research_provider_credits_used"] == 2
    assert metadata["research_provider_credits_estimated"] is False
    assert metadata["sources"] == [
        {
            "id": 1,
            "title": "Example Source",
            "url": "https://example.com/source",
            "fetched_at": "2026-03-02T00:00:00+00:00",
        }
    ]
    assert any("WEB RESEARCH SOURCES:" in str(msg.get("content", "")) for msg in updated_messages)


def test_research_mode_on_falls_back_to_raw_prompt_when_sanitizer_blocks_query():
    orchestrator = CortexOrchestrator()
    session_id = "session-web-on-raw-fallback"
    fake_research = FakeResearchService()
    orchestrator.research_service = fake_research

    prompt = "was that 2020 or 2025?"
    updated_messages, metadata = orchestrator._apply_research_if_needed(
        prompt=prompt,
        messages=[{"role": "user", "content": prompt}],
        research_mode="on",
        context=UserContext(session_id=session_id),
    )

    assert len(fake_research.calls) == 1
    assert fake_research.calls[0]["query"] == prompt
    assert fake_research.calls[0]["use_cache"] is False
    assert metadata["research_used"] is True
    assert metadata["research_error"] is None
    assert any("WEB RESEARCH SOURCES:" in str(msg.get("content", "")) for msg in updated_messages)


def test_prepared_messages_reuse_shared_research_without_new_search(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake_research = FakeResearchService()
    fake_client = SpyClient("gemini", "gemini-2.5-flash")
    orchestrator.research_service = fake_research
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake_client)

    context = UserContext(
        session_id="session-prepared-compare",
        conversation_history=[
            {
                "role": "user",
                "content": "how has bollywood transitioned over the years in term of movies",
            },
            {
                "role": "assistant",
                "content": "Bollywood became romance-heavy in the 1990s.",
            },
        ],
    )
    prompt = "List the top-grossing or most popular films of the 1990s."
    prepared = orchestrator.prepare_messages_for_turn(
        prompt=prompt,
        context=context,
        research_mode="on",
    )

    first = orchestrator.ask(
        prompt=prompt,
        model_type="gemini",
        model_name="gemini-2.5-flash",
        routing_mode="legacy",
        research_mode="on",
        _prepared_prompt=prepared["prompt"],
        _prepared_messages=prepared["messages"],
        _prepared_research_metadata=prepared["research_metadata"],
        _prepared_opt_metadata=prepared["optimization_metadata"],
    )
    second = orchestrator.ask(
        prompt=prompt,
        model_type="gemini",
        model_name="gemini-2.5-flash",
        routing_mode="legacy",
        research_mode="on",
        _prepared_prompt=prepared["prompt"],
        _prepared_messages=prepared["messages"],
        _prepared_research_metadata=prepared["research_metadata"],
        _prepared_opt_metadata=prepared["optimization_metadata"],
    )

    assert first.is_success
    assert second.is_success
    assert len(fake_research.calls) == 1
    assert fake_research.calls[0]["query"].startswith("bollywood List the top-grossing")
    assert len(fake_client.calls) == 2
    assert fake_client.calls[0] == fake_client.calls[1]


def test_compare_normalizes_empty_output_to_provider_error(monkeypatch):
    orchestrator = CortexOrchestrator()
    fake = EmptyResponseClient("openai", "gpt-4o-mini")
    monkeypatch.setattr(orchestrator, "_get_client", lambda *_args, **_kwargs: fake)

    result = orchestrator.compare(
        prompt="hello",
        models_list=[{"provider": "openai", "model": "gpt-4o-mini"}],
        research_mode="off",
    )

    assert len(result.responses) == 1
    resp = result.responses[0]
    assert resp.is_error is True
    assert resp.error is not None
    assert resp.error.code == "provider_error"
    assert "empty response" in resp.error.message.lower()


def test_compare_preserves_historical_answers_when_sources_are_off(monkeypatch):
    orchestrator = CortexOrchestrator()

    def build_client(provider, model, **_kwargs):
        text = (
            f"{provider.title()} assessment: for the Napoleonic era, roughly 1792-1815, "
            "France ranks 1st overall while the remaining powers require a qualified comparison."
        )
        return StaticTextClient(provider, model, text)

    monkeypatch.setattr(orchestrator, "_get_client", build_client)

    result = orchestrator.compare(
        prompt=(
            "If you need to rate the artillery system of the Napoleonic era, "
            "rank the different European powers of that time."
        ),
        models_list=[
            {"provider": "openai", "model": "gpt-4o-mini"},
            {"provider": "gemini", "model": "gemini-2.5-flash-lite"},
            {"provider": "deepseek", "model": "deepseek-chat"},
        ],
        research_mode="off",
    )

    assert len(result.responses) == 3
    assert all(response is not None and response.is_success for response in result.responses)
    assert all("1792-1815" in response.text for response in result.responses if response)
    assert all(
        "fabrication_detected" not in response.metadata
        for response in result.responses
        if response
    )
