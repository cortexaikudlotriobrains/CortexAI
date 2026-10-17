# Analyze design with the original Compare response display

Date: 16 October 2026. Branch: `Compare-mode-refactor`.
Default-tab and source-citation follow-ups: 17 October 2026.

The supplied Analyze handoff informed the analysis workflow. The latest user
instruction adds the mockup's source-specific **Cited for** passages and
**View the model’s answer** navigation. Cortex Analysis remains the default on
entry/re-entry to Analyze, retaining its live loading views. The contributor/status/
credits strip above the tabs and Models + analysis composer footer stay removed.
The original model-response display, approved Cortex Analysis layouts and application theme
remain intact.
Literal HTML shell/theme replacements remain removed. No commit, merge,
deployment or production data mutation was made.

## Final presentation

- Sources now follows the supplied source-card screenshots. Each card shows its
  publisher/domain, provider identities, original-source link and **Cited for**
  excerpts when explicit `[n]` or matching Markdown/reference links establish
  the relationship. The previous renderer only listed source metadata and model
  attribution; the mockup's `usedFor` copy was hardcoded and was never connected
  to real response passages. The new Markdown parser derives actual excerpts
  from existing response text and per-revision source ordering. Code, bibliography
  entries, unsafe URLs and uncited metadata cannot create claim support.
  Navigation opens/highlights that immutable model revision and offers **Back
  to Sources**. Missing old text never falls through to a newer response. No extra
  model/search call, API/schema change, migration or history rewrite is involved.
- While answers arrive, Analysis shows provider-logo rows for queued, search/tool,
  writing, complete/partial and failed/stopped responses, with live announcements,
  animated spinners, response navigation and cost-estimated model retry actions.
  The synthesis view shows the reference's three connected steps. Persisted
  `progressStage` advances them at actual preparation/provider/validation
  milestones; final-step copy announces quote checks before saving. No mockup
  timers or fabricated percentages are shipped. Reduced-motion users see static
  indicators. A saved analysis stays readable during an update or its failure.
- Original Manrope/Space Grotesk typography, light/dark shell palette, brand
  images and provider logos remain in use. Global theme CSS, ProviderLogo,
  model pickers, sidebar/ChatPage CSS and ResponseCard CSS match original Git
  HEAD. CortexIcon adds layers, sparkles, split and book-open glyphs without
  replacing the original glyphs.
- Sidebar collapse preferences, the existing 900px mobile shell, mobile history,
  account menu and centered/docked composer keep their original behavior. Empty
  Analyze says `Hi, what would you like to analyze?`. Existing composer controls
  remain visible, without the Models + analysis estimate footer. Submitted
  questions keep the original shared user-message bubble.
- Analyze-only result layouts use CSS modules and existing Cortex theme tokens,
  plus the screenshot's exact scoped lavender takeaway fill (`#f1eefc` light /
  `#2d2845` dark). Model bylines, quotation headers and source
  attributions reuse proper provider icons. The Cortex Analysis tab and takeaway
  use layers; the synthesis header uses sparkles.
- Optional answer headline/introduction lead into the takeaway. Titled
  consequential differences follow with **See both passages**, then **In their
  words** quotations with model/revision headers. Quotes sit side by side where
  readable and stack on narrow screens. The Sources link/count follows them.
  Analysis, Model responses and Sources remain accessible. Confidence/voting/
  forced-uniqueness reports remain absent.
- Cortex Analysis is the default view on entry/re-entry to Analyze. Model responses retains its original layout and selected model. `CompareResponses.tsx` restores the
  original response UI using unchanged `ResultsSection.module.css`: equal-height
  desktop cards side by side, three models sharing one row above 1180px,
  two-column wrapping at 901–1180px and stacked tall cards at 761–900px.
  Desktop/tablet card bodies scroll independently. At 760px and below, the
  original horizontal provider tabs switch one natural-height answer and stay
  sticky during reading. Keyboard control, model selection, copy, regeneration,
  retry, metadata, local feedback and code/table containment remain intact.
  The replacement response dropdown and response-specific CSS overrides were
  removed. The Cortex Analysis design remains in its separate view.
