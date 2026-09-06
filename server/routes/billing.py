"""Authenticated Stripe Checkout and Customer Portal session routes."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, status

from orchestrator.model_registry import ModelRegistry
from server import persistence as persistence_service
from server.billing.credit_calculator import ADVANCED_WEB_SEARCH_CREDITS
from server.billing.credit_estimator import estimate_model_credits
from server.billing.entitlement_service import load_allowance_usage
from server.billing.errors import (
    BillingConfigurationError,
    BillingIdentityError,
    BillingNotConfiguredError,
    BillingPlanSelectionError,
    BillingProviderError,
    BillingWebhookProcessingError,
    InvalidWebhookSignatureError,
    StripeCustomerRequiredError,
    billing_configuration_http_exception,
    billing_database_required_http_exception,
    billing_identity_http_exception,
    billing_not_configured_http_exception,
    billing_plan_selection_http_exception,
    billing_provider_http_exception,
    billing_webhook_processing_http_exception,
    invalid_webhook_signature_http_exception,
    stripe_customer_required_http_exception,
)
from server.billing.session_service import (
    create_checkout_redirect,
    create_portal_redirect,
)
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import resolve_effective_subscription
from server.billing.stripe_gateway import (
    StripeBillingConfig,
    StripeGateway,
    require_stripe_billing_config,
    stripe_billing_is_enabled,
)
from server.billing.webhook_service import process_stripe_webhook
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.requests import (
    CheckoutSessionRequest,
    GenerationEstimateRequest,
    PortalSessionRequest,
)
from server.schemas.responses import (
    BillingPlansResponseDTO,
    BillingSubscriptionResponseDTO,
    BillingWebhookResponseDTO,
    CheckoutSessionResponseDTO,
    GenerationEstimateResponseDTO,
    GenerationEstimateTargetDTO,
    PortalSessionResponseDTO,
    PublicBillingPlanAllowancesDTO,
    PublicBillingPlanDTO,
    PublicBillingPlanFeaturesDTO,
)
from server.generation_service import resolve_request_budget
from utils.logger import get_logger

router = APIRouter(prefix="/v1/billing", tags=["Billing"])
logger = get_logger(__name__)

API_DB_ENABLED = persistence_service.API_DB_ENABLED
_resolve_identity = persistence_service.resolve_identity
_db_uow = persistence_service.db_uow
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="Billing",
    rejection_event="billing.auth.session_required",
    logger=logger,
)
_MAX_WEBHOOK_BODY_BYTES = 1024 * 1024


def _gateway_factory(config: StripeBillingConfig) -> StripeGateway:
    return StripeGateway(config)


async def _read_webhook_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > _MAX_WEBHOOK_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={
                    "code": "billing_webhook_payload_too_large",
                    "message": "The Stripe webhook payload is too large.",
                },
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_billing_http_error(error: Exception, *, request_id: str) -> None:
    if isinstance(error, BillingNotConfiguredError):
        raise billing_not_configured_http_exception() from error
    if isinstance(error, BillingPlanSelectionError):
        raise billing_plan_selection_http_exception(error) from error
    if isinstance(error, StripeCustomerRequiredError):
        raise stripe_customer_required_http_exception() from error
    if isinstance(error, BillingProviderError):
        logger.warning(
            "Stripe hosted billing session creation failed",
            extra={"extra_fields": {"request_id": request_id}},
        )
        raise billing_provider_http_exception() from error
    if isinstance(error, BillingIdentityError):
        raise billing_identity_http_exception() from error
    logger.exception(
        "Billing session configuration failed",
        extra={"extra_fields": {"request_id": request_id}},
    )
    raise billing_configuration_http_exception() from error


def _authenticated_user_id(auth: AuthResult, *, request_id: str) -> UUID:
    with _db_uow() as db_session:
        identity = _resolve_identity(
            auth=auth,
            request_id=request_id,
            db_session=db_session,
        )
        return identity.user_id


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post("/estimate-generation", response_model=GenerationEstimateResponseDTO)
async def estimate_generation(
    payload: GenerationEstimateRequest,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    """Estimate the maximum temporary AI-credit hold without reserving credits."""
    if not API_DB_ENABLED:
        raise billing_database_required_http_exception()
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=request_id)
    registry = ModelRegistry.from_yaml()
    target_estimates: list[GenerationEstimateTargetDTO] = []
    total = ADVANCED_WEB_SEARCH_CREDITS if payload.research_enabled else 0
    for target in payload.targets:
        candidate = registry.find_model(target.provider, target.model)
        if candidate is None or not candidate.enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "invalid_model_selection",
                    "message": f"Unknown or disabled model '{target.provider}:{target.model}'",
                },
            )
        budget = resolve_request_budget(
            provider=target.provider,
            model=target.model,
            generation=target.generation or payload.generation,
            legacy_max_tokens=None,
            input_text=payload.prompt,
            registry=registry,
        )
        estimate = estimate_model_credits(
            candidate,
            input_text=payload.prompt,
            max_output_tokens=budget.effective_max_output_tokens,
        )
        total += estimate.charge.total_credits
        target_estimates.append(
            GenerationEstimateTargetDTO(
                provider=target.provider,
                model=target.model,
                profile=budget.profile,
                effective_max_output_tokens=budget.effective_max_output_tokens,
                estimated_max_ai_credits=estimate.charge.total_credits,
            )
        )

    with _db_uow() as db_session:
        identity = _resolve_identity(auth=auth, request_id=request_id, db_session=db_session)
        effective = resolve_effective_subscription(db_session, identity.user_id)
        remaining = load_allowance_usage(db_session, effective)["ai_credits"].remaining
    return GenerationEstimateResponseDTO(
        targets=target_estimates,
        estimated_max_ai_credits=total,
        remaining_ai_credits=remaining,
        can_authorize=total <= remaining,
    )


@router.get("/plans", response_model=BillingPlansResponseDTO)
async def billing_plans():
    """Return display-safe, server-owned plan information without Stripe identifiers."""
    catalog = get_plan_catalog()
    return BillingPlansResponseDTO(
        billing_enabled=stripe_billing_is_enabled(),
        plans=[
            PublicBillingPlanDTO(
                code=plan.code,
                display_name=plan.display_name,
                monthly_price=float(plan.monthly_price_usd),
                recommended=plan.code == "plus",
                features=PublicBillingPlanFeaturesDTO(
                    max_compare_models=plan.entitlements.max_compare_models,
                    research_enabled=plan.entitlements.research_enabled,
                    prompt_improvement_enabled=(plan.entitlements.prompt_improvement_enabled),
                    file_analysis_enabled=plan.entitlements.file_analysis_enabled,
                    work_enabled=plan.entitlements.work_enabled,
                    verified_connectors_enabled=(plan.entitlements.verified_connectors_enabled),
                    custom_mcp_enabled=plan.entitlements.custom_mcp_enabled,
                    action_tools_enabled=(plan.entitlements.action_tools_enabled),
                    max_active_work_runs=plan.limits.max_active_work_runs,
                    allowed_billing_classes=sorted(plan.entitlements.allowed_billing_classes),
                ),
                allowances=PublicBillingPlanAllowancesDTO(
                    ai_credits=plan.allowances.ai_credits,
                ),
            )
            for plan in catalog.list_plans()
        ],
    )


@router.get("/subscription", response_model=BillingSubscriptionResponseDTO)
async def current_subscription(
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    """Return the authenticated user's effective, provider-safe subscription state."""
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=request_id)
    if not API_DB_ENABLED:
        raise billing_database_required_http_exception()

    try:
        with _db_uow() as db_session:
            identity = _resolve_identity(
                auth=auth,
                request_id=request_id,
                db_session=db_session,
            )
            effective = resolve_effective_subscription(
                db_session,
                identity.user_id,
                now=datetime.now(UTC),
            )
    except BillingIdentityError as exc:
        raise billing_identity_http_exception() from exc
    except BillingConfigurationError as exc:
        logger.exception(
            "Current subscription resolution failed",
            extra={"extra_fields": {"request_id": request_id}},
        )
        raise billing_configuration_http_exception() from exc

    return BillingSubscriptionResponseDTO(
        plan_code=effective.plan.code,
        status=effective.status,
        provider=effective.provider,
        current_period_start=_iso(effective.current_period_start),
        current_period_end=_iso(effective.current_period_end),
        cancel_at_period_end=effective.cancel_at_period_end,
        can_manage=bool(effective.provider == "stripe" and effective.provider_subscription_id),
    )


