"""
CortexOrchestrator - Core business logic layer for CortexAI.

Key guarantees:
- CLI/API layers stay thin (no provider imports there)
- No exceptions bubble up from ask() / compare()
- TokenTracker updates happen here (business layer)
"""

import hashlib
import os
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from config.cache_optimization import cache_friendly_prompt_ordering_enabled
from api.base_client import BaseAIClient
from api.client_registry import ClientRegistry
from models.unified_response import (
    MultiUnifiedResponse,
    NormalizedError,
    TokenUsage,
    UnifiedResponse,
)
from models.user_context import UserContext
from orchestrator.multi_orchestrator import MultiModelOrchestrator
from orchestrator.cache_context import stable_context_digest
from orchestrator.model_registry import ModelRegistry
from orchestrator.model_selector import ModelSelector, ReliabilityStore
from orchestrator.prompt_analyzer import PromptAnalyzer
from orchestrator.response_validator import ResponseValidator
from orchestrator.routing_types import (
    ModelCandidate,
    RoutingConstraints,
    Tier,
)
from orchestrator.fallback_manager import FallbackManager, FallbackPolicy
from orchestrator.smart_router import SmartRouter
from orchestrator.tier_decider import TierDecider
from server import circuit_breaker
from server.utils import get_client_safe_provider_error_message
from tools.web import create_research_service_from_env
from tools.web.intent import (
    normalize_topic,
    sanitize_query,
    should_reuse_research,
    should_search,
    wants_more_sources,
)
from tools.web.research_state import (
    ResearchSource,
    ResearchState,
    create_initial_state,
)
from tools.web.persistent_research_store import (
    load_research_state,
    save_research_state,
)
from tools.web.session_state import get_session_store
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger
from utils.prompt_optimizer import PromptOptimizer
from utils.token_tracker import TokenTracker

logger = get_logger(__name__)
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default) or "").strip().lower() in _TRUE_ENV_VALUES