- Sources distinguishes attached HTTP(S) metadata from model passages. It
  preserves query parameters and attribution, never invents claim-to-page
  mappings and honestly shows **No linked sources supplied** when appropriate.
- Evidence opens its saved full revision and can return to the latest response.
  Regeneration marks prior analysis stale. Cost-disclosed Update keeps the
  previous run readable during work or failure. View changes purchase no work.

## Preserved functional implementation

Analyze is a display label; internal `compare` routes, identities and legacy
history remain compatible. Browser Send requests automatic initial synthesis
and checks its combined estimate for authorization without a composer footer.
Both stages are checked and reserved before
model answers start. Owned durable intents and atomic claims authorize synthesis
when at least two saved answers succeed. Navigation does not cancel the
server-owned producer; explicit Stop uses an owned cancellation endpoint.

Cortex makes one configured structured OpenAI call with search off and a default
1,800-token output ceiling. Actual usage is settled even if required output fails
validation; no automatic paid repair call follows. Quotes validate immutable
response identity, revision and whitespace-normalized text. Bounded owned context
is stored and included in reuse fingerprints. Optional nullable v2 fields
`headline`, `introduction` and `differences[].title` support the requested result
design. Old runs render without them and are never rewritten or silently
regenerated. Policy `cortex-analysis-v2-editorial` uses the same single call and
output ceiling. New overview prose receives the existing known-source URL
checks and anonymous-label restoration. Evidence identity/revision validation
remains unchanged.

## Validation

| Check | Result |
| --- | --- |
| Source-citation frontend regression suite | **448 passed**, 55 files, including source-card actions and citation/revision parsing cases |
| Source cards and retained evidence browser flows | **12 scenarios validated**: six source-specific scenarios passed; six retained evidence scenarios passed after making the Model passages heading selector exact for the new combined heading |
| Source-citation production build, lint and lockfile | Build/lint passed; existing Markdown parser dependencies explicitly declared and offline lockfile validation passed |
| 17 October commit follow-up: persistence, synthesis and analysis suites | **63 passed**, with no deselections; shared-session streaming test uses an explicit 128-token output limit after the research charge |
| Cortex Analysis default and re-entry behavior | **24 responsive browser checks passed**: 11 desktop/tablet and 13 mobile; retained model grids/tabs, tables, actions and live Analysis loading |
| Default-tab frontend unit suite | **432 passed**, 53 files; workspace mode isolated after the re-entry regression check |
| Default-tab production build and lint | Passed; frontend-only behavior, with no API/configuration/operational contract change |
| Live-progress frontend unit suite | **432 passed**, 53 files |
| Live-progress backend evidence/status/lifecycle subset | **40 passed**, including persisted stage reads and no duplicate paid calls |
| Live-progress responsive browser checks | **12 passed**: retained result/navigation checks plus live ledger and all server-stage transitions at 320, 390, 768, 1024, 1440px and phone landscape; light/dark and reduced motion |
| Final accessibility/link-spacing refinement | **28 focused UI tests passed**; all **6 live-progress browser checks passed again** |
| Live-progress production build, lint, Ruff and CI-style MyPy | Passed; MyPy checked 6 source files |
| Earlier two-row removal: composer, responses and analysis UI tests | **41 passed**, 3 files |
| Earlier two-row removal: build, lint and responsive browser checks | Build/lint passed; **6 browser scenarios passed** |
| Frontend unit suite after restoring the original response display | **429 passed**, 53 files |
| Focused overview/difference/quotation UI subset | **25 passed** |
| Frontend build and lint | Passed; existing build warnings remain |
| Original response-layout and retained Analyze browser checks | **18 scenarios validated**: desktop/tablet 8 passed; mobile 9 passed initially and the sticky-header check passed on its focused rerun |
| Focused screenshot browser checks | **6 passed**, desktop/tablet/phones/landscape with light/dark color and shell parity assertions |
| Earlier screenshot-version desktop/iPad scenarios | **60 validated** before response restoration; latest relevant checks are listed above |
| Earlier screenshot-version mobile scenarios | **44 validated** before response restoration; latest relevant checks are listed above |
| Final synthesis, Cortex Analysis and generation-policy tests | **68 passed**, including optional-field compatibility, URL checks and anonymous-label restoration |
| Earlier backend persistence/schema subset | **60 passed**, one known baseline failure deselected |
| Earlier generation-policy and synthesis tests | **59 passed**, including all six fixture passage sets |
| Ruff / CI-style MyPy checks | Earlier 19-file check passed; four files changed for the screenshot presentation rechecked successfully |
| Earlier full release gate | Compile passed; pytest **1,154 passed, 18 skipped, 4 failed** |
| Isolated SQLite database smoke | Passed with provider doubles |
| Live PostgreSQL/provider E2E | Pending unapplied migration |
| Live semantic evaluation | Not run; external disclosure approval was rejected |

