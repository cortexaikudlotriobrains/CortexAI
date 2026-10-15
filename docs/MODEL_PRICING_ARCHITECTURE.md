# Provider pricing architecture assessment

The pre-refactor runtime already centralized effective-dated standard, cache,
long-context and lifecycle-aware pricing in `config/model_registry.yaml` through
`config/pricing.py`. `utils/cost_calculator.py` selected those rules and calculated
floats. OpenAI, Claude, Gemini, Grok and DeepSeek adapters normalized usage into
`TokenUsage`; BaseAIClient propagated model identity/rule evidence to UnifiedResponse.
Response snapshots and the immutable credit ledger already retained price evidence.

Customer AI credits use model input/output multipliers and independent fixed tool
charges, rather than a universal provider-dollar conversion. A runtime floor keeps
each multiplier at or above the applied provider rate divided by the credit
calibration ceiling (USD 1 per million raw credits by default), so long-context
bands and newly approved higher rates cannot drop below the calibration policy. Existing reservation,
supplementation, partial Compare settlement and release belong to billing services.
Cache ratios are the intentional seam between provider rates and input-credit
policy. Managed Agents have a separate cumulative usage/reported-cost floor.
The refactor preserves these choices instead of changing subscription economics.

The principal debt was release-coupled rates, float provider calculations, duplicated
Work token expense formulas, fixed search/runtime expense constants, and absence of
persistent source sync, immutable database rate versions and operator overrides.
The deployment already has operational scripts and background reservation/Work
workers; pricing uses an external scheduled CLI rather than adding startup network I/O.

The resulting ownership is:

| Responsibility | Components |
|---|---|
| Product identity, lifecycle, enablement, credits | Existing registry/subscription modules; `pricing/identity.py` maps exact external IDs |
| Rollout/validation policy | `config/model_pricing.py` |
| Normalized usage/rate validation and precision | `pricing/models.py`, `pricing/engine.py` |
| Persisted versions, observations, sync audit | `db/pricing_repository.py`, `20261007_add_versioned_model_rate_cards.sql` |
| Historical local selection and bounded caching | `pricing/service.py`, compatibility facade `config/pricing.py` |
| UTC peak/off-peak selection and verified calendar validation | `pricing/schedules.py`; immutable schedule JSON in registry/card rates |
| External/manual source parsing | `pricing/sources.py` |
| Seed, sync, candidate approval, manual overrides | `pricing/sync.py`, `scripts/sync_model_pricing.py` |
| Request and ledger calculation evidence | Existing BaseAIClient, response repository, billing settlement, Work service |

An Ask request reserves the existing worst-case customer credits, calls its selected
provider directly, normalizes usage, selects the local card applicable at call start,
calculates Decimal USD components, and returns the compatibility amount plus exact
snapshot. Billing applies its credit policy, settles the authorized reservation and
persists ledger evidence. The response persists the snapshot, rate foreign key and
precise provider total. Compare, optimizer and Cortex Analysis share these seams;
Work preserves its independent cumulative/provider-floor contract.

Sync fetches bounded HTTPS JSON outside any database transaction, maps exact
identities, validates complete rates, compares fingerprints, and locks activation.
Unchanged cards refresh verification; ordinary approved changes create a new interval;
suspicious/default-review proposals become candidates. Discovery and missing-source
observations never alter user enablement or lifecycle. Manual cards occupy a separate
priority layer. Approved monetary history cannot be overwritten or deleted.

Imported optional cache prices retain absence versus explicit zero, and context
tiers require explicit optional partitions. Retired source identities are
observation-only; unsupported audio partitions retain actionable source evidence.
Search service evidence requires review even when token fingerprints are unchanged.
Approved verified expiry cannot resurrect legacy seeds or be extended by flat
source ingestion. Operator JSON uses the existing effective interval columns for
bounded promotions, and schedule completeness applies across every approval path.
Known promotional identities have registry `pricing_review.requires_expiry`
metadata. Approval requires a verified exclusive end, including older candidates,
and the operator CLI validates approval dry-runs. Optional provider-verified
`pricing_review.effective_until` metadata sets the latest allowed exclusive end
for both candidate approval and manual overrides in `pricing/sync.py`.
For Gemini 3.6 Flash, Google's December 31, 2026 boundary is stored as
`2027-01-01T00:00:00Z`. It leaves unbounded imports pending without creating a
future successor card or changing legacy rates. The boundary limits only
intervals that start before it: a regular-price successor that starts at or after
it may be open-ended (manual override from the boundary, or a candidate approved
after it). `select_card` also accepts a legacy seed that starts once every started
verified card has ended, which is how a registry-declared regular price (Gemini
3.8 Flash, Gemini 4 Argon) follows an approved launch card; legacy seeds that
predate a verified card are still never revived. Financial fingerprints and
approved history remain immutable. OpenAI cache-write TTL selection follows the
served model: GPT-5.6 and later use `30m`; older models retain only their supported
retention controls and unexpected write counts fail explicitly. CI and local
pre-push enforce the complete rate-card regression suite.
See [the correctness review and verification gates](LITELLM_PRICING_GUARDRAILS.md).

