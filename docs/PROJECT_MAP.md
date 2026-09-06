# CortexAI Project Map

This map is the quick "where do I change X?" reference for the current API-first CortexAI codebase.

## Runtime Entrypoints

- Full local app runner and guarded Free/Plus/Pro/unrestricted IntelliJ profiles: `run_app.py`; effective local profile construction: `server/billing/subscription_service.py`
- API server: `run_server.py`
- Main FastAPI app wiring: `server/app.py`
- Frontend runtime config renderer: `server/frontend_runtime_config.py` (`GET /runtime-config.js`)
- Browser lifecycle diagnostics ingestion: `server/routes/client_diagnostics.py` (`POST /v1/client-diagnostics`)
- Browser E2E harness bootstrap: `e2e/server/run_e2e_server.py`

## API Contracts

- Routes: `server/routes/`
- Request/response DTOs: `server/schemas/`
- Error mapping and route guardrails: `server/utils.py`

## Orchestration and Routing

- Core orchestration path: `orchestrator/core.py`
- Smart routing planner: `orchestrator/smart_router.py`
- Tier selection: `orchestrator/tier_decider.py`
- Candidate ranking: `orchestrator/model_selector.py`
- Fallback decisions: `orchestrator/fallback_manager.py`
- Web research intent/query sanitization and Tavily search options: `tools/web/`

## Provider Clients

- Client registry: `api/client_registry.py`
- Base contract: `api/base_client.py`
- Providers:
  - `api/openai_client.py`
  - `api/google_gemini_client.py`
  - `api/deepseek_client.py`
  - `api/grok_client.py`
  - `api/claude_client.py`

## Config Sources of Truth

