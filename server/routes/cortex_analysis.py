"""Authenticated Cortex Analysis endpoints for Compare runs."""

from __future__ import annotations

import asyncio
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from config.cache_optimization import (
    CORTEX_ANALYSIS_POLICY_VERSION,
    cortex_analysis_reuse_enabled,
)
from db import (
    CortexAnalysisSchemaUnavailable,
    create_cortex_analysis_run,
    get_compare_analysis_sources,
    list_cortex_analysis_runs,
    record_cache_reuse_event,
    require_cortex_analysis_schema,
)
from server import cortex_analysis as analysis_service
from server import persistence as persistence_service
from server.billing.enforcement_service import (
    BillableModelUsage,
    ReservedRequestUsage,
    resolve_model_target,
)
from server.billing.errors import enforcement_http_exception
from server.billing.response_credit_service import calculate_response_credit_usage
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.cortex_analysis import (
    CortexAnalysisConfidenceDTO,
    CortexAnalysisDisagreementDTO,
    CortexAnalysisRunDTO,
    CortexAnalysisSourceDTO,
    CortexAnalysisUniqueInsightDTO,
)
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["Cortex Analysis"])
logger = get_logger(__name__)
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="Cortex Analysis",
    rejection_event="cortex_analysis.route.rejected.auth_mode",
    logger=logger,
)

API_DB_ENABLED = persistence_service.API_DB_ENABLED
_db_uow = persistence_service.db_uow
_resolve_identity = persistence_service.resolve_identity


def _record_analysis_reuse_decision(
    *,
    user_id: UUID,
    request_id: str,
    reused: bool,
) -> None:
    """Best-effort audit recording must never block Cortex Analysis."""

    try:
        with _db_uow() as db_session:
            record_cache_reuse_event(
                db_session,
                user_id=user_id,
                request_id=request_id,
                operation_type="cortex_analysis",
                reused=reused,
            )
    except Exception:
        logger.warning(
            "Cortex Analysis reuse telemetry unavailable",
            exc_info=True,
        )


def _confidence_level(value: object) -> Literal["limited", "moderate", "high"]:
    normalized = str(value or "").strip().lower()
    if normalized == "moderate":
        return "moderate"
    if normalized == "high":
        return "high"
    return "limited"


def _require_db_mode() -> None:
    if API_DB_ENABLED:
        return
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "cortex_analysis_requires_db",
            "message": "Cortex Analysis requires database persistence.",
        },
    )


def _require_analysis_schema(*, request_id: str) -> None:
    try:
        require_cortex_analysis_schema()
    except CortexAnalysisSchemaUnavailable as exc:
        logger.warning(
            "Cortex Analysis schema is unavailable",
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "migrations": [
                        "20260727_add_cortex_analysis_runs.sql",
                        "20260802_add_cortex_analysis_attribution.sql",
                    ],
                }
            },
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "cortex_analysis_schema_unavailable",
                "message": (
                    "Cortex Analysis is temporarily unavailable because its "
                    "database setup is incomplete."
                ),
            },
        ) from exc


def _load_sources(
    *,
    user_id: UUID,
    request_group_id: UUID,
) -> dict | None:
    with _db_uow(commit_on_success=False) as db_session:
        return get_compare_analysis_sources(
            db_session,
            user_id,
            request_group_id,
        )


@router.get(
    "/compare/analysis-runs",
    response_model=list[CortexAnalysisRunDTO],
)
async def list_analysis_runs(
    request: Request,
    session_id: UUID | None = Query(default=None),
    request_group_id: UUID | None = Query(default=None),
    auth: AuthResult = Depends(get_auth),
):
    """List saved runs for a Compare group or all Compare turns in one session."""
    _require_db_mode()
    if session_id is None and request_group_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide session_id or request_group_id.",
        )

    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    _require_analysis_schema(request_id=req_id)
    with _db_uow(commit_on_success=False) as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        runs = list_cortex_analysis_runs(
            db_session,
            resolution.user_id,
            session_id=session_id,
            request_group_id=request_group_id,
        )

    fingerprints = _current_fingerprints(
        resolution.user_id,
        runs,
    )
    return [
        _run_to_dto(
            run,
            current_fingerprint=fingerprints.get(run["request_group_id"]),
        )
        for run in runs
    ]