The rollout is schema → legacy seed → parity tests → database runtime switch →
external proposals/review → optional conservative auto-activation. Legacy registry
rates remain the explicit rollback/bootstrap source; database mode does not silently
consult YAML when PostgreSQL or a model's approved pricing is unavailable.

See [operational commands, configuration, audit fields, failure behavior and current
limits](runbooks/model-pricing.md). Tests cover every enabled model against the old
formula and representative standard/long-context/cache-TTL usage before cutover.

The follow-up repairs declare the official Mini snapshot alias, normalize Gemini
Interactions answer plus thought output in the common finalization path, and add
DeepSeek price versions with UTC weekday windows and a verified holiday calendar.
Schedule selection happens per call from cached immutable cards, not when the
cache is filled. Annual coverage is explicit. Upgrading seeds closes only the
old declared interval and adds the next version without changing financial history.
Separate source search/Maps prices are retained for service-card review; equal
image/input rates use aggregate tokens once, and unsupported partitions remain
rejected. Peak-only proposals cannot displace complete scheduled pricing.

## Initial implementation validation (2026-10-07)

- All 34 enabled registry models matched the legacy provider-cost formula within
  $0.000000000001 for standard and long-context input, cache reads, and both
  cache-write TTLs. These are configured-rate fixtures, not invoice reconciliation.
- The dedicated pricing tests cover normalization, precision, historical
  selection, safe unknown rates, manual overrides, idempotence, quarantine,
  discovery, source failure, bounded caching, freshness and response persistence.
  Final focused pricing/billing/provider/Work validation: 234 tests passed,
  including 42 pricing unit/integration tests and two real PostgreSQL tests.
- Isolated PostgreSQL 17 tests applied the migration twice, seeded twice,
  verified immutable approved history, and synchronized two concurrent writers.
- Final full backend regression: 1,007 passed, 15 skipped, one existing failure in
  `test_run_app.py::test_runner_defaults_match_documented_local_urls` (test
  expects port 5173; the existing runner defaults to 5174). This blocks a clean
  full release gate. Live provider and separate billing-PostgreSQL tests remain
  skipped without their required environment.
- API persistence smoke and React production build passed. Ruff and changed-file
  MyPy passed. No subscription configuration, frontend behavior or HTTP endpoint
  changed; README, API/Postman, project map, native-search/Work guides, migration
  operations and core project context were reviewed and synchronized.
- A read-only live LiteLLM fetch found 238 direct-provider entries, 40 exact
  mapped IDs, 14 supported mapped entries and 26 rejected mapped entries with
  additional search, image or audio charge dimensions. Rejected entries retain
  approved local rates and require audited manual pricing until their dimensions
  are supported; multiple IDs for one canonical model must all validate and agree.
  Catalog SHA-256: `c274b2a1bf9b686a206c1467d7cf2f02b12e13bfef2c1bd92e7ffe798c701a6b`.

## Repair validation and local rollout (2026-10-08 UTC)

