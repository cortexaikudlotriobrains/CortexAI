# Ask and Compare credit efficiency

## OpenAI search context

OpenAI Ask/Compare native search uses a fixed `search_context_size="medium"`
on the Responses `web_search` tool for buffered and streaming requests. There
is no environment flag, public request override or automatic low/high selector.
Existing search-off handling, three-call caps, citations, reasoning/output
settings and billing remain unchanged. Restart API workers after deploying
the adapter change. Pinning this balanced setting does not demonstrate a new
credit saving; evaluate actual provider usage before changing context size.

Validate the adapter contract and streaming compatibility with
`python -m pytest tests/test_native_web_search_contracts.py tests/test_provider_live_streaming.py -q`.
No provider inference is needed for these tests. See
[OpenAI's search-context guidance](https://developers.openai.com/api/docs/guides/tools-web-search#search-context-size).

## Claude prompt caching

Set `CLAUDE_PROMPT_CACHE_ENABLED=true` and configure a long random
`CACHE_KEY_SECRET` in the API process environment. Preserve an existing secret;
do not print or commit it. Restart API workers after environment changes.
The sample environment enables the Claude flag, but an absent secret still
disables Cortex cache activation. Other providers' cache flags are independent.

Claude receives top-level `cache_control={"type":"ephemeral"}` with the default
five-minute TTL. Full messages, model selection, reasoning and output ceilings
are unchanged. Anthropic can also cache growing server-tool result prefixes
between iterations within one request. Exact prefixes and provider minimum
lengths still apply; cache writes carry their approved provider-price premium.
An hour-old conversation is not a guaranteed cache hit.

Inspect `Claude completion successful` logs for `cache_control_sent`,
`normal_input_tokens`, `cached_input_tokens` and `cache_write_tokens`.
`cache_control_sent=true` confirms the payload option, not a cache hit. Only
provider-reported cache reads establish reuse. An unsupported-parameter retry
that removes the marker logs `cache_control_sent=false`.

The existing cache-aware credit calculator partitions normal input, reads and
writes and settles from the stored pricing snapshot. Keep
`CACHE_AWARE_CREDIT_SETTLEMENT_ENABLED` at the deployment's reviewed setting;
do not change pricing authority or billing rollout flags to activate caching.
The displayed saving is already included in the charged total.

Validate with `python -m pytest tests/test_claude_credit_efficiency.py
tests/test_unified_response_contract.py tests/test_credit_calculator.py -q`.
Provider doubles verify create/stream payloads and usage partitions; they do
not establish live savings. Compare actual ledger input/cache/search components
before and after representative live requests without silently replaying paid work.

Rollback: set `CLAUDE_PROMPT_CACHE_ENABLED=false` and restart workers.

References: [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
and [server-tool caching](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching).

## Filtered Claude web search

`CLAUDE_FILTERED_WEB_SEARCH_ENABLED=true` selects `web_search_20260318` on
documented capable families. Unsupported/unknown models retain the basic tool.
The server-side filter selects relevant search content; it does not lower model
reasoning or output limits. Required search still receives an explicit system
instruction, and the provider `max_uses` stays capped at three. Provider search
counts, citations, cached usage and token charges are parsed as before; code
execution blocks are never exposed as answer text or extra search operations.

Inspect `web_search_tool_version` on completion logs. Compare citation coverage,
answer completeness and input/cache usage on representative research questions;
provider-double tests establish payload/accounting compatibility, not a measured
quality or savings guarantee. Roll back with the flag set to false and restart.

## Selective Compare follow-up history

The React Compare client adds optional `response_variants` provenance to prior
assistant history messages. Each variant contains its exact provider, model
and complete answer text, and joining the texts with two newlines must reproduce
the parent content. Invalid provenance is a 422 request error. Provenance is
never passed to a provider. Existing 10-message/20,000-character history guards
still apply; trimming or compaction discards affected provenance.

`COMPARE_SELECTIVE_HISTORY_ENABLED=true` (default) selects each target's exact
own prior answer for ordinary follow-ups. Retained user/system messages are not
rewritten. Comparisons, named models, ordinal or ambiguous references retain
shared context. Non-ASCII-language prompts, missing/changed target identities,
duplicate matches and legacy clients also retain shared content. This is a
conservative reference heuristic, not a semantic quality guarantee. Preserve
complete user constraints and inspect representative follow-ups before rollout.

Both streaming targets and non-streaming Compare use the same selector, as does
Compare-card regeneration through Chat. Ask's ordinary history format remains
unchanged. Initial synthesis still reads all current answers and sources.
Reservations/estimates remain conservative over shared input; only actual
provider token usage determines settled charges. `compare.history.selected`
logs selected-answer count and before/after character counts without prompt text.

Rollback: set `COMPARE_SELECTIVE_HISTORY_ENABLED=false` and restart the API.
Old clients remain compatible. No migration is required. Validate provenance,
cross-answer references, model switches, restored history and regeneration with
`tests/test_compare_history_selection.py` and the React history/follow-up tests.
