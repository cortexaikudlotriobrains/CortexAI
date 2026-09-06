"""Pydantic response models (DTOs) for FastAPI endpoints."""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Literal
from server.billing.credit_calculator import research_credit_usage_from_metadata
from server.billing.response_credit_service import resolve_response_credit_usage


class TokenUsageDTO(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


class ErrorDTO(BaseModel):
    code: str
    message: str
    provider: str
    retryable: bool
    details: Dict[str, Any] = Field(default_factory=dict)


class GenerationBudgetDTO(BaseModel):
    profile: str
    requested_max_output_tokens: int
    effective_max_output_tokens: int
    requested_reasoning_mode: str
    effective_reasoning_mode: str
    requested_reasoning_effort: str
    effective_reasoning_effort: str
    reasoning_disable_supported: bool = True
    reasoning_counts_against_output: bool = True
    policy_version: str


class RetryWithMoreRoomDTO(BaseModel):
    available: bool = False
    recommended_profile: Optional[str] = None


class GenerationEstimateTargetDTO(BaseModel):
    provider: str
    model: str
    profile: str
    effective_max_output_tokens: int
    estimated_max_ai_credits: int


class GenerationEstimateResponseDTO(BaseModel):
    targets: List[GenerationEstimateTargetDTO]
    estimated_max_ai_credits: int
    remaining_ai_credits: int
    can_authorize: bool
    temporary_hold_released_after_settlement: bool = True


class ChatResponseDTO(BaseModel):
    request_id: str
    response_version: int = 1
    session_id: Optional[str] = None
    text: str
    provider: str
    model: str
    requested_model: Optional[str] = None
    served_model: Optional[str] = None
    pricing_model: Optional[str] = None
    model_lifecycle_status: str = "UNKNOWN"
    alias_redirected: bool = False
    replacement_model: Optional[str] = None
    migration_reason: Optional[str] = None
    reasoning_mode: Optional[str] = None
    latency_ms: int
    token_usage: TokenUsageDTO
    estimated_cost: float
    cost_currency: str = "USD"
    pricing_version: Optional[str] = None
    pricing_rule_applied: Optional[str] = None
    pricing_unknown: bool = False
    pricing_snapshot: Dict[str, Any] = Field(default_factory=dict)
    ai_credits: int = 0
    credit_usage_estimated: bool = False
    cache_hit: bool = False
    cache_hit_ratio: float = 0.0
    cache_savings_ai_credits: int = 0
    uncached_equivalent_ai_credits: int = 0
    finish_reason: Optional[str] = None
    completion_status: Literal["complete", "incomplete", "failed"] = "complete"
    stop_cause: str = "unknown"
    generation_budget: Optional[GenerationBudgetDTO] = None
    retry_with_more_room: RetryWithMoreRoomDTO = Field(default_factory=RetryWithMoreRoomDTO)
    error: Optional[ErrorDTO] = None
    web_source_items: List[Dict[str, str]] = Field(default_factory=list)
    timestamp: str

    @classmethod
    def from_unified_response(
        cls,
        ur,
        *,
        session_id: Optional[str] = None,
        response_version: int = 1,
        include_research_charge: bool = True,
    ):
        """Convert UnifiedResponse to DTO."""
        metadata = ur.metadata if isinstance(getattr(ur, "metadata", None), dict) else {}
        source_candidates = metadata.get("web_source_items")
        if not isinstance(source_candidates, list):
            source_candidates = metadata.get("sources")

        normalized_sources: List[Dict[str, str]] = []
        seen_urls: set[str] = set()
        if isinstance(source_candidates, list):
            for item in source_candidates:
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
                normalized_sources.append({"title": title, "url": url})
                if len(normalized_sources) >= 8:
                    break

        credit_usage = resolve_response_credit_usage(
            ur,
            include_research_charge=include_research_charge,
        )
        generation_budget_raw = metadata.get("generation_budget")
        generation_budget = (
            GenerationBudgetDTO(**generation_budget_raw)
            if isinstance(generation_budget_raw, dict)
            else None
        )
        completion_status = str(metadata.get("completion_status") or "")
        stop_cause = str(metadata.get("stop_cause") or "")
        if completion_status not in {"complete", "incomplete", "failed"}:
            if ur.error:
                completion_status = "failed"
                stop_cause = "error"
            elif ur.finish_reason == "length":
                completion_status = "incomplete"
                stop_cause = "token_limit"
            else:
                completion_status = "complete"
                stop_cause = stop_cause or "unknown"
        retry_profile = (
            str(generation_budget_raw.get("retry_profile") or "").strip() or None
            if isinstance(generation_budget_raw, dict)
            else None
        )
        return cls(
            request_id=ur.request_id,
            response_version=max(1, int(response_version)),
            session_id=session_id,
            text=ur.text,
            provider=ur.provider,
            model=ur.model,
            requested_model=getattr(ur, "requested_model", None) or ur.model,
            served_model=getattr(ur, "served_model", None) or ur.model,
            pricing_model=getattr(ur, "pricing_model", None) or ur.model,
            model_lifecycle_status=str(
                getattr(ur, "model_lifecycle_status", "UNKNOWN") or "UNKNOWN"
            ),
            alias_redirected=bool(getattr(ur, "alias_redirected", False)),
            replacement_model=getattr(ur, "replacement_model", None),
            migration_reason=getattr(ur, "migration_reason", None),
            reasoning_mode=getattr(ur, "reasoning_mode", None),
            latency_ms=ur.latency_ms,
            token_usage=TokenUsageDTO(
                prompt_tokens=ur.token_usage.prompt_tokens,
                completion_tokens=ur.token_usage.completion_tokens,
                total_tokens=ur.token_usage.total_tokens,
                cached_input_tokens=getattr(ur.token_usage, "cached_input_tokens", 0),
                cache_write_tokens=getattr(ur.token_usage, "cache_write_tokens", 0),
                reasoning_tokens=getattr(ur.token_usage, "reasoning_tokens", 0),
            ),
            estimated_cost=ur.estimated_cost,
            cost_currency=ur.cost_currency,
            pricing_version=getattr(ur, "pricing_version", None),
            pricing_rule_applied=getattr(ur, "pricing_rule_applied", None),
            pricing_unknown=bool(getattr(ur, "pricing_unknown", False)),
            pricing_snapshot=dict(getattr(ur, "pricing_snapshot", {}) or {}),
            ai_credits=credit_usage.ai_credits,
            credit_usage_estimated=credit_usage.credit_usage_estimated,
            cache_hit=credit_usage.cache_hit,
            cache_hit_ratio=credit_usage.cache_hit_ratio,
            cache_savings_ai_credits=credit_usage.cache_savings_ai_credits,
            uncached_equivalent_ai_credits=credit_usage.uncached_equivalent_ai_credits,
            finish_reason=ur.finish_reason,
            completion_status=completion_status,
            stop_cause=stop_cause or "unknown",
            generation_budget=generation_budget,
            retry_with_more_room=RetryWithMoreRoomDTO(
                available=completion_status == "incomplete" and bool(retry_profile),
                recommended_profile=retry_profile,
            ),
            error=(
                ErrorDTO(
                    code=ur.error.code,
                    message=ur.error.message,
                    provider=ur.error.provider,
                    retryable=ur.error.retryable,
                    details=ur.error.details,
                )
                if ur.error
                else None
            ),
            web_source_items=normalized_sources,
            timestamp=ur.timestamp,
        )


class CompareResponseDTO(BaseModel):
    request_group_id: str
    session_id: Optional[str] = None
    responses: List[ChatResponseDTO]
    success_count: int
    error_count: int
    total_tokens: int
    total_cost: float
    total_ai_credits: int = 0
    total_cache_savings_ai_credits: int = 0
    timestamp: str

    @classmethod
    def from_multi_unified_response(cls, mur, *, session_id: Optional[str] = None):
        """Convert MultiUnifiedResponse to DTO."""
        response_dtos = [
            ChatResponseDTO.from_unified_response(
                r,
                session_id=session_id,
                include_research_charge=False,
            )
            for r in mur.responses
        ]
        research_credits = max(
            (
                research_credit_usage_from_metadata(
                    getattr(response, "metadata", None)
                ).cortex_credits
                for response in mur.responses
            ),
            default=0,
        )
        return cls(
            request_group_id=mur.request_group_id,
            session_id=session_id,
            responses=response_dtos,
            success_count=mur.success_count,
            error_count=mur.error_count,
            total_tokens=mur.total_tokens,
            total_cost=mur.total_cost,
            total_ai_credits=(sum(item.ai_credits for item in response_dtos) + research_credits),
            total_cache_savings_ai_credits=sum(
                item.cache_savings_ai_credits for item in response_dtos
            ),
            timestamp=mur.timestamp,
        )


class HealthResponseDTO(BaseModel):
    status: str
    timestamp: str
    version: str = "1.0.0"


class ModelCatalogItemDTO(BaseModel):
    provider: str
    model: str
    tier: str
    billing_class: str
    access_category: str
    input_credit_multiplier: float
    output_credit_multiplier: float
    credit_usage_label: str
    credit_pricing_version: str
    input_cost_per_1m: float
    output_cost_per_1m: float
    cached_input_cost_per_1m: Optional[float] = None
    cache_write_cost_per_1m: Optional[float] = None
    context_limit: int
    max_output_tokens: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    enabled: bool
    selectable: bool = True
    display_name: str = ""
    description: str = ""
    release_status: str = "unknown"
    lifecycle_status: str = "ACTIVE"
    replacement_model: Optional[str] = None
    retirement_date: Optional[str] = None
    migration_reason: Optional[str] = None
    pricing_model: Optional[str] = None
    pricing_rule_id: Optional[str] = None
    pricing_effective_from: Optional[str] = None
    pricing_effective_until: Optional[str] = None
    long_context_threshold_tokens: Optional[int] = None
    aliases: List[str] = Field(default_factory=list)
    reasoning_modes: List[str] = Field(default_factory=list)
    default_reasoning_mode: Optional[str] = None
    reasoning_efforts: List[str] = Field(default_factory=list)
    reasoning_disable_supported: bool = True
    reasoning_counts_against_output: bool = True
    pricing_source_url: Optional[str] = None
    lifecycle_source_url: Optional[str] = None
    source_verified_at: Optional[str] = None
    supports_image_input: bool = False
    supported_attachment_mime_types: List[str] = Field(default_factory=list)
    max_attachment_bytes: Optional[int] = None
    max_attachments_per_request: Optional[int] = None


class ProviderCatalogItemDTO(BaseModel):
    provider: str
    label: str
    api_key_env: str
    default_model_env: str
    default_model: str
    byok_supported: bool
    capabilities: List[str] = Field(default_factory=list)
    ui: Dict[str, Any] = Field(default_factory=dict)
    model_count: int = 0
    enabled_model_count: int = 0


class ProvidersCatalogResponseDTO(BaseModel):
    providers: List[ProviderCatalogItemDTO] = Field(default_factory=list)
    total: int
    timestamp: str


class ModelsCatalogResponseDTO(BaseModel):
    provider: Optional[str] = None
    enabled_only: bool
    models: List[ModelCatalogItemDTO] = Field(default_factory=list)
    total: int
    timestamp: str


class FailedAttemptDTO(BaseModel):
    request_id: str
    request_group_id: str
    attempt_number: int
    tier: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    validation: Optional[str] = None
    latency_ms: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


class FailedAttemptsByGroupDTO(BaseModel):
    request_group_id: str
    count: int
    items: List[FailedAttemptDTO] = Field(default_factory=list)


class UsageTotalsDTO(BaseModel):
    requests: int
    tokens: int
    cost: float


class UsageBucketDTO(BaseModel):
    bucket: str
    requests: int
    tokens: int
    cost: float


class UsageReportDTO(BaseModel):
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    group_by: str
    totals: UsageTotalsDTO
    breakdown: List[UsageBucketDTO] = Field(default_factory=list)


class UsageSummaryPeriodDTO(BaseModel):
    from_: str = Field(alias="from")
    to: str
    label: str

    class Config:
        allow_population_by_field_name = True
        populate_by_name = True


class UsageSummaryModelDTO(BaseModel):
    provider: str
    modelId: str
    displayName: str
    replies: int
    viaSmart: int


class UsageSummarySessionModesDTO(BaseModel):
    askOnly: int
    compareOnly: int
    mixed: int


class UsageSummaryActivityDayDTO(BaseModel):
    date: str
    tokens: int


class UsageSummaryDTO(BaseModel):
    period: UsageSummaryPeriodDTO
    totalTokens: int
    totalRequests: int
    totalSessions: int
    avgLatencyMs: float
    p95LatencyMs: float
    minLatencyMs: float
    avgCostPerRequest: float
    totalSpend: float
    tokensDeltaPct: float
    smartRoutedTotal: int
    models: List[UsageSummaryModelDTO] = Field(default_factory=list)
    sessionModes: UsageSummarySessionModesDTO
    switchedMidSession: int
    activityDaily: List[UsageSummaryActivityDayDTO] = Field(default_factory=list)
    totalAiCredits: int = 0
    averageAiCreditsPerRequest: float = 0.0
    normalInputTokens: int = 0
    cachedInputTokens: int = 0
    cacheWriteTokens: int = 0
    cacheHitRatio: float = 0.0
    cacheSavingsAiCredits: int = 0
    providerCostCacheSavings: float = 0.0
    reservationCredits: int = 0
    settledCredits: int = 0
    reservationReleaseRatio: float = 0.0
    outputTokenUtilization: float = 0.0
    reasoningTokens: int = 0
    researchRequests: int = 0
    researchReuseRate: float = 0.0
    promptOptimizationReuseRate: float = 0.0
    cortexAnalysisReuseRate: float = 0.0


class SavingsTotalsDTO(BaseModel):
    requests: int
    successful_requests: int = 0
    failed_requests: int = 0
    actual_cost: float
    baseline_cost: float
    savings_amount: float
    savings_pct: float


class SavingsBucketDTO(BaseModel):
    bucket: str
    requests: int
    successful_requests: int = 0
    failed_requests: int = 0
    actual_cost: float
    baseline_cost: float
    savings_amount: float
    savings_pct: float


class SavingsReportDTO(BaseModel):
    from_date: Optional[str] = None
    to_date: Optional[str] = None
    group_by: str
    totals: SavingsTotalsDTO
    breakdown: List[SavingsBucketDTO] = Field(default_factory=list)


class ByokProviderStatusDTO(BaseModel):
    provider: str
    configured: bool
    key_last4: Optional[str] = None
    fingerprint_prefix: Optional[str] = None
    updated_at: Optional[str] = None


class ByokStatusDTO(BaseModel):
    providers: List[ByokProviderStatusDTO] = Field(default_factory=list)
    baseline_provider: Optional[str] = None
    baseline_model: Optional[str] = None
    requests_per_minute: Optional[int] = None


class ByokUpdateResponseDTO(BaseModel):
    updated_providers: List[str] = Field(default_factory=list)
    baseline_provider: Optional[str] = None
    baseline_model: Optional[str] = None
    requests_per_minute: Optional[int] = None


class ByokDeleteResponseDTO(BaseModel):
    deleted_count: int


class WhoAmIBaselineDTO(BaseModel):
    provider: str
    model: str
    source: str


class WhoAmIRateLimitConfigDTO(BaseModel):
    requests_per_minute: int
    daily_cap_scope: str
    daily_token_cap: Optional[int] = None
    daily_cost_cap: Optional[float] = None


class WhoAmIBreakerConfigDTO(BaseModel):
    failure_threshold: int
    window_seconds: int
    cooldown_seconds: int
    scope: str


class WhoAmIBillingDTO(BaseModel):
    plan_code: str
    status: str


class WhoAmIResponseDTO(BaseModel):
    api_key_id: Optional[str] = None
    user_id: Optional[str] = None
    plan_tier: Optional[str] = None
    storage_policy: str
    redact_pii: bool
    baseline: WhoAmIBaselineDTO
    rate_limits: WhoAmIRateLimitConfigDTO
    breakers: WhoAmIBreakerConfigDTO
    billing: Optional[WhoAmIBillingDTO] = None


class EntitlementPlanDTO(BaseModel):
    code: str
    display_name: str
    status: str
    source: str
    renews_at: str
    cancel_at_period_end: bool
    grace_until: Optional[str] = None


class EntitlementFeaturesDTO(BaseModel):
    compare_enabled: bool
    max_compare_models: int
    research_enabled: bool
    prompt_improvement_enabled: bool
    file_analysis_enabled: bool
    usage_export_enabled: bool
    saved_history_enabled: bool
    models_catalog_enabled: bool
    work_enabled: bool
    verified_connectors_enabled: bool
    custom_mcp_enabled: bool
    action_tools_enabled: bool


class EntitlementModelAccessDTO(BaseModel):
    allowed_billing_classes: List[str] = Field(default_factory=list)


class EntitlementLimitsDTO(BaseModel):
    max_files_per_request: int
    max_file_bytes: int
    max_active_work_runs: int
    max_tool_connections: int
    max_mcp_servers_per_run: int
    max_work_credit_budget: int


class EntitlementAllowanceDTO(BaseModel):
    used: int
    reserved: int
    limit: int
    remaining: int


class EntitlementPeriodDTO(BaseModel):
    starts_at: str
    ends_at: str


class EntitlementsResponseDTO(BaseModel):
    plan: EntitlementPlanDTO
    features: EntitlementFeaturesDTO
    model_access: EntitlementModelAccessDTO
    limits: EntitlementLimitsDTO
    allowances: Dict[str, EntitlementAllowanceDTO] = Field(default_factory=dict)
    period: EntitlementPeriodDTO


class CreditTransactionDTO(BaseModel):
    id: str
    request_id: str
    activity_id: str
    query: Optional[str] = None
    operation_type: str
    item_type: str
    provider: Optional[str] = None
    model: Optional[str] = None
    input_tokens: int
    normal_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    output_tokens: int
    input_credits: int
    normal_input_credits: int = 0
    cached_input_credits: int = 0
    cache_write_credits: int = 0
    output_credits: int
    fixed_credits: int
    total_credits: int
    uncached_equivalent_credits: int = 0
    cache_savings_credits: int = 0
    provider_cost_usd: float
    usage_estimated: bool
    pricing_version: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str


class CreditTransactionsResponseDTO(BaseModel):
    items: List[CreditTransactionDTO] = Field(default_factory=list)
    limit: int
    offset: int


class PublicBillingPlanFeaturesDTO(BaseModel):
    max_compare_models: int
    research_enabled: bool
    prompt_improvement_enabled: bool
    file_analysis_enabled: bool
    work_enabled: bool
    verified_connectors_enabled: bool
    custom_mcp_enabled: bool
    action_tools_enabled: bool
    max_active_work_runs: int
    allowed_billing_classes: List[str] = Field(default_factory=list)


class PublicBillingPlanAllowancesDTO(BaseModel):
    ai_credits: int


class PublicBillingPlanDTO(BaseModel):
    code: str
    display_name: str
    monthly_price: float
    recommended: bool = False
    features: PublicBillingPlanFeaturesDTO
    allowances: PublicBillingPlanAllowancesDTO


class BillingPlansResponseDTO(BaseModel):
    currency: Literal["USD"] = "USD"
    billing_period: Literal["monthly"] = "monthly"
    billing_enabled: bool
    plans: List[PublicBillingPlanDTO] = Field(default_factory=list)


class BillingSubscriptionResponseDTO(BaseModel):
    plan_code: str
    status: str
    provider: Optional[str] = None
    current_period_start: str
    current_period_end: str
    cancel_at_period_end: bool
    can_manage: bool


class CheckoutSessionResponseDTO(BaseModel):
    checkout_url: str
    destination: Literal["checkout", "portal"] = "checkout"


class PortalSessionResponseDTO(BaseModel):
    portal_url: str


class BillingWebhookResponseDTO(BaseModel):
    received: bool = True


class FileBaseDTO(BaseModel):
    file_id: str
    original_filename: str
    mime_type: str
    size_bytes: int
    status: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    ingestion_meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: Optional[str] = None
    expires_at: Optional[str] = None


class FileUploadResponseDTO(FileBaseDTO):
    deduplicated: bool = False


class FileBatchUploadResponseDTO(BaseModel):
    files: List[FileUploadResponseDTO] = Field(default_factory=list)


class PresignedPostDTO(BaseModel):
    url: str
    fields: Dict[str, str]
    expires_at: str


class FileUploadIntentItemDTO(FileBaseDTO):
    upload: PresignedPostDTO


class FileUploadIntentResponseDTO(BaseModel):
    files: List[FileUploadIntentItemDTO] = Field(default_factory=list)


class FileStatusResponseDTO(FileBaseDTO):
    deduplicated: bool = False