- Provider catalog and defaults: `config/providers.yaml`
- Canonical model catalogue, lifecycle, official source evidence, effective-dated provider pricing, smart-router metadata, and consumer credit metadata: `config/model_registry.yaml`; loaders/resolution: `config/pricing.py`, `orchestrator/model_registry.py`
- Canonical generation v3 Auto policy, explicit profiles, and operational ceiling: `config/generation_profiles.yaml`; provider/model/prompt/context resolver: `orchestrator/generation_policy.py`; reservation-affordability clamp: `server/generation_service.py`; normalized terminal status: `orchestrator/completion_status.py`
- Subscription placement is data-driven through each registry row's `billing_class` and the plan catalogue's allowed classes. Claude Sonnet 4.6 is `advanced` (Plus/Pro), while Claude Opus 4.5 and 4.6 are `premium` (Pro); do not duplicate per-model plan lists elsewhere.
- Consumer subscription plans: `config/subscription_plans.yaml`
- Immutable plan types, validation, and cache: `server/billing/models.py`, `server/billing/plan_catalog.py`
- Effective account/subscription lifecycle and entitlement decisions: `server/billing/account_service.py`, `server/billing/subscription_service.py`, `server/billing/entitlement_service.py`, `server/billing/errors.py`
- Cache-aware credit arithmetic and deterministic token estimates: `server/billing/credit_calculator.py`, `server/billing/credit_estimator.py`, `utils/token_estimation.py`; cache policy/rollout flags: `config/cache_optimization.py`; canonical requested-model response-card, history, and aggregate DTO credit calculation: `server/billing/response_credit_service.py`, `server/schemas/responses.py`; Ask/Compare/Optimize/Cortex Analysis authorization, Smart candidate affordability, conservative cache-write reservation, shadow/authoritative settlement, adjustment-ledger reconciliation, and per-item initial-query/activity metadata: `server/billing/enforcement_service.py`; privacy-policy query sanitization, reusable context-summary persistence, and committing route adapters: `server/persistence.py`; request integration and failure-safe reservation release: `server/routes/chat.py`, `server/routes/compare.py`, `server/routes/optimize.py`, `server/routes/cortex_analysis.py`
- Atomic allowance reservation lifecycle: `server/billing/metering_service.py`; transaction-neutral locks, activity heartbeats, and counter mutations: `db/billing_repository.py`; PostgreSQL startup validation: `server/billing/schema_preflight.py`; automatic heartbeat/stale cleanup worker: `server/billing/reservation_cleanup.py`, `server/app.py`
- Public effective-plan snapshot and authenticated itemized credit history with `activity_id` plus nullable initial-query context: `server/routes/entitlements.py` (`GET /v1/entitlements`, `GET /v1/credits/transactions`); compatibility fields: `server/routes/whoami.py`
- Server-owned Stripe config and API adapter: `server/billing/stripe_gateway.py`; Customer/Checkout/Portal orchestration: `server/billing/session_service.py`; verified event lifecycle/reconciliation: `server/billing/webhook_service.py`; public plans, effective subscription, hosted-session, and webhook endpoints: `server/routes/billing.py`
- React subscription transport, access presentation, and in-memory authority boundary: `frontend-react/src/api/billing.ts`, `frontend-react/src/api/entitlements.ts`, `frontend-react/src/hooks/useSubscription.ts`, `frontend-react/src/subscription/subscriptionErrors.ts`, `frontend-react/src/subscription/subscriptionAccess.ts`; contract/denial tests: `frontend-react/src/__tests__/subscriptionDataLayer.test.tsx`, `subscriptionDenialDraft.test.tsx`
- React consumer plan management: route wiring in `frontend-react/src/App.tsx`; public catalogue in `frontend-react/src/pages/PricingPage.tsx`; authenticated lifecycle/allowance view in `frontend-react/src/pages/BillingPage.tsx`; shared responsive account shell in `frontend-react/src/components/subscription/SubscriptionPageShell.tsx`; backend-plan-to-menu mapping in `frontend-react/src/subscription/accountMenuPresentation.ts`; summary navigation in `frontend-react/src/components/layout/AccountMenu.tsx` across Chat, Models, Usage, AI credits, Pricing, and Billing; state/route tests in `frontend-react/src/__tests__/subscriptionPages.test.tsx`, `accountMenuPresentation.test.ts`, and `subscriptionRoutes.test.tsx`
- React entitlement UX: reusable dialog/badge/allowance/banner components in `frontend-react/src/components/subscription/`; model/Compare/Web/Improve/file controls in `frontend-react/src/components/composer/`; entitlement-aware initial Ask/Compare defaults in `frontend-react/src/config/askDefaults.ts`, `config/compareDefaults.ts`, and `components/composer/PromptComposer.tsx`; shared raw-unit-to-customer-credit presentation in `frontend-react/src/utils/aiCredits.ts`; draft-safe backend denial handling plus shared Prompt Optimizer/Ask/Compare credit activity IDs in `frontend-react/src/hooks/useChat.ts` and `optimization/promptOptimization.ts`; live `/v1/models` catalogue transformation and official pricing evidence in `frontend-react/src/config/modelsCatalog.ts` plus `frontend-react/src/pages/ModelsPage.tsx`; activity-grouped AI-credit totals and expandable breakdowns that fold optimizer-plus-answer charges into one final optimized answer in `frontend-react/src/pages/CreditsPage.tsx`; behavior/accessibility coverage in `frontend-react/src/__tests__/askDefaults.test.ts`, `compareDefaults.test.ts`, `subscriptionGating.test.tsx`, `promptOptimization.test.tsx`, `CreditsPage.test.tsx`, and `aiCredits.test.ts`
- Provider cost calculation and requested/served/pricing-model resolution: `config/pricing.py`, `utils/cost_calculator.py`, `api/base_client.py`, provider clients under `api/`
- Attachment ownership/capability preflight, cached parsing, query-relevant chunk selection, and provider-neutral materialization: `server/attachments.py`; effective-plan upload policy, legacy batch rollback, direct-upload intent/completion verification, queued deletion, upload/ingestion lifecycle, and private parsed-text caching: `server/files_service.py`; direct-migration startup guard: `server/attachment_schema_preflight.py`; S3 POST/HEAD abstraction plus optional exact SSE-S3/SSE-KMS signing fields: `server/object_storage.py`; legacy, upload-intent, completion, status, and deletion routes: `server/routes/files.py`; repository-versus-AWS ownership, production prerequisites, WAF/CORS/IAM/KMS checks, smoke tests, rollout, and rollback: `docs/runbooks/direct-s3-attachment-rollout.md`
- CortexAI Work runtime and policy: `server/work/`, with current-information Web resolution in `prompt_policy.py`, output guardrails in `output_policy.py`, browser-independent leased synchronization in `reconciler.py`, provider-session context continuity, and per-file artifact recovery; user-owned Work/tool contracts including chronological run history: `server/routes/work.py`, `server/routes/tools.py`, `server/schemas/work.py`; short-transaction persistence and leases: `db/work_repository.py`; additive schema: `db/migrations/20260820_add_cortex_work_mode.sql` and `db/migrations/20260829_add_work_web_output_and_model_identity.sql`; React transcript/history hydration: `frontend-react/src/pages/WorkPage.tsx`, `frontend-react/src/components/work/`, `frontend-react/src/api/work.ts`, `frontend-react/src/store/workStore.ts`; architecture and operations: `docs/work/architecture.md`, `docs/work/00-infrastructure-readiness.md`, `docs/runbooks/cortex-work.md`
- React direct-upload ownership: API metadata/completion/status/delete contracts in `frontend-react/src/api/files.ts`; safe rollout flags in `config/runtimeConfig.ts`; header-free `XMLHttpRequest`/`FormData` S3 transport in `uploads/directS3Upload.ts`; transient task state in `store/attachmentUploadStore.ts`; bounded authorization/transfer/retry/poll/cancel orchestration in `uploads/attachmentUploadQueue.ts`; ready promotion and per-file UI in `components/composer/AttachmentStrip.tsx`; ready-only Send gating in `components/composer/PromptComposer.tsx`. `chatStore.attachments` remains server-ready data only.

