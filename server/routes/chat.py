"""Chat endpoint for single AI model requests."""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from config.provider_catalog import (
    get_provider_default_model_envs,
    get_provider_default_models,
    get_provider_ids,
)
from models.user_context import UserContext
from orchestrator.core import CortexOrchestrator
from orchestrator.model_registry import ModelRegistry
from server import attachments as attachments_service
from server.billing.enforcement_service import (
    BillableModelUsage,
    ReservedRequestUsage,
    resolve_model_target,
)
from server.billing.credit_calculator import research_credit_usage_from_metadata
from server.billing.entitlement_service import ModelTargetIntent
from server.billing.errors import enforcement_http_exception
from server.dependencies import AuthResult, get_auth, get_orchestrator
from server.generation_service import annotate_response, resolve_request_budget
from server.generation_service import constrain_budget_to_reservation
from server import persistence as persistence_service
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.requests import ChatRequest
from server.schemas.responses import ChatResponseDTO
from server.stream_observability import StreamLogContext
from server.utils import (
    get_client_safe_error_display_text,
    normalize_empty_success_response,
    sanitize_provider_error_response,
    validate_and_trim_context,
)
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["Chat"])
STREAM_LINE_DELAY_S = 0.1
STREAM_HEARTBEAT_INTERVAL_S = 15.0
API_DB_ENABLED = persistence_service.API_DB_ENABLED
ApiKeyPersistenceResolution = persistence_service.ApiKeyPersistenceResolution

logger = get_logger(__name__)
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="Chat",
    rejection_event="chat.route.rejected.auth_mode",
    logger=logger,
)

DEFAULT_MODELS = get_provider_default_models()
DEFAULT_MODEL_ENVS = get_provider_default_model_envs()
SUPPORTED_PROVIDERS = tuple(DEFAULT_MODELS.keys())
SMART_ROUTING_MODE = "smart"
LEGACY_ROUTING_MODE = "legacy"
ATTACHMENTS_ONLY_FALLBACK_PROMPT = "Please analyze the attached file(s)."
TRUE_SMART_ROUTING_ENABLED = os.getenv(
    "ENABLE_TRUE_SMART_CHAT_ROUTING", "true"
).strip().lower() in {"1", "true", "yes", "on"}

_resolve_and_enforce_caps = persistence_service.resolve_and_enforce_usage_caps
_persist_chat_interaction = persistence_service.persist_chat_interaction
_resolve_runtime_byok_provider_keys = persistence_service.resolve_runtime_byok_provider_keys
_reserve_subscription_usage = persistence_service.reserve_subscription_usage
_finalize_subscription_usage = persistence_service.finalize_subscription_usage
_release_subscription_usage = persistence_service.release_subscription_usage


@dataclass(frozen=True)
class ChatExecutionPlan:
    strategy: str
    model_type: str | None
    model_name: str | None
    routing_mode: str
    routing_constraints: dict[str, Any] | None
    preview_provider: str
    preview_model: str


def _default_model_for_provider(provider: str) -> str:
    env_key = DEFAULT_MODEL_ENVS.get(provider)
    default_model = DEFAULT_MODELS[provider]
    if not env_key:
        return default_model
    return os.getenv(env_key, default_model)


def _first_supported_provider(
    candidates: list[str] | tuple[str, ...],
    *,
    fallback: str | None = None,
) -> str:
    for candidate in candidates:
        provider = _normalize_provider(candidate)
        if provider:
            return provider

    if fallback:
        provider = _normalize_provider(fallback)
        if provider:
            return provider

    return SUPPORTED_PROVIDERS[0]


def _parse_float_env(name: str) -> float | None:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except Exception:
        logger.warning("Ignoring invalid %s value: %s", name, raw)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive %s value: %s", name, raw)
        return None
    return value


def _parse_int_env(name: str) -> int | None:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except Exception:
        logger.warning("Ignoring invalid %s value: %s", name, raw)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive %s value: %s", name, raw)
        return None
    return value


def _stream_heartbeat_interval_s() -> float:
    raw = str(os.getenv("STREAM_HEARTBEAT_INTERVAL_SECONDS", "") or "").strip()
    if not raw:
        return STREAM_HEARTBEAT_INTERVAL_S
    try:
        value = float(raw)
    except Exception:
        logger.warning("Ignoring invalid STREAM_HEARTBEAT_INTERVAL_SECONDS: %s", raw)
        return STREAM_HEARTBEAT_INTERVAL_S
    return value if value > 0 else 0.0


def _normalize_provider(provider: str | None) -> str | None:
    value = str(provider or "").strip().lower()
    if value in SUPPORTED_PROVIDERS:
        return value
    return None


def _normalize_provider_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    for item in values:
        provider = _normalize_provider(item)
        if provider and provider not in normalized:
            normalized.append(provider)
    return normalized


def _build_chat_routing_constraints() -> dict[str, Any] | None:
    constraints: dict[str, Any] = {}

    max_cost_usd = _parse_float_env("SMART_CHAT_MAX_COST_USD")
    if max_cost_usd is not None:
        constraints["max_cost_usd"] = max_cost_usd

    max_total_latency_ms = _parse_int_env("SMART_CHAT_MAX_TOTAL_LATENCY_MS")
    if max_total_latency_ms is not None:
        constraints["max_total_latency_ms"] = max_total_latency_ms

    min_context_limit = _parse_int_env("SMART_CHAT_MIN_CONTEXT_LIMIT")
    if min_context_limit is not None:
        constraints["min_context_limit"] = min_context_limit

    preferred_provider = _normalize_provider(os.getenv("SMART_CHAT_PREFERRED_PROVIDER"))
    if preferred_provider:
        constraints["preferred_provider"] = preferred_provider

    allowed_raw = str(os.getenv("SMART_CHAT_ALLOWED_PROVIDERS", "") or "").strip()
    if allowed_raw:
        allowed = _normalize_provider_list([part.strip() for part in allowed_raw.split(",")])
        if allowed:
            constraints["allowed_providers"] = allowed

    return constraints or None


