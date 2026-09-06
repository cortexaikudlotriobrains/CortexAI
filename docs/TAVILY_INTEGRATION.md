# Tavily Integration Guide

## Purpose

Tavily is used as the web-research provider for research-enabled Ask/Compare turns.

Primary integration path:

- `tools/web/tavily_client.py`
- `tools/web/tavily_resolver.py`
- `tools/web/tavily_service.py`
- `tools/web/factory.py`
- `tools/web/intent.py`
- `tools/web/research_pack.py`

## Runtime Modes

At route level, API contract uses `routing.research_mode` as a boolean:

- `false` -> no web research for this turn
- `true` -> research-enabled flow

Inside orchestration, research state tracks behavior as:

- `off`
- `auto`
- `on`

Current behavior highlights:

- `on` performs a fresh search for the current turn.
- In `on`, local research cache reuse is bypassed.
- If sanitized query is empty in `on`, system falls back to raw prompt.
- Source metadata is normalized into `web_source_items` on responses.
- Research metadata is additive: CortexAI does not post-classify successful model text as fabricated or replace it based on phrases, dates, numbers, or missing citation markers.
- Missing provider timestamp values are normalized to server UTC ISO timestamps.
- Tavily search-option resolution is deterministic and local. It does not call an LLM, does not call Tavily, and does not rewrite the query.

## Credit Settlement

- Research preflight reserves `2 Tavily credits x 5,000 = 10,000 Cortex credits`, matching the normal Advanced Search call.
- Settlement uses the successful provider response's usage: `Tavily API credits used x 5,000 Cortex credits`.
- If Tavily omits usage metadata, settlement uses the two-credit Advanced Search fallback and marks the ledger row as estimated.
- Cache hits and session-state reuse report zero provider credits and add no new research charge.
- Compare performs one shared retrieval and adds its research charge once, not once per target.
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

## Notes

- This integration is API-first; do not rely on legacy CLI-only flows when validating web research behavior.
- For end-to-end behavior, use the browser E2E suite (`npm run --prefix e2e test`) and inspect inline response citation pills + persisted metadata.

---

Last updated: 2026-07-31
