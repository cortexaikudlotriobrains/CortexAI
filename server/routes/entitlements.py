"""Authenticated effective-plan and allowance snapshot endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request

from db.billing_repository import list_credit_transactions

from server import persistence as persistence_service
from server.billing.entitlement_service import load_allowance_usage
from server.billing.errors import (
    BillingConfigurationError,
    BillingIdentityError,
    billing_configuration_http_exception,
    billing_database_required_http_exception,
    billing_identity_http_exception,
)
from server.billing.subscription_service import resolve_effective_subscription
from server.dependencies import AuthResult, get_auth
from server.schemas.responses import (
    EntitlementAllowanceDTO,
    EntitlementFeaturesDTO,
    EntitlementLimitsDTO,
    EntitlementModelAccessDTO,
    EntitlementPeriodDTO,
    EntitlementPlanDTO,
    EntitlementsResponseDTO,
    CreditTransactionDTO,
    CreditTransactionsResponseDTO,
)
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["Billing"])
logger = get_logger(__name__)

API_DB_ENABLED = persistence_service.API_DB_ENABLED
_resolve_identity = persistence_service.resolve_identity
_db_uow = persistence_service.db_uow


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _required_iso(value: datetime) -> str:
    result = _iso(value)
    if result is None:  # pragma: no cover - defensive for type narrowing
        raise BillingConfigurationError("Subscription period timestamp is missing")
    return result


@router.get("/entitlements", response_model=EntitlementsResponseDTO)
async def entitlements(
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    """Return the server-resolved plan, features, period, and all counters."""
    if not API_DB_ENABLED:
        raise billing_database_required_http_exception()

    request_id = str(getattr(request.state, "request_id", "") or uuid4())
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
            allowances = load_allowance_usage(db_session, effective)
    except BillingIdentityError as exc:
        raise billing_identity_http_exception() from exc
    except BillingConfigurationError as exc:
        logger.exception(
            "Entitlement resolution failed",
            extra={"extra_fields": {"request_id": request_id}},
        )
        raise billing_configuration_http_exception() from exc

    plan_entitlements = effective.plan.entitlements
    return EntitlementsResponseDTO(
        plan=EntitlementPlanDTO(
            code=effective.plan.code,
            display_name=effective.plan.display_name,
            status=effective.status,
            source=effective.source,
            renews_at=_required_iso(effective.current_period_end),
            cancel_at_period_end=effective.cancel_at_period_end,
            grace_until=_iso(effective.grace_until),
        ),
        features=EntitlementFeaturesDTO(
            compare_enabled=plan_entitlements.compare_enabled,
            max_compare_models=plan_entitlements.max_compare_models,
            research_enabled=plan_entitlements.research_enabled,
            prompt_improvement_enabled=plan_entitlements.prompt_improvement_enabled,
            file_analysis_enabled=plan_entitlements.file_analysis_enabled,
            usage_export_enabled=plan_entitlements.usage_export_enabled,
            saved_history_enabled=plan_entitlements.saved_history_enabled,
            models_catalog_enabled=plan_entitlements.models_catalog_enabled,
            work_enabled=plan_entitlements.work_enabled,
            verified_connectors_enabled=plan_entitlements.verified_connectors_enabled,
            custom_mcp_enabled=plan_entitlements.custom_mcp_enabled,
            action_tools_enabled=plan_entitlements.action_tools_enabled,
        ),
        model_access=EntitlementModelAccessDTO(
            allowed_billing_classes=sorted(plan_entitlements.allowed_billing_classes)
        ),
        limits=EntitlementLimitsDTO(
            max_files_per_request=effective.plan.limits.max_files_per_request,
            max_file_bytes=effective.plan.limits.max_file_bytes,
            max_active_work_runs=effective.plan.limits.max_active_work_runs,
            max_tool_connections=effective.plan.limits.max_tool_connections,
            max_mcp_servers_per_run=effective.plan.limits.max_mcp_servers_per_run,
            max_work_credit_budget=effective.plan.limits.max_work_credit_budget,
        ),
        allowances={
            meter: EntitlementAllowanceDTO(
                used=allowance.used,
                reserved=allowance.reserved,
                limit=allowance.limit,
                remaining=allowance.remaining,
            )
            for meter, allowance in allowances.items()
        },
        period=EntitlementPeriodDTO(
            starts_at=_required_iso(effective.current_period_start),
            ends_at=_required_iso(effective.current_period_end),
        ),
    )


@router.get("/credits/transactions", response_model=CreditTransactionsResponseDTO)
async def credit_transactions(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth: AuthResult = Depends(get_auth),
):
    """Return itemized AI-credit usage for the authenticated billing account."""
    if not API_DB_ENABLED:
        raise billing_database_required_http_exception()
    request_id = str(getattr(request.state, "request_id", "") or uuid4())
    try:
        with _db_uow() as db_session:
            identity = _resolve_identity(
                auth=auth,
                request_id=request_id,
                db_session=db_session,
            )
            effective = resolve_effective_subscription(db_session, identity.user_id)
            rows = list_credit_transactions(
                db_session,
                billing_account_id=effective.billing_account_id,
                limit=limit,
                offset=offset,
            )
    except BillingIdentityError as exc:
        raise billing_identity_http_exception() from exc
    except BillingConfigurationError as exc:
        raise billing_configuration_http_exception() from exc

    return CreditTransactionsResponseDTO(
        items=[
            CreditTransactionDTO(
                id=str(row["id"]),
                request_id=str(row["request_id"]),
                activity_id=str(
                    (row.get("metadata") or {}).get("credit_activity_id") or row["request_id"]
                ),
                query=(str((row.get("metadata") or {}).get("initial_query") or "").strip() or None),
                operation_type=str(row["operation_type"]),
                item_type=str(row["item_type"]),
                provider=str(row["provider"]) if row.get("provider") else None,
                model=str(row["model"]) if row.get("model") else None,
                input_tokens=int(row.get("input_tokens") or 0),
                normal_input_tokens=int(row.get("normal_input_tokens") or 0),
                cached_input_tokens=int(row.get("cached_input_tokens") or 0),
                cache_write_tokens=int(row.get("cache_write_tokens") or 0),
                reasoning_tokens=int(row.get("reasoning_tokens") or 0),
                output_tokens=int(row.get("output_tokens") or 0),
                input_credits=int(row.get("input_credits") or 0),
                normal_input_credits=int(row.get("normal_input_credits") or 0),
                cached_input_credits=int(row.get("cached_input_credits") or 0),
                cache_write_credits=int(row.get("cache_write_credits") or 0),
                output_credits=int(row.get("output_credits") or 0),
                fixed_credits=int(row.get("fixed_credits") or 0),
                total_credits=int(row.get("total_credits") or 0),
                uncached_equivalent_credits=int(row.get("uncached_equivalent_credits") or 0),
                cache_savings_credits=int(row.get("cache_savings_credits") or 0),
                provider_cost_usd=float(row.get("provider_cost_usd") or 0),
                usage_estimated=bool(row.get("usage_estimated")),
                pricing_version=str(row["pricing_version"]),
                metadata=dict(row.get("metadata") or {}),
                created_at=_required_iso(row["created_at"]),
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )
