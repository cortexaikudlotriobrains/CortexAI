# Model updates: add, hide and retire models

This is the standard way to change which models CortexAI offers. It covers new
provider models, their AI-credit price and subscription plans, replacing older
models, and retiring models. Provider *prices* for models we already have are a
separate flow: see [provider rate-card operations](model-pricing.md).

The short version:

1. Every Monday the **Model catalog** workflow compares LiteLLM's price catalog
   with `config/model_registry.yaml` and opens a report issue (label
   `model-catalog`) when something changed.
2. A reviewer ticks the models to add in the issue and corrects the proposal
   YAML if needed.
3. Ticking starts **Apply model updates**, which waits for approval in the
   `model-updates` environment. After approval it edits the registry, providers
   and React defaults and runs the pricing checks that must pass: re-seeding
   over `develop`'s price cards (what the production deploy does), the rate-card
   and the credit tests. If they pass it opens a pull request to `develop`; if
   anything fails it opens none and comments on the issue with the run link.
4. A person finishes the pull request (provider code, tests that list the
   current models, docs), merges it, and deploys with Production Deploy.

Locally, `python scripts/models.py` does the same steps.

## Rules

- **Price source.** New models are compared and priced with LiteLLM's current
  catalog only, including launch promotions while they last (decided
  2026-10-14). The registry's own prices are not used for comparisons.
- **Credits come from the credit ladder** (`config/credit_ladder.yaml`). A model
  takes the cheapest step whose input and output credits per token are at or
  above LiteLLM's standard USD per 1M tokens. The step also sets the billing
  class and the picker label:

  | Step | Credits in/out per token | Class | Plans | Label |
  |---|---|---|---|---|
  | E1 | 0.25 / 1 | economical | Free, Plus, Pro | Very low |
  | E2 | 0.5 / 2 | economical | Free, Plus, Pro | Very low |
  | S1 | 1 / 4 | standard | Free, Plus, Pro | Standard |
  | S2 | 2 / 8 | standard | Free, Plus, Pro | Standard |
  | A1 | 3 / 14 | advanced | Plus, Pro | High |
  | A2 | 4 / 18 | advanced | Plus, Pro | High |
  | P1 | 6 / 30 | premium | Pro | Premium |
  | P2 | 10 / 50 | premium | Pro | Premium |

  Plans come from `config/subscription_plans.yaml`; never list plans per model.
  A model priced above P2 is reported separately: add a step first.
- **Long context and ended promotions** are covered at runtime: the credit
  calculator raises a multiplier to the provider rate a request actually used
  (`floor_credit_multipliers`, USD 1 per million credits by default), and the
  reservation estimate uses the long-context band for the estimated prompt.
  `tests/test_new_model_credits.py` pins the charges.
- **Promotions** get two dated registry rules (launch price until the provider's
  end date, then the regular price) plus `pricing_review.requires_expiry`. In
  database mode the regular-price seed starts at the boundary, so a card stays in
  force when the promotion ends, also after a LiteLLM launch card is approved. A
  reviewed open-ended successor can replace it: a manual override starting at the
  boundary, or a LiteLLM candidate approved after it.
- **Replace means hide, not delete.** The replaced model gets `selectable: false`
  and `lifecycle.replacement_model`; its status stays `ACTIVE` while the provider
  serves it. It keeps working and billing for existing threads, API callers and
  history. A replacement is the same provider and the same or a cheaper class.
- **Defaults move only off hidden models**: `ui_defaults`, `routing_defaults`
  (including the smart coding order) and `config/providers.yaml`.
- **IDs are never removed or reused.** `config/known_model_ids.txt` lists every
  name and alias the registry has had; `check` fails if one disappears.
- **Retiring** (provider shutdown): mark `SCHEDULED_FOR_RETIREMENT` with
  `retirement_date`, then `RETIRED` with a `replacement_model` once the provider
  stops serving it. A retired ID stays as a tombstone that redirects to its
  replacement (for example `deepseek-v4-flash` -> `deepseek-flash`). Use
  `DEPRECATED` without a date when the provider serves a model only to existing
  users and announced no shutdown (Gemini 2.5).

## The weekly report issue

The report lists:

- **New models**: one checkbox per proposal with the model it replaces, the
  change in cost of a typical turn (1,000 input + 500 output tokens), the
  credits, class and plans, followed by a YAML block with every field.
- Models priced above the top credit step, and LiteLLM entries our parser rejects.
- Provider deprecation dates for models in the registry.
- Selectable models whose credits are below LiteLLM's price (the runtime floor
  is charging; reprice on purpose).
