# Compare Mode Usage Guide

## Overview

Compare Mode allows you to send every query to multiple LLM providers simultaneously and compare their responses side-by-side. This is useful for:

- Evaluating response quality across different models
- Finding the fastest model for your use case
- Comparing costs between providers
- Testing prompt effectiveness across models

This guide covers both:
- API compare mode (`POST /v1/compare`, `POST /v1/compare/stream`)
- CLI compare mode (`COMPARE_MODE=true`)
- Browser Compare mode, where provider-native search is automatic and no Web control is shown. Empty initial model slots are filled only after effective entitlements load and only from models allowed by the current plan; higher-plan models remain visible for discovery.
- Browser Compare reasoning starts at Low and enables only levels supported by every selected target. Auto lets each model use its own managed default; unsupported levels remain visible but disabled, and changing a target recomputes the intersection.
- Browser response headers show model names, exact model IDs, response-start duration, and AI-credit usage. Response and Cortex Analysis credit amounts use a neutral credit icon without currency symbols. Live and restored responses omit decorative category badges and fastest-response labels and highlighting on desktop, tablet, and phone layouts.
- Browser Analyze, with automatically authorized synthesis of two or three completed model responses.

## Cortex Analysis

Cortex Analysis brings the selected model answers together in **Analyze**, the
browser display label for the existing internal `compare` mode. The browser
shows a combined AI-credit estimate before Send and submits
`automatic_analysis: true`. Send authorizes the selected model answers plus one
initial synthesis once at least two answers succeed. Existing API clients keep
their response-only behavior when this additive field is omitted or false.

- `POST /v1/billing/estimate-generation` accepts `include_analysis: true`, `include_native_search: true` and
  bounded `context`; `estimated_max_ai_credits` includes the synthesis and
  `analysis_ai_credits` reports its component. This is an estimate of the
  temporary hold, not a guaranteed charge. Estimation creates no reservation.
- Combined execution checks schema, provider/pricing readiness, plan access and
  reserves both stages before provider work. Each stage uses the canonical
  wallet and settles actual usage. Invalid structured output with reported
  provider usage is still settled; provider failure without usage releases its
  hold. Fewer than two successes releases the synthesis hold and retains the
  initial authorization for an individual failed-response retry.
- A server-owned producer completes an authorized stream after navigation.
  `POST /v1/compare/{request_group_id}/cancel` explicitly stops an active stream
  owned by the signed-in user through a shared cancellation flag. Normal view changes do
  not invoke cancellation. The queue worker resumes only persisted, authorized
  intents; it never creates an intent from a history read. Interrupted provider
  calls with unknown outcomes become a failed, explicitly retryable analysis
  after 15 minutes, rather than being automatically purchased again.
- `POST /v1/compare/{request_group_id}/analysis` creates a cost-disclosed manual
  retry, update, or legacy analysis. Automatic-intent duplicates reuse the saved result; legacy manual
  reuse honors `CORTEX_ANALYSIS_REUSE_ENABLED`. Reuse keys include model/policy/context/revisions; `?regenerate=true` explicitly requests another paid
  run. A concurrent run returns `409 analysis_in_progress`. Successful results
  return `201`; invalid/failed synthesis returns `502 cortex_analysis_failed`.
- `GET /v1/compare/{request_group_id}/analysis-state` returns read-only state
  (`idle|waiting|queued|processing|complete|failed`), `automatic`, `analysisId`,
  `errorCode`, `estimatedAiCredits` and nullable `progressStage`
  (`comparing|reading|synthesizing|validating`, only while processing). It never
  reserves credits or calls a model. Progress is persisted worker metadata;
  terminal/legacy states return null and polling can skip short stages.
- `GET /v1/compare/analysis-runs?request_group_id=<uuid>` (or `session_id`) lists
  owned runs newest-first, including after a plan downgrade. Legacy fields stay
  readable. V2 adds `structuredResult.schemaVersion=2`, `takeaway`, exact
  attributed `passages`, internally supported `points`, optional consequential
  `differences`, contextual `limitations`, and server-resolved `linkedSources`.
  `sourceResponses` includes immutable text/identity/revision snapshots;
  `contextSnapshot` records at most 10 recent user/assistant messages and 12,000
  characters. Context participates in reuse fingerprints. Existing privacy
  redaction applies. Metadata-only persistence cannot authorize automatic
  analysis because saved response text is unavailable.
- The configured `CORTEX_ANALYSIS_MODEL` remains `gpt-5.4-mini` by default.
  Synthesis makes one structured call with search off, shuffled anonymous labels
  and a bounded output ceiling. `CORTEX_ANALYSIS_MAX_OUTPUT_TOKENS` defaults to
  1800, configurable from 1800 through 4000; larger configured ceilings and the
  complete prompt/context/evidence payload are included in reservations.
- React has Cortex Analysis (Analysis on phones), Model responses and always-visible Sources tabs.
  Model bylines and quotation headers reuse proper provider icons. The
  contributor/status/credits strip above the tabs is omitted.
  A layers symbol identifies Cortex Analysis and the takeaway; sparkles identify
  the synthesis header. An optional answer headline and introduction lead into
  the lavender takeaway (`#f1eefc` light / `#2d2845` dark). Optional consequential
  differences have a title and **See both passages** disclosure, followed by
  the **In their words** grid and a Sources link with its real count.
  Quotes sit side by side where space permits and stack on narrow screens. Confidence badges,
  voting banners and forced uniqueness sections are absent. Sources separates
  attached website metadata from model passages, including **No linked sources
  supplied**. Quotes validate against the actual immutable inputs; generated
  URLs cannot become new sources. HTTP(S) links retain query parameters and all
  attributions; title/URL metadata does not establish claim support. Source cards
  show **Cited for** only when numbered citations or matching Markdown/reference
  links in that exact response revision identify a passage. Each model/revision
  uses its own original source ordering, including saved snapshots after a
  regeneration. Code and bibliography entries are excluded. **View the model’s
  answer** opens the corresponding revision and quoted context, with **Back to
  Sources**; absent old text is never replaced with a newer answer. Metadata-only
  sources say no specific cited passage was supplied. This uses existing text and
  metadata without another model/search call, schema migration or history rewrite.
- Analyze preserves the original Cortex fonts, colors, brand assets, provider
  icons, sidebar preference and shared composer. The existing shell switches
  to mobile navigation/history at 900px. Empty Analyze uses the original
  centered composer with `Hi, what would you like to analyze?`. The Models +
  analysis estimate footer is omitted; combined-estimate Send authorization
  remains active. Analyze-specific layouts use scoped theme tokens.
  Cortex Analysis is the default view when opening Analyze or returning from Ask. Model responses remains available with its original layout and selected model preserved.

  While model answers arrive, the Analysis tab shows the mockup's live ledger:
  provider logos, queued/search/tool/writing activity, completed or partial
  answers, and failed/stopped models. Read Model responses switches views without
  starting work; failed models have their existing cost-estimated retry callback.
  Automatic synthesis then shows the three connected workflow steps, with an
  active spinner and completed checks driven by `progressStage`. Quote validation
  updates the final step before saving. Reduced-motion settings stop animation.
  Previous results remain readable during updates and failures. Preparation
  stages can finish between polls; the UI never adds demo delays or fake progress.

  The original response layout retains equal-height
  side-by-side cards on desktop, three columns above 1180px for three models,
  two-column wrapping at 901–1180px and stacked tall cards at 761–900px. Card
  bodies scroll independently on desktop/tablet. At 760px and below, the original
  horizontal provider tabs switch one natural-height answer; sticky controls,
  keyboard navigation and composer clearance are preserved. This display is
  independent of the Cortex Analysis layout. Wide code remains contained.
- Structured v2 runs optionally include nullable `headline`, `introduction` and
  `differences[].title`. Existing saved runs remain readable without them; the
  browser does not fabricate a headline from a question or a quote. Policy
  `cortex-analysis-v2-editorial` generates the new presentation fields with the
  existing single call and token ceiling. New overview text passes the same
  known-source URL checks and anonymous-label restoration as the takeaway.