## Persistence and Reporting

- SQLAlchemy table reflection and repository access: `db/`
- B2C billing/Cortex/pricing/generation/cache and direct-upload lifecycle persistence plus transaction-neutral repository operations: `db/migrations/20260718_add_b2c_billing_foundation.sql`, `db/migrations/20260727_add_cortex_analysis_runs.sql`, `db/migrations/20260729_add_unified_ai_credits.sql`, `db/migrations/20260730_add_usage_reservation_activity.sql`, `db/migrations/20260731_add_model_pricing_audit.sql`, `db/migrations/20260802_add_cortex_analysis_attribution.sql`, `db/migrations/20260804_add_generation_budget_audit.sql`, `db/migrations/20260807_add_cache_aware_credit_accounting.sql`, `db/migrations/20260811_add_direct_s3_attachment_upload.sql`, `db/billing_repository.py`, `db/repository.py`, `db/tables.py`
- Credit arithmetic, schema preflight, lifecycle, entitlement, supplemental metering, Stripe, legacy/direct file routes and S3 policy normalization, and opt-in PostgreSQL concurrency coverage: `tests/test_credit_calculator.py`, `tests/test_billing_schema_preflight.py`, `tests/test_billing_repository.py`, `tests/test_billing_entitlements.py`, `tests/test_billing_metering.py`, `tests/test_stripe_billing.py`, `tests/test_stripe_webhooks.py`, `tests/test_baseline_safety_rails.py`, `tests/test_fastapi_contract_and_guardrails.py`, `tests/test_files_routes.py`, `tests/test_files_service.py`, `tests/test_object_storage.py`, `tests/test_billing_postgres_integration.py`
- Persistence service: `server/persistence.py`
- Cortex Analysis source normalization, anonymized GPT-5.4-mini call, structured validation, and source fingerprinting: `server/cortex_analysis.py`
- Cortex Analysis persistence/revision and attributed-result migrations: `db/migrations/20260727_add_cortex_analysis_runs.sql`, `db/migrations/20260802_add_cortex_analysis_attribution.sql`
- Provider cache affinity and stable prompt ordering: `orchestrator/cache_context.py`, `orchestrator/core.py`, and adapters under `api/`; persistent research reuse: `tools/web/persistent_research_store.py`; deterministic optimizer/Cortex reuse: `utils/prompt_optimizer.py`, `server/routes/optimize.py`, `server/routes/cortex_analysis.py`; context compaction/reusable summaries: `server/utils.py`, `server/persistence.py`; SHA-deduplicated extraction and query-relevant chunk reuse: `server/files_service.py`, `server/attachments.py`
- Usage reporting, including cache token/credit, reservation, reasoning, research, and measured reuse metrics: `server/usage_reporting.py`; per-request research/optimizer/Cortex reuse audit writes: `server/persistence.py`, `server/routes/optimize.py`, `server/routes/cortex_analysis.py`, and `db/repository.py`
- Savings reporting: `server/savings.py`
- DB migrations: `db/migrations/`

