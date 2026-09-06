# CortexAI - B2B LLM Gateway (CLI + API + Frontend)

CortexAI is a multi-provider orchestration gateway for OpenAI, Gemini, DeepSeek, Grok, and Claude with:
- API-first chat/compare/streaming
- smart routing + fallback
- full DB audit trail in DB mode
- BYOK tenant key support
- usage/cost/savings reporting

## Launch-Ready Capabilities

- API endpoints: `/v1/chat`, `/v1/chat/stream`, `/v1/compare`, `/v1/compare/stream`, `/v1/compare/{request_group_id}/analysis`, `/v1/compare/analysis-runs`, `/v1/providers`, `/v1/models`
- Integration diagnostics endpoints: `/v1/whoami`, `/v1/entitlements`, `/v1/client-diagnostics`
- Request attribution: `users`, `api_keys`, `sessions`, `messages`, `llm_requests`, `llm_responses`
- Routing telemetry: `routing_decisions`, `routing_attempts`
- Governance: `usage_daily`, daily caps, per-key rate limit, circuit breaker
- Savings telemetry: per-request baseline vs actual cost (`llm_savings`)
- Reporting/export: `/v1/usage`, `/v1/usage/summary`, `/v1/savings`, CSV export endpoints
- BYOK lifecycle: set/status/delete with encrypted-at-rest secrets
- Privacy controls: metadata-only persistence and optional PII redaction
- Optional prompt optimization endpoint: `/v1/optimize`
- Release gate: compile checks + full tests + DB smoke

## CortexAI Work

CortexAI Work is a separate durable task surface at `/work`; it does not reuse
the Ask/Compare turn reducer. Paid users can start a Work session with files,
an explicit AI-credit ceiling, server-resolved Web Auto/On/Off access, and selected tool
connections. The backend reserves the ceiling, creates or reuses an Anthropic
Managed Agent session, persists provider-neutral events, resumes through SSE
with `Last-Event-ID`, imports validated artifacts into private Cortex object
storage, and settles only the cumulative-usage delta for the run.

Work is off by default. Free has no Work entitlement; Plus enables one active
run and verified connectors; Pro raises the run/connection/budget limits and
adds custom remote MCP. Read tools are automatic. WRITE actions require an
approval unless the user explicitly saved the exact tool + connection grant
for the current Work session. Destructive, deployment, financial, and external
communication actions always interrupt for approval.

Work defaults to a 1,000,000-credit ($1.00) raw run ceiling, presented as 1,000
AI credits in the browser, and clamps that value to the effective plan maximum.
Anthropic enforces the corresponding session budget
and pauses at `budget_reached`; Cortex does not send polling interrupts. A later
run extends the reused provider session cap by its newly reserved budget, and a
budget-paused turn resumes from the provider budget update without a competing
`user.message`. Built-in `read`, `glob`, `grep`, and enabled web reads run
without confirmation. `bash`, `write`, `edit`, MCP writes, and every sensitive
action retain the applicable approval gate.

Every run also receives a server-owned 40,000 output-token ceiling. At 32,000
output tokens Cortex asks the Agent to stop exploring and finalize the best
available deliverable; at 40,000 it interrupts the remote session and records
the distinct `output_limit_reached` outcome. A PostgreSQL-lease background
reconciler applies this policy and completes usage, artifact, and billing sync
even when the browser is closed. The observed total can exceed the threshold by
tokens produced between provider usage snapshots, but it cannot continue as an
unbounded browser-owned run.

Work Web defaults to `Auto`. The backend—not the browser—classifies prompts that
need current information and enables the provider Web tools only for those
runs. `On` always requests Web and `Off` never mounts it; choosing `Off` for a
current-information prompt shows a warning but remains an explicit user choice.

Work settlement treats Managed Agent normal input, cache reads, and cache
writes as independent cumulative usage partitions; follow-ups subtract the
prior provider snapshot from each partition. The reconstructed model, active
runtime, and web-search charge is compared with Anthropic's cumulative USD
`list_cost` delta, and the greater value is the settlement floor. Invalid
currency or cost data stops reconciliation instead of silently underbilling.
The billing model is the model ID in the retrieved Anthropic session's resolved
Agent snapshot. Cortex persists the provider model, canonical pricing model,
Agent ID/version, and source on each run; an unknown or multi-model snapshot
fails closed instead of falling back to a separate billing-model environment
variable.

React remembers a newly created Work session before it requests the first run,
so a rejected start can be retried without creating another session. The
sidebar treats Work history as run-backed and omits session shells whose first
run never passed validation, entitlement, or credit reservation.
As soon as the user submits, React replaces the landing composer with a
dedicated `Starting work` state while the API validates and creates the durable
run. It does not fabricate a run ID or expose Stop until the accepted run is
returned, so provider-session startup latency never looks like a frozen button.
Work session titles are normalized to one line before persistence and again at
the Managed Agent boundary. Multiline prompts and invisible Unicode control or
format characters therefore cannot prevent provider-session creation, and an
existing failed session with an older unsafe title can be retried in place.

Opening a Work session now hydrates every durable run through
`GET /v1/work/sessions/{id}/runs` and renders the prompts, final responses, and
deliverables as one chronological transcript. Submitting a follow-up appends a
new turn instead of replacing the previous result, so an earlier security
analysis and its files remain visible and downloadable in the same task.
Changing Web or MCP selections updates the existing Managed Agent session so
its conversational context is retained. A provider session is replaced only
when its immutable vault-resource set changes; that fallback prepends a bounded
transcript of prior user instructions and visible outcomes from PostgreSQL.

The Work activity rail is a user-facing history, not a raw provider trace. It
omits unlabeled internal progress telemetry and animates only the latest visible
activity while a run is nonterminal. Terminal runs render every retained event
as settled, and a completed run marks all plan steps done. If status
reconciliation reaches a terminal state ahead of the browser's stream cursor,
React fetches and merges the remaining durable events before closing the stream
so the final written outcome appears without a page refresh.

Artifact import is idempotent per provider file and tied to the provider
session recorded on the originating run. Input and non-downloadable provider
files are skipped, one failed output cannot block the remaining deliverables,
and listing artifacts for a terminal run retries any files that could not be
copied into private Cortex object storage during completion.

Before enabling it, apply
`db/migrations/20260820_add_cortex_work_mode.sql`, then
`db/migrations/20260829_add_work_web_output_and_model_identity.sql`, and complete
[the Work rollout runbook](docs/runbooks/cortex-work.md). The architecture and
recovery contract are documented in [docs/work/architecture.md](docs/work/architecture.md);
the evidence-based infrastructure gate is
[docs/work/00-infrastructure-readiness.md](docs/work/00-infrastructure-readiness.md).

For a local provider-double smoke test only:

```ini
CORTEX_WORK_ENABLED=true
CORTEX_WORK_AGENT_PROVIDER=fake
CORTEX_WORK_MCP_ENABLED=true
CORTEX_WORK_ACTION_TOOLS_ENABLED=true
CORTEX_WORK_ARTIFACT_IMPORT_ENABLED=true
CORTEX_WORK_WEB_ENABLED=true
DEV_SUBSCRIPTION_PLAN=pro
```

Production must use `anthropic_managed_agents` and valid provider IDs; the fake
adapter is deterministic test infrastructure, not a deployment fallback.

## Runtime Modes

- `DATABASE_URL` is required at startup:
  - `DATABASE_URL` must be PostgreSQL (`postgresql://` or `postgresql+psycopg://`).
  - FastAPI persists to repository-backed SQLAlchemy tables (same artifact family as CLI).
  - Chat history endpoints read/write only from PostgreSQL tables.
  - Daily caps, rate limits, BYOK settings, savings, and reporting endpoints are active.
  - API startup fails fast when `DATABASE_URL` is missing.
  - Optional dev override: `ALLOW_NON_POSTGRES_DATABASE_URL=true` (not recommended for production).

## Quick Start

1. Create and activate a virtual environment.
```bash
python -m venv venv
venv\Scripts\activate
```
MacOS
source .venv/bin/activate

2. Install dependencies.
```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```
`requirements.txt` includes `tavily-python`, so Research Mode works once `TAVILY_API_KEY` is set. The FastAPI dependency is constrained away from `0.136.3` because that release is currently flagged by `pip-audit` advisory `MAL-2026-4750`.
`requirements.txt` includes `tavily-python`, so Research Mode works once `TAVILY_API_KEY` is set.
React frontend dependencies are managed by npm, not `requirements.txt`. If you want to run or build the React UI from `frontend-react/`, also install its locked Node dependencies:
```bash
npm ci --prefix frontend-react
```
Use Node.js 20.x for the React/Vite toolchain.

3. Configure `.env`.

Minimum API setup:
```ini
API_KEYS=dev-key-1
OPENAI_API_KEY=...
GOOGLE_GEMINI_API_KEY=...
DEEPSEEK_API_KEY=...
GROK_API_KEY=...
ANTHROPIC_API_KEY=...
```

Configure DB (required):
```ini
DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname
DB_SCHEMA=public
AUTO_REGISTER_UNMAPPED_API_KEYS=true
```

## Key Environment Variables

```ini
# Governance
# DAILY_TOKEN_CAP=100000       # optional max tokens per day
# DAILY_COST_CAP=25.00         # optional max daily spend in USD
DAILY_CAP_SCOPE=api_key   # api_key|user
REQUESTS_PER_MINUTE=60
REPORT_MAX_RANGE_DAYS=366 # reporting/export date-range guard (<=0 disables)

# Circuit breaker
CIRCUIT_FAILURE_THRESHOLD=5
CIRCUIT_WINDOW_SECONDS=60
CIRCUIT_COOLDOWN_SECONDS=120

# Ask smart routing controls (optional)
ENABLE_TRUE_SMART_CHAT_ROUTING=true
SMART_CHAT_MAX_COST_USD=
SMART_CHAT_MAX_TOTAL_LATENCY_MS=
SMART_CHAT_MIN_CONTEXT_LIMIT=
SMART_CHAT_PREFERRED_PROVIDER=      # openai|gemini|deepseek|grok|claude
SMART_CHAT_ALLOWED_PROVIDERS=       # comma-separated, e.g. openai,gemini

# Prompt optimization (optional)
ENABLE_PROMPT_OPTIMIZATION=false       # enables explicit POST /v1/optimize
ENABLE_ORCHESTRATOR_PROMPT_OPTIMIZATION=false  # opt-in auto-rewrite inside chat/compare
PROMPT_OPTIMIZER_PROVIDER=gemini    # openai|gemini|deepseek|grok|claude
PROMPT_OPTIMIZER_MODEL=             # optional; must belong to PROMPT_OPTIMIZER_PROVIDER
PROMPT_OPTIMIZER_MAX_RETRIES=3
PROMPT_OPTIMIZER_TIMEOUT_MS=5000    # explicit /v1/optimize hard deadline
PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES=2 # explicit /v1/optimize attempt count
PROMPT_OPTIMIZER_MAX_OUTPUT_TOKENS=450 # compact optimizer output cap
PROMPT_OPTIMIZER_TEMPERATURE=0.2    # low-drift optimizer generation setting

# B2C subscription resolution and metering
BILLING_ENABLED=false               # Stripe off; Cortex grants still apply
SUBSCRIPTION_PAYMENT_GRACE_DAYS=3   # fallback past_due grace window
STRIPE_SECRET_KEY=                  # server-only; required when billing is enabled
STRIPE_WEBHOOK_SECRET=              # server-only signing secret for the webhook endpoint
STRIPE_PLUS_MONTHLY_PRICE_ID=       # server-owned Price mapping
STRIPE_PRO_MONTHLY_PRICE_ID=        # server-owned Price mapping
STRIPE_CHECKOUT_SUCCESS_URL=https://app.example.com/account/billing?checkout=success
STRIPE_CHECKOUT_CANCEL_URL=https://app.example.com/pricing?checkout=cancelled
STRIPE_PORTAL_RETURN_URL=https://app.example.com/account/billing
# STRIPE_API_VERSION=               # optional; default is the SDK-pinned version
# DEV_SUBSCRIPTION_PLAN=pro         # local/dev only; also supports guarded unrestricted mode

# On-demand Compare synthesis (requires OPENAI_API_KEY)
CORTEX_ANALYSIS_MODEL=gpt-5.4-mini

# Tavily research retrieval
TAVILY_API_KEY=                     # required when research_mode=true
TAVILY_ENHANCED_SEARCH_ENABLED=true # false => fixed Tavily params only
TAVILY_CHUNKS_PER_SOURCE=3          # 1..3; invalid values fall back to 3
TAVILY_ENHANCED_SEARCH_DOMAIN_RULES=true
TAVILY_NETWORK_DIAGNOSTICS_HOST=api.tavily.com
TAVILY_NETWORK_DIAGNOSTICS_PORT=443
TAVILY_NETWORK_DIAGNOSTICS_INTERVAL_SECONDS=300
TAVILY_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS=2

# Storage/privacy
STORAGE_POLICY=full       # full|metadata (default: full when unset)
REDACT_PII=false          # true|false

# Attachments / object storage (feature-flagged)
ENABLE_ATTACHMENTS=false
ATTACHMENTS_DIRECT_UPLOAD_ENABLED=false       # opt-in browser-to-S3 API
ATTACHMENTS_LEGACY_PROXY_UPLOAD_ENABLED=true  # keep current clients working
ATTACHMENTS_PRESIGN_TTL_SECONDS=300
ATTACHMENTS_UPLOAD_INTENT_TTL_MINUTES=30
ATTACHMENTS_OBJECT_STORAGE_BACKEND=s3
ATTACHMENTS_S3_BUCKET=
ATTACHMENTS_S3_REGION=us-east-1
ATTACHMENTS_S3_ENDPOINT_URL=      # set for MinIO/local S3, leave empty for AWS S3
ATTACHMENTS_S3_ACCESS_KEY_ID=
ATTACHMENTS_S3_SECRET_ACCESS_KEY=
ATTACHMENTS_S3_SESSION_TOKEN=
ATTACHMENTS_S3_USE_SSL=true
ATTACHMENTS_S3_FORCE_PATH_STYLE=false  # use true with a MinIO/local endpoint when required
ATTACHMENTS_S3_KEY_PREFIX=attachments
ATTACHMENTS_S3_SERVER_SIDE_ENCRYPTION= # blank uses bucket default; AES256 or aws:kms signs an exact field
ATTACHMENTS_S3_SSE_KMS_KEY_ID=         # optional; valid only with aws:kms
ATTACHMENTS_MAX_FILE_BYTES=20971520
ATTACHMENTS_FILE_TTL_HOURS=168
ENABLE_ATTACHMENTS_CLEANUP_WORKER=false
ATTACHMENTS_CLEANUP_INTERVAL_SECONDS=300
ATTACHMENTS_ALLOWED_MIME_TYPES=image/jpeg,image/png,image/webp,image/gif,application/pdf,text/plain,text/csv,application/json,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet

# Structured logging (EC2 / CloudWatch friendly)
LOG_LEVEL=INFO
LOG_DESTINATION=both              # file|stdout|both (default: file)
LOG_CONSOLE_LEVEL=INFO
LOG_DIR=logs                      # relative paths resolve from repo root
LOG_FILE_MAX_BYTES=10485760
LOG_FILE_BACKUP_COUNT=5
LOG_NOISY_LIBRARIES_LEVEL=WARNING # httpx/urllib3/boto3/botocore logger level
# Backward compatibility: LOG_TO_CONSOLE=true implies LOG_DESTINATION=both when LOG_DESTINATION is unset

# Streaming resilience
STREAM_HEARTBEAT_INTERVAL_SECONDS=15 # 0 disables heartbeat NDJSON events while providers run

# BYOK encryption
MASTER_KEY=replace-with-strong-random-secret

# Savings baseline
BASELINE_MODEL_ID=openai:gpt-4o-mini
# or BASELINE_PROVIDER=openai + BASELINE_MODEL=gpt-4o-mini

# Component boundary controls
SERVE_FRONTEND=true      # false => API-only runtime (no static frontend mount)
FRONTEND_DIR=frontend-react/dist # optional override; this is also the default static build path
# Frontend runtime config served by GET /runtime-config.js
FRONTEND_RUNTIME_API_BASE=                           # optional; defaults to request origin
FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN=false      # optional browser override
FRONTEND_RUNTIME_DEV_SESSION_LOGIN_TOKEN=            # optional browser-visible local token

# AWS / reverse-proxy settings (important for CloudFront/ALB deployments)
ENABLE_PROXY_HEADERS=true          # trust X-Forwarded-Proto/Host from upstream proxy (default: true)
TRUSTED_PROXY_IPS=*                # comma-separated trusted upstream IPs, or '*' for all (default: *)
# SESSION_COOKIE_SECURE=           # optional explicit Secure flag; leave unset to auto-detect HTTPS
SESSION_MAX_AGE_SECONDS=604800     # session cookie lifetime in seconds (default: 7 days; 0 = session cookie)
COGNITO_SSL_VERIFY=true            # set false to skip TLS verification for Cognito endpoints (not recommended)
```

## Model catalogue, lifecycle, and pricing

- `config/model_registry.yaml` is the canonical, effective-dated source for selectable models, compatibility aliases, lifecycle state, context limits, official provider evidence, token-price rules, smart-routing metadata, and consumer credit metadata. A normal catalogue or pricing refresh is one data edit in this file.
- Runtime token-cost estimation in `config/pricing.py` and smart routing in `orchestrator/model_registry.py` both load that canonical registry; do not maintain a second price table in Python.
- Consumer plan definitions use `config/subscription_plans.yaml` and are validated at API startup by `server/billing/plan_catalog.py`.
- Provider metadata/defaults/allowlists use `config/providers.yaml` via `config/provider_catalog.py`.
- Provider defaults in `config/providers.yaml` must reference selectable registry IDs. Before changing a price or lifecycle date, verify it against the provider's official pages and update `source_verified_at`; the current evidence links are [OpenAI pricing/deprecations](https://developers.openai.com/api/docs/pricing), [Google Gemini pricing/deprecations](https://ai.google.dev/gemini-api/docs/pricing), [DeepSeek pricing/news](https://api-docs.deepseek.com/quick_start/pricing/), [xAI pricing/models](https://docs.x.ai/developers/pricing), [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing), and [Anthropic lifecycle](https://platform.claude.com/docs/en/about-claude/model-deprecations).
- Pricing rules may be effective-dated, processing-mode-specific, and long-context-specific. Cached input and cache writes are charged separately when a provider reports them. Reasoning tokens are retained for audit but are not double-billed when the provider already includes them in output tokens.
- Every response preserves `requested_model`, provider-reported `served_model`, and `pricing_model`, together with alias/lifecycle status and the exact pricing rule/version. Unknown exact model pricing uses a marked conservative provider fallback; it is never silently recorded as zero.
- Smart-routing tiers (`T0`-`T3`) and consumer model access categories (`economical`, `standard`, `advanced`, `premium`) are separate controls. Every configured model must declare an access category, input/output credit multipliers, a customer-facing credit-usage label, and a pricing version; missing or unknown values fail registry loading.
- The current plan catalogue defines server-owned Free, Plus, and Pro prices, entitlements, allowances, and safety limits. `server/billing/subscription_service.py` resolves a database-backed effective plan, applies the lifecycle/grace policy, and creates the matching usage period. `server/billing/entitlement_service.py` exposes feature/model/file decisions and exact reservation quantities without mutating counters.
- `server/billing/metering_service.py` atomically reserves, supplements, partially settles, releases, and expires allowance reservations inside a caller-owned transaction. Idempotency keys are scoped to a billing account, counter rows are locked in deterministic order, terminal transitions are repeat-safe, and the API runs concurrency-safe stale cleanup at startup and every five minutes by default while heartbeat activity protects live requests.
- `BILLING_ENABLED=false` is the safe default for Stripe hosted billing. Cortex-issued Plus/Pro grants still apply; accounts without a valid grant resolve to Free. It keeps Stripe lazy and makes Checkout, Portal, and webhook routes return `503 billing_not_configured` without requiring Stripe credentials. A local-only `DEV_SUBSCRIPTION_PLAN` override is ignored when billing is enabled or the runtime is not explicitly local/development.
- When billing is enabled, startup validates the Stripe secret key, webhook signing secret, every paid plan's configured Price ID, all backend redirect URLs, and any explicit API version. The client can submit only `plan_code` and `billing_period`; extra Price, amount, currency, Customer, or redirect fields are rejected. `POST /v1/billing/webhook` verifies the exact raw body and `Stripe-Signature`, stores provider event IDs idempotently, synchronizes Checkout/subscription/invoice lifecycle events, rejects stale snapshots, applies payment grace and cancellation policy, and creates each paid usage period once. Verified webhook state is authoritative for Stripe access; Cortex grants provide an independent fallback.
- DB-mode Ask, Compare, Optimize, Cortex Analysis, and attachment-backed generation use one short reserve/settle/release lifecycle. A preflight estimate reserves AI credits before provider work; settlement charges each successful model by actual input/output tokens and configured multipliers, then releases the unused estimate. If actual usage is higher, settlement atomically supplements the reservation when capacity exists; otherwise it bills only the authorized amount, never makes the wallet negative, and records the unbilled difference as a reconciliation adjustment without discarding the answer. Every resulting model, research, and adjustment ledger item carries the privacy-policy-sanitized initial user query in metadata; metadata-only storage omits it. React submissions also carry a display-only `credit_activity_id`, shared by Prompt Optimizer and the following Ask/Compare request, so ledger presentation can group one user action without changing authorization or settlement. Advanced Web Search reserves the normal two-credit Tavily call (10,000 Cortex credits), then settles `Tavily API credits used x 5,000 Cortex credits` once per fresh provider call. Tavily usage omitted by the provider falls back to two API credits and is marked estimated; cache/session reuse adds no research charge. Compare shares one retrieval across its targets, and Cortex reuses existing Compare research without a second retrieval charge. Improve Prompt reserves every configured provider attempt. Smart Routing ranks plan-allowed candidates, removes unaffordable candidates, reserves the first appropriate affordable candidate using materialized input plus the clamped output ceiling, and requires supplemental authorization before a more expensive fallback. No percentage buffer is added.
- Ask and Compare resolve consumer-credit multipliers from each successful response's canonical requested model, while retaining provider-returned served and pricing identities for audit and provider-cost calculation. The same canonical identity drives each response card's AI-credit value and the aggregate Compare total, keeping inline statistics reconciled with the credit ledger when a provider returns a versioned served-model snapshot. A versioned provider snapshot ID therefore cannot suppress sibling Compare ledger rows. If finalization still fails, the route immediately releases and unregisters the reservation so the heartbeat worker cannot keep credits indefinitely reserved.
- The canonical catalogue and official source evidence were refreshed on `2026-07-31`.
- The selectable catalogue contains 21 models. Claude Sonnet 4.6 is an `advanced` model available to Plus and Pro; Claude Opus 4.5 and Opus 4.6 are `premium` models available to Pro. The three models remain `ACTIVE` in Anthropic's lifecycle table. Current standard API rates are $3 input/$15 output per million tokens for Sonnet 4.6 and $5 input/$25 output for both Opus versions; cache prices are retained in the same registry records.
- Validation command:
```bash
python -m pytest tests/test_pricing.py tests/test_registry_pricing_alignment.py tests/test_model_registry_capabilities.py -q
python -m pytest tests/test_subscription_plan_catalog.py -q
python -m pytest tests/test_billing_metering.py tests/test_billing_entitlements.py tests/test_billing_repository.py -q
python -m pytest tests/test_stripe_billing.py tests/test_stripe_webhooks.py -q
```

## Run

Run the full local app (FastAPI API + React/Vite frontend):
```bash
python run_app.py
```

Useful URLs:
- React frontend: `http://127.0.0.1:5173/`
- React pricing: `http://127.0.0.1:5173/pricing`
- React plan and billing management: `http://127.0.0.1:5173/account/billing`
- API health: `http://127.0.0.1:8000/health`
- Swagger: `http://127.0.0.1:8000/docs`

For local browser session bootstrap, keep `ENABLE_DEV_SESSION_LOGIN=true` in `.env` or pass:
```bash
python run_app.py --enable-dev-login
```

`run_app.py` starts FastAPI with `SERVE_FRONTEND=false`, runs `npm run --prefix frontend-react dev`, and points both Vite's `/v1`, `/auth`, and `/runtime-config.js` proxy plus `FRONTEND_RUNTIME_API_BASE` at the selected API host/port.
Before launching either process, the runner verifies that both requested ports are available and reports a clear error when another process owns one. On Windows, shutdown terminates the complete FastAPI/Vite process trees so failed startup or `Ctrl+C` does not leave an orphaned Vite listener.

### IntelliJ/PyCharm local subscription profiles

Create one Python run configuration, then duplicate it four times:

- Script path: `$ProjectFileDir$/run_app.py`
- Working directory: `$ProjectFileDir$`
- Python interpreter: the repository virtual environment
- Environment variables: none required for the plan selection

Use one Program arguments value per configuration:

```text
--subscription-plan free
--subscription-plan plus
--subscription-plan pro
--subscription-plan unrestricted
```

Name the configurations `CortexAI - Free`, `CortexAI - Plus`, `CortexAI - Pro`, and `CortexAI - Unrestricted`. The selected profile forces local runtime mode, disables Stripe billing, enables the local session bootstrap, and takes precedence over conflicting billing/plan values from `.env`. It is rejected when either server binds to a non-loopback host.

`unrestricted` keeps the normal backend entitlement and metering path active but presents `Local Unrestricted` with Pro model/features and very high monthly allowances. Authentication, provider/model capabilities, the global five-attachment/20 MB safety ceiling, object-storage validation, and provider API keys still apply. Run without `--subscription-plan` to use the normal `.env`/Stripe configuration.

Run the API server by itself:
```bash
python run_server.py --reload
```

Useful URLs:
- Health: `http://127.0.0.1:8000/health`
- Swagger: `http://127.0.0.1:8000/docs`
- React frontend when its build is mounted: `http://127.0.0.1:8000/`

### Run the React Frontend Through FastAPI

Build the React/Vite app first, then point the FastAPI static mount at the generated `dist` folder:

```powershell
npm ci --prefix frontend-react
npm run --prefix frontend-react build
$env:FRONTEND_DIR=(Resolve-Path .\frontend-react\dist).Path
python run_server.py --reload
```

When `FRONTEND_DIR` is unset, FastAPI uses `frontend-react/dist`. Set the variable explicitly in deployments that place the compiled assets elsewhere.

Do not add React packages to `requirements.txt`; keep React dependencies in `frontend-react/package.json` and `frontend-react/package-lock.json`.

### Run Components Independently

API only (disable static frontend mount):
```bash
# PowerShell
$env:SERVE_FRONTEND="false"
python run_server.py --reload
```

Frontend only (preview a production build):
```bash
npm run --prefix frontend-react build
npm run --prefix frontend-react preview
```

React development server (hot reload, with `/v1`, `/auth`, and `/runtime-config.js` proxied to the API on port 8000):
```bash
npm run --prefix frontend-react dev
```

When launched by `run_app.py`, Vite reads `CORTEX_API_PROXY_TARGET` from the runner so custom `--api-host` / `--api-port` values stay aligned with the frontend proxy.

For this split local workflow, run the API separately with `SERVE_FRONTEND=false`:
```powershell
$env:SERVE_FRONTEND="false"
python run_server.py --reload
```

Frontend runtime config (`/runtime-config.js`):
- In monolith mode (`SERVE_FRONTEND=true`), FastAPI serves `/runtime-config.js` dynamically with `Cache-Control: no-store`.
- `apiBase` defaults to current request origin. Override with `FRONTEND_RUNTIME_API_BASE` when API is on another origin.
- Browser dev-session bootstrap flag:
  - defaults from `ENABLE_DEV_SESSION_LOGIN`
  - optional explicit frontend override: `FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN`
  - forced off in production-like runtimes (`APP_ENV/ENVIRONMENT/ENV = prod|production`)
