"""Shared FastAPI persistence service.

Route modules call these helpers when DATABASE_URL is configured.
This keeps DB ownership, cap enforcement, and audit writes in one place.
"""

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Generator, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import and_, func, literal, select
from sqlalchemy.orm import Session

from db import (
    check_usage_limit,
    create_api_key,
    create_context_snapshot,
    create_llm_request,
    create_llm_response,
    create_request_attachment,
    create_routing_attempts,
    create_routing_decision,
    create_session,
    get_active_session,
    get_api_key_settings,
    get_compare_response_regeneration_source,
    get_failed_routing_attempts_by_request_group,
    get_session_by_id,
    get_or_create_service_user,
    get_or_create_user_by_cognito,
    get_user_by_api_key,
    record_cache_reuse_event,
    save_compare_summary,
    save_message,
    update_api_key_last_used,
    update_session_timestamp,
    upsert_usage_daily,
    verify_session_belongs_to_user,
)
from db.session import SessionLocal
from models.unified_response import NormalizedError, TokenUsage, UnifiedResponse
from server import byok_service
from server.dependencies import AuthResult
from server.utils import context_summary_payload
from server import privacy as privacy_service
from server import rate_limit as rate_limit_service
from server import savings as savings_service
from server.billing.enforcement_service import (
    BillableModelUsage,
    BillableWebSearchUsage,
    ReservedRequestUsage,
    authorize_and_reserve_usage,
    finalize_reserved_usage,
    release_reserved_usage,
)
from server.billing.metering_service import supplement_usage_reservation
from server.billing.plan_catalog import get_plan_catalog
from server.billing.response_credit_service import resolve_response_credit_usage
from server.billing.entitlement_service import ModelTargetIntent
from server.billing.subscription_service import resolve_effective_subscription
from utils.logger import get_logger

API_DB_ENABLED = bool(os.getenv("DATABASE_URL"))

logger = get_logger(__name__)


@dataclass(frozen=True)
class ApiKeyPersistenceResolution:
    user_id: UUID
    api_key_id: UUID | None
    decision_path: str


@dataclass(frozen=True)
class RequestAttachmentPersistenceItem:
    file_id: UUID
    usage_role: str = "primary"
    transform_mode: str = "auto"
    order_index: int = 0
    resolved_artifact_meta: dict | None = None


@dataclass(frozen=True)
class CompareRegenerationContext:
    response_revision_root_id: UUID
    response_revision: int
    session_id: UUID
    request_group_id: UUID
    provider: str
    model: str


@contextmanager
def db_uow(*, commit_on_success: bool = True) -> Generator[Session, None, None]:
    """
    DB unit-of-work with explicit transaction semantics.

    - commit on success (default)
    - rollback on exception
    - close always
    """
    db_session = SessionLocal()
    try:
        yield db_session
        if commit_on_success:
            db_session.commit()
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.close()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def persist_context_summary(
    *,
    context_request: object | None,
    user_id: UUID,
) -> None:
    """Best-effort persistence for deterministic reusable context summaries."""

    payload = context_summary_payload(context_request)
    session_id = coerce_uuid(str(getattr(context_request, "session_id", "") or ""))
    if payload is None or session_id is None:
        return
    try:
        with db_uow() as db_session:
            create_context_snapshot(
                db_session,
                user_id=user_id,
                session_id=session_id,
                context_text=payload["context_text"],
                source_message_range=payload["source_message_range"],
                source_hash=payload["source_hash"],
                summary_policy_version=payload["summary_policy_version"],
            )
    except Exception:
        # Context compaction is an optimization and must never block inference.
        logger.warning(
            "Context summary persistence unavailable; continuing with compacted context",
            exc_info=True,
            extra={
                "extra_fields": {
                    "event": "context.summary.persistence_unavailable",
                    "session_id": str(session_id),
                    "source_hash_prefix": payload["source_hash"][:12],
                }
            },
        )


def coerce_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except Exception:
        return None