## Frontend and Browser Tests

- React/Vite frontend: `frontend-react/`
  - Runtime deps: `frontend-react/package.json` + `frontend-react/package-lock.json`
  - App entry: `frontend-react/src/main.tsx`, `frontend-react/src/App.tsx`
  - Static-hosting runtime config template: `frontend-react/runtime-config.example.js`
  - Browser boot/reload diagnostics: `frontend-react/src/diagnostics/bootDiagnostics.ts`
  - API hooks/client: `frontend-react/src/api/`, `frontend-react/src/hooks/`
  - Shared visual primitives: `frontend-react/src/components/common/`
  - Task-first Models destination, live `/v1/models` catalogue transformation/evidence, presentation-only JSON, and offline fallback: `frontend-react/src/pages/ModelsPage.tsx`, `frontend-react/src/pages/ModelsPage.module.css`, `frontend-react/src/config/models.data.json`, `frontend-react/src/config/defaultModels.ts`, `frontend-react/src/config/modelsCatalog.ts`, `frontend-react/src/hooks/useModels.ts`
  - React prompt optimization request shaping and UI fallback state: `frontend-react/src/optimization/promptOptimization.ts`
  - Compare model preference resolution: `frontend-react/src/config/compareDefaults.ts`
  - Cortex-managed Auto generation requests, incomplete-response notice, and retry-with-more-room flow: `frontend-react/src/components/composer/PromptComposer.tsx`, `frontend-react/src/hooks/useChat.ts`, `frontend-react/src/history/historyThreads.ts`, `frontend-react/src/components/results/ResponseCard.tsx`
  - Shared provider-first manual Ask/Compare picker, fine-pointer hover cascade, touch drill-down/Back flow, provider grouping, keyboard navigation, portal positioning, plan locks, and duplicate-state presentation: `frontend-react/src/components/composer/ModelPicker.tsx`, `frontend-react/src/components/composer/ModelPicker.module.css`
  - Model display labels and provider logo metadata: `frontend-react/src/config/modelPresentation.ts`
  - History thread grouping, persisted session rename, per-thread delete, and Compare-turn reconstruction: `server/routes/history.py`, `db/repository.py`, `frontend-react/src/api/history.ts`, `frontend-react/src/history/historyThreads.ts`, `frontend-react/src/hooks/useHistory.ts`, `frontend-react/src/components/layout/Sidebar.tsx`, `frontend-react/src/pages/ChatPage.tsx`
  - Cortex Analysis API/run hydration, append-only run state, stale-source detection, history selector, attributed continuous-document presentation, and handoff tokens: `server/routes/cortex_analysis.py`, `server/schemas/cortex_analysis.py`, `frontend-react/src/api/cortexAnalysis.ts`, `frontend-react/src/hooks/useCortexAnalysis.ts`, `frontend-react/src/store/chatStore.ts`, `frontend-react/src/components/results/CortexAnalysisZone.tsx`, `frontend-react/src/styles/cortex-analysis-tokens.css`
  - Usage & insights route, analytics states, KPI row, mobile compact dashboard, model leaderboard/provider-logo tiles, session modes panel, activity chart, period selector/export, and data layer: `frontend-react/src/pages/UsageInsightsPage.tsx`, `frontend-react/src/pages/UsageInsightsPage.module.css`, `frontend-react/src/api/usage.ts`, `frontend-react/src/hooks/useUsageSummary.ts`
  - AI credits route, unified balance/reset presentation, and itemized credit ledger: `frontend-react/src/pages/CreditsPage.tsx`, `frontend-react/src/pages/CreditsPage.module.css`, `frontend-react/src/components/subscription/UsageAllowance.tsx`, `frontend-react/src/api/entitlements.ts`
  - Active thread browser persistence and fresh-login reset markers: `frontend-react/src/session/activeSession.ts`
  - Transcript/session state: `frontend-react/src/store/chatStore.ts`
  - Main shell and responsive navigation: `frontend-react/src/pages/ChatPage.tsx`
  - Top-right Cognito account menu and summary plan/billing action: `frontend-react/src/components/layout/AccountMenu.tsx`
  - Desktop sidebar navigation, Models/Usage/AI credits route entries, history list, and collapse rail: `frontend-react/src/components/layout/Sidebar.tsx`
  - Ask/Compare result rendering: `frontend-react/src/components/results/`
  - Deterministic assistant-offered follow-up extraction and response-level chip row: `frontend-react/src/followups/suggestedFollowups.ts`, `frontend-react/src/components/results/SuggestedFollowUps.tsx`, `frontend-react/src/components/results/ResponseCard.tsx`
  - Composer, attachments, model selection, and routing toggles: `frontend-react/src/components/composer/`
  - Local full-app dev: `run_app.py` starts FastAPI plus Vite, sets `CORTEX_API_PROXY_TARGET` / `FRONTEND_RUNTIME_API_BASE`, and can select guarded loopback-only subscription profiles with `--subscription-plan`.
  - Production build output: `frontend-react/dist` after `npm run --prefix frontend-react build`