- The frontend completes Cognito/local dev-session bootstrap before fetching session-scoped startup data (`/v1/providers`, `/v1/models`, and `/v1/history`). If Cognito is enabled and no user session is present, React shows a sign-in gate instead of calling those session-scoped startup endpoints; local development history still appears on load after dev-session bootstrap.
- Optional browser token for local bootstrap: `FRONTEND_RUNTIME_DEV_SESSION_LOGIN_TOKEN`
- For static-only hosting (nginx, CDN, etc.): copy `frontend-react/runtime-config.example.js` to `runtime-config.js` at the deployed React origin and set `window.CORTEX_RUNTIME_CONFIG.apiBase`.
- For standalone React production hosting, make `/runtime-config.js` available at the same origin as the React app and route `/v1/*` plus `/auth` to the API origin. The current React client uses same-origin relative API paths, so a CDN/load-balancer/nginx rule should proxy those paths to the FastAPI service.
- Composer keyboard behavior: `Enter` sends the prompt, `Shift+Enter` inserts a new line.
- React startup waits for Cognito/local dev-session bootstrap before fetching `/v1/models` and `/v1/history`; signed-out Cognito users see a sign-in gate instead of the Ask/Compare workspace, and background history failures do not surface as the primary chat error banner. It persists the active `session_id` in browser storage and restores that transcript after reloads, Chrome tab refreshes, mobile/desktop browser resume, and same-browser reauth.
- React records non-sensitive browser lifecycle diagnostics (`boot`, `pagehide`, `beforeunload`, `visibilitychange`, long tasks, and frontend errors) in a short local buffer and posts them to `/v1/client-diagnostics`, where they appear as `frontend.diagnostic` structured logs. This distinguishes production page reloads, Chrome tab discards, back/forward cache restores, long main-thread stalls, and render/runtime failures.
- The React sidebar uses a compact navigation rail with subtle Ask/Compare/Usage/AI credits/Models and current-session states. On desktop, an icon-only control at the top right collapses the sidebar to a narrow action rail and expands it back to the full history view; the bottom-left session tile is status-only once a session is active, while explicit logout stays in the account menu. Mobile continues to use the separate Ask/Compare/History bottom navigation, with `Usage & insights`, `AI credits`, and `Models` reached from the account menu. The mobile Usage route uses the compact 2x2 KPI grid, Smart-pick leaderboard strip, session-mode bar, and Ask/Compare/History footer navigation. The expanded desktop `Recent` list displays up to the 100 newest grouped chat threads under Today, Yesterday, or month-day labels and renders each `session_id` as a single 36px row with a compact 11.5px ellipsized title and a narrow `MODE · time` caption, without a redundant leading mode glyph, so substantially more of the identifying prompt remains visible. Hover or keyboard focus swaps the caption for a two-item Rename/Delete menu; Rename persists `sessions.title`, while Delete keeps the short in-row confirmation and removes every persisted row in that thread. Model names, turn counts, and token counts stay hidden, search still covers every persisted turn plus the renamed title, and deleting the active thread starts a clean chat. The collapsed desktop rail and separate mobile History screen remain unchanged.
- Selecting a history thread reloads its complete persisted transcript. Ask rows become chronological turns, while Compare target rows sharing a `request_group_id` are reconstructed as one multi-model turn.
- An explicit fresh sign-in starts an empty new chat session instead of appending the first prompt to the previously active thread. Browser refreshes, tab resume/reload, same-browser reauth, and explicit History selections continue the selected thread.
- React Ask and Compare turns send `context.session_id`, bounded `conversation_history`, and `new_session` so follow-ups continue the selected thread and New Chat starts a new backend session.
- React Ask starts with the `Web` source toggle enabled for new page sessions, and React Compare starts with `With sources` enabled; users can turn either off for the current page session. Compare mode streams `/v1/compare/stream` events into each model response column.
- React Compare keeps every selected response visible in a responsive grid without a horizontal response rail on desktop and tablet widths: three columns on wide desktop, two columns at tablet widths, and stacked tall cards at the app's tablet/mobile shell breakpoint. Phone-sized mobile restores the segmented model switcher, shows one selected response card at a time in natural page flow, and elevates the switcher into a frosted sticky bar with provider-tinted active cues while keeping every model pill horizontally stable during scroll transitions.
- Compare card headers and action footers remain fixed inside each desktop/tablet panel while only the answer body scrolls. The transcript retains bottom breathing room above the persistent composer so the input area does not compress the reading workspace.
- React Compare renders submitted prompts with the same right-aligned user bubble used by Ask mode. Aggregate result totals remain in a separate compact row, while model cards keep friendly names, exact API model IDs, and compact icon actions.
- React Ask and Compare use one rounded composer shell with a borderless textarea that starts at one line and auto-grows to a bounded height. Attachments stay above the compact routing controls and fixed-size send action; mode changes remain in the app navigation rather than a redundant composer switch. Compare model selectors use a subtle opposing-arrows connector between active models: a bordered medallion on desktop and a compact borderless glyph on mobile. The selector row scrolls horizontally rather than compressing model names when a third model exceeds the mobile width. Its shared provider-first picker is rendered in a viewport-positioned body portal so overflow containers cannot clip it. Fine-pointer desktop users hover a provider to reveal an adjacent model panel immediately; moving outside the picker dismisses that preview after a short intent delay, while click remains a fallback. Touch/mobile users tap a provider to enter its compact model list and use the in-menu Back action to return to providers.
- The React composer shell uses a transparent structural border and soft elevation instead of a visible rectangular outline. Textarea focus removes the browser outline and strengthens the shell shadow without changing layout on desktop or mobile.
- React Smart, Web/With sources, and Improve chips expose concise guidance through accessible tooltips. Enabled chips use a theme-aware high-contrast fill, label, and accent ring so their state remains clear in light and dark themes. Compare's With sources and Improve controls use the same background, border, label, and shadow treatment whenever they share the same toggle state. Tooltips open on pointer hover or keyboard focus and stay within narrow viewports. On touch screens, the same tap toggles the chip and shows its tooltip for two seconds.
- On mobile answer screens, the follow-up composer rests as a docked pill above the fixed Ask/Compare/History navigation. The pill shows the current routing context and opens the bottom sheet on tap; that same tap focuses the textarea and places the cursor at the end of any draft so typing can begin immediately. The expanded sheet keeps attachment chips, routing controls, and the fixed-size send action clear of the tab bar. Answer transcripts reserve enough bottom scroll clearance for response copy, regenerate, and feedback actions to remain reachable above the dock.
- Mobile exposes one persistent square-pen action in the header for starting a new session from Ask, Compare, or History. It cancels active generation, clears the current thread, returns to chat, and preserves the selected mode; History does not duplicate this action.
- React reads the backend upload flags from `/runtime-config.js`. With `ATTACHMENTS_DIRECT_UPLOAD_ENABLED=true`, it validates the complete selection from `/v1/entitlements`, renders each file immediately as Preparing/Uploading/Processing/Ready, requests metadata-only `/v1/files/upload-intents`, and transfers bytes directly to the returned S3 URL with `XMLHttpRequest` progress. At most two S3 transfers run concurrently; siblings remain usable when one fails, Retry obtains a fresh intent after upload failure, removal aborts the active request and calls `DELETE /v1/files/{file_id}`, and Send remains disabled until every selected file is `ready`. When direct mode is off and `ATTACHMENTS_LEGACY_PROXY_UPLOAD_ENABLED=true`, the same task UI temporarily falls back to multipart `/v1/files/upload-batch`. Ask/Compare payloads still contain only ready Cortex file IDs.
- Compare response cards use compact footers while the compare summary bar carries success/error counts and aggregate AI-credit usage. Completed duration and AI-credit metrics render as a packed mono strip; token counts remain available to the API and frontend data layer but are not displayed on Ask or Compare results. The fastest winner gets the success tint, with its text label shown only where desktop space allows.
- Mobile and desktop response cards show completed duration and AI-credit usage directly in the header without a run-details chevron. Pending and failed cards keep their muted elapsed/status line visible on mobile and desktop. Response-card duration uses the same UI-observed elapsed timing when live timestamps are available and falls back to API `latency_ms` for restored rows.
- The latest completed Ask response can render deterministic suggested follow-up chips when the assistant ends with clear offered options, one concrete follow-up offer, or a quoted follow-up query. The row lives inside `ResponseCard` below the answer body and above the response action toolbar; tapping a chip sends that chip text as a new follow-up turn through the normal chat streaming path while skipping the Improve prompt-optimization flow and preserving any composer draft.
- Ask and Compare response headers use the same shared provider-logo and model-presentation resolver as the model picker. Failed or unavailable logo assets retain a provider-initial fallback instead of leaving a blank header.
- Empty streaming cards render an independent request-aware loading state with a subtle sparkle and skeleton lines. Ask, Compare, source-enabled, and prompt-improved turns use contextual copy, and the loading block disappears when that card receives its first token or error.
- Smart Ask pending cards remain model-neutral because the chat stream `start` event contains only a pre-runtime routing preview. They show `Smart routing` while waiting and adopt the authoritative provider/model from `response_done`.
- Response card controls render as a minimal icon row for copy, regenerate, and feedback actions. Copy shows a brief visible success confirmation in the toolbar. Regenerate refills the clicked response card in place through the existing chat streaming path for that response model, including when the clicked card came from Compare mode. It preserves the original turn's source-enabled flag, so source-backed regenerations run a new backend research/search pass.
- Response sources render inline as publisher-name citation pills derived from `web_source_items`; grouped markers such as `[1][2][3]` collapse into one pill with a preview card listing each linked source. On desktop, hovering the pill opens a viewport-contained preview directly beside the pill and leaving the hover area closes it; on mobile, tapping still opens the bottom sheet. The external-link icon opens the first cited source directly.
- Response Markdown rendering preserves explicit ordered-list numbering even when numbered items are separated by explanatory text.
- React response Markdown includes inline citation pills with tap/click source previews, direct external source-icon links, blockquote callout styling, styled code blocks with copy controls, GFM tables, and sanitized provider error states. Tables stay inside a horizontal response-card scroller on desktop and become labelled stacked rows on mobile.
- Streaming Ask and Compare cards progressively render buffered Markdown instead of raw text.
- Whenever a new Ask or Compare turn is submitted, React performs one smooth reveal of that new turn so the submitted question becomes visible even when the user was viewing an older turn.
- Streaming responses do not auto-follow generated text as it grows, and the transcript no longer renders a floating down-arrow jump control.
- React frontend uses the Alabaster Minimal workspace shell: a quiet 272px desktop navigation rail that can collapse to a narrow icon rail, top Ask/Compare tabs with theme-aware high-contrast active and inactive text, prompt starter landing, horizontal compare canvas, unified bottom composer, visually aligned mobile Ask/Compare/History navigation, and light/dark theme tokens switched from the account menu.
- The empty Ask workspace introduces CortexAI as a place for answers, file analysis, content work, and model comparison, then offers four actionable prompts covering debugging, document summaries, writing refinement, and file analysis. Example selection fills the composer without submitting automatically.
- The empty Compare workspace explains the ask-once, multi-model workflow and highlights accuracy, depth, speed, tone, and usefulness as comparison dimensions. Three responsive examples cover system design, debugging, and model review; selection fills the Compare composer without changing selected models or submitting.
- Frontend model selectors fall back to a bundled display catalog when `/v1/models` is unavailable, so local UI controls remain usable while backend/session setup is incomplete.
- The React AI-credit destination is available at `/credits`. It owns the authenticated account's unified balance, billing-period reset date, and newest model/search/feature charges from `/v1/entitlements` and `/v1/credits/transactions`. React groups itemized API rows by `activity_id` into one activity card, including Prompt Optimizer and the following Ask/Compare request, and shows the original pre-optimization question, action label, total credits, and timestamp first. When an optimizer charge has a following answer in the same activity, the collapsed breakdown combines both into one `Final optimized ... answer` line whose description explicitly includes the optimizer attempt count and final answer generation; a standalone optimizer-only activity remains labelled `Prompt Optimizer`. Compare, Cortex Analysis, and Web Search remain understandable within the requested breakdown, optimizer retries are aggregated, and zero-credit reconciliation rows do not create user-facing noise. Older activity created before grouping/query tracking falls back to its request ID and an unavailable-question message. Provider token/cost analytics and CSV export remain under `/usage`.
- The React Models destination is available at `/models`. Its production rows are built from the session-scoped `/v1/models` response, so the backend registry owns model names, availability, lifecycle, current token prices, and official evidence links. `frontend-react/src/config/models.data.json` remains only the offline presentation/filter fallback. Desktop reaches Models from the sidebar below AI credits, mobile reaches it from the account sheet, and the screen filters by task/search and shows the official price source plus its verification date in expanded details.
- Compare response cards render as tall equal-height model panels with fixed model headers/actions and independently scrolling readable Markdown bodies.
- The composer keeps active compare models as removable chips and exposes Add Model from the same options row without separating the prompt, attachments, feature controls, or send action into independent boxes.

## Authentication

Most `/v1/*` routes accept API key, gateway bearer token, or session cookie auth.
Most `/v1/*` routes accept API key, bearer token, or session cookie auth (route-dependent).

Session-scoped routes require signed-in identity auth (not API key):
- `/v1/chat`
- `/v1/chat/stream`
- `/v1/compare`
- `/v1/compare/stream`
- `/v1/compare/{request_group_id}/analysis`
- `/v1/compare/analysis-runs`
- `/v1/files/*`
- `/v1/providers`
- `/v1/models`
- `/v1/optimize`
- `/v1/history`
- `/v1/history/{entry_id}`
- `/v1/history/session/{session_id}`

Accepted auth for session-scoped routes:
```http
Authorization: Bearer <gateway-bearer-token>
```
or `cortex_session` cookie.

API-key-only auth on session-scoped routes is rejected with `403` (`session_auth_required`).

Local development helper:
- `POST /v1/auth/dev-login` can mint a local `cortex_session` cookie when `ENABLE_DEV_SESSION_LOGIN=true`.
- It is disabled by default and rejected when runtime env is production-like (`APP_ENV/ENVIRONMENT/ENV` = `prod|production`).
- Optional token guard: set `DEV_SESSION_LOGIN_TOKEN`, then send it as `X-Dev-Login-Token`.