def _pick_smart_provider(prompt: str, *, smart_mode: bool, research_mode: bool) -> str:
    return _pick_smart_provider_from_candidates(
        prompt,
        smart_mode=smart_mode,
        research_mode=research_mode,
        candidates=None,
    )


def _pick_smart_provider_from_candidates(
    prompt: str,
    *,
    smart_mode: bool,
    research_mode: bool,
    candidates: list[str] | tuple[str, ...] | None,
) -> str:
    text = (prompt or "").lower()
    candidate_pool = get_provider_ids() if candidates is None else list(candidates)
    candidate_set = {provider for provider in candidate_pool if _normalize_provider(provider)}
    default_provider = _first_supported_provider(candidate_pool, fallback="openai")

    def _pick_ranked(preferred: tuple[str, ...]) -> str:
        ranked = [provider for provider in preferred if provider in candidate_set]
        return _first_supported_provider(ranked, fallback=default_provider)

    if not smart_mode:
        return _pick_ranked(("gemini",))

    if research_mode:
        return _pick_ranked(("openai",))

    code_signals = (
        "code",
        "bug",
        "debug",
        "stack trace",
        "python",
        "javascript",
        "typescript",
        "sql",
        "api",
        "refactor",
        "algorithm",
        "function",
        "class ",
        "exception",
    )
    deep_reasoning_signals = (
        "analyze",
        "analysis",
        "tradeoff",
        "architecture",
        "design",
        "compare",
        "step by step",
        "explain why",
        "root cause",
        "research",
        "cite",
    )
    creative_signals = ("poem", "story", "creative", "brainstorm", "tagline", "tweet")

    if any(signal in text for signal in code_signals):
        return _pick_ranked(("deepseek", "claude", "openai"))
    if any(signal in text for signal in deep_reasoning_signals) or len(text) > 900:
        return _pick_ranked(("openai", "claude"))
    if any(signal in text for signal in creative_signals):
        return _pick_ranked(("grok", "gemini", "claude"))
    return _pick_ranked(("gemini", "claude"))


def _resolve_chat_execution_plan(
    request: ChatRequest,
    *,
    effective_prompt: str,
    context: UserContext | None,
    orchestrator: CortexOrchestrator,
    provider_api_keys: dict[str, str] | None = None,
    attachment_compatible_providers: list[str] | None = None,
    allowed_billing_classes: frozenset[str] | None = None,
    allowed_models: tuple[str, ...] | None = None,
) -> ChatExecutionPlan:
    manual_provider = (request.provider or "").strip().lower()
    manual_model = (request.model or "").strip()
    normalized_manual_provider = _normalize_provider(manual_provider)

    if normalized_manual_provider:
        chosen_model = manual_model or _default_model_for_provider(normalized_manual_provider)
        return ChatExecutionPlan(
            strategy="explicit_manual",
            model_type=normalized_manual_provider,
            model_name=chosen_model,
            routing_mode=LEGACY_ROUTING_MODE,
            routing_constraints=None,
            preview_provider=normalized_manual_provider,
            preview_model=chosen_model,
        )

    routing = request.routing
    smart_mode = True if routing is None else bool(routing.smart_mode)
    research_mode = False if routing is None else bool(routing.research_mode)

    if TRUE_SMART_ROUTING_ENABLED and smart_mode:
        constraints = _build_chat_routing_constraints()
        reasoning = request.generation.reasoning if request.generation is not None else None
        requested_reasoning_mode = str(getattr(reasoning, "mode", "auto") or "auto")
        requested_reasoning_effort = str(getattr(reasoning, "effort", "auto") or "auto")
        if requested_reasoning_mode != "auto" or requested_reasoning_effort != "auto":
            constraints = dict(constraints or {})
            constraints["reasoning_mode"] = requested_reasoning_mode
            constraints["reasoning_effort"] = requested_reasoning_effort
        if allowed_billing_classes is not None:
            constraints = dict(constraints or {})
            constraints["allowed_billing_classes"] = sorted(allowed_billing_classes)
        if allowed_models is not None:
            constraints = dict(constraints or {})
            constraints["allowed_models"] = list(allowed_models)
        if attachment_compatible_providers:
            compatible_providers = _normalize_provider_list(attachment_compatible_providers)
            if compatible_providers:
                constraints = dict(constraints or {})
                existing_allowed = _normalize_provider_list(
                    constraints.get("allowed_providers")
                    if isinstance(constraints.get("allowed_providers"), (list, tuple))
                    else None
                )
                if existing_allowed:
                    compatible_set = set(compatible_providers)
                    intersected = [
                        provider for provider in existing_allowed if provider in compatible_set
                    ]
                    if not intersected:
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "no_attachment_compatible_provider",
                                "message": (
                                    "No providers remain after applying SMART_CHAT_ALLOWED_PROVIDERS "
                                    "and attachment compatibility filters."
                                ),
                            },
                        )
                    constraints["allowed_providers"] = intersected
                else:
                    constraints["allowed_providers"] = compatible_providers

        preview = None
        preview_fn = getattr(orchestrator, "preview_smart_target", None)
        if callable(preview_fn):
            preview = preview_fn(
                prompt=effective_prompt,
                context=context,
                routing_mode=SMART_ROUTING_MODE,
                routing_constraints=constraints,
                provider_api_keys=provider_api_keys,
            )

        preview_provider: str | None = None
        preview_model: str | None = None
        if isinstance(preview, tuple) and len(preview) == 2:
            pp = _normalize_provider(preview[0])
            pm = str(preview[1] or "").strip()
            if pp and pm:
                preview_provider, preview_model = pp, pm

        if not preview_provider or not preview_model:
            available_providers: list[str] | None = None
            available_fn = getattr(orchestrator, "available_providers", None)
            if callable(available_fn):
                available_providers = available_fn(provider_api_keys=provider_api_keys)
            if constraints:
                allowed_providers = _normalize_provider_list(
                    constraints.get("allowed_providers")
                    if isinstance(constraints.get("allowed_providers"), (list, tuple))
                    else None
                )
                if allowed_providers:
                    if available_providers is None:
                        available_providers = allowed_providers
                    else:
                        allowed_set = set(allowed_providers)
                        available_providers = [
                            provider for provider in available_providers if provider in allowed_set
                        ]
            if available_providers is not None and len(available_providers) == 0:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "no_attachment_compatible_provider",
                        "message": (
                            "No providers available for this request after attachment compatibility preflight."
                        ),
                    },
                )

            fallback_provider = _pick_smart_provider_from_candidates(
                request.prompt,
                smart_mode=True,
                research_mode=research_mode,
                candidates=available_providers,
            )
            preview_provider = fallback_provider
            preview_model = _default_model_for_provider(fallback_provider)

        return ChatExecutionPlan(
            strategy="smart_orchestrator",
            model_type=None,
            model_name=None,
            routing_mode=SMART_ROUTING_MODE,
            routing_constraints=constraints,
            preview_provider=preview_provider,
            preview_model=preview_model,
        )

    provider = _pick_smart_provider(
        request.prompt,
        smart_mode=smart_mode,
        research_mode=research_mode,
    )
    model = _default_model_for_provider(provider)
    return ChatExecutionPlan(
        strategy="legacy_auto",
        model_type=provider,
        model_name=model,
        routing_mode=LEGACY_ROUTING_MODE,
        routing_constraints=None,
        preview_provider=provider,
        preview_model=model,
    )