def _resolve_api_key_for_request_in_session(
    db_session: Session,
    *,
    api_key: str,
    request_id: str,
) -> ApiKeyPersistenceResolution:
    """
    Resolve persistence owner for API key.

    Behavior for unmapped keys:
    - AUTO_REGISTER_UNMAPPED_API_KEYS=true: map key to service user and persist api_key_id
    - ALLOW_UNMAPPED_API_KEY_PERSIST=true: persist under service user with api_key_id=None
    - default (both false): reject with 403
    """
    mapped = get_user_by_api_key(db_session, api_key)
    if mapped:
        user_id, api_key_id = mapped
        return ApiKeyPersistenceResolution(
            user_id=user_id,
            api_key_id=api_key_id,
            decision_path="mapped",
        )

    auto_register = env_bool("AUTO_REGISTER_UNMAPPED_API_KEYS", default=False)
    allow_unmapped = env_bool("ALLOW_UNMAPPED_API_KEY_PERSIST", default=False)

    if not auto_register and not allow_unmapped:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is not mapped for persistence",
        )

    fallback_email = os.getenv("API_KEY_FALLBACK_USER_EMAIL", "api@cortexai.local")
    fallback_name = os.getenv("API_KEY_FALLBACK_USER_NAME", "API Service User")
    service_user_id = get_or_create_service_user(
        db_session,
        email=fallback_email,
        display_name=fallback_name,
    )

    if auto_register:
        api_key_id, owner_user_id = create_api_key(
            db_session,
            user_id=service_user_id,
            raw_api_key=api_key,
            label=f"api-autoreg-{request_id[:8]}",
        )
        return ApiKeyPersistenceResolution(
            user_id=owner_user_id,
            api_key_id=api_key_id,
            decision_path="auto_registered",
        )

    return ApiKeyPersistenceResolution(
        user_id=service_user_id,
        api_key_id=None,
        decision_path="service_fallback",
    )


def resolve_api_key_for_request(
    *,
    api_key: str,
    request_id: str,
    db_session: Session | None = None,
) -> ApiKeyPersistenceResolution:
    """
    Resolve API key ownership for persistence.

    When db_session is provided, caller owns transaction boundaries.
    """
    normalized_api_key = str(api_key or "").strip()
    if not normalized_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid or missing credentials "
                "(send cortex_session cookie, X-API-Key, or Authorization: Bearer)"
            ),
        )

    if db_session is not None:
        return _resolve_api_key_for_request_in_session(
            db_session,
            api_key=normalized_api_key,
            request_id=request_id,
        )

    try:
        with db_uow() as owned_session:
            return _resolve_api_key_for_request_in_session(
                owned_session,
                api_key=normalized_api_key,
                request_id=request_id,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("API key persistence resolution failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Persistence initialization failed: {exc}",
        ) from exc


def _resolve_cognito_for_request_in_session(
    db_session: Session,
    *,
    sub: str,
    email: str | None,
    issuer: str,
) -> ApiKeyPersistenceResolution:
    """Resolve Cognito identity to user_id for persistence (api_key_id is None)."""
    user_id = get_or_create_user_by_cognito(
        db_session,
        sub=sub,
        email=email,
        issuer=issuer,
    )
    return ApiKeyPersistenceResolution(
        user_id=user_id,
        api_key_id=None,
        decision_path="cognito",
    )


def resolve_identity(
    *,
    auth: AuthResult,
    request_id: str,
    db_session: Session | None = None,
) -> ApiKeyPersistenceResolution:
    """
    Resolve auth (session cookie, API key, or Cognito) to persistence identity.

    When db_session is provided, caller owns transaction boundaries.
    """
    if auth.user_id is not None:
        return ApiKeyPersistenceResolution(
            user_id=auth.user_id,
            api_key_id=None,
            decision_path="session_cookie",
        )
    if auth.api_key is not None:
        return resolve_api_key_for_request(
            api_key=auth.api_key,
            request_id=request_id,
            db_session=db_session,
        )
    if auth.cognito_claims is not None:
        claims = auth.cognito_claims
        if db_session is not None:
            return _resolve_cognito_for_request_in_session(
                db_session,
                sub=claims.sub,
                email=claims.email,
                issuer=claims.iss,
            )
        try:
            with db_uow() as owned_session:
                return _resolve_cognito_for_request_in_session(
                    owned_session,
                    sub=claims.sub,
                    email=claims.email,
                    issuer=claims.iss,
                )
        except Exception as exc:
            logger.exception("Cognito persistence resolution failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Persistence initialization failed: {exc}",
            ) from exc
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


def resolve_compare_regeneration_context(
    *,
    user_id: UUID,
    source_request_id: str,
) -> CompareRegenerationContext:
    """Validate and resolve an owned Compare response before provider work starts."""
    with db_uow(commit_on_success=False) as db_session:
        source = get_compare_response_regeneration_source(
            db_session,
            user_id,
            source_request_id,
        )
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "compare_response_not_found",
                "message": "The Compare response to regenerate was not found.",
            },
        )

    session_id = coerce_uuid(str(source.get("session_id") or ""))
    request_group_id = coerce_uuid(str(source.get("request_group_id") or ""))
    revision_root_id = coerce_uuid(str(source.get("response_revision_root_id") or ""))
    if session_id is None or request_group_id is None or revision_root_id is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "compare_response_revision_unavailable",
                "message": "This Compare response cannot be regenerated in place.",
            },
        )

    return CompareRegenerationContext(
        response_revision_root_id=revision_root_id,
        response_revision=max(2, int(source.get("response_revision") or 2)),
        session_id=session_id,
        request_group_id=request_group_id,
        provider=str(source.get("provider") or ""),
        model=str(source.get("model") or ""),
    )


def _extract_cap_value(raw: str | None, *, kind: str) -> int | float | None:
    if not raw:
        return None
    try:
        if kind == "token":
            return int(raw)
        return float(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid {kind} cap configuration: {raw}",
        ) from exc


def _read_api_key_usage_today(db_session: Session, api_key_id: UUID) -> tuple[int, float] | None:
    """
    Read today's usage for one API key by aggregating llm_requests/llm_responses.

    Returns None when schema lacks required columns and caller should fallback to user scope.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")

    req_cols = {col.name for col in llm_requests.columns}
    resp_cols = {col.name for col in llm_responses.columns}

    if "api_key_id" not in req_cols:
        return None

    created_col = llm_requests.c.created_at if "created_at" in req_cols else None
    if created_col is None:
        return None

    tokens_col = llm_responses.c.total_tokens if "total_tokens" in resp_cols else None
    cost_col = llm_responses.c.estimated_cost if "estimated_cost" in resp_cols else None

    token_expr = (
        func.coalesce(func.sum(tokens_col), 0).label("total_tokens")
        if tokens_col is not None
        else literal(0).label("total_tokens")
    )
    cost_expr = (
        func.coalesce(func.sum(cost_col), 0.0).label("total_cost")
        if cost_col is not None
        else literal(0.0).label("total_cost")
    )

    today = date.today()
    tomorrow = today + timedelta(days=1)
    stmt = (
        select(token_expr, cost_expr)
        .select_from(
            llm_requests.outerjoin(
                llm_responses,
                llm_responses.c.llm_request_id == llm_requests.c.id,
            )
        )
        .where(
            and_(
                llm_requests.c.api_key_id == api_key_id,
                created_col >= today,
                created_col < tomorrow,
            )
        )
    )

    row = db_session.execute(stmt).first()
    if not row:
        return (0, 0.0)

    payload = row._mapping
    current_tokens = int(payload.get("total_tokens") or 0)
    current_cost = float(payload.get("total_cost") or 0.0)
    return (current_tokens, current_cost)


def _deny_usage_limit(*, reason: str, current_tokens: int, current_cost: float, scope: str) -> None:
    logger.warning(
        "Usage limit exceeded",
        extra={
            "extra_fields": {
                "event": "usage.cap.exceeded",
                "scope": scope,
                "current_tokens": int(current_tokens),
                "current_cost": float(current_cost),
                "reason": reason,
            }
        },
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "code": "usage_limit_exceeded",
            "message": reason,
            "current_tokens": current_tokens,
            "current_cost": current_cost,
            "scope": scope,
        },
    )


def _enforce_usage_caps_in_session(
    db_session: Session,
    *,
    user_id: UUID,
    api_key_id: UUID | None,
) -> None:
    token_cap_raw = os.getenv("DAILY_TOKEN_CAP")
    cost_cap_raw = os.getenv("DAILY_COST_CAP")

    if not token_cap_raw and not cost_cap_raw:
        return

    token_cap_value = _extract_cap_value(token_cap_raw, kind="token")
    cost_cap_value = _extract_cap_value(cost_cap_raw, kind="cost")
    token_cap = int(token_cap_value) if token_cap_value is not None else None
    cost_cap = float(cost_cap_value) if cost_cap_value is not None else None

    # Default to api_key scope when available; fallback to user scope.
    scope = os.getenv("DAILY_CAP_SCOPE", "api_key").strip().lower()
    if scope not in {"api_key", "user"}:
        scope = "api_key"

    if scope == "api_key" and api_key_id is not None:
        try:
            usage = _read_api_key_usage_today(db_session, api_key_id)
        except Exception:
            logger.exception("API key scoped cap check failed; falling back to user scope")
            usage = None

        if usage is not None:
            current_tokens, current_cost = usage
            logger.info(
                "Usage cap check evaluated",
                extra={
                    "extra_fields": {
                        "event": "usage.cap.checked",
                        "scope": "api_key",
                        "api_key_id": str(api_key_id),
                        "token_cap": token_cap,
                        "cost_cap": cost_cap,
                        "current_tokens": int(current_tokens),
                        "current_cost": float(current_cost),
                    }
                },
            )
            if token_cap is not None and current_tokens >= token_cap:
                _deny_usage_limit(
                    reason=f"Daily token limit exceeded ({current_tokens}/{token_cap})",
                    current_tokens=current_tokens,
                    current_cost=current_cost,
                    scope="api_key",
                )
            if cost_cap is not None and current_cost >= cost_cap:
                _deny_usage_limit(
                    reason=f"Daily cost limit exceeded (${current_cost:.2f}/${cost_cap:.2f})",
                    current_tokens=current_tokens,
                    current_cost=current_cost,
                    scope="api_key",
                )
            return

    # CLI-compatible fallback (user/day aggregate).
    result = check_usage_limit(
        db_session,
        user_id,
        token_cap=token_cap,
        cost_cap=cost_cap,
    )
    logger.info(
        "Usage cap check evaluated",
        extra={
            "extra_fields": {
                "event": "usage.cap.checked",
                "scope": "user",
                "user_id": str(user_id),
                "token_cap": token_cap,
                "cost_cap": cost_cap,
                "current_tokens": int(result.get("current_tokens") or 0),
                "current_cost": float(result.get("current_cost") or 0.0),
                "allowed": bool(result.get("allowed")),
            }
        },
    )
    if not result["allowed"]:
        _deny_usage_limit(
            reason=result["reason"],
            current_tokens=result["current_tokens"],
            current_cost=result["current_cost"],
            scope="user",
        )


def _resolve_requests_per_minute(
    db_session: Session,
    *,
    user_id: UUID,
    api_key_id: UUID | None,
) -> int:
    try:
        rpm = resolve_effective_subscription(
            db_session,
            user_id,
        ).plan.limits.requests_per_minute
    except Exception:
        rpm = rate_limit_service.default_requests_per_minute()
    if api_key_id is None:
        return rpm

    try:
        settings = get_api_key_settings(db_session, api_key_id)
    except Exception:
        settings = None

    if not settings:
        return rpm

    override = settings.get("requests_per_minute")
    if override is None:
        return rpm
    try:
        return max(0, int(override))
    except Exception:
        return rpm


def _enforce_rate_limit_in_session(
    db_session: Session,
    *,
    user_id: UUID,
    api_key_id: UUID | None,
) -> None:
    rpm = _resolve_requests_per_minute(
        db_session,
        user_id=user_id,
        api_key_id=api_key_id,
    )
    subject = str(api_key_id or user_id)
    rate_limit_service.enforce_rate_limit(
        subject_key=subject,
        requests_per_minute=rpm,
    )
    logger.info(
        "Rate limit check passed",
        extra={
            "extra_fields": {
                "event": "rate_limit.checked",
                "subject_type": "api_key" if api_key_id is not None else "user",
                "subject": subject,
                "requests_per_minute": int(rpm),
            }
        },
    )


def enforce_usage_caps(
    *,
    user_id: UUID,
    api_key_id: UUID | None,
    db_session: Session | None = None,
) -> None:
    """
    Apply daily caps in API mode.

    When db_session is provided, caller owns transaction boundaries.
    """
    if db_session is not None:
        _enforce_usage_caps_in_session(
            db_session,
            user_id=user_id,
            api_key_id=api_key_id,
        )
        return

    with db_uow(commit_on_success=False) as owned_session:
        _enforce_usage_caps_in_session(
            owned_session,
            user_id=user_id,
            api_key_id=api_key_id,
        )


def resolve_and_enforce_usage_caps(
    *,
    auth: AuthResult,
    request_id: str,
) -> ApiKeyPersistenceResolution:
    """
    Resolve auth (API key or Cognito) and enforce caps using one DB unit-of-work.
    """
    try:
        with db_uow() as db_session:
            resolution = resolve_identity(
                auth=auth,
                request_id=request_id,
                db_session=db_session,
            )
            _enforce_usage_caps_in_session(
                db_session,
                user_id=resolution.user_id,
                api_key_id=resolution.api_key_id,
            )
            _enforce_rate_limit_in_session(
                db_session,
                user_id=resolution.user_id,
                api_key_id=resolution.api_key_id,
            )
            logger.info(
                "API preflight passed",
                extra={
                    "extra_fields": {
                        "event": "api.preflight.success",
                        "request_id": request_id,
                        "user_id": str(resolution.user_id),
                        "api_key_id": str(resolution.api_key_id) if resolution.api_key_id else None,
                        "decision_path": resolution.decision_path,
                    }
                },
            )
            return resolution
    except HTTPException:
        logger.warning(
            "API preflight rejected request",
            extra={
                "extra_fields": {
                    "event": "api.preflight.rejected",
                    "request_id": request_id,
                }
            },
        )
        raise
    except Exception as exc:
        logger.exception("API preflight failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Persistence initialization failed: {exc}",
        ) from exc


def reserve_subscription_usage(
    *,
    user_id: UUID,
    request_id: str,
    operation_type: str,
    model_targets: Iterable[ModelTargetIntent],
    research_enabled: bool,
    web_search_reservation_credits: int | None = None,
    smart_routing: bool = False,
    optimization_enabled: bool = False,
    attachment_count: int = 0,
    total_attachment_bytes: int = 0,
    attachment_sizes: Iterable[int] = (),
    input_text: str = "",
    initial_query: str = "",
    credit_activity_id: str | None = None,
    max_output_tokens: int | None = None,
    max_output_tokens_by_target: Mapping[str, int] | None = None,
    model_attempt_count: int = 1,
) -> ReservedRequestUsage:
    """Authorize and atomically reserve subscription usage before provider work."""
    with db_uow() as db_session:
        reservation = authorize_and_reserve_usage(
            db_session,
            user_id=user_id,
            request_id=request_id,
            operation_type=operation_type,
            model_targets=tuple(model_targets),
            research_enabled=research_enabled,
            web_search_reservation_credits=web_search_reservation_credits,
            smart_routing=smart_routing,
            optimization_enabled=optimization_enabled,
            attachment_count=attachment_count,
            total_attachment_bytes=total_attachment_bytes,
            attachment_sizes=tuple(attachment_sizes),
            input_text=input_text,
            initial_query=(
                None
                if privacy_service.is_metadata_only()
                else privacy_service.sanitize_user_message_for_storage(initial_query)
            ),
            credit_activity_id=credit_activity_id,
            max_output_tokens=max_output_tokens,
            max_output_tokens_by_target=max_output_tokens_by_target,
            model_attempt_count=model_attempt_count,
        )
    from server.billing.reservation_cleanup import register_active_reservation

    register_active_reservation(reservation.reservation_id)
    return reservation


def finalize_subscription_usage(
    *,
    reservation: ReservedRequestUsage,
    successful_targets: Iterable[ModelTargetIntent],
    model_usages: Iterable[BillableModelUsage] = (),
    research_provider_credits_used: int,
    research_usage_estimated: bool = False,
    web_search_usages: Iterable[BillableWebSearchUsage] = (),
    optimization_performed: bool = False,
    file_analysis_performed: bool = False,
    uploaded_bytes: int = 0,
    release_reason: str = "provider_failed_before_billable_output",
) -> None:
    """Settle successful subscription units and release unused units."""
    from server.billing.reservation_cleanup import unregister_active_reservation

    try:
        with db_uow() as db_session:
            finalize_reserved_usage(
                db_session,
                reservation=reservation,
                successful_targets=tuple(successful_targets),
                model_usages=tuple(model_usages),
                research_provider_credits_used=research_provider_credits_used,
                research_usage_estimated=research_usage_estimated,
                web_search_usages=tuple(web_search_usages),
                optimization_performed=optimization_performed,
                file_analysis_performed=file_analysis_performed,
                uploaded_bytes=uploaded_bytes,
                release_reason=release_reason,
            )
    finally:
        unregister_active_reservation(reservation.reservation_id)


def release_subscription_usage(
    *,
    reservation: ReservedRequestUsage,
    reason: str,
) -> None:
    """Release all units for a request that never reached finalization."""
    from server.billing.reservation_cleanup import unregister_active_reservation

    try:
        with db_uow() as db_session:
            release_reserved_usage(
                db_session,
                reservation=reservation,
                reason=reason,
            )
    finally:
        unregister_active_reservation(reservation.reservation_id)


def authorize_smart_fallback(
    *,
    reservation: ReservedRequestUsage,
    provider: str,
    model: str,
) -> bool:
    """Ensure one planned Smart fallback fits the active reservation ceiling."""

    candidate = next(
        (
            item
            for item in reservation.smart_candidates
            if item.provider == str(provider or "").strip().lower()
            and item.model == str(model or "").strip()
        ),
        None,
    )
    if candidate is None:
        return False
    plan = get_plan_catalog().require(reservation.current_plan)
    with db_uow() as db_session:
        return supplement_usage_reservation(
            db_session,
            reservation_id=reservation.reservation_id,
            target_quantity=candidate.reservation_credits,
            allowance_limit=plan.allowances.ai_credits,
        )


def get_failed_attempts_by_request_group(
    *,
    auth: AuthResult,
    request_id: str,
    request_group_id: UUID,
    limit: int = 100,
) -> list[dict]:
    """
    Fetch failed routing attempts for one compare request_group_id scoped to auth owner.
    """
    try:
        with db_uow(commit_on_success=False) as db_session:
            resolution = resolve_identity(
                auth=auth,
                request_id=request_id,
                db_session=db_session,
            )
            return get_failed_routing_attempts_by_request_group(
                db_session,
                resolution.user_id,
                request_group_id,
                limit=limit,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed-attempts lookup failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed-attempts lookup failed: {exc}",
        ) from exc


def resolve_runtime_byok_provider_keys(
    *,
    resolution: ApiKeyPersistenceResolution,
    providers: Iterable[str] | None = None,
) -> dict[str, str]:
    """
    Resolve per-tenant provider API keys for runtime usage (BYOK).
    """
    try:
        with db_uow(commit_on_success=False) as db_session:
            resolved = byok_service.resolve_provider_api_keys(
                db_session,
                api_key_id=resolution.api_key_id,
                providers=providers,
            )
            logger.info(
                "BYOK provider keys resolved",
                extra={
                    "extra_fields": {
                        "event": "byok.resolve.success",
                        "user_id": str(resolution.user_id),
                        "api_key_id": str(resolution.api_key_id) if resolution.api_key_id else None,
                        "provider_count": len(resolved),
                        "providers": sorted(list(resolved.keys())),
                    }
                },
            )
            return resolved
    except Exception:
        logger.exception("BYOK resolution failed")
        return {}


def get_or_create_api_session(
    db_session: Session,
    user_id: UUID,
    requested_session_id: str | None,
    *,
    mode: str,
    title: str,
    force_new_session: bool = False,
) -> UUID:
    """Resolve API session ownership safely; create one when needed."""
    provided = coerce_uuid(requested_session_id)
    provided_for_create: UUID | None = None

    if provided:
        existing_session = get_session_by_id(db_session, provided)
        if existing_session:
            if verify_session_belongs_to_user(db_session, provided, user_id):
                return provided
            # Existing session belongs to another user; never reuse its id.
            provided_for_create = None
        else:
            # Caller may provide a fresh UUID for a brand new session.
            provided_for_create = provided

    if force_new_session:
        return create_session(
            db_session,
            user_id,
            mode=mode,
            title=title,
            session_id=provided_for_create,
        )

    # Session continuity is now shared across Ask and Compare turns.
    active = get_active_session(db_session, user_id, mode=None)
    if active:
        return active

    return create_session(
        db_session,
        user_id,
        mode=mode,
        title=title,
        session_id=provided_for_create,
    )


def _normalize_web_source_items(raw_items: object) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    if not isinstance(raw_items, list):
        return items

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        normalized_url = url.lower()
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        title = str(item.get("title") or "").strip() or url
        items.append({"title": title, "url": url})
        if len(items) >= 8:
            break

    return items


def extract_web_source_items(response: UnifiedResponse) -> list[dict[str, str]]:
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    if not metadata:
        return []

    source_candidates = metadata.get("web_source_items")
    if not isinstance(source_candidates, list):
        source_candidates = metadata.get("sources")

    return _normalize_web_source_items(source_candidates)


def extract_routing_payload(response: UnifiedResponse) -> tuple[dict, list[dict], dict]:
    """Extract routing metadata blobs from UnifiedResponse metadata."""
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    routing_metadata = metadata.get("routing", {})
    if not isinstance(routing_metadata, dict):
        return {}, [], {}

    attempt_rows = (
        routing_metadata.get("selected_sequence") or routing_metadata.get("attempts") or []
    )
    if not isinstance(attempt_rows, list):
        attempt_rows = []

    features = routing_metadata.get("features", {})
    if not isinstance(features, dict):
        features = {}

    return routing_metadata, attempt_rows, features


def persist_routing_telemetry(
    db_session: Session,
    llm_request_id: UUID,
    response: UnifiedResponse,
    research_mode: str,
    *,
    include_research_charge: bool = True,
) -> None:
    """Persist routing telemetry and the exact response-card credit snapshot."""
    routing_metadata, attempt_rows, features = extract_routing_payload(response)
    web_source_items = extract_web_source_items(response)
    credit_usage = resolve_response_credit_usage(
        response,
        include_research_charge=include_research_charge,
    )
    base_routing = dict(routing_metadata or {})
    base_routing.setdefault("mode", "explicit")
    base_routing.setdefault("attempt_count", max(len(attempt_rows), 1))
    base_routing.setdefault("fallback_used", False)
    base_routing["ai_credits"] = credit_usage.ai_credits
    base_routing["credit_usage_estimated"] = credit_usage.credit_usage_estimated
    base_routing["research_ai_credits"] = credit_usage.research_ai_credits
    base_routing["research_credit_usage_estimated"] = (
        credit_usage.research_credit_usage_estimated
    )
    metadata = response.metadata if isinstance(response.metadata, dict) else {}
    raw_web_search = metadata.get("web_search")
    if isinstance(raw_web_search, dict):
        base_routing["web_search"] = {
            key: raw_web_search.get(key)
            for key in (
                "provider",
                "backend",
                "requested_mode",
                "effective_mode",
                "status",
                "operations",
                "usage_estimated",
            )
        }
    if web_source_items:
        base_routing["web_source_items"] = web_source_items
        base_routing["web_sources"] = len(web_source_items)
    routing_metadata = base_routing

    (
        routing_metadata,
        attempt_rows,
        features,
    ) = privacy_service.sanitize_routing_payload_for_storage(
        routing_metadata,
        attempt_rows,
        features,
    )

    prompt_category = (
        metadata.get("prompt_category") or routing_metadata.get("prompt_category") or "unknown"
    )
    normalized_research_mode = (
        metadata.get("research_mode")
        or routing_metadata.get("research_mode")
        or research_mode
        or "off"
    )

    with db_session.begin_nested():
        routing_decision_id = create_routing_decision(
            db_session,
            llm_request_id,
            routing_metadata,
            prompt_category=str(prompt_category),
            research_mode=str(normalized_research_mode),
            features=features,
        )
        create_routing_attempts(db_session, routing_decision_id, attempt_rows)


def _record_research_reuse_event(
    db_session: Session,
    *,
    user_id: UUID,
    request_id: str,
    responses: Sequence[UnifiedResponse],
) -> None:
    """Persist one research decision per Ask/Compare operation."""

    for response in responses:
        metadata = response.metadata if isinstance(response.metadata, dict) else {}
        if not bool(metadata.get("research_used")):
            continue
        record_cache_reuse_event(
            db_session,
            user_id=user_id,
            request_id=request_id,
            operation_type="research",
            reused=bool(metadata.get("research_reused")),
        )
        return


def build_error_response(
    *,
    provider: str,
    model: str,
    message: str,
    request_id: str | None = None,
    code: str = "provider_error",
    retryable: bool = True,
) -> UnifiedResponse:
    """Build a minimal UnifiedResponse error payload for persistence/audit."""
    return UnifiedResponse(
        request_id=request_id or str(uuid4()),
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
            details={},
        ),
    )


def _persist_request_attachments(
    db_session: Session,
    *,
    llm_request_id: UUID,
    attachments: Sequence[object] | None,
) -> None:
    """Persist request attachment links for one llm_request row."""
    if not attachments:
        return

    for idx, item in enumerate(attachments):
        file_id: object | None
        if isinstance(item, RequestAttachmentPersistenceItem):
            file_id = item.file_id
            usage_role = item.usage_role
            transform_mode = item.transform_mode
            order_index = int(item.order_index)
            resolved_artifact_meta = (
                dict(item.resolved_artifact_meta)
                if isinstance(item.resolved_artifact_meta, dict)
                else {}
            )
        elif isinstance(item, dict):
            file_id = item.get("file_id")
            usage_role = str(item.get("usage_role") or "primary")
            transform_mode = str(item.get("transform_mode") or "auto")
            order_index = int(item.get("order_index", idx))
            meta_raw = item.get("resolved_artifact_meta")
            resolved_artifact_meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
        else:
            file_id = getattr(item, "file_id", None)
            usage_role = str(getattr(item, "usage_role", "primary") or "primary")
            transform_mode = str(getattr(item, "transform_mode", "auto") or "auto")
            order_index = int(getattr(item, "order_index", idx))
            meta_raw = getattr(item, "resolved_artifact_meta", None)
            resolved_artifact_meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}

        if file_id is None:
            raise ValueError("Attachment item is missing file_id")
        if not isinstance(file_id, UUID):
            file_id = UUID(str(file_id))

        create_request_attachment(
            db_session,
            llm_request_id=llm_request_id,
            file_id=file_id,
            order_index=order_index,
            usage_role=usage_role,
            transform_mode=transform_mode,
            resolved_artifact_meta=resolved_artifact_meta,
        )


def persist_chat_interaction(
    *,
    api_key: str | None,
    resolution: ApiKeyPersistenceResolution,
    prompt: str,
    response: UnifiedResponse,
    requested_session_id: str | None,
    research_mode: bool,
    force_new_session: bool = False,
    attachments: Sequence[object] | None = None,
    regeneration: CompareRegenerationContext | None = None,
) -> str:
    """Persist API chat request/response using the same artifacts as CLI."""
    with db_uow() as db_session:
        if regeneration is not None:
            session_id = regeneration.session_id
            if not verify_session_belongs_to_user(
                db_session,
                session_id,
                resolution.user_id,
            ):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Compare session not found",
                )
        else:
            session_id = get_or_create_api_session(
                db_session,
                resolution.user_id,
                requested_session_id,
                mode="ask",
                title="API Chat",
                force_new_session=force_new_session,
            )

        stored_user_message = privacy_service.sanitize_user_message_for_storage(prompt)
        if regeneration is None:
            save_message(db_session, session_id, "user", stored_user_message)

        llm_request_id = create_llm_request(
            db_session,
            user_id=resolution.user_id,
            request_id=response.request_id,
            route_mode="compare" if regeneration is not None else "ask",
            provider=response.provider,
            model=response.requested_model or response.model,
            requested_model=response.requested_model or response.model,
            prompt=prompt,
            session_id=session_id,
            request_group_id=(regeneration.request_group_id if regeneration is not None else None),
            api_key_id=resolution.api_key_id,
            store_prompt=True,
            prompt_text_override=stored_user_message,
            response_revision_root_id=(
                regeneration.response_revision_root_id if regeneration is not None else None
            ),
            response_revision=(regeneration.response_revision if regeneration is not None else 1),
            generation_budget=dict((response.metadata or {}).get("generation_budget") or {}),
        )
        _persist_request_attachments(
            db_session,
            llm_request_id=llm_request_id,
            attachments=attachments,
        )
        stored_response = privacy_service.sanitize_response_for_storage(response)
        create_llm_response(db_session, llm_request_id, stored_response)

        persist_routing_telemetry(
            db_session,
            llm_request_id,
            response,
            research_mode="on" if research_mode else "off",
        )
        _record_research_reuse_event(
            db_session,
            user_id=resolution.user_id,
            request_id=response.request_id,
            responses=(response,),
        )

        if regeneration is None and not response.is_error and response.text:
            assistant_text = privacy_service.sanitize_assistant_message_for_storage(response.text)
            save_message(db_session, session_id, "assistant", assistant_text)

        try:
            savings_service.persist_request_savings(
                db_session,
                llm_request_id=llm_request_id,
                user_id=resolution.user_id,
                api_key_id=resolution.api_key_id,
                response=response,
            )
        except Exception:
            logger.exception("Savings persistence failed for chat request")

        update_session_timestamp(db_session, session_id)
        upsert_usage_daily(
            db_session,
            user_id=resolution.user_id,
            total_tokens=response.token_usage.total_tokens,
            estimated_cost=response.estimated_cost,
        )

        if resolution.api_key_id is not None and api_key is not None:
            update_api_key_last_used(db_session, api_key)

        return str(session_id)


def _selected_compare_index(responses: Iterable[UnifiedResponse]) -> int:
    for index, response in enumerate(responses):
        if not response.is_error:
            return index
    return 0


def persist_compare_interaction(
    *,
    api_key: str | None,
    resolution: ApiKeyPersistenceResolution,
    prompt: str,
    responses: list[UnifiedResponse],
    request_group_id: str,
    requested_session_id: str | None,
    research_mode: bool,
    force_new_session: bool = False,
    attachments: Sequence[object] | None = None,
) -> str:
    """Persist compare run artifacts using shared DB tables and request grouping."""
    with db_uow() as db_session:
        session_id = get_or_create_api_session(
            db_session,
            resolution.user_id,
            requested_session_id,
            mode="compare",
            title="API Compare",
            force_new_session=force_new_session,
        )

        group_uuid = coerce_uuid(request_group_id)

        stored_user_message = privacy_service.sanitize_user_message_for_storage(prompt)
        save_message(db_session, session_id, "user", stored_user_message)
        stored_compare_responses: list[UnifiedResponse] = []

        for response in responses:
            llm_request_id = create_llm_request(
                db_session,
                user_id=resolution.user_id,
                request_id=response.request_id,
                route_mode="compare",
                provider=response.provider,
                model=response.requested_model or response.model,
                requested_model=response.requested_model or response.model,
                prompt=prompt,
                session_id=session_id,
                request_group_id=group_uuid,
                api_key_id=resolution.api_key_id,
                store_prompt=True,
                prompt_text_override=stored_user_message,
                generation_budget=dict((response.metadata or {}).get("generation_budget") or {}),
            )
            _persist_request_attachments(
                db_session,
                llm_request_id=llm_request_id,
                attachments=attachments,
            )
            stored_response = privacy_service.sanitize_response_for_storage(response)
            stored_compare_responses.append(stored_response)
            create_llm_response(db_session, llm_request_id, stored_response)
            persist_routing_telemetry(
                db_session,
                llm_request_id,
                response,
                research_mode="on" if research_mode else "off",
                include_research_charge=False,
            )
            try:
                savings_service.persist_request_savings(
                    db_session,
                    llm_request_id=llm_request_id,
                    user_id=resolution.user_id,
                    api_key_id=resolution.api_key_id,
                    response=response,
                )
            except Exception:
                logger.exception("Savings persistence failed for compare request")

        _record_research_reuse_event(
            db_session,
            user_id=resolution.user_id,
            request_id=str(request_group_id),
            responses=responses,
        )

        if responses and any(not r.is_error for r in responses):
            save_compare_summary(
                db_session,
                session_id,
                stored_compare_responses,
                selected_model_index=_selected_compare_index(responses),
            )

        update_session_timestamp(db_session, session_id)

        total_tokens = sum(r.token_usage.total_tokens for r in responses)
        total_cost = sum(r.estimated_cost for r in responses)
        upsert_usage_daily(
            db_session,
            user_id=resolution.user_id,
            total_tokens=total_tokens,
            estimated_cost=total_cost,
        )

        if resolution.api_key_id is not None and api_key is not None:
            update_api_key_last_used(db_session, api_key)

        return str(session_id)