class CortexOrchestrator:
    def __init__(self):
        self._multi_orchestrator = MultiModelOrchestrator()
        self._client_cache: dict[str, BaseAIClient] = {}
        self._client_registry = ClientRegistry.from_catalog()
        self._research_states: dict[str, Any] = {}  # session_id -> ResearchState
        self._research_lock = threading.Lock()  # thread-safe access to states
        self._smart_router: SmartRouter | None = None
        self._model_registry: ModelRegistry | None = None
        self._selector: ModelSelector | None = None
        self._validator: ResponseValidator | None = None
        self._fallback_manager: FallbackManager | None = None
        self._prompt_analyzer: PromptAnalyzer | None = None
        self._tier_decider: TierDecider | None = None
        # /v1/optimize is the default UI optimization path. Orchestrator-level
        # auto-optimization is opt-in so chat/compare do not rewrite twice.
        self._prompt_optimizer = None
        if _env_flag("ENABLE_ORCHESTRATOR_PROMPT_OPTIMIZATION", "false"):
            try:
                provider = os.getenv("PROMPT_OPTIMIZER_PROVIDER", "gemini")
                self._prompt_optimizer = PromptOptimizer(provider=provider)
                logger.info(f"Prompt optimizer initialized with provider: {provider}")
            except Exception as e:
                logger.warning(f"Prompt optimizer initialization failed: {e}")
                self._prompt_optimizer = None

        # Initialize research service (optional - gracefully handle if not configured)
        try:
            self.research_service = create_research_service_from_env()
            self.session_store = get_session_store()
            logger.info("Research service initialized successfully")
        except Exception as e:
            self.research_service = None
            self.session_store = None
            if isinstance(e, ModuleNotFoundError):
                logger.error(
                    f"Research service initialization failed: {e}",
                    extra={
                        "extra_fields": {
                            "event": "research.init.failed",
                            "error_kind": "missing_dependency",
                        }
                    },
                )
            else:
                logger.warning(
                    f"Research service not available: {e}",
                    extra={
                        "extra_fields": {
                            "event": "research.init.unavailable",
                        }
                    },
                )

        # Initialize smart routing components (optional but preferred)
        try:
            self._model_registry = ModelRegistry.from_yaml()
            thresholds = self._model_registry.routing_defaults().get("thresholds", {})
            token_buffer = thresholds.get("token_buffer", 200)
            self._selector = ModelSelector(
                reliability_store=ReliabilityStore(), token_buffer=token_buffer
            )
            self._validator = ResponseValidator(thresholds=thresholds)
            self._fallback_manager = FallbackManager()
            self._prompt_analyzer = PromptAnalyzer()
            self._tier_decider = TierDecider(thresholds=thresholds)
            self._smart_router = SmartRouter(
                registry=self._model_registry,
                selector=self._selector,
                validator=self._validator,
                fallback_manager=self._fallback_manager,
                analyzer=self._prompt_analyzer,
                decider=self._tier_decider,
            )
            logger.info("Smart routing components initialized")
        except Exception as e:
            logger.warning(f"Smart routing initialization failed: {e}")

    # ---------- helpers ----------

    def _error_response(
        self,
        *,
        provider: str,
        model: str,
        message: str,
        code: str = "unknown",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> UnifiedResponse:
        return UnifiedResponse(
            request_id=str(uuid.uuid4()),
            text="",
            provider=provider,
            model=model,
            latency_ms=0,
            token_usage=TokenUsage(0, 0, 0),
            estimated_cost=0.0,
            finish_reason="error",
            error=NormalizedError(
                code=code,
                message=message,
                provider=provider,
                retryable=retryable,
                details=details or {},
            ),
        )

    def _normalize_empty_success_response(self, response: UnifiedResponse) -> UnifiedResponse:
        """
        Preserve billable length stops; convert other blank successes to errors.

        Some providers can return successful envelopes with no assistant text
        (e.g., content filtering, tool-only payloads, or schema edge cases).
        A length stop can mean reasoning exhausted the output allowance, so it
        remains an incomplete response for the route/UI retry contract.
        """
        if response.is_error:
            return response

        text_value = str(response.text or "")
        if text_value.strip():
            return response

        finish_reason = str(response.finish_reason or "").strip().lower()
        if finish_reason == "length":
            metadata = dict(response.metadata or {})
            metadata.setdefault("completion_status", "incomplete")
            metadata.setdefault("stop_cause", "token_limit")
            if int(response.token_usage.reasoning_tokens or 0) > 0:
                metadata["reasoning_budget_exhausted"] = True
            return replace(response, metadata=metadata)

        blocked_by_filter = finish_reason == "content_filter"
        message = (
            "Provider returned no text because content was filtered."
            if blocked_by_filter
            else "Provider returned an empty response."
        )
        retryable = not blocked_by_filter

        metadata = response.metadata if isinstance(response.metadata, dict) else {}
        details: dict[str, Any] = {}
        if response.finish_reason:
            details["finish_reason"] = str(response.finish_reason)
        if metadata.get("provider_finish_reason"):
            details["provider_finish_reason"] = str(metadata.get("provider_finish_reason"))
        if metadata.get("endpoint"):
            details["endpoint"] = str(metadata.get("endpoint"))

        logger.warning(
            "Normalized empty model output to provider_error",
            extra={
                "extra_fields": {
                    "provider": response.provider,
                    "model": response.model,
                    "finish_reason": response.finish_reason,
                    "endpoint": metadata.get("endpoint"),
                }
            },
        )

        return replace(
            response,
            text="",
            finish_reason="error",
            error=NormalizedError(
                code="provider_error",
                message=message,
                provider=response.provider,
                retryable=retryable,
                details=details,
            ),
        )

    def _get_client(
        self,
        model_type: str,
        model_name: str | None = None,
        *,
        api_key_override: str | None = None,
    ) -> BaseAIClient:
        model_type = (model_type or "").lower().strip()
        requested_model = str(model_name or "").strip() or None
        identity = None
        if requested_model and self._model_registry:
            identity = self._model_registry.resolve_model_identity(model_type, requested_model)
        runtime_model = (
            str(identity.get("runtime_model") or "").strip()
            if isinstance(identity, dict)
            else ""
        ) or requested_model
        key_scope = (
            hashlib.sha256(api_key_override.encode("utf-8")).hexdigest()[:12]
            if api_key_override
            else "env"
        )
        cache_key = f"{model_type}:{requested_model or 'default'}:{runtime_model or 'default'}:{key_scope}"
        if cache_key in self._client_cache:
            return self._client_cache[cache_key]

        client = self._client_registry.create_client(
            model_type,
            model_name=runtime_model,
            api_key_override=api_key_override,
        )
        resolved_model = client.model_name or runtime_model
        if identity is None and resolved_model and self._model_registry:
            identity = self._model_registry.resolve_model_identity(model_type, resolved_model)
        client.requested_model_name = requested_model or resolved_model
        client.model_identity = dict(identity or {})

        self._client_cache[cache_key] = client
        logger.info(
            "Initialized client",
            extra={
                "extra_fields": {
                    "provider": model_type,
                    "requested_model": requested_model or resolved_model,
                    "runtime_model": resolved_model,
                }
            },
        )
        return client

    def _build_messages(
        self, prompt: str, context: UserContext | None, research_mode: str = "auto"
    ) -> list[dict[str, str]]:
        research_mode_norm = (research_mode or "auto").lower().strip()
        current_date = datetime.now(timezone.utc)
        current_date_text = f"{current_date.strftime('%B')} {current_date.day}, {current_date.year}"

        if research_mode_norm == "off":
            system_content = f"""SYSTEM CONTEXT AND RULES:

You are CortexAI.
CURRENT DATE: {current_date_text}

Web research is disabled for this turn.

Rules:
1) Do not reference "provided sources" unless source excerpts are explicitly present in this conversation.
2) For current-data requests (prices, recent events, percentages), if no sources are present, say you do not have current data.
3) For general knowledge, answer from training data.
4) Never fabricate numbers, dates, percentages, or citations.
5) Never claim you performed web browsing yourself.
"""
        else:
            system_content = f"""SYSTEM CONTEXT AND RULES:

You are CortexAI with REAL-TIME WEB RESEARCH capability.
CURRENT DATE: {current_date_text}

If a system message containing "WEB RESEARCH SOURCES:" is present:
- Use those excerpts for factual claims.
- Cite sources as [1], [2], etc.
- If sources are partial, give the best sourced summary first.
- If an exact detail is missing, state what is missing and suggest one focused follow-up search query.

If no web source excerpts are present:
- For current-data requests, say you do not have current data.
- For general knowledge, answer from training data.

Never fabricate numbers, dates, percentages, or citations.
Never claim you performed web browsing yourself; the system handles retrieval.
"""

        if cache_friendly_prompt_ordering_enabled():
            system_content = system_content.replace(
                f"CURRENT DATE: {current_date_text}\n\n",
                "",
            )

        system_instruction = {"role": "system", "content": system_content}
        runtime_instruction = {
            "role": "system",
            "content": f"DYNAMIC RUNTIME CONTEXT:\nCURRENT DATE: {current_date_text}",
        }
        prefix = (
            [system_instruction, runtime_instruction]
            if cache_friendly_prompt_ordering_enabled()
            else [system_instruction]
        )

        if context and context.conversation_history:
            msgs = context.get_messages()
            msgs.append({"role": "user", "content": prompt})
            return [*prefix, *msgs]
        return [*prefix, {"role": "user", "content": prompt}]

    @staticmethod
    def _inject_reference_context(
        messages: list[dict[str, str]], content: str
    ) -> list[dict[str, str]]:
        reference = {"role": "system", "content": content}
        if cache_friendly_prompt_ordering_enabled() and messages:
            return [messages[0], reference, *messages[1:]]
        return [reference, *messages]

    def _cache_scope(
        self,
        *,
        context: UserContext | None,
        messages: list[dict[str, str]],
        mode: str,
    ) -> dict[str, str]:
        scope_id = self._get_session_id(context, messages)
        stable_messages = messages[:-1] if messages else []
        return {
            "scope_id": scope_id,
            "mode": mode,
            "stable_context_hash": stable_context_digest(stable_messages),
            "retention_policy": "ephemeral",
        }

    @staticmethod
    def _empty_research_metadata(error: str | None = None) -> dict[str, Any]:
        return {
            "research_used": False,
            "research_reused": False,
            "research_provider_credits_used": 0,
            "research_provider_credits_estimated": False,
            "research_topic": None,
            "research_error": error,
            "sources": [],
        }

    def prepare_messages_for_turn(
        self,
        *,
        prompt: str,
        context: UserContext | None = None,
        research_mode: str = "auto",
    ) -> dict[str, Any]:
        """Build one optimized/research-injected message payload for a turn."""
        optimized_prompt, opt_metadata = self._optimize_prompt_if_enabled(
            prompt,
            context=context,
        )
        if opt_metadata.get("optimization_used"):
            logger.debug("Using optimized prompt for request")

        messages = self._build_messages(optimized_prompt, context, research_mode=research_mode)
        if self.research_service:
            messages, research_metadata = self._apply_research_if_needed(
                prompt=optimized_prompt,
                messages=messages,
                research_mode=research_mode,
                context=context,
            )
        else:
            research_metadata = self._empty_research_metadata("service_not_configured")

        return {
            "prompt": optimized_prompt,
            "messages": messages,
            "research_metadata": research_metadata,
            "optimization_metadata": opt_metadata,
        }

    def _generate_session_id(self, messages: list[dict[str, str]]) -> str:
        """
        Generate session ID from conversation history.

        Uses hash of all messages (except current user prompt) to identify unique sessions.
        If messages is empty or only has one user message, returns "default" session.

        Args:
            messages: Conversation messages

        Returns:
            Session ID string
        """
        if not messages or len(messages) <= 1:
            return "default"

        # Hash all messages except the last one (current prompt)
        history = messages[:-1]
        history_str = str(history)
        session_hash = hashlib.sha256(history_str.encode()).hexdigest()[:16]
        return f"session_{session_hash}"

    def _get_session_id(self, context: UserContext | None, messages: list[dict[str, str]]) -> str:
        """
        Get session ID from context or generate from messages.

        Args:
            context: UserContext (may have session_id)
            messages: Conversation messages

        Returns:
            Session ID string
        """
        if context and hasattr(context, "session_id") and context.session_id:
            return context.session_id
        return self._generate_session_id(messages)

    def _optimize_prompt_if_enabled(
        self,
        prompt: str,
        context: UserContext | None = None,
    ) -> tuple[str, dict]:
        """
        Optimize prompt if optimization is enabled.

        Returns:
            tuple: (optimized_prompt, metadata)
            - If optimization disabled/fails: returns (original_prompt, {})
            - If optimization succeeds: returns (optimized_prompt, optimization_metadata)
        """
        if not self._prompt_optimizer:
            return prompt, {}

        try:
            payload: dict[str, Any] = {"prompt": prompt}
            if context and context.conversation_history:
                payload["context"] = {
                    "session_id": context.session_id,
                    "conversation_history": context.conversation_history,
                }
            result = self._prompt_optimizer.optimize_prompt(payload)

            if result.get("error"):
                logger.warning(f"Prompt optimization failed: {result['error']['message']}")
                return prompt, {"optimization_error": result["error"]["message"]}

            optimized = result.get("optimized_prompt", prompt)
            metadata = {
                "optimization_used": True,
                "original_prompt": prompt,
                "optimization_steps": result.get("steps", []),
                "optimization_metrics": result.get("metrics", {}),
            }

            logger.info(f"Prompt optimized: '{prompt[:50]}...' -> '{optimized[:50]}...'")
            return optimized, metadata

        except Exception as e:
            logger.error(f"Prompt optimization error: {e}")
            return prompt, {"optimization_error": str(e)}

    def _get_or_create_research_state(self, session_id: str, research_mode: str) -> ResearchState:
        """
        Get or create ResearchState for a session (thread-safe).

        Args:
            session_id: Session identifier
            research_mode: Current research mode

        Returns:
            ResearchState instance
        """
        with self._research_lock:
            if session_id not in self._research_states:
                # Get TTL from env, default to 900 seconds (15 minutes)
                ttl_seconds = int(os.getenv("RESEARCH_TTL_SECONDS", "900"))
                persisted = load_research_state(session_id)
                self._research_states[session_id] = (
                    persisted
                    if persisted is not None and not persisted.is_expired()
                    else create_initial_state(
                        session_id=session_id,
                        mode=research_mode,
                        ttl_seconds=ttl_seconds,
                    )
                )
            else:
                # Update mode if it changed
                existing = self._research_states[session_id]
                if existing.mode != research_mode:
                    self._research_states[session_id] = existing.with_update(mode=research_mode)

            return self._research_states[session_id]

    def _apply_research_if_needed(
        self,
        *,
        prompt: str,
        messages: list[dict[str, str]],
        research_mode: str,
        context: UserContext | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        """
        Apply web research if needed based on research_mode and session state.

        Enforces deterministic, state-driven behavior:
        1. Reuse existing research when appropriate
        2. Perform new search when needed
        3. Never search on meta follow-ups
        4. Never pass garbage queries to search

        Args:
            prompt: User prompt
            messages: Current messages list
            research_mode: "off" | "auto" | "on"
            context: Optional user context

        Returns:
            Tuple of (updated_messages, research_metadata)
        """
        # 1) Determine session ID
        session_id = self._get_session_id(context, messages)

        # 2) Get or create research state (thread-safe)
        state = self._get_or_create_research_state(session_id, research_mode)

        # 2.5) Hard stop: when web research is toggled OFF for this turn,
        # never inject or reuse prior web research context.
        if research_mode == "off":
            logger.info("Research disabled for this turn (mode=off)")
            return messages, {
                "research_used": False,
                "research_reused": False,
                "research_provider_credits_used": 0,
                "research_provider_credits_estimated": False,
                "research_topic": None,
                "research_error": None,
                "sources": [],
            }

        # 3) Check if we should reuse existing research.
        # Forced web mode ("on") should always refresh sources for this turn.
        should_reuse = should_reuse_research(prompt, state, current_mode=research_mode)
        logger.info(f"Research decision - reuse: {should_reuse}, prompt: {prompt[:50]}...")

        if should_reuse:
            # Inject previous research
            injected_messages = self._inject_reference_context(messages, state.injected_text)

            # Update last_used_at
            updated_state = state.with_update(last_used_at=datetime.now(timezone.utc).isoformat())
            with self._research_lock:
                self._research_states[session_id] = updated_state
            save_research_state(updated_state)
            logger.info(
                "Reused research context",
                extra={
                    "extra_fields": {
                        "event": "research.reused",
                        "operation_type": "research",
                        "provider": "tavily",
                        "credits": 0,
                    }
                },
            )

            # Build metadata
            metadata = {
                "research_used": True,
                "research_reused": True,
                "research_provider_credits_used": 0,
                "research_provider_credits_estimated": False,
                "research_topic": state.topic if state.topic else None,
                "research_error": None,
                "sources": [
                    {"id": s.id, "title": s.title, "url": s.url, "fetched_at": s.fetched_at}
                    for s in state.sources
                ],
            }
            return injected_messages, metadata

        # 4) Check if we should perform new search
        should_do_search = should_search(prompt, research_mode, state)
        logger.info(
            f"Research decision - search: {should_do_search}, mode: {research_mode}, prompt: {prompt[:50]}..."
        )

        if not should_do_search:
            logger.info(f"No research needed (mode={research_mode})")
            return messages, {
                "research_used": False,
                "research_reused": False,
                "research_provider_credits_used": 0,
                "research_provider_credits_estimated": False,
                "research_topic": None,
                "research_error": None,
                "sources": [],
            }

        # 5) Perform new search
        if not self.research_service:
            return messages, {
                "research_used": False,
                "research_reused": False,
                "research_provider_credits_used": 0,
                "research_provider_credits_estimated": False,
                "research_topic": None,
                "research_error": "service_not_configured",
                "sources": [],
            }

        # Extract PREVIOUS user message for context (helps with meta-commands like "check again")
        # Skip the current prompt (last user message) and get the one before it
        last_user_msg = None
        user_messages = [
            msg["content"] for msg in messages if msg.get("role") == "user" and msg.get("content")
        ]
        if len(user_messages) >= 2:
            # Get second-to-last user message (the one before current prompt)
            last_user_msg = user_messages[-2]

        # Apply query sanitization (remove stop words, handle meta-commands)
        logger.debug(f"Applying sanitization to: '{prompt[:50]}...'")
        query = sanitize_query(prompt, state, last_user_msg)

        if not query and research_mode == "on":
            fallback_query = prompt.strip()
            if fallback_query:
                logger.info(
                    "Research mode on: sanitization produced empty query; "
                    "falling back to raw prompt."
                )
                query = fallback_query

        if not query:
            # No query and no previous state - can't proceed
            logger.warning(f"Blocked garbage query with no fallback: {prompt[:50]}...")
            return messages, {
                "research_used": False,
                "research_reused": False,
                "research_provider_credits_used": 0,
                "research_provider_credits_estimated": False,
                "research_topic": None,
                "research_error": "invalid_query",
                "sources": [],
            }

        # Log if query was transformed (different from prompt)
        if query != prompt:
            # Sanitization changed the query
            if wants_more_sources(prompt):
                logger.info(f"'More sources' requested, re-searching topic: '{query[:50]}...'")
            else:
                logger.info(f"Heuristics transformed query: '{prompt[:30]}...' → '{query[:50]}...'")

        # Execute search
        logger.info(f"Executing new search: {query[:50]}...")
        use_cache = research_mode != "on"
        research_ctx = self.research_service.build(query, use_cache=use_cache)

        if research_ctx.used:
            # Convert SourceDoc to ResearchSource
            sources = [
                ResearchSource(
                    id=s.id, title=s.title, url=s.url, fetched_at=s.fetched_at, excerpt=s.excerpt
                )
                for s in research_ctx.sources
            ]

            # Create new ResearchState
            topic = normalize_topic(prompt)
            now = datetime.now(timezone.utc).isoformat()

            new_state = ResearchState(
                topic=topic,
                query=query,
                injected_text=research_ctx.injected_text,
                sources=sources,
                created_at=now,
                last_used_at=now,
                used=True,
                cache_hit=research_ctx.cache_hit,
                error=None,
                session_id=session_id,
                mode=research_mode,
                ttl_seconds=state.ttl_seconds,
                provider_credits_consumed=max(
                    0, int(getattr(research_ctx, "provider_credits_used", 0) or 0)
                ),
            )

            # Store new state (thread-safe)
            with self._research_lock:
                self._research_states[session_id] = new_state
            save_research_state(new_state)

            # Inject research
            injected_messages = self._inject_reference_context(
                messages, research_ctx.injected_text
            )

            metadata = {
                "research_used": True,
                "research_reused": bool(research_ctx.cache_hit),
                "research_provider_credits_used": max(
                    0, int(getattr(research_ctx, "provider_credits_used", 0) or 0)
                ),
                "research_provider_credits_estimated": bool(
                    getattr(research_ctx, "provider_credits_estimated", False)
                ),
                "research_topic": topic,
                "research_error": None,
                "sources": [
                    {"id": s.id, "title": s.title, "url": s.url, "fetched_at": s.fetched_at}
                    for s in sources
                ],
            }
            if bool(research_ctx.cache_hit):
                logger.info(
                    "Reused research context",
                    extra={
                        "extra_fields": {
                            "event": "research.reused",
                            "operation_type": "research",
                            "provider": "tavily",
                            "credits": 0,
                        }
                    },
                )
            return injected_messages, metadata
        else:
            # Search failed
            logger.warning(f"Research failed: {research_ctx.error}")
            return messages, {
                "research_used": False,
                "research_reused": False,
                "research_provider_credits_used": max(
                    0, int(getattr(research_ctx, "provider_credits_used", 0) or 0)
                ),
                "research_provider_credits_estimated": bool(
                    getattr(research_ctx, "provider_credits_estimated", False)
                ),
                "research_topic": None,
                "research_error": research_ctx.error,
                "sources": [],
            }

    def _build_routing_constraints(self, raw: dict[str, Any] | None) -> RoutingConstraints | None:
        if not raw:
            return None
        allowed_providers = raw.get("allowed_providers") or raw.get("allow_providers")
        if isinstance(allowed_providers, str):
            allowed_providers = [allowed_providers]
        allowed_billing_classes = raw.get("allowed_billing_classes")
        if isinstance(allowed_billing_classes, str):
            allowed_billing_classes = [allowed_billing_classes]
        allowed_models = raw.get("allowed_models")
        if isinstance(allowed_models, str):
            allowed_models = [allowed_models]

        return RoutingConstraints(
            max_cost_usd=raw.get("max_cost_usd"),
            max_total_latency_ms=raw.get("max_total_latency_ms"),
            preferred_provider=raw.get("preferred_provider"),
            allowed_providers=allowed_providers,
            allowed_billing_classes=allowed_billing_classes,
            allowed_models=allowed_models,
            min_context_limit=raw.get("min_context_limit"),
            json_only=bool(raw.get("json_only", False)),
            strict_format=bool(raw.get("strict_format", False)),
        )

    def available_providers(
        self,
        *,
        providers: list[str] | tuple[str, ...] | None = None,
        provider_api_keys: dict[str, str] | None = None,
    ) -> list[str]:
        return self._client_registry.available_providers(
            providers=providers,
            provider_api_keys=provider_api_keys,
        )

    def _constrain_to_routable_providers(
        self,
        constraints: RoutingConstraints | None,
        *,
        provider_api_keys: dict[str, str] | None = None,
    ) -> RoutingConstraints:
        requested = None
        preferred_provider = None
        if constraints:
            requested = constraints.allowed_providers
            preferred_provider = constraints.preferred_provider

        available = self.available_providers(
            providers=requested,
            provider_api_keys=provider_api_keys,
        )
        normalized_preferred = (preferred_provider or "").strip().lower()
        if normalized_preferred and normalized_preferred not in available:
            preferred_provider = None

        if constraints is None:
            return RoutingConstraints(
                allowed_providers=available,
                preferred_provider=preferred_provider,
            )

        return replace(
            constraints,
            allowed_providers=available,
            preferred_provider=preferred_provider,
        )

    def _resolve_forced_tier(self, routing_mode: str) -> Tier | None:
        if routing_mode == "cheap":
            return Tier.T0
        if routing_mode == "strong":
            return Tier.T2
        return None

    def preview_smart_target(
        self,
        *,
        prompt: str,
        context: UserContext | None = None,
        routing_mode: str = "smart",
        routing_constraints: dict[str, Any] | None = None,
        provider_api_keys: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        """
        Return the first planned smart-routing candidate without invoking providers.

        This is used by API streaming routes to emit a stable `start` event
        (provider/model) before the response is generated.
        """
        try:
            if not self._smart_router or not self._model_registry:
                return None

            constraints = self._build_routing_constraints(routing_constraints)
            constraints = self._constrain_to_routable_providers(
                constraints,
                provider_api_keys=provider_api_keys,
            )
            if constraints.allowed_providers == []:
                return None
            _features, _tier, ordered_candidates, _metadata = self._smart_router.route_once_plan(
                prompt=prompt,
                context=context,
                routing_mode=(routing_mode or "smart").lower().strip() or "smart",
                constraints=constraints,
                runtime_messages=None,
            )
            if not ordered_candidates:
                return None
            first = ordered_candidates[0]
            return first.provider, first.model_name
        except Exception:
            logger.exception("preview_smart_target() failed")
            return None

    def plan_smart_targets(
        self,
        *,
        prompt: str,
        context: UserContext | None = None,
        routing_mode: str = "smart",
        routing_constraints: dict[str, Any] | None = None,
        provider_api_keys: dict[str, str] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Return the ordered provider/model plan without invoking providers."""

        if not self._smart_router or not self._model_registry or not self._selector:
            return ()
        constraints = self._build_routing_constraints(routing_constraints)
        constraints = self._constrain_to_routable_providers(
            constraints,
            provider_api_keys=provider_api_keys,
        )
        if constraints.allowed_providers == []:
            return ()
        features, tier, ordered, _metadata = self._smart_router.route_once_plan(
            prompt=prompt,
            context=context,
            routing_mode=(routing_mode or "smart").lower().strip() or "smart",
            constraints=constraints,
            runtime_messages=None,
        )
        planned: list[ModelCandidate] = list(ordered)
        tier_order = [
            Tier(value)
            for value in self._model_registry.routing_defaults().get(
                "tier_order",
                ["T0", "T1", "T2", "T3"],
            )
        ]
        initial_index = tier_order.index(tier)
        fallback_tiers = [
            *reversed(tier_order[:initial_index]),
            *tier_order[initial_index + 1 :],
        ]
        for fallback_tier in fallback_tiers:
            candidates = self._model_registry.get_candidates(fallback_tier, constraints)
            if not candidates:
                continue
            selection = self._selector.select(features, candidates, constraints)
            planned.extend([selection.primary_candidate, *selection.fallback_candidates])

        result: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for candidate in planned:
            key = (candidate.provider, candidate.model_name)
            if key in seen:
                continue
            seen.add(key)
            result.append(key)
        return tuple(result)

    def _validate_explicit_model_selection(
        self, model_type: str | None, model_name: str | None
    ) -> tuple[bool, str]:
        if not model_type or not model_name:
            return False, "Both provider and model are required for explicit model selection"
        if not self._model_registry:
            return True, ""

        candidate = self._model_registry.find_model(model_type, model_name)
        if not candidate:
            return (
                False,
                f"Model '{model_name}' for provider '{model_type}' is not configured in model_registry.yaml",
            )
        if not candidate.enabled:
            return (
                False,
                f"Model '{model_name}' for provider '{model_type}' is currently disabled",
            )
        return True, ""

    def model_billing_class(self, provider: str, model: str) -> str | None:
        """Return the server-owned subscription class for one enabled model."""
        if not self._model_registry:
            return None
        candidate = self._model_registry.find_model(provider, model)
        if candidate is None or not candidate.enabled:
            return None
        return candidate.billing_class.value

    def _invoke_candidate(
        self,
        candidate: ModelCandidate,
        messages: list[dict[str, str]],
        *,
        provider_api_keys: dict[str, str] | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        provider_api_keys = provider_api_keys or {}
        if not circuit_breaker.circuit_allows(candidate.provider, candidate.model_name):
            return self._error_response(
                provider=candidate.provider,
                model=candidate.model_name,
                message=(
                    f"Circuit open for {candidate.provider}/{candidate.model_name}; "
                    "temporarily skipped for cooldown"
                ),
                code="provider_error",
                retryable=True,
            )
        try:
            override_key = provider_api_keys.get(candidate.provider.lower())
            client = self._get_client(
                candidate.provider,
                candidate.model_name,
                api_key_override=override_key,
            )
            response = client.get_completion(messages=messages, **kwargs)
            response = self._normalize_empty_success_response(response)
            response = replace(
                response,
                metadata={
                    **(response.metadata or {}),
                    "provider_cost_owner": "customer" if override_key else "cortex",
                },
            )
            circuit_breaker.record_response(response)
            return response
        except Exception as e:
            logger.exception("Candidate invocation failed")
            circuit_breaker.record_failure(candidate.provider, candidate.model_name)
            return self._error_response(
                provider=candidate.provider,
                model=candidate.model_name,
                message=str(e),
                code="unknown",
            )

    def _explain_attempt_failure(self, response: UnifiedResponse, validation_reason: str) -> str:
        if response.is_error and response.error:
            details = response.error.details if isinstance(response.error.details, dict) else {}
            kind = str(details.get("kind") or "").strip()
            safe_message = get_client_safe_provider_error_message(response.error)
            kind_fragment = f" kind={kind}" if kind else ""
            return f"provider_error:{response.error.code}" f"{kind_fragment} message={safe_message}"

        reason_map = {
            "refusal": "model_refused_request_or_system_instruction_conflict",
            "too_short": "response_below_minimum_quality_length_threshold",
            "format_violation": "response_did_not_meet_required_output_format",
            "truncated": "response_truncated_before_completion",
            "timeout": "provider_timed_out",
            "rate_limit": "provider_rate_limited_request",
            "provider_error": "provider_returned_invalid_or_error_response",
            "latency_budget": "routing_latency_budget_exceeded",
            "max_attempts": "routing_max_attempts_reached",
        }
        return reason_map.get(validation_reason, f"validation_failed:{validation_reason}")

    def _update_routing_metadata_for_attempt(
        self,
        routing_md: dict[str, Any],
        *,
        attempt_number: int,
        tier: Tier,
        candidate: ModelCandidate,
        response: UnifiedResponse,
        validation_reason: str,
        validation_ok: bool,
    ) -> None:
        status = "success" if validation_ok else "failed"
        why_worked = (
            "response_passed_validator_checks_and_returned_usable_output" if validation_ok else None
        )
        why_failed = (
            None if validation_ok else self._explain_attempt_failure(response, validation_reason)
        )

        attempt_entry = {
            "attempt_number": attempt_number,
            "tier": tier.value,
            "provider": candidate.provider,
            "model": candidate.model_name,
            "billing_class": candidate.billing_class.value,
            "validation": validation_reason,
            "latency_ms": response.latency_ms,
            "status": status,
            "why_worked": why_worked,
            "why_failed": why_failed,
        }
        routing_md["attempts"].append(attempt_entry)

        plan_entry = None
        for item in routing_md.get("candidate_plan", []):
            if (
                item.get("provider") == candidate.provider
                and item.get("model") == candidate.model_name
                and item.get("status") == "pending"
            ):
                plan_entry = item
                break

        selected_item = {
            "attempt_number": attempt_number,
            "provider": candidate.provider,
            "model": candidate.model_name,
            "billing_class": candidate.billing_class.value,
            "tier": tier.value,
            "status": status,
            "why_selected": (plan_entry or {}).get(
                "why_selected",
                ["selected_by_runtime_fallback_or_tier_reselection"],
            ),
            "why_worked": why_worked,
            "why_failed": why_failed,
        }

        if plan_entry:
            plan_entry["status"] = status
            plan_entry["outcome_reason"] = "validator_ok" if validation_ok else validation_reason
            plan_entry["why_worked"] = why_worked
            plan_entry["why_failed"] = why_failed
            selected_item["order"] = plan_entry.get("order")

        routing_md.setdefault("selected_sequence", []).append(selected_item)

        seq = routing_md.get("selected_sequence", [])
        routing_md["first_selected_model"] = seq[0] if len(seq) >= 1 else None
        routing_md["second_selected_model"] = seq[1] if len(seq) >= 2 else None
        routing_md["third_selected_model"] = seq[2] if len(seq) >= 3 else None

    def _run_smart_attempt_loop(
        self,
        *,
        prompt: str,
        context: UserContext | None,
        messages: list[dict[str, str]],
        routing_mode: str,
        routing_constraints: RoutingConstraints | None,
        provider_api_keys: dict[str, str] | None = None,
        candidate_authorizer: Callable[[str, str], bool] | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        if not self._smart_router or not self._model_registry or not self._validator:
            return self._error_response(
                provider="orchestrator",
                model="smart_router",
                message="Smart routing not initialized",
                code="unknown",
            )

        routing_constraints = self._constrain_to_routable_providers(
            routing_constraints,
            provider_api_keys=provider_api_keys,
        )
        if routing_constraints.allowed_providers == []:
            return self._error_response(
                provider="orchestrator",
                model="smart_router",
                message=(
                    "No routable providers are available for smart routing. "
                    "Check provider API keys and installed SDK dependencies."
                ),
                code="provider_error",
            )

        start_time = time.monotonic()
        try:
            features, initial_tier, ordered_candidates, routing_md = (
                self._smart_router.route_once_plan(
                    prompt=prompt,
                    context=context,
                    routing_mode=routing_mode,
                    constraints=routing_constraints,
                    runtime_messages=messages,
                )
            )
        except Exception as e:
            logger.exception("Smart routing plan failed")
            return self._error_response(
                provider="orchestrator",
                model="smart_router",
                message=str(e),
                code="unknown",
            )

        defaults = self._model_registry.routing_defaults()
        max_attempts = int(defaults.get("max_attempts", 2))
        max_latency_ms = int(defaults.get("max_total_latency_ms", 12000))
        if routing_constraints and routing_constraints.max_total_latency_ms is not None:
            max_latency_ms = int(routing_constraints.max_total_latency_ms)

        policy = FallbackPolicy(
            max_attempts=max_attempts,
            max_total_latency_ms=max_latency_ms,
            allow_escalation=True,
        )

        current_tier = initial_tier
        current_candidates = list(ordered_candidates)
        attempt_index = 0
        best_non_error: UnifiedResponse | None = None
        final_response: UnifiedResponse | None = None
        last_response: UnifiedResponse | None = None

        while attempt_index < policy.max_attempts:
            if not current_candidates:
                next_tier = None
                if self._model_registry:
                    next_tier = self._model_registry.next_tier(current_tier)
                if not next_tier:
                    break
                current_tier = next_tier
                candidates = self._model_registry.get_candidates(current_tier, routing_constraints)
                selection = self._selector.select(features, candidates, routing_constraints)
                current_candidates = [
                    selection.primary_candidate,
                    *selection.fallback_candidates,
                ]
                continue

            candidate = current_candidates.pop(0)
            if candidate_authorizer is not None:
                try:
                    authorized = candidate_authorizer(
                        candidate.provider,
                        candidate.model_name,
                    )
                except Exception:
                    logger.exception(
                        "Smart candidate credit authorization failed",
                        extra={
                            "extra_fields": {
                                "provider": candidate.provider,
                                "model": candidate.model_name,
                            }
                        },
                    )
                    authorized = False
                if not authorized:
                    routing_md.setdefault("credit_exclusions", []).append(
                        {
                            "provider": candidate.provider,
                            "model": candidate.model_name,
                            "reason": "supplemental_reservation_denied",
                        }
                    )
                    continue
            resp = self._invoke_candidate(
                candidate,
                messages,
                provider_api_keys=provider_api_keys,
                **kwargs,
            )
            prev_response = last_response
            last_response = resp
            if attempt_index > 0 and prev_response:
                resp = replace(
                    resp,
                    attempt=attempt_index + 1,
                    fallback_from=f"{prev_response.provider}:{prev_response.model}",
                )
            else:
                resp = replace(resp, attempt=attempt_index + 1)

            validation = self._validator.validate(features, routing_constraints, resp)
            self._update_routing_metadata_for_attempt(
                routing_md,
                attempt_number=attempt_index + 1,
                tier=current_tier,
                candidate=candidate,
                response=resp,
                validation_reason=validation.reason,
                validation_ok=validation.ok,
            )

            if not resp.is_error and best_non_error is None:
                best_non_error = resp

            if validation.ok:
                final_response = resp
                break

            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            decision = self._fallback_manager.decide(
                current_tier=current_tier,
                validation=validation,
                attempt_index=attempt_index,
                elapsed_ms=elapsed_ms,
                remaining_same_tier_candidates=len(current_candidates),
                policy=policy,
                next_tier_fn=self._model_registry.next_tier,
            )
            if routing_md.get("selected_sequence"):
                latest_selected = routing_md["selected_sequence"][-1]
                latest_selected["next_action"] = (
                    decision.action.value
                    if hasattr(decision.action, "value")
                    else str(decision.action)
                )
                latest_selected["next_action_reason"] = decision.reason

            if decision.action == "retry_same_tier":
                routing_md["fallback_used"] = True
                attempt_index += 1
                continue
            if decision.action == "escalate_tier" and decision.next_tier:
                routing_md["fallback_used"] = True
                current_tier = decision.next_tier
                candidates = self._model_registry.get_candidates(current_tier, routing_constraints)
                selection = self._selector.select(features, candidates, routing_constraints)
                current_candidates = [
                    selection.primary_candidate,
                    *selection.fallback_candidates,
                ]
                attempt_index += 1
                continue

            final_response = resp
            break

        if not final_response:
            if best_non_error:
                final_response = best_non_error
            elif last_response:
                final_response = last_response
            else:
                return self._error_response(
                    provider="orchestrator",
                    model="smart_router",
                    message="No available model candidates",
                    code="unknown",
                )

        routing_md["attempt_count"] = len(routing_md["attempts"])
        routing_md["final_tier"] = current_tier.value

        md = final_response.metadata or {}
        md["routing"] = routing_md
        final_response = replace(final_response, metadata=md)

        return final_response

    # ---------- public API ----------
    def ask(
        self,
        prompt: str,
        model_type: str | None = None,
        context: UserContext | None = None,
        model_name: str | None = None,
        token_tracker: TokenTracker | None = None,
        research_mode: str = "auto",
        routing_mode: str = "smart",
        routing_constraints: dict[str, Any] | None = None,
        **kwargs,
    ) -> UnifiedResponse:
        try:
            provider_api_keys = kwargs.pop("provider_api_keys", {}) or {}
            if not isinstance(provider_api_keys, dict):
                provider_api_keys = {}
            candidate_authorizer = kwargs.pop("_smart_candidate_authorizer", None)
            prepared_messages = kwargs.pop("_prepared_messages", None)
            prepared_research_metadata = kwargs.pop("_prepared_research_metadata", None)
            prepared_opt_metadata = kwargs.pop("_prepared_opt_metadata", None)
            prepared_prompt = kwargs.pop("_prepared_prompt", None)

            if prepared_messages is not None:
                optimized_prompt = str(prepared_prompt or prompt)
                opt_metadata = (
                    prepared_opt_metadata if isinstance(prepared_opt_metadata, dict) else {}
                )
                messages = [dict(message) for message in prepared_messages]
                research_metadata = (
                    prepared_research_metadata
                    if isinstance(prepared_research_metadata, dict)
                    else self._empty_research_metadata()
                )
            else:
                prepared_turn = self.prepare_messages_for_turn(
                    prompt=prompt,
                    context=context,
                    research_mode=research_mode,
                )
                optimized_prompt = prepared_turn["prompt"]
                messages = prepared_turn["messages"]
                research_metadata = prepared_turn["research_metadata"]
                opt_metadata = prepared_turn["optimization_metadata"]

            kwargs.setdefault(
                "_cache_scope",
                self._cache_scope(context=context, messages=messages, mode="ask"),
            )

            routing_mode_norm = (routing_mode or "").lower().strip()
            # Only use smart routing when explicitly requested.
            # Any other value (e.g., "legacy") preserves direct model invocation.
            use_smart = routing_mode_norm in {"smart", "cheap", "strong"}
            explicit_model_selected = bool(model_type and model_name)
            record_direct_circuit = False

            if explicit_model_selected:
                is_valid, validation_error = self._validate_explicit_model_selection(
                    model_type, model_name
                )
                if not is_valid:
                    return self._error_response(
                        provider=model_type or "unknown",
                        model=model_name or "unknown",
                        message=validation_error,
                        code="bad_request",
                    )

                provider_norm = (model_type or "").strip().lower()
                if not circuit_breaker.circuit_allows(provider_norm, model_name or ""):
                    return self._error_response(
                        provider=provider_norm or "unknown",
                        model=model_name or "unknown",
                        message=f"Circuit open for {provider_norm}/{model_name}; try later",
                        code="provider_error",
                        retryable=True,
                    )

                client = self._get_client(
                    model_type,
                    model_name,
                    api_key_override=provider_api_keys.get(provider_norm),
                )
                resp = client.get_completion(messages=messages, **kwargs)
                resp = self._normalize_empty_success_response(resp)
                resp = replace(
                    resp,
                    metadata={
                        **(resp.metadata or {}),
                        "provider_cost_owner": (
                            "customer" if provider_api_keys.get(provider_norm) else "cortex"
                        ),
                    },
                )
                record_direct_circuit = True
                md = resp.metadata or {}
                md["routing"] = {
                    "mode": "explicit",
                    "initial_tier": "N/A",
                    "final_tier": "N/A",
                    "attempt_count": 1,
                    "fallback_used": False,
                    "attempts": [
                        {
                            "tier": "N/A",
                            "provider": model_type,
                            "model": model_name,
                            "validation": "ok",
                            "latency_ms": resp.latency_ms,
                        }
                    ],
                    "decision_reasons": ["explicit_model_selection"],
                }
                resp = replace(resp, metadata=md)
            elif use_smart:
                constraints = self._build_routing_constraints(routing_constraints)
                if model_type:
                    if constraints is None:
                        constraints = RoutingConstraints(preferred_provider=model_type)
                    elif not constraints.preferred_provider:
                        constraints = replace(constraints, preferred_provider=model_type)

                resp = self._run_smart_attempt_loop(
                    prompt=optimized_prompt,
                    context=context,
                    messages=messages,
                    routing_mode=routing_mode_norm,
                    routing_constraints=constraints,
                    provider_api_keys=provider_api_keys,
                    candidate_authorizer=(
                        candidate_authorizer if callable(candidate_authorizer) else None
                    ),
                    **kwargs,
                )
            else:
                if not model_type:
                    return self._error_response(
                        provider="orchestrator",
                        model=model_name or "default",
                        message="provider is required when routing_mode is not smart/cheap/strong",
                        code="bad_request",
                    )

                if model_name:
                    is_valid, validation_error = self._validate_explicit_model_selection(
                        model_type, model_name
                    )
                    if not is_valid:
                        return self._error_response(
                            provider=model_type,
                            model=model_name,
                            message=validation_error,
                            code="bad_request",
                        )

                provider_norm = (model_type or "").strip().lower()
                if not circuit_breaker.circuit_allows(provider_norm, model_name or ""):
                    return self._error_response(
                        provider=provider_norm or "unknown",
                        model=model_name or "unknown",
                        message=f"Circuit open for {provider_norm}/{model_name}; try later",
                        code="provider_error",
                        retryable=True,
                    )

                client = self._get_client(
                    model_type,
                    model_name,
                    api_key_override=provider_api_keys.get(provider_norm),
                )
                resp = client.get_completion(messages=messages, **kwargs)
                resp = self._normalize_empty_success_response(resp)
                resp = replace(
                    resp,
                    metadata={
                        **(resp.metadata or {}),
                        "provider_cost_owner": (
                            "customer" if provider_api_keys.get(provider_norm) else "cortex"
                        ),
                    },
                )
                record_direct_circuit = True

            # Merge research and optimization metadata into response
            md = resp.metadata or {}
            merged_md = {**md, **research_metadata, **opt_metadata, "research_mode": research_mode}
            resp = replace(resp, metadata=merged_md)

            if record_direct_circuit:
                circuit_breaker.record_response(resp)

            # Update token tracker here (business layer)
            if token_tracker:
                token_tracker.update(resp)

            return resp

        except Exception as e:
            logger.exception("ask() failed")
            return self._error_response(
                provider=model_type or "orchestrator",
                model=model_name or "default",
                message=str(e),
                code="unknown",
            )

    def compare(
        self,
        prompt: str,
        models_list: list[dict[str, str]],
        context: UserContext | None = None,
        timeout_s: float | None = None,
        token_tracker: TokenTracker | None = None,
        research_mode: str = "auto",
        request_group_id: str | None = None,
        **kwargs,
    ) -> MultiUnifiedResponse:
        request_group_id = request_group_id or str(uuid.uuid4())
        responses: list[UnifiedResponse] = []
        research_metadata: dict[str, Any] = {
            "research_used": False,
            "research_reused": False,
            "research_provider_credits_used": 0,
            "research_provider_credits_estimated": False,
            "research_topic": None,
            "research_error": "not_performed",
            "sources": [],
        }
        provider_api_keys = kwargs.pop("provider_api_keys", {}) or {}
        if not isinstance(provider_api_keys, dict):
            provider_api_keys = {}
        per_client_generation = kwargs.pop("_per_client_generation", {}) or {}
        if not isinstance(per_client_generation, dict):
            per_client_generation = {}

        def with_turn_metadata(response: UnifiedResponse) -> UnifiedResponse:
            normalized = self._normalize_empty_success_response(response)
            metadata = normalized.metadata or {}
            return replace(
                normalized,
                metadata={
                    **metadata,
                    **research_metadata,
                    "research_mode": research_mode,
                    "provider_cost_owner": (
                        "customer"
                        if provider_api_keys.get(response.provider.lower())
                        else "cortex"
                    ),
                },
            )

        try:
            # Optimize prompt if enabled (ONCE for all models - fair comparison)
            optimized_prompt, opt_metadata = self._optimize_prompt_if_enabled(
                prompt,
                context=context,
            )
            if opt_metadata.get("optimization_used"):
                logger.debug("Using optimized prompt for comparison")

            messages = self._build_messages(optimized_prompt, context, research_mode=research_mode)

            # Apply research ONCE for all models (compare fairness)
            if self.research_service:
                messages, research_metadata = self._apply_research_if_needed(
                    prompt=optimized_prompt,
                    messages=messages,
                    research_mode=research_mode,
                    context=context,
                )
            else:
                research_metadata = {
                    "research_used": False,
                    "research_reused": False,
                    "research_provider_credits_used": 0,
                    "research_provider_credits_estimated": False,
                    "research_topic": None,
                    "research_error": "service_not_configured",
                    "sources": [],
                }

            kwargs.setdefault(
                "_cache_scope",
                self._cache_scope(context=context, messages=messages, mode="compare"),
            )

            clients: list[BaseAIClient] = []

            for cfg in models_list:
                provider = (cfg.get("provider") or "").lower().strip()
                model = (cfg.get("model") or "").strip()

                if not provider or not model:
                    responses.append(
                        self._error_response(
                            provider=provider or "unknown",
                            model=model or "unknown",
                            message=f"Invalid model config: {cfg}",
                            code="bad_request",
                        )
                    )
                    continue

                if not circuit_breaker.circuit_allows(provider, model):
                    responses.append(
                        self._error_response(
                            provider=provider,
                            model=model,
                            message=f"Circuit open for {provider}/{model}; try later",
                            code="provider_error",
                            retryable=True,
                        )
                    )
                    continue

                try:
                    c = self._get_client(
                        provider,
                        model,
                        api_key_override=provider_api_keys.get(provider),
                    )
                    clients.append(c)
                except Exception as init_err:
                    circuit_breaker.record_failure(provider, model)
                    responses.append(
                        self._error_response(
                            provider=provider,
                            model=model,
                            message=str(init_err),
                            code="auth" if "API_KEY" in str(init_err) else "unknown",
                        )
                    )

            # If we have no valid clients, still return a MultiUnifiedResponse (no exceptions)
            if not clients:
                responses = [with_turn_metadata(response) for response in responses]
                if token_tracker:
                    for r in responses:
                        token_tracker.update(r)
                return MultiUnifiedResponse.from_responses(request_group_id, prompt, responses)

            # Execute comparisons with research-injected messages (parallel inside MultiModelOrchestrator)
            result = self._multi_orchestrator.get_comparisons_sync(
                prompt=prompt,
                clients=clients,
                timeout_s=timeout_s,
                request_group_id=request_group_id,
                messages=messages,  # Pass research-injected messages to all models
                _per_client_generation=per_client_generation,
                **kwargs,
            )

            # Merge init-time failures + runtime results
            responses.extend(result.responses)

            # Merge shared research metadata into each provider response without
            # rewriting the provider's successful answer text.
            updated_responses = []
            for resp in responses:
                resp_final = with_turn_metadata(resp)
                updated_responses.append(resp_final)
                circuit_breaker.record_response(resp_final)

            if token_tracker:
                for r in updated_responses:
                    token_tracker.update(r)

            return MultiUnifiedResponse.from_responses(request_group_id, prompt, updated_responses)

        except Exception as e:
            logger.exception("compare() failed")
            responses.append(
                self._error_response(
                    provider="orchestrator",
                    model="compare",
                    message=str(e),
                    code="unknown",
                )
            )
            responses = [with_turn_metadata(response) for response in responses]
            if token_tracker:
                for r in responses:
                    token_tracker.update(r)
            return MultiUnifiedResponse.from_responses(request_group_id, prompt, responses)

    # --- keep these helpers (your CLI uses them) ---
    def create_token_tracker(self, model_type: str, model_name: str | None = None) -> TokenTracker:
        return TokenTracker(model_type=model_type, model_name=model_name)

    def create_cost_calculator(
        self, model_type: str, model_name: str | None = None
    ) -> CostCalculator:
        if not model_name:
            if model_type.lower() == "openai":
                model_name = os.getenv("DEFAULT_OPENAI_MODEL", "gpt-3.5-turbo")
            elif model_type.lower() == "gemini":
                model_name = os.getenv("DEFAULT_GEMINI_MODEL", "gemini-2.5-flash-lite")
            elif model_type.lower() == "deepseek":
                model_name = os.getenv("DEFAULT_DEEPSEEK_MODEL", "deepseek-chat")
            elif model_type.lower() == "grok":
                model_name = os.getenv("DEFAULT_GROK_MODEL", "grok-4-latest")
        return CostCalculator(model_type=model_type, model_name=model_name)