def _providers_for_runtime_keys(plan: ChatExecutionPlan) -> list[str]:
    if plan.strategy != "smart_orchestrator":
        return [plan.preview_provider]

    constraints = plan.routing_constraints or {}
    allowed = constraints.get("allowed_providers") or constraints.get("allow_providers")
    normalized_allowed = _normalize_provider_list(
        allowed if isinstance(allowed, (list, tuple)) else None
    )
    if normalized_allowed:
        return normalized_allowed
    return list(SUPPORTED_PROVIDERS)


def _build_user_context(context_req):
    """Convert request context to UserContext dataclass."""
    if not context_req:
        return None

    history = []
    if context_req.conversation_history:
        history = [
            {"role": item.role, "content": item.content}
            for item in context_req.conversation_history
        ]

    return UserContext(session_id=context_req.session_id, conversation_history=history)


def _resolve_effective_prompt(prompt: str | None, *, has_attachments: bool) -> str:
    value = str(prompt or "").strip()
    if value:
        return value
    if has_attachments:
        return ATTACHMENTS_ONLY_FALLBACK_PROMPT
    return ""


def _iter_stream_lines(text: str):
    """Split response text into line chunks while preserving newline boundaries."""
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    if len(lines) <= 1:
        # If the model returns one long paragraph, chunk it for smoother streaming.
        chunk_size = 120
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
    return lines


def _to_ndjson(event: dict) -> str:
    """Serialize one stream event as NDJSON."""
    return json.dumps(event, ensure_ascii=False) + "\n"


def _research_was_performed(response: object) -> bool:
    metadata = getattr(response, "metadata", None)
    return research_credit_usage_from_metadata(metadata).provider_credits_used > 0


def _successful_billing_targets(
    response: object,
    *,
    orchestrator: CortexOrchestrator,
) -> tuple[ModelTargetIntent, ...]:
    if bool(getattr(response, "is_error", False)):
        return ()
    return (
        resolve_model_target(
            provider=str(getattr(response, "provider", "") or ""),
            model=str(
                getattr(response, "requested_model", None) or getattr(response, "model", "") or ""
            ),
            orchestrator=orchestrator,
        ),
    )


def _billing_input_text(request: ChatRequest) -> str:
    parts = [str(request.prompt or "")]
    if request.context and request.context.conversation_history:
        parts.extend(str(item.content or "") for item in request.context.conversation_history)
    return "\n".join(part for part in parts if part)


def _billing_materialized_input_text(
    request: ChatRequest,
    inference_attachments: list[dict[str, Any]],
) -> str:
    parts = [_billing_input_text(request)]
    parts.extend(
        str(item.get("extracted_text") or "")
        for item in inference_attachments
        if str(item.get("extracted_text") or "").strip()
    )
    return "\n".join(part for part in parts if part)


def _billing_operation_type(request: ChatRequest) -> str:
    if request.regeneration is not None:
        return "regenerate"
    if request.context and request.context.conversation_history:
        return "follow_up"
    return "ask"


def _billable_model_usages(response: object) -> tuple[BillableModelUsage, ...]:
    if bool(getattr(response, "is_error", False)):
        return ()
    usage = getattr(response, "token_usage", None)
    return (
        BillableModelUsage(
            provider=str(getattr(response, "provider", "") or ""),
            model=str(
                getattr(response, "requested_model", None) or getattr(response, "model", "") or ""
            ),
            prompt_tokens=max(0, int(getattr(usage, "prompt_tokens", 0) or 0)),
            cached_input_tokens=max(0, int(getattr(usage, "cached_input_tokens", 0) or 0)),
            cache_write_tokens=max(0, int(getattr(usage, "cache_write_tokens", 0) or 0)),
            output_tokens=max(0, int(getattr(usage, "completion_tokens", 0) or 0)),
            reasoning_tokens=max(0, int(getattr(usage, "reasoning_tokens", 0) or 0)),
            output_text=str(getattr(response, "text", "") or ""),
            provider_cost_usd=max(0.0, float(getattr(response, "estimated_cost", 0.0) or 0.0)),
            pricing_snapshot=(
                dict(getattr(response, "pricing_snapshot", {}) or {})
                if isinstance(getattr(response, "pricing_snapshot", None), dict)
                else None
            ),
            pricing_version=str(getattr(response, "pricing_version", "") or "") or None,
            provider_cost_owner=str(
                (getattr(response, "metadata", {}) or {}).get("provider_cost_owner", "cortex")
            ),
        ),
    )