- Frontend selection in FastAPI: `server/app.py`
  - `FRONTEND_DIR` explicitly selects the static directory to mount.
  - `frontend-react/dist` is the default when `FRONTEND_DIR` is unset.
- Frontend container boundary: `Dockerfile.frontend` + `nginx.conf`
- Playwright E2E suite: `e2e/specs/`
  - Live full-stack browser scenarios: `e2e/specs/`
  - Frontend-only phone coverage: `e2e/responsive/mobile/`
  - Frontend-only desktop and iPad coverage: `e2e/responsive/desktop-ipad/`
  - Independent configs: `e2e/playwright.mobile.config.mjs`, `e2e/playwright.desktop-ipad.config.mjs`
- Playwright config: `e2e/playwright.config.mjs`

## CI and Workflows

- Main CI: `.github/workflows/ci.yml`
  - Detects React frontend/backend/shared path changes.
  - Runs blocking Ruff/MyPy checks against changed Python files with pinned dev tools.
  - Runs Black as an advisory changed-file format check until a repo-wide baseline is applied.
  - Runs Gitleaks as a pinned CLI directory scan of the checked-out tree.
  - Managed local hooks are configured in `.pre-commit-config.yaml`, launched by `scripts/run_local_ci_hook.sh`, executed by `scripts/run_local_ci.py`, and documented in `.codex/ci-commit-gate.md`; pre-commit checks staged content, both hook stages isolate pytest in a gate-private temp root, and pre-push blocks locally runnable CI failures while deferring an unavailable Docker image build to GitHub Actions.
- Targeted backend regression pack: `.github/workflows/incident-regression-38.yml`
- Live browser E2E: `.github/workflows/live-e2e.yml`

## Common Change Paths

- Add/modify an API endpoint:
  1. Update `server/routes/`
  2. Update `server/schemas/`
  3. Add/adjust tests

- Change React history behavior:
  1. Keep `/v1/history` row-level persistence and session-title rename semantics in `server/routes/history.py` and `db/repository.py`.
  2. Update session/thread normalization in `frontend-react/src/history/historyThreads.ts`.
  3. Update active-thread browser persistence in `frontend-react/src/session/activeSession.ts` when reload/fresh-login behavior changes.
  4. Update hydration in `frontend-react/src/store/chatStore.ts` and presentation in the desktop/mobile history surfaces.
  5. Cover Ask session grouping and Compare `request_group_id` grouping in React and API tests.
  6. Update `README.md` and related docs