Responsive Analyze checks compare its computed fonts, theme tokens, brand assets,
sidebar width and mobile-navigation geometry with Ask. They also verify the exact
light/dark takeaway fill, proper provider-logo elements, side-by-side/stacked
quotations, difference disclosure/context targets, feedback, keyboard tabs and
absence of paid work on view changes. Screenshots use the actual React app with
isolated API fixtures, not production sessions or live answers. The earlier
`analyze-redesign` and `cortex-theme-restored` galleries predate this screenshot
integration. The earlier `analyze-screenshot-design` gallery has 18 captures:
overview light/dark and difference/quotation detail at 320, 390, 768, 1024 and
1440px plus 844px phone landscape.

The broad browser run had four 30-second startup timeouts and one mobile toolbar
assertion that mixed bounding boxes from different frames while the estimate
resized the sheet. The toolbar assertion now captures all control positions in
one browser snapshot, retaining its one-pixel tolerance. Targeted reruns use
one worker per suite and a 60-second setup budget; result-only screenshots are
recaptured with the exact reference SVG paths.
All **104 distinct responsive scenarios** passed for the earlier screenshot
version across broad runs and targeted reruns. For the latest restoration, the
original desktop/tablet card-layout and mobile-provider-tab acceptance tests
were restored from Git and run with the retained Analyze evidence/action checks.
No product shell/composer styles were changed to satisfy these checks.

The response restoration keeps the original sticky-provider-tab test, measuring
scroll distance relative to the actual provider row instead of its former fixed
260px header offset. The targeted rerun passed. The response-restoration
`compare-responses-restored` gallery includes 13 actual React captures: the
three-model desktop row and light/dark response views at 320, 390, 768, 1024,
1440 and 844px landscape. API fixtures supply the content; no live provider calls
or database changes were made for this restoration.

The full release gate ran before theme restoration. Its four failures were
reproduced independently with the original HEAD Compare route loaded; pricing
and runner code is unchanged by this task:

1. `test_stream_chat_and_compare_share_one_session_id_in_done_events`: existing
   Free/search allowance enforcement returns `402`, expected `200`.
2. `test_every_enabled_model_is_calibrated_below_one_dollar_per_million_credits`:
   Gemini 2.5 Flash Lite rate/multiplier is `0.3 / 0.25 > 1`.
3. `test_cost_calculator_cumulative_tracking_is_consistent`: `0.45` actual versus
   `0.15` expected.
4. `test_runner_defaults_match_documented_local_urls`: local port `5174` versus
   expected `5173`.

The 17 October commit follow-up resolved the first failure in the test setup.
The research response used 16,007 of the Free account's 100,000 credits; the
following Compare request's default output reservation required 84,618 with
83,993 remaining. Its `402 insufficient_credits` was correct. The session/history
regression now requests an explicit 128-token output limit, retains real credit
reservation/settlement and all existing session, history and charge assertions,
and includes the response body in a failed status assertion. All 63 persistence,
synthesis and analysis tests pass without exclusions. This correction changes no
runtime, UI, pricing, API, Postman or migration contract; the remaining historical
release-gate failures were not re-evaluated in this follow-up.

