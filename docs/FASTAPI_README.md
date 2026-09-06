# FastAPI Integration

## Quick Start

1. Install dependencies:
```bash
pip install -r requirements.txt
```
`requirements.txt` includes `tavily-python` for research-enabled Ask/Compare flows and a compatible Stripe 15.3 minor range for hosted billing sessions. FastAPI excludes `0.136.3` because `pip-audit` currently flags that release with advisory `MAL-2026-4750`.

React UI dependencies live in `frontend-react/package.json` and `frontend-react/package-lock.json`; they are not Python requirements:
```bash
npm ci --prefix frontend-react
```
Use Node.js 20.x for the React/Vite toolchain.

2. Configure auth in `.env`:
```ini
API_KEYS=dev-key-1,dev-key-2
```

3. Configure required DB persistence:
```ini
DATABASE_URL=postgresql+psycopg://...
```

Notes:
- `DATABASE_URL` is required at startup.
- PostgreSQL URLs are required by default (`postgresql://` or `postgresql+psycopg://`).
- Dev-only override: `ALLOW_NON_POSTGRES_DATABASE_URL=true`.

4. Optional deployment boundary controls:
```ini
SERVE_FRONTEND=false
FRONTEND_DIR=frontend-react/dist
# Optional frontend runtime-config override for clients that honor apiBase
# FRONTEND_RUNTIME_API_BASE=https://kudlo.triobrain.com
# Optional explicit browser flag override (otherwise inherits ENABLE_DEV_SESSION_LOGIN)
# FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN=false
# Optional browser-visible dev-login token (local only)
# FRONTEND_RUNTIME_DEV_SESSION_LOGIN_TOKEN=
# Reverse-proxy handling (enabled by default for CloudFront/ALB deployments)
# ENABLE_PROXY_HEADERS=true
# TRUSTED_PROXY_IPS=*
# Session cookie policy: leave the Secure override unset to auto-detect HTTPS; 0 max age uses a browser-session cookie
# SESSION_COOKIE_SECURE=
# SESSION_MAX_AGE_SECONDS=604800
# Cognito JWKS/token endpoint certificate verification; keep true in production
# COGNITO_SSL_VERIFY=true
# Optional stream keep-alive interval; set 0 to disable heartbeat events
# STREAM_HEARTBEAT_INTERVAL_SECONDS=15
# Optional prompt optimization
# ENABLE_PROMPT_OPTIMIZATION=false
# ENABLE_ORCHESTRATOR_PROMPT_OPTIMIZATION=false
# PROMPT_OPTIMIZER_PROVIDER=gemini
# PROMPT_OPTIMIZER_MODEL=
# PROMPT_OPTIMIZER_MAX_RETRIES=3
# PROMPT_OPTIMIZER_TIMEOUT_MS=5000
# PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES=2
# PROMPT_OPTIMIZER_MAX_OUTPUT_TOKENS=450
# PROMPT_OPTIMIZER_TEMPERATURE=0.2
# On-demand Compare synthesis model (requires OPENAI_API_KEY)
# CORTEX_ANALYSIS_MODEL=gpt-5.4-mini
# Optional Tavily search-option resolver
# TAVILY_ENHANCED_SEARCH_ENABLED=true
# TAVILY_CHUNKS_PER_SOURCE=3
# TAVILY_ENHANCED_SEARCH_DOMAIN_RULES=true
```

5. Start the full local app:
```bash
python run_app.py
```

This starts FastAPI on `http://127.0.0.1:8000` and the React/Vite frontend on `http://127.0.0.1:5173`. The runner starts the API with `SERVE_FRONTEND=false`, launches `npm run --prefix frontend-react dev`, and sets Vite's proxy target plus `FRONTEND_RUNTIME_API_BASE` from the selected API host/port.

For IntelliJ/PyCharm local plan testing, use the project virtual environment, set the script to `$ProjectFileDir$/run_app.py`, the working directory to `$ProjectFileDir$`, and choose one Program arguments value: `--subscription-plan free`, `plus`, `pro`, or `unrestricted`. The option forces local mode, disables Stripe, enables dev-session bootstrap, and is rejected for non-loopback hosts. `unrestricted` uses the normal entitlement/metering path with Pro access and very high allowances; global attachment/provider safety constraints still apply. Omit the option to use normal `.env`/Stripe state.

For local browser session bootstrap, keep `ENABLE_DEV_SESSION_LOGIN=true` in `.env` or run:
```bash
python run_app.py --enable-dev-login
```

Start only the FastAPI server:
```bash
python run_server.py --reload
```

To serve the React/Vite frontend through FastAPI, build it and point `FRONTEND_DIR` to the built output:
```powershell
npm ci --prefix frontend-react
npm run --prefix frontend-react build
$env:FRONTEND_DIR=(Resolve-Path .\frontend-react\dist).Path
python run_server.py --reload
```

If `FRONTEND_DIR` is unset, `server/app.py` serves `frontend-react/dist`. Set the variable explicitly when compiled assets live elsewhere.

6. Open docs:
- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`
- Frontend composer keyboard UX: `Enter` sends prompt, `Shift+Enter` inserts newline.
- React startup waits for Cognito/local dev-session bootstrap before fetching session-scoped model and history data. Signed-out Cognito users see a workspace sign-in gate instead of the Ask/Compare composer, while authenticated and local dev-session users restore the persisted active `session_id` transcript when the page was reloaded, resumed, or silently reauthenticated in the same browser.
- React Ask/Compare turns send `context.session_id`, bounded `conversation_history`, and `new_session` to preserve selected-thread continuity while allowing explicit New Chat resets.
- The React sidebar uses a compact navigation rail with subtle mode/current-session states plus Usage, AI credits, and Models destinations. Desktop has an icon-only top-right control that collapses the sidebar to a narrow action rail and expands it back to the full history view; mobile remains on the separate Ask/Compare/History bottom navigation, with Usage, AI credits, and Models reached from the account menu. The expanded desktop `Recent` list displays up to the 100 newest grouped chat threads by Today, Yesterday, and month/day, with one 36px row per session: compact 11.5px ellipsized title and a narrowed `MODE · time` caption, with no leading mode glyph, to preserve substantially more identifying title text. Hover/focus replaces the caption with a Rename/Delete menu; renamed titles persist in `sessions.title`, Delete retains the short in-row confirmation, and keyboard rows support arrows, Enter, R, D, and Escape. The collapsed desktop rail and the separate mobile History surface remain unchanged.
- Selecting a React history row reloads the complete session transcript. Ask rows are restored chronologically and Compare target rows are grouped into one turn by `request_group_id`.
- Explicit frontend fresh sign-in starts an empty new chat session; browser refreshes, Chrome tab reload/resume, same-browser reauth, and explicit History selections continue the selected thread.
- React posts non-sensitive lifecycle diagnostics to `/v1/client-diagnostics`; backend logs them as `frontend.diagnostic` events so production refresh reports can be separated into reload/navigation, tab discard, back/forward cache restore, long main-thread task, or frontend error cases.
- React attachment UX reads `directAttachmentUploads` and `legacyAttachmentUploads` from `/runtime-config.js`, which mirror the backend rollout flags. In direct mode it validates the complete selection locally from server entitlements, renders immediate per-file Preparing/Uploading/Processing/Ready state, authorizes metadata through `POST /v1/files/upload-intents`, sends bytes to S3 with header-free `XMLHttpRequest` forms, and calls `POST /v1/files/{file_id}/complete` only after S3 succeeds. Two transfers run concurrently; failures and retries are per-file, polling is bounded, removal aborts and deletes, and Ask/Compare Send is blocked until all selected files are Cortex-verified `ready`. With direct mode off, the same queue UI temporarily uses multipart `POST /v1/files/upload-batch` while `ATTACHMENTS_LEGACY_PROXY_UPLOAD_ENABLED=true`. Upload/storage remains free.
- React Ask defaults `Web` on for new page sessions, React Compare defaults `With sources` on, and users can turn either off for the current page session. Compare streams `/v1/compare/stream` events into per-model response columns.
- React Ask waits for both `/v1/models` and `/v1/entitlements` before initializing its manual model. Turning Smart routing off shows the plan default: Free uses `openai:gpt-5.6-luna`, Plus uses `claude:claude-sonnet-4-6`, and Pro uses `openai:gpt-5.6-terra`. Valid existing/manual selections remain unchanged, and locked models remain visible in the picker.
- React Compare keeps every selected response visible in a responsive grid without horizontal response scrolling on desktop and tablet widths: three columns on wide desktop, two at tablet widths, and stacked tall cards at the app's tablet/mobile shell breakpoint. Phone-sized mobile uses a segmented model switcher, shows one selected response card at a time in natural page flow, and elevates the stuck switcher into a frosted provider-tinted bar without changing model-pill horizontal positions.
- Model headers and action footers remain fixed inside each desktop/tablet Compare card while only the answer body scrolls. The transcript reserves bottom breathing room above the persistent composer so the input area does not compress the reading workspace.
- React Compare uses the same right-aligned user-message bubble as Ask mode and keeps aggregate totals in a separate compact row. Model cards show a friendly model name with the exact API model ID, use compact icon actions, and reserve most of the column height for response content.
- React Ask and Compare use one rounded composer shell with a borderless textarea that starts at one line and auto-grows to a bounded height, attachment chips above a compact action row, routing controls, and a fixed-size send action. Mode changes use the app navigation; the composer does not duplicate the Ask/Compare switch. Compare model selectors remain in a compact options row above the textarea and scroll horizontally on narrow screens when needed.
- The React composer shell keeps a transparent structural border to prevent layout movement and uses soft elevation rather than a visible rectangular outline. Textarea focus suppresses the browser outline and increases the shell shadow on desktop and mobile.
- The empty React Ask workspace explains the product value across answers, file analysis, content generation, and model comparison. Its four responsive example actions populate the composer for debugging, summarization, writing refinement, and file-analysis tasks without triggering a request.
- The empty React Compare workspace explains that one prompt can be reviewed across multiple selected models and frames accuracy, depth, speed, tone, and usefulness as practical comparison dimensions. Its three responsive examples populate the composer without changing model selections or triggering a request.
- On mobile answer screens, the follow-up composer rests as a docked pill above the fixed Ask/Compare/History navigation. Tapping the pill opens the bottom-sheet composer, focuses its textarea, and places the cursor at the end of any draft so typing can begin without a second tap. Attachment chips grow upward without narrowing the textarea or displacing the send action. Answer transcripts reserve enough bottom scroll clearance for response copy, regenerate, and feedback actions to remain reachable above the dock.
- Mobile uses a persistent square-pen header action to start a new session from Ask, Compare, or History. The action cancels active generation, clears the current thread, returns to chat, and preserves the selected mode; the History panel does not render a separate New chat button.
- Frontend Compare selectors keep at least two active models and send only active selected models in compare requests. Initial empty slots wait for `/v1/entitlements` and use only models whose billing class is allowed by the effective Free, Plus, or Pro plan; locked higher-plan models remain in the offering, and valid existing/manual selections are preserved. Within that eligible default set, the offline fallback prefers `openai:gpt-5.6-luna` plus `claude:claude-sonnet-5`, and Add Model prefers `deepseek:deepseek-v4-flash`; normal authenticated startup replaces that fallback with selectable `/v1/models` rows. Remove controls appear with three active models and compact whichever two remain after any slot is removed.
- React manual Ask and Compare controls render through the same accessible provider-first model picker. It opens with provider logos and model counts. On fine-pointer desktop layouts, hovering a provider immediately reveals its readable model labels, exact IDs, credit-use hints, locks, and active state in an adjacent panel; the preview remains stable while crossing panels, switches on another provider hover, and dismisses shortly after leaving the picker. Click plus Right/Left Arrow remain accessible fallbacks. Touch/mobile layouts use a compact tap-to-model drill-down with Back. The viewport-positioned body portal prevents Compare's horizontally scrollable model row from clipping either layout. Compare supplies duplicate-selection prevention and removal behavior; synchronized hidden native selects preserve existing Playwright selectors and `selectOption` flows.
- Frontend Compare response cards use restrained model headings, compact Markdown paragraph/list spacing, compact footers, and a packed mono metric strip for completed duration and AI-credit usage; success/error counts and aggregate AI-credit usage remain in the summary bar without token totals.
- React composer feature chips provide legacy-compatible explanatory tooltips for Smart, Web/With sources, and Improve in Ask and Compare. Enabled chips use a theme-aware high-contrast fill, label, and accent ring so their state remains clear in light and dark themes. Compare's With sources and Improve controls use the same background, border, label, and shadow treatment whenever they share the same toggle state. The descriptions are associated through `aria-describedby`, open on hover or keyboard focus, and remain viewport-contained on mobile. A touch tap toggles the chip and keeps its tooltip visible for two seconds.
- React desktop top mode navigation keeps the active and inactive Ask/Compare labels legible in both light and dark themes, with a theme-accent underline identifying the selected mode independently of the sidebar navigation.
- Mobile and desktop completed response-card duration and AI-credit usage appear directly in the header without a run-details chevron. Loading and failed cards keep a muted elapsed/status line visible on mobile and desktop. The frontend displays the same UI-observed elapsed duration when live timestamps are available and falls back to API `latency_ms` for restored rows. Token usage remains in the API response and React data layer for persistence and reporting but is not rendered on Ask or Compare results.
- React response headers reuse the model picker's shared provider-logo and model-presentation resolver, including the provider-initial fallback when an image is unavailable.
- React exposes `/models` for a task-first model selection guide. Authenticated production rows are generated from `/v1/models`, including availability, lifecycle, current prices, and official provider source links; `frontend-react/src/config/models.data.json` supplies only offline presentation/filter defaults. Update `config/model_registry.yaml` for catalogue or pricing changes instead of duplicating model rows in React.
- The current selectable catalogue contains 21 models. Claude Sonnet 4.6 uses the `advanced` billing class and is available to Plus and Pro; Claude Opus 4.5 and 4.6 use `premium` and are available to Pro. `/v1/models` exposes these rows and their official Anthropic pricing/lifecycle evidence directly from the canonical registry.
- `config/subscription_plans.yaml` is the server-owned Free/Plus/Pro plan catalogue. `server/billing/plan_catalog.py` validates and caches it during API startup, including ranks, prices, Stripe price environment-variable mappings, entitlements, allowances, limits, and allowed billing classes.
- `db/billing_repository.py` provides transaction-neutral access to billing accounts, provider subscription snapshots, usage periods/counters/reservations, and webhook idempotency records created by `db/migrations/20260718_add_b2c_billing_foundation.sql`.
- `server/billing/account_service.py` validates user ownership and lazily creates B2C accounts. `server/billing/subscription_service.py` applies the server-side lifecycle/grace policy and creates the effective usage period. `server/billing/entitlement_service.py` returns feature/model/file decisions and exact reservation quantities without mutating counters.
- `server/billing/metering_service.py` owns atomic allowance mutation. It locks the billing owner for idempotency-key creation, locks required counters in deterministic order, rejects over-limit reservations before mutation, supplements underestimates when capacity exists, settles only safe successful quantities, releases unused quantities, and expires clearly stale reservations after a default 30-minute threshold. The API runs cleanup once at startup and every five minutes by default, while persisted heartbeats protect demonstrably active requests and `FOR UPDATE SKIP LOCKED` makes multiple instances safe.
- `server/billing/enforcement_service.py` composes effective-plan resolution, entitlement evaluation, atomic reservation, and output-aware settlement/release for DB-mode Ask, Compare, Optimize, Cortex Analysis, and attachment-backed model calls. `server/persistence.py` owns the short committing units of work; no billing transaction remains open during a provider, optimizer, or object-storage call.
- `BILLING_ENABLED=false` keeps valid Cortex grants effective, falls back to Free for other users, keeps Stripe lazy, and makes Checkout, Portal, and webhook routes return `503 billing_not_configured`. `DEV_SUBSCRIPTION_PLAN` works only when billing is disabled and the runtime is explicitly local/development. The `unrestricted` development value additionally requires `DEV_SUBSCRIPTION_BYPASS_ENABLED=true`; prefer the guarded `run_app.py --subscription-plan unrestricted` entrypoint.
- With billing enabled, startup validates the secret key, webhook signing secret, paid-plan Price IDs, server redirect URLs, and optional API version. The API rejects client-supplied Price IDs, amounts, currencies, Customer IDs, and redirects. `server/billing/webhook_service.py` makes verified Stripe Checkout/subscription/invoice state authoritative, locks provider-event retries, rejects stale snapshots, preserves usage counters across same-period changes, and delegates paid/grace/cancellation access to `subscription_service.py`.
- `/v1/chat`, `/v1/chat/stream`, `/v1/compare`, and `/v1/compare/stream` calculate one effective output limit and use it for both provider execution and credit reservation. They settle actual successful input/output credits and release unused estimates and failed targets. Advanced Web Search reserves 10,000 Cortex credits for the normal two-credit Tavily call, then settles `provider credits used x 5,000`; missing Tavily usage falls back to two credits and is marked estimated. Cached/session-reused research is free for the turn. Compare shares retrieval and performs aggregate partial settlement. Improve Prompt reserves all configured attempts and settles each billable usage item. Cortex Analysis reserves and settles its source-context synthesis as a separate unified-wallet call without charging again for reused Compare research. Upload/storage itself is free.
- Cache-aware accounting partitions reported prompt usage into normal, cached-read, and cache-write tokens, applies effective provider-price ratios to the existing Cortex input multiplier, and falls back to the full multiplier when pricing evidence is absent. The canonical calculator feeds settlement, response DTOs, and history. Initial rollout computes `cache_aware_shadow_total`, `legacy_total`, and their delta while `CACHE_AWARE_CREDIT_SETTLEMENT_ENABLED=false` keeps legacy settlement authoritative.
- Provider caching, persistent research reuse, optimizer/Cortex reuse, credit-aware ceilings, and context compaction are independently flag-controlled. Affinity identifiers are HMAC-SHA256 values derived with `CACHE_KEY_SECRET`; raw session, user, prompt, and file content never appears in cache keys or cache telemetry.
- Ask/Compare consumer-credit settlement and response DTO statistics use the canonical requested model even when a provider reports a versioned served-model snapshot. Served/pricing identities remain available for response audit and provider-cost calculation. Per-model response credits, aggregate Compare credits, and itemized ledger charges therefore use the same multipliers, and one provider snapshot cannot prevent the other successful Compare targets from producing ledger rows. Any finalization failure releases and unregisters the reservation instead of leaving it heartbeat-active.
- Focused validation: `python -m pytest tests/test_billing_metering.py tests/test_billing_entitlements.py tests/test_stripe_billing.py tests/test_stripe_webhooks.py tests/test_baseline_safety_rails.py tests/test_fastapi_contract_and_guardrails.py -q`. Use `BILLING_TEST_DATABASE_URL` with `tests/test_billing_postgres_integration.py` for real row-lock concurrency coverage.
- `/v1/models?enabled_only=true` exposes only currently selectable rows. `enabled_only=false` also returns compatibility and lifecycle records retained for historical resolution. Every row includes `requested`-independent canonical identity, display/lifecycle/replacement/alias fields, current input/output/cached-input prices, the effective pricing rule and date, official pricing/lifecycle URLs with `source_verified_at`, context/output limits, reasoning modes, attachment capabilities, and `billing_class`/credit metadata. Credit access classes remain independent from smart-routing `tier`.
- Pending Ask and Compare cards show independent contextual loading blocks with a subtle sparkle and skeleton lines. A card removes its loading state on its first streamed token or error without waiting for the other Compare targets.
- Smart Ask pending cards remain model-neutral because the `start` provider/model is a routing preview that can differ after research and runtime context are applied. They show `Smart routing` while waiting and adopt the authoritative provider/model from `response_done`.
- Frontend response card controls render as a minimal icon row for copy, regenerate, and feedback actions. Copy shows a brief visible success confirmation in the toolbar. Regenerate uses the existing `/v1/chat/stream` path, refills the clicked response card in place, and preserves the original source-enabled flag. Compare card regeneration is intentionally single-target so clicking one card does not rerun or replace the other comparison cards.
- Frontend response sources render inline as publisher-name citation pills derived from `web_source_items`; grouped markers such as `[1][2][3]` collapse into one pill with a preview card listing each linked source. Desktop keeps the hover preview viewport-contained and directly beside its pill, while phone-sized mobile keeps the tap-to-open bottom sheet.
- Frontend response Markdown keeps explicit ordered-list numbering when numbered items are split by explanatory text.
- React response Markdown renders inline citation pills with tap/click source previews, blockquote callout styling, styled code blocks with copy controls, GFM tables, and sanitized provider error states. Desktop tables scroll within the response card, while mobile tables stack cells under their column labels.
- Frontend streaming responses render buffered Markdown progressively for Ask and Compare.
- Submitting a new Ask or Compare turn always performs one smooth reveal of the new turn, including when the user was viewing an older turn.
- Frontend streaming responses do not auto-follow generated text as it grows, and the transcript no longer renders a floating down-arrow jump control.

## Endpoints

### CortexAI Work

- `POST /v1/work/sessions` and `GET /v1/work/sessions`
- `GET /v1/work/sessions/{work_session_id}`
- `GET /v1/work/sessions/{work_session_id}/runs`
- `GET /v1/work/sessions/{work_session_id}/runs/latest`
- `POST /v1/work/sessions/{work_session_id}/runs`
- `POST /v1/work/sessions/{work_session_id}/instructions`
- `GET /v1/work/runs/{run_id}`
- `GET /v1/work/runs/{run_id}/events?after_sequence=N`
- `GET /v1/work/runs/{run_id}/stream?after_sequence=N`
- `POST /v1/work/runs/{run_id}/cancel`
- `GET /v1/work/runs/{run_id}/artifacts`
- `GET /v1/work/runs/{run_id}/artifacts/{file_id}/download`
- `GET /v1/work/approvals/{approval_id}`
- `POST /v1/work/approvals/{approval_id}/approve`
- `POST /v1/work/approvals/{approval_id}/deny`
- `GET /v1/tools/catalog` and `GET /v1/tools/connections`
- `POST /v1/tools/connections`, `POST /v1/tools/connections/{id}/test`, and
  `DELETE /v1/tools/connections/{id}`
- `POST /v1/tools/{connector_key}/oauth/start` and
  `GET /v1/tools/{connector_key}/oauth/callback`

Every Work lookup is authenticated and joined through the owning Work session.
`POST /v1/work/sessions` collapses whitespace and removes Unicode control or
format characters from `title`, yielding an optional provider-safe single-line
value of at most 200 characters. The Managed Agent adapter repeats this
normalization before remote session creation so older stored titles are safe to
retry.
Starting a run accepts `Idempotency-Key`; the ID is the idempotency key within the
user's Work history. React stores the newly created session before starting its
first run, reuses that session after a structured start denial, and shows only
run-backed Work sessions in the sidebar; zero-run shells remain outside visible
history. While the accepted-run response is pending, React immediately renders
a `Starting work` workspace with the submitted instruction. Stop remains
unavailable until the backend returns a durable run ID. The activity rail omits
unlabeled internal progress events, animates
only the latest visible activity while the run is nonterminal, and renders no
active indicators after a terminal outcome; completed runs show every plan step
done. SSE emits persisted events in sequence, accepts either the
`after_sequence` query or `Last-Event-ID`, sends heartbeat events without
advancing the durable cursor, and can reconstruct the current state after a
process or browser reconnect. When a run status becomes terminal ahead of the
browser's current event sequence, React fetches and merges the remaining events
before stopping the stream so the final written outcome appears without a page
refresh. Provider thinking and raw secret values are never included in the
public event payload.

`GET /v1/work/sessions/{work_session_id}/runs` returns every owned run in
chronological order. React hydrates each run's durable events and artifacts and
renders a session transcript, so sending an instruction appends a turn without
removing earlier prompts, outcomes, or deliverables. Web/MCP selection changes
are applied to the existing provider session to retain context. When immutable
provider vault resources require a replacement session, the backend supplies a
bounded PostgreSQL-backed transcript of prior visible turns with the new
instruction.

Artifact listing for a terminal run performs a best-effort idempotent import
retry before returning Cortex-owned files. Import uses the provider session ID
recorded on that run, skips input/non-downloadable files, and isolates each
provider output so one failed download or storage write does not suppress the
other deliverables.

The run request accepts `web_mode: "auto" | "on" | "off"` and defaults to
`auto`. The backend resolves `auto` from the instruction's current-information
intent, persists both requested and effective Web state, and remains the
authority even if browser state changes while a session is being created.
Legacy `web_enabled` booleans remain accepted as explicit On/Off requests.

The master feature flag is `CORTEX_WORK_ENABLED`. A disabled environment returns
404 for Work operations and omits Work navigation through `/runtime-config.js`.
The full provider, MCP, OAuth, AWS, rollout, rollback, and troubleshooting
contract is in `docs/runbooks/cortex-work.md`.

The default Work ceiling is 1,000,000 raw AI-credit units ($1.00), presented as
1,000 AI credits in the browser and clamped to the effective plan limit. Managed
Agents enforces the provider session budget and
pauses it at `budget_reached`; reconciliation never emits timer-driven
`user.interrupt` events. Reused provider sessions receive a cumulative cap
extension for the newly reserved run. If the prior turn is budget-paused, that
budget update resumes it without sending a concurrent follow-up message.
Built-in file/search reads are automatic, while bash/write/edit and sensitive
or mutating connector actions retain approval enforcement.

Separately, each run defaults to a 40,000 output-token ceiling. The always-on
lease-based reconciler requests concise finalization at 32,000 and interrupts
at 40,000, recording `output_limit_reached` after the provider stops. This
worker also reconciles active runs when no browser SSE request is connected.
Provider usage is sampled, so the recorded total may include a bounded amount
produced between snapshots.

Work billing prices normal input, cache-read input, cache-write input, output,
active runtime, and web searches from their independent cumulative deltas. It
also converts the provider's cumulative USD `list_cost` delta to AI credits and
uses it as a minimum settlement floor when the reconstructed component charge
is lower. Non-USD or malformed provider cost snapshots fail reconciliation so
the reservation remains open for investigation rather than being underbilled.
The pricing model is resolved from the retrieved Managed Agent session's Agent
snapshot and persisted with provider model and Agent identity. There is no
runtime `ANTHROPIC_MANAGED_BILLING_MODEL` fallback; missing, unknown, or
multi-model identity fails closed before the task is sent or settled.

- `GET /health`
- `GET /health/runtime`
- `GET /runtime-config.js`
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
- `POST /v1/optimize`
- `GET /v1/history`
- `PATCH /v1/history/session/{session_id}`
- `DELETE /v1/history/{entry_id}`
- `DELETE /v1/history?session_id=<optional>`
- `GET /v1/whoami`
- `GET /v1/entitlements`
- `POST /v1/billing/estimate-generation`
- `POST /v1/billing/checkout-session`
- `POST /v1/billing/portal-session`
- `POST /v1/billing/webhook`
- `GET /v1/usage/summary?from=YYYY-MM-DD&to=YYYY-MM-DD`
- `GET /v1/usage?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=day|provider|model|operation`
- `GET /v1/savings?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=day|provider|model`
- `GET /v1/usage/export?format=csv&from=...&to=...&group_by=...`
- `GET /v1/savings/export?format=csv&from=...&to=...&group_by=...`
- `POST /v1/byok`
- `GET /v1/byok/status`
- `DELETE /v1/byok?provider=<provider-id>`
- `GET /v1/admin/request-groups/{request_group_id}/failed-attempts`
- `GET /v1/auth/cognito-config`
- `POST /v1/auth/dev-login`
- `POST /v1/auth/logout`

### Direct attachment upload contract

Direct upload is metadata-first and session-scoped. With
`ATTACHMENTS_DIRECT_UPLOAD_ENABLED=true`, submit the entire selection before
any S3 write:

```json
{
  "files": [
    {"filename": "report.pdf", "mime_type": "application/pdf", "size_bytes": 48123}
  ],
  "provider": "openai",
  "model": "gpt-5.6-luna"
}
```

`POST /v1/files/upload-intents` returns one `uploading` file record with
`upload.url`, `upload.fields`, and `upload.expires_at` per item. The client must
construct `FormData`, append every returned field unchanged, append the file
last, and POST directly to `upload.url`. It must never invent or modify the key,
content type, `x-amz-meta-cortex-file-id`, or optional exact encryption fields.
`ATTACHMENTS_S3_SERVER_SIDE_ENCRYPTION` supports `AES256` or `aws:kms`; the
optional `ATTACHMENTS_S3_SSE_KMS_KEY_ID` is valid only with `aws:kms`. Leave both
blank to use bucket default encryption. After S3 succeeds, call
`POST /v1/files/{file_id}/complete` with no body. Completion is idempotent for
`ready`/`processing`; missing objects return
`409 attachment_upload_not_complete`, verification mismatches return
`409 attachment_upload_mismatch`, and expired intents return
`410 attachment_upload_expired`. Only `ready` files are accepted by Ask/Compare.

`DELETE /v1/files/{file_id}` returns `deleting` immediately and queues the
existing cleanup worker; repeated calls are safe. The migration
`20260811_add_direct_s3_attachment_upload.sql` makes `sha256` nullable for
pre-byte intents and extends the lifecycle constraint with `uploading` and
`deleting`. PostgreSQL startup fails fast when the direct flag is on but this
schema contract is absent. This repository has no AWS infrastructure as code.
See `docs/runbooks/direct-s3-attachment-rollout.md` for the repo/AWS ownership
boundary, exact S3 CORS shape, IAM/KMS and bucket-policy checks, WAF/CloudTrail
diagnostics, cleanup verification, smoke tests, rollout, and rollback.

### History response contract

`GET /v1/history?limit=<n>&session_id=<optional>` returns persisted request rows, newest first. It does not pre-group records for presentation.

- `session_id` identifies the user-visible conversation thread.
- `session_title` is optional; user-authored values replace the first prompt as the thread label. React ignores the system-generated `API Chat` and `API Compare` placeholders.
- `request_group_id` is optional and is populated for Compare target rows.
- One Ask turn produces one row.
- One Compare turn produces one row per target model; all target rows from that turn share the same `request_group_id`.
- Completed rows include `prompt_tokens`, `completion_tokens`, `ai_credits`, `credit_usage_estimated`, `research_ai_credits`, and `research_credit_usage_estimated`. The credit values are the persisted response-card snapshot; React uses the shared research component once when rebuilding a Compare aggregate. Legacy rows without a snapshot derive their model-credit value from persisted token counts.
- Completed rows also expose `cached_input_tokens`, `cache_write_tokens`, `reasoning_tokens`, `cache_hit`, `cache_hit_ratio`, `cache_savings_ai_credits`, and `uncached_equivalent_ai_credits`. Savings remain informational; `ai_credits` is authoritative.
- Completed model responses retain `requested_model`, provider-reported `served_model`, `pricing_model`, lifecycle/alias resolution, reasoning mode, cached-input/cache-write/reasoning token detail, and the exact pricing rule/version. History returns the identity and pricing-evidence fields needed to explain old charges after the live catalogue changes.
- If an exact served-model price is absent, the calculator uses the provider's highest current configured rate, marks `pricing_unknown=true`, and persists the full price snapshot; it never turns an unknown model into a zero-dollar response.
- The React client groups sidebar items by `session_id`, reconstructs Compare turns by `request_group_id` when a thread is selected, and persists the active thread id as `cortex_active_session_id` so startup can restore the same transcript after a browser refresh/remount.
- `PATCH /v1/history/session/{session_id}` accepts `{"title":"..."}` to rename one user-owned session. The title is trimmed, limited to 120 characters, and persisted without changing latest-activity ordering.
- `DELETE /v1/history?session_id=<id>` clears only that session's persisted request rows for the authenticated identity; omitting `session_id` clears all history. React per-thread delete uses `DELETE /v1/history/{entry_id}` for each row in the selected thread.

### Usage summary contract

`GET /v1/usage/summary?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>` returns the aggregate contract for the Usage & insights screen. If both dates are omitted, the period defaults to the last 30 inclusive calendar days.

- Request, token, spend, latency, and model reply totals are aggregated from `llm_requests` joined to `llm_responses`.
- `smartRoutedTotal` and per-model `viaSmart` count routing rows whose `routing_mode` is `smart`, `cheap`, or `strong`; missing/explicit/legacy routing rows count as manual.
- `sessionModes` is classified from `llm_requests.route_mode` per `session_id` within the period: Ask only, Compare only, or Mixed. The `sessions.mode` creation value is not used.
- `tokensDeltaPct` compares the selected period with the immediately preceding equal-length period. It returns `0` when both periods have zero tokens and `100` when the current period has tokens but the previous period is zero.
- `activityDaily` always contains 14 entries ending at `period.to`, zero-filled for days without usage.
- Cache/accounting fields include total and average AI credits, normal/cached/cache-write tokens, cache-hit ratio, Cortex and provider-cost cache savings, reservation/settlement/release values, output utilization, reasoning tokens, research calls, and reuse-rate fields. A missing pre-migration table safely returns zero metrics rather than inventing discounts.

## Authentication

Protected `/v1/*` endpoints accept any one of:
- `cortex_session` cookie
- `Authorization: Bearer <gateway-bearer-token>`
- `X-API-Key: <key-from-API_KEYS>`

Invalid or missing credentials return `401`.

### Effective subscription and entitlements

`GET /v1/entitlements` uses API-key, Cognito bearer, or signed-session authentication. It commits lazy account/usage-period creation and returns the unified `allowances.ai_credits` counter with `used`, `reserved`, `limit`, and nonnegative `remaining` values. It also returns the effective plan's server-owned `limits.max_files_per_request` and `limits.max_file_bytes` so clients can explain file denials without copying plan configuration. Free periods are UTC calendar months; Stripe periods use stored provider boundaries; grant periods use monthly UTC anniversaries of the grant start, clipped at expiry.

Plan budgets and safety limits are server-owned: Free has 100,000 credits, 5 requests/minute, and 1 × 10 MB files; Plus has 1,000,000 credits, 15 requests/minute, and 3 × 20 MB files; Pro has 3,000,000 credits, 30 requests/minute, and 5 × 20 MB files. Plus is USD 6.99/month and Pro is USD 12.99/month.

`GET /v1/credits/transactions?limit=100&offset=0` returns the authenticated account's newest immutable reconciliation rows. Each item includes display-only `activity_id` and nullable `query` context derived from the privacy-policy-sanitized initial user query. React shares one activity ID across Prompt Optimizer and the following Ask/Compare request; older rows fall back to `request_id`. Metadata-only storage omits the query, PII redaction applies before storage, and older rows return `query: null`. Model, research, and adjustment items also include the operation, provider/model where applicable, input/output tokens and credits, fixed credits, total credits, provider cost, estimated-usage flag, pricing version, metadata, and timestamp. Tavily research metadata includes `provider_credits_used` and the 5,000-credit conversion factor.

Effective lifecycle rules are server-side and conservative:

- `active`: Stripe paid plan for a valid current stored period
- `trialing`: no Stripe paid access
- `past_due`: paid plan only through `grace_until`; otherwise grant/Free fallback
- `canceled`: paid plan only when `cancel_at_period_end=true` and the stored period has not ended
- `unpaid`, `incomplete`, `incomplete_expired`, `paused`, expired cancellation, unknown status, or unknown plan: valid Cortex grant, then Free

The endpoint returns `plan`, `features`, `model_access`, `limits`, `allowances`, and `period` sections and never exposes provider subscription IDs, Stripe price IDs, customer IDs, amounts, or secrets. `/v1/whoami.plan_tier` remains a compatibility display field populated from the effective plan in database mode; new integrations should use `/v1/entitlements` plus `/v1/whoami.billing.plan_code`.

Cortex-issued Plus/Pro access uses the existing plan catalogue and `EffectiveSubscription`. Resolution order is guarded local override, valid Stripe paid state when enabled, valid Cortex grant, then Free. `/v1/entitlements` returns `plan.source="cortex_grant"`; `/v1/billing/subscription` returns the granted plan, active status, null provider, no cancellation and `can_manage=false`. `/v1/billing/plans.billing_enabled` still means Stripe hosted-billing availability. There is no grant-assignment HTTP endpoint; use the [operator CLI](runbooks/subscription-grants.md). Pricing shows Current plan for granted access while Stripe is disabled; Billing describes CortexAI-provided access and monthly usage resets.

### Stripe hosted billing sessions

The hosted Checkout and Portal routes require signed-session or Cognito bearer identity; API-key-only authentication returns `403 session_auth_required`. The webhook route uses Stripe signature verification instead of user authentication.

`GET /v1/billing/plans` is public and returns only display-safe catalogue fields: USD monthly price, Plus recommendation state, model billing classes, feature availability, core allowances, and boolean `billing_enabled`. The availability flag supports a truthful disabled Checkout state without exposing or probing configuration. Price IDs, configured environment-variable names, Customers, secrets, and provider objects are omitted.

`GET /v1/billing/subscription` requires signed-session or Cognito identity and database mode. It returns the effective plan code/status, provider label, current period, cancellation state, and `can_manage` without exposing a provider subscription ID. Its effective state comes from `subscription_service.py`, so disabled billing and unsafe lifecycle states still resolve conservatively.

`POST /v1/billing/estimate-generation` requires signed-session or Cognito identity and database mode. It accepts the prompt, one to three explicit targets, a shared optional `generation` value, optional target-level overrides, and `research_enabled`. It uses the same generation resolver and model-credit arithmetic as Ask/Compare but creates no reservation. The response includes each target's effective ceiling, maximum temporary AI-credit hold, current remaining credits, and `can_authorize`.

`POST /v1/billing/checkout-session` accepts the strict body `{"plan_code":"plus","billing_period":"monthly"}`. The server validates that the plan exists and is paid, resolves its Price ID from `config/subscription_plans.yaml` plus environment, creates/reuses the account Customer, and returns:

```json
{"checkout_url":"https://checkout.stripe.com/...","destination":"checkout"}
```

An existing provider-live subscription is not duplicated. The same endpoint creates a Portal session and returns its hosted URL with `destination: "portal"`. The browser still follows only the returned short-lived URL and never selects the Customer or redirect target.

`POST /v1/billing/portal-session` accepts no body or `{}` and returns `{"portal_url":"https://billing.stripe.com/..."}` for the persisted Customer. Missing Customer state returns `409 stripe_customer_required`; normalized Stripe errors return `502 billing_provider_unavailable`. Hosted URLs are never persisted, and neither hosted-session endpoint grants paid access.

`POST /v1/billing/webhook` has no user-auth dependency. It reads the raw request bytes, verifies `Stripe-Signature` with `STRIPE_WEBHOOK_SECRET`, rejects invalid signatures with structured `400`, and caps the body at 1 MiB. Valid events are hashed and persisted by Stripe event ID before processing. Unknown valid event types are marked ignored; processed/ignored duplicates return `200`; failed events retain a safe failure code and are retried under a row lock. Required handlers cover `checkout.session.completed`, `customer.subscription.created|updated|deleted`, `invoice.paid`, and `invoice.payment_failed`. The response is always `{"received":true}` after successful or duplicate handling and never exposes Stripe objects or identifiers.

Checkout and invoice handlers retrieve the current Subscription; subscription handlers consume the signed snapshot and compare its event creation time with `last_provider_event_at`. Price IDs are reverse-mapped only through server configuration. Paid periods use the existing `(billing_account_id, starts_at)` identity, so duplicate renewal events cannot reset counters; same-period plan/end changes preserve the row and counters. Payment failure sets grace only when the retrieved Subscription remains `past_due`, and deletion resolves through the existing lifecycle policy to Free without deleting history. The internal `reconcile_billing_account()` helper refreshes an account from Stripe, but no HTTP reconciliation endpoint is exposed because the repository does not yet provide administrator authorization.

Subscription environment controls:

```ini
BILLING_ENABLED=false
SUBSCRIPTION_PAYMENT_GRACE_DAYS=3
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PLUS_MONTHLY_PRICE_ID=
STRIPE_PRO_MONTHLY_PRICE_ID=
STRIPE_CHECKOUT_SUCCESS_URL=https://app.example.com/account/billing?checkout=success
STRIPE_CHECKOUT_CANCEL_URL=https://app.example.com/pricing?checkout=cancelled
STRIPE_PORTAL_RETURN_URL=https://app.example.com/account/billing
# STRIPE_API_VERSION=  # optional
# DEV_SUBSCRIPTION_PLAN=pro  # local/dev only; guarded unrestricted is runner-managed
```

See `docs/runbooks/stripe-billing.md` for the enablement checklist. Do not place Stripe secrets in frontend runtime configuration or commit them.

React consumes these contracts through `frontend-react/src/api/billing.ts`, `frontend-react/src/api/entitlements.ts`, and the auth-aware `useSubscription` hook. Signed-out hooks call only the public plans endpoint. Checkout-success polling remains bounded and considers payment confirmed only after `/v1/entitlements` reports a paid effective plan with `source=stripe` (Cortex grants do not confirm payment); browser storage and the return query string are never plan authority.

React exposes `/pricing` for the public Free/Plus/Pro catalogue and `/account/billing` for authenticated plan status, lifecycle notices, allowance progress, and Portal management. Billing-disabled, Free, active paid, past-due, cancel-at-period-end, fully cancelled, and delayed Checkout-confirmation states render from these server contracts. Account-menu plan context stays summary-only.

The API, database, ledger, reservations, and React state retain raw integer AI-credit units. Customer-facing React surfaces use `frontend-react/src/utils/aiCredits.ts` to display one AI credit per 1,000 raw units: Free/Plus/Pro therefore render 100/1,000/3,000 AI credits while the API contracts remain 100,000/1,000,000/3,000,000. Balances, allowance meters, response/Compare usage, itemized activity, structured insufficient-credit messages, and Work budgets use the same formatter. React still sends raw Work budgets and other credit-bearing request values back to the API.

React model/composer/file locks are user-experience controls only. They consume live `/v1/models` catalogue and billing metadata, show the Pro-only third Compare target, display Web/Improve/file allowances, and open contextual dialogs for structured backend denials. Unknown model billing metadata is unavailable rather than optimistically allowed. Prompt text and attachments are cleared only after a stream is accepted, so a preflight denial remains editable; restored historical responses are not filtered after downgrade. The AI credits route shows the unified allowance separately from the provider token/cost analytics on Usage & insights. Its credit history groups itemized API rows by `activity_id` into one card with the original pre-optimization question and total first; Prompt Optimizer and the following Ask/Compare share that display key. When both exist, the native expandable breakdown presents their combined credits as one `Final optimized ... answer` line and explicitly identifies the included optimizer attempts and final answer generation. Optimizer-only activity remains identifiable, Compare/Cortex Analysis/Web Search charges remain understandable, optimizer retries are aggregated, zero-credit adjustments stay out of the visible breakdown, and an explicit fallback covers legacy or privacy-policy-limited activity with no query.

Session-scoped endpoints are session-scoped:
- `/v1/chat*`
- `/v1/compare*`
- `/v1/files/*`
- `/v1/providers`
- `/v1/models`
- `/v1/optimize`
- `/v1/history*`
- `/v1/billing/*`
- accepted auth: `cortex_session` cookie or `Authorization: Bearer <gateway-bearer-token>`
- API-key-only auth is rejected with `403` (`session_auth_required`)

Local-only session bootstrap helper:
- `POST /v1/auth/dev-login`
- disabled by default (`ENABLE_DEV_SESSION_LOGIN=false`)
- blocked when `APP_ENV`, `ENVIRONMENT`, or `ENV` is `prod`/`production`
- optional shared secret: `DEV_SESSION_LOGIN_TOKEN` via header `X-Dev-Login-Token`

### Reverse proxy, session cookie, and Cognito TLS settings

- `ENABLE_PROXY_HEADERS=true` is the default. It lets requests behind CloudFront or an ALB use forwarded connection metadata so HTTPS-dependent callback URLs, runtime config, and session cookies see the public request scheme. Set it to `false` when the service is directly internet-facing without a trusted upstream proxy.
- `TRUSTED_PROXY_IPS=*` is the default trusted-host list for the application proxy-header middleware. Replace `*` with a comma-separated set of trusted proxy IPs or CIDRs when the API can also be reached directly.
- Set `COGNITO_REDIRECT_URI` explicitly in production to the exact HTTPS callback registered in the Cognito app client. Automatic derivation uses the incoming request, so it depends on correct proxy-header forwarding.
- `SESSION_COOKIE_SECURE` overrides the session cookie's `Secure` attribute when set. When it is unset, the API enables `Secure` when the effective request scheme is HTTPS; leave the example line commented to use auto-detection.
- `SESSION_MAX_AGE_SECONDS=604800` keeps the signed session cookie for seven days by default. Set it to `0` to create a browser-session cookie instead.
- `COGNITO_SSL_VERIFY=true` verifies certificates for Cognito JWKS and token requests. Setting it to `false` disables TLS verification and should be limited to controlled local troubleshooting or a trusted TLS-inspection environment.

## Frontend Runtime Config

When `SERVE_FRONTEND=true`, backend serves `GET /runtime-config.js` dynamically:
- `apiBase` defaults to current request origin.
- Override API base with `FRONTEND_RUNTIME_API_BASE` (useful for split-origin deployments).
- `enableDevSessionLogin` defaults from `ENABLE_DEV_SESSION_LOGIN`.
- Optional override for browser config: `FRONTEND_RUNTIME_ENABLE_DEV_SESSION_LOGIN`.
- In production-like runtimes (`APP_ENV/ENVIRONMENT/ENV=prod|production`), dev-login bootstrap is forced off.
- Frontend startup completes Cognito/local dev-session bootstrap before calling session-scoped startup endpoints (`/v1/providers`, `/v1/models`, `/v1/history`). If Cognito is enabled and no user session is present, React shows a sign-in gate instead of calling those endpoints; authenticated and local dev-session users still hydrate Ask/Compare selectors and the history sidebar immediately.
- The React workspace shows a `Sign in to use CortexAI` gate for signed-out Cognito users, and the top-right account icon remains a secondary menu with Cognito `Sign in` when available, a persisted light/dark theme switch, and `Log off` as a session-clear fallback, including when the icon is still labelled `Guest account`. Log off clears the local session cookie through `/v1/auth/logout`, resets the active React chat/history state, then follows the Cognito Hosted UI `logoutUrl` when available.
- Response is sent with no-cache headers so config changes apply immediately.

React/Vite frontend notes:
- Build output lives in `frontend-react/dist` after `npm run --prefix frontend-react build`.
- `frontend-react/runtime-config.example.js` is the static-hosting template for deployments where FastAPI or a reverse proxy does not provide `/runtime-config.js`.
- Local hot-reload development can use `python run_app.py` for the full app, or `npm run --prefix frontend-react dev` plus a separate API process. Vite proxies `/v1`, `/auth`, and `/runtime-config.js` to `http://localhost:8000` by default.
- The React router exposes `/usage` for Usage & insights, `/credits` for unified AI-credit balance/activity, and `/models` for the task-first model guide. Desktop reaches all three from the sidebar; mobile reaches them from the account menu, while the bottom navigation remains Ask/Compare/History only.
- `run_app.py` sets `CORTEX_API_PROXY_TARGET` for Vite and `FRONTEND_RUNTIME_API_BASE` for runtime config so custom API host/port flags stay aligned with the frontend proxy.
- `run_app.py` checks both requested ports before starting either child process. On Windows it terminates each full child process tree, preventing npm/Vite descendants from remaining bound after partial startup failure or `Ctrl+C`.
- Standalone production hosting must provide `/runtime-config.js` at the React origin and route `/v1/*` plus `/auth` to the FastAPI service. The current React client uses same-origin relative API paths, so split-origin deployments need a reverse proxy/CDN/nginx rule for those paths.
- `Dockerfile.frontend` builds the React app and serves static assets with nginx. `Dockerfile.api` is API-only; it does not include `frontend-react/dist` unless the deployment image is extended to copy those files.

## API Key Persistence Policy

In required DB runtime mode, API-key flows can resolve key ownership before model invocation.
Session-scoped chat/compare/files flows resolve persisted user identity from session/bearer auth.

Env flags:
- `AUTO_REGISTER_UNMAPPED_API_KEYS=false` (safe default)
- `ALLOW_UNMAPPED_API_KEY_PERSIST=false` (safe default)
- `API_KEY_FALLBACK_USER_EMAIL=api@cortexai.local`
- `API_KEY_FALLBACK_USER_NAME=API Service User`

Behavior for key present in `API_KEYS` but unmapped in `public.api_keys`:
1. If `AUTO_REGISTER_UNMAPPED_API_KEYS=true`: creates DB mapping under service user.
2. Else if `ALLOW_UNMAPPED_API_KEY_PERSIST=true`: persists with service user and `api_key_id=NULL`.
3. Else: rejects with `403`.

Guardrail:
- If `llm_requests.api_key_id` is set, `llm_requests.user_id` must match `api_keys.user_id`.
- Enforced in app logic and DB trigger migration.

## Register Dev/Test Key

Preferred zero-arg helper (IDE-friendly):
```bash
python tools/register_dev_key.py
```

Param-based helper:
```bash
python tools/create_api_key.py --email api@cortexai.local --name "API Service User" --key "dev-key-1" --label "postman-dev"
```

## Shared Request Concepts

Common request fields used by Ask and Compare:

```json
{
  "context": {
    "session_id": "string (optional)",
    "conversation_history": [
      {"role": "user|assistant|system", "content": "string"}
    ],
    "new_session": false
  },
  "routing": {
    "smart_mode": true,
    "research_mode": false
  },
  "generation": {
    "profile": "auto",
    "reasoning": {"mode": "auto", "effort": "auto"}
  },
  "temperature": 0.7
}
```

Notes:
- `routing.smart_mode` defaults to `true`.
- `routing.research_mode` is a boolean in the current API contract, not `"off|auto|on"`.
- Ask and Compare can reuse the same `session_id`; session continuity is shared across both modes.
- `generation.profile` is `auto|quick|balanced|deep|extended`. Auto selects 4K for normal economical/standard calls, 8K for advanced reasoning models, and 12K for premium or deterministically complex/detailed tasks. Explicit Quick/Balanced/Deep/Extended use 1K/4K/12K/32K before model/context/affordability limits. `generation.max_output_tokens` is the mutually exclusive custom alternative. `generation` cannot be combined with legacy `max_tokens`.
- Omitted API requests use Quick/1K. React sends Auto explicitly and exposes no Answer depth selector or live hold estimate; requested answer detail belongs in the prompt. Unsafe explicit ceilings and unsupported reasoning combinations return `422 invalid_generation_budget`.
- Claude reasoning controls follow the selected model's registry declaration. Claude 4.6 and supported Claude 5 models use adaptive thinking; the manual-budget-only Claude 4.5 family defaults to normal generation and returns `422 invalid_generation_budget` for explicit reasoning-on. Claude custom temperature is omitted when Anthropic requires default sampling, including adaptive-thinking and Claude 5 requests.
- If `session_id` is omitted in DB mode, the backend may resolve the user's most recent active session.
- The browser UI avoids that fallback on explicit fresh login by marking `cortex_fresh_login_pending`, consuming the backend `fresh_login=1` callback marker, clearing the stored active thread id, and sending `new_session=true` for the first turn after sign-in.

Prompt optimization:
- `POST /v1/optimize` is gated by `ENABLE_PROMPT_OPTIMIZATION=true`.
- `/v1/optimize` is the UI optimization path; chat/compare do not auto-optimize by default.
- In DB mode, the endpoint resolves the server-owned plan, checks prompt-improvement access, and reserves every configured GPT-4.1 mini optimizer attempt before setup. Setup failures release the reservation; each provider attempt with billable usage settles actual input/output credits, and unused attempt capacity is released.
- Improve Prompt counts as one submitted action against the effective plan's requests-per-minute limit.
- Sending the returned prompt through Ask or Compare is a separate model call charged through the normal Ask/Compare credit calculation; it does not duplicate the optimizer charge. React shares a display-only `credit_activity_id` and the original `initial_query` across both requests so the Credits screen can show one question-level total.
- Set `ENABLE_ORCHESTRATOR_PROMPT_OPTIMIZATION=true` only when chat/compare should automatically rewrite prompts without the explicit optimize endpoint.
- `PROMPT_OPTIMIZER_MODEL` must match the configured `PROMPT_OPTIMIZER_PROVIDER`.
- `/v1/optimize` uses `PROMPT_OPTIMIZER_TIMEOUT_MS` (default `5000`) as its hard deadline and `PROMPT_OPTIMIZER_ROUTE_MAX_RETRIES` (default `2`) for explicit-route attempts.
- Weak or vague prompts are classified locally and get one extra retry if the optimizer returns the original prompt unchanged; strong prompts can keep the original without retry.
- Optimizer calls use compact generation defaults from `PROMPT_OPTIMIZER_MAX_OUTPUT_TOKENS` (default `450`) and `PROMPT_OPTIMIZER_TEMPERATURE` (default `0.2`), with JSON-object mode for OpenAI chat models.
- Optimizer route logs include status, fallback reason, prompt-quality class, attempt count, and retry reasons without logging raw prompt text.
- Optimize request payloads may include optional display-only `credit_activity_id`, `context_hint`, and compact `context`; Chat/Compare may carry the same activity ID plus the original `initial_query`. These display fields never affect billing authority or arithmetic. The frontend sends recent mixed user/assistant context whenever a thread has prior messages. Attachment file contents are not copied into optimize requests. React caps optimize context to ten compact messages and a 4,000-character `context_hint` so ordinal, pronoun, and formatting references like "the second one", "their cadres", or "write it as a table" can be resolved in longer chats.
- Optimizer output is parsed as schema-constrained JSON and rejected when it appears to answer the prompt, or when it introduces unresolved placeholders such as `[specific topic]`, instead of rewriting it.
- Responses include `optimization_status` (`optimized`, `kept_original`, `disabled`, `timeout`, `failed`, `rejected`), `fallback_reason`, and `optimization_reused`. A reused result performs no provider call and consumes no new optimizer credits.
- Rejected, timed out, failed, kept-original, or disabled optimization returns the original prompt with `was_optimized=false`.
- With the frontend Improve toggle enabled, the user bubble always shows the prompt being sent in normal case. Optimization state renders as a right-aligned pill below the bubble: pending shows `Improving your prompt`, optimized shows `Prompt optimized` with `View original`, and kept-original shows `Already clear — sent as-is`. Ask response cards and Compare response tabs, cards, and summary remain hidden while optimization is pending, then appear when optimization resolves and model generation begins; cancelling during optimization leaves the placeholder response UI hidden.

## Chat API

### Request shape

```json
{
  "prompt": "string (required)",
  "provider": "openai|gemini|deepseek|grok|claude (optional in smart mode)",
  "model": "string (optional when provider is set)",
  "routing": {
    "smart_mode": true,
    "research_mode": false
  },
  "context": {
    "session_id": "string (optional)",
    "conversation_history": [
      {"role": "user|assistant|system", "content": "string"}
    ],
    "new_session": false
  },
  "generation": {
    "profile": "auto",
    "reasoning": {"mode": "auto", "effort": "auto"}
  },
  "temperature": 0.7
}
```

Rules:
- If `model` is provided, `provider` is required.
- In manual Ask mode, `provider` + `model` gives deterministic targeting.
- With `routing.smart_mode=true`, Ask uses the smart orchestration path.
- With `routing.research_mode=true`, Ask uses orchestrator-managed web research with fresh sources for the current turn.
- The resolved effective generation ceiling is used unchanged for provider execution and credit authorization.
- Provider-neutral `temperature` remains optional. For Claude, the adapter forwards it only to Claude 4.5/4.6 requests with thinking off; incompatible values are omitted so Anthropic uses its required default sampling.

### Response shape

```json
{
  "request_id": "string",
  "session_id": "string (optional)",
  "text": "string",
  "provider": "string",
  "model": "string",
  "latency_ms": 1234,
  "token_usage": {
    "prompt_tokens": 100,
    "completion_tokens": 250,
    "total_tokens": 350
  },
  "estimated_cost": 0.00123,
  "cost_currency": "USD",
  "finish_reason": "stop|length|tool|content_filter|error|null",
  "completion_status": "complete|incomplete|failed",
  "stop_cause": "natural|token_limit|context_limit|content_filter|error|unknown",
  "generation_budget": {
    "profile": "auto",
    "requested_max_output_tokens": 8192,
    "effective_max_output_tokens": 8192,
    "requested_reasoning_mode": "auto",
    "effective_reasoning_mode": "standard",
    "requested_reasoning_effort": "auto",
    "effective_reasoning_effort": "medium",
    "reasoning_disable_supported": true,
    "reasoning_counts_against_output": true,
    "policy_version": "generation-budget-v3"
  },
  "retry_with_more_room": {"available": false, "recommended_profile": null},
  "error": null,
  "web_source_items": [
    {"title": "Source title", "url": "https://example.com"}
  ],
  "timestamp": "2026-03-08T00:00:00Z"
}
```

### Streaming contract

`POST /v1/chat/stream` returns NDJSON events:
- `start`
- `heartbeat`
- `line`
- `response_done`
- `done`
- `error`

Notes:
- `start` includes the routed preview `provider` and `model`, plus `session_id`, `research_mode`, and an initial `web_source_items` array. Smart-mode clients should not present the preview as the final selected model.
- `response_done` includes the full `ChatResponseDTO`, including `session_id` and `web_source_items`.
- `done` includes the resolved `session_id`.
- `heartbeat` carries elapsed timing while provider work is pending. It keeps
  the HTTP response body active, is ignored by the React UI, and does not call
  provider APIs or consume model tokens.
- Server logs emit `chat.stream.*` lifecycle events from inside the response body generator, including provider-call start/completion and terminal stream reason.

## Compare API

### Request shape

```json
{
  "prompt": "string (required)",
  "targets": [
    {"provider": "openai", "model": "gpt-5.6-luna"},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"}
  ],
  "routing": {
    "smart_mode": true,
    "research_mode": false
  },
  "context": {
    "session_id": "string (optional)",
    "conversation_history": [
      {"role": "user|assistant|system", "content": "string"}
    ],
    "new_session": false
  },
  "generation": {
    "profile": "auto",
    "reasoning": {"mode": "auto", "effort": "auto"}
  },
  "timeout_s": 30,
  "temperature": 0.7
}
```

Rules:
- 2 to 3 targets at the API boundary. Free and Plus allow 2; Pro allows 3.
- Compare always uses explicit targets.
- `routing.smart_mode` is ignored in compare mode by design.
- With `routing.research_mode=true`, research runs once per compare turn and is shared across all selected targets for fairness.
- Browser Ask sends `routing.research_mode=true` by default because the `Web` toggle starts on, and Browser Compare does the same because `With sources` starts on; users can turn either off for the current page session.
- A target may provide its own `generation` object; it overrides the shared Compare generation value for that target only.
- Each Claude target independently receives only the thinking, effort, and temperature fields supported by that model generation.

Subscription enforcement:
- The effective plan, model billing classes, feature access, and meter quantities are resolved server-side; client-supplied billing identifiers are ignored.
- Entitlement/model denials return structured `403` responses before provider execution. Insufficient monthly AI credits return `402 insufficient_credits`; unsafe billing configuration returns a provider-safe `500`.
- Smart Ask resolves and ranks only enabled models allowed by the effective plan, estimates every appropriate candidate from materialized input, bounded research context, model-specific multipliers, the exact clamped output ceiling, and any fixed retrieval charge, then removes unaffordable candidates. It reserves the first appropriate affordable candidate without an arbitrary percentage. A more expensive fallback must atomically supplement the same reservation before invocation; if that fails, the router skips it and continues with an affordable candidate.
- Streaming reservations are created before the `StreamingResponse` is returned. Ask settles a successful model after its first meaningful output is emitted, releases model units on a pre-output disconnect, and still settles performed research once. Compare finalizes aggregate successful targets before `done`, or settles only partial targets whose output started on disconnect/error.
- `POST /v1/billing/estimate-generation` runs the same per-target resolver and credit estimate without reserving credits. It returns the maximum temporary hold, remaining credits, and `can_authorize`; actual settlement releases unused held credits.

Persistence:
- One `llm_requests` + `llm_responses` row per compare target response.
- Shared `llm_requests.request_group_id` per compare run.
- API response `request_group_id` is canonical and matches orchestrator/log/persistence group ID.
- A regenerated Compare response appends a versioned `llm_requests` row tied to its original response root. History returns only the latest revision for that logical response while retaining every row for audit and Cortex staleness checks.

### Response shape

```json
{
  "request_group_id": "string",
  "session_id": "string (optional)",
  "responses": [
    {
      "request_id": "string",
      "session_id": "string (optional)",
      "text": "string",
      "provider": "string",
      "model": "string",
      "latency_ms": 1234,
      "token_usage": {
        "prompt_tokens": 100,
        "completion_tokens": 250,
        "total_tokens": 350
      },
      "estimated_cost": 0.00123,
      "cost_currency": "USD",
      "finish_reason": "stop|length|tool|content_filter|error|null",
      "error": null,
      "web_source_items": [
        {"title": "Source title", "url": "https://example.com"}
      ],
      "timestamp": "2026-03-08T00:00:00Z"
    }
  ],
  "success_count": 2,
  "error_count": 0,
  "total_tokens": 700,
  "total_cost": 0.00246,
  "timestamp": "2026-03-08T00:00:00Z"
}
```

### Streaming contract

`POST /v1/compare/stream` returns NDJSON events:
- `start`
- `heartbeat`
- `response_start`
- `line`
- `response_done`
- `done`
- `error`

Notes:
- `start` includes `session_id`, `research_mode`, and target count.
- Each `response_done` includes one full `ChatResponseDTO`.
- Final `done` includes the aggregate compare payload with both `request_group_id` and `session_id`.
- `heartbeat` may appear while Compare targets are pending and has the same
  keep-alive semantics as chat streaming.
- Server logs emit `compare.stream.*` lifecycle events from inside the response body generator, including per-target provider-call progress and terminal stream reason.

### Cortex Analysis API

`POST /v1/compare/{request_group_id}/analysis`:

- Requires session-scoped auth, database mode, an owned Compare group, and two or three latest successful response revisions.
- Calls `CORTEX_ANALYSIS_MODEL` (default `gpt-5.4-mini`) only after the user requests analysis.
- Sends shuffled `Response A/B/C` content without provider/model metadata and
  requests strict JSON-schema Structured Outputs. Before persistence, anonymous
  response references in every user-visible field are restored to their real
  provider-and-model display names, such as `Claude (Sonnet 4.6)`, so multiple
  models from the same provider remain distinguishable.
- Persists a run only after the provider result passes local schema validation.
- Returns attributed disagreement positions as `disagreements: [{"who": "ChatGPT (…)", "text": "…"}]` plus nullable `disagreementNote`. The analysis model supplies only anonymous `Response A/B/C` labels; the server resolves `who` before persistence and response serialization.
- Returns the saved run with `201`; provider/validation failures return `502` and do not add history.
- Verifies the Cortex persistence schema before provider work. Missing or
  incomplete migration state returns
  `503 cortex_analysis_schema_unavailable` without calling the model.

`GET /v1/compare/analysis-runs?session_id=<uuid>` or `?request_group_id=<uuid>`:

- Requires one of the two filters and returns every owned run newest-first.
- Each run includes `analysisId`, `requestGroupId`, `sessionId`, `model`, the structured result sections, `sourceResponses`, `createdAt`, and `isStale`.
- The structured result includes attributed `disagreements`, nullable `disagreementNote`, attributed `uniqueInsights`, qualitative confidence, and verification items. Legacy flat-string disagreement rows are restored as readable `One response` entries.
- `sourceResponses` records the exact `requestId` and `responseVersion` inputs. A later Compare response regeneration changes the current source fingerprint, so earlier runs remain available with `isStale=true`.

The browser hydrates these runs with session history after reload or History
reopening. It shows the newest analysis by default and exposes all prior runs
through Analysis history. Creation and regeneration are new synthesized model
calls charged against the unified AI-credit wallet; there is no separate Cortex
quota. The reservation includes the Compare question and successful source
responses with a 1,800-token output ceiling. Existing Compare research is reused
without another Tavily charge. Historical runs remain readable after downgrade.

## Schema Migrations

Apply these when enabling updated persistence flows:

```bash
psql "$DATABASE_URL" -f db/migrations/20260218_add_request_group_id_to_llm_requests.sql
psql "$DATABASE_URL" -f db/migrations/20260218_llm_requests_api_key_owner_guard.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260718_add_b2c_billing_foundation.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260727_add_cortex_analysis_runs.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260729_add_unified_ai_credits.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260730_add_usage_reservation_activity.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260731_add_model_pricing_audit.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260802_add_cortex_analysis_attribution.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260804_add_generation_budget_audit.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260807_add_cache_aware_credit_accounting.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260811_add_direct_s3_attachment_upload.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260820_add_cortex_work_mode.sql
psql "$MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260829_add_work_web_output_and_model_identity.sql
```

The first Cortex Analysis migration adds Compare response revision metadata and
the append-only `cortex_analysis_runs` table; the `20260802` attribution
migration adds nullable `disagreement_note` for the attributed result contract. Run them with a role that owns
`llm_requests`; the normal application role may have read/write access without
permission to alter that table. Restart the API after applying the migration so
SQLAlchemy reflects the new columns. Shared Ask/Compare session continuity
itself remains implemented in persistence/session resolution logic.

The billing foundation, Cortex revision table, unified-credit ledger,
reservation-activity migration, model-pricing audit migration, Cortex
attribution migration, generation-budget audit migration, cache-aware accounting migration, and direct-S3 attachment lifecycle migration are all required. They are additive and
idempotent under the repository migration convention. PostgreSQL startup checks
the required tables and columns and fails before serving provider routes when a
migration is missing. See `docs/runbooks/db-migrations.md` for verification and
rollback guidance.

The cache-aware migration also creates `cache_reuse_events`. Ask/Compare
research, prompt optimization, and Cortex Analysis each record one idempotent
reuse decision per request; `/v1/usage/summary` derives the three period-scoped
reuse rates from those audit rows.

## OpenAI Compatibility Note

GPT-5.6 and Codex-family models use the Responses API with `max_output_tokens` and resolved reasoning effort. Compatible Chat Completions models retain the adaptive `max_tokens` to `max_completion_tokens` retry when the provider rejects the legacy parameter.

## Research Behavior

- Web research is orchestrator-managed.
- When `routing.research_mode=true`, Ask performs a fresh research pass for the current turn.
- When `routing.research_mode=true`, Compare performs one shared research pass for the compare turn.
- Injected sources are primary evidence for current/source-dependent facts; models may still use non-conflicting baseline knowledge for background context.
- Successful provider answers are not scanned or replaced using phrase, number, date, or citation heuristics. When research is disabled, non-empty answers are returned as generated by the selected models.
- Response payloads expose normalized source metadata through `web_source_items`.
- Query sanitization anchors underspecified follow-up searches to the previous user topic when the current prompt omits that topic.
- Tavily search calls use a deterministic local resolver with fixed retrieval params: `max_results=5`, `search_depth=advanced`, `chunks_per_source=1..3` (default `3`), `include_raw_content=false`, `include_answer=false`, and `auto_parameters=false`.
- With `TAVILY_ENHANCED_SEARCH_ENABLED=true`, the resolver may add Tavily `topic` for `finance`/`news`, a bounded `time_range`, country targeting only when no topic is sent, and curated finance domain allowlists for Canada, US, US SEC filings, and UK economic queries.
- The resolver does not rewrite queries. Prompt optimization and query sanitization remain separate layers before the Tavily client receives its query string.
- Set `TAVILY_ENHANCED_SEARCH_ENABLED=false` to disable topic/time/country/domain enrichment while keeping the fixed retrieval params.

## Guardrails

Applied in `server/utils.py`:
- Conversation history trimmed to last 10 messages.
- Oversized conversation-history payloads are soft-trimmed server-side instead of rejected; the newest context is retained first and older or oversized message content is trimmed before provider calls.
- Ask/Compare generation profiles are resolved centrally against model, context, and operational limits. Explicit unsafe ceilings return `422`; omitted legacy calls retain Quick/2K.
- Empty length-limited responses remain successful-but-incomplete billable work, even when reasoning consumed the allowance before visible output. Unexplained empty successes still normalize to provider errors.
- Provider-native availability failures are sanitized before DTO/stream output. Upstream 503/high-demand/overloaded errors are tagged as `error.details.kind="transient_capacity"` and rendered as `This model is temporarily busy. Try again shortly or switch to another model.` instead of raw provider JSON.
- Smart Ask keeps the existing automatic fallback loop for retryable provider failures. Manual Ask and Compare keep the user-selected model targets and return safe per-model errors when those explicit targets are unavailable.
- CortexAI does not attempt to determine whether a non-empty successful LLM answer is fabricated. Web research provides optional grounding; provider-native safety/filter outcomes remain authoritative.

Security/logging:
- `X-API-Key` and `Authorization` headers are redacted in auth logs.
- Middleware sets/returns `X-Request-ID` and emits request lifecycle events (`http.request.start|complete|exception`) for correlation.
- The browser frontend sends `X-Request-ID` on Ask/Compare/Optimize calls and logs stream read failures to the developer console with request id, server request id, status, elapsed time, classified kind, and received event count.
- Browser lifecycle diagnostics are accepted by unauthenticated `POST /v1/client-diagnostics` and logged as `frontend.diagnostic` with sanitized metadata only; prompts, responses, attachment contents, and auth secrets are not sent.
- Structured persistence logs include `request_id`/`request_group_id`, resolved `user_id`, `api_key_id`, decision path, and status.
- Research logs include `research.*` events with hashed prompt/query fields (raw Tavily query text is not logged).
- Tavily search-option resolver logs include category, topic/time/country decisions, domain-rule metadata, returned source-content lengths, and API credits used.
- Tavily emits `research.network.diagnostics` entries (DNS + TCP reachability to Tavily host) plus normalized failure `error_kind` values for EC2 network troubleshooting.
- Attachment pipeline logs include `upload.*` + `storage.*` events for legacy uploads, presign success/failure, metadata write, HEAD verification failures, sync/deferred ingestion, deletion, and rollback/error paths. Presigned policies, signatures, tokens, returned form fields, and file bytes are never logged.
- Upload route adds `upload.route.*` events with edge/proxy request context (`X-Amz-Cf-Id`, `X-Forwarded-*`, content-length vs payload-size checks) to help isolate CloudFront/WAF/origin issues.
- Auth failures log `auth.failed` with method/path and auth-header presence flags (while redacting sensitive values).
- Circuit-breaker telemetry includes `circuit.failure.recorded`, `circuit.transition.open`, `circuit.open.blocked`, and `circuit.transition.closed`.
- File upload/status APIs sanitize client-facing `error_message` values to avoid leaking bucket names, object keys, or storage internals.
- Frontend attachment upload failures are sanitized before rendering (network/size/type/timeout/generic) so raw backend/storage error text is not shown to end users; raw errors remain available in browser console logs for debugging.
- Frontend model response errors are also sanitized during live stream finalization and history hydration, so older raw provider error payloads are not replayed in response cards. Transient model-capacity failures render with the amber `.model-soft-error` treatment.
- Logging destinations are configurable for EC2/containers via `LOG_DESTINATION=file|stdout|both`; see `docs/LOGGING.md`.

## Testing

Run FastAPI contract tests:
```bash
pytest tests/test_fastapi_contract_and_guardrails.py -v
```

Run React frontend build validation:
```bash
npm ci --prefix frontend-react
npm run --prefix frontend-react build
```

Run persistence guardrail tests:
```bash
pytest tests/test_api_persistence_guardrails.py -v
```

Run mocked Stripe billing tests (no Stripe network calls):
```bash
pytest tests/test_stripe_billing.py tests/test_billing_repository.py -q
```

Run compare orchestrator tests:
```bash
pytest tests/test_multi_compare_mode.py -v
```

Run React responsive browser tests without a backend or database:
```bash
npm run --prefix e2e test:mobile
npm run --prefix e2e test:desktop-ipad
```
The suites start isolated Vite servers and mock frontend API contracts. Mobile tests cover phone navigation, composer clearance and growth, attachments, Compare model controls, history restoration, and stacked response cards. Desktop/iPad tests cover the desktop shell, iPad breakpoint behavior, model selection, and independent Compare response scrolling.

---

Last updated: 2026-07-29
