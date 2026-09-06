// Mirrors server/schemas/requests.py and server/schemas/responses.py.

export type MessageRole = "user" | "assistant" | "system";

export interface ConversationHistoryItem {
  role: MessageRole;
  content: string;
}

export interface UserContextRequest {
  session_id?: string;
  conversation_history?: ConversationHistoryItem[];
  new_session?: boolean;
}

export interface ChatRoutingRequest {
  smart_mode?: boolean;
  research_mode?: boolean;
}

export type AttachmentUsageRole = "primary" | "reference";
export type AttachmentTransformMode = "auto" | "text_only" | "vision_pages" | "table_summary";
export type GenerationProfile = "auto" | "quick" | "balanced" | "deep" | "extended";
export type ReasoningMode = "auto" | "off" | "on";
export type ReasoningEffort =
  | "auto"
  | "minimal"
  | "low"
  | "medium"
  | "high"
  | "xhigh"
  | "max";

export interface GenerationRequest {
  profile?: GenerationProfile;
  max_output_tokens?: number;
  reasoning?: {
    mode?: ReasoningMode;
    effort?: ReasoningEffort;
  };
}

export interface AttachmentRequestItem {
  file_id: string;
  usage_role?: AttachmentUsageRole;
  transform_mode?: AttachmentTransformMode;
}

export interface ChatRequest {
  prompt: string;
  credit_activity_id?: string;
  initial_query?: string;
  provider?: string;
  model?: string;
  context?: UserContextRequest;
  routing?: ChatRoutingRequest;
  attachments?: AttachmentRequestItem[];
  regeneration?: {
    source_request_id: string;
    refresh_research?: boolean;
    retry_reason?: "output_limit";
  };
  generation?: GenerationRequest;
  temperature?: number;
  max_tokens?: number;
}

export interface CompareTargetRequest {
  provider: string;
  model?: string;
  generation?: GenerationRequest;
}

export interface CompareRequest {
  prompt: string;
  credit_activity_id?: string;
  initial_query?: string;
  targets: CompareTargetRequest[];
  routing?: ChatRoutingRequest;
  context?: UserContextRequest;
  attachments?: AttachmentRequestItem[];
  generation?: GenerationRequest;
  timeout_s?: number;
  temperature?: number;
  max_tokens?: number;
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_input_tokens?: number;
  cache_write_tokens?: number;
  reasoning_tokens?: number;
}

export type ResponseRunStatus =
  | "queued"
  | "optimizing"
  | "requesting"
  | "streaming"
  | "finalizing"
  | "complete"
  | "incomplete"
  | "failed";

export interface ApiError {
  code: string;
  message: string;
  provider: string;
  retryable: boolean;
  details: Record<string, unknown>;
}

export interface WebSourceItem {
  title: string;
  url: string;
}

export interface ChatResponse {
  request_id: string;
  response_version?: number;
  session_id?: string;
  text: string;
  provider: string;
  model: string;
  requested_model?: string;
  served_model?: string;
  pricing_model?: string;
  model_lifecycle_status?: string;
  alias_redirected?: boolean;
  replacement_model?: string;
  migration_reason?: string;
  reasoning_mode?: string;
  latency_ms: number | null;
  token_usage: TokenUsage | null;
  estimated_cost: number;
  cost_currency: string;
  pricing_version?: string;
  pricing_rule_applied?: string;
  pricing_unknown?: boolean;
  pricing_snapshot?: Record<string, unknown>;
  ai_credits?: number;
  credit_usage_estimated?: boolean;
  cache_hit?: boolean;
  cache_hit_ratio?: number;
  cache_savings_ai_credits?: number;
  uncached_equivalent_ai_credits?: number;
  finish_reason?: string;
  completion_status?: "complete" | "incomplete" | "failed";
  stop_cause?: string;
  generation_budget?: {
    profile: string;
    requested_max_output_tokens: number;
    effective_max_output_tokens: number;
    requested_reasoning_mode: string;
    effective_reasoning_mode: string;
    requested_reasoning_effort: string;
    effective_reasoning_effort: string;
    reasoning_disable_supported: boolean;
    reasoning_counts_against_output: boolean;
    policy_version: string;
  };
  retry_with_more_room?: {
    available: boolean;
    recommended_profile?: GenerationProfile;
  };
  error?: ApiError;
  web_source_items: WebSourceItem[];
  timestamp: string;
  ui_status?: ResponseRunStatus;
  started_at?: string;
  completed_at?: string;
  failed_at?: string;
}