Optional request correlation:
```http
X-Request-ID: <custom-id>
```
The browser frontend now sends `X-Request-ID` on Ask/Compare/Optimize calls. For
streaming requests, frontend console diagnostics include the client request id
and the server-returned request id when a stream read fails.
Browser lifecycle diagnostics post to `POST /v1/client-diagnostics` and are logged
as `frontend.diagnostic` events without prompt or response content.
For EC2/Linux operational logging setup and event catalog, see `docs/LOGGING.md`.
For full AWS EC2 troubleshooting steps (CloudFront/WAF/origin correlation and Linux commands), see `docs/runbooks/aws-ec2-logging.md`.

Integration debug snapshot:
```bash
curl -H "X-API-Key: dev-key-1" http://127.0.0.1:8000/v1/whoami
```
Returns owner IDs (in DB mode), active storage policy, baseline model, and guardrail config snapshot.
Response includes:
- `api_key_id`, `user_id`, `plan_tier` (`plan_tier` is a compatibility display field)
- `billing.plan_code`, `billing.status` (database-backed runtime)
- `storage_policy`, `redact_pii`
- `baseline.provider`, `baseline.model`, `baseline.source`
- `rate_limits.requests_per_minute`, `rate_limits.daily_cap_scope`
- `breakers.failure_threshold`, `breakers.window_seconds`, `breakers.cooldown_seconds`

## API Endpoints

- `GET /health`
- `GET /health/runtime`
- `GET /v1/providers`
- `GET /v1/models?provider=<optional>&enabled_only=true|false`
- `POST /v1/files/upload`
- `POST /v1/files/upload-batch`
- `POST /v1/files/upload-intents`
- `POST /v1/files/{file_id}/complete`
- `GET /v1/files/{file_id}`
- `DELETE /v1/files/{file_id}`
- `POST /v1/client-diagnostics`
- `POST /v1/chat`
- `POST /v1/chat/stream`
- `POST /v1/compare`
- `POST /v1/compare/stream`
- `POST /v1/compare/{request_group_id}/analysis`
- `GET /v1/compare/analysis-runs?session_id=<id>`
- `GET /v1/compare/analysis-runs?request_group_id=<id>`
- `POST /v1/optimize`
- `GET /v1/history`
- `PATCH /v1/history/session/{session_id}`
- `DELETE /v1/history/{entry_id}`
- `DELETE /v1/history?session_id=<optional>`
- `GET /v1/whoami`
- `GET /v1/entitlements`
- `GET /v1/credits/transactions`
- `POST /v1/billing/estimate-generation`
- `POST /v1/billing/checkout-session`
- `POST /v1/billing/portal-session`
- `POST /v1/billing/webhook`
- `GET /v1/usage/summary?from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /v1/usage?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=day|provider|model`
- `GET /v1/savings?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=day|provider|model`
- `GET /v1/usage/export?format=csv&from=...&to=...&group_by=...`
- `GET /v1/savings/export?format=csv&from=...&to=...&group_by=...`
- `POST /v1/byok`
- `GET /v1/byok/status`
- `DELETE /v1/byok?provider=<provider-id>` (or omit provider to delete all)
- `GET /v1/admin/request-groups/{request_group_id}/failed-attempts`
- `GET /v1/auth/cognito-config` (no auth; returns public Cognito config for frontend)
- `POST /v1/auth/dev-login` (local-development helper; gated by env flags)
- `POST /v1/auth/logout` (clears the session cookie)

### Subscription entitlement snapshot

`GET /v1/entitlements` accepts the same API-key, Cognito bearer, or signed-session authentication as `/v1/whoami`. On first access it lazily creates the authenticated user's billing account, resolves the effective plan, creates the current usage period, and ensures the unified `ai_credits` counter exists. Free periods use UTC calendar-month boundaries; Stripe periods use the stored provider period; Cortex grants use monthly UTC anniversaries of their start, clipped to grant expiry.

The response contains:

- `plan`: effective code/display name, lifecycle status/source, renewal/reset time, cancellation-at-period-end state, and optional grace deadline
- `features`: Compare, research, prompt improvement, file analysis, usage export, saved history, and model-catalog access
- `model_access.allowed_billing_classes`: `economical`, `standard`, `advanced`, and/or `premium`
- `limits`: server-owned `max_files_per_request` and `max_file_bytes` values for the effective plan
- `allowances.ai_credits`: `used`, `reserved`, `limit`, and nonnegative `remaining`
- `period.starts_at` / `period.ends_at`

Lifecycle resolution is conservative: `active` grants Stripe paid access for a valid stored period; `trialing` does not grant paid access; `past_due` grants it only through `grace_until`; cancel-at-period-end access lasts only until the stored period end; `unpaid`, `incomplete`, `incomplete_expired`, `paused`, expired cancellations, unknown plans, and unknown statuses fall back to a valid Cortex grant, then Free. No Stripe IDs or provider secrets are exposed by this endpoint.

The monthly plan budgets are Free 100,000, Plus 1,000,000, and Pro 3,000,000 AI credits. Their request-rate limits are respectively 5, 15, and 30 requests per minute. File limits are 1 × 10 MB, 3 × 20 MB, and 5 × 20 MB per request. Plus costs USD 6.99/month and Pro costs USD 12.99/month.

`GET /v1/credits/transactions?limit=100&offset=0` returns the authenticated account's immutable, itemized reconciliation history. Each row includes `activity_id` for display grouping plus a nullable `query`, derived from the privacy-policy-sanitized initial query stored with the transaction, and identifies the operation and model/research item, input/output tokens and credits, fixed credits, total credits, provider cost, pricing version, whether usage was estimated, metadata such as file context or prompt improvement, and its timestamp. `activity_id` is display-only and falls back to `request_id` for older rows. Metadata-only storage omits the query, PII redaction applies before storage, and rows created before query tracking return `query: null`. Research rows record `provider_credits_used` and `cortex_credits_per_provider_credit` so the formula-based charge is auditable.

`plan_tier` remains on `/v1/whoami` for backward compatibility and is now populated from the effective plan display name in database mode. New consumers should use `/v1/entitlements` and `billing.plan_code` instead.

Ask and Compare enforcement is backend-authoritative in database mode. Feature/model denials happen before provider execution and return structured `403`; insufficient credits return `402 insufficient_credits` with required and remaining credit values; request-rate exhaustion returns `429`; unsafe billing configuration returns a provider-safe `500`. Compare accepts two or three targets at the platform boundary; Free and Plus allow two, while Pro allows three. Streaming reservations are finalized inside the response generator: successful output settles actual credits, disconnects before output release them, performed research still settles once, and Compare partially settles only successful targets.

Smart Ask receives `allowed_billing_classes` and an affordability-filtered model list as routing constraints. The router still chooses by its independent `T0`-`T3` tier logic, but candidates outside the effective plan's access categories or remaining wallet are removed. Preflight reserves the first appropriate affordable candidate using materialized input, bounded tool context, and the full clamped output ceiling; a more expensive fallback must supplement that reservation before invocation. Settlement charges only billable provider usage.

### Stripe Checkout and Customer Portal

`GET /v1/billing/plans` is public and returns the display-safe, server-owned Free/Plus/Pro catalogue: USD monthly prices, the Plus recommendation marker, model-class access, feature availability, core monthly allowances, and a boolean `billing_enabled` availability flag. The flag lets public pricing UI disable hosted billing without probing Checkout; it never returns Stripe Price IDs, environment-variable names, Customer IDs, secrets, or provider objects.

`GET /v1/billing/subscription` is session-scoped and returns the authenticated user's effective plan/status, provider label, current period, cancellation state, and whether the account has a manageable Stripe subscription. It applies the same conservative lifecycle resolution as `/v1/entitlements` and never exposes the provider subscription ID.

`POST /v1/billing/checkout-session` is session-scoped and accepts only:

```json
{"plan_code":"plus","billing_period":"monthly"}
```

The backend resolves the paid plan through `config/subscription_plans.yaml`, reads its Price ID from the named server environment variable, creates or reuses the authenticated user's Stripe Customer, and returns a short-lived `checkout_url`. It never accepts a Price ID, amount, currency, Customer ID, success URL, or cancel URL from the browser. Customer creation and Checkout use stable account-scoped idempotency keys. If the account already has any provider-live subscription, the route creates a Portal session instead and returns `destination: "portal"` without creating another Checkout subscription.

`POST /v1/billing/portal-session` accepts an omitted or empty strict body and returns `portal_url` only for the persisted Stripe Customer. Its return URL is server configuration. An account without a Customer receives `409 stripe_customer_required`; Stripe failures become a provider-safe `502 billing_provider_unavailable`. Neither route stores hosted URLs.

The hosted-session routes do not write paid state. `POST /v1/billing/webhook` is intentionally unauthenticated by user credentials and instead requires Stripe's signature over the exact raw body. It accepts the required Checkout, subscription, and invoice lifecycle events, records valid unknown events as ignored, returns `400 invalid_webhook_signature` before persistence for invalid payloads/signatures, and returns a non-2xx response when verified processing fails so Stripe can retry. Duplicate provider event IDs are harmless; failed rows can be retried; older subscription snapshots cannot overwrite newer ones. Subscription Price IDs are reverse-mapped through server configuration, never event metadata.

Stripe usage periods follow Stripe period boundaries. Repeated events and multiple lifecycle events for the same period reuse the existing period row and preserve settled counters; plan changes with the same period start update that row without resetting usage. `invoice.payment_failed` grants the configured grace only when a fresh authoritative Subscription remains `past_due`; cancellation resolution downgrades through the existing conservative lifecycle service without deleting account or conversation data. See `docs/runbooks/stripe-billing.md` and `docs/runbooks/subscription-incidents.md` for configuration, replay, and recovery guidance.

Cortex-issued access is managed through `scripts/manage_subscription_grant.py`, with required actor, reason and expiry. Resolution is guarded local override, valid Stripe paid subscription when enabled, valid Cortex grant, then Free. Grants use `subscription_grants` and monthly `usage_periods.subscription_grant_id` without changing the plan catalogue or unified-credit accounting. Apply `20260905_add_subscription_grants.sql` during a coordinated API/worker cutover before this build. See [Cortex grant operations](docs/runbooks/subscription-grants.md) for commands, migration order, monthly resets and rollback constraints.

### React subscription data layer

The React client owns subscription transport in `frontend-react/src/api/billing.ts` and `frontend-react/src/api/entitlements.ts`, structured error normalization in `frontend-react/src/subscription/subscriptionErrors.ts`, and auth-aware in-memory state in `frontend-react/src/hooks/useSubscription.ts`. Signed-out users may load only the public plan catalogue; subscription and entitlement requests wait for authentication bootstrap and require a logged-in user. Billing state is never read from `localStorage`.

`useSubscription` exposes effective entitlements, current subscription state, plan data, allowance/model/feature helpers, explicit reload, Checkout and Portal actions, and bounded Checkout-success polling. A successful Checkout redirect is treated only as a refresh hint: the hook reports `confirming` until `/v1/entitlements` returns a paid effective plan with `source=stripe`, then `confirmed`; if the webhook remains delayed after ten bounded attempts, it reports `pending` so future billing UI can show a safe refresh action. React follows only validated HTTPS hosted URLs returned by the backend and never calls Stripe directly.

The React consumer plan surfaces are `/pricing` and `/account/billing`. Pricing renders the server catalogue, current-plan state, monthly allowances, billing-disabled state, and auth/lifecycle-aware Checkout or Portal actions. Billing renders effective plan status, renewal or cancellation dates, payment-grace warnings, and allowance progress. Signed-out users can read public pricing but must authenticate for account billing. The shared account menu shows only the plan label, past-due state, and Upgrade/Manage action; it intentionally omits detailed counters. Paid access is displayed only from the webhook-synchronized subscription and entitlement responses, including after `?checkout=success`.

Backend APIs, PostgreSQL counters, ledger entries, reservations, and React state keep AI credits in raw integer metering units. Customer-facing React surfaces divide those values by 1,000 through `frontend-react/src/utils/aiCredits.ts`, so the Free/Plus/Pro monthly allowances display as 100/1,000/3,000 AI credits while their server values remain 100,000/1,000,000/3,000,000. The same presentation rule applies to balances, plan/billing meters, response and Compare usage, itemized activity, insufficient-credit messages, and Work budgets. Outgoing request payloads remain in raw units; the formatter never changes billing authority or arithmetic.

Composer and catalogue gating is explanatory UX over the same backend authority. Manual model pickers and the Models screen consume live `/v1/models` catalogue, billing-class, lifecycle, and credit-usage metadata; disallowed models remain visible with the server-derived required plan, while missing live billing metadata is shown conservatively as unavailable. Ask waits for both the model catalogue and effective `/v1/entitlements` snapshot before initializing its manual selection: when Smart routing is turned off, Free defaults to `openai:gpt-5.6-luna`, Plus to `claude:claude-sonnet-4-6`, and Pro to `openai:gpt-5.6-terra`. Compare also waits for effective entitlements before filling empty initial model slots, then chooses those defaults only from billing classes allowed by the current plan. These rules change initial values only: the complete model offering remains visible, and valid existing/manual selections are preserved. Free/Plus users see a Pro action instead of silently adding a third Compare target. Web, Improve, file count/size, AI-credit balance, and CSV export use the current `/v1/entitlements` snapshot for preflight messaging, but every request is still enforced and reserved by the backend.

Structured subscription denials open an accessible contextual dialog and keep the current prompt and attachments intact. The composer clears only after the stream is accepted; an HTTP `model_not_in_plan`, `feature_not_in_plan`, `insufficient_credits`, or `subscription_payment_required` response removes the optimistic placeholder and restores no client-side authority. Existing premium history is never filtered or deleted after downgrade. The dedicated AI credits page renders the unified balance/reset date and recent itemized credit activity, while Usage & insights stays focused on provider-usage analytics and export; both layouts remain responsive on narrow screens. Response cards display actual AI credits, and Compare also displays the aggregate total.

### Cognito (Gmail) sign-in

To enable "Sign in with Google" via Amazon Cognito:

1. **AWS Cognito**: Create a User Pool, add an App client, configure the Cognito domain, and add Google as an identity provider (see [AWS docs](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-social-idp.html)).
2. **Environment** (see `.env.example`):
   - `COGNITO_USER_POOL_ID` – User pool ID (e.g. `us-east-1_xxxxxxxxx`)
   - `COGNITO_CLIENT_ID` – App client ID
   - `COGNITO_REGION` – AWS region
   - `COGNITO_DOMAIN` – Hosted UI base URL (e.g. `https://your-prefix.auth.us-east-1.amazoncognito.com`)
   - `COGNITO_REDIRECT_URI` – **Required in production**. Must exactly match the callback URL registered in the Cognito App Client (e.g. `https://app.example.com/auth`). Without this, the backend computes the redirect URI from the incoming request; behind CloudFront or an ALB the scheme may be resolved as `http://` instead of `https://`, causing a token-exchange failure and an auth redirect loop that looks like an automatic page refresh.
3. **Frontend**: Load the app; signed-out Cognito users see a `Sign in to use CortexAI` gate in the workspace and the React top-right account icon remains a secondary account menu. Cognito guests can `Sign in`, the theme switch toggles the React `data-theme` between light and dark and persists the browser preference, and `Log off` remains available as a session-clear fallback even when the icon is labelled `Guest account`. Log off posts to `/v1/auth/logout`, clears the active React session/history state, then redirects to the Cognito `logoutUrl` when the backend provides one. After sign-in, sessions/history are tied to the authenticated user identity.

`/v1/models` returns selectable models by default; `enabled_only=false` includes retained lifecycle/alias records for compatibility and history. Each model includes billing, catalogue, price-evidence, and attachment metadata:
- `billing_class` / `access_category` (`economical`, `standard`, `advanced`, or `premium`; independent of routing `tier`)
- `input_credit_multiplier`, `output_credit_multiplier`, `credit_usage_label`, and `credit_pricing_version`
- `display_name`, `description`, `selectable`, `release_status`, `lifecycle_status`, `replacement_model`, and `aliases`
- `pricing_model`, current input/output/cached-input prices, `pricing_rule_id`, `pricing_effective_from`, `pricing_source_url`, `lifecycle_source_url`, and `source_verified_at`
- `max_output_tokens` and supported `reasoning_modes`
- `supports_image_input`
- `supported_attachment_mime_types`
- `max_attachment_bytes`
- `max_attachments_per_request`

## Attachment Upload Contract (Current)

The backend supports an opt-in direct-to-S3 flow that keeps attachment bytes out
of Cortex and its WAF-protected origin:

1. The browser sends metadata only to `POST /v1/files/upload-intents`.
2. Cortex validates the whole batch, creates `uploaded_files` rows in
   `uploading`, and returns one short-lived presigned POST per file.
3. The browser submits every returned form field plus the file directly to the
   returned S3 URL.
4. The browser calls `POST /v1/files/{file_id}/complete`. Cortex performs S3
   `HeadObject` against the server-owned bucket/key and verifies exact size,
   exact MIME type, and `x-amz-meta-cortex-file-id` before moving the row to
   `ready` or `processing`.

Enable this API with `ATTACHMENTS_DIRECT_UPLOAD_ENABLED=true`. Presigned forms
expire after `ATTACHMENTS_PRESIGN_TTL_SECONDS` (default 300), while abandoned
`uploading` rows expire after `ATTACHMENTS_UPLOAD_INTENT_TTL_MINUTES` (default
30). Completion resets the normal `ATTACHMENTS_FILE_TTL_HOURS` retention clock.
Object keys are generated by Cortex as
`<prefix>/users/<user_id>/YYYY/MM/DD/<file_id>-<sanitized_filename>`; clients
cannot supply a bucket or key. Creating an intent does not invent a SHA-256
value, so direct-upload rows keep `sha256=NULL`.

The React client selects this flow when `/runtime-config.js` exposes
`directAttachmentUploads=true`, which mirrors
`ATTACHMENTS_DIRECT_UPLOAD_ENABLED`. It keeps raw browser files in a separate
transient queue, uses `XMLHttpRequest` for real S3 progress, limits transfers to
two, and promotes only Cortex-verified `ready` records into Chat/Compare. While
direct mode is off, multipart `POST /v1/files/upload-batch` and raw-byte
`POST /v1/files/upload` remain compatibility contracts when
`ATTACHMENTS_LEGACY_PROXY_UPLOAD_ENABLED=true`; the React queue uses the batch
route without duplicating the attachment UI. Both legacy routes keep their
current plan validation and SHA-256 same-user deduplication behavior.

S3 remains private. Cortex can optionally add exact SSE-S3 (`AES256`) or
SSE-KMS (`aws:kms` plus an approved key ID) fields to both presigned POSTs and
legacy writes; leave those variables blank when bucket default encryption
already satisfies policy. This repository contains no AWS infrastructure as
code, so production S3 CORS, IAM, bucket/KMS policy, WAF, signing-identity, and
cleanup deployment verification must follow
`docs/runbooks/direct-s3-attachment-rollout.md` before the direct flag is enabled.

In DB mode, upload access and the plan per-file limit are enforced from the authenticated user's server-resolved plan. Upload and storage are free; credits are charged only when a model processes file context as part of a billable generation.

Authentication (session-scoped routes):
- `cortex_session` cookie, or
- `Authorization: Bearer <gateway-bearer-token>`

Headers:
- `X-File-Name: <original-filename>` (optional, defaults to `file`)
- `X-File-Content-Type: <mime-type>` (optional, falls back to `Content-Type`)

Example:
```bash
curl -X POST http://127.0.0.1:8000/v1/files/upload \
  -H "Authorization: Bearer <gateway-bearer-token>" \
  -H "X-File-Name: contract.pdf" \
  -H "X-File-Content-Type: application/pdf" \
  --data-binary "@contract.pdf"
```

`GET /v1/files/{file_id}` returns file metadata and processing status for the same authenticated owner.
`DELETE /v1/files/{file_id}` immediately moves an owned file to `deleting` and
idempotently enqueues object removal. Run the attachment cleanup worker (or
`scripts/attachment_cleanup_job.py`) to finalize it as `deleted` and to clean up
expired/abandoned intents.
Attachment routes reject API-key-only auth with `403` (`session_auth_required`).

Upload status semantics:
- `uploading`: an upload intent exists, but Cortex has not verified the S3 object.
- `ready`: file is immediately usable in chat/compare.
- `processing`: server accepted file and deferred/ongoing ingestion; client should poll status.
- `failed`: ingestion failed; `error_code`/`error_message` explain why.
- `expired`: the intent/file retention window elapsed and cleanup is queued.
- `deleting`: the file is unusable and queued for object deletion.
- `deleted`: object deletion completed; repeated delete remains safe.
- Upload API responses now sanitize `error_message` for client safety (no bucket/key/internal storage paths in response text).
- Frontend upload UX maps failures to safe user-facing messages (network issue, file too large, unsupported type, timeout, generic retry prompt) and does not render raw backend/object-storage error text.
- Sent chat/compare turns render each uploaded attachment as a flat file card using stored metadata: original filename as the primary label, size/type detail, optional image thumbnail preview, and inline readiness text such as `Ready for analysis`.
- Raw upload errors are still logged in frontend developer console for debugging.

MVP ingestion policy:
- Small files are handled inline.
- Text-extractable Office documents persist a private parsed-text cache at ingestion. Follow-up and Compare requests reuse that cache, apply deterministic prompt-term relevance selection when a document exceeds the chunk budget, and send only the selected chunks to each model. The cached text is never returned by file-status APIs.
- Office docs (`.docx`, `.pptx`, `.xlsx`) may return `processing` when above `ATTACHMENTS_SYNC_INGEST_MAX_BYTES`.
- Poll `GET /v1/files/{file_id}` until `ready` before sending chat/compare with that `file_id`.
- Frontend polling budget is 60 seconds; if exceeded, attachment is marked failed in UI and user can remove/retry upload.

Use attachments in chat/compare payloads by passing file references:
```json
{
  "prompt": "Analyze this file",
  "provider": "openai",
  "model": "gpt-4o-mini",
  "attachments": [
    {
      "file_id": "11111111-1111-1111-1111-111111111111",
      "usage_role": "primary",
      "transform_mode": "auto"
    }
  ]
}
```

Current guardrail behavior:
- attachments require `DATABASE_URL` (DB mode)
- all attachment `file_id` values must belong to the same API key owner
- the plan's `max_files_per_request` and `max_file_bytes` are enforced for Ask/Compare attachments, and the model call that processes file context is covered by the request's AI-credit reservation
- model compatibility is validated before orchestration starts
- same-user deduplication is hash+size based (no cross-user dedup)
- only legacy proxy uploads participate in hash+size deduplication; direct
  upload intents have no trusted content hash
- provider adapter support in this release:
  - OpenAI: images + PDF
  - Gemini: images + PDF (inline data)
  - Claude: images + PDF (base64 document/image blocks)
  - Grok: images only
  - DeepSeek: binary attachments unsupported; text-materialized attachments are accepted

Attachment metadata semantics:
- `uploaded_files.ingestion_meta` is **file-level** metadata (ingestion requirement/state, extraction stats, etc.).
- `request_attachments.resolved_artifact_meta` is **request-level** metadata (effective transform/materialization used for that specific turn).

## Routing Modes

For Ask (`/v1/chat`, `/v1/chat/stream`) requests:
- auth must be session-based (`cortex_session` cookie or `Authorization: Bearer`)
- Explicit `provider` + `model`: deterministic target.
- `routing.smart_mode=true` (or omitted): true smart orchestration path (`routing_mode="smart"` with optional constraints from `SMART_CHAT_*` env vars).
- `routing.smart_mode=false`: legacy deterministic auto-pick path.
- `routing.research_mode=true`: orchestrator-managed web research flow with fresh sources for the current turn.
- API contract note: `routing.research_mode` is boolean (`true|false`) and is mapped to orchestrator modes `on|off`.
- Smart routing tiering now considers full runtime message payload (including research/system injection), not just base prompt/history estimates.

Prompt optimization (`/v1/optimize`):
- disabled by default unless `ENABLE_PROMPT_OPTIMIZATION=true`
- this explicit endpoint is the UI optimization path; chat/compare do not auto-optimize by default
- in DB mode, the explicit endpoint checks `prompt_improvement_enabled`, reserves every configured GPT-4.1 mini optimizer attempt before setup, and settles each attempt that actually returns billable usage
- Improve Prompt counts as one submitted user action against the effective plan's requests-per-minute limit
- a later Ask/Compare submission of the returned prompt is a separate model call and uses the normal Ask/Compare credit calculation; it does not duplicate the optimizer charge. The React flow sends the same display-only `credit_activity_id` and original `initial_query` to both requests so the Credits screen presents one question-level total.
- optional orchestrator-level auto-optimization for chat/compare requires `ENABLE_ORCHESTRATOR_PROMPT_OPTIMIZATION=true`
- uses `PROMPT_OPTIMIZER_PROVIDER` + optional `PROMPT_OPTIMIZER_MODEL`
- `/v1/optimize` has an optimize-specific hard deadline from `PROMPT_OPTIMIZER_TIMEOUT_MS` (default `5000`) and explicit-route retry count from `PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES` (default `2`)
- weak or vague prompts are classified locally and get one extra retry if the optimizer returns the original prompt unchanged; strong prompts can keep the original without retry
- optimizer calls use compact generation defaults from `PROMPT_OPTIMIZER_MAX_OUTPUT_TOKENS` (default `450`) and `PROMPT_OPTIMIZER_TEMPERATURE` (default `0.2`), with JSON-object mode for OpenAI chat models
- optimizer route logs include status, fallback reason, prompt-quality class, attempt count, and retry reasons without logging raw prompt text
- request payloads may include optional display-only `credit_activity_id`, `context_hint`, and compact `context`; the frontend sends recent mixed user/assistant context whenever a thread has prior messages. Chat/Compare may also include the original `initial_query` for the ledger display. These fields never affect entitlement or credit arithmetic. Attachment file contents are not copied into optimize requests. React caps optimize context to the last ten compact messages and a 4,000-character `context_hint` so follow-up rewrites can resolve ordinal, pronoun, and formatting references like "the second one", "their cadres", or "write it as a table".
- optimizer model output must be valid optimizer JSON and is rejected if it appears to answer the prompt, or if it introduces unresolved placeholders such as `[specific topic]`, instead of rewriting it
- responses include `optimization_status` (`optimized`, `kept_original`, `disabled`, `timeout`, `failed`, `rejected`) plus `fallback_reason`
- if optimization is disabled, times out, fails, is rejected, or keeps the original, the API returns the original prompt with `was_optimized=false`
- when the frontend Improve toggle is enabled, the user bubble always shows the prompt being sent in normal case. Optimization state renders as a right-aligned pill below the bubble: pending shows `Improving your prompt`, optimized shows `Prompt optimized` with `View original`, and kept-original shows `Already clear — sent as-is`. Ask response cards and Compare response tabs, cards, and summary remain hidden while optimization is pending, then appear when optimization resolves and model generation begins; cancelling during optimization leaves the placeholder response UI hidden

## Generation Budgets