- Change Cortex Analysis behavior:
  1. Keep model prompting/anonymization and structured validation in `server/cortex_analysis.py`.
  2. Keep session ownership and HTTP contracts in `server/routes/cortex_analysis.py` plus `server/schemas/cortex_analysis.py`.
  3. Keep append-only run and Compare response-revision persistence in `db/repository.py`; add a forward migration for schema changes.
  4. Update `frontend-react/src/api/cortexAnalysis.ts`, `useCortexAnalysis.ts`, `chatStore.ts`, and `CortexAnalysisZone.tsx` together.
  5. Validate reload/history hydration and staleness after response regeneration in backend and React tests.

- Change routing behavior:
  1. Update files in `orchestrator/`
  2. Validate with routing/fallback tests
  3. Update routing docs (`README.md`, `docs/SMART_ROUTING_DIAGRAM.md`)

- Change web research or Tavily retrieval behavior:
  1. Update `tools/web/intent.py`, `tools/web/contracts.py`, `tools/web/tavily_service.py`, `tools/web/tavily_client.py`, or `tools/web/tavily_resolver.py`
  2. Validate with Tavily/research tests
  3. Update `README.md`, `docs/TAVILY_INTEGRATION.md`, and logging/runbook docs when option or telemetry behavior changes

- Change token/cost behavior:
  1. Verify current pricing/lifecycle against official provider pages, then update the effective-dated record and `source_verified_at` in `config/model_registry.yaml`
  2. Update provider usage extraction only when the provider SDK response shape changes; `config/pricing.py` must stay a loader/resolver, not a second price table
  3. Preserve requested/served/pricing identity and pricing snapshot fields through `UnifiedResponse`, persistence, history, and reporting
  4. Update contract docs and tests

- Change generation-budget or reasoning behavior:
  1. Update `config/generation_profiles.yaml` for profile policy and `config/model_registry.yaml` for native model capabilities; do not add route/provider/billing-local ceilings.
  2. Keep resolution in `orchestrator/generation_policy.py`, terminal normalization in `orchestrator/completion_status.py`, and provider translation in `api/`.
  3. Pass the same resolved target value to `server/billing/enforcement_service.py`, provider execution, response metadata, and `db/repository.py`.
  4. Update managed-Auto/incomplete/retry behavior in React and validate `tests/test_generation_policy.py`, provider contract tests, frontend tests, and responsive E2E.
  5. Synchronize `docs/GENERATION_BUDGETS.md`, the rollout runbook, Postman, and every migration apply sequence.

- Change subscription plan definitions or model credit economics:
  1. Update `config/subscription_plans.yaml`, `server/billing/credit_calculator.py`, and/or each model's access category, input/output multipliers, usage label, and pricing version in `config/model_registry.yaml`; Tavily research settlement uses provider credits from research metadata
  2. Keep model access categories separate from smart-routing `T0`-`T3` tiers; never add a fallback multiplier
  3. Validate with `tests/test_credit_calculator.py`, `tests/test_subscription_plan_catalog.py`, `tests/test_model_registry_capabilities.py`, and the `/v1/models` contract tests

- Change DB schema:
  1. Add SQL migration under `db/migrations/`
  2. Update repository/table usage under `db/` + `server/`
  3. Follow `docs/runbooks/db-migrations.md`

- Change B2C billing persistence:
  1. Keep schema constraints/indexes in `db/migrations/20260718_add_b2c_billing_foundation.sql`, `db/migrations/20260729_add_unified_ai_credits.sql`, and `db/migrations/20260730_add_usage_reservation_activity.sql` aligned with `db/billing_repository.py` and `server/billing/schema_preflight.py`
  2. Register new tables for lazy reflection in `db/tables.py` and export public repository functions through `db/__init__.py`
  3. Keep repository functions transaction-neutral; callers own commit/rollback boundaries
  4. Validate portable repository behavior in `tests/test_billing_repository.py` and PostgreSQL row locking with `BILLING_TEST_DATABASE_URL`
  5. Keep effective paid access and lifecycle/grace policy in `server/billing/subscription_service.py`; routes must not infer access directly from a stored row

