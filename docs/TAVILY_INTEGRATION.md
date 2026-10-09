# Tavily Integration Guide

## Purpose

Tavily is the backend search provider for DeepSeek's Ask/Compare function tool and for the legacy shared-research rollback path. OpenAI, Claude, Gemini, and Grok use their own provider-native search tools when native search is enabled.

Primary integration path:

- `tools/web/tavily_client.py`
- `tools/web/tavily_resolver.py`
- `tools/web/tavily_service.py`
- `tools/web/factory.py`
- `tools/web/intent.py`
- `tools/web/research_pack.py`
- `api/deepseek_client.py`

See `docs/PROVIDER_NATIVE_WEB_SEARCH.md` for the cross-provider policy and rollout contract.

## Runtime Modes

At route level, the current API contract uses `routing.web_mode=off|auto|required`; `on` aliases required. `routing.research_mode` remains a backward-compatible boolean:

- `false` -> no web research for this turn
- `true` -> research-enabled flow

Inside orchestration, research state tracks behavior as:

- `off`
- `auto`
- `on`

Current behavior highlights:

- With `NATIVE_WEB_SEARCH_MODE=enabled`, DeepSeek can invoke the local Tavily-backed `web_search` function up to three times. Each operation is limited to five results.
- Compare runs that function loop independently for each DeepSeek target; Tavily results are not shared with other providers.
- `on` performs a fresh search for the current turn.
- In `on`, local research cache reuse is bypassed.
- If sanitized query is empty in `on`, system falls back to raw prompt.
- Source metadata is normalized into `web_source_items` on responses.
- Research metadata is additive: CortexAI does not post-classify successful model text as fabricated or replace it based on phrases, dates, numbers, or missing citation markers.
- Missing provider timestamp values are normalized to server UTC ISO timestamps.
- Tavily search-option resolution is deterministic and local. It does not call an LLM, does not call Tavily, and does not rewrite the query.

## Credit Settlement

- One Tavily credit costs USD 0.008 at the pay-as-you-go rate and converts to `8,000 Cortex credits` at the USD 1 per million credits calibration ceiling. Advanced Search consumes two Tavily credits (USD 0.016).
- DeepSeek native-search preflight reserves up to three operations at `2 Tavily credits x 8,000 = 16,000 Cortex credits` per operation.
- DeepSeek settlement charges `16,000 Cortex credits` per successful Tavily tool operation and records an immutable `item_type=tool` row with `backend=tavily` and a USD 0.016 provider cost.
- The legacy shared-research path reserves `2 Tavily credits x 8,000 = 16,000 Cortex credits` and settles `Tavily API credits used x 8,000 Cortex credits`.
- If Tavily omits usage metadata, settlement uses the two-credit Advanced Search fallback and marks the ledger row as estimated.
- Cache hits and session-state reuse report zero provider credits and add no new research charge.
- Only the legacy rollback path performs one shared Compare retrieval. Native Compare search is per target.
- A successful Tavily response is settled from its reported usage even when it yields no usable sources. Calls that fail without a usage response add no research charge.
- Research ledger metadata records `provider_credits_used` and `cortex_credits_per_provider_credit`.

## Search-Options Resolver

The resolver receives the sanitized search query plus optional locale context and returns Tavily `/search` options.

Fixed params on every Tavily search call:

- `max_results=5`
- `search_depth=advanced`
- `chunks_per_source` from `TAVILY_CHUNKS_PER_SOURCE` (`1..3`, default/fallback `3`)
- `include_raw_content=false`
- `include_answer=false`
- `auto_parameters=false`

Enhanced params are added only when `TAVILY_ENHANCED_SEARCH_ENABLED=true`:

- `topic=finance|news` for finance/news categories only.
- `time_range=day|week|month|year` when the prompt has a freshness signal or a non-stable finance/news query should use current results. Multi-year/historical prompts are left unbounded.
- `country=<lowercase full country>` only when no `topic` is sent, because Tavily country filtering is a general-search option.
- `include_domains` for curated finance rules only:
  - Canada economics: `bankofcanada.ca`, `statcan.gc.ca`
  - US economics: `bls.gov`, `bea.gov`, `federalreserve.gov`
  - US SEC filings: `sec.gov`
  - UK economics: `ons.gov.uk`, `bankofengland.co.uk`

Category precedence is `finance > health > news > coding > general`. Finance/news map to Tavily `topic`; health/coding/general omit `topic`.

Country precedence is explicit prompt country first, then locale context, then omit. Multi-country and regional prompts such as EU/global queries omit country targeting. Finance/news country decisions are logged but not sent to Tavily; regional precision comes from domain allowlists where a rule exists.

The resolver is intentionally not a query rewriter. Prompt optimization and existing query sanitization stay outside this module.

## Configuration

Set API key in environment:

```ini
TAVILY_API_KEY=tvly-xxxxxxxxxxxxxxxxxxxxxx
TAVILY_ENHANCED_SEARCH_ENABLED=true
TAVILY_CHUNKS_PER_SOURCE=3
TAVILY_ENHANCED_SEARCH_DOMAIN_RULES=true
DEEPSEEK_AGENTIC_SEARCH_ENABLED=true
DEEPSEEK_WEB_SEARCH_MAX_RESULTS=5
```

Dependency:

- `tavily-python` is included in `requirements.txt` for standard installs.

Kill switch:

- `TAVILY_ENHANCED_SEARCH_ENABLED=false` disables topic/time/country/domain enrichment while keeping the fixed Tavily retrieval params above.

Operational diagnostics:

- `research.search.resolver` logs the resolver decision without raw query text.
- `research.search.success` adds `result_count`, `source_content_lengths`, `credits_used`, and `credits_estimated`.
- Tavily advanced search is treated as `2` API credits when the provider response does not include usage metadata.

## Validation

Recommended checks:

- `tests/test_tavily_client.py`
- `tests/test_tavily_service.py`
- `tests/test_credit_calculator.py`
- `tests/test_billing_metering.py`
- `tests/test_tavily_resolver.py`
- `tests/test_research_pack.py`
- `tests/test_routing_regression.py`
- `tests/test_native_web_search_contracts.py`

## Notes

- This integration is API-first; do not rely on legacy CLI-only flows when validating web research behavior.
- For end-to-end behavior, use the browser E2E suite (`npm run --prefix e2e test`) and inspect inline response citation pills + persisted metadata.

---

Last updated: 2026-09-27
