"""Compare endpoint for multi-model requests."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

from config.web_search import load_native_web_search_config
from models.unified_response import (
    MultiUnifiedResponse,
    NormalizedError,
    TokenUsage,
    UnifiedResponse,
)
from models.user_context import UserContext
from orchestrator.core import CortexOrchestrator
from orchestrator.generation_policy import GenerationBudgetResolution
from orchestrator.model_registry import ModelRegistry
from server import attachments as attachments_service
from server import persistence as persistence_service
from server.billing.enforcement_service import (
    BillableModelUsage,
    ReservedRequestUsage,
    resolve_model_target,
)
from server.billing.credit_calculator import (
    ResearchCreditUsage,
    research_credit_usage_from_metadata,
)
from server.billing.entitlement_service import ModelTargetIntent
from server.billing.errors import enforcement_http_exception
from server.billing.web_search_billing import (
    billable_web_search_usages,
    web_search_reservation_override,
)
from server.dependencies import AuthResult, get_auth, get_orchestrator
from server.generation_service import (
    annotate_response,
    constrain_budget_to_reservation,
    resolve_request_budget,
)
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.requests import CompareRequest
from server.schemas.responses import ChatResponseDTO, CompareResponseDTO
from server.stream_observability import StreamLogContext
from server.utils import (
    get_client_safe_error_display_text,
    normalize_empty_success_response,
    sanitize_provider_error_response,
    validate_and_trim_context,
)
from tools.web.native_policy import (
    WebSearchPolicy,
    orchestrator_research_mode,
    resolve_web_search_policy,
)
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["Compare"])

MAX_COMPARE_TARGETS = 3
STREAM_LINE_DELAY_S = 0.1
STREAM_HEARTBEAT_INTERVAL_S = 15.0
ATTACHMENTS_ONLY_FALLBACK_PROMPT = "Please analyze the attached file(s)."

API_DB_ENABLED = persistence_service.API_DB_ENABLED
ApiKeyPersistenceResolution = persistence_service.ApiKeyPersistenceResolution

logger = get_logger(__name__)
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="Compare",
    rejection_event="compare.route.rejected.auth_mode",
    logger=logger,
)

_resolve_and_enforce_caps = persistence_service.resolve_and_enforce_usage_caps
_persist_compare_interaction = persistence_service.persist_compare_interaction
_resolve_runtime_byok_provider_keys = persistence_service.resolve_runtime_byok_provider_keys
_reserve_subscription_usage = persistence_service.reserve_subscription_usage
_finalize_subscription_usage = persistence_service.finalize_subscription_usage
_release_subscription_usage = persistence_service.release_subscription_usage


def _resolve_compare_web_policy(
    request: CompareRequest, prompt: str
) -> tuple[WebSearchPolicy, bool]:
    """
    Compare mode always uses explicit targets; ignore smart_mode flags completely.
    """
    routing = request.routing
    if routing and bool(getattr(routing, "smart_mode", False)):
        logger.debug("Compare mode ignores routing.smart_mode=true and uses explicit targets only")
        try:
            routing.smart_mode = False
        except Exception:
            pass

    config = load_native_web_search_config()
    policy = resolve_web_search_policy(
        prompt,
        requested_mode=getattr(routing, "web_mode", None) if routing else None,
        legacy_research_mode=(getattr(routing, "research_mode", None) if routing else None),
        max_operations=config.max_operations,
    )
    return policy, bool(config.enabled and config.compare_enabled)


def _resolve_compare_generation_budgets(
    request: CompareRequest,
    *,
    input_text: str,
) -> list[GenerationBudgetResolution]:
    registry = ModelRegistry.from_yaml()
    return [
        resolve_request_budget(
            provider=target.provider,
            model=target.model or "",
            generation=target.generation or request.generation,
            legacy_max_tokens=request.max_tokens,
            input_text=input_text,
            registry=registry,
        )
        for target in request.targets
    ]


def _budget_for_response(
    response: UnifiedResponse,
    budgets: list[GenerationBudgetResolution],
    index: int,
) -> GenerationBudgetResolution:
    if index < len(budgets):
        expected = budgets[index]
        if expected.provider == response.provider and expected.model in {
            response.model,
            response.requested_model,
        }:
            return expected
    for budget in budgets:
        if budget.provider == response.provider and budget.model in {
            response.model,
            response.requested_model,
        }:
            return budget
    return budgets[min(index, len(budgets) - 1)]


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

    return UserContext(
        session_id=context_req.session_id,
        conversation_history=history,
    )


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


def _max_research_usage(*usages: ResearchCreditUsage) -> ResearchCreditUsage:
    return max(
        usages or (ResearchCreditUsage(0, 0, False),),
        key=lambda usage: (usage.provider_credits_used, not usage.estimated),
    )


def _research_credit_usage(responses: list[UnifiedResponse]) -> ResearchCreditUsage:
    return _max_research_usage(
        *(
            research_credit_usage_from_metadata(response.metadata)
            for response in responses
        )
    )


def _research_was_performed(responses: list[UnifiedResponse]) -> bool:
    return _research_credit_usage(responses).provider_credits_used > 0


def _prepared_research_credit_usage(
    prepared_turn: dict[str, Any] | None,
) -> ResearchCreditUsage:
    metadata = prepared_turn.get("research_metadata") if prepared_turn else None
    return research_credit_usage_from_metadata(metadata)


def _prepared_research_was_performed(prepared_turn: dict[str, Any] | None) -> bool:
    return _prepared_research_credit_usage(prepared_turn).provider_credits_used > 0


def _billable_stream_responses(
    ordered_responses: list[UnifiedResponse | None],
    billable_response_indices: set[int],
) -> list[UnifiedResponse]:
    """Return emitted model responses plus any completed billable search usage."""
    return [
        response
        for index, response in enumerate(ordered_responses)
        if response is not None
        and (
            index in billable_response_indices
            or bool(billable_web_search_usages((response,)))
        )
    ]


def _successful_billing_targets(
    responses: list[UnifiedResponse],
    *,
    orchestrator: CortexOrchestrator,
) -> tuple[ModelTargetIntent, ...]:
    return tuple(
        resolve_model_target(
            provider=response.provider,
            model=response.requested_model or response.model,
            orchestrator=orchestrator,
        )
        for response in responses
        if not response.is_error
    )


def _billing_input_text(request: CompareRequest) -> str:
    parts = [str(request.prompt or "")]
    if request.context and request.context.conversation_history:
        parts.extend(str(item.content or "") for item in request.context.conversation_history)
    return "\n".join(part for part in parts if part)


def _billable_model_usages(
    responses: list[UnifiedResponse],
) -> tuple[BillableModelUsage, ...]:
    return tuple(
        BillableModelUsage(
            provider=response.provider,
            model=response.requested_model or response.model,
            prompt_tokens=max(0, int(response.token_usage.prompt_tokens or 0)),
            cached_input_tokens=max(
                0, int(getattr(response.token_usage, "cached_input_tokens", 0) or 0)
            ),
            cache_write_tokens=max(
                0, int(getattr(response.token_usage, "cache_write_tokens", 0) or 0)
            ),
            output_tokens=max(0, int(response.token_usage.completion_tokens or 0)),
            reasoning_tokens=max(
                0, int(getattr(response.token_usage, "reasoning_tokens", 0) or 0)
            ),
            output_text=response.text,
            provider_cost_usd=max(0.0, float(response.estimated_cost or 0.0)),
            pricing_snapshot=dict(response.pricing_snapshot or {}),
            pricing_version=str(response.pricing_version or "") or None,
            provider_cost_owner=str(
                (response.metadata or {}).get("provider_cost_owner", "cortex")
            ),
        )
        for response in responses
        if not response.is_error
    )


def _reserve_compare_usage(
    *,
    resolution: persistence_service.ApiKeyPersistenceResolution,
    request_id: str,
    request: CompareRequest,
    research_enabled: bool,
    web_search_reservation_credits_value: int | None,
    orchestrator: CortexOrchestrator,
    resolved_attachments: list[attachments_service.ResolvedAttachment],
    initial_query: str,
    credit_activity_id: str | None,
    generation_budgets: list[GenerationBudgetResolution],
) -> ReservedRequestUsage:
    try:
        targets = tuple(
            resolve_model_target(
                provider=target.provider,
                model=target.model or "",
                orchestrator=orchestrator,
            )
            for target in request.targets
        )
        return _reserve_subscription_usage(
            user_id=resolution.user_id,
            request_id=request_id,
            operation_type="compare",
            model_targets=targets,
            research_enabled=research_enabled,
            web_search_reservation_credits=web_search_reservation_credits_value,
            smart_routing=False,
            attachment_count=len(resolved_attachments),
            total_attachment_bytes=sum(item.size_bytes for item in resolved_attachments),
            attachment_sizes=tuple(item.size_bytes for item in resolved_attachments),
            input_text=_billing_input_text(request),
            initial_query=initial_query,
            credit_activity_id=credit_activity_id,
            max_output_tokens_by_target={
                f"{item.provider}:{item.model}": item.effective_max_output_tokens
                for item in generation_budgets
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        http_error = enforcement_http_exception(exc)
        log = logger.exception if http_error.status_code >= 500 else logger.warning
        log(
            "Compare subscription preflight denied",
            extra={"extra_fields": {"request_id": request_id, "error_type": type(exc).__name__}},
        )
        raise http_error from exc


def _finalize_compare_usage(
    *,
    reservation: ReservedRequestUsage,
    responses: list[UnifiedResponse],
    orchestrator: CortexOrchestrator,
    research_usage: ResearchCreditUsage | None = None,
    attachments_present: bool = False,
) -> None:
    try:
        successful_targets = _successful_billing_targets(
            responses,
            orchestrator=orchestrator,
        )
        resolved_research_usage = research_usage or _research_credit_usage(responses)
        web_search_usages = billable_web_search_usages(responses)
        _finalize_subscription_usage(
            reservation=reservation,
            successful_targets=successful_targets,
            model_usages=_billable_model_usages(responses),
            research_provider_credits_used=resolved_research_usage.provider_credits_used,
            research_usage_estimated=resolved_research_usage.estimated,
            web_search_usages=web_search_usages,
            file_analysis_performed=attachments_present and bool(successful_targets),
        )
    except Exception as exc:
        logger.exception("Compare subscription usage finalization failed")
        _release_compare_usage_safely(
            reservation,
            reason="billing_finalization_failed",
        )
        raise enforcement_http_exception(exc) from exc


def _release_compare_usage_safely(
    reservation: ReservedRequestUsage | None,
    *,
    reason: str,
) -> None:
    if reservation is None:
        return
    try:
        _release_subscription_usage(reservation=reservation, reason=reason)
    except Exception:
        logger.exception("Compare subscription usage release failed")


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


def _make_error_response(
    provider: str,
    model: str,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict | None = None,
) -> UnifiedResponse:
    """Create a normalized error response without raising exceptions."""
    return UnifiedResponse(
        request_id=str(uuid.uuid4()),
        text="",
        provider=provider or "unknown",
        model=model or "unknown",
        latency_ms=0,
        token_usage=TokenUsage(0, 0, 0),
        estimated_cost=0.0,
        finish_reason="error",
        error=NormalizedError(
            code=code,
            message=message,
            provider=provider or "unknown",
            retryable=retryable,
            details=details or {},
        ),
    )


async def _run_compare_target(
    *,
    index: int,
    prompt: str,
    provider: str,
    model: str,
    context: UserContext | None,
    orchestrator: CortexOrchestrator,
    timeout_s: float | None,
    research_mode: str,
    request_id: str,
    kwargs: dict,
    prepared_turn: dict[str, Any] | None = None,
):
    """Run one compare target and capture timeout as UnifiedResponse."""
    call_kwargs = dict(kwargs or {})
    call_kwargs.pop("request_id", None)
    if prepared_turn:
        call_kwargs["_prepared_prompt"] = prepared_turn.get("prompt")
        call_kwargs["_prepared_messages"] = prepared_turn.get("messages")
        call_kwargs["_prepared_research_metadata"] = prepared_turn.get("research_metadata")
        call_kwargs["_prepared_opt_metadata"] = prepared_turn.get("optimization_metadata")
    ask_coro = asyncio.to_thread(
        orchestrator.ask,
        prompt=prompt,
        model_type=provider,
        context=context,
        model_name=model,
        token_tracker=None,
        research_mode=research_mode,
        request_id=f"{request_id}:target:{index}",
        **call_kwargs,
    )

    try:
        response = (
            await asyncio.wait_for(ask_coro, timeout=timeout_s) if timeout_s else await ask_coro
        )
        return index, response
    except asyncio.TimeoutError:
        timeout_msg = f"Request timed out after {timeout_s}s"
        return index, _make_error_response(
            provider=provider,
            model=model,
            code="timeout",
            message=timeout_msg,
            retryable=True,
            details={"timeout_seconds": timeout_s},
        )


@router.post("/compare", response_model=CompareResponseDTO)
async def compare(
    request: CompareRequest,
    http_request: Request,
    orchestrator: CortexOrchestrator = Depends(get_orchestrator),
    auth: AuthResult = Depends(get_auth),
):
    """Send a prompt to multiple AI models and compare responses."""
    if len(request.targets) > MAX_COMPARE_TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_COMPARE_TARGETS} targets allowed",
        )

    req_id = str(getattr(http_request.state, "request_id", "") or uuid.uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    request.context = validate_and_trim_context(request.context)
    context = _build_user_context(request.context)
    requested_session_id = request.context.session_id if request.context else None
    force_new_session = bool(request.context and request.context.new_session)
    web_policy, native_web_search = _resolve_compare_web_policy(request, request.prompt)
    research_mode = web_policy.enabled
    routing = request.routing
    orchestrator_mode = orchestrator_research_mode(
        web_policy,
        requested_mode=getattr(routing, "web_mode", None) if routing else None,
        legacy_research_mode=(getattr(routing, "research_mode", None) if routing else None),
    )

    persistence_resolution: persistence_service.ApiKeyPersistenceResolution | None = None
    provider_api_keys: dict[str, str] = {}
    if API_DB_ENABLED:
        persistence_resolution = _resolve_and_enforce_caps(auth=auth, request_id=req_id)
        persistence_service.persist_context_summary(
            context_request=request.context,
            user_id=persistence_resolution.user_id,
        )
        providers = [(target.provider or "").strip().lower() for target in request.targets]
        provider_api_keys = _resolve_runtime_byok_provider_keys(
            resolution=persistence_resolution,
            providers=providers,
        )

    resolved_attachments: list[attachments_service.ResolvedAttachment] = []
    inference_attachments: list[dict[str, Any]] = []
    persistence_attachments: list[dict[str, Any]] = []
    if request.attachments:
        if persistence_resolution is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "code": "attachments_require_db",
                    "message": "Attachments require DATABASE_URL (DB mode).",
                },
            )
        resolved_attachments = attachments_service.resolve_request_attachments(
            user_id=persistence_resolution.user_id,
            attachments=request.attachments,
        )
        for target in request.targets:
            provider = (target.provider or "").strip().lower()
            model = (target.model or "").strip()
            if not model:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "attachment_model_required",
                        "message": (
                            "Compare targets must specify model when attachments are present."
                        ),
                    },
                )
            attachments_service.enforce_model_attachment_compatibility(
                provider=provider,
                model=model,
                attachments=resolved_attachments,
                mode="compare",
            )
        inference_attachments = attachments_service.materialize_inference_attachments(
            resolved_attachments,
            query_text=request.prompt,
        )
        persistence_attachments = attachments_service.build_request_attachment_persistence_items(
            resolved_attachments=resolved_attachments,
            inference_attachments=inference_attachments,
        )
    effective_prompt = _resolve_effective_prompt(
        request.prompt,
        has_attachments=bool(resolved_attachments),
    )

    models_list = [{"provider": t.provider, "model": t.model or ""} for t in request.targets]

    generation_budgets = _resolve_compare_generation_budgets(
        request,
        input_text=_billing_input_text(request),
    )
    kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    kwargs["_per_client_generation"] = {
        f"{item.provider}:{item.model}": item.provider_kwargs() for item in generation_budgets
    }
    if provider_api_keys:
        kwargs["provider_api_keys"] = provider_api_keys
    if inference_attachments:
        kwargs["attachments"] = inference_attachments
    kwargs["request_id"] = req_id
    kwargs["web_search_policy"] = web_policy.to_provider_dict()
    kwargs["_native_web_search"] = native_web_search

    billing_reservation: ReservedRequestUsage | None = None
    if API_DB_ENABLED and persistence_resolution is not None:
        billing_reservation = _reserve_compare_usage(
            resolution=persistence_resolution,
            request_id=f"{req_id}:compare:{uuid.uuid4()}",
            request=request,
            research_enabled=research_mode,
            web_search_reservation_credits_value=web_search_reservation_override(
                (target.provider for target in request.targets),
                native_enabled=native_web_search,
                search_enabled=web_policy.enabled,
                per_target=True,
            ),
            orchestrator=orchestrator,
            resolved_attachments=resolved_attachments,
            initial_query=request.initial_query or effective_prompt,
            credit_activity_id=request.credit_activity_id,
            generation_budgets=generation_budgets,
        )
        generation_budgets = [
            constrain_budget_to_reservation(
                budget,
                billing_reservation,
                provider=target.provider,
                model=target.model or "",
            )
            for target, budget in zip(request.targets, generation_budgets, strict=False)
        ]
        kwargs["_per_client_generation"] = {
            f"{item.provider}:{item.model}": item.provider_kwargs()
            for item in generation_budgets
        }

    provider_completed = False
    try:
        response = await asyncio.to_thread(
            orchestrator.compare,
            prompt=effective_prompt,
            models_list=models_list,
            context=context,
            timeout_s=request.timeout_s,
            token_tracker=None,
            research_mode=orchestrator_mode,
            **kwargs,
        )
        provider_completed = True
        normalized_responses = [
            (
                annotate_response(
                    sanitize_provider_error_response(
                        normalize_empty_success_response(item)
                    ),
                    _budget_for_response(item, generation_budgets, index),
                )
                if item is not None
                else None
            )
            for index, item in enumerate(response.responses)
        ]
        response = MultiUnifiedResponse.from_responses(
            request_group_id=response.request_group_id,
            prompt=response.prompt,
            responses=normalized_responses,
        )
        if billing_reservation is not None:
            _finalize_compare_usage(
                reservation=billing_reservation,
                responses=[item for item in response.responses if item is not None],
                orchestrator=orchestrator,
                attachments_present=bool(resolved_attachments),
            )
    except BaseException:
        if not provider_completed:
            _release_compare_usage_safely(
                billing_reservation,
                reason="provider_execution_failed",
            )
        raise

    resolved_session_id = requested_session_id
    if API_DB_ENABLED and persistence_resolution is not None:
        try:
            persistable = [r for r in response.responses if r is not None]
            resolved_session_id = _persist_compare_interaction(
                api_key=auth.api_key_or_none(),
                resolution=persistence_resolution,
                prompt=effective_prompt,
                responses=persistable,
                request_group_id=response.request_group_id,
                requested_session_id=requested_session_id,
                research_mode=research_mode,
                force_new_session=force_new_session,
                attachments=persistence_attachments,
            )
        except Exception:
            logger.exception("Compare persistence failed in DB mode")

    dto = CompareResponseDTO.from_multi_unified_response(response, session_id=resolved_session_id)
    return dto


@router.post("/compare/stream")
async def compare_stream(
    request: CompareRequest,
    http_request: Request,
    orchestrator: CortexOrchestrator = Depends(get_orchestrator),
    auth: AuthResult = Depends(get_auth),
):
    """Stream compare responses as NDJSON events, then emit aggregate summary."""
    if len(request.targets) > MAX_COMPARE_TARGETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_COMPARE_TARGETS} targets allowed",
        )

    req_id = str(getattr(http_request.state, "request_id", "") or uuid.uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    request.context = validate_and_trim_context(request.context)
    context = _build_user_context(request.context)
    requested_session_id = request.context.session_id if request.context else None
    force_new_session = bool(request.context and request.context.new_session)
    web_policy, native_web_search = _resolve_compare_web_policy(request, request.prompt)
    research_mode = web_policy.enabled
    routing = request.routing
    orchestrator_mode = orchestrator_research_mode(
        web_policy,
        requested_mode=getattr(routing, "web_mode", None) if routing else None,
        legacy_research_mode=(getattr(routing, "research_mode", None) if routing else None),
    )

    persistence_resolution: persistence_service.ApiKeyPersistenceResolution | None = None
    provider_api_keys: dict[str, str] = {}
    if API_DB_ENABLED:
        persistence_resolution = _resolve_and_enforce_caps(auth=auth, request_id=req_id)
        persistence_service.persist_context_summary(
            context_request=request.context,
            user_id=persistence_resolution.user_id,
        )
        providers = [(target.provider or "").strip().lower() for target in request.targets]
        provider_api_keys = _resolve_runtime_byok_provider_keys(
            resolution=persistence_resolution,
            providers=providers,
        )

    resolved_attachments: list[attachments_service.ResolvedAttachment] = []
    inference_attachments: list[dict[str, Any]] = []
    persistence_attachments: list[dict[str, Any]] = []
    if request.attachments:
        if persistence_resolution is None:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail={
                    "code": "attachments_require_db",
                    "message": "Attachments require DATABASE_URL (DB mode).",
                },
            )
        resolved_attachments = attachments_service.resolve_request_attachments(
            user_id=persistence_resolution.user_id,
            attachments=request.attachments,
        )
        for target in request.targets:
            provider = (target.provider or "").strip().lower()
            model = (target.model or "").strip()
            if not model:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "attachment_model_required",
                        "message": (
                            "Compare targets must specify model when attachments are present."
                        ),
                    },
                )
            attachments_service.enforce_model_attachment_compatibility(
                provider=provider,
                model=model,
                attachments=resolved_attachments,
                mode="compare",
            )
        inference_attachments = attachments_service.materialize_inference_attachments(
            resolved_attachments,
            query_text=request.prompt,
        )
        persistence_attachments = attachments_service.build_request_attachment_persistence_items(
            resolved_attachments=resolved_attachments,
            inference_attachments=inference_attachments,
        )
    effective_prompt = _resolve_effective_prompt(
        request.prompt,
        has_attachments=bool(resolved_attachments),
    )

    generation_budgets = _resolve_compare_generation_budgets(
        request,
        input_text=_billing_input_text(request),
    )
    kwargs: dict[str, Any] = {}
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if provider_api_keys:
        kwargs["provider_api_keys"] = provider_api_keys
    if inference_attachments:
        kwargs["attachments"] = inference_attachments
    kwargs["request_id"] = req_id
    kwargs["web_search_policy"] = web_policy.to_provider_dict()
    kwargs["_native_web_search"] = native_web_search

    request_group_id = str(uuid.uuid4())
    billing_reservation: ReservedRequestUsage | None = None
    if API_DB_ENABLED and persistence_resolution is not None:
        billing_reservation = _reserve_compare_usage(
            resolution=persistence_resolution,
            request_id=f"{req_id}:compare-stream:{uuid.uuid4()}",
            request=request,
            research_enabled=research_mode,
            web_search_reservation_credits_value=web_search_reservation_override(
                (target.provider for target in request.targets),
                native_enabled=native_web_search,
                search_enabled=web_policy.enabled,
                per_target=True,
            ),
            orchestrator=orchestrator,
            resolved_attachments=resolved_attachments,
            initial_query=request.initial_query or effective_prompt,
            credit_activity_id=request.credit_activity_id,
            generation_budgets=generation_budgets,
        )
        generation_budgets = [
            constrain_budget_to_reservation(
                budget,
                billing_reservation,
                provider=target.provider,
                model=target.model or "",
            )
            for target, budget in zip(request.targets, generation_budgets, strict=False)
        ]

    async def event_stream():
        stream_started_at = time.monotonic()
        heartbeat_interval_s = _stream_heartbeat_interval_s()
        stream_log = StreamLogContext(
            stream_name="compare",
            request_id=req_id,
            research_mode=research_mode,
            target_count=len(request.targets),
            request_group_id=request_group_id,
        )
        stream_log.log("opened")
        start_line = _to_ndjson(
            {
                "type": "start",
                "mode": "compare",
                "session_id": requested_session_id,
                "target_count": len(request.targets),
                "research_mode": research_mode,
                "web_sources": 0,
                "web_source_items": [],
            }
        )
        try:
            yield stream_log.record_event(start_line)
        except BaseException:
            _release_compare_usage_safely(
                billing_reservation,
                reason="client_disconnected_before_provider_execution",
            )
            raise
        stream_log.log("start_event_sent")

        ordered_responses: list[UnifiedResponse | None] = [None] * len(request.targets)
        billable_response_indices: set[int] = set()
        tasks: list[asyncio.Task] = []
        prepared_turn: dict[str, Any] | None = None
        billing_finalization_attempted = False
        try:
            prepare_messages = getattr(orchestrator, "prepare_messages_for_turn", None)
            if callable(prepare_messages):
                try:
                    try:
                        prepared_turn = await asyncio.to_thread(
                            prepare_messages,
                            prompt=effective_prompt,
                            context=context,
                            research_mode=orchestrator_mode,
                            use_shared_research=not native_web_search,
                        )
                    except TypeError as exc:
                        if "use_shared_research" not in str(exc):
                            raise
                        prepared_turn = await asyncio.to_thread(
                            prepare_messages,
                            prompt=effective_prompt,
                            context=context,
                            research_mode=orchestrator_mode,
                        )
                except Exception:
                    logger.exception("Compare stream shared message preparation failed")

            for i, target in enumerate(request.targets):
                provider = (target.provider or "").strip().lower()
                model = (target.model or "").strip()

                if not provider or not model:
                    bad = sanitize_provider_error_response(
                        _make_error_response(
                            provider=provider or "unknown",
                            model=model or "unknown",
                            code="bad_request",
                            message=f"Invalid model config: provider='{provider}', model='{model}'",
                            retryable=False,
                        )
                    )
                    ordered_responses[i] = bad
                    bad_dto = ChatResponseDTO.from_unified_response(
                        bad,
                        session_id=requested_session_id,
                        include_research_charge=False,
                    )

                    yield stream_log.record_event(
                        _to_ndjson(
                            {
                                "type": "response_start",
                                "index": i,
                                "provider": bad_dto.provider,
                                "model": bad_dto.model,
                            }
                        )
                    )
                    stream_text = (
                        get_client_safe_error_display_text(bad_dto.error) if bad_dto.error else ""
                    )
                    for line in _iter_stream_lines(stream_text):
                        yield stream_log.record_event(
                            _to_ndjson({"type": "line", "index": i, "text": line})
                        )
                        await asyncio.sleep(STREAM_LINE_DELAY_S)
                    yield stream_log.record_event(
                        _to_ndjson(
                            {
                                "type": "response_done",
                                "index": i,
                                "response": jsonable_encoder(bad_dto),
                            }
                        )
                    )
                    stream_log.log(
                        "response_done_sent",
                        target_index=i,
                        provider=bad_dto.provider,
                        model=bad_dto.model,
                    )
                    continue

                stream_log.log(
                    "provider_call_started",
                    target_index=i,
                    provider=provider,
                    model=model,
                )
                tasks.append(
                    asyncio.create_task(
                        _run_compare_target(
                            index=i,
                            prompt=effective_prompt,
                            provider=provider,
                            model=model,
                            context=context,
                            orchestrator=orchestrator,
                            timeout_s=request.timeout_s,
                            research_mode=orchestrator_mode,
                            request_id=req_id,
                            kwargs={**kwargs, **generation_budgets[i].provider_kwargs()},
                            prepared_turn=prepared_turn,
                        )
                    )
                )

            pending_tasks = set(tasks)
            while pending_tasks:
                done, pending_tasks = await asyncio.wait(
                    pending_tasks,
                    timeout=heartbeat_interval_s if heartbeat_interval_s > 0 else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    elapsed_ms = int((time.monotonic() - stream_started_at) * 1000)
                    yield stream_log.record_event(
                        _to_ndjson({"type": "heartbeat", "elapsed_ms": elapsed_ms})
                    )
                    stream_log.log("heartbeat_sent", elapsed_ms=elapsed_ms)
                    continue

                for task in done:
                    idx, response = await task
                    response = sanitize_provider_error_response(
                        normalize_empty_success_response(response)
                    )
                    response = annotate_response(response, generation_budgets[idx])
                    ordered_responses[idx] = response
                    stream_log.log(
                        "provider_call_completed",
                        target_index=idx,
                        response_provider=getattr(response, "provider", ""),
                        response_model=getattr(response, "model", ""),
                        response_is_error=bool(getattr(response, "is_error", False)),
                    )
                    dto = ChatResponseDTO.from_unified_response(
                        response,
                        session_id=requested_session_id,
                        include_research_charge=False,
                    )

                    yield stream_log.record_event(
                        _to_ndjson(
                            {
                                "type": "response_start",
                                "index": idx,
                                "provider": dto.provider,
                                "model": dto.model,
                            }
                        )
                    )

                    stream_text = dto.text or ""
                    if not stream_text and dto.error:
                        stream_text = get_client_safe_error_display_text(dto.error)
                    for line in _iter_stream_lines(stream_text):
                        if not response.is_error and line:
                            billable_response_indices.add(idx)
                        yield stream_log.record_event(
                            _to_ndjson({"type": "line", "index": idx, "text": line})
                        )
                        await asyncio.sleep(STREAM_LINE_DELAY_S)

                    yield stream_log.record_event(
                        _to_ndjson(
                            {
                                "type": "response_done",
                                "index": idx,
                                "response": jsonable_encoder(dto),
                            }
                        )
                    )
                    stream_log.log(
                        "response_done_sent",
                        target_index=idx,
                        provider=dto.provider,
                        model=dto.model,
                    )

            raw_responses = [r for r in ordered_responses if r is not None]
            research_usage = _max_research_usage(
                _prepared_research_credit_usage(prepared_turn),
                _research_credit_usage(raw_responses),
            )
            if billing_reservation is not None:
                billing_finalization_attempted = True
                _finalize_compare_usage(
                    reservation=billing_reservation,
                    responses=raw_responses,
                    orchestrator=orchestrator,
                    research_usage=research_usage,
                    attachments_present=bool(resolved_attachments),
                )
            resolved_session_id = requested_session_id
            if API_DB_ENABLED and persistence_resolution is not None:
                try:
                    resolved_session_id = _persist_compare_interaction(
                        api_key=auth.api_key_or_none(),
                        resolution=persistence_resolution,
                        prompt=effective_prompt,
                        responses=raw_responses,
                        request_group_id=request_group_id,
                        requested_session_id=requested_session_id,
                        research_mode=research_mode,
                        force_new_session=force_new_session,
                        attachments=persistence_attachments,
                    )
                except Exception:
                    logger.exception("Compare stream persistence failed in DB mode")

            dtos = [
                ChatResponseDTO.from_unified_response(
                    r,
                    session_id=resolved_session_id,
                    include_research_charge=False,
                )
                for r in raw_responses
            ]
            compare_payload = {
                "request_group_id": request_group_id,
                "session_id": resolved_session_id,
                "responses": [jsonable_encoder(r) for r in dtos],
                "success_count": sum(1 for r in dtos if r.error is None),
                "error_count": sum(1 for r in dtos if r.error is not None),
                "total_tokens": sum(r.token_usage.total_tokens for r in dtos),
                "total_cost": sum(r.estimated_cost for r in dtos),
                "total_ai_credits": (
                    sum(r.ai_credits for r in dtos)
                    + research_usage.cortex_credits
                ),
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }

            yield stream_log.record_event(
                _to_ndjson(
                    {
                        "type": "done",
                        "mode": "compare",
                        "session_id": resolved_session_id,
                        "compare": compare_payload,
                    }
                )
            )
            stream_log.log("done_sent", terminal_reason="done")

        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if not billing_finalization_attempted:
                partial_responses = _billable_stream_responses(
                    ordered_responses,
                    billable_response_indices,
                )
                completed_responses = [
                    response for response in ordered_responses if response is not None
                ]
                research_usage = _max_research_usage(
                    _prepared_research_credit_usage(prepared_turn),
                    _research_credit_usage(completed_responses),
                )
                if partial_responses or research_usage.provider_credits_used:
                    try:
                        billing_finalization_attempted = True
                        if billing_reservation is not None:
                            _finalize_compare_usage(
                                reservation=billing_reservation,
                                responses=partial_responses,
                                orchestrator=orchestrator,
                                research_usage=research_usage,
                                attachments_present=bool(resolved_attachments),
                            )
                    except Exception:
                        logger.exception("Compare stream partial subscription finalization failed")
                else:
                    _release_compare_usage_safely(
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
            for task in tasks:
                if not task.done():
                    task.cancel()
            if not billing_finalization_attempted:
                partial_responses = _billable_stream_responses(
                    ordered_responses,
                    billable_response_indices,
                )
                completed_responses = [
                    response for response in ordered_responses if response is not None
                ]
                research_usage = _max_research_usage(
                    _prepared_research_credit_usage(prepared_turn),
                    _research_credit_usage(completed_responses),
                )
                if partial_responses or research_usage.provider_credits_used:
                    try:
                        billing_finalization_attempted = True
                        if billing_reservation is not None:
                            _finalize_compare_usage(
                                reservation=billing_reservation,
                                responses=partial_responses,
                                orchestrator=orchestrator,
                                research_usage=research_usage,
                                attachments_present=bool(resolved_attachments),
                            )
                    except Exception:
                        logger.exception("Compare stream partial subscription finalization failed")
                else:
                    _release_compare_usage_safely(
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
                    partial_responses = [r for r in ordered_responses if r is not None]
                    if not partial_responses:
                        fallback_error = persistence_service.build_error_response(
                            provider="unknown",
                            model="unknown",
                            message=str(exc),
                            code="provider_error",
                            retryable=True,
                        )
                        partial_responses = [fallback_error]
                    _persist_compare_interaction(
                        api_key=auth.api_key_or_none(),
                        resolution=persistence_resolution,
                        prompt=effective_prompt,
                        responses=partial_responses,
                        request_group_id=request_group_id,
                        requested_session_id=requested_session_id,
                        research_mode=research_mode,
                        force_new_session=force_new_session,
                        attachments=persistence_attachments,
                    )
                except Exception:
                    logger.exception("Compare stream error persistence failed in DB mode")
            yield stream_log.record_event(_to_ndjson({"type": "error", "message": str(exc)}))

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