- Ask and Compare accept `generation.profile` (`auto`, `quick`, `balanced`, `deep`, or `extended`) or an explicit `generation.max_output_tokens`. Auto is the React default: normal economical/standard calls receive 4K, advanced reasoning models such as GPT-5.6 Terra and Claude Sonnet receive 8K, premium Opus/Fable models receive 12K, and clearly complex or explicitly detailed tasks receive 12K. Auto never selects 32K; Extended is API-explicit or a retry.
- `generation.reasoning` accepts provider-neutral `mode` (`auto|off|on`) and effort (`auto|minimal|low|medium|high|xhigh|max`). The resolver validates the selected model and translates these values for provider adapters.
- Claude thinking is model-specific: Claude 4.6 and supported Claude 5 models use registry-declared adaptive thinking, while the manual-budget-only Claude 4.5 family defaults to normal generation and rejects explicit reasoning-on before provider work. The Claude adapter omits custom `temperature` whenever Anthropic requires default sampling, including all adaptive-thinking requests and Claude 5 requests.
- React does not expose a technical depth selector or live hold estimate. It sends `auto`, while users express desired answer detail in the prompt. API callers that omit both `generation` and `max_tokens` retain the `quick`/1K compatibility default. Supplying `generation` together with legacy `max_tokens`, or asking for an unsafe explicit custom ceiling, returns `422 invalid_generation_budget`.
- The same resolved per-model ceiling drives provider execution and the maximum temporary AI-credit hold. `POST /v1/billing/estimate-generation` returns that hold without reserving it; successful settlement charges actual usage and releases the unused amount.
- Responses expose `completion_status`, `stop_cause`, `generation_budget`, and `retry_with_more_room`. Length-limited responses preserve partial text as `incomplete`; Auto 4K/8K calls retry at Deep/12K, Auto 12K calls retry at Extended/32K, and every retry is a new call that may use more credits.
- `config/generation_profiles.yaml` is the budget source of truth. `GENERATION_BUDGET_POLICY_ENABLED=false` is the operational rollback switch to Quick/2K execution.

See `docs/GENERATION_BUDGETS.md` for the full contract and `docs/runbooks/generation-budget-rollout.md` for deployment checks.

## Cache-aware credits and reuse

- `server/billing/credit_calculator.py` is the shared authority for settlement, response cards, history, and cache-savings information. Provider-reported prompt tokens are partitioned into normal input, cache reads, and cache writes; effective provider-price ratios scale the existing model input-credit multiplier. Missing pricing evidence receives no discount, quantities are clamped to the prompt total, and every component rounds up independently.
- Reservations remain conservative: they assume no cache hit and cover a more expensive cache-write ratio when applicable. `CACHE_AWARE_CREDIT_CALCULATION_ENABLED=true` computes and records shadow totals; `CACHE_AWARE_CREDIT_SETTLEMENT_ENABLED=false` keeps legacy totals authoritative until provider invoices and shadow telemetry are validated.
- Chat responses expose `cache_hit`, `cache_hit_ratio`, `cache_savings_ai_credits`, and `uncached_equivalent_ai_credits`; Compare adds `total_cache_savings_ai_credits`. The normal response card keeps one AI-credit total and only adds a context-reuse savings line when settlement makes the savings real. Provider dollar cost is not shown on that card.
- Provider cache behavior is independently gated. `CACHE_KEY_SECRET` produces opaque HMAC-SHA256 affinity keys; OpenAI supports `prompt_cache_key`, Claude supports top-level ephemeral `cache_control`, Grok supports `x-grok-conv-id`, and Gemini/DeepSeek use stable-prefix ordering plus reported cache usage. Extended OpenAI retention is a separate opt-in.
- Persistent research reuse, prompt-optimization reuse, identical Cortex Analysis reuse, credit-aware generation ceilings, and deterministic context compaction each have independent flags. File extraction is already SHA-256 deduplicated; repeated narrow questions reuse the persisted extraction and select query-relevant chunks without rebuilding duplicate content.
- Generation policy v3 uses server-managed Auto ceilings of 4K, 8K, or 12K from task/model capability; explicit Quick, Balanced, Deep, and Extended remain 1,024, 4,096, 12,288, and 32,768. Credit-aware ceilings use a safety margin and the shared Compare reservation before provider execution.

For Compare (`/v1/compare`, `/v1/compare/stream`) requests:
- auth must be session-based (`cortex_session` cookie or `Authorization: Bearer`)
- Targets are always explicit (`targets[]`).
- Requests contain two or three targets. The absolute API maximum is three; the effective plan may limit the request to two.
- `routing.smart_mode` is ignored by design in compare mode.
- `routing.research_mode=true` is still honored and runs once per compare turn for all selected targets.
- In the browser UI, Ask starts with `Web` enabled by default and Compare starts with `With sources` enabled by default; each keeps a manual off choice for that page session.
- Frontend Compare selectors support per-model removal with compact circular controls attached to each selector. Remove controls show only when three models are active; removing any slot compacts the remaining two models, preserving the API's two-target minimum. Request payloads include only active selected models.
- Frontend Compare response cards hide per-card action label text in side-by-side layouts and keep compact copy/feedback icons; inline citation pills carry source previews, while success/error counts and aggregate AI-credit usage remain in the summary bar without token totals.
- Frontend Compare selectors appear as removable model chips in the unified composer. React prefers `openai:gpt-5.6-luna` and `claude:claude-sonnet-5` for the initial two targets, and Add Model prefers `deepseek:deepseek-v4-flash`; unavailable preferences fall back to distinct selectable catalogue models. Request payloads include only active selected models.
- React manual Ask and Compare model controls share one accessible provider-first picker. It opens on provider names, logos, and model counts, then shows only the active provider's readable model-family labels, exact API IDs, credit-use hints, plan locks, and active state. Fine-pointer desktop layouts use an immediate adjacent hover preview that stays open while the pointer moves between provider and model panels, switches as another provider is hovered, and closes after the pointer leaves the picker; click and Right/Left Arrow remain keyboard-accessible fallbacks. Touch/mobile layouts use a compact tap-to-drill-down flow with Back. The body portal stays viewport-contained, Compare adds duplicate-option disabling and removal rules through the shared component API, and hidden native selects remain synchronized for browser automation compatibility.
- Frontend Compare response cards use side-by-side model columns with a packed per-card metric strip for completed duration and AI-credit usage on desktop; mobile exposes the same metrics directly in the compact header and phone-sized Compare switches between model responses with the frosted sticky model switcher. Token usage remains in the response contract and React state for persistence and reporting but is not rendered on result cards. Loading cards show live elapsed time plus `Queued`, `Refining prompt`, `Connecting to model`, `Generating response`, or `Finalizing`; completed cards keep that same UI-observed elapsed basis when live timestamps are available, and failed cards show elapsed failure time.

## Web Research Behavior (Current)

- API contract: `routing.research_mode` accepts `true|false` (mapped to `on|off`).
- Internal orchestrator modes:
- `research_mode=off`: hard stop for this turn (no research injection, no reuse).
- `research_mode=auto`: reuse prior research only when intent/topic heuristics match; otherwise search.
- `research_mode=on`: always perform fresh search for the current turn and bypass local research cache.
- When sources are injected, they are treated as primary evidence for current/source-dependent factual claims; non-conflicting model background knowledge can still be used for explanation/context.
- CortexAI does not use phrase, number, date, or citation heuristics to classify successful provider answers as fabricated or replace their text. With research off, non-empty answers are returned as generated by the selected models.
- Query sanitization anchors underspecified follow-up searches to the previous user topic when the current prompt omits that topic, so a Compare follow-up like a 1990s film list stays scoped to the active conversation subject.
- If query sanitization yields empty query in `on` mode, orchestrator falls back to the raw prompt.
- Prompt injection includes citation requirements, partial-source fallback guidance, and a UTC retrieval timestamp.
- Tavily search options are resolved by a deterministic local resolver before the API call. It always sends `max_results=5`, `search_depth=advanced`, `chunks_per_source` from `TAVILY_CHUNKS_PER_SOURCE` (default `3`), `include_raw_content=false`, `include_answer=false`, and `auto_parameters=false`.
- When `TAVILY_ENHANCED_SEARCH_ENABLED=true`, the resolver may add Tavily `topic` (`finance` or `news` only), `time_range`, country targeting for non-topic searches, and curated finance domain allowlists. It does not rewrite the query; prompt optimization and existing query sanitization remain separate.
- Tavily country targeting is omitted whenever `topic` is sent because Tavily country filtering applies only to general searches. Finance regional targeting uses curated domains instead, for example `bankofcanada.ca`/`statcan.gc.ca`, `bls.gov`/`bea.gov`/`federalreserve.gov`, `sec.gov`, or `ons.gov.uk`/`bankofengland.co.uk`.
- `TAVILY_ENHANCED_SEARCH_ENABLED=false` is the kill switch for enhanced Tavily options; the call still uses the fixed retrieval params above.
- When provider timestamps are missing, Tavily source timestamps fall back to server UTC ISO timestamps (never `Timestamp: N/A`).
- Tavily runtime diagnostics emit `research.network.diagnostics` with DNS + TCP egress health (host/port configurable via `TAVILY_NETWORK_DIAGNOSTICS_*` envs) for EC2 troubleshooting.
- Tavily failures emit normalized `error_kind` fields (for example `dns_resolution_failed`, `timeout`, `auth_forbidden`, `rate_limited`) without logging raw query text.

## Session Continuity

- Ask and Compare now share the same conversation session when the same `session_id` is reused.
- Switching between Ask and Compare does not require creating a separate thread.
- Compare turns still persist their per-target rows under a shared `request_group_id`, but the user-visible chat session can remain the same across both modes.
- `GET /v1/history` intentionally returns persisted request rows rather than pre-grouped UI threads. Each row includes optional `session_id`, `session_title`, and `request_group_id`; clients group sidebar threads by `session_id` and Compare responses by `request_group_id`. Completed rows also return the persisted response-card `ai_credits` snapshot, its estimated flag, the input/output token split, and the shared Compare research-credit component so reopened Ask and Compare cards retain the same credit statistics shown live. Legacy rows without a snapshot derive model credits from their stored token split. A user-authored `session_title` overrides the first prompt as the sidebar label, while the legacy system placeholders (`API Chat` and `API Compare`) are ignored.
- Compare history exposes each response's immutable `request_id` and current `response_version`. Regenerating a Compare response appends a new revision to the same logical response slot; restored history displays the latest revision without deleting the prior audit row.
- `PATCH /v1/history/session/{session_id}` with `{"title":"..."}` renames one authenticated user's persisted session. Titles are trimmed, limited to 120 characters, and do not change the thread's latest-activity ordering.
- `DELETE /v1/history?session_id=<id>` clears only that user-visible conversation thread. Omitting `session_id` clears all history for the authenticated identity. React per-thread delete calls `DELETE /v1/history/{entry_id}` for every persisted row in the selected thread.
- The browser UI persists the active thread id as `cortex_active_session_id` and restores that transcript after reload/resume. It does not auto-continue the last active thread after an explicit fresh login: that path marks `cortex_fresh_login_pending`, consumes the `fresh_login=1` callback marker, clears the active thread id, and sends `new_session=true` for the first turn. Users can still reopen older threads from History.

## Chat Context Guardrails

- Conversation history sent to the API is trimmed to the last `10` messages.
- Oversized conversation-history payloads are soft-trimmed server-side instead of rejected.
- The server keeps the newest context first and trims older or oversized message content within the internal context budget before calling providers.

## Compare and `request_group_id`

`/v1/compare` and `/v1/compare/stream` return a `request_group_id` that groups all per-target persisted rows.
- Each target persists its own request/response pair.
- All target rows share the same `request_group_id`.
- Use this ID to debug failed attempts via admin route.

## Cortex Analysis

Cortex Analysis is an on-demand synthesized model call below completed browser Compare responses. It consumes the same unified monthly AI-credit wallet and has no separate Cortex quota.

- `POST /v1/compare/{request_group_id}/analysis` analyzes the latest successful revisions from two or three responses with `CORTEX_ANALYSIS_MODEL` (default `gpt-5.4-mini`) and returns `201`.
- Before invoking the model, the route resolves the effective plan, verifies model access, and reserves for the Compare question, source responses, and a clamped 1,800-token output ceiling. Successful actual input/output usage settles the reservation; generation failure releases it. Each re-run is a new billable model call.
- Provider/model identities are removed before the analysis model receives
  shuffled `Response A/B/C` content. After generation, the server replaces
  those anonymous labels with canonical provider-and-model display names (for
  example, `Claude (Sonnet 4.6)`) in every user-visible result section before
  saving the run. Responses from different models of one provider therefore
  remain distinct in insights, differences, confidence, and verification copy.
- The model uses strict Structured Outputs, and the API validates the result before persistence. Provider failure or invalid output returns `502`; a failed generation does not create a run.
- Each disagreement is returned as `{ "who": <display name>, "text": <position> }`; optional `disagreementNote` explains the nature of the difference without choosing a winner. Unique insights retain their model attribution.
- The finished React result is one continuous document: combined answer and inline qualitative confidence first, then always-visible agreement/difference/unique-insight evidence, followed by one verification band. There are no per-section disclosures or saved open/closed preferences. When confidence is `limited` and disagreements exist, the difference column leads visually without changing model ranking or hiding original responses.
- Successful runs are append-only in `cortex_analysis_runs`. Re-running never overwrites an earlier result.
- While a re-run is processing, the result area temporarily replaces the
  previous combined answer with the analysis progress state. If the re-run
  fails, the saved answer returns below the retry message.
- `GET /v1/compare/analysis-runs` requires either `session_id` or `request_group_id` and returns every owned run newest-first.
- Each run snapshots exact source `requestId` and `responseVersion` values. If a source response is regenerated later, earlier analyses remain readable and return `isStale: true`; the browser offers an explicit update action.
- Reloading or reopening a history thread hydrates its Compare transcript and all saved Cortex Analysis runs. The result area defaults to the newest run and exposes older runs through Analysis history.
- Analysis starts only after a user action and only when at least two Compare responses succeeded. Existing Compare research context is reused without another research charge. Saved runs stay readable after downgrade; a new run can still be denied if its configured model is no longer allowed or the unified wallet has insufficient credits. There is no automatic synthesis, separate Cortex quota, or Cortex-only mobile tab.

Apply `db/migrations/20260727_add_cortex_analysis_runs.sql` with a database role
that owns `llm_requests`, then apply
`db/migrations/20260802_add_cortex_analysis_attribution.sql` before deploying
this behavior. Restart the API so reflected table metadata includes the new
column. Until that schema is ready,
both Cortex Analysis endpoints return
`503 cortex_analysis_schema_unavailable`; the create endpoint performs this
check before calling the analysis model.

## Streaming NDJSON Contract

`/v1/chat/stream` events:
- `start`
- `heartbeat`
- `line`
- `response_done`
- `done`
- `error`

Notes:
- `heartbeat` is emitted while provider work is still running so proxies and
  browsers do not see an idle response body. It carries only elapsed timing and
  does not consume provider tokens.