- Change effective subscription or entitlement behavior:
  1. Keep account validation/lazy creation in `server/billing/account_service.py`
  2. Keep lifecycle state, Free fallback, development override guards, and period selection in `server/billing/subscription_service.py`
  3. Keep feature/model/file checks and required allowance quantities in `server/billing/entitlement_service.py`; atomic reserve/settle/release/expiry mutations belong in `server/billing/metering_service.py`
  4. Update `server/schemas/responses.py`, `/v1/entitlements`, `/v1/whoami`, Postman, and `tests/test_billing_entitlements.py` together
  5. Preserve the separation between smart-routing tiers and model access categories, and fail unknown plan/category/economics/status data conservatively

- Change Stripe Checkout or Customer Portal behavior:
  1. Keep credentials, paid-plan Price mapping, and all redirect URLs in `server/billing/stripe_gateway.py`; never accept those values from request bodies
  2. Keep short DB units of work and provider calls separated in `server/billing/session_service.py`; Customer claiming must remain compare-and-set and Customer/Checkout creation idempotent
  3. Keep routes session-scoped and provider-safe in `server/routes/billing.py`; existing provider-live subscriptions must route to Portal instead of creating Checkout
  4. Update strict request/response schemas, `.env.example`, `docs/runbooks/stripe-billing.md`, README/FastAPI docs, Postman, and `tests/test_stripe_billing.py` together
  5. Hosted-session creation never grants paid access; only the verified webhook lifecycle may update paid subscription state

- Change the React subscription data layer:
  1. Keep public plan, current subscription, entitlement, Checkout, and Portal transport in `frontend-react/src/api/billing.ts` and `frontend-react/src/api/entitlements.ts`
  2. Keep structured billing error parsing in `frontend-react/src/subscription/subscriptionErrors.ts`; do not parse provider payloads or expose secrets in React
  3. Keep auth-aware, memory-only subscription state and bounded Checkout-return polling in `frontend-react/src/hooks/useSubscription.ts`; signed-out users must not call authenticated billing endpoints
  4. Treat return query parameters as refresh hints only; paid access remains the `/v1/entitlements` result synchronized by verified webhooks
  5. Validate API calls, typed errors, redirects, polling, and browser-storage non-authority in `frontend-react/src/__tests__/subscriptionDataLayer.test.tsx`

- Change React subscription gating UX:
  1. Derive model classes, feature flags, file limits, counters, and recommended plans only from `/v1/models`, `/v1/entitlements`, and `/v1/billing/plans`; never duplicate plan YAML values in React
  2. Keep locked premium models and restored history visible; unknown live billing metadata must fail conservatively without deleting or filtering prior content
  3. Keep backend enforcement authoritative and route structured access/allowance/payment denials through `subscriptionErrors.ts`, `subscriptionAccess.ts`, and `UpgradeDialog.tsx`
  4. Preserve prompt text and attachments until the backend accepts the stream; provider/file compatibility errors remain separate from subscription denials
  5. Validate composer, model catalogue, Usage allowances, keyboard dialog behavior, and narrow layouts with `subscriptionGating.test.tsx`, `subscriptionDenialDraft.test.tsx`, and affected page/component tests

- Change Stripe webhook or reconciliation behavior:
  1. Keep raw-body signature verification and Stripe API access in `server/billing/stripe_gateway.py`; never deserialize or mutate the request before verification
  2. Keep event dispatch, provider ownership checks, Price-to-plan mapping, stale-event policy, and reconciliation in `server/billing/webhook_service.py`
  3. Keep event/Subscription/period row locks and mutations transaction-neutral in `db/billing_repository.py`; callers own commit/rollback
  4. Keep effective paid/grace/cancellation access in `server/billing/subscription_service.py`; webhook handlers synchronize snapshots but do not invent a second entitlement policy
  5. Update `.env.example`, billing runbooks, README/FastAPI docs, Postman, and `tests/test_stripe_webhooks.py` together; do not expose cross-account reconciliation until administrator authorization exists

