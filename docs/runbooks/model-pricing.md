# Provider rate-card operations

Provider usage, provider USD expense, and customer AI credits have separate authorities.
The registry owns identities, lifecycle, capabilities, access categories and credit
multipliers. PostgreSQL owns provider rates when `MODEL_PRICING_MODE=database`.
LiteLLM is a synchronization source, never an inference gateway or request dependency.

## Initial rollout

1. Keep `MODEL_PRICING_MODE=legacy` (the default) while preparing the database.
2. Apply `db/migrations/20261007_add_versioned_model_rate_cards.sql` with the
   table-owner migration connection, after the existing migrations. It creates
   `model_rate_cards`, `pricing_catalog_observations`, and `pricing_sync_runs`,
   plus nullable `llm_responses.rate_card_id` and
   `usage_calculated_provider_cost_usd numeric(30,15)`. No historical response,
   subscription, or ledger is recalculated or backfilled with an invented card.
3. Seed using an operator connection with INSERT/UPDATE on the pricing tables:

   ```powershell
   venv\Scripts\python.exe scripts/sync_model_pricing.py --seed-legacy --dry-run
   venv\Scripts\python.exe scripts/sync_model_pricing.py --seed-legacy
   venv\Scripts\python.exe scripts/sync_model_pricing.py --inspect
   ```

   Seeding preserves registry effective intervals, cache TTLs and whole-request
   long-context bands. It also seeds native web-search and Managed Agent runtime
   rates as explicit service cards. Repeat seeding does not create duplicates.
   These are known legacy values, not newly verified provider prices.
4. Run `tests/test_model_rate_cards.py` and the pricing/credit/provider/Work tests.
   Include the typed pricing service in the local MyPy check; checking only the
   test with skipped imports does not validate the imported selector/snapshot signatures:

   ```powershell
   venv\Scripts\python.exe -m mypy --explicit-package-bases --follow-imports=skip pricing/service.py tests/test_model_rate_cards.py
   ```

5. Set `MODEL_PRICING_MODE=database` and restart every API/worker. Startup verifies
   the schema and approved pricing for enabled models and required service cards.
   Runtime needs SELECT on pricing tables and normal response/ledger privileges;
   keep pricing mutations restricted to the trusted operator/job connection.
6. Inspect external proposals before activation:

   ```powershell
   venv\Scripts\python.exe scripts/sync_model_pricing.py --dry-run
   venv\Scripts\python.exe scripts/sync_model_pricing.py
   venv\Scripts\python.exe scripts/sync_model_pricing.py --inspect --provider openai
   venv\Scripts\python.exe scripts/sync_model_pricing.py --approve <candidate-uuid> --actor <operator> --reason "Verified provider pricing"
   ```

The CLI uses `DATABASE_URL` and the configured `DB_SCHEMA`.
The CLI loads the repository `.env`; explicit process environment values win.
Seeding a newer registry version may close an old open seed interval at its
declared end and add the new version; approved monetary fields and existing
response/ledger snapshots remain unchanged.
Migration SQL targets
`public`, like existing migrations. Scheduling is external: invoke the command
approximately every six hours through the deployment's scheduler. No startup
fetch or permanent pricing scheduler is installed.

## Approval and history

Cards are selected by provider, exact canonical pricing identity, processing mode
and provider-call start time. Selection priority is approved manual override,
approved synchronized card, then seeded legacy card. Intervals are `[from,to)`.
Approving a new source version closes that source's old interval atomically.
Manual overrides use an independent source layer and survive later syncs.
Removing an override closes its interval; historical requests still resolve it.

Writers serialize with a PostgreSQL transaction advisory lock. Partial unique
indexes prevent duplicate open approved cards and duplicate candidates. The
repository rejects overlapping approved intervals within each source. A trigger
protects approved financial fields and deletion; closing an interval and updating
its verification time are allowed. Rates are immutable once approved.