- Final backend regression: 1,030 passed, 15 skipped, one pre-existing frontend
  port assertion failure (5173 expected versus the runner's 5174 default).
- Three real PostgreSQL tests verify repeatable migration/seeding, immutable
  financial history with seed interval closure, and concurrent synchronization.
- Mini identity, Gemini thinking-token totals, per-call DeepSeek tier transitions,
  registry/database parity at historical and current times, and updated credit
  settlement/Compare totals are covered by the suite. DeepSeek multipliers now
  cover peak expense under the existing calibration policy; credit versions and
  historical ledger records remain auditable.
- Ruff, compilation and scoped MyPy (12 pricing source files, dependency imports
  silent) pass. An expanded MyPy dependency traversal also exposes an existing
  `Result.rowcount` annotation error in `db/billing_repository.py:1066`.
- The user's local PostgreSQL database contains 38 approved registry seed cards,
  including two new scheduled DeepSeek versions; the two preceding intervals
  close at `2026-10-08T00:00:00Z`. There are 29 unapproved LiteLLM candidates.
  The repaired import observed 238 source entries, 195 discoveries, 27 candidate
  proposals and five rejected model groups; it activated no external prices.
- Startup pricing readiness and database calculations passed. Checksums before
  and after repairs confirm all 1,392 response rows, 847 credit transactions and
  existing approved financial fields remain unchanged. Mini's 1,000 input plus
  500 output example costs USD 0.003; Pro's one million input plus one million
  output costs USD 5.28 peak or USD 2.64 off-peak.
- README, API/Postman documentation, pricing/native-search guides, operations and
  core project context are synchronized. No HTTP request field or endpoint changed.

The local `.env` already selects database pricing. Restart API/workers to load
the adapter, identity and credit-configuration repairs. Production deployment
and scheduler installation remain separate operations; no historical charge
was rewritten or external candidate approved during these repairs.

## Credit floor and DeepSeek search repricing (2026-10-09 UTC)

- `server/billing/credit_calculator.py` floors input/output credit multipliers at
  the provider rates selected by each request's pricing snapshot, divided by
  `CREDIT_MAX_PROVIDER_USD_PER_MILLION_CREDITS` (default `1`, valid `(0, 1]`).
  Settlement (cache-aware and legacy totals), reservations, DTO credits and Work
  share the calculator; ledger metadata records the effective multipliers.
  Example: a 250K-token Gemini 3.1 Pro Preview prompt previously charged its 3/14
  registry multipliers against the USD 4/18 long-context band (about 1.25x at a
  USD 14.99 / 9,000-credit plan); it now charges 4/18 (about 1.67x). A 300K-token
  GPT-5.6 Sol prompt moves from 6/30 against USD 10/45 (about 1.0x) to 10/45.
- DeepSeek's Tavily-backed search remains Advanced Search and is priced at its
  real pay-as-you-go cost: two Tavily credits at USD 0.008 (USD 0.016), charged at
  16,000 raw credits per operation. The legacy research conversion is 8,000 raw
  credits per Tavily credit. Seed history closes the earlier USD 0.010 DeepSeek
  search card at `2026-10-09T00:00:00Z` and adds the new version.
- Validation: Ruff and changed-file MyPy pass; the backend suite passes
  (1,047 passed, 18 skipped) apart from the pre-existing frontend port assertion in
  `test_run_app.py` (5173 expected versus the runner's 5174 default).

## Database-mode registry incident and fix (2026-10-13 UTC)

- Production enabled `MODEL_PRICING_MODE=database`. `/health` and startup readiness
  passed, but `ModelRegistry.from_yaml()` raised `KeyError: 'cache_write'`: the
  approved LiteLLM card for `gemini-3.6-flash` has no cache-write rate, and
  `orchestrator/model_registry.py` read `cached_input`/`cache_write` from every
  snapshot by index. Database snapshots carry only the dimensions on the card
  (legacy snapshots always filled them), so legacy mode never exposed this.
- Every route that builds the registry failed: `/v1/model-options`, `/v1/providers`,
  `/v1/history` (credit usage per row), Ask and Compare. React fell back to its
  bundled five-model catalog (OpenAI, Claude, DeepSeek only) and kept its last
  loaded history because history load errors are not shown. Rolling back to
  legacy mode restored service.
- Fix: the registry reads optional rates with `_optional_rate`, so a missing
  dimension is `None` in `ModelCandidate` and `null` in the catalog DTO; an
  explicit zero stays `0.0`. Input/output remain required. Billing is unchanged:
  `pricing/engine.py` still raises `PRICING_UNKNOWN` when usage needs a missing
  rate, and database mode still has no conservative or legacy fallback.
- `pricing/service.py::validate_readiness` now also builds the registry, so this
  class of failure stops startup instead of degrading every provider.
  `scripts/preflight_database_pricing.py` runs the same checks read-only from a
  separate process before any mode change; see the runbook.
- Remaining exposure: an enabled model with no applicable approved card still
  prevents the registry from building. The GPT-5.6 Sol card ends
  `2026-11-21T00:00:00Z` and the Gemini 3.6 Flash card ends
  `2027-01-01T00:00:00Z`; approve reviewed successors before then.
- Validation: `tests/test_database_pricing_model_registry.py` (22 cases) seeds
  every enabled model plus the four approved production LiteLLM cards in an
  isolated SQLite store and fails with the production `KeyError` on the old code.
  Pricing suites: 156 passed. Full backend suite: 1,067 passed, 3 skipped, with
  the same 10 failures as unmodified `develop` (nine order-dependent
  `test_routing_regression.py` cases from shared circuit-breaker state and the
  runner port assertion).