@router.post(
    "/checkout-session",
    response_model=CheckoutSessionResponseDTO,
)
async def checkout_session(
    payload: CheckoutSessionRequest,
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=request_id)

    try:
        config = require_stripe_billing_config()
        if not API_DB_ENABLED:
            raise billing_database_required_http_exception()
        redirect = await create_checkout_redirect(
            uow_factory=_db_uow,
            gateway=_gateway_factory(config),
            config=config,
            user_id=_authenticated_user_id(auth, request_id=request_id),
            plan_code=payload.plan_code,
            billing_period=payload.billing_period,
        )
    except (
        BillingConfigurationError,
        BillingIdentityError,
        BillingNotConfiguredError,
        BillingPlanSelectionError,
        BillingProviderError,
        StripeCustomerRequiredError,
    ) as exc:
        _raise_billing_http_error(exc, request_id=request_id)
        raise AssertionError("unreachable") from exc

    return CheckoutSessionResponseDTO(
        checkout_url=redirect.url,
        destination=redirect.destination,
    )


@router.post(
    "/portal-session",
    response_model=PortalSessionResponseDTO,
)
async def portal_session(
    request: Request,
    payload: PortalSessionRequest | None = Body(default=None),
    auth: AuthResult = Depends(get_auth),
):
    del payload
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=request_id)

    try:
        config = require_stripe_billing_config()
        if not API_DB_ENABLED:
            raise billing_database_required_http_exception()
        redirect = await create_portal_redirect(
            uow_factory=_db_uow,
            gateway=_gateway_factory(config),
            user_id=_authenticated_user_id(auth, request_id=request_id),
        )
    except (
        BillingConfigurationError,
        BillingIdentityError,
        BillingNotConfiguredError,
        BillingPlanSelectionError,
        BillingProviderError,
        StripeCustomerRequiredError,
    ) as exc:
        _raise_billing_http_error(exc, request_id=request_id)
        raise AssertionError("unreachable") from exc

    return PortalSessionResponseDTO(portal_url=redirect.url)