export interface CompareResponse {
  request_group_id: string;
  session_id?: string;
  responses: ChatResponse[];
  success_count: number;
  error_count: number;
  total_tokens: number;
  total_cost: number;
  total_ai_credits?: number;
  total_cache_savings_ai_credits?: number;
  timestamp: string;
}

export type ModelBillingClass = "economical" | "standard" | "advanced" | "premium";

export type SubscriptionPlanCode = "free" | "plus" | "pro";

export type SubscriptionStatus =
  | "free"
  | "trialing"
  | "active"
  | "past_due"
  | "unpaid"
  | "canceled"
  | "incomplete"
  | "incomplete_expired"
  | "paused"
  | "configuration_error";

export type SubscriptionMeterKey = "ai_credits";

export interface AllowanceCounter {
  used: number;
  reserved: number;
  limit: number;
  remaining: number;
}

export interface PublicBillingPlanFeatures {
  max_compare_models: number;
  research_enabled: boolean;
  prompt_improvement_enabled: boolean;
  file_analysis_enabled: boolean;
  work_enabled?: boolean;
  verified_connectors_enabled?: boolean;
  custom_mcp_enabled?: boolean;
  action_tools_enabled?: boolean;
  max_active_work_runs?: number;
  allowed_billing_classes: ModelBillingClass[];
}

export interface PublicBillingPlanAllowances {
  ai_credits: number;
}

export interface PublicBillingPlan {
  code: SubscriptionPlanCode;
  display_name: string;
  monthly_price: number;
  recommended: boolean;
  features: PublicBillingPlanFeatures;
  allowances: PublicBillingPlanAllowances;
}

export interface BillingPlansResponse {
  currency: "USD";
  billing_period: "monthly";
  billing_enabled: boolean;
  plans: PublicBillingPlan[];
}

export interface BillingSubscriptionResponse {
  plan_code: SubscriptionPlanCode;
  status: SubscriptionStatus;
  provider: string | null;
  current_period_start: string;
  current_period_end: string;
  cancel_at_period_end: boolean;
  can_manage: boolean;
}

export interface EntitlementsResponse {
  plan: {
    code: SubscriptionPlanCode;
    display_name: string;
    status: SubscriptionStatus;
    source: string;
    renews_at: string;
    cancel_at_period_end: boolean;
    grace_until: string | null;
  };
  features: {
    compare_enabled: boolean;
    max_compare_models: number;
    research_enabled: boolean;
    prompt_improvement_enabled: boolean;
    file_analysis_enabled: boolean;
    usage_export_enabled: boolean;
    saved_history_enabled: boolean;
    models_catalog_enabled: boolean;
    work_enabled?: boolean;
    verified_connectors_enabled?: boolean;
    custom_mcp_enabled?: boolean;
    action_tools_enabled?: boolean;
  };
  model_access: {
    allowed_billing_classes: ModelBillingClass[];
  };
  limits: {
    max_files_per_request: number;
    max_file_bytes: number;
    max_active_work_runs?: number;
    max_tool_connections?: number;
    max_mcp_servers_per_run?: number;
    max_work_credit_budget?: number;
  };
  allowances: Partial<Record<SubscriptionMeterKey, AllowanceCounter>>;
  period: {
    starts_at: string;
    ends_at: string;
  };
}

export interface CreditTransaction {
  id: string;
  request_id: string;
  activity_id: string;
  query: string | null;
  operation_type: string;
  item_type: "model" | "research" | "adjustment";
  provider: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  input_credits: number;
  output_credits: number;
  fixed_credits: number;
  total_credits: number;
  provider_cost_usd: number;
  usage_estimated: boolean;
  pricing_version: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface CreditTransactionsResponse {
  items: CreditTransaction[];
  limit: number;
  offset: number;
}

export interface CheckoutSessionResponse {
  checkout_url: string;
  destination: "checkout" | "portal";
}

export interface PortalSessionResponse {
  portal_url: string;
}

export interface ModelCatalogItem {
  provider: string;
  model: string;
  tier: string;
  billing_class: ModelBillingClass;
  access_category?: ModelBillingClass;
  input_credit_multiplier?: number;
  output_credit_multiplier?: number;
  credit_usage_label?: string;
  credit_pricing_version?: string;
  input_cost_per_1m: number;
  output_cost_per_1m: number;
  cached_input_cost_per_1m?: number;
  cache_write_cost_per_1m?: number;
  context_limit: number;
  max_output_tokens?: number;
  tags: string[];
  enabled: boolean;
  selectable?: boolean;
  display_name?: string;
  description?: string;
  release_status?: string;
  lifecycle_status?: string;
  replacement_model?: string;
  retirement_date?: string;
  migration_reason?: string;
  pricing_model?: string;
  pricing_rule_id?: string;
  pricing_effective_from?: string;
  pricing_effective_until?: string;
  long_context_threshold_tokens?: number;
  aliases?: string[];
  reasoning_modes?: string[];
  default_reasoning_mode?: string;
  reasoning_efforts?: string[];
  reasoning_disable_supported?: boolean;
  reasoning_counts_against_output?: boolean;
  pricing_source_url?: string;
  lifecycle_source_url?: string;
  source_verified_at?: string;
  supports_image_input: boolean;
  supported_attachment_mime_types: string[];
  max_attachment_bytes?: number;
  max_attachments_per_request?: number;
}

export interface ProviderCatalogItem {
  provider: string;
  label: string;
  api_key_env: string;
  default_model_env: string;
  default_model: string;
  byok_supported: boolean;
  capabilities: string[];
  ui: Record<string, unknown>;
  model_count: number;
  enabled_model_count: number;
}

export interface ModelsCatalogResponse {
  provider?: string;
  enabled_only: boolean;
  models: ModelCatalogItem[];
  total: number;
  timestamp: string;
}

export interface ProvidersCatalogResponse {
  providers: ProviderCatalogItem[];
  total: number;
  timestamp: string;
}

export interface HistoryEntry {
  id: number;
  request_id?: string;
  session_id?: string;
  session_title?: string;
  request_group_id?: string;
  timestamp: string;
  mode: string;
  prompt: string;
  provider: string;
  model: string;
  requested_model?: string;
  served_model?: string;
  pricing_model?: string;
  model_lifecycle_status?: string;
  alias_redirected?: boolean;
  replacement_model?: string;
  migration_reason?: string;
  pricing_rule_applied?: string;
  pricing_version?: string;
  pricing_unknown?: boolean;
  response_version?: number;
  generation_profile?: GenerationProfile;
  effective_max_output_tokens?: number;
  effective_reasoning_mode?: string;
  effective_reasoning_effort?: string;
  generation_policy_version?: string;
  completion_status?: "complete" | "incomplete" | "failed";
  stop_cause?: string;
  response: string;
  latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  tokens?: number;
  cost?: number;
  ai_credits?: number;
  credit_usage_estimated?: boolean;
  research_ai_credits?: number;
  research_credit_usage_estimated?: boolean;
  web_source_items: WebSourceItem[];
}

export type HistoryThreadMode = ChatMode | "mixed";

export interface HistoryThread {
  key: string;
  sessionId?: string;
  entries: HistoryEntry[];
  title: string;
  latestTimestamp: string;
  latestTimestampMs: number;
  mode: HistoryThreadMode;
  preferredMode: ChatMode;
  providerLabel: string;
  modelLabel: string;
  totalCost: number;
  totalTokens: number;
  turnCount: number;
  searchText: string;
}

export type FileUploadStatus = "ready" | "processing" | "failed" | string;

export interface FileUploadResponse {
  file_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  status: FileUploadStatus;
  error_code?: string | null;
  error_message?: string | null;
  ingestion_meta: Record<string, unknown>;
  created_at: string;
  updated_at?: string | null;
  expires_at?: string | null;
  deduplicated: boolean;
}

export interface PresignedPost {
  url: string;
  fields: Record<string, string>;
  expires_at: string;
}

export interface FileUploadIntentItem {
  file_id: string;
  original_filename: string;
  mime_type: string;
  size_bytes: number;
  status: FileUploadStatus;
  error_code?: string | null;
  error_message?: string | null;
  ingestion_meta: Record<string, unknown>;
  created_at: string;
  updated_at?: string | null;
  expires_at?: string | null;
  upload: PresignedPost;
}

export interface FileUploadIntentResponse {
  files: FileUploadIntentItem[];
}

export interface CognitoConfig {
  enabled: boolean;
  client_id?: string;
  clientId?: string;
  domain?: string;
  region?: string;
  redirect_uri?: string;
  redirectUri?: string;
  logout_url?: string;
  logoutUrl?: string;
}

export interface WhoAmIBaseline {
  provider: string;
  model: string;
  source: string;
}

export interface WhoAmIRateLimitConfig {
  requests_per_minute: number;
  daily_cap_scope: string;
  daily_token_cap?: number;
  daily_cost_cap?: number;
}

export interface WhoAmIBreakerConfig {
  failure_threshold: number;
  window_seconds: number;
  cooldown_seconds: number;
  scope: string;
}

export interface WhoAmIResponse {
  api_key_id?: string;
  user_id?: string;
  plan_tier?: string;
  storage_policy: string;
  redact_pii: boolean;
  baseline: WhoAmIBaseline;
  rate_limits: WhoAmIRateLimitConfig;
  breakers: WhoAmIBreakerConfig;
}

export interface UsageSummaryPeriod {
  from: string;
  to: string;
  label: string;
}

export interface UsageSummaryModel {
  provider: "openai" | "anthropic" | "deepseek" | "google" | "meta" | "mistral" | string;
  modelId: string;
  displayName: string;
  replies: number;
  viaSmart: number;
}

export interface UsageSummarySessionModes {
  askOnly: number;
  compareOnly: number;
  mixed: number;
}

export interface UsageActivityDay {
  date: string;
  tokens: number;
}

export interface UsageSummary {
  period: UsageSummaryPeriod;
  totalTokens: number;
  totalRequests: number;
  totalSessions: number;
  avgLatencyMs: number;
  p95LatencyMs: number;
  minLatencyMs: number;
  avgCostPerRequest: number;
  totalSpend: number;
  tokensDeltaPct: number;
  smartRoutedTotal: number;
  models: UsageSummaryModel[];
  sessionModes: UsageSummarySessionModes;
  switchedMidSession: number;
  activityDaily: UsageActivityDay[];
  totalAiCredits?: number;
  averageAiCreditsPerRequest?: number;
  normalInputTokens?: number;
  cachedInputTokens?: number;
  cacheWriteTokens?: number;
  cacheHitRatio?: number;
  cacheSavingsAiCredits?: number;
  providerCostCacheSavings?: number;
  reservationCredits?: number;
  settledCredits?: number;
  reservationReleaseRatio?: number;
  outputTokenUtilization?: number;
  reasoningTokens?: number;
  researchRequests?: number;
  researchReuseRate?: number;
  promptOptimizationReuseRate?: number;
  cortexAnalysisReuseRate?: number;
}

export interface OptimizeRequest {
  prompt: string;
  credit_activity_id?: string;
  context_hint?: string;
  context?: UserContextRequest;
}

export interface OptimizeResponse {
  original_prompt: string;
  optimized_prompt: string;
  was_optimized: boolean;
  server_optimization_enabled: boolean;
  optimization_status: string;
  fallback_reason?: string;
  optimization_reused?: boolean;
}

export type ChatMode = "single" | "compare";
export type TurnStatus = "idle" | "optimizing" | "streaming" | "complete" | "error" | "cancelled";
export type CortexAnalysisStatus = "idle" | "processing" | "failed";

export interface CortexAnalysisSource {
  requestId: string;
  responseVersion: number;
  responseName: string;
}

export interface CortexAnalysisUniqueInsight {
  responseName: string;
  text: string;
}

export interface CortexAnalysisDisagreement {
  who: string;
  text: string;
}

export interface CortexAnalysisRun {
  analysisId: string;
  requestGroupId: string;
  sessionId: string;
  model: string;
  recommendedAnswer: string;
  agreements: string[];
  disagreements: CortexAnalysisDisagreement[];
  disagreementNote: string | null;
  uniqueInsights: CortexAnalysisUniqueInsight[];
  confidence: {
    level: "limited" | "moderate" | "high";
    reason: string;
  };
  verify: string[];
  highStakesDomain: "financial" | "medical" | "legal" | "safety" | null;
  sourceFingerprint: string;
  sourceResponses: CortexAnalysisSource[];
  combinedResponseCount: number;
  failedResponseCount: number;
  createdAt: string;
  isStale: boolean;
}

export type PromptOptimizationUiStatus = "pending" | "optimized" | "kept_original" | "cancelled";

export interface PromptOptimizationState {
  status: PromptOptimizationUiStatus;
  originalPrompt: string;
  displayPrompt: string;
  note?: string;
  optimizationStatus?: string;
  fallbackReason?: string;
}

export type WorkSessionStatus =
  | "idle"
  | "running"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled";

export type WorkRunStatus =
  | "created"
  | "planning"
  | "running"
  | "waiting_for_approval"
  | "completed"
  | "failed"
  | "cancelled"
  | "budget_exhausted"
  | "output_limit_reached";

export type WorkWebMode = "auto" | "on" | "off";

export interface WorkSession {
  id: string;
  session_id: string;
  title: string | null;
  status: WorkSessionStatus;
  agent_provider: string;
  created_at: string;
  updated_at: string;
  latest_run_status: WorkRunStatus | null;
}

export interface WorkRun {
  id: string;
  work_session_id: string;
  request_id: string;
  instruction: string;
  status: WorkRunStatus;
  provider: string;
  max_credit_budget: number;
  max_output_tokens: number;
  actual_output_tokens: number;
  reserved_credits: number;
  actual_credits: number;
  provider_model_id: string | null;
  billing_model_id: string | null;
  billing_model_source: string | null;
  provider_agent_id: string | null;
  provider_agent_version: number | null;
  output_finalize_requested_at: string | null;
  output_limit_interrupt_requested_at: string | null;
  configuration_snapshot: Record<string, unknown>;
  usage_snapshot: Record<string, unknown>;
  stop_reason: string | null;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkEvent {
  id: string;
  sequence: number;
  type: string;
  display_message: string | null;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface WorkEventsResponse {
  items: WorkEvent[];
  latest_sequence: number;
}

export interface WorkArtifact {
  id: string;
  file_id: string;
  role: "input" | "artifact";
  source: "user" | "agent" | "connector";
  filename: string;
  mime_type: string;
  size_bytes: number;
  artifact_type: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface WorkApproval {
  id: string;
  work_run_id: string;
  tool_call_id: string;
  connection_id: string | null;
  action_type: string;
  tool_name: string;
  description: string;
  request_payload: Record<string, unknown>;
  status: "pending" | "approved" | "denied" | "expired";
  requested_at: string;
  decided_at: string | null;
}

export interface ToolCatalogItem {
  connector_key: string;
  display_name: string;
  description: string;
  icon: string;
  connection_state: string;
  plan_requirement: string;
  capabilities: string[];
  risk_classes: string[];
  configuration_required: boolean;
}

export interface ToolConnection {
  id: string;
  connector_key: string;
  connection_type: "cortex_builtin" | "mcp_remote";
  display_name: string;
  server_url: string | null;
  auth_type: string;
  status: "pending" | "connected" | "expired" | "error" | "disabled";
  granted_scopes: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  last_verified_at: string | null;
}

export interface ModelKey {
  provider: string;
  model: string;
}

export interface ChatTurn {
  id: string;
  mode: ChatMode;
  prompt: string;
  submittedPrompt: string;
  researchEnabled?: boolean;
  optimizeEnabled?: boolean;
  attachments: FileUploadResponse[];
  responses: ChatResponse[];
  status: TurnStatus;
  createdAt: string;
  requestGroupId?: string;
  compareSummary?: CompareResponse;
  optimization?: PromptOptimizationState;
  analysisRuns?: CortexAnalysisRun[];
  analysisStatus?: CortexAnalysisStatus;
  analysisError?: string;
}

export interface AppState {
  mode: ChatMode;
  smartMode: boolean;
  researchMode: boolean;
  compareResearchMode: boolean;
  optimizeMode: boolean;
  selectedModelKey: string;
  compareModelKeys: string[];
}

export interface StreamChunk {
  type: "delta" | "done" | "error" | "metadata" | "start";
  text?: string;
  error?: string;
  metadata?: Partial<ChatResponse>;
  provider?: string;
  model?: string;
  session_id?: string;
}

export interface CompareStreamChunk {
  type: "start" | "response_start" | "delta" | "response_done" | "done" | "error";
  index?: number;
  text?: string;
  error?: string;
  provider?: string;
  model?: string;
  response?: ChatResponse;
  compare?: CompareResponse;
  session_id?: string;
}
