# Provider pricing architecture assessment

The pre-refactor runtime already centralized effective-dated standard, cache,
long-context and lifecycle-aware pricing in `config/model_registry.yaml` through
`config/pricing.py`. `utils/cost_calculator.py` selected those rules and calculated
floats. OpenAI, Claude, Gemini, Grok and DeepSeek adapters normalized usage into
`TokenUsage`; BaseAIClient propagated model identity/rule evidence to UnifiedResponse.
Response snapshots and the immutable credit ledger already retained price evidence.

Customer AI credits use model input/output multipliers and independent fixed tool
charges, rather than a universal provider-dollar conversion. Existing reservation,
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