@router.post(
    "/webhook",
    response_model=BillingWebhookResponseDTO,
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
):
    """Synchronize verified Stripe lifecycle state without user authentication."""
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    raw_body = await _read_webhook_body(request)

    try:
        config = require_stripe_billing_config()
        if not API_DB_ENABLED:
            raise billing_database_required_http_exception()
        await process_stripe_webhook(
            uow_factory=_db_uow,
            gateway=_gateway_factory(config),
            config=config,
            payload=raw_body,
            signature=stripe_signature or "",
        )
    except InvalidWebhookSignatureError as exc:
        logger.warning(
            "Stripe webhook signature verification failed",
            extra={
                "extra_fields": {
                    "event": "billing.webhook.signature_rejected",
                    "request_id": request_id,
                }
            },
        )
        raise invalid_webhook_signature_http_exception() from exc
    except BillingNotConfiguredError as exc:
        raise billing_not_configured_http_exception() from exc
    except BillingWebhookProcessingError as exc:
        raise billing_webhook_processing_http_exception() from exc
    except BillingConfigurationError as exc:
        logger.exception(
            "Stripe webhook configuration failed",
            extra={"extra_fields": {"request_id": request_id}},
        )
        raise billing_configuration_http_exception() from exc

    return BillingWebhookResponseDTO()