@router.post(
    "/compare/{request_group_id}/analysis",
    response_model=CortexAnalysisRunDTO,
    status_code=status.HTTP_201_CREATED,
)
async def create_analysis_run(
    request_group_id: UUID,
    request: Request,
    regenerate: bool = Query(default=False),
    auth: AuthResult = Depends(get_auth),
):
    """Analyze the latest successful revisions in one owned Compare run."""
    _require_db_mode()
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    _require_analysis_schema(request_id=req_id)

    with _db_uow(commit_on_success=False) as db_session:
        resolution = _resolve_identity(
            auth=auth,
            request_id=req_id,
            db_session=db_session,
        )
        source_payload = get_compare_analysis_sources(
            db_session,
            resolution.user_id,
            request_group_id,
        )

    if source_payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "compare_run_not_found",
                "message": "The Compare run was not found.",
            },
        )

    sources, failed_count = analysis_service.normalize_analysis_sources(source_payload["responses"])
    if len(sources) < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "insufficient_compare_responses",
                "message": "At least two successful responses are required.",
            },
        )
    if len(sources) > 3:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "too_many_compare_responses",
                "message": "Cortex Analysis supports two or three responses.",
            },
        )

    fingerprint = analysis_service.source_fingerprint(sources)
    reusable = None
    if cortex_analysis_reuse_enabled() and not regenerate:
        with _db_uow(commit_on_success=False) as db_session:
            prior_runs = list_cortex_analysis_runs(
                db_session,
                resolution.user_id,
                request_group_id=request_group_id,
            )
        reusable = next(
            (
                run
                for run in prior_runs
                if run.get("source_fingerprint") == fingerprint
                and run.get("model") == analysis_service.configured_analysis_model()
                and run.get("analysis_policy_version")
                == CORTEX_ANALYSIS_POLICY_VERSION
            ),
            None,
        )
    _record_analysis_reuse_decision(
        user_id=resolution.user_id,
        request_id=req_id,
        reused=reusable is not None,
    )
    if reusable is not None:
        logger.info(
            "Reused an existing Cortex Analysis run",
            extra={
                "extra_fields": {
                    "event": "cortex_analysis.reused",
                    "request_id": req_id,
                    "operation_type": "cortex_analysis",
                    "provider": "openai",
                    "model": analysis_service.configured_analysis_model(),
                    "credits": 0,
                    "cache_policy_version": CORTEX_ANALYSIS_POLICY_VERSION,
                }
            },
        )
        return _run_to_dto(reusable, current_fingerprint=fingerprint)

    billing_reservation: ReservedRequestUsage | None = None
    try:
        billing_reservation = persistence_service.reserve_subscription_usage(
            user_id=resolution.user_id,
            request_id=f"{req_id}:cortex-analysis:{uuid4()}",
            operation_type="cortex_analysis",
            model_targets=(
                resolve_model_target(
                    provider="openai",
                    model=analysis_service.configured_analysis_model(),
                ),
            ),
            research_enabled=False,
            input_text="\n".join(
                [
                    str(source_payload.get("question") or ""),
                    *(source.content for source in sources),
                ]
            ),
            initial_query=str(source_payload.get("question") or ""),
            max_output_tokens=analysis_service.CORTEX_ANALYSIS_MAX_OUTPUT_TOKENS,
        )
    except Exception as exc:
        raise enforcement_http_exception(exc) from exc

    try:
        generated = await asyncio.to_thread(
            analysis_service.analyze_responses,
            question=str(source_payload.get("question") or ""),
            sources=sources,
        )
    except analysis_service.CortexAnalysisGenerationError as exc:
        if billing_reservation is not None:
            try:
                persistence_service.release_subscription_usage(
                    reservation=billing_reservation,
                    reason="cortex_analysis_provider_failed",
                )
            except Exception:
                logger.exception("Cortex Analysis credit reservation release failed")
        logger.warning(
            "Cortex Analysis generation failed",
            extra={
                "extra_fields": {
                    "request_id": req_id,
                    "request_group_id": str(request_group_id),
                    "error_type": type(exc).__name__,
                }
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "cortex_analysis_failed",
                "message": "Cortex couldn't combine these answers. Try again.",
            },
        ) from exc

    if billing_reservation is not None:
        try:
            persistence_service.finalize_subscription_usage(
                reservation=billing_reservation,
                successful_targets=(),
                model_usages=(
                    BillableModelUsage(
                        provider="openai",
                        model=analysis_service.configured_analysis_model(),
                        prompt_tokens=generated.prompt_tokens,
                        cached_input_tokens=generated.cached_input_tokens,
                        cache_write_tokens=generated.cache_write_tokens,
                        output_tokens=generated.completion_tokens,
                        reasoning_tokens=generated.reasoning_tokens,
                        output_text=generated.recommended_answer,
                        provider_cost_usd=generated.estimated_cost,
                        pricing_snapshot=generated.pricing_snapshot,
                        pricing_version=generated.pricing_version,
                    ),
                ),
                research_provider_credits_used=0,
            )
        except Exception as exc:
            raise enforcement_http_exception(exc) from exc

    session_id = persistence_service.coerce_uuid(str(source_payload.get("session_id") or ""))
    if session_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "compare_session_missing",
                "message": "This Compare run is not attached to a saved session.",
            },
        )

    snapshot = analysis_service.source_snapshot(sources)
    with _db_uow() as db_session:
        analysis_id = create_cortex_analysis_run(
            db_session,
            user_id=resolution.user_id,
            session_id=session_id,
            request_group_id=request_group_id,
            model=analysis_service.configured_analysis_model(),
            source_fingerprint=fingerprint,
            source_snapshot=snapshot,
            recommended_answer=generated.recommended_answer,
            agreements=generated.agreements,
            disagreements=generated.disagreements,
            disagreement_note=generated.disagreement_note,
            unique_insights=generated.unique_insights,
            confidence_level=generated.confidence_level,
            confidence_reason=generated.confidence_reason,
            verify_items=generated.verify_items,
            high_stakes_domain=generated.high_stakes_domain,
            combined_response_count=len(sources),
            failed_response_count=failed_count,
            prompt_tokens=generated.prompt_tokens,
            completion_tokens=generated.completion_tokens,
            total_tokens=generated.total_tokens,
            estimated_cost=generated.estimated_cost,
            analysis_policy_version=CORTEX_ANALYSIS_POLICY_VERSION,
            cached_input_tokens=generated.cached_input_tokens,
            cache_write_tokens=generated.cache_write_tokens,
            reasoning_tokens=generated.reasoning_tokens,
            pricing_snapshot=generated.pricing_snapshot,
            pricing_version=generated.pricing_version,
        )

    with _db_uow(commit_on_success=False) as db_session:
        saved_runs = list_cortex_analysis_runs(
            db_session,
            resolution.user_id,
            request_group_id=request_group_id,
        )
    saved = next(
        (run for run in saved_runs if run["analysis_id"] == str(analysis_id)),
        None,
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "cortex_analysis_persistence_failed",
                "message": "The analysis could not be saved.",
            },
        )
    return _run_to_dto(saved, current_fingerprint=fingerprint)


def _current_fingerprints(
    user_id: UUID,
    runs: list[dict],
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    group_ids = {
        persistence_service.coerce_uuid(str(run.get("request_group_id") or "")) for run in runs
    }
    for group_id in group_ids:
        if group_id is None:
            continue
        current = _load_sources(user_id=user_id, request_group_id=group_id)
        if current is None:
            continue
        sources, _ = analysis_service.normalize_analysis_sources(current["responses"])
        fingerprints[str(group_id)] = analysis_service.source_fingerprint(sources)
    return fingerprints


def _run_to_dto(
    run: dict,
    *,
    current_fingerprint: str | None,
) -> CortexAnalysisRunDTO:
    snapshot = run.get("source_snapshot")
    if not isinstance(snapshot, list):
        snapshot = []
    insights = run.get("unique_insights")
    if not isinstance(insights, list):
        insights = []
    disagreements = run.get("disagreements")
    if not isinstance(disagreements, list):
        disagreements = []
    source_fingerprint = str(run.get("source_fingerprint") or "")
    analysis_model = str(
        run.get("model") or analysis_service.configured_analysis_model()
    )
    pricing_snapshot = run.get("pricing_snapshot")
    credit_usage = calculate_response_credit_usage(
        provider="openai",
        model=analysis_model,
        input_tokens=max(0, int(run.get("prompt_tokens") or 0)),
        cached_input_tokens=max(0, int(run.get("cached_input_tokens") or 0)),
        cache_write_tokens=max(0, int(run.get("cache_write_tokens") or 0)),
        reasoning_tokens=max(0, int(run.get("reasoning_tokens") or 0)),
        output_tokens=max(0, int(run.get("completion_tokens") or 0)),
        output_text=str(run.get("recommended_answer") or ""),
        pricing_snapshot=(
            pricing_snapshot if isinstance(pricing_snapshot, dict) else None
        ),
        include_research_charge=False,
    )
    return CortexAnalysisRunDTO(
        analysisId=str(run.get("analysis_id") or ""),
        requestGroupId=str(run.get("request_group_id") or ""),
        sessionId=str(run.get("session_id") or ""),
        model=analysis_model,
        recommendedAnswer=str(run.get("recommended_answer") or ""),
        agreements=[str(item) for item in run.get("agreements") or [] if str(item)],
        disagreements=[
            CortexAnalysisDisagreementDTO(
                who=str(item.get("who") or "One response"),
                text=str(item.get("text") or ""),
            )
            if isinstance(item, dict)
            else CortexAnalysisDisagreementDTO(who="One response", text=str(item))
            for item in disagreements
            if (
                isinstance(item, dict)
                and str(item.get("text") or "").strip()
            )
            or (not isinstance(item, dict) and str(item).strip())
        ],
        disagreementNote=(str(run.get("disagreement_note") or "").strip() or None),
        uniqueInsights=[
            CortexAnalysisUniqueInsightDTO(
                responseName=str(item.get("responseName") or "One response"),
                text=str(item.get("text") or ""),
            )
            for item in insights
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ],
        confidence=CortexAnalysisConfidenceDTO(
            level=_confidence_level(run.get("confidence_level")),
            reason=str(run.get("confidence_reason") or ""),
        ),
        verify=[str(item) for item in run.get("verify_items") or [] if str(item)],
        highStakesDomain=run.get("high_stakes_domain"),
        sourceFingerprint=source_fingerprint,
        sourceResponses=[
            CortexAnalysisSourceDTO(
                requestId=str(item.get("requestId") or ""),
                responseVersion=max(1, int(item.get("responseVersion") or 1)),
                responseName=str(item.get("responseName") or "One response"),
            )
            for item in snapshot
            if isinstance(item, dict)
        ],
        combinedResponseCount=max(
            2,
            min(3, int(run.get("combined_response_count") or 2)),
        ),
        failedResponseCount=max(0, int(run.get("failed_response_count") or 0)),
        aiCredits=credit_usage.ai_credits,
        creditUsageEstimated=credit_usage.credit_usage_estimated,
        createdAt=str(run.get("created_at") or ""),
        isStale=(current_fingerprint is not None and source_fingerprint != current_fingerprint),
    )