- Chat responses now include `session_id`.
- Chat `response_done` payloads include `web_source_items` for rendered citation pills.
- Chat `start` and `done` stream events include the active `session_id`.
- Server logs include `chat.stream.*` body lifecycle events so mid-stream
  disconnects can be distinguished from normal HTTP request completion.

`/v1/compare/stream` events:
- `start`
- `heartbeat`
- `response_start`
- `line`
- `response_done`
- `done` (contains aggregate compare payload with `request_group_id`)
- `error`

Notes:
- `heartbeat` may appear while Compare targets are still pending; React ignores it
  visually and waits for normal per-target events.
- Compare responses now include `session_id`.
- Per-model compare `response_done` payloads include `web_source_items`.
- Compare `start` and `done` stream events include the active `session_id`.
- The final compare `done` payload includes both `request_group_id` and `session_id`.
- Server logs include `compare.stream.*` body lifecycle events with per-target
  provider-call progress and terminal stream reason.

## BYOK (Bring Your Own Keys)

Security model:
- Provider secrets are encrypted at rest using `MASTER_KEY`.
- Raw provider secrets are never returned from status APIs.
- Runtime requests resolve tenant BYOK keys per provider when available.

At-rest verification (DB spot check):
```sql
SELECT provider, encrypted_key, key_last4
FROM byok_provider_keys
ORDER BY updated_at DESC
LIMIT 20;
```
Expected: `encrypted_key` should never match original plaintext keys.

`MASTER_KEY` rotation (current model):
1. Set `OLD_MASTER_KEY` outside app runtime and decrypt existing rows in a one-time admin script.
2. Re-encrypt with new `MASTER_KEY`.
3. Restart API with only the new `MASTER_KEY`.
4. Spot-check `byok_provider_keys` and run `/v1/byok/status`.

Set/update:
```bash
curl -X POST http://127.0.0.1:8000/v1/byok \
  -H "X-API-Key: dev-key-1" \
  -H "Content-Type: application/json" \
  -d '{
    "provider_keys": {"openai": "sk-tenant-openai-key"},
    "baseline_provider": "openai",
    "baseline_model": "gpt-4o-mini",
    "requests_per_minute": 120
  }'
```

Status:
```bash
curl -H "X-API-Key: dev-key-1" http://127.0.0.1:8000/v1/byok/status
```

Delete:
```bash
curl -X DELETE -H "X-API-Key: dev-key-1" "http://127.0.0.1:8000/v1/byok?provider=openai"
```

## Usage and Savings Reporting

Usage summary for the Usage & insights screen:
```bash
curl -H "X-API-Key: dev-key-1" \
  "http://127.0.0.1:8000/v1/usage/summary?from=2026-02-01&to=2026-02-22"
```

`GET /v1/usage/summary` defaults to the last 30 inclusive calendar days when no range is provided. It returns the screen contract: period label, total tokens/requests/sessions, average/p95/min latency, average cost/request, total spend, token delta versus the previous equal-length period, Smart-routed totals, per-provider/model reply rows, Ask/Compare/Mixed session counts, a zero-padded 14-day token activity series, and cache-aware credit/token/reservation/reasoning/reuse metrics. Research, prompt-optimization, and Cortex Analysis reuse rates are calculated from per-request `cache_reuse_events` audit rows within the selected period. Usage and exports may group by `day`, `provider`, `model`, or `operation`.
The React Usage & insights screen uses the same period-scoped summary query; its period selector refetches the full analytics dashboard and its Export button downloads day-grouped CSV rows from `/v1/usage/export` for the loaded period. On phone layouts it preserves the same backend-driven values in shortened labels and a compact model/session presentation. AI-credit balance and ledger data are intentionally displayed on `/credits`, not mixed into this screen.

Usage:
```bash
curl -H "X-API-Key: dev-key-1" \
  "http://127.0.0.1:8000/v1/usage?from=2026-02-01&to=2026-02-22&group_by=day"
```

Savings:
```bash
curl -H "X-API-Key: dev-key-1" \
  "http://127.0.0.1:8000/v1/savings?from=2026-02-01&to=2026-02-22&group_by=model"
```

CSV export (invoicing hook):
```bash
curl -H "X-API-Key: dev-key-1" \
  "http://127.0.0.1:8000/v1/usage/export?format=csv&from=2026-02-01&to=2026-02-22&group_by=provider"
```

Export determinism:
- CSV column order is fixed.
- Response includes `X-Export-Columns` for schema verification in ingestion jobs.
- Large date ranges are bounded by `REPORT_MAX_RANGE_DAYS` to keep exports responsive.

## Error Codes

Common `detail.code` values:
- `usage_limit_exceeded`
- `rate_limited`
- `provider_error`
- `timeout`
- `bad_request`
- `invalid_model`
- `unauthorized`
- `billing_not_configured`
- `invalid_subscription_plan`
- `paid_subscription_plan_required`
- `stripe_customer_required`
- `billing_provider_unavailable`
- `invalid_generation_budget`

Provider-model failures that originate from upstream APIs are normalized before
they reach API/stream/frontend surfaces. `error.details.kind` carries the stable
failure class when available, such as `transient_capacity`, `rate_limited`,
`quota_exceeded`, `timeout`, `auth`, `bad_request`, or `provider_5xx`.

## Output Guardrails (Current)

- Ask/Compare output ceilings are resolved centrally from `config/generation_profiles.yaml` and `config/model_registry.yaml`; there is no global 2,048 clamp for explicit profiles.
- Legacy omitted requests and direct adapter calls retain the 2,048 Quick default for compatibility.
- Explicit limits above the model/context/operational maximum are rejected with `422` rather than silently clipped.
- Empty length-limited responses remain billable incomplete work, including cases where reasoning consumed the output allowance before visible text. Other unexplained empty successes continue through provider-error normalization.
- Raw provider availability payloads (for example 503/high-demand/overloaded errors) are converted to client-safe messages before chat, compare, stream, and history rendering. Smart Ask still uses its existing fallback loop; manual Ask and Compare preserve explicit model choices and show `This model is temporarily busy. Try again shortly or switch to another model.` when the selected model is unavailable.
- Non-empty successful provider answers are not content-policed or rewritten by CortexAI. Grounding is controlled by the explicit Web/With sources mode, while provider-native safety and error outcomes continue through normal response handling.

## Minimal Python SDK Snippet

```python
import requests

class CortexClient:
    def __init__(self, base_url: str, bearer_token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

    def chat(self, prompt: str, provider: str | None = None, model: str | None = None):
        payload = {"prompt": prompt}
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        r = requests.post(f"{self.base_url}/v1/chat", json=payload, headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def compare(self, prompt: str, targets: list[dict]):
        payload = {"prompt": prompt, "targets": targets}
        r = requests.post(f"{self.base_url}/v1/compare", json=payload, headers=self.headers, timeout=120)
        r.raise_for_status()
        return r.json()

    def usage(self, from_date: str, to_date: str, group_by: str = "day"):
        r = requests.get(
            f"{self.base_url}/v1/usage",
            params={"from": from_date, "to": to_date, "group_by": group_by},
            headers=self.headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def usage_summary(self, from_date: str, to_date: str):
        r = requests.get(
            f"{self.base_url}/v1/usage/summary",
            params={"from": from_date, "to": to_date},
            headers=self.headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
```

## Reliability and Guardrails

- Daily cost/token caps are enforced only in DB mode.
- Rate limiting is per API key (or key owner fallback when unmapped).
- Circuit breaker opens per `provider:model` and allows automatic fallback.
- Circuit breaker scope is currently global per `provider:model` (not tenant-scoped).
- Retry/fallback paths are bounded; no unbounded loops.
- Transient provider capacity failures are tagged with `error.details.kind="transient_capacity"` so routing telemetry, circuit-breaker behavior, and the amber `.model-soft-error` UI treatment stay consistent across providers.

## Privacy and Retention

- default (unset): `STORAGE_POLICY=full` behavior.
- `STORAGE_POLICY=metadata`: no raw prompt/response text persistence.
- `STORAGE_POLICY=full`: content persistence enabled.
- `REDACT_PII=true`: regex redaction for emails, phones, and card-like numbers before DB storage.

## Build Artifacts

React static frontend build and optional zip/manifest:
```bash
npm ci --prefix frontend-react
npm run --prefix frontend-react build
python scripts/build_frontend_artifact.py
```

API runtime image:
```bash
docker build -f Dockerfile.api -t cortexai-api:dev .
```

Standalone React/nginx image:
```bash
docker build -f Dockerfile.frontend -t cortexai-frontend:dev .
```

Production deployment notes:
- `Dockerfile.api` is API-only and does not copy `frontend-react/dist`; if the API image should serve React directly, include the built `frontend-react/dist` directory in that runtime image and set `FRONTEND_DIR` to its absolute path.
- `Dockerfile.frontend` builds and serves React static assets with nginx. Put it behind a reverse proxy or update nginx/CDN routing so `/v1/*`, `/auth`, and `/runtime-config.js` reach the FastAPI service.
- Keep `APP_ENV`, `ENVIRONMENT`, or `ENV` set to `prod`/`production` in production-like deployments so browser dev-session login is forced off.

### AWS Production: preventing automatic page refreshes

The UI page can appear to refresh automatically in production for these reasons:

1. **Cognito auth redirect loop (most common)**

   Behind CloudFront or an ALB, HTTPS is terminated at the load balancer. FastAPI sees plain HTTP, so `request.base_url` returns `http://…` unless proxy header forwarding is enabled. The computed Cognito redirect URI (`http://…/auth`) then mismatches the registered `https://…/auth` callback, causing every token exchange to fail and redirecting the browser back to the Cognito login page repeatedly. Fix:

   - Set `COGNITO_REDIRECT_URI=https://app.example.com/auth` explicitly, **and**
   - Ensure `ENABLE_PROXY_HEADERS=true` (default) so `request.base_url` / `runtime-config.js` reflect HTTPS.

2. **Session cookie not persisting across browser sessions**

   Without an explicit `max_age`, the `cortex_session` cookie is a session cookie that is deleted when the browser closes. On mobile, background/resume cycles and low-memory tab discards trigger this frequently. The default is now 7 days (`SESSION_MAX_AGE_SECONDS=604800`). Increase or decrease to match your security policy.

3. **Session cookie `Secure` flag**

   In HTTPS deployments, the session cookie must carry the `Secure` attribute so browsers send it on encrypted requests. The flag is now auto-detected from `request.url.scheme` (which is `https` when `ENABLE_PROXY_HEADERS=true`). Override with `SESSION_COOKIE_SECURE=true` if needed.

4. **ALB idle timeout vs SSE streaming**

   AWS ALB defaults to a 60-second idle timeout. The backend sends heartbeat events every `STREAM_HEARTBEAT_INTERVAL_SECONDS` (default 15 s) to keep streaming connections alive. If the ALB is configured with a shorter timeout, or WAF rules terminate long-lived connections, users will see streams fail. Increase the ALB idle timeout to at least 120 seconds for streaming routes.

5. **Safari background-tab eviction (confirmed root cause in July 2026)**

   Any `beforeunload` event listener makes the page ineligible for the browser's back/forward cache (bfcache). Safari must then keep the full JS context in memory for backgrounded tabs. After ~8–10 minutes it evicts the tab and does a full reload when the user returns. The `beforeunload` listener has been removed from `bootDiagnostics.ts`; `pagehide` is used instead (covers the same events and is bfcache-compatible). After deploying this change, look for `frontend.diagnostic` events with `details.navigationType="pagehide"` and `details.persisted=true`, which confirms the page is now entering bfcache successfully.

6. **`frontend.diagnostic` log events**

   The React client records every page boot, unload, and visibility change to `/v1/client-diagnostics`. Search for `frontend.diagnostic` events in CloudWatch / `app.log` around the reported timestamp and inspect `details.navigationType` and `details.wasDiscarded` to identify the exact refresh cause (user reload, Chrome tab discard, bfcache restore, or Cognito redirect). See `docs/runbooks/aws-ec2-logging.md` § "Browser Refresh / Blink Workflow".

These artifacts are intentionally separate so frontend-only or API-only changes can be built independently.

## DB Migration Runbook