def _reserve_chat_usage(
    *,
    resolution: persistence_service.ApiKeyPersistenceResolution,
    request_id: str,
    execution_plan: ChatExecutionPlan,
    research_enabled: bool,
    orchestrator: CortexOrchestrator,
    resolved_attachments: list[attachments_service.ResolvedAttachment],
    input_text: str,
    initial_query: str,
    credit_activity_id: str | None,
    max_output_tokens: int | None,
    operation_type: str,
    smart_targets: tuple[tuple[str, str], ...] = (),
) -> ReservedRequestUsage:
    try:
        target_pairs = (
            smart_targets
            if execution_plan.strategy == "smart_orchestrator" and smart_targets
            else ((execution_plan.preview_provider, execution_plan.preview_model),)
        )
        targets = tuple(
            resolve_model_target(
                provider=provider,
                model=model,
                orchestrator=orchestrator,
            )
            for provider, model in target_pairs
        )
        return _reserve_subscription_usage(
            user_id=resolution.user_id,
            request_id=request_id,
            operation_type=operation_type,
            model_targets=targets,
            research_enabled=research_enabled,
            smart_routing=execution_plan.strategy == "smart_orchestrator",
            attachment_count=len(resolved_attachments),
            total_attachment_bytes=sum(item.size_bytes for item in resolved_attachments),
            attachment_sizes=tuple(item.size_bytes for item in resolved_attachments),
            input_text=input_text,
            initial_query=initial_query,
            credit_activity_id=credit_activity_id,
            max_output_tokens=max_output_tokens,
        )
    except HTTPException:
        raise
    except Exception as exc:
        http_error = enforcement_http_exception(exc)
        log = logger.exception if http_error.status_code >= 500 else logger.warning
        log(
            "Chat subscription preflight denied",
            extra={"extra_fields": {"request_id": request_id, "error_type": type(exc).__name__}},
        )
        raise http_error from exc


def _finalize_chat_usage(
    *,
    reservation: ReservedRequestUsage,
    response: object,
    orchestrator: CortexOrchestrator,
    settle_model_response: bool = True,
    attachments_present: bool = False,
) -> None:
    try:
        successful_targets = (
            _successful_billing_targets(
                response,
                orchestrator=orchestrator,
            )
            if settle_model_response
            else ()
        )
        research_usage = research_credit_usage_from_metadata(getattr(response, "metadata", None))
        _finalize_subscription_usage(
            reservation=reservation,
            successful_targets=successful_targets,
            model_usages=(_billable_model_usages(response) if settle_model_response else ()),
            research_provider_credits_used=research_usage.provider_credits_used,
            research_usage_estimated=research_usage.estimated,
            file_analysis_performed=attachments_present and bool(successful_targets),
        )
    except Exception as exc:
        logger.exception("Chat subscription usage finalization failed")
        _release_chat_usage_safely(
            reservation,
            reason="billing_finalization_failed",
        )
        raise enforcement_http_exception(exc) from exc


def _release_chat_usage_safely(
    reservation: ReservedRequestUsage | None,
    *,
    reason: str,
) -> None:
    if reservation is None:
        return
    try:
        _release_subscription_usage(reservation=reservation, reason=reason)
    except Exception:
        logger.exception("Chat subscription usage release failed")


