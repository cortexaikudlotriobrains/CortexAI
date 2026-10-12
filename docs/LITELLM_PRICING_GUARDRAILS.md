# LiteLLM pricing guardrails review

Reviewed 2026-10-11 (America/Toronto), against freshly fetched `develop`
`68e3e92bd9abe6948eefca7d1f5f6bc80c3b4338`, on
`fix/litellm-pricing-guardrails`. This is a correctness patch, with no new rate
approvals, schema changes, credit-policy changes, production database writes,
environment edits, deployment changes, merge or deployment.

## Root causes and implemented scope

| Issue | Finding | Change and remaining gate |
|---|---|---|
| 1. Context/cache writes | `validate_rates` synthesized absent cache prices; `snapshot` then merged incomplete context bands into base rates. The calculator accessed optional rates even when usage was zero. | Retain missing versus explicit zero; clear optional base/preceding-tier partitions when applying a band; require prices only for nonzero usage and raise `PricingUnavailableError`/`PRICING_UNKNOWN` otherwise. Explicit one-hour selection requires a distinct rate. Keep token subsets disjoint. The OpenAI adapter already reads the actual write counter and now records its documented `30m` lifetime. No one-hour usage is inferred. |
| 2. DeepSeek aliases | A missing write price became regular input, while explicit zero stayed zero. The sync also processed retired identities as activation targets. | Preserve both source representations and exact active-model conflict rejection. Retired DeepSeek Chat aliases are observation-only, with `retired_model_observation_only`; they cannot be candidate-approved or mapped to replacement prices by sync. The existing runtime identity/lifecycle mapping remains authoritative. No retired pricing redesign or global equivalence rule. |
| 3. Gemini audio | Normalized `TokenUsage` retains aggregate input/cache/output/thought tokens, but discards provider modality partitions. Distinct audio rates cannot be applied safely through that contract. | Retain rejection; explain that normalized audio and cached-audio token partitions are required. Persist rejected dimensions and their source values. Existing text/image aggregate billing remains intact. Partition propagation through response persistence and billing is follow-up scope, not a guessed audio charge. |
| 4. Search services | Token/service separation already existed. The unchanged-token fast path could skip review of new service evidence. | Preserve `separate_charges`, add complete observed charge evidence, and require review even when token prices match an approved source card. No service prices are imported into model token units or activated from LiteLLM. Independent service-card cost and customer search-credit caps are regression-tested. Mini search-content billing still needs provider usage reconciliation before cutover. |
| 5. DeepSeek schedules | Sync checked synchronized/legacy baselines but could miss a manual-only schedule. Manual override creation did not enforce completeness. | Sync, manual override and candidate approval require schedules from applicable approved cards and registry rules. Flat source proposals remain rejected as `incomplete_billing_schedule`. UTC boundaries, holidays, calendar year and per-call evidence remain unchanged. |
| 6. Promotions/history | Schema already stores immutable financial cards and `[effective_from,effective_to)` intervals. CLI manual JSON could not express expiry; candidate approval reset future starts; selection could revive legacy seeds after verified expiry; sync could reopen an expired promotion. | Expose optional manual effective boundaries using the existing schema, reject invalid/expired intervals and expired candidates, retain future candidate starts and ends, reject same-source overlap and interval extension, and prevent automatic legacy resurrection. Expired/bounded changes require review. No successor price is invented. |

## Verified provider contracts and remaining questions

- [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)
  documents `input_tokens_details.cached_tokens` and `cache_write_tokens` as
  subsets of aggregate input. GPT-5.6 and later currently use `30m`; a cache
  write substitutes its rate for those tokens rather than adding a fee. Cortex's
  shared OpenAI-compatible adapter already extracts both counters. One-hour
  pricing fields in LiteLLM are not proof of OpenAI one-hour usage. The fixture's
  five-minute/one-hour cases exercise generic dimensions, not Sol capabilities.