- See `docs/runbooks/db-migrations.md` for migration authoring, apply order, rollback strategy, and verification checks.
- Apply the required subscription and Cortex migrations in this deterministic order before deploying the API:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260718_add_b2c_billing_foundation.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260727_add_cortex_analysis_runs.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260729_add_unified_ai_credits.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260730_add_usage_reservation_activity.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260731_add_model_pricing_audit.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260802_add_cortex_analysis_attribution.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260804_add_generation_budget_audit.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260807_add_cache_aware_credit_accounting.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260811_add_direct_s3_attachment_upload.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260820_add_cortex_work_mode.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260829_add_work_web_output_and_model_identity.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260905_add_subscription_grants.sql
```

- `20260718` creates the billing foundation; `20260727` adds Compare revisions and append-only Cortex runs; `20260729` creates the immutable `credit_transactions` ledger and unified `ai_credits` contract; `20260730` adds reservation heartbeats; `20260731` adds model/pricing audit evidence; `20260802` adds Cortex disagreement attribution; `20260804` adds generation-budget/reasoning audit fields plus normalized completion status; `20260807` adds cache-aware ledger columns, reusable optimizer/research/Cortex/context-summary persistence, and the `cache_reuse_events` telemetry table; `20260811` permits checksum-free upload intents and adds `uploading`/`deleting` attachment states; `20260820` adds durable Work sessions/runs/events, files, tool connections/calls, approvals, OAuth state, and reconciliation leases; and `20260829` adds Work Web/output/model-identity audit fields plus the output-limit terminal status. The scripts are additive/idempotent. Alteration scripts require ownership of the affected tables. Restart the API after apply. PostgreSQL startup validates the required billing, pricing-audit, Cortex, generation-budget, and cache-accounting columns before provider traffic; when direct upload or Work is enabled it also validates the corresponding additive schema.

## Release Gate

Use this before launch/deploy:
```bash
python scripts/release_gate.py
```

It runs:
- `python -m py_compile ...`
- `pytest -q`
- DB mode smoke test (`/v1/chat` + DB row assertions for `llm_requests` and `usage_daily`)

## CI and Workflows

- `.github/workflows/ci.yml`:
  - path-aware frontend/backend quality checks
  - changed-file Python Ruff/MyPy gates with pinned dev tool versions
  - Black format check is advisory until the repository has a formatting baseline
  - frontend artifact build
  - API image build metadata export
  - pinned Gitleaks CLI directory scan of the checked-out tree

Install the repository's [local Git gates](.codex/ci-commit-gate.md) once per
clone so checks run automatically:

```powershell
venv\Scripts\python.exe -m pre_commit install --install-hooks --hook-type pre-commit --hook-type pre-push
```

The blocking `pre-commit` hook scans the exact staged tree and runs staged-Python
Ruff/MyPy plus fast staged/component tests. The blocking `pre-push` hook mirrors
every locally runnable `ci.yml` backend, React, security, artifact, and image job
against the committed branch. Both hooks give pytest a gate-private temp root so
Windows user-temp ACL problems cannot create false test failures. If the Docker
CLI or daemon is unavailable, the hook defers only the API image build to GitHub
Actions; set
`CORTEX_CI_REQUIRE_DOCKER=1` when local Docker availability must be mandatory.

- `.github/workflows/incident-regression-38.yml`:
  - targeted backend regression pack for routing/guardrail mismatches (38 tests)
  - no live provider keys required
- `.github/workflows/live-e2e.yml`:
  - live Playwright browser suite with real providers
  - uses GitHub Environment `live-e2e`
  - runs on `windows-latest` and provisions local PostgreSQL in-workflow
  - initializes schema from `db/schema_public_snapshot.sql` + `db/migrations/*.sql`
  - publishes Playwright JUnit results directly into GitHub Checks + run summary (no artifact download needed for first-pass triage)

Required secrets for `live-e2e` environment:
- `E2E_API_KEY` (gateway auth key used by E2E suite; not a provider billing key)
- `OPENAI_API_KEY`
- `GOOGLE_GEMINI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GROK_API_KEY`
- `DEEPSEEK_API_KEY`
- optional web/search keys used by test prompts: `BRAVE_API_KEY`, `SERPAPI_API_KEY`

The live harness enables the production-blocked dev-session endpoint only inside its local E2E server process so React and cleanup helpers can access session-scoped routes. `E2E_DEV_SESSION_LOGIN_TOKEN` can override the harness-local default when needed.

## Tenant Onboarding Script

One-command onboarding (create tenant user + api key + baseline + optional BYOK):
```bash
python scripts/onboard_tenant.py \
  --email founder@startup.com \
  --name "Startup Tenant" \
  --baseline-model-id openai:gpt-4o-mini \
  --requests-per-minute 60 \
  --byok-openai sk-tenant-openai-key \
  --base-url http://127.0.0.1:8000
```

The script prints:
- `user_id`, `api_key_id`
- generated or assigned API key
- ready-to-run `curl` examples including `/v1/whoami`

## Monthly Proof Pack Script

Generate customer proof pack (CSV + Markdown + HTML):
```bash
python scripts/generate_proof_pack.py --api-key dev-key-1 --month 2026-02
```

Outputs:
- `summary.csv` (spend, baseline, savings, error rate, config snapshot)
- `top_models.csv`
- `proof_pack.md`
- `proof_pack.html` (print to PDF if needed)

## Tests

Run full suite:
```bash
python -m pytest -q
```

Run full suite with an HTML report artifact:
```bash
python scripts/test_report_runner.py
```

This generates:
- timestamped report folder under `reports/test-results/<YYYYMMDD-HHMMSS>/`
- HTML dashboard report at `reports/test-results/latest-report.html`
- JUnit XML at `reports/test-results/latest-junit.xml`

Run B2B launch tests only:
```bash
python -m pytest tests/test_b2b_launch_features.py -q
```

Run Stripe gateway/session contract tests without making real Stripe calls:
```bash
python -m pytest tests/test_stripe_billing.py tests/test_billing_repository.py -q
```

Run full B2B API checklist against a live server:
```bash
python scripts/e2e_b2b_checklist.py --base-url http://127.0.0.1:8000 --api-key dev-key-1
```

Run browser E2E suite (Playwright):
```bash
npm run --prefix e2e test
```

Run the frontend-only responsive suites independently:
```bash
npm run --prefix e2e test:mobile
npm run --prefix e2e test:desktop-ipad
```
These responsive suites start their own Vite server and use mocked frontend API contracts, so they do not require PostgreSQL, provider keys, or the live E2E environment. Mobile coverage exercises 320px and 390px phone behavior; the desktop/iPad suite covers desktop plus iPad portrait and landscape layouts.

Run high-impact UI business scenarios only:
```bash
npm run --prefix e2e test -- specs/50.high-impact-business-ui.spec.mjs
```

Run incident regression pack locally (same target set as workflow):
```bash
python -m pytest -q \
  tests/test_routing_regression.py \
  tests/test_fallback_manager.py \
  tests/test_smart_router_metadata.py \
  tests/test_server_utils.py \
  tests/test_unified_response_contract.py \
  tests/test_fastapi_contract_and_guardrails.py \
  tests/test_api_persistence_guardrails.py
```

Postman collection:
- `docs/postman/CortexAI_B2B.postman_collection.json`

## Project Structure

```text
OpenAIProject/
  .dockerignore
  Dockerfile.api
  Dockerfile.frontend
  nginx.conf
  main.py
  run_server.py
  list-models.cmd
  quick_test_optimizer.py
  README.md
  requirements.txt
  requirements-dev.txt
  pyproject.toml
  pytest.ini

  .github/
    workflows/
      ci.yml
      incident-regression-38.yml
      live-e2e.yml

  api/
    base_client.py
    claude_client.py
    client_registry.py
    deepseek_client.py
    google_gemini_client.py
    grok_client.py
    openai_client.py
    provider_adapter.py

  config/
    __init__.py
    config.py
    model_registry.yaml
    provider_catalog.py
    providers.yaml
    pricing.py

  context/
    __init__.py
    conversation_manager.py

  db/
    __init__.py
    billing_repository.py
    engine.py
    schema_public_snapshot.sql
    migrations/
      20260218_add_request_group_id_to_llm_requests.sql
      20260218_llm_requests_api_key_owner_guard.sql
      20260222_b2b_launch_tables.sql
      20260222_go_live_hardening.sql
      20260320_add_attachments_foundation.sql
      20260718_add_b2c_billing_foundation.sql
      20260727_add_cortex_analysis_runs.sql
      20260729_add_unified_ai_credits.sql
      20260730_add_usage_reservation_activity.sql
      20260731_add_model_pricing_audit.sql
      20260802_add_cortex_analysis_attribution.sql
      20260804_add_generation_budget_audit.sql
      20260807_add_cache_aware_credit_accounting.sql
      20260811_add_direct_s3_attachment_upload.sql
    repository.py
    session.py
    tables.py

  docs/
    README.md
    CHANGELOG.md
    COMPARE_MODE_GUIDE.md
    DATABASE_INTEGRATION_COMPLETE.md
    FASTAPI_README.md
    LOGGING.md
    PROJECT_MAP.md
    REFACTORING_SUMMARY.md
    SMART_ROUTING_DIAGRAM.md
    TAVILY_INTEGRATION.md
    UNIFIED_RESPONSE_CONTRACT.md
    USER_FLOW_DIAGRAM_SOURCE.md
    adr/
      0001-architecture-baseline-and-deploy-boundaries.md
      0002-provider-validation-and-safety-rails.md
      0003-component-deployment-readiness-boundaries.md
      0004-react-only-frontend-boundary.md
    runbooks/
      db-migrations.md
      direct-s3-attachment-rollout.md
    postman/
      CortexAI_B2B.postman_collection.json

  frontend-react/
    index.html
    package.json
    package-lock.json
    runtime-config.example.js
    vite.config.ts
    src/
      main.tsx
      App.tsx
      components/
      hooks/
      api/
      store/

  models/
    __init__.py
    multi_unified_response.py
    unified_response.py
    user_context.py

  orchestrator/
    __init__.py
    core.py
    fallback_manager.py
    model_registry.py
    model_selector.py
    multi_orchestrator.py
    prompt_analyzer.py
    response_validator.py
    routing_types.py
    smart_router.py
    tier_decider.py

  scripts/
    build_frontend_artifact.py
    db_mode_smoke.py
    e2e_b2b_checklist.py
    generate_proof_pack.py
    onboard_tenant.py
    release_gate.py
    run_playwright_mcp.py
    test_report_runner.py

  server/
    __init__.py
    app.py
    byok_service.py
    circuit_breaker.py
    dependencies.py
    middleware.py
    persistence.py
    privacy.py
    rate_limit.py
    runtime_checks.py
    savings.py
    usage_reporting.py
    utils.py
    billing/
      account_service.py
      entitlement_service.py
      errors.py
      models.py
      plan_catalog.py
      session_service.py
      stripe_gateway.py
      subscription_service.py
    routes/
      __init__.py
      admin.py
      byok.py
      billing.py
      catalog.py
      chat.py
      compare.py
      entitlements.py
      files.py
      health.py
      history.py
      optimize.py
      reporting.py
      whoami.py
    schemas/
      __init__.py
      requests.py
      responses.py

  tools/
    create_api_key.py
    register_dev_key.py
    web/
      __init__.py
      cache.py
      contracts.py
      factory.py
      intent.py
      research_pack.py
      research_state.py
      research_state_store.py
      session_state.py
      tavily_client.py
      tavily_resolver.py
      tavily_service.py

  utils/
    __init__.py
    GeminiAvailableModels.py
    api_key_utils.py
    cost_calculator.py
    logger.py
    model_utils.py
    prompt_optimizer.py
    token_tracker.py

  tests/
    __init__.py
    conftest.py
    README.md
    test_api.py
    test_api_key_hashing.py
    test_api_persistence_guardrails.py
    test_b2b_launch_features.py
    test_component_boundaries.py
    test_compare_session_totals.py
    test_conversation.py
    test_fallback_manager.py
    test_fastapi_contract_and_guardrails.py
    test_model_selector.py
    test_model_utils.py
    test_multi_compare_mode.py
    test_pricing.py
    test_prompt_analyzer.py
    test_prompt_optimizer.py
    test_registry_pricing_alignment.py
    test_response_validator.py
    test_research_pack.py
    test_routing_regression.py
    test_server_utils.py
    test_smart_router_metadata.py
    test_tavily_client.py
    test_tavily_resolver.py
    test_tier_decider.py
    test_unified_response_contract.py
    test_dynamic_provider_discovery_e2e.py
    ... (additional unit/integration suites)

  e2e/
    README.md
    package.json
    playwright.config.mjs
    global-setup.mjs
    global-teardown.mjs
    fixtures/
      live-e2e.mjs
    helpers/
      api.mjs
      cleanup.mjs
      config.mjs
      db.mjs
      ids.mjs
      network.mjs
      prompts.mjs
      runtime-state.mjs
      ui.mjs
    server/
      run_e2e_server.py
      fault_injection.py
      stream_tuning.py
    specs/
      00.app-readiness.spec.mjs
      10.ask-and-routing.spec.mjs
      20.session-and-history.spec.mjs
      30.persistence-and-fallback.spec.mjs
      40.compare-three-models.spec.mjs
      50.high-impact-business-ui.spec.mjs
      _helpers.mjs
    test-data/
      routing-outcomes.mjs
```

## What to Edit for Common Changes

- Add a new API endpoint: `server/routes/*.py` + wire router in `server/app.py` + request/response models in `server/schemas/`.
- Add business logic/service code: `server/*.py` (keep route handlers thin; move logic into services).
- Add or change provider behavior: `api/*.py`, `api/client_registry.py`, orchestration flow in `orchestrator/core.py`, and provider metadata in `config/providers.yaml`.
- Change smart routing rules: `orchestrator/prompt_analyzer.py`, `orchestrator/tier_decider.py`, `orchestrator/model_selector.py`, `orchestrator/smart_router.py`.
- Change web research behavior: `orchestrator/core.py` + `tools/web/intent.py` + `tools/web/tavily_service.py` + `tools/web/research_pack.py`.
- Add DB tables/columns/indexes: create SQL migration in `db/migrations/`, then update reflected usage in `db/tables.py` and queries in `db/repository.py`.
- Change persistence/audit behavior for FastAPI: `server/persistence.py` (shared write path for chat/compare/stream).
- Change usage/savings/BYOK behavior: `server/usage_reporting.py`, `server/savings.py`, `server/byok_service.py`, and related routes under `server/routes/`.
- Change Stripe hosted-session behavior: `server/billing/stripe_gateway.py`, `server/billing/session_service.py`, `server/routes/billing.py`, strict billing schemas, `.env.example`, `docs/runbooks/stripe-billing.md`, Postman, and `tests/test_stripe_billing.py`. Never move Price, amount, Customer, or redirect authority into the client.
- Change Stripe webhook lifecycle behavior: `server/billing/webhook_service.py`, `server/billing/stripe_gateway.py`, `db/billing_repository.py`, `server/billing/subscription_service.py`, `server/routes/billing.py`, billing response schemas, runbooks, Postman, and `tests/test_stripe_webhooks.py`. Preserve raw-body verification, provider-event idempotency, stale-event protection, server-owned Price mapping, and conservative plan resolution.
- Add/adjust guardrails: `server/rate_limit.py`, `server/circuit_breaker.py`, privacy policy in `server/privacy.py`.
- Update CI/workflow behavior: `.github/workflows/ci.yml`, `.github/workflows/live-e2e.yml`, `.github/workflows/incident-regression-38.yml`.
- Add tenant/dev tooling scripts: `scripts/` (runtime checks/reporting) and `tools/` (operator/dev helpers).
- Add tests: put new tests in `tests/` (mirror by feature area) and run `python -m pytest -q` + `python scripts/release_gate.py`.
- Update API docs and examples after behavior changes: `README.md` and `docs/postman/CortexAI_B2B.postman_collection.json`.

Last updated: 2026-07-29