@router.post("/chat", response_model=ChatResponseDTO)
async def chat(
    request: ChatRequest,
    http_request: Request,
    orchestrator: CortexOrchestrator = Depends(get_orchestrator),
    auth: AuthResult = Depends(get_auth),
):
    """Send a prompt to a single AI model and get a response."""
    req_id = str(getattr(http_request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    request.context = validate_and_trim_context(request.context)
    context = _build_user_context(request.context)
    requested_session_id = request.context.session_id if request.context else None
    force_new_session = bool(request.context and request.context.new_session)
    routing = request.routing
    research_mode = bool(routing and routing.research_mode)
    orchestrator_research_mode = "on" if research_mode else "off"
    if (
        research_mode
        and request.regeneration is not None
        and not request.regeneration.refresh_research
    ):
        orchestrator_research_mode = "auto"

    if request.regeneration is not None and not API_DB_ENABLED:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "compare_regeneration_requires_db",
                "message": "Compare response regeneration requires database persistence.",
            },
        )

    persistence_resolution: persistence_service.ApiKeyPersistenceResolution | None = None
    regeneration_context: persistence_service.CompareRegenerationContext | None = None
    provider_api_keys: dict[str, str] = {}
    if API_DB_ENABLED:
        persistence_resolution = _resolve_and_enforce_caps(auth=auth, request_id=req_id)
        persistence_service.persist_context_summary(
            context_request=request.context,
            user_id=persistence_resolution.user_id,
        )
        if request.regeneration is not None:
            regeneration_context = persistence_service.resolve_compare_regeneration_context(
                user_id=persistence_resolution.user_id,
                source_request_id=request.regeneration.source_request_id,
            )
            if (
                request.provider != regeneration_context.provider
                or request.model != regeneration_context.model
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "compare_regeneration_target_mismatch",
                        "message": "Regenerated Compare responses must use their original model.",
                    },
                )
            requested_session_id = str(regeneration_context.session_id)
            force_new_session = False
        provider_api_keys = _resolve_runtime_byok_provider_keys(
            resolution=persistence_resolution,
            providers=SUPPORTED_PROVIDERS,
        )

    resolved_attachments: list[attachments_service.ResolvedAttachment] = []
    attachment_compatible_providers: list[str] | None = None
    if request.attachments:
        if persistence_resolution is None:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "attachments_require_db",
                    "message": "Attachments require DATABASE_URL (DB mode).",
                },
            )
        resolved_attachments = attachments_service.resolve_request_attachments(
            user_id=persistence_resolution.user_id,
            attachments=request.attachments,
        )
        attachment_compatible_providers = attachments_service.compatible_providers_for_attachments(
            resolved_attachments
        )
        if not attachment_compatible_providers:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "no_attachment_compatible_provider",
                    "message": "No configured providers can accept the supplied attachments.",
                },
            )

    effective_prompt = _resolve_effective_prompt(
        request.prompt,
        has_attachments=bool(resolved_attachments),
    )
    execution_plan = _resolve_chat_execution_plan(
        request,
        effective_prompt=effective_prompt,
        context=context,
        orchestrator=orchestrator,
        provider_api_keys=provider_api_keys,
        attachment_compatible_providers=attachment_compatible_providers,
    )
    inference_attachments: list[dict[str, Any]] = []
    persistence_attachments: list[dict[str, Any]] = []
    if resolved_attachments:
        attachments_service.enforce_model_attachment_compatibility(
            provider=execution_plan.preview_provider,
            model=execution_plan.preview_model,
            attachments=resolved_attachments,
            mode="chat",
        )
        inference_attachments = attachments_service.materialize_inference_attachments(
            resolved_attachments,
            query_text=request.prompt,
        )
        persistence_attachments = attachments_service.build_request_attachment_persistence_items(
            resolved_attachments=resolved_attachments,
            inference_attachments=inference_attachments,
        )

    generation_registry = ModelRegistry.from_yaml()
    generation_input_text = _billing_materialized_input_text(request, inference_attachments)
    generation_budget = resolve_request_budget(
        provider=execution_plan.preview_provider,
        model=execution_plan.preview_model,
        generation=request.generation,
        legacy_max_tokens=request.max_tokens,
        input_text=generation_input_text,
        registry=generation_registry,
    )
    effective_max_tokens = generation_budget.effective_max_output_tokens
    kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    kwargs.update(generation_budget.provider_kwargs())
    if provider_api_keys:
        kwargs["provider_api_keys"] = provider_api_keys
    if inference_attachments:
        kwargs["attachments"] = inference_attachments
    kwargs["request_id"] = req_id

    billing_reservation: ReservedRequestUsage | None = None
    if API_DB_ENABLED and persistence_resolution is not None:
        smart_targets: tuple[tuple[str, str], ...] = ()
        if execution_plan.strategy == "smart_orchestrator":
            plan_fn = getattr(orchestrator, "plan_smart_targets", None)
            if callable(plan_fn):
                smart_targets = tuple(
                    plan_fn(
                        prompt=effective_prompt,
                        context=context,
                        routing_mode=execution_plan.routing_mode,
                        routing_constraints=execution_plan.routing_constraints,
                        provider_api_keys=provider_api_keys,
                    )
                )
        billing_reservation = _reserve_chat_usage(
            resolution=persistence_resolution,
            request_id=f"{req_id}:chat:{uuid4()}",
            execution_plan=execution_plan,
            research_enabled=research_mode,
            orchestrator=orchestrator,
            resolved_attachments=resolved_attachments,
            input_text=_billing_materialized_input_text(request, inference_attachments),
            initial_query=request.initial_query or effective_prompt,
            credit_activity_id=request.credit_activity_id,
            max_output_tokens=effective_max_tokens,
            operation_type=_billing_operation_type(request),
            smart_targets=smart_targets,
        )
        if execution_plan.strategy == "smart_orchestrator":
            try:
                allowed_models = tuple(
                    f"{item.provider}:{item.model}" for item in billing_reservation.smart_candidates
                )
                execution_plan = _resolve_chat_execution_plan(
                    request,
                    effective_prompt=effective_prompt,
                    context=context,
                    orchestrator=orchestrator,
                    provider_api_keys=provider_api_keys,
                    attachment_compatible_providers=attachment_compatible_providers,
                    allowed_billing_classes=billing_reservation.allowed_billing_classes,
                    allowed_models=allowed_models,
                )
                kwargs["_smart_candidate_authorizer"] = (
                    lambda provider, model: persistence_service.authorize_smart_fallback(
                        reservation=billing_reservation,
                        provider=provider,
                        model=model,
                    )
                )
                if resolved_attachments:
                    attachments_service.enforce_model_attachment_compatibility(
                        provider=execution_plan.preview_provider,
                        model=execution_plan.preview_model,
                        attachments=resolved_attachments,
                        mode="chat",
                    )
            except Exception:
                _release_chat_usage_safely(
                    billing_reservation,
                    reason="smart_routing_preflight_failed",
                )
                raise

        generation_budget = constrain_budget_to_reservation(
            generation_budget,
            billing_reservation,
            provider=execution_plan.preview_provider,
            model=execution_plan.preview_model,
        )
        kwargs.update(generation_budget.provider_kwargs())

    def resolve_smart_generation(provider: str, model: str):
        resolved = resolve_request_budget(
            provider=provider,
            model=model,
            generation=request.generation,
            legacy_max_tokens=request.max_tokens,
            input_text=generation_input_text,
            registry=generation_registry,
        )
        return constrain_budget_to_reservation(
            resolved,
            billing_reservation,
            provider=provider,
            model=model,
        )

    if execution_plan.strategy == "smart_orchestrator":
        kwargs["_smart_generation_resolver"] = resolve_smart_generation

    provider_completed = False
    try:
        response = await asyncio.to_thread(
            orchestrator.ask,
            prompt=effective_prompt,
            model_type=execution_plan.model_type,
            context=context,
            model_name=execution_plan.model_name,
            token_tracker=None,
            research_mode=orchestrator_research_mode,
            routing_mode=execution_plan.routing_mode,
            routing_constraints=execution_plan.routing_constraints,
            **kwargs,
        )
        provider_completed = True
        response = sanitize_provider_error_response(normalize_empty_success_response(response))
        if execution_plan.strategy == "smart_orchestrator":
            response_model = str(response.requested_model or response.model or "").strip()
            if generation_registry.find_model(response.provider, response_model) is not None:
                generation_budget = resolve_smart_generation(response.provider, response_model)
        response = annotate_response(response, generation_budget)
        if billing_reservation is not None:
            _finalize_chat_usage(
                reservation=billing_reservation,
                response=response,
                orchestrator=orchestrator,
                attachments_present=bool(resolved_attachments),
            )
    except BaseException:
        if not provider_completed:
            _release_chat_usage_safely(
                billing_reservation,
                reason="provider_execution_failed",
            )
        raise

    resolved_session_id = requested_session_id
    if API_DB_ENABLED and persistence_resolution is not None:
        try:
            resolved_session_id = _persist_chat_interaction(
                api_key=auth.api_key_or_none(),
                resolution=persistence_resolution,
                prompt=effective_prompt,
                response=response,
                requested_session_id=requested_session_id,
                research_mode=research_mode,
                force_new_session=force_new_session,
                attachments=persistence_attachments,
                regeneration=regeneration_context,
            )
        except Exception:
            logger.exception("Chat persistence failed in DB mode")

    dto = ChatResponseDTO.from_unified_response(
        response,
        session_id=resolved_session_id,
        response_version=(
            regeneration_context.response_revision if regeneration_context is not None else 1
        ),
    )
    return dto