- Change atomic subscription metering:
  1. Keep transaction orchestration and transition rules in `server/billing/metering_service.py`; keep SQL row locks and counter mutations in `db/billing_repository.py`
  2. Preserve caller-owned commit/rollback boundaries, deterministic counter lock ordering, account-scoped request idempotency, nonnegative counters, and conservative configuration failures
  3. Validate portable transitions in `tests/test_billing_metering.py` and real concurrent overuse prevention with `BILLING_TEST_DATABASE_URL` in `tests/test_billing_postgres_integration.py`
  4. Ask/Compare/Optimize/Cortex Analysis routes integrate through `server/billing/enforcement_service.py`; preserve pre-execution affordability/reservation, clamped execution parity, supplemental actual-usage settlement, fixed research charging, and failure release without folding provider execution into billing transactions
  5. Keep uploads free, account attachment context through the consuming model call, and write one immutable `credit_transactions` row per reconciled item

- Investigate production logging incidents on AWS EC2:
  1. Follow `docs/runbooks/aws-ec2-logging.md`
  2. Correlate CloudFront/WAF logs with `request_id`/`X-Amz-Cf-Id`
  3. Use upload (`upload.*`) and research (`research.*`) event families for root cause

- Change React frontend behavior:
  1. Update `frontend-react/src/`
  2. Keep npm dependencies in `frontend-react/package.json` and `frontend-react/package-lock.json`
  3. Validate with `npm run --prefix frontend-react build`
  4. For FastAPI-hosted React, build first and set `FRONTEND_DIR` to `frontend-react/dist`
  5. Update `README.md` / `docs/FASTAPI_README.md` if setup, runtime config, or deployment assumptions change

- Change CortexAI Work behavior:
  1. Keep HTTP/auth ownership in `server/routes/work.py` and `server/routes/tools.py`; keep provider, MCP, billing, approval, artifact, and recovery orchestration in `server/work/`
  2. Keep external provider/MCP/OAuth/S3 calls outside DB transactions and preserve lease/event/request idempotency in `db/work_repository.py`
  3. Update `server/schemas/work.py`, `config/subscription_plans.yaml`, `/v1/entitlements`, `/v1/billing/plans`, React Work types/store/API, and Postman together when a contract or entitlement changes
  4. Preserve explicit approval for sensitive actions, exact scope for remembered WRITE grants, redaction, SSRF protection, chronological transcript hydration, provider-session continuity, and authenticated Cortex-owned artifact downloads
  5. Run Work repository/runtime/policy tests, full backend regressions, React typecheck/lint/tests/build, and the responsive browser suites; update `docs/work/` plus `docs/runbooks/cortex-work.md`

---

Last updated: 2026-08-20

## Cortex-issued subscription access

- `server/billing/grant_service.py`: existing-user validation, issue/change/revoke, actor/reason/expiry validation, monthly anchor calculation and inspection.
- `scripts/manage_subscription_grant.py`: trusted operator CLI and single commit/rollback boundary; no HTTP assignment route.
- `db/billing_repository.py`, `db/tables.py`, `server/billing/schema_preflight.py`: grant persistence, account locking, reflection and mandatory schema checks.
- `db/migrations/20260905_add_subscription_grants.sql`: additive grant/audit table, period source FK and separate grant period identity.
- `server/billing/subscription_service.py`: guarded local > enabled valid Stripe > valid grant > Free; existing entitlement, Work and credit services consume the catalogue unchanged.
- `frontend-react/src/pages/PricingPage.tsx`, `BillingPage.tsx`: granted current-plan display, CortexAI-provided access and usage-reset wording.
- `docs/runbooks/subscription-grants.md`: operator commands, coordinated deployment and audit policy.
