"""Subscription authorization and unified AI-credit settlement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from math import floor
from typing import Protocol, TypedDict
from uuid import UUID

from sqlalchemy.orm import Session

from config.cache_optimization import (
    CACHE_AWARE_CREDIT_POLICY_VERSION,
    cache_aware_credit_calculation_enabled,
    cache_aware_credit_settlement_enabled,
    credit_aware_generation_budget_enabled,
)
from db import billing_repository as repository
from orchestrator.model_registry import ModelRegistry
from orchestrator.routing_types import ModelCandidate
from server.billing.credit_calculator import (
    ADVANCED_WEB_SEARCH_CREDITS,
    CORTEX_CREDITS_PER_TAVILY_CREDIT,
    CreditCharge,
    calculate_credit_charge,
    calculate_model_credit_charge,
    calculate_research_credit_charge,
    floor_credit_multipliers,
    max_provider_usd_per_million_credits,
)
from server.billing.credit_estimator import estimate_model_credits, fallback_actual_tokens
from server.billing.entitlement_service import (
    EntitlementDenial,
    ModelTargetIntent,
    SubscriptionRequestIntent,
    evaluate_entitlement,
    load_allowance_usage,
)
from server.billing.errors import (
    BillingConfigurationError,
    EntitlementDeniedError,
    InvalidModelSelectionError,
    UsageAllowanceExceededError,
)
from server.billing.metering_service import (
    release_usage,
    reserve_usage,
    settle_usage_with_supplement,
)
from server.billing.models import ALLOWED_MODEL_BILLING_CLASSES
from server.billing.plan_catalog import get_plan_catalog
from server.billing.subscription_service import (
    EffectiveSubscription,
    resolve_effective_subscription,
)
from utils.logger import get_logger

_ATTACHMENT_ESTIMATE_CHARS = 12_000
_RESEARCH_CONTEXT_ESTIMATE_CHARS = 30_000
logger = get_logger(__name__)


@dataclass(frozen=True)
class ReservedModelEstimate:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    input_multiplier: float
    output_multiplier: float
    pricing_version: str
    total_credits: int


@dataclass(frozen=True)
class SmartCandidateEstimate:
    provider: str
    model: str
    billing_class: str
    reservation_credits: int
    expected_input_credits: int = 0
    expected_output_credits: int = 0
    expected_research_credits: int = 0
    expected_total_credits: int = 0


@dataclass(frozen=True)
class ReservedRequestUsage:
    reservation_id: UUID
    request_id: str
    operation_type: str
    requested_quantities: dict[str, int]
    allowed_billing_classes: frozenset[str]
    current_plan: str
    reset_at: datetime
    input_text: str = ""
    initial_query: str | None = None
    credit_activity_id: str | None = None
    model_estimates: tuple[ReservedModelEstimate, ...] = ()
    smart_candidates: tuple[SmartCandidateEstimate, ...] = ()


@dataclass(frozen=True)
class BillableModelUsage:
    provider: str
    model: str
    prompt_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    output_text: str = ""
    provider_cost_usd: float = 0.0
    pricing_snapshot: Mapping[str, object] | None = None
    pricing_version: str | None = None
    provider_cost_owner: str = "cortex"
    usage_estimated: bool = False
    # Transitional constructor compatibility for older internal callers/tests.
    input_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens == 0 and self.input_tokens is not None:
            object.__setattr__(self, "prompt_tokens", max(0, int(self.input_tokens)))
        if self.input_tokens is None:
            object.__setattr__(self, "input_tokens", max(0, int(self.prompt_tokens)))


@dataclass(frozen=True)
class BillableWebSearchUsage:
    provider: str
    backend: str
    operations: int
    fixed_credits: int
    provider_cost_usd: float = 0.0
    usage_estimated: bool = False
    pricing_snapshot: Mapping[str, object] | None = None


class _CreditTransactionItem(TypedDict):
    item_type: str
    provider: str | None
    model: str | None
    input_tokens: int
    normal_input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    output_tokens: int
    input_credits: int
    normal_input_credits: int
    cached_input_credits: int
    cache_write_credits: int
    output_credits: int
    fixed_credits: int
    total_credits: int
    uncached_equivalent_credits: int
    cache_savings_credits: int
    provider_cost_usd: float | Decimal
    usage_estimated: bool
    pricing_version: str
    metadata: dict[str, object]


class _BillingClassResolver(Protocol):
    def model_billing_class(self, provider: str, model: str) -> str | None: ...


@lru_cache(maxsize=1)
def _default_model_registry() -> ModelRegistry:
    try:
        return ModelRegistry.from_yaml()
    except Exception as exc:
        raise BillingConfigurationError("The model credit registry could not be loaded") from exc


def resolve_model_target(
    *,
    provider: str,
    model: str,
    orchestrator: _BillingClassResolver | object | None = None,
) -> ModelTargetIntent:
    """Resolve an enabled model to its server-owned access category."""
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip()
    candidate = _default_model_registry().find_model(normalized_provider, normalized_model)
    if not candidate or not candidate.enabled:
        raise InvalidModelSelectionError(
            provider=normalized_provider or "unknown",
            model=normalized_model or "unknown",
        )

    resolver = getattr(orchestrator, "model_billing_class", None)
    billing_class: object = (
        resolver(normalized_provider, normalized_model)
        if callable(resolver)
        else candidate.billing_class
    )
    normalized_class = _normalized_billing_class(billing_class)
    if normalized_class not in ALLOWED_MODEL_BILLING_CLASSES:
        raise BillingConfigurationError("A model is missing a valid subscription access category")
    if normalized_class != candidate.billing_class.value:
        raise BillingConfigurationError("Model access metadata does not match the credit registry")
    return ModelTargetIntent(
        provider=normalized_provider,
        model=normalized_model,
        billing_class=normalized_class,
    )


def _normalized_billing_class(value: object) -> str:
    normalized = getattr(value, "value", value)
    return str(normalized or "").strip().lower()


def _candidate_for_target(target: ModelTargetIntent) -> ModelCandidate:
    candidate = _default_model_registry().find_model(target.provider, target.model)
    if (
        candidate is None
        or not candidate.enabled
        or candidate.billing_class.value != _normalized_billing_class(target.billing_class)
    ):
        raise BillingConfigurationError("A billable model target is unavailable or misclassified")
    return candidate


def _allowance_denial(
    error: UsageAllowanceExceededError,
    effective: EffectiveSubscription,
) -> EntitlementDenial:
    recommended = next(
        (
            plan.code
            for plan in get_plan_catalog().list_plans()
            if plan.rank > effective.plan.rank and plan.allowances.ai_credits > error.limit
        ),
        None,
    )
    return EntitlementDenial(
        code="insufficient_credits",
        message=(
            f"This request is estimated to require {error.requested:,} credits. "
            f"You have {error.remaining:,} remaining."
        ),
        meter="ai_credits",
        current_plan=error.current_plan,
        recommended_plan=recommended,
        used=error.used,
        limit=error.limit,
        required=error.requested,
        remaining=error.remaining,
        reset_at=error.reset_at,
    )


def authorize_and_reserve_usage(
    db_session: Session,
    *,
    user_id: UUID,
    request_id: str,
    operation_type: str,
    model_targets: Sequence[ModelTargetIntent],
    research_enabled: bool,
    web_search_reservation_credits: int | None = None,
    smart_routing: bool = False,
    optimization_enabled: bool = False,
    attachment_count: int = 0,
    total_attachment_bytes: int = 0,
    attachment_sizes: Sequence[int] = (),
    input_text: str = "",
    initial_query: str | None = None,
    credit_activity_id: str | None = None,
    max_output_tokens: int | None = None,
    max_output_tokens_by_target: Mapping[str, int] | None = None,
    model_attempt_count: int = 1,
) -> ReservedRequestUsage:
    """Resolve access, estimate the maximum likely charge, and reserve it atomically."""
    effective = resolve_effective_subscription(db_session, user_id)
    normalized_targets = tuple(
        ModelTargetIntent(
            provider=str(target.provider or "").strip().lower(),
            model=str(target.model or "").strip(),
            billing_class=_normalized_billing_class(target.billing_class),
        )
        for target in model_targets
    )
    estimated_text = str(input_text or "") + (
        "x" * (_ATTACHMENT_ESTIMATE_CHARS * max(0, attachment_count))
    )
    if isinstance(web_search_reservation_credits, bool) or (
        web_search_reservation_credits is not None and web_search_reservation_credits < 0
    ):
        raise ValueError("web_search_reservation_credits must be a nonnegative integer or None")
    fixed_search_reservation = (
        int(web_search_reservation_credits)
        if web_search_reservation_credits is not None
        else (ADVANCED_WEB_SEARCH_CREDITS if research_enabled else 0)
    )
    if research_enabled:
        estimated_text += "x" * _RESEARCH_CONTEXT_ESTIMATE_CHARS
    if isinstance(model_attempt_count, bool) or model_attempt_count < 1:
        raise ValueError("model_attempt_count must be a positive integer")
    estimates: list[ReservedModelEstimate] = []
    smart_candidate_estimates: list[SmartCandidateEstimate] = []
    allowances = load_allowance_usage(db_session, effective)

    def estimate_with_affordable_output(
        candidate: ModelCandidate,
        *,
        available_credits: int,
        attempt_count: int = 1,
    ):
        requested_limit = target_output_limit(candidate)
        estimate = estimate_model_credits(
            candidate,
            input_text=estimated_text,
            max_output_tokens=requested_limit,
        )
        if (
            not credit_aware_generation_budget_enabled()
            or estimate.charge.total_credits * attempt_count <= max(0, available_credits)
        ):
            return estimate

        input_only = estimate_model_credits(
            candidate,
            input_text=estimated_text,
            max_output_tokens=1,
        )
        input_credits = max(
            0, input_only.charge.total_credits - input_only.charge.output_credits
        )
        # Preserve a small reconciliation margin so rounding and usage-report
        # variance cannot consume the final available credit.
        per_attempt_available = floor(
            (max(0, available_credits) // max(1, attempt_count)) * 0.95
        )
        output_credit_budget = max(0, per_attempt_available - input_credits)
        affordable_output = floor(
            output_credit_budget / float(candidate.output_credit_multiplier)
        )
        requested_output = estimate.output_tokens
        clamped_output = min(requested_output, max(1, affordable_output))
        return estimate_model_credits(
            candidate,
            input_text=estimated_text,
            max_output_tokens=clamped_output,
        )

    def target_output_limit(candidate: ModelCandidate) -> int | None:
        if max_output_tokens_by_target:
            value = max_output_tokens_by_target.get(
                f"{candidate.provider}:{candidate.model_name}"
            )
            if value is not None:
                return int(value)
        return max_output_tokens

    candidates: tuple[ModelCandidate, ...]
    if smart_routing:
        if not normalized_targets:
            raise BillingConfigurationError("Smart routing produced no candidate plan")
        remaining = allowances["ai_credits"].remaining
        plan_candidates: list[tuple[ModelCandidate, int, ReservedModelEstimate]] = []
        for target in normalized_targets:
            candidate = _candidate_for_target(target)
            if (
                candidate.billing_class.value
                not in effective.plan.entitlements.allowed_billing_classes
            ):
                continue
            estimate = estimate_with_affordable_output(
                candidate,
                available_credits=max(
                    0,
                    remaining
                    - fixed_search_reservation,
                ),
            )
            reservation_credits = estimate.charge.total_credits + (
                fixed_search_reservation
            )
            model_estimate = ReservedModelEstimate(
                provider=candidate.provider,
                model=candidate.model_name,
                input_tokens=estimate.input_tokens,
                output_tokens=estimate.output_tokens,
                input_multiplier=candidate.input_credit_multiplier,
                output_multiplier=candidate.output_credit_multiplier,
                pricing_version=candidate.credit_pricing_version,
                total_credits=estimate.charge.total_credits,
            )
            plan_candidates.append((candidate, reservation_credits, model_estimate))
            if reservation_credits <= remaining:
                smart_candidate_estimates.append(
                    SmartCandidateEstimate(
                        provider=candidate.provider,
                        model=candidate.model_name,
                        billing_class=candidate.billing_class.value,
                        reservation_credits=reservation_credits,
                        expected_input_credits=estimate.charge.input_credits,
                        expected_output_credits=estimate.charge.output_credits,
                        expected_research_credits=(
                            fixed_search_reservation
                        ),
                        expected_total_credits=reservation_credits,
                    )
                )
        if not plan_candidates:
            raise BillingConfigurationError("The plan has no eligible Smart routing models")
        if not smart_candidate_estimates:
            required = min(item[1] for item in plan_candidates)
            recommended = next(
                (
                    plan.code
                    for plan in get_plan_catalog().list_plans()
                    if plan.rank > effective.plan.rank
                    and plan.allowances.ai_credits > effective.plan.allowances.ai_credits
                ),
                None,
            )
            raise EntitlementDeniedError(
                EntitlementDenial(
                    code="insufficient_credits",
                    message=(
                        f"The least expensive appropriate Smart candidate requires "
                        f"{required:,} credits. You have {remaining:,} remaining."
                    ),
                    meter="ai_credits",
                    current_plan=effective.plan.code,
                    recommended_plan=recommended,
                    used=allowances["ai_credits"].used,
                    limit=allowances["ai_credits"].limit,
                    required=required,
                    remaining=remaining,
                    reset_at=effective.current_period_end,
                )
            )
        selected_authorization = smart_candidate_estimates[0]
        selected = next(
            item
            for item in plan_candidates
            if item[0].provider == selected_authorization.provider
            and item[0].model_name == selected_authorization.model
        )
        candidates = (selected[0],)
        estimates.append(selected[2])
        estimated_credits = selected[1]
    else:
        candidates = tuple(_candidate_for_target(target) for target in normalized_targets)
        if not candidates:
            raise BillingConfigurationError("A credit reservation requires at least one model")
        estimated_credits = fixed_search_reservation
        remaining_for_models = max(0, allowances["ai_credits"].remaining - estimated_credits)
        for candidate_index, candidate in enumerate(candidates):
            remaining_candidates = max(1, len(candidates) - candidate_index)
            estimate = estimate_with_affordable_output(
                candidate,
                available_credits=remaining_for_models // remaining_candidates,
                attempt_count=model_attempt_count,
            )
            candidate_total = estimate.charge.total_credits * model_attempt_count
            estimated_credits += candidate_total
            remaining_for_models = max(0, remaining_for_models - candidate_total)
            for _attempt in range(model_attempt_count):
                estimates.append(
                    ReservedModelEstimate(
                        provider=candidate.provider,
                        model=candidate.model_name,
                        input_tokens=estimate.input_tokens,
                        output_tokens=estimate.output_tokens,
                        input_multiplier=candidate.input_credit_multiplier,
                        output_multiplier=candidate.output_credit_multiplier,
                        pricing_version=candidate.credit_pricing_version,
                        total_credits=estimate.charge.total_credits,
                    )
                )

    intent_targets = (
        (
            ModelTargetIntent(
                provider=candidates[0].provider,
                model=candidates[0].model_name,
                billing_class=candidates[0].billing_class.value,
            ),
        )
        if smart_routing
        else normalized_targets
    )
    intent = SubscriptionRequestIntent(
        operation_type=operation_type,
        model_targets=intent_targets,
        research_enabled=research_enabled,
        optimization_enabled=optimization_enabled,
        attachment_count=attachment_count,
        total_attachment_bytes=total_attachment_bytes,
        attachment_sizes=tuple(attachment_sizes),
        estimated_credits=estimated_credits,
    )
    decision = evaluate_entitlement(effective, intent, allowances)
    if not decision.allowed:
        if decision.denial is None:
            raise BillingConfigurationError("Subscription access was denied without a reason")
        raise EntitlementDeniedError(decision.denial)

    try:
        reservation = reserve_usage(
            db_session,
            effective_subscription=effective,
            request_id=request_id,
            operation_type=operation_type,
            requested_quantities={"ai_credits": estimated_credits},
        )
    except UsageAllowanceExceededError as exc:
        raise EntitlementDeniedError(_allowance_denial(exc, effective)) from exc

    return ReservedRequestUsage(
        reservation_id=reservation.id,
        request_id=request_id,
        operation_type=operation_type,
        requested_quantities=dict(reservation.requested_quantities),
        allowed_billing_classes=effective.plan.entitlements.allowed_billing_classes,
        current_plan=effective.plan.code,
        reset_at=effective.current_period_end,
        input_text=str(input_text or ""),
        initial_query=str(initial_query or "").strip() or None,
        credit_activity_id=str(credit_activity_id or "").strip() or None,
        model_estimates=tuple(estimates),
        smart_candidates=tuple(smart_candidate_estimates),
    )


def _usage_charge(
    usage: BillableModelUsage, reservation: ReservedRequestUsage
) -> tuple[CreditCharge, CreditCharge, ModelCandidate]:
    candidate = _default_model_registry().find_model(usage.provider, usage.model)
    if candidate is None or not candidate.enabled:
        raise BillingConfigurationError("Successful model usage is absent from the credit registry")
    input_tokens = max(0, int(usage.prompt_tokens))
    output_tokens = max(0, int(usage.output_tokens))
    estimated = bool(usage.usage_estimated)
    if input_tokens == 0 and output_tokens == 0:
        input_tokens, output_tokens = fallback_actual_tokens(
            input_text=reservation.input_text,
            output_text=usage.output_text,
        )
        estimated = True
    legacy_charge = calculate_credit_charge(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_multiplier=candidate.input_credit_multiplier,
        output_multiplier=candidate.output_credit_multiplier,
        estimated=estimated,
        pricing_snapshot=usage.pricing_snapshot,
    )
    cache_aware_charge = calculate_model_credit_charge(
        prompt_tokens=input_tokens,
        cached_input_tokens=max(0, int(usage.cached_input_tokens)),
        cache_write_tokens=max(0, int(usage.cache_write_tokens)),
        output_tokens=output_tokens,
        input_credit_multiplier=candidate.input_credit_multiplier,
        output_credit_multiplier=candidate.output_credit_multiplier,
        pricing_snapshot=usage.pricing_snapshot,
        estimated=estimated,
    )
    calculation_enabled = cache_aware_credit_calculation_enabled()
    settlement_enabled = calculation_enabled and cache_aware_credit_settlement_enabled()
    authoritative = cache_aware_charge if settlement_enabled else legacy_charge
    return authoritative, cache_aware_charge, candidate


def _estimated_usage_for_target(
    target: ModelTargetIntent,
    reservation: ReservedRequestUsage,
) -> BillableModelUsage:
    estimate = next(
        (
            item
            for item in reservation.model_estimates
            if item.provider == target.provider and item.model == target.model
        ),
        None,
    )
    if estimate is None and len(reservation.model_estimates) == 1:
        estimate = reservation.model_estimates[0]
    if estimate is None:
        raise BillingConfigurationError("Successful usage cannot be matched to its reservation")
    return BillableModelUsage(
        provider=target.provider,
        model=target.model,
        prompt_tokens=estimate.input_tokens,
        output_tokens=estimate.output_tokens,
        usage_estimated=True,
    )


def finalize_reserved_usage(
    db_session: Session,
    *,
    reservation: ReservedRequestUsage,
    successful_targets: Sequence[ModelTargetIntent] = (),
    model_usages: Sequence[BillableModelUsage] = (),
    research_provider_credits_used: int,
    research_usage_estimated: bool = False,
    web_search_usages: Sequence[BillableWebSearchUsage] = (),
    optimization_performed: bool = False,
    file_analysis_performed: bool = False,
    uploaded_bytes: int = 0,
    release_reason: str = "provider_failed_before_billable_output",
) -> None:
    """Settle actual model/search credits and persist itemized reconciliation rows."""
    del uploaded_bytes
    usages = tuple(model_usages) or tuple(
        _estimated_usage_for_target(target, reservation) for target in successful_targets
    )
    transaction_items: list[_CreditTransactionItem] = []
    total_credits = 0
    for usage in usages:
        charge, cache_aware_charge, candidate = _usage_charge(usage, reservation)
        total_credits += charge.total_credits
        settlement_enabled = (
            cache_aware_credit_calculation_enabled()
            and cache_aware_credit_settlement_enabled()
        )
        if settlement_enabled:
            normal_input_credits = charge.normal_input_credits
            cached_input_credits = charge.cached_input_credits
            cache_write_credits = charge.cache_write_credits
        else:
            normal_input_credits = charge.input_credits
            cached_input_credits = 0
            cache_write_credits = 0
        pricing_snapshot = (
            dict(usage.pricing_snapshot) if isinstance(usage.pricing_snapshot, Mapping) else {}
        )
        cache_hit_ratio = (
            cache_aware_charge.cached_input_tokens / cache_aware_charge.prompt_tokens
            if cache_aware_charge.prompt_tokens
            else 0.0
        )
        legacy_total = calculate_credit_charge(
            input_tokens=cache_aware_charge.prompt_tokens,
            output_tokens=cache_aware_charge.output_tokens,
            input_multiplier=candidate.input_credit_multiplier,
            output_multiplier=candidate.output_credit_multiplier,
            estimated=cache_aware_charge.estimated,
            pricing_snapshot=pricing_snapshot,
        ).total_credits
        effective_input_multiplier, effective_output_multiplier = floor_credit_multipliers(
            input_credit_multiplier=candidate.input_credit_multiplier,
            output_credit_multiplier=candidate.output_credit_multiplier,
            pricing_snapshot=pricing_snapshot,
        )
        reservation_estimate = next(
            (
                item
                for item in reservation.model_estimates
                if item.provider == candidate.provider
                and item.model == candidate.model_name
            ),
            None,
        )
        transaction_items.append(
            {
                "item_type": "model",
                "provider": candidate.provider,
                "model": candidate.model_name,
                "input_tokens": cache_aware_charge.prompt_tokens,
                "normal_input_tokens": cache_aware_charge.normal_input_tokens,
                "cached_input_tokens": cache_aware_charge.cached_input_tokens,
                "cache_write_tokens": cache_aware_charge.cache_write_tokens,
                "reasoning_tokens": max(0, int(usage.reasoning_tokens)),
                "output_tokens": charge.output_tokens,
                "input_credits": charge.input_credits,
                "normal_input_credits": normal_input_credits,
                "cached_input_credits": cached_input_credits,
                "cache_write_credits": cache_write_credits,
                "output_credits": charge.output_credits,
                "fixed_credits": 0,
                "total_credits": charge.total_credits,
                "uncached_equivalent_credits": cache_aware_charge.uncached_equivalent_credits,
                "cache_savings_credits": cache_aware_charge.cache_savings_credits,
                "provider_cost_usd": Decimal(
                    str(
                        pricing_snapshot.get(
                            "usage_calculated_provider_cost_usd",
                            max(0.0, float(usage.provider_cost_usd)),
                        )
                    )
                ),
                "usage_estimated": charge.estimated,
                "pricing_version": usage.pricing_version or candidate.credit_pricing_version,
                "metadata": {
                    "file_context": bool(file_analysis_performed),
                    "prompt_optimization": bool(optimization_performed),
                    "credit_policy_version": CACHE_AWARE_CREDIT_POLICY_VERSION,
                    "credit_pricing_version": candidate.credit_pricing_version,
                    "input_credit_multiplier": candidate.input_credit_multiplier,
                    "output_credit_multiplier": candidate.output_credit_multiplier,
                    "effective_input_credit_multiplier": str(effective_input_multiplier),
                    "effective_output_credit_multiplier": str(effective_output_multiplier),
                    "credit_rate_floor_applied": (
                        effective_input_multiplier
                        > Decimal(str(candidate.input_credit_multiplier))
                        or effective_output_multiplier
                        > Decimal(str(candidate.output_credit_multiplier))
                    ),
                    "credit_max_provider_usd_per_million_credits": str(
                        max_provider_usd_per_million_credits()
                    ),
                    "provider_pricing_version": (
                        usage.pricing_version
                        or pricing_snapshot.get("pricing_version")
                        or candidate.credit_pricing_version
                    ),
                    "pricing_rule_applied": pricing_snapshot.get("pricing_rule_id")
                    or pricing_snapshot.get("rule_id"),
                    "pricing_snapshot": pricing_snapshot,
                    "reserved_output_tokens": (
                        int(reservation_estimate.output_tokens)
                        if reservation_estimate is not None
                        else 0
                    ),
                    "cache_discount_source": (
                        "provider_pricing_snapshot" if pricing_snapshot else "full_input_fallback"
                    ),
                    "cache_hit": cache_aware_charge.cached_input_tokens > 0,
                    "cache_hit_ratio": cache_hit_ratio,
                    "cache_aware_shadow_total": cache_aware_charge.total_credits,
                    "legacy_total": legacy_total,
                    "cache_aware_delta": cache_aware_charge.total_credits - legacy_total,
                    "cache_aware_settlement_enabled": settlement_enabled,
                    "provider_cost_owner": (
                        "customer"
                        if str(usage.provider_cost_owner).lower() == "customer"
                        else "cortex"
                    ),
                },
            }
        )
        logger.info(
            "Cache-aware billing calculation completed",
            extra={
                "extra_fields": {
                    "event": (
                        "billing.cache_aware.settled"
                        if settlement_enabled
                        else "billing.cache_aware.shadow"
                    ),
                    "request_id": reservation.request_id,
                    "operation_type": reservation.operation_type,
                    "provider": candidate.provider,
                    "model": candidate.model_name,
                    "prompt_tokens": cache_aware_charge.prompt_tokens,
                    "cached_tokens": cache_aware_charge.cached_input_tokens,
                    "cache_write_tokens": cache_aware_charge.cache_write_tokens,
                    "output_tokens": cache_aware_charge.output_tokens,
                    "reasoning_tokens": max(0, int(usage.reasoning_tokens)),
                    "credits": charge.total_credits,
                    "pricing_version": usage.pricing_version or candidate.credit_pricing_version,
                    "cache_policy_version": CACHE_AWARE_CREDIT_POLICY_VERSION,
                }
            },
        )
    for web_usage in web_search_usages:
        operations = max(0, int(web_usage.operations or 0))
        fixed_credits = max(0, int(web_usage.fixed_credits or 0))
        if operations <= 0 or fixed_credits <= 0:
            continue
        total_credits += fixed_credits
        transaction_items.append(
            {
                "item_type": "tool",
                "provider": str(web_usage.provider or "").strip().lower() or None,
                "model": None,
                "input_tokens": 0,
                "normal_input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "output_tokens": 0,
                "input_credits": 0,
                "normal_input_credits": 0,
                "cached_input_credits": 0,
                "cache_write_credits": 0,
                "output_credits": 0,
                "fixed_credits": fixed_credits,
                "total_credits": fixed_credits,
                "uncached_equivalent_credits": fixed_credits,
                "cache_savings_credits": 0,
                "provider_cost_usd": Decimal(
                    str(
                        (web_usage.pricing_snapshot or {}).get(
                            "usage_calculated_provider_cost_usd",
                            max(0.0, float(web_usage.provider_cost_usd or 0.0)),
                        )
                    )
                ),
                "usage_estimated": bool(web_usage.usage_estimated),
                "pricing_version": "web-search-2026-09-27",
                "metadata": {
                    "tool_kind": "web_search",
                    **(
                        {"pricing_snapshot": dict(web_usage.pricing_snapshot)}
                        if web_usage.pricing_snapshot
                        else {}
                    ),
                    "backend": str(web_usage.backend or "").strip().lower(),
                    "operations": operations,
                },
            }
        )

    research_credits = calculate_research_credit_charge(research_provider_credits_used)
    if research_credits:
        total_credits += research_credits
        transaction_items.append(
            {
                "item_type": "research",
                "provider": "tavily",
                "model": None,
                "input_tokens": 0,
                "normal_input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "output_tokens": 0,
                "input_credits": 0,
                "normal_input_credits": 0,
                "cached_input_credits": 0,
                "cache_write_credits": 0,
                "output_credits": 0,
                "fixed_credits": research_credits,
                "total_credits": research_credits,
                "uncached_equivalent_credits": research_credits,
                "cache_savings_credits": 0,
                "provider_cost_usd": 0.0,
                "usage_estimated": bool(research_usage_estimated),
                "pricing_version": "research-2026-07-31",
                "metadata": {
                    "provider_credits_used": research_provider_credits_used,
                    "cortex_credits_per_provider_credit": CORTEX_CREDITS_PER_TAVILY_CREDIT,
                },
            }
        )
    if total_credits == 0:
        release_usage(db_session, reservation_id=reservation.reservation_id, reason=release_reason)
        return

    plan = get_plan_catalog().require(reservation.current_plan)
    settlement = settle_usage_with_supplement(
        db_session,
        reservation_id=reservation.reservation_id,
        actual_quantity=total_credits,
        allowance_limit=plan.allowances.ai_credits,
    )
    settled = settlement.reservation
    transaction_items = _reconcile_transaction_items(
        transaction_items,
        billed_credits=settlement.billed_quantity,
        actual_credits=total_credits,
    )
    for index, item in enumerate(transaction_items):
        metadata = dict(item.get("metadata") or {})
        if reservation.initial_query:
            metadata["initial_query"] = reservation.initial_query
        if reservation.credit_activity_id:
            metadata["credit_activity_id"] = reservation.credit_activity_id
        item["metadata"] = metadata
        repository.create_credit_transaction(
            db_session,
            billing_account_id=settled.billing_account_id,
            usage_period_id=settled.usage_period_id,
            reservation_id=settled.id,
            request_id=settled.request_id,
            operation_type=settled.operation_type,
            item_index=index,
            **item,
        )


def _reconcile_transaction_items(
    items: Sequence[_CreditTransactionItem],
    *,
    billed_credits: int,
    actual_credits: int,
) -> list[_CreditTransactionItem]:
    """Keep ledger totals equal to settled credits while retaining overrun evidence."""

    if billed_credits >= actual_credits:
        return [item.copy() for item in items]

    remaining = max(0, billed_credits)
    reconciled: list[_CreditTransactionItem] = []
    unbilled_provider_cost = Decimal(0)
    # Fixed search/tool charges are prioritized because they are included in preflight.
    ordered = sorted(
        items,
        key=lambda item: 0 if item.get("item_type") in {"research", "tool"} else 1,
    )
    for original in ordered:
        item = original.copy()
        item_actual = max(0, int(item.get("total_credits") or 0))
        item_billed = min(item_actual, remaining)
        remaining -= item_billed
        original_cost = max(Decimal(0), Decimal(str(item.get("provider_cost_usd") or 0)))
        billed_ratio = (Decimal(item_billed) / Decimal(item_actual)) if item_actual else Decimal(0)
        billed_cost = original_cost * billed_ratio
        unbilled_provider_cost += max(Decimal(0), original_cost - billed_cost)

        metadata = dict(item.get("metadata") or {})
        metadata.update(
            {
                "under_reserved": True,
                "calculated_total_credits": item_actual,
                "billed_total_credits": item_billed,
            }
        )
        item["metadata"] = metadata
        item["provider_cost_usd"] = billed_cost
        item["total_credits"] = item_billed
        if item.get("item_type") in {"research", "tool"}:
            item["fixed_credits"] = item_billed
            item["input_credits"] = 0
            item["output_credits"] = 0
        else:
            input_actual = max(0, int(item.get("input_credits") or 0))
            billed_input = min(input_actual, item_billed)
            item["input_credits"] = billed_input
            item["normal_input_credits"] = billed_input
            item["cached_input_credits"] = 0
            item["cache_write_credits"] = 0
            item["output_credits"] = max(0, item_billed - billed_input)
            item["fixed_credits"] = 0
        reconciled.append(item)

    reconciled.append(
        {
            "item_type": "adjustment",
            "provider": None,
            "model": None,
            "input_tokens": 0,
            "normal_input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "output_tokens": 0,
            "input_credits": 0,
            "normal_input_credits": 0,
            "cached_input_credits": 0,
            "cache_write_credits": 0,
            "output_credits": 0,
            "fixed_credits": 0,
            "total_credits": 0,
            "uncached_equivalent_credits": 0,
            "cache_savings_credits": 0,
            "provider_cost_usd": unbilled_provider_cost,
            "usage_estimated": False,
            "pricing_version": "reconciliation-2026-07-30",
            "metadata": {
                "under_reserved": True,
                "calculated_credits": actual_credits,
                "billed_credits": billed_credits,
                "unbilled_credits": max(0, actual_credits - billed_credits),
                "unbilled_provider_cost_usd": float(unbilled_provider_cost),
                "unbilled_provider_cost_usd_exact": str(unbilled_provider_cost),
            },
        }
    )
    return reconciled


def release_reserved_usage(
    db_session: Session,
    *,
    reservation: ReservedRequestUsage,
    reason: str,
) -> None:
    release_usage(db_session, reservation_id=reservation.reservation_id, reason=reason)