@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    http_request: Request,
    orchestrator: CortexOrchestrator = Depends(get_orchestrator),
    auth: AuthResult = Depends(get_auth),
):
    """Stream a single-model chat response as NDJSON events."""
    req_id = str(getattr(http_request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    request.context = validate_and_trim_context(request.context)
    context = _build_user_context(request.context)
    requested_session_id = request.context.session_id if request.context else None
    force_new_session = bool(request.context and request.context.new_session)
    routing = request.routing
    research_mode = bool(routing and routing.research_mode)
    orchestrator_research_mode = "on" if research_mode else "off"
    if (
        research_mode
        and request.regeneration is not None
        and not request.regeneration.refresh_research
    ):
        orchestrator_research_mode = "auto"

    if request.regeneration is not None and not API_DB_ENABLED:
        raise HTTPException(
            status_code=501,
            detail={
                "code": "compare_regeneration_requires_db",
                "message": "Compare response regeneration requires database persistence.",
            },
        )

    persistence_resolution: persistence_service.ApiKeyPersistenceResolution | None = None
    regeneration_context: persistence_service.CompareRegenerationContext | None = None
    provider_api_keys: dict[str, str] = {}
    if API_DB_ENABLED:
        persistence_resolution = _resolve_and_enforce_caps(auth=auth, request_id=req_id)
        persistence_service.persist_context_summary(
            context_request=request.context,
            user_id=persistence_resolution.user_id,
        )
        if request.regeneration is not None:
            regeneration_context = persistence_service.resolve_compare_regeneration_context(
                user_id=persistence_resolution.user_id,
                source_request_id=request.regeneration.source_request_id,
            )
            if (
                request.provider != regeneration_context.provider
                or request.model != regeneration_context.model
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "compare_regeneration_target_mismatch",
                        "message": "Regenerated Compare responses must use their original model.",
                    },
                )
            requested_session_id = str(regeneration_context.session_id)
            force_new_session = False
        provider_api_keys = _resolve_runtime_byok_provider_keys(
            resolution=persistence_resolution,
            providers=SUPPORTED_PROVIDERS,
        )

    resolved_attachments: list[attachments_service.ResolvedAttachment] = []
    attachment_compatible_providers: list[str] | None = None
    if request.attachments:
        if persistence_resolution is None:
            raise HTTPException(
                status_code=501,
                detail={
                    "code": "attachments_require_db",
                    "message": "Attachments require DATABASE_URL (DB mode).",
                },
            )
        resolved_attachments = attachments_service.resolve_request_attachments(
            user_id=persistence_resolution.user_id,
            attachments=request.attachments,
        )
        attachment_compatible_providers = attachments_service.compatible_providers_for_attachments(
            resolved_attachments
        )
        if not attachment_compatible_providers:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "no_attachment_compatible_provider",
                    "message": "No configured providers can accept the supplied attachments.",
                },
            )

    effective_prompt = _resolve_effective_prompt(
        request.prompt,
        has_attachments=bool(resolved_attachments),
    )
    execution_plan = _resolve_chat_execution_plan(
        request,
        effective_prompt=effective_prompt,
        context=context,
        orchestrator=orchestrator,
        provider_api_keys=provider_api_keys,
        attachment_compatible_providers=attachment_compatible_providers,
    )
    inference_attachments: list[dict[str, Any]] = []
    persistence_attachments: list[dict[str, Any]] = []
    if resolved_attachments:
        attachments_service.enforce_model_attachment_compatibility(
            provider=execution_plan.preview_provider,
            model=execution_plan.preview_model,
            attachments=resolved_attachments,
            mode="chat",
        )
        inference_attachments = attachments_service.materialize_inference_attachments(
            resolved_attachments,
            query_text=request.prompt,
        )
        persistence_attachments = attachments_service.build_request_attachment_persistence_items(
            resolved_attachments=resolved_attachments,
            inference_attachments=inference_attachments,
        )
    target_provider = execution_plan.preview_provider
    target_model = execution_plan.preview_model

    generation_registry = ModelRegistry.from_yaml()
    generation_input_text = _billing_materialized_input_text(request, inference_attachments)
    generation_budget = resolve_request_budget(
        provider=execution_plan.preview_provider,
        model=execution_plan.preview_model,
        generation=request.generation,
        legacy_max_tokens=request.max_tokens,
        input_text=generation_input_text,
        registry=generation_registry,
    )
    effective_max_tokens = generation_budget.effective_max_output_tokens
    kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    kwargs.update(generation_budget.provider_kwargs())
    if provider_api_keys:
        kwargs["provider_api_keys"] = provider_api_keys
    if inference_attachments:
        kwargs["attachments"] = inference_attachments
    kwargs["request_id"] = req_id

    billing_reservation: ReservedRequestUsage | None = None
    if API_DB_ENABLED and persistence_resolution is not None:
        smart_targets: tuple[tuple[str, str], ...] = ()
        if execution_plan.strategy == "smart_orchestrator":
            plan_fn = getattr(orchestrator, "plan_smart_targets", None)
            if callable(plan_fn):
                smart_targets = tuple(
                    plan_fn(
                        prompt=effective_prompt,
                        context=context,
                        routing_mode=execution_plan.routing_mode,
                        routing_constraints=execution_plan.routing_constraints,
                        provider_api_keys=provider_api_keys,
                    )
                )
        billing_reservation = _reserve_chat_usage(
            resolution=persistence_resolution,
            request_id=f"{req_id}:chat-stream:{uuid4()}",
            execution_plan=execution_plan,
            research_enabled=research_mode,
            orchestrator=orchestrator,
            resolved_attachments=resolved_attachments,
            input_text=_billing_materialized_input_text(request, inference_attachments),
            initial_query=request.initial_query or effective_prompt,
            credit_activity_id=request.credit_activity_id,
            max_output_tokens=effective_max_tokens,
            operation_type=_billing_operation_type(request),
            smart_targets=smart_targets,
        )
        if execution_plan.strategy == "smart_orchestrator":
            try:
                allowed_models = tuple(
                    f"{item.provider}:{item.model}" for item in billing_reservation.smart_candidates
                )
                execution_plan = _resolve_chat_execution_plan(
                    request,
                    effective_prompt=effective_prompt,
                    context=context,
                    orchestrator=orchestrator,
                    provider_api_keys=provider_api_keys,
                    attachment_compatible_providers=attachment_compatible_providers,
                    allowed_billing_classes=billing_reservation.allowed_billing_classes,
                    allowed_models=allowed_models,
                )
                kwargs["_smart_candidate_authorizer"] = (
                    lambda provider, model: persistence_service.authorize_smart_fallback(
                        reservation=billing_reservation,
                        provider=provider,
                        model=model,
                    )
                )
                if resolved_attachments:
                    attachments_service.enforce_model_attachment_compatibility(
                        provider=execution_plan.preview_provider,
                        model=execution_plan.preview_model,
                        attachments=resolved_attachments,
                        mode="chat",
                    )
            except Exception:
                _release_chat_usage_safely(
                    billing_reservation,
                    reason="smart_routing_preflight_failed",
                )
                raise

        generation_budget = constrain_budget_to_reservation(
            generation_budget,
            billing_reservation,
            provider=execution_plan.preview_provider,
            model=execution_plan.preview_model,
        )
        kwargs.update(generation_budget.provider_kwargs())
        target_provider = execution_plan.preview_provider
        target_model = execution_plan.preview_model

    def resolve_smart_generation(provider: str, model: str):
        resolved = resolve_request_budget(
            provider=provider,
            model=model,
            generation=request.generation,
            legacy_max_tokens=request.max_tokens,
            input_text=generation_input_text,
            registry=generation_registry,
        )
        return constrain_budget_to_reservation(
            resolved,
            billing_reservation,
            provider=provider,
            model=model,
        )

    if execution_plan.strategy == "smart_orchestrator":
        kwargs["_smart_generation_resolver"] = resolve_smart_generation

    async def event_stream():
        response_generation_budget = generation_budget
        stream_started_at = time.monotonic()
        heartbeat_interval_s = _stream_heartbeat_interval_s()
        provider_task: asyncio.Task | None = None
        billing_finalization_attempted = False
        response: object | None = None
        meaningful_output_emitted = False
        stream_log = StreamLogContext(
            stream_name="chat",
            request_id=req_id,
            provider=target_provider,
            model=target_model,
            research_mode=research_mode,
        )
        stream_log.log("opened")
        start_line = _to_ndjson(
            {
                "type": "start",
                "mode": "chat",
                "provider": target_provider,
                "model": target_model,
                "session_id": requested_session_id,
                "research_mode": research_mode,
                "web_sources": 0,
                "web_source_items": [],
            }
        )
        try:
            yield stream_log.record_event(start_line)
        except BaseException:
            _release_chat_usage_safely(
                billing_reservation,
                reason="client_disconnected_before_provider_execution",
            )
            raise
        stream_log.log("start_event_sent")

        try:
            stream_log.log("provider_call_started")
            provider_task = asyncio.create_task(
                asyncio.to_thread(
                    orchestrator.ask,
                    prompt=effective_prompt,
                    model_type=execution_plan.model_type,
                    context=context,
                    model_name=execution_plan.model_name,
                    token_tracker=None,
                    research_mode=orchestrator_research_mode,
                    routing_mode=execution_plan.routing_mode,
                    routing_constraints=execution_plan.routing_constraints,
                    **kwargs,
                )
            )
            if heartbeat_interval_s > 0:
                while not provider_task.done():
                    done, _ = await asyncio.wait(
                        {provider_task},
                        timeout=heartbeat_interval_s,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if done:
                        break
                    elapsed_ms = int((time.monotonic() - stream_started_at) * 1000)
                    yield stream_log.record_event(
                        _to_ndjson({"type": "heartbeat", "elapsed_ms": elapsed_ms})
                    )
                    stream_log.log("heartbeat_sent", elapsed_ms=elapsed_ms)
            response = await provider_task
            response = sanitize_provider_error_response(normalize_empty_success_response(response))
            if execution_plan.strategy == "smart_orchestrator":
                response_model = str(response.requested_model or response.model or "").strip()
                if generation_registry.find_model(response.provider, response_model) is not None:
                    response_generation_budget = resolve_smart_generation(
                        response.provider,
                        response_model,
                    )
            response = annotate_response(response, response_generation_budget)
            stream_log.log(
                "provider_call_completed",
                response_provider=getattr(response, "provider", ""),
                response_model=getattr(response, "model", ""),
                response_is_error=bool(getattr(response, "is_error", False)),
            )
            stream_text = response.text or ""
            if not stream_text and response.error:
                stream_text = get_client_safe_error_display_text(response.error)

            if billing_reservation is not None and response.is_error:
                billing_finalization_attempted = True
                _finalize_chat_usage(
                    reservation=billing_reservation,
                    response=response,
                    orchestrator=orchestrator,
                    attachments_present=bool(resolved_attachments),
                )

            for line in _iter_stream_lines(stream_text):
                if not response.is_error and line:
                    meaningful_output_emitted = True
                yield stream_log.record_event(
                    _to_ndjson({"type": "line", "index": 0, "text": line})
                )
                if billing_reservation is not None and not billing_finalization_attempted:
                    billing_finalization_attempted = True
                    _finalize_chat_usage(
                        reservation=billing_reservation,
                        response=response,
                        orchestrator=orchestrator,
                        attachments_present=bool(resolved_attachments),
                    )
                await asyncio.sleep(STREAM_LINE_DELAY_S)

            if billing_reservation is not None and not billing_finalization_attempted:
                billing_finalization_attempted = True
                _finalize_chat_usage(
                    reservation=billing_reservation,
                    response=response,
                    orchestrator=orchestrator,
                    attachments_present=bool(resolved_attachments),
                )

            resolved_session_id = requested_session_id
            if API_DB_ENABLED and persistence_resolution is not None:
                try:
                    resolved_session_id = _persist_chat_interaction(
                        api_key=auth.api_key_or_none(),
                        resolution=persistence_resolution,
                        prompt=effective_prompt,
                        response=response,
                        requested_session_id=requested_session_id,
                        research_mode=research_mode,
                        force_new_session=force_new_session,
                        attachments=persistence_attachments,
                        regeneration=regeneration_context,
                    )
                except Exception:
                    logger.exception("Chat stream persistence failed in DB mode")

            dto = ChatResponseDTO.from_unified_response(
                response,
                session_id=resolved_session_id,
                response_version=(
                    regeneration_context.response_revision
                    if regeneration_context is not None
                    else 1
                ),
            )
            yield stream_log.record_event(
                _to_ndjson(
                    {
                        "type": "response_done",
                        "index": 0,
                        "response": jsonable_encoder(dto),
                    }
                )
            )
            stream_log.log("response_done_sent")
            yield stream_log.record_event(
                _to_ndjson({"type": "done", "mode": "chat", "session_id": resolved_session_id})
            )
            stream_log.log("done_sent", terminal_reason="done")

        except asyncio.CancelledError:
            if provider_task is not None and not provider_task.done():
                provider_task.cancel()
            if not billing_finalization_attempted:
                if (
                    billing_reservation is not None
                    and response is not None
                    and (meaningful_output_emitted or _research_was_performed(response))
                ):
                    try:
                        billing_finalization_attempted = True
                        _finalize_chat_usage(
                            reservation=billing_reservation,
                            response=response,
                            orchestrator=orchestrator,
                            settle_model_response=meaningful_output_emitted,
                            attachments_present=bool(resolved_attachments),
                        )
                    except Exception:
                        logger.exception("Chat stream partial subscription finalization failed")
                else:
                    _release_chat_usage_safely(
                        billing_reservation,
                        reason="client_disconnected_before_billable_output",
                    )
            stream_log.log(
                "client_disconnected",
                level="warning",
                terminal_reason="client_disconnected",
            )
            raise
        except Exception as exc:
            if not billing_finalization_attempted:
                if (
                    billing_reservation is not None
                    and response is not None
                    and (meaningful_output_emitted or _research_was_performed(response))
                ):
                    try:
                        billing_finalization_attempted = True
                        _finalize_chat_usage(
                            reservation=billing_reservation,
                            response=response,
                            orchestrator=orchestrator,
                            settle_model_response=meaningful_output_emitted,
                            attachments_present=bool(resolved_attachments),
                        )
                    except Exception:
                        logger.exception("Chat stream partial subscription finalization failed")
                else:
                    _release_chat_usage_safely(
                        billing_reservation,
                        reason="provider_execution_failed",
                    )
            stream_log.log(
                "exception",
                level="exception",
                terminal_reason="exception",
                error_type=type(exc).__name__,
            )
            if API_DB_ENABLED and persistence_resolution is not None:
                try:
                    error_response = persistence_service.build_error_response(
                        provider=target_provider,
                        model=target_model,
                        message=str(exc),
                        code="provider_error",
                        retryable=True,
                    )
                    _persist_chat_interaction(
                        api_key=auth.api_key_or_none(),
                        resolution=persistence_resolution,
                        prompt=effective_prompt,
                        response=error_response,
                        requested_session_id=requested_session_id,
                        research_mode=research_mode,
                        force_new_session=force_new_session,
                        attachments=persistence_attachments,
                        regeneration=regeneration_context,
                    )
                except Exception:
                    logger.exception("Chat stream error persistence failed in DB mode")
            yield stream_log.record_event(_to_ndjson({"type": "error", "message": str(exc)}))

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