- Regenerating an answer creates a new revision and marks earlier synthesis
  stale. **Update analysis** and retry show their estimated cost and need no
  second confirmation. The previous analysis stays readable during an update
  and after failure. Evidence opens the earlier full response and offers a
  return to the latest version. Copy, code/table rendering, regeneration,
  incomplete/error controls and local revision-keyed ratings remain available.
  Tab/view changes and resizing create no paid work. Ratings are session-local,
  not server-persisted.

Apply `db/migrations/20261016_add_compare_synthesis.sql` after the existing
subscription/Cortex/pricing migrations and restart the API. It adds JSONB
structured/context snapshots and `compare_analysis_intents`; existing runs are
not rewritten. PostgreSQL startup checks the new schema. See
[the migration runbook](runbooks/db-migrations.md) for operator-owned rollout.

The API accepts two or three explicit targets. Subscription enforcement may reduce the effective maximum: Free and Plus allow two targets, while Pro allows three. The four-provider examples below describe the legacy CLI `COMPARE_MODE=true` flow, not the FastAPI request limit.

In database-backed API mode, all target/model entitlements and monthly counters are enforced before providers start. Successful targets settle independently against their canonical requested model identity, even when a provider returns a versioned served-model snapshot; that same identity drives each response card's credits and the aggregate Compare credit total. Failed targets release their reserved model-response units. Provider-native search is reserved at up to three operations per target and settles each target's reported operations up to that product cap; legacy shared research still settles once when the rollback path is active. One unrecognized served snapshot cannot suppress inline credits or the other successful target ledger rows. For response-only API streams, on a streaming disconnect or error only successful targets whose output started are settled, plus independently reported search usage that must not be lost; completed-but-unemitted targets are otherwise released. A finalization failure releases and unregisters the reservation rather than allowing its activity heartbeat to keep credits reserved.

## Release Notes (2026-02-18)

- FastAPI `POST /v1/compare` now persists compare runs to DB when `DATABASE_URL` is set.
- Each model response in a compare run writes one `llm_requests` + one `llm_responses` row.
- `request_group_id` is now canonical and shared across:
  - API response payload
  - orchestrator compare logs
  - `public.llm_requests.request_group_id`
- API key attribution for compare now uses the same guardrails as chat:
  - mapped key owner is authoritative
  - unmapped key behavior controlled by `AUTO_REGISTER_UNMAPPED_API_KEYS` and `ALLOW_UNMAPPED_API_KEY_PERSIST`
- Required DB migrations for compare persistence:
  - `db/migrations/20260218_add_request_group_id_to_llm_requests.sql`
  - `db/migrations/20260218_llm_requests_api_key_owner_guard.sql`

## How to Enable Compare Mode

### Step 1: Edit your `.env` file

Add or update this line:
```bash
COMPARE_MODE=true
```

To disable compare mode and return to single-model mode:
```bash
COMPARE_MODE=false
```

### Step 2: Configure API Keys

Make sure you have at least one API key configured in `.env`:
```bash
OPENAI_API_KEY=sk-...
GOOGLE_GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
GROK_API_KEY=xai-...
```

The system will automatically use all configured providers.

### Step 3: Customize Comparison Targets (Optional)

Edit `config/config.py` to customize which models are compared:
```python
COMPARE_TARGETS = [
    {"provider": "openai", "model": "gpt-4o-mini"},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
    {"provider": "deepseek", "model": "deepseek-v4-flash"},
    {"provider": "grok", "model": "grok-4-latest"},
]
```

### Step 4: Run the Application

```bash
python main.py
```

You'll see:
```
=== Compare Mode Active ===
Queries will be sent to 4 models simultaneously

=== AI Chat (Compare Mode, Multi-turn Context Enabled) ===
Type 'exit' to quit, 'stats' to see token usage, or 'help' for commands

You: _
```

## Usage Examples

### Example 1: Simple Query

