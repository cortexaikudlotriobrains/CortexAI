from orchestrator.fallback_manager import FallbackManager
from orchestrator.model_registry import ModelRegistry
from orchestrator.model_selector import ModelSelector, ReliabilityStore
from orchestrator.prompt_analyzer import PromptAnalyzer
from orchestrator.response_validator import ResponseValidator
from orchestrator.smart_router import SmartRouter
from orchestrator.tier_decider import TierDecider


def _build_router() -> SmartRouter:
    registry = ModelRegistry.from_yaml()
    thresholds = registry.routing_defaults().get("thresholds", {})
    token_buffer = int(thresholds.get("token_buffer", 200))
    return SmartRouter(
        registry=registry,
        selector=ModelSelector(reliability_store=ReliabilityStore(), token_buffer=token_buffer),
        validator=ResponseValidator(thresholds=thresholds),
        fallback_manager=FallbackManager(),
        analyzer=PromptAnalyzer(),
        decider=TierDecider(thresholds=thresholds),
    )


def _first_pick(prompt: str) -> tuple[str, str, str, list[str]]:
    router = _build_router()
    _, tier, ordered_candidates, metadata = router.route_once_plan(
        prompt=prompt,
        context=None,
        routing_mode="smart",
        constraints=None,
    )
    first = ordered_candidates[0]
    return tier.value, first.provider, first.model_name, metadata.get("decision_reasons", [])


def test_simple_prompt_routes_to_t0_and_picks_gpt_6_luna():
    tier, provider, model, reasons = _first_pick(
        "Rewrite this sentence in simpler words: We should accelerate implementation to reduce timeline risk."
    )
    assert tier == "T0"
    assert "short_simple_rewrite" in reasons
    assert provider == "openai"
    assert model == "gpt-6-luna"


def test_analysis_prompt_routes_to_t2_and_picks_gpt_5_4_mini():
    tier, provider, model, reasons = _first_pick(
        "Analyze the tradeoffs between event-driven architecture and request-response microservices "
        "for a B2B SaaS platform. Give a decision framework and risks."
    )
    assert tier == "T2"
    assert "analysis_detected" in reasons
    assert provider == "openai"
    assert model == "gpt-5.4-mini"


def test_coding_prompt_routes_to_t3_and_picks_gpt_6_astra():
    tier, provider, model, reasons = _first_pick(
        "Debug this Python traceback and provide a production-ready patch with tests. "
        "Traceback (most recent call last): File \"app.py\", line 42, in run -> KeyError: 'user_id'"
    )
    assert tier == "T3"
    assert "coding_priority_models" in reasons
    assert provider == "openai"
    # First entry of routing_defaults.coding_preference.
    assert model == "gpt-6-astra"
