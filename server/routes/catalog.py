"""Provider and model discovery endpoints."""

import os
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from config.provider_catalog import get_provider_catalog, get_provider_ids
from orchestrator.model_registry import ModelRegistry
from server.dependencies import AuthResult, get_auth
from server.routes.session_auth import SessionScopedAuthGuard
from server.schemas.responses import (
    ModelCatalogItemDTO,
    ModelsCatalogResponseDTO,
    ProviderCatalogItemDTO,
    ProvidersCatalogResponseDTO,
)
from utils.logger import get_logger

router = APIRouter(prefix="/v1", tags=["Catalog"])
logger = get_logger(__name__)
_SESSION_AUTH_GUARD = SessionScopedAuthGuard(
    route_label="Catalog",
    rejection_event="catalog.route.rejected.auth_mode",
    logger=logger,
)


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _models_catalog_enabled() -> bool:
    raw = os.getenv("ENABLE_MODELS_CATALOG")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _ensure_models_catalog_enabled() -> None:
    if not _models_catalog_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _normalize_provider(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized or None


def _validate_provider_or_400(provider: str | None) -> str | None:
    provider_norm = _normalize_provider(provider)
    if provider_norm is None:
        return None

    allowed = set(get_provider_ids())
    if provider_norm not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported provider '{provider_norm}'. " f"Supported providers: {allowed_text}"
            ),
        )
    return provider_norm


def _model_to_dto(candidate) -> ModelCatalogItemDTO:
    return ModelCatalogItemDTO(
        provider=candidate.provider,
        model=candidate.model_name,
        tier=candidate.tier.value,
        billing_class=candidate.billing_class.value,
        access_category=candidate.billing_class.value,
        input_credit_multiplier=candidate.input_credit_multiplier,
        output_credit_multiplier=candidate.output_credit_multiplier,
        credit_usage_label=candidate.credit_usage_label,
        credit_pricing_version=candidate.credit_pricing_version,
        input_cost_per_1m=float(candidate.input_cost_per_1m),
        output_cost_per_1m=float(candidate.output_cost_per_1m),
        cached_input_cost_per_1m=candidate.cached_input_cost_per_1m,
        cache_write_cost_per_1m=candidate.cache_write_cost_per_1m,
        context_limit=int(candidate.context_limit),
        max_output_tokens=candidate.max_output_tokens,
        tags=list(candidate.tags or []),
        enabled=bool(candidate.enabled),
        selectable=bool(candidate.selectable),
        display_name=candidate.display_name,
        description=candidate.description,
        release_status=candidate.release_status,
        lifecycle_status=candidate.lifecycle_status,
        replacement_model=candidate.replacement_model,
        retirement_date=candidate.retirement_date,
        migration_reason=candidate.migration_reason,
        pricing_model=candidate.pricing_model,
        pricing_rule_id=candidate.pricing_rule_id,
        pricing_effective_from=candidate.pricing_effective_from,
        pricing_effective_until=candidate.pricing_effective_until,
        long_context_threshold_tokens=candidate.long_context_threshold_tokens,
        aliases=list(candidate.aliases or []),
        reasoning_modes=list(candidate.reasoning_modes or []),
        default_reasoning_mode=candidate.default_reasoning_mode,
        reasoning_efforts=list(candidate.reasoning_efforts or []),
        reasoning_disable_supported=bool(candidate.reasoning_disable_supported),
        reasoning_counts_against_output=bool(candidate.reasoning_counts_against_output),
        pricing_source_url=candidate.pricing_source_url,
        lifecycle_source_url=candidate.lifecycle_source_url,
        source_verified_at=candidate.source_verified_at,
        supports_image_input=bool(getattr(candidate, "supports_image_input", False)),
        supported_attachment_mime_types=list(
            getattr(candidate, "supported_attachment_mime_types", []) or []
        ),
        max_attachment_bytes=getattr(candidate, "max_attachment_bytes", None),
        max_attachments_per_request=getattr(candidate, "max_attachments_per_request", None),
    )


@router.get("/providers", response_model=ProvidersCatalogResponseDTO)
async def list_providers(
    request: Request,
    auth: AuthResult = Depends(get_auth),
):
    """List discoverable providers and metadata for API/front-end clients."""
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    catalog = get_provider_catalog()
    registry = ModelRegistry.from_yaml()

    items: list[ProviderCatalogItemDTO] = []
    for spec in catalog.provider_specs():
        all_models = registry.list_models(spec.provider_id, include_disabled=True)
        enabled_models = [model for model in all_models if model.enabled and model.selectable]
        items.append(
            ProviderCatalogItemDTO(
                provider=spec.provider_id,
                label=spec.label,
                api_key_env=spec.api_key_env,
                default_model_env=spec.default_model_env,
                default_model=spec.default_model,
                byok_supported=spec.byok_supported,
                capabilities=list(spec.capabilities),
                ui=dict(spec.ui),
                model_count=len(all_models),
                enabled_model_count=len(enabled_models),
            )
        )

    return ProvidersCatalogResponseDTO(
        providers=items,
        total=len(items),
        timestamp=_utc_now_iso(),
    )


@router.get(
    "/models",
    response_model=ModelsCatalogResponseDTO,
    dependencies=[Depends(_ensure_models_catalog_enabled)],
)
async def list_models(
    request: Request,
    provider: str | None = Query(default=None),
    enabled_only: bool = Query(default=True),
    auth: AuthResult = Depends(get_auth),
):
    """List discoverable models, optionally filtered by provider."""
    req_id = str(getattr(request.state, "request_id", "") or uuid4())
    _SESSION_AUTH_GUARD.require(auth=auth, request_id=req_id)
    provider_norm = _validate_provider_or_400(provider)
    registry = ModelRegistry.from_yaml()

    candidates = (
        registry.list_selectable_models(provider=provider_norm)
        if enabled_only
        else registry.list_models(provider=provider_norm, include_disabled=True)
    )
    items = [_model_to_dto(candidate) for candidate in candidates]
    items.sort(key=lambda item: (item.provider, item.model))

    return ModelsCatalogResponseDTO(
        provider=provider_norm,
        enabled_only=enabled_only,
        models=items,
        total=len(items),
        timestamp=_utc_now_iso(),
    )
