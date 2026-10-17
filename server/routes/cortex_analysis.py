"""Authenticated Cortex Analysis endpoints for Compare runs."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from db import (
    CortexAnalysisSchemaUnavailable,
    get_compare_analysis_sources,
    list_cortex_analysis_runs,
    require_cortex_analysis_schema,
)
from server import cortex_analysis as analysis_service
from server import persistence as persistence_service
from server.billing.response_credit_service import calculate_response_credit_usage
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.cortex_analysis import (
    CortexAnalysisConfidenceDTO,
    CortexAnalysisDisagreementDTO,
    CortexAnalysisRunDTO,
    CortexAnalysisSourceDTO,
    CortexAnalysisStateDTO,
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
            current_fingerprint=fingerprints.get(run["analysis_id"]),
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

    from server import analysis_jobs
    saved = await analysis_jobs.create_manual(
        resolution.user_id, request_group_id, regenerate=regenerate, request_id=req_id,
    )
    sources, _ = analysis_service.normalize_analysis_sources(source_payload["responses"])
    return _run_to_dto(saved, current_fingerprint=analysis_service.source_fingerprint(
        sources, saved.get("context_snapshot") or [],
    ))


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
        for run in runs:
            if str(run.get("request_group_id")) != str(group_id):
                continue
            fingerprints[str(run["analysis_id"])] = analysis_service.source_fingerprint(
                sources, run.get("context_snapshot") or []
            ) if run.get("analysis_policy_version") != "cortex-analysis-v1" else analysis_service.source_fingerprint(sources)
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
        structuredResult=run.get("structured_result") or None,
        contextSnapshot=run.get("context_snapshot") or [],
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
                provider=str(item.get("provider") or ""), model=str(item.get("model") or ""),
                content=item.get("content"), webSources=item.get("webSources") or [],
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
        creditUsageEstimated=(credit_usage.credit_usage_estimated or
            bool((run.get("structured_result") or {}).get("usageEstimated"))),
        createdAt=str(run.get("created_at") or ""),
        isStale=(current_fingerprint is not None and source_fingerprint != current_fingerprint),
    )


@router.get("/compare/{request_group_id}/analysis-state", response_model=CortexAnalysisStateDTO)
async def analysis_state(request_group_id: UUID, request: Request, auth: AuthResult = Depends(get_auth)):
    """Read authorization/status and an estimate; this endpoint never starts paid work."""
    from db import analysis_repository
    from server.billing.credit_estimator import estimate_model_credits
    _require_db_mode()
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    with _db_uow(commit_on_success=False) as db:
        identity = _resolve_identity(auth=auth, request_id=req_id, db_session=db)
        payload = get_compare_analysis_sources(db, identity.user_id, request_group_id)
        if payload is None:
            raise HTTPException(404, detail="Analyze run was not found.")
        intent = analysis_repository.get_intent(db, identity.user_id, request_group_id)
    sources, _ = analysis_service.normalize_analysis_sources(payload["responses"])
    context = intent.get("context_snapshot") if intent else payload.get("prior_context", [])
    estimated = None
    try:
        target = analysis_service._model_registry().find_model("openai", analysis_service.configured_analysis_model())
        estimated = estimate_model_credits(target,
            input_text=analysis_service.reservation_input(payload["question"], sources, context),
            max_output_tokens=analysis_service.analysis_output_tokens()).charge.total_credits
    except Exception:
        logger.warning("Analysis estimate unavailable", exc_info=True)
    return {"state": intent["state"] if intent else "idle",
            "progressStage": intent.get("progress_stage") if intent and intent["state"] == "processing" else None,
            "automatic": bool(intent and intent["automatic"]),
            "analysisId": str(intent.get("analysis_id") or "") if intent else None,
            "errorCode": intent.get("error_code") if intent else None,
            "estimatedAiCredits": estimated}
