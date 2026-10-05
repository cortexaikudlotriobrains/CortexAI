# Provider-Native Web Search

## Product Contract

Ask and Compare do not expose a Web control. The React client sends
`routing.web_mode="auto"`, leaving the user free to describe the outcome rather
than configure retrieval.

The route policy resolves each turn before provider execution:

- `off`: never mount or call a search tool.
- `required`: require search for the turn.
- `auto`: explicit no-browse wording becomes `off`; current-information or
  explicit search wording becomes `required`; other prompts leave the decision
  to the selected provider.
- `on` is accepted as an API alias for `required`.
- Legacy `routing.research_mode=true|false` remains accepted as required/off.

A context-only follow-up such as “continue from where you left” does not by
itself force a new search.

## Provider Execution

| Provider | Search integration | Required-mode control | Operation evidence |
| --- | --- | --- | --- |
| OpenAI | Responses `web_search` | `tool_choice=required` | `web_search_call` output items |
| Claude | `web_search_20250305` server tool | provider tool choice | server-tool usage/result blocks |
| Gemini | Interactions `google_search` | required-search system instruction | Google search call/result steps and grounding usage |
| Grok | Responses `web_search` | provider tool choice | server-side tool usage and citations |
| DeepSeek | local `web_search` function backed by Tavily | first function turn requires the tool | successful local Tavily executions |

Gemini native search requires `google-genai>=2.0.0` after Google's June 2026
retirement of the legacy Interactions schema. Cortex reads the v2 `steps`
response and grounding-tool usage and sends multi-turn history as
`user_input`/`model_output` steps. It does not send the legacy `allowed_tools`
selector: SDK 2.x interprets that selector as a client-function allowlist, while
plain `tool_choice="any"` can force another tool call instead of allowing the
model to write its final answer. Required mode is expressed by the bounded
Google Search instruction and the mounted server-side tool. Gemini 3.5 requests
also omit the legacy `temperature` sampling control while preserving reasoning
level and the output-token limit.

Every model request is capped at three billable search operations. OpenAI,
Claude, Grok, and DeepSeek receive request- or loop-level enforcement. Gemini's
managed Google Search tool does not expose an exact query-count request field,
so Cortex supplies the same three-query instruction, records any reported
provider overrun for audit, and never bills the user above the product cap.
Normalized response metadata keeps the billable operation count, the provider-
reported count, and at most eight deduplicated `web_source_items`. DeepSeek
requests at most five Tavily results per operation.

Compare never shares a source pack across targets. Each model receives only its
own search facility, and each response retains its own citations and operation
count. This preserves the meaningful difference between model research
strategies. Cortex Analysis receives those per-response sources and a compact
used/operation summary, but its synthesis request has web search explicitly
disabled.

## Live Progress Boundary

Ask and Compare expose provider-neutral `activity` events in their NDJSON
streams. Cortex-owned research, including DeepSeek's local Tavily function loop,
emits `search_started`, `search_completed`, and `analyzing_results` only at the
corresponding real execution boundaries. OpenAI, Claude, Gemini, and Grok manual
Ask/Compare adapters consume their native provider streams. Known hosted-search
events are normalized into safe activity, and real answer deltas are forwarded
immediately while the adapter still builds the final response used for
authoritative search counts and billing. The UI must not claim that provider
search started merely because `web_mode` is enabled; unknown or absent provider
events remain generic progress. Smart Ask and DeepSeek retain their existing
buffered paths. OpenAI-compatible native stream accumulation requires
`openai>=2.14.0,<3.0.0`.

## Billing

The reservation covers the maximum three operations for each potential Ask
provider or each explicit Compare target. Settlement releases unused capacity
and charges only operations reported by the adapter, in addition to normal
model-token credits.

| Search backend | Cortex credits per operation | Provider-cost audit value |
| --- | ---: | ---: |
| OpenAI | 10,000 | $0.010 |
| Claude | 10,000 | $0.010 |
| Gemini | 14,000 | $0.014 |
| Grok | 5,000 | $0.005 |
| DeepSeek via Tavily | 10,000 | $0.010 |

The DeepSeek amount is two Tavily credits at $0.005 each. Search charges are
stored as immutable `credit_transactions.item_type='tool'` rows with provider,
backend, operation count, fixed credits, provider cost, and whether usage was
estimated. Apply
`db/migrations/20260927_add_tool_credit_transaction_item.sql` before deploying
the writer. A search that reports no successful operation adds no tool charge;
DeepSeek preserves completed tool usage even if a later model turn fails.

## Rollout and Rollback

```ini
NATIVE_WEB_SEARCH_MODE=enabled
NATIVE_WEB_SEARCH_PROVIDERS=openai,claude,gemini,grok,deepseek
COMPARE_NATIVE_WEB_SEARCH_ENABLED=true
DEEPSEEK_AGENTIC_SEARCH_ENABLED=true
PROVIDER_LIVE_STREAMING_PROVIDERS=openai,claude,gemini,grok
WEB_SEARCH_MAX_OPERATIONS=3
WEB_SEARCH_MAX_DISPLAY_SOURCES=8
DEEPSEEK_WEB_SEARCH_MAX_RESULTS=5
TAVILY_API_KEY=tvly-...
```

- `enabled` activates native adapters. `off` and `shadow` retain the legacy
  shared orchestrator-research path; shadow is suitable for configuration and
  telemetry rollout without changing provider execution.
- `NATIVE_WEB_SEARCH_PROVIDERS` narrows adapter enablement. An excluded provider
  receives no native tool and does not borrow another provider's sources.
- `COMPARE_NATIVE_WEB_SEARCH_ENABLED=false` rolls Compare back independently.
- `DEEPSEEK_AGENTIC_SEARCH_ENABLED=false` disables the DeepSeek tool loop.
- `PROVIDER_LIVE_STREAMING_PROVIDERS` is an independent manual Ask/Compare
  stream allowlist. Set it to a subset for staged rollout or `off` to retain
  buffered delivery; it does not enable DeepSeek or Smart streaming.
- The three product caps are hard upper bounds even if larger environment
  values are supplied.
- `TAVILY_API_KEY` is required for DeepSeek search and for the legacy path, but
  not for the other four native adapters.
- Reinstall `requirements.txt` and restart every API worker during this rollout;
  a worker still running `google-genai` 1.x will receive HTTP 400 for every
  Gemini Interactions request after the legacy-schema retirement.

CortexAI Work is intentionally out of scope and retains its separate
`Web · Auto|On|Ask|Off` policy and approval behavior.

## Validation

Focused coverage:

```powershell
venv\Scripts\python.exe -m pytest tests/test_native_web_search_contracts.py -q
venv\Scripts\python.exe -m pytest tests/test_billing_metering.py tests/test_billing_repository.py -q
npm run --prefix frontend-react test -- --run
npm run --prefix frontend-react build
```

Validate both non-streaming and streaming Ask/Compare responses, provider-local
sources, zero-operation settlement, failure-after-search settlement, history
rehydration, source rendering, and absence of the Ask/Compare Web control on
desktop, tablet, and mobile.