```
You: What is Python?

=== Comparison Results ===

[1] OPENAI/gpt-4o-mini
    Latency: 347ms | Tokens: 45 | Cost: $0.000023
    Response: Python is a high-level, interpreted programming language...

[2] GEMINI/gemini-3.5-flash-lite
    Latency: 289ms | Tokens: 52 | Cost: $0.000015
    Response: Python is a versatile programming language known for...

[3] DEEPSEEK/deepseek-v4-flash
    Latency: 412ms | Tokens: 38 | Cost: $0.000008
    Response: Python is an easy-to-learn programming language...

[4] GROK/grok-4-latest
    Latency: 523ms | Tokens: 61 | Cost: $0.000041
    Response: Python is a powerful, interpreted language that...

=== Summary ===
Successful: 4/4
Failed: 0/4
Total Tokens: 196
Total Cost: $0.000087
Session Total Cost: $0.000087
```

### Example 2: Conversation with Context

Compare mode maintains conversation history just like single mode:

```
You: What is machine learning?
[All 4 models respond with explanations]

You: Can you give me a simple example?
[All 4 models respond with examples, referencing previous context]
```

## Key Features

### 1. Automatic Conversation History

All models receive the full conversation context, so follow-up questions work naturally.

### 2. Session Statistics Tracking

All responses update your session statistics:
```
You: stats

=== Session Statistics ===
Requests: 8  (includes all model responses)
Total tokens: 372
Total cost: $0.000174
```

### 3. Error Handling

If some models fail, the system continues with successful responses:
```
[1] OPENAI/gpt-4o-mini
    Response: Successfully completed...

[2] GEMINI/gemini-3.5-flash-lite
    [ERROR] timeout: Request timed out after 60s

[3] DEEPSEEK/deepseek-v4-flash
    Response: Successfully completed...

=== Summary ===
Successful: 2/3
Failed: 1/3
```

### 4. Help Command Shows Current Mode

```
You: help

=== Available Commands ===
help          - Show this help message
stats         - Show token usage statistics
/reset        - Clear conversation history
/history      - Show recent conversation
exit/quit     - Exit the program

Current Mode: Compare Mode (COMPARE_MODE=true)
All prompts are sent to multiple models for comparison
```

## Switching Between Modes

To switch from Compare Mode to Single Model Mode:

1. Edit `.env`:
   ```bash
   COMPARE_MODE=false
   MODEL_TYPE=openai  # or gemini, deepseek, grok
   ```

2. Restart the application:
   ```bash
   python main.py
   ```

You'll see:
```
Initialized OpenAI client with model: gpt-3.5-turbo

=== AI Chat (Single Model, Multi-turn Context Enabled) ===
Type 'exit' to quit, 'stats' to see token usage, or 'help' for commands

You: _
```

## Tips

1. **Cost Awareness**: Compare mode costs more since you're calling multiple APIs. Check `stats` regularly.

2. **Performance**: The slowest model determines response time. Consider removing slow models from `COMPARE_TARGETS`.

3. **API Rate Limits**: Each provider has rate limits. If one fails, others still work.

4. **Context Length**: All models receive the same conversation history. Long conversations may hit token limits on some models.

5. **Response Selection**: The first successful response is added to conversation history for context continuity.

## Troubleshooting

### "ERROR: COMPARE_MODE=true but no API keys configured"

Solution: Add at least one API key to `.env`.

### Some models always fail

Solution: Check that API keys are valid and have sufficient credits.

### Responses are too slow

Solution: Remove slow models from `COMPARE_TARGETS` in `config/config.py`.

### Want to compare only 2 models

Solution: Edit `COMPARE_TARGETS` to include only the models you want:
```python
COMPARE_TARGETS = [
    {"provider": "openai", "model": "gpt-4o-mini"},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
]
```

## Architecture Notes