- [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
  reports separate five-minute and one-hour creation counts. Cortex's Claude
  adapter already passes the one-hour subset to `CostCalculator`; the mixed
  duration regression preserves distinct rates without duplicate input charges.
- [DeepSeek cache documentation](https://api-docs.deepseek.com/guides/kv_cache/)
  and [cache usage announcement](https://api-docs.deepseek.com/news/news0802/)
  describe hit/miss accounting. The usage regression prices misses as ordinary
  input and observes no separate write counter. Missing and zero source write
  rates are still distinct evidence; no assumption covers arbitrary future
  provider write fields. The registry's retired Chat identity resolves runtime
  requests to V4 Flash; source discovery deliberately preserves the old identity.
- [Gemini's API usage schema](https://ai.google.dev/api/interactions-api)
  exposes optional `input_tokens_by_modality` and `cached_tokens_by_modality`.
  Cortex currently does not propagate them into its normalized/persisted usage.
  Before accepting audio-priced imports, verify their availability for the exact
  endpoint/model, propagate and validate both partitions, and partition aggregate
  counts instead of adding an audio charge on top. Missing breakdowns must fail
  safely on audio usage. The current attachment allowlist does not admit audio.
- Actual search tools are OpenAI Responses `web_search`, Claude
  `web_search_20250305`, Gemini Google Search, Grok native search, and DeepSeek's
  Tavily bridge; Work retains its separate billing integration. Model calls use
  token cards; `build_web_search_metadata` uses `__web_search__` service cards in
  database mode. Provider expense counts every reported operation, while customer
  search credits retain their three-operation cap. LiteLLM's context-search fee
  must be compared to the exact tool/API mode, not treated as a universal rate.
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing) distinguishes
  non-preview/preview service fees and Mini's fixed 8,000 search-content input
  tokens per non-preview call. Reconcile that block against actual Responses
  usage and invoices before adding anything: current aggregate input could
  already include it. The same page describes Sol's promotion as available
  **at least** through November 21, 2026; this is not an exact expiry or an
  authorization to assume a future successor rate. No dates or prices from that
  page are hardcoded by this patch.

No active headline price changes are approved here. Claude Sonnet 5, Gemini 3.6
Flash and GPT-5.6 Sol proposals still need exact model/mode/provider evidence,
promotion validity and human approval. Existing legacy seeds are historical
bootstrap values, not independently verified current provider authority.

## Regression evidence

The initial 61-test run had one local baseline failure: `.env` enabled automatic
activation, contradicting the test's default-policy assumption. The pricing
fixture now forces auto-activation off; tests requesting automatic activation
continue to pass an explicit policy. No environment file was edited.

New tests were run before their respective fixes. Thirteen initially failed
for missing/context prices, audio evidence, retired aliases, expiry and
manual-only schedules; two more reproduced approval/manual-schedule defects;
one later reproduced expired-promotion extension.

The pricing suite now has 91 passing cases, retaining all 61 original cases.
It adds context boundary, zero/missing optional prices, distinct duration subsets,
OpenAI usage extraction/lifetime, retired DeepSeek alias observations, cache-miss
accounting, audio rejection evidence, active alias conflicts, zero/normal/cap-hit
search service expense, unchanged-token service review, effective intervals,
expired promotions/candidates, and both V4 schedule guards. Final additions cover
explicit-zero audio evidence, Gemini text/image aggregate input, a verified
promotion successor, interval extension rejection, and retired candidate approval.

Final related validation: 241 passing pricing/registry/credits/provider-streaming/
native-search/identity/Work/metering cases, including all 91 pricing cases. Ruff
and MyPy passed for all nine changed Python files. The available repository venv uses Python
3.14.2, rather than the request's `python3.11` spelling; the repository targets
Python 3.12. PostgreSQL concurrency and live-provider/invoice verification are
separate operational gates; this task does not run production writes.

Full `scripts/release_gate.py`: all 826 Python compilation inputs passed;
pytest reported **1 failed, 1,072 passed, 18 skipped**. The unrelated failure is
`tests/test_run_app.py::test_runner_defaults_match_documented_local_urls`:
`run_app.py` hardcodes frontend port 5174, while the test/documentation expect
5173. `git show HEAD:run_app.py` confirms this mismatch already exists on the
fetched `develop` baseline; no runner code was changed. The full gate therefore
remains red and stopped before its smoke step.

The remaining full suite then passed **1,072 cases, 18 skipped, 1 deselected**,
with only that known runner test excluded. This broad run preceded the final
five pricing cases; all final pricing changes were subsequently covered by the
241-case related run. The skipped checks need a live API/provider or isolated
PostgreSQL test database. Production/provider access was disabled in the full
validation process. The smoke step was run separately against a repository-local
temporary SQLite database and passed (`llm_requests=1`, `usage_daily=1`).

Pricing documentation, architecture guidance and Postman descriptions were
synchronized and `git diff --check` passed. The pre-existing runner-port
code/test/documentation mismatch is explicitly recorded above and left outside
this focused pricing change. No React changes require a frontend build or browser
suite for this patch.

## Modified files

- `pricing/models.py`, `pricing/engine.py`: preserve optional dimensions and
  fail explicitly for used unpriced partitions.
- `pricing/service.py`: tier selection, explicit TTL selection and expiry safety.
- `pricing/sources.py`: raw charge evidence, actionable rejection and bounded
  manual JSON input.
- `pricing/sync.py`: retired observations, service review, schedule completeness,
  promotion and approval boundaries.
- `db/pricing_repository.py`: interval validation and extension protection.
- `api/openai_client.py`: actual reported-write lifetime evidence.
- `utils/cost_calculator.py`: expose absent optional rates without inventing them.
- `tests/test_model_rate_cards.py`: regression cases and policy isolation.
- `README.md`, `docs/FASTAPI_README.md`, `docs/runbooks/model-pricing.md`,
  `docs/MODEL_PRICING_ARCHITECTURE.md`,
  `docs/PROJECT_MAP.md`, `.codex/project-context.md`,
  `docs/postman/CortexAI_B2B.postman_collection.json`, and this review: synchronized
  contract/operations/verification documentation. No endpoint/schema or code-path
  ownership changes; the Postman change documents pricing evidence only.

## Financial impact

Legacy rates, product credit multipliers, balances and historical transactions
are unchanged. Existing supported usage produces the same Decimal totals.
OpenAI cache writes already reached the calculator; recording `30m` changes
audit evidence, not its current legacy write rate. In database mode a used
unpriced partition now fails explicitly instead of fabricating a potentially
wrong cost. Correctly approved tier/cache rates may change future provider-cost
calculations and, where applicable, the existing credit floor/cache-aware policy;
approval should compare both expense and credits for representative requests.

Retired observations do not change runtime identity or active prices. Audio
records remain quarantined. Search service fees and customer caps are unchanged;
the Mini search-content question is a cutover blocker, not a confirmed amount to
add. Expired verified cards with no approved successor become unavailable instead
of reverting to outdated seeds. Stored snapshots retain original evidence; this
patch does not retrospectively correct synthesized fields in old cards.

## Production rollout and rollback

1. Review/merge through the normal Git gates when authorized. Release the code
   with `MODEL_PRICING_MODE=legacy` and
   `MODEL_PRICING_ALLOW_AUTO_ACTIVATION=false` unchanged. No new migration.
2. In a staging database, inspect approved history and fresh dry-run observations;
   compare old candidates against the new importer. Never repair approved cards
   in place. Use reviewed new versions for corrected dimensions. Confirm financial
   seed parity, source attribution, exact identities, schedules and service modes.
3. Resolve audio/search-content/provider-mode questions and temporary-price
   validity. Independently verify active-model and service coverage; preflight
   cannot prove every cache/context/audio dimension is available. Exercise expiry
   boundaries, same-source overlap rejection and PostgreSQL rollback/concurrency
   in staging. Approve only with actor, evidence and reason; supply a bounded
   manual card for a verified promotion when source validity is absent.
4. Database cutover is a separate explicit operator approval. Prepare verified
   successors before promotion/calendar expiry, restart all API/workers on any
   flag change, and monitor `PRICING_UNKNOWN`, stale evidence, review reasons,
   selected intervals, component totals and credit/provider-cost reconciliation.
5. Before cutover, rollback is reverting this code patch and restarting with
   legacy mode unchanged. After an independently approved cutover, prefer a
   corrected verified successor card; changing mode to legacy is an explicit
   emergency operator action, with awareness that seed rates may be outdated.
   Never reopen expired cards, invent successor prices, or recalculate ledgers.

## Proposed PR

**Title:** Fix LiteLLM pricing normalization, long-context billing, and synchronization guardrails

**Description:** LiteLLM imports previously synthesized missing cache prices,
inherited optional base prices into long-context tiers, and could propose pricing
for retired identities or extend expired promotional intervals. Preserve missing
versus zero evidence, fail explicitly for used unpriced dimensions, retain retired
records as observations, and enforce schedules and effective intervals across
sync and operator approval. Audio imports remain quarantined with actionable
evidence; search fees remain independent service charges and require review even
when token prices are unchanged. Keep legacy pricing and automatic activation
defaults unchanged; no schema or production data changes.

Validation: 91 pricing regression cases; 241 related passing cases; a broad run
with 1,072 passed, 18 skipped and the one pre-existing runner-port failure excluded;
changed-file Ruff/MyPy; Python compilation; and local SQLite persistence smoke.
The complete release gate remains blocked by that baseline runner test. Database
cutover remains blocked on verified coverage, source/promotion evidence and
operational staging checks. Do not merge or deploy automatically.