Sync fingerprints normalized rate dimensions. Unchanged data refreshes verification
without making another approved card. Multiple external IDs mapping to one model
must agree exactly; ambiguous entries are rejected. Unknown models are saved as
observations with no user enablement. A missing external record changes only
`missing_since`, never lifecycle or registry enablement.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MODEL_PRICING_MODE` | `legacy` | Explicit rollout/rollback switch: `legacy` or `database` |
| `MODEL_PRICING_ALLOW_AUTO_ACTIVATION` | `false` | Review candidates by default; true permits validated ordinary changes |
| `MODEL_PRICING_MAX_INCREASE_RATIO` | `2` | Above 2x requires review |
| `MODEL_PRICING_MAX_DECREASE_RATIO` | `0.5` | Below 0.5x requires review |
| `MODEL_PRICING_STALE_HOURS` | `48` | Emit stale evidence while retaining the approved rate |
| `MODEL_PRICING_CACHE_SECONDS` | `60` | Bounded 0–300 second process cache; database remains authoritative |
| `CREDIT_MAX_PROVIDER_USD_PER_MILLION_CREDITS` | `1` | Credit-calibration ceiling in `(0, 1]`; multipliers are floored at applied provider rate / ceiling |

New prices without a known baseline, zero transitions, and changes to long-context
thresholds or charge dimensions require review even with automatic activation.
The fixed LiteLLM HTTPS endpoint has TLS verification, bounded time/16 MiB size,
duplicate-key and JSON validation, and no redirects or caller-controlled URL.
Unknown nonzero standard cost dimensions are rejected for review; standard rates
are not assumed to apply to batch/priority/flex execution.

Identity mapping uses registry names, declared aliases and optional
`pricing_source_ids` for exact external identities. Cortex's `claude`/`grok`
provider names map centrally to LiteLLM `anthropic`/`xai`; no model-prefix guesses
or external fallback regexes are used. Adding an identity mapping is an operator
catalog decision. Adding a new provider/model to the product remains a normal
capability/entitlement change; routine rate updates require no application release.

Provider-returned runtime snapshot IDs must be declared in `aliases`, which both
runtime pricing and external mapping recognize. GPT-5.4 Mini includes the official
`gpt-5.4-mini-2026-03-17` alias; `pricing_source_ids` alone is for source mapping.

## Scheduled DeepSeek pricing

The verified registry versions starting `2026-10-08T00:00:00Z` retain peak token
rates and a `schedule` containing `timezone: UTC`, `peak_weekdays`, half-open
`peak_windows`, `off_peak_tokens`, and an explicit `holiday_calendar` with
`year`, date `ranges`, and `source_url`. Seeding persists the complete schedule
inside immutable `pricing_components`; no additional SQL migration is required.
The shared selector uses provider-call start time, including when cached cards
are reused across a clock boundary. Snapshots retain `pricing_tier` and
`schedule_calendar_year`. Both legacy and database modes use this selection.
DeepSeek tool loops price each model call independently, retaining `provider_calls`
and `rate_application=per_provider_call` in their snapshot. Calls spanning a peak
boundary sum exact components and report `pricing_tier=mixed`; aggregate customer
token multipliers remain the existing policy. Multiple card versions retain their
individual IDs in the call evidence rather than inventing a single rate reference.

Peak hours are 01:00–04:00 and 06:00–10:00 UTC on weekdays excluding Chinese
public holidays. Weekends and holidays are off-peak. The 2026 calendar is verified
against the [State Council notice](https://www.gov.cn/zhengce/content/202511/content_7047090.htm).
Pro peak input/cache-read/output rates are 1.32/0.044/3.96 USD per million;
off-peak rates are 0.66/0.022/1.98. Flash peak rates are 0.30/0.006/1.20 and
off-peak rates are 0.15/0.003/0.60. See [provider pricing](https://api-docs.deepseek.com/quick_start/pricing/).
These are new locally verified versions, not a retroactive repair of old costs.

Before a new calendar year, create and verify a new scheduled registry/card
version with that year's official holiday calendar. Requests outside the
calendar's year fail explicitly; Cortex never guesses holiday dates. Combined
clock/context tiers are currently rejected rather than partially applied.
Peak-only external proposals cannot replace an active scheduled card or be
approved through the CLI. Keep the verified scheduled seed/manual card until
the source supplies a complete schedule. After upgrading this code, rerun
`--seed-legacy --dry-run`, `--seed-legacy`, and `--inspect` before cutover.

## Separate source charges

The importer preserves search-context and Maps charges in observation metadata
`separate_charges`; these do not become model-token unit charges. Proposals with
such evidence require operator review even when ordinary auto-activation is on.
Native search remains charged through its separate service card. Matching
image/input token rates are recorded as `included_input_dimensions` and use
aggregate input tokens once. Different image rates, unhandled audio partitions,
unknown standard charge fields, or contradictory exact identities are rejected.
Do not approve separate-charge proposals before comparing observations with the
applicable service-card rates and provider usage contract.

## Manual override

Prepare an operator-owned JSON file:

```json
{
  "provider": "claude",
  "model": "claude-sonnet-4-6",
  "processing_mode": "standard",
  "rates": {
    "tokens": {"input": "3", "output": "15", "cached_input": "0.3", "cache_write": "3.75", "cache_write_1h": "6"},
    "units": {},
    "bands": []
  }
}
```

Token rates are USD/million; units are USD/unit. Token-only cards have no unit
charges. Supported explicit unit keys are web-search count, request count,
active seconds, and image/audio input/output units. Do not add separate image or
reasoning costs when those resources are already counted in provider tokens.

```powershell
venv\Scripts\python.exe scripts/sync_model_pricing.py --override approved-card.json --actor <operator> --reason "Contract rate" --dry-run
venv\Scripts\python.exe scripts/sync_model_pricing.py --override approved-card.json --actor <operator> --reason "Contract rate"
venv\Scripts\python.exe scripts/sync_model_pricing.py --remove-override --provider claude --model claude-sonnet-4-6 --actor <operator> --reason "Contract ended"
```

Use service identity `__web_search__` with token prices zero and
`units.web_search_count` for search rates. `claude:__managed_runtime__` uses
`units.active_seconds` for Managed Agent runtime. Zero tokens on these service
cards do not imply free model inference. Explicit actor/reason are mandatory for
override creation/removal and candidate approval; each action retains audit data.

## Runtime audit and precision

The shared calculator consumes normalized usage and uses Decimal precision 40,
without rounding individual components. Input includes cache reads/writes;
output includes reasoning, so neither is counted twice. An explicit reasoning
rate replaces the output rate for that subset rather than adding another charge.
Mixed 5-minute/one-hour cache writes retain separate rates when the provider
reports the duration split. Existing provider
adapters remain responsible for their provider-specific usage normalization.
Gemini Interactions normalizes completion tokens as reported output plus thought
tokens in both streaming and nonstreaming paths, retaining the thought subset
separately. The total-token fallback includes thoughts; the calculator and credit
policy consume this normalized output once.
JSON financial evidence is serialized as decimal strings. The existing
`estimated_cost` numeric API field is retained for display compatibility, while
`pricing_snapshot.cost_kind=usage_calculated_provider_cost` identifies provider
usage-based calculations precisely. This is not invoice-reconciled expense.

Snapshots contain `rate_card_id`, pricing source/version, effective interval,
selection reason, normalized usage, component USD costs, calculation version,
and exact usage-calculated total. Responses persist the rate foreign key and
numeric total in addition to their existing snapshot. Decimal database storage
rounds only the total with ROUND_HALF_EVEN to its declared scale; existing credit-ledger cost scale
remains eight decimal places and its snapshot retains the unrounded total.
Model ledger metadata also records credit policy/version and multipliers.
Optimizer and Cortex Analysis reuse the same calculator and snapshot flow.
Historical records retain their original evidence; no current-price recomputation
is required to read new persisted costs.

Search expense counts all provider-reported operations, while customer credits
retain the product's three-operation cap and existing fixed rates. Search ledger
metadata retains its service-card calculation. Work uses token, runtime and search
cards plus the existing Managed Agent reported-USD minimum floor; its ledger
retains each calculation's rate reference. Work runs pin selection to submission
time, and Ask/Compare provider calls pin to provider-call start time.
Work snapshots also retain exact reconstructed, provider-reported and selected
provider-cost totals as decimal strings; the selected total reaches the ledger
without passing through a display float.

Customer credit conversion remains in `server/billing/credit_calculator.py` and
the existing subscription configuration. The October 8 repair calibrates enabled
DeepSeek V4 Flash (including retained chat/reasoner compatibility IDs)
input/output multipliers to 0.5/1.5 and V4 Pro to 1.5/4.0,
version `2026-10-08`, to cover verified peak prices under the existing maximum
USD 1 per million credits policy. This affects future charges only. Search credit
caps, reservation/supplement/release behavior and plan entitlements are preserved.
Cache-aware credit ratios use the effective local provider snapshot, as before.

The credit calculator enforces the same policy at runtime. Each input/output
multiplier is floored at `applied provider rate / ceiling`, using the rates the
request's pricing snapshot actually selected; an explicit reasoning rate counts
toward the output floor. Registry multipliers already meet the ceiling at
standard rates, so the floor raises charges only for requests priced above
them: whole-request long-context bands, or an approved card version with a
higher price. Cache ratios scale from the floored input multiplier. The floor
applies to settlement (both cache-aware and legacy totals), preflight
reservations, response DTO credits and Work. Ledger model metadata records
`effective_input_credit_multiplier`, `effective_output_credit_multiplier`,
`credit_rate_floor_applied` and `credit_max_provider_usd_per_million_credits`.
Set `CREDIT_MAX_PROVIDER_USD_PER_MILLION_CREDITS` below `1` to raise the minimum
margin for every model (for example `0.8333` for roughly 2x at a USD 14.99 /
9,000-credit plan); values outside `(0, 1]` fail API startup.

## Native search service cards

Search credits per operation equal provider USD per operation at the ceiling:
OpenAI/Claude 10,000, Gemini 14,000, Grok 5,000 and DeepSeek 16,000. DeepSeek
search is a Tavily Advanced Search (two Tavily credits at the USD 0.008
pay-as-you-go rate, USD 0.016). Seeded history in
`tools/web/provider_metadata.py` keeps DeepSeek's earlier USD 0.010 card and
adds the USD 0.016 version from `2026-10-09T00:00:00Z`. After upgrading, rerun
`--seed-legacy --dry-run`, `--seed-legacy` and `--inspect`; seeding closes the
old interval without rewriting it. A database that should switch at a different
time can use a manual override instead:

```json
{
  "provider": "deepseek",
  "model": "__web_search__",
  "processing_mode": "standard",
  "rates": {"tokens": {"input": "0", "output": "0"}, "units": {"web_search_count": "0.016"}, "bands": []}
}
```

## Failure behavior and visibility

| Condition | Behavior |
|---|---|
| External source/network failure | Record failed sync and retain approved cards; user requests never fetch external data |
| Unknown model or missing applicable rate | Structured `pricing_unknown_model`/`cost_calculation_failure`; fail calculation explicitly, never assume zero |
| Malformed catalog or conflicting mappings | Reject affected proposal; malformed catalog fails before mutation |
| Suspicious rate | Persist candidate/reason; current approved price continues |
| Database unavailable | A fresh lookup fails; a previously cached approved card may serve only within its TTL; no YAML fallback in database mode |
| Stale pricing | Retain last approved card, mark snapshot `pricing_stale`, emit structured warning |
| Manual override | Wins over sync until deliberately closed |

Inspect cards with `--inspect`; `--provider` and `--model` scope sync/inspection.
`--dry-run` performs no persistent changes. `pricing_sync_runs.summary` includes
seen/changed/added/unchanged/discovered/candidate/invalid/missing counts and reasons.
Structured log events expose these counters for the existing log pipeline;
no separate metrics server, dashboard or admin HTTP route is added.

## Boundaries and rollback

The first external adapter supports direct-provider standard token/cache/reasoning prices
and long-context token bands. Batch, priority/flex, invoice reconciliation, and
external discovery-to-product enablement remain operator/future work. Separate
image/audio unit prices can be manually represented and calculated when normalized
unit usage is supplied; current image-input adapters report token usage. Complex
unsupported external charge dimensions stay in review rather than underbilling.
Search and Managed Agent runtime service-card changes currently use manual overrides.

Rollback uses `MODEL_PRICING_MODE=legacy` and an API/worker restart. Retain all
rate cards, observations, sync runs and request evidence. The default legacy mode
keeps the original registry pricing/fallback available during the staged rollout;
the database path requires migration and seeding, and is the target runtime mode.