- Registry check errors and warnings (for example a promotion ending within 45
  days, or credits that predate the ladder). A new or cleared warning counts as a
  change and opens a new issue.

A new issue opens only when these findings change; otherwise last week's issue
and its ticks stay. Older report issues are closed as superseded.

Not proposed: dated snapshots, preview/beta/experimental and non-chat variants,
models not newer than the newest one of their family in the app, older families,
and IDs in `config/model_catalog_ignore.yaml`. Add an ID there, with a reason and
a source, only when a proposal is wrong for good (for example Grok 4.20, which
sorts above Grok 4.7 but is older than Grok 4.3). Unticked proposals come back
every week until they are added or ignored.

### Reviewing a proposal

Check and edit in the YAML block before ticking:

| Field | What to check |
|---|---|
| `replaces` | The model it takes over in the picker, by position in the family (for example GPT-6 Luna replaces GPT-5.6 Luna). `null` adds without hiding. Add `also_replaces: [provider:model, ...]` to hide more. A model may only replace one in the same or a dearer class; the report proposes a dearer newcomer alongside its family instead (GPT-5.5 next to GPT-5.4), and `check` rejects the opposite. |
| `based_on` | The same-provider model whose capabilities it inherits (reasoning, tags, attachment settings) and next to which it is inserted, independent of hiding. The report sets it to the newest family member even when `replaces` is `null`; without it, `replaces` is used. |
| `display_name`, `description` | Generated from the predecessor; rewrite when the model is positioned differently. |
| `tier` | Smart-routing tier `T0`-`T3`. Independent of credits and plans. |
| `credits` | Must be a ladder step. Leave it unless you are deliberately pricing above the covering step. |
| `context_limit` | Use the provider's documented window. OpenAI's registry limit is the whole window (LiteLLM's `max_input_tokens` plus output); the report converts it from the predecessor. |
| `max_output_tokens` | The new model's own LiteLLM limit, even when lower than the predecessor's. A LiteLLM value equal to the context window (xAI rows) is not trusted; the predecessor's is kept and a note asks you to check. |
| `attachments` | `supports_image_input`, `supported_attachment_mime_types`, `max_attachment_bytes`, `max_attachments_per_request`. Taken from the predecessor (or the provider defaults it uses); LiteLLM flags only remove types the model cannot read. Limits and new types are never invented: a vision-capable model whose predecessor took no images gets a note instead. Fields equal to the provider defaults are not written. |
| `notes` | Points to check, also shown under the checkbox. Ignored by apply. |
| `promo` | For a launch price: `{until: "<exclusive UTC end>", regular: {input, output, cached_input}, note: "..."}`. |
| `reasoning` | `reasoning_modes`, `default_reasoning_mode`, `reasoning_efforts`, `reasoning_disable_supported`, `reasoning_counts_against_output`. The report fills it from the `based_on` model adjusted with LiteLLM's flags (for example `thinking_always_on` removes `none`), so you can see and edit the controls the model gets; without reasoning modes it gets no reasoning controls and Smart routing skips it for reasoning requests. Do not carry provider modes a source does not confirm. |
| `aliases` | Moved from the hidden model when listed (for example `grok-4-latest`). |
| `effective_from` | Omit: the first rule applies from the start, like most registry rules. |

Apply refuses unknown fields, malformed IDs or aliases, unknown rate names and
negative prices, because the issue body is editable.

## One-time setup

1. Create the `model-updates` environment (Settings > Environments) with
   required reviewers. Without reviewers the apply job runs without approval.
2. Allow GitHub Actions to create pull requests (Settings > Actions > General >
   Workflow permissions).
3. Optional: add a fine-grained token as the `MODEL_CATALOG_TOKEN` repository
   secret (contents and pull requests: read and write). Pull requests opened with
   the default token do not start CI; with this token they do. Without it, push
   an empty commit or close and reopen the pull request to start CI.
4. Run **Model catalog** once from Actions (Run workflow, issue empty).

To re-run apply for an issue (for example after an approval expired), run the
workflow with that issue number.

## Finishing the pull request

The bot's pull request contains only data: registry, providers, the ID lock and
the generated React defaults. Its body shows the blocking pricing checks (with the
re-seed summary) and the non-blocking tests that list the current models, which
are expected to fail when the selectable set or defaults change. Before merging, a
person:

1. Fixes the tests that list the current models (the PR body shows the
   failures), for example `tests/test_database_pricing_model_registry.py`,
   `tests/test_model_registry_capabilities.py`,
   `tests/test_routing_selection_examples.py`, and adds the model to the golden
   tables in `tests/test_new_model_credits.py`.
2. Adds provider client behaviour the model needs: API surface (OpenAI GPT-5.6+
   use the Responses API), sampling or thinking parameters, attachments.
3. Updates `.env.example` if a `DEFAULT_*_MODEL` pointed at a hidden model,
   the README and guide docs that name defaults, and the preflight values in
   [model-pricing.md](model-pricing.md).
4. Runs `python scripts/models.py check`, the backend and React suites.

## Deploying (database pricing)

Production prices from database rate cards, and startup fails if an enabled model
has no approved card. Deploy with the **Production Deploy** workflow: the EC2
controller seeds the new cards (dry run, then the write) and runs the preflight
before the API stops, and stops the deploy if either fails. Check its seed
summary and `RESULT: PASS`. Never seed by hand alongside a deploy. The
controller runs the equivalent of these commands, kept here for reference:

```bash
sudo /usr/bin/python3.11 scripts/sync_model_pricing.py --seed-legacy --dry-run
sudo /usr/bin/python3.11 scripts/sync_model_pricing.py --seed-legacy
sudo /usr/bin/python3.11 scripts/preflight_database_pricing.py --min-selectable <selectable count>
```

After the deploy, check the picker lists every provider, History loads, existing
threads on hidden models still answer, and run one Ask and one Compare turn on
each new model.
Production `.env` `DEFAULT_*_MODEL` values override `config/providers.yaml`;
change any that point at a hidden model.

## Local commands

```bash
python scripts/models.py report                       # download LiteLLM, print the issue Markdown
python scripts/models.py report --catalog litellm.json --markdown report.md --proposals proposals.yaml
python scripts/models.py apply --proposals proposals.yaml --select openai:gpt-7-luna
python scripts/models.py apply --issue-body issue.md  # ticked models of a saved issue body
python scripts/models.py check                        # CI gate
python scripts/models.py sync-frontend                # after editing ui_defaults by hand
python scripts/models.py lock                         # after adding an ID by hand
```

`check` verifies: the registry and pricing identities load; no duplicate or
removed IDs; non-`ACTIVE` models are not selectable and carry dates or
replacements; selectable credits sit on a ladder step (or a listed legacy
exception) with matching class and label and cover the current standard price;
replacement chains stay within the provider, never move to a dearer class and
reach a selectable model; registry and provider defaults are selectable (plan
defaults also allowed for their plan); `.env.example` model variables name
enabled models, with a warning when one is hidden or no longer `ACTIVE`;
promotions ending within 45 days; and the generated React defaults are current.

## 2026-10-14 update

| Added | Replaces (hidden) | Credits | Class | Price (LiteLLM, per 1M) |
|---|---|---|---|---|
| GPT-6 Luna | GPT-5.6 Luna | 0.25/1 | economical | $0.10/$0.50; >272K $0.20/$0.75 |
| GPT-6.1 Sol | GPT-5.6 Terra | 3/14 | advanced | $2/$10; >272K $4/$15 |
| GPT-6 Astra | GPT-5.6 Sol | 10/50 | premium | $10/$50; >272K $20/$75 |
| Claude Haiku 5.5 | Haiku 4.5 | 0.25/1 | economical | $0.10/$0.50; >100K $0.50/$2.50 |
| Claude Sonnet 5.5 | Sonnet 5, Sonnet 4.6 | 3/14 | advanced | $2/$10 |
| Claude Opus 5.5 | Opus 5, Opus 4.6, Opus 4.5 | 6/30 | premium | $4/$20 |
| Claude Fable 5.1 | Fable 5 | 10/50 | premium | $10/$50 |
| Gemini 3.8 Flash | Gemini 3.6 Flash | 1/4 | standard | $0.75/$3.75 to 2026-12-31, then $1.50/$7.50 |
| Gemini 4 Argon | Gemini 3.1 Pro Preview | 3/14 | advanced | $2/$10 to 2026-11-30, then $4/$20 |
| Grok 4.7 | Grok 4.5 | 2/8 | standard | $2/$6; >200K $4/$12 |

Also: `deepseek-v4-flash` renamed `deepseek-flash` (image input enabled),
Claude Sonnet 5 moved to $2/$10 with 3/14 credits, Claude Sonnet 4.5 scheduled
for retirement on 2026-11-30, and Gemini 2.5 Flash-Lite/Flash/Pro marked
`DEPRECATED`. 18 models are selectable.
