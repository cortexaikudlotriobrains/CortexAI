# Generation Budgets

Ask and Compare use one provider-aware generation budget for request validation,
provider execution, credit authorization, response metadata, persistence, and retry UX.
The canonical policy is in `config/generation_profiles.yaml`; routes, providers,
billing, and React do not own independent output-token defaults.

## Cortex-managed Auto policy

The React app does not expose output-token profiles as an Answer depth control. New Ask
and Compare calls send `generation.profile=auto`, and the server chooses enough output
room from the selected model and prompt. The adjacent Reasoning control is independent:
it selects model thinking effort, not answer length. Users still request concise,
detailed, tabular, or step-by-step output in the prompt.

| Auto case | Requested output ceiling | Default reasoning effort |
| --- | ---: | --- |
| Normal task on an economical or standard model | 4,096 | low |
| Normal task on an advanced reasoning model, including GPT-5.6 Terra and Claude Sonnet | 8,192 | medium |
| Premium model, including Claude Opus and Fable | 12,288 | high |
| Clearly complex, accuracy-constrained, long, or explicitly detailed task | 12,288 | high |

Complex-task detection is deterministic and local. Explicit output markers such as
`comprehensive`, `detailed`, `step-by-step`, `architecture`, `production code`, and
`full report` select 12K. Code, logs, math, or analysis select 12K when paired with
strict/accuracy constraints or an estimated input of at least 1,800 tokens. Auto does
not silently select 32K.

All ceilings remain subject to the selected model's native output maximum, remaining
context after the 1,024-token safety margin, the 32,768 operational ceiling, and any
affordability clamp applied before provider execution.

## Explicit API profiles

API callers may deliberately override Auto:

| Profile | Requested output ceiling | Default reasoning effort | Intended use |
| --- | ---: | --- | --- |
| `quick` | 1,024 | low | Short, low-credit API calls and omitted legacy requests |
| `balanced` | 4,096 | medium | Fixed normal API budget |
| `deep` | 12,288 | high | Explicit long-analysis budget or first Auto retry |
| `extended` | 32,768 | max | Explicit maximum room or final retry |

An explicit `generation.max_output_tokens` or legacy `max_tokens` above a model's safe
bound is rejected with `422 invalid_generation_budget`; it is never silently clipped.

## Request contract

The browser sends:

```json
{
  "generation": {
    "profile": "auto",
    "reasoning": {"mode": "auto", "effort": "auto"}
  }
}
```

API callers may also supply provider-neutral reasoning controls:

```json
{
  "generation": {
    "profile": "auto",
    "reasoning": {"mode": "auto", "effort": "auto"}
  }
}
```

`profile` and `max_output_tokens` are mutually exclusive. `generation` and legacy
`max_tokens` are also mutually exclusive. Compare additionally permits a target-level
`generation` override; a target override wins over the shared Compare value. API
callers that omit both budget fields retain the compatibility default `quick`/1K.

## Reasoning mapping

The browser exposes one product scale: `Auto`, `Low`, `Medium`, `High`, and `Maximum`.
`Auto` leaves both mode and effort automatic. A manual choice sends `mode=on` with
`effort=low|medium|high|max`. Maximum maps to native `max` when available and otherwise
to `xhigh`; Low maps to `low` or `minimal`. A manual request never silently jumps from
Low to Medium/High or from Medium to High.

Smart Ask starts at Auto. Selecting a manual effort becomes a hard routing constraint:
models that cannot honor it are removed, including during fallback or tier escalation,
and each attempted model receives its own resolved provider parameters. Manual Ask
starts at the selected model's lowest supported public level. Compare starts at Low and
enables only the intersection supported by every selected target. A model with no
controllable reasoning exposes an unavailable control. Work has no new control.

After a successful Smart Ask completes, the response card shows the authoritative
effective `Low`, `Medium`, `High`, or `Max` effort beside AI-credit usage. The metric
uses both `routing_mode=smart` and `generation_budget.effective_reasoning_effort`, so it
reflects the route and budget actually executed rather than the request-time preview.
Each level has distinct text and color styling; color is never the only signal. Manual
Ask, Compare, Work, pending/failed cards, and unknown or disabled efforts omit it. The
same fields are retained in history so restored Smart responses render consistently.

`GET /v1/model-options` and the opt-in `/v1/models` expose `reasoning_levels`,
`default_reasoning_level`, and `reasoning_controllable` in addition to the native
reasoning metadata. The React UI consumes only these normalized fields.

`orchestrator/generation_policy.py` translates the provider-neutral mode and effort
into adapter parameters. The model registry declares supported modes, efforts,
disable support, whether reasoning counts against output, and native output limits.
DeepSeek receives thinking configuration, Gemini receives thinking configuration, and
supported OpenAI models receive reasoning effort. Claude translation is generation
specific: registry-declared Claude 4.6 and Claude 5 models receive adaptive thinking
and supported output effort, while manual-budget-only Claude 4.5 models default to
normal generation because the public generation contract does not expose Anthropic
`budget_tokens`. Explicit reasoning-on for those 4.5 models is rejected before credits
are reserved.

Anthropic sampling controls are coupled to thinking support. The Claude adapter has no
implicit temperature default. It forwards a caller-supplied custom temperature only
to Claude 4.5/4.6 requests whose thinking is off; adaptive-thinking requests and Claude
5 requests omit it so Anthropic applies its required default sampling.

Reasoning tokens are model work tokens, not hidden conversation history. When a
provider counts them inside its output allowance, a small output ceiling can be
exhausted before visible text is produced. Such a response remains billable provider
work and is returned as `incomplete`, not rewritten as a provider failure.

## Billing and estimates

The exact effective ceiling passed to a provider is also passed to
`server/billing/enforcement_service.py`. Compare authorizes each target with its own
resolved ceiling. The preflight is a maximum temporary AI-credit hold; settlement
uses actual successful usage and releases the unused amount. The browser does not show
a live hold estimate in the composer. Higher manual reasoning can increase latency and
provider output work; actual successful usage remains the settlement authority.

`POST /v1/billing/estimate-generation` remains available to API callers. It performs
the same Auto/profile resolution and credit calculation without reserving credits,
returning per-target ceilings, the maximum temporary hold, remaining credits, and
whether the hold can currently be authorized.

## Completion and retry contract

Every response exposes:

- `completion_status`: `complete`, `incomplete`, or `failed`
- `stop_cause`: normalized terminal cause such as `natural`, `token_limit`,
  `context_limit`, `content_filter`, or `error`
- `generation_budget`: requested/effective profile, ceiling, reasoning values, and
  policy version
- `retry_with_more_room`: whether a larger profile is available and its recommendation

Partial text is preserved. React displays an incomplete banner and can retry the same
response slot. Auto 4K and 8K calls retry with `deep`/12K; Auto 12K calls retry with
`extended`/32K; an Auto call already at 32K has no larger retry. A retry is a new model
call and may use additional AI credits. If it fails, the original partial response
remains.

## Persistence and operations

Apply `db/migrations/20260804_add_generation_budget_audit.sql` before deploying this
code. It adds request-level budget/reasoning audit fields plus response-level terminal
status and stop cause. PostgreSQL startup preflight requires these columns.

`GENERATION_BUDGET_POLICY_ENABLED=true` enables provider-aware budgets and is the
default. Setting it to `false` is the rollback switch: new profile requests execute
with the historical Quick/2K ceiling while the public contract remains available.
Restart API processes after changing the setting or applying the migration.

See `docs/runbooks/generation-budget-rollout.md` for deployment and rollback checks.
