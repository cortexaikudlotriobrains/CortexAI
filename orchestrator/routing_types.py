from dataclasses import dataclass, field
from enum import Enum

from server.billing.models import ModelBillingClass


class Tier(str, Enum):
    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


@dataclass(frozen=True)
class PromptFeatures:
    word_count: int
    char_count: int
    token_estimate: int
    has_code: bool
    has_math: bool
    has_analysis: bool
    has_creative: bool
    has_factual: bool
    strict_format: bool
    has_logs_stacktrace: bool
    context_token_estimate: int
    context_messages: int
    is_follow_up: bool
    needs_latest_info: bool
    needs_accuracy: bool
    intent: str  # "rewrite"|"summarize"|"bullets"|"brainstorm"|"code"|"analysis"|"general"
    has_strict_constraints: bool


@dataclass(frozen=True)
class TierDecision:
    tier: Tier
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutingConstraints:
    max_cost_usd: float | None = None
    max_total_latency_ms: int | None = None
    preferred_provider: str | None = None
    allowed_providers: list[str] | None = None
    allowed_billing_classes: list[str] | None = None
    allowed_models: list[str] | None = None
    min_context_limit: int | None = None
    reasoning_mode: str | None = None
    reasoning_effort: str | None = None
    json_only: bool = False
    strict_format: bool = False


@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model_name: str
    tier: Tier
    input_cost_per_1m: float
    output_cost_per_1m: float
    context_limit: int
    tags: list[str]
    billing_class: ModelBillingClass = ModelBillingClass.ADVANCED
    input_credit_multiplier: float = 0.0
    output_credit_multiplier: float = 0.0
    credit_usage_label: str = ""
    credit_pricing_version: str = ""
    enabled: bool = True
    selectable: bool = True
    display_name: str = ""
    description: str = ""
    release_status: str = "unknown"
    lifecycle_status: str = "ACTIVE"
    replacement_model: str | None = None
    retirement_date: str | None = None
    migration_reason: str | None = None
    pricing_model: str | None = None
    pricing_rule_id: str | None = None
    pricing_effective_from: str | None = None
    pricing_effective_until: str | None = None
    long_context_threshold_tokens: int | None = None
    cached_input_cost_per_1m: float | None = None
    cache_write_cost_per_1m: float | None = None
    max_output_tokens: int | None = None
    aliases: list[str] = field(default_factory=list)
    reasoning_modes: list[str] = field(default_factory=list)
    default_reasoning_mode: str | None = None
    reasoning_efforts: list[str] = field(default_factory=list)
    reasoning_disable_supported: bool = True
    reasoning_counts_against_output: bool = True
    pricing_source_url: str | None = None
    lifecycle_source_url: str | None = None
    source_verified_at: str | None = None
    supports_image_input: bool = False
    supported_attachment_mime_types: list[str] = field(default_factory=list)
    max_attachment_bytes: int | None = None
    max_attachments_per_request: int | None = None


@dataclass(frozen=True)
class SelectionResult:
    primary_candidate: ModelCandidate
    fallback_candidates: list[ModelCandidate]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: str = "ok"
    severity: str = "none"


class NextAction(str, Enum):
    RETRY_SAME_TIER = "retry_same_tier"
    ESCALATE_TIER = "escalate_tier"
    STOP = "stop"


@dataclass(frozen=True)
class FallbackDecision:
    action: NextAction
    next_tier: Tier | None
    reason: str