## Runtime and evaluation limits

**No migration was applied by this implementation session.**
`db/migrations/20261016_add_compare_synthesis.sql` adds JSONB result/context
snapshots and `compare_analysis_intents`, without rewriting legacy results.
The 17 October startup follow-up verified that the local database now has the
initial synthesis objects but lacks `public.compare_analysis_intents.progress_stage`.
The runtime role `cortex` cannot alter this `postgres`-owned table, and no
`MIGRATION_DATABASE_URL` owner connection is configured. Startup therefore still
fails schema preflight. The synthesis migration's idempotent ALTER adds this
nullable progress column when run by the table owner.
Use the owner-operated [migration runbook](runbooks/db-migrations.md) before a
staging API restart and live validation. No database was changed during restoration.

The six supplied cases in `tests/fixtures/cortex_analysis_cases.json` pass exact
passage/revision resolution tests: `athletes_daughters`, `fast_remittance`,
`ai_investment_circularity`, `newborn_weight_gain`, `wwii_lend_lease_follow_up`
and `tennis_regional_representation`. Deterministic checks also cover attribution,
partial answers, context bounds, unsafe links, invalid-output settlement,
navigation-safe execution, ownership, history and action targets.

These tests do not prove semantic model quality or independently verify supplied
claims. Automatic approval review rejected sending the six cases and prior context
to the external OpenAI integration because the payload and destination lacked
explicit authorization. Those calls were not executed or retried elsewhere.

README, FastAPI docs, Postman, the Compare guide, project map and Codex project
context describe the screenshot result layout and optional v2 fields. Existing
routes, environment settings and the operator-owned migration workflow are
unchanged. The migration runbook still applies; no extra schema migration is
needed for nullable fields inside the existing JSONB snapshot. Documentation
review found no known drift.

The preceding two-row removal also synchronized README, FastAPI docs, the Compare
guide, project map and Codex context. The browser checks assert that the
above-tab contributor row and composer estimate footer are absent, while
quotation provider icons and response navigation remain intact. Current
captures are in `.runtime/remove-analyze-rows-desktop` and
`.runtime/remove-analyze-rows-mobile`; the published galleries predate this
removal. Combined-estimate requests, Send authorization and server billing
remain unchanged. Postman, backend contracts and operational runbooks are
unaffected by this frontend-only removal. No known documentation drift remains.

The live-progress implementation synchronizes README, FastAPI and Compare guides,
the Postman status example, project map, migration runbook and Codex context. Its
only API addition is nullable `progressStage` on owned status reads. The synthesis
migration includes that intent column; no migration was applied by this session.
Final live-progress screenshots are captured in
`.runtime/analyze-progress-desktop-final` and `.runtime/analyze-progress-mobile-final`.
The `analyze-live-progress` visualization gallery includes 18 actual React
captures: model waiting in light, and synthesis in light/dark, at all six sizes.
No known documentation drift remains.

The source-citation update synchronizes README, FastAPI/Compare guides, project
map, E2E README and Codex context. Existing title/URL arrays, API/Postman fields,
billing and migration contracts remain unchanged. A first parallel desktop run
hit a Windows Vite dependency-cache rename conflict before app readiness;
responsive servers now use isolated `.runtime/e2e-vite-cache/<port>` directories.
Both suites then exercised the source cards successfully. Final source captures
are in `.runtime/analysis-source-review-desktop` and
`.runtime/analysis-source-review-mobile`; the six retained evidence checks passed
in `.runtime/analysis-source-retained-desktop` / `analysis-source-retained-mobile`.
The `analyze-source-citations` gallery contains 12 current React captures at
320, 390, 768, 1024, 1440px and 844px phone landscape, in light/dark themes.
No database changes or live provider calls were made. No known documentation
drift remains.