- **Concurrent Execution**: All model calls run in parallel for speed
- **Timeout Protection**: Each model has a 60-second timeout
- **Immutable Results**: All responses are stored immutably for consistency
- **Order Preservation**: Results appear in the order configured, not completion order
- **Graceful Degradation**: System continues even if some models fail
- **Canonical Grouping**: API compare returns one `request_group_id` used consistently in logs and DB persistence
- **Automatic Provider Search**: The frontend has no Compare Web switch and sends `routing.web_mode=auto`. Every target decides through its own native search tool and keeps its own sources; DeepSeek alone uses the Tavily-backed function loop.
- **Plan-Aware Model Defaults**: The frontend fills empty initial Compare slots from the current `/v1/entitlements` billing-class allowlist. It does not remove locked models from the picker and does not rewrite valid existing/manual selections.
- **Original Multi-Turn Response Layout**: Analyze opens Model responses with the existing equal-height desktop grid and independent card scrolling, tablet wrapping/stacking and horizontal sticky provider tabs on phones. Three desktop model responses share one row above 1180px. Phone tabs select one natural-height answer with the existing responsive card controls. Cortex Analysis and Sources remain separate views; view changes retain local feedback, selected model and reading position. Evidence opens the exact immutable revision.
- **Shared Prompt Presentation**: Compare prompts use the same right-aligned `You` bubble as Ask mode, including attachment and prompt-optimization states. While Improve is pending, the prompt and optimization status remain visible but model tabs, response cards, and aggregate totals stay hidden; they appear only after optimization resolves and model generation begins. Aggregate Compare totals render separately.
- **New-Turn Reveal**: Submitting a Compare follow-up always smoothly reveals that new question once, even when the user was viewing an older turn. Streaming response growth does not continuously move the transcript, and the UI no longer renders a floating down-arrow jump control.
- **Independent Response Readiness**: Every browser response card shows the shared provider/model logo treatment and owns its own calm loading state. Loading cards show live elapsed time with `Queued`, `Refining prompt`, `Connecting to model`, `Generating response`, or `Finalizing` instead of placeholder zero metrics. The skeleton body disappears and that card's timer freezes when its first renderable response content reaches the UI, while slower targets continue timing and showing their own source/improvement-aware loading treatment. Failed cards show elapsed failure time, and completed cards retain the observed response-start duration while hiding unavailable token counts.
- **Provider-Aware Live Activity**: `/v1/compare/stream` emits a normalized `activity` envelope with a request id, monotonically increasing sequence, timestamp, event type, safe display message, and target index. OpenAI, Claude, Gemini, and Grok adapters consume their native SDK streams and multiplex real answer deltas and known search/thinking/tool phases per target. DeepSeek remains on the buffered path. Request-wide research events apply only to cards still running, unknown raw provider events are ignored, and provider terminal events cannot complete the public request. A failed target keeps any partial answer and shows safe retry guidance while the other targets continue.
- **Compact Compare Composer**: The React composer starts as a single-line prompt, expands only for longer input, and relies on the main Ask/Compare navigation instead of duplicating a mode switch beside Send. Attach is followed by the shared sliders-style More options trigger, whose single dialog holds the Improve switch and inline Reasoning choices; Web is automatic and has no UI control. Enabled Improve and explicit non-Auto Reasoning render as removable chips, with the chip stack moving to its own row on narrow phones. On narrow screens, active model chips stay in their options row and scroll horizontally when a third model is added.
- **Responsive Compare Cue**: Active model chips are separated by a decorative opposing-arrows connector instead of a literal `VS` label. Desktop uses a quiet circular medallion; mobile removes the border and background to preserve model-name space inside the horizontal selector scroller.
- **Mobile Model Picker**: Compare model dropdowns render through a fixed-position body portal, keeping every option visible and selectable above the horizontally scrollable mobile model row.
- **Purposeful Empty State**: Before the first turn, Compare mode explains the ask-once, multi-model workflow and the value of comparing accuracy, depth, speed, tone, and usefulness. Three practical examples fill the prompt without submitting or changing the selected models.
- **Controlled Response Rhythm**: Compare card titles, metadata, paragraphs, Markdown headings, and lists use a restrained type scale and tighter spacing so long model outputs remain readable without excessive vertical gaps.
- **Append-only Cortex Analysis**: Successful synthesis runs live below Compare responses, survive reload/history reopening, retain every prior run, and use exact response revisions to disclose stale analyses without overwriting them.

---

**Last Updated:** 2026-09-27
**Applies To:** OpenAI Project v2.0+
