# Stripe Billing and Webhook Lifecycle

This runbook covers server-owned Stripe Customer, Checkout, Customer Portal, verified webhook synchronization, and safe replay for B2C subscriptions.

## Safety invariants

- Stripe secret keys and webhook signing secrets stay in backend environment or secret management. Never put them in frontend runtime config, Postman files, logs, or source control.
- The browser submits only `plan_code` and `billing_period`. Price IDs, amounts, currencies, Customer IDs, and redirect URLs are server-owned.
- Checkout and Portal never grant paid access. Only a verified Stripe webhook or an explicitly authorized future reconciliation caller may update the provider snapshot used by entitlements.
- The webhook verifies `Stripe-Signature` against the exact raw body before persistence. Dashboard and Stripe CLI endpoint secrets are different even though both start with `whsec_`.
- Stripe Price IDs are reverse-mapped through the configured plan catalogue. Metadata plan names are not billing authority.
- Do not manually edit paid subscription rows or usage counters to compensate for a provider incident.

## Required configuration

Install `requirements.txt`, create one recurring monthly Price for each paid plan, configure Customer Portal, and set:

```ini
BILLING_ENABLED=true
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PLUS_MONTHLY_PRICE_ID=price_...
STRIPE_PRO_MONTHLY_PRICE_ID=price_...
STRIPE_CHECKOUT_SUCCESS_URL=https://app.example.com/account/billing?checkout=success
STRIPE_CHECKOUT_CANCEL_URL=https://app.example.com/pricing?checkout=cancelled
STRIPE_PORTAL_RETURN_URL=https://app.example.com/account/billing
SUBSCRIPTION_PAYMENT_GRACE_DAYS=3
```

`STRIPE_API_VERSION` is optional. Leave it unset to use the Stripe SDK-pinned version unless a reviewed compatibility test requires an override. HTTP redirect URLs are accepted only for loopback development; public redirects and the registered webhook endpoint must use HTTPS. Startup fails conservatively when billing is enabled and a required Stripe value is missing or malformed.

The success and Portal return URLs must target the React `/account/billing` route, and the cancellation URL must target `/pricing`. The `checkout=success` query is only a bounded refresh hint: do not treat the browser return as proof of payment. Confirm the page remains in a waiting state until the verified webhook-backed entitlement response reports a paid plan.

Register `POST /v1/billing/webhook` for these snapshot events:

```text
checkout.session.completed
customer.subscription.created
customer.subscription.updated
customer.subscription.deleted
invoice.paid
invoice.payment_failed
```

Listening only to required event types reduces load and avoids retaining unrelated event metadata. The endpoint has no Cognito/session/API-key authentication because Stripe's signature is its authentication boundary.

## Local test-mode validation

For fast entitlement UX testing without Stripe, run `python run_app.py --subscription-plan free`, `plus`, `pro`, or `unrestricted`. These profiles are accepted only when both runner hosts are loopback addresses, force billing off and local dev-session login on, and override conflicting subscription values from `.env`. `unrestricted` uses the Pro feature set with very large subscription allowances while retaining authentication, provider requirements, upload/file safety ceilings, and production guards. Omit `--subscription-plan` when validating the real Stripe lifecycle below.

1. Apply and verify all current scripts through `20260804_add_generation_budget_audit.sql` in the order documented in `docs/runbooks/db-migrations.md`.
2. Use Stripe test keys, test Prices, and a CLI forwarding secret only.
3. Forward the six required events to the local API, for example with Stripe CLI event filtering and `--forward-to http://127.0.0.1:8000/v1/billing/webhook`.
4. Set the `whsec_...` value printed by that CLI process as `STRIPE_WEBHOOK_SECRET`; do not reuse a Dashboard endpoint secret.
5. Run `python -m pytest tests/test_stripe_billing.py tests/test_stripe_webhooks.py tests/test_billing_repository.py -q`. Tests use mocked provider retrieval except for local cryptographic signature construction and make no Stripe network calls.
6. Complete a test Checkout. Confirm Customer and Subscription IDs appear only in database/provider state, the event row becomes `processed`, `/v1/entitlements` changes only after the webhook, and one paid usage period uses the Stripe period boundaries.
7. Resend the same event. Confirm the event count and usage-period count do not change and existing counters retain their values.
8. Use a failing test payment to confirm `past_due`, `grace_until`, and the eventual Free fallback. Cancel through Portal and confirm history remains readable after access downgrades.
9. Set `BILLING_ENABLED=false` and confirm `/pricing` labels the effective plan Current plan and other plans Unavailable while `/account/billing` shows the effective allowances. A valid Cortex grant retains paid access without Checkout or Portal.
10. Validate subscription UX at desktop and phone widths: locked models remain visible, the third Compare target offers Pro, Web/Improve/file limits explain denials without clearing the draft, and the dedicated AI credits destination shows one activity card and one total per submitted question. Confirm the composer has no Answer depth or live temporary-hold control, sends `generation.profile=auto`, the provider and authorization receive the same server-resolved per-target ceiling, and unused held credits are released after settlement. Use `POST /v1/billing/estimate-generation` separately to verify the API-only no-reservation hold preview. With Improve and Web enabled, confirm the card retains the original pre-optimization question and combines Prompt Optimizer, the final Ask/Compare answer, and Web Search under one activity ID. Expand the card to confirm Prompt Optimizer and final answer generation appear together as one `Final optimized ... answer` charge, optimizer retries are aggregated inside that line, Web Search remains separate, and zero-credit adjustments do not add noise. Confirm optimizer-only activity is still labelled clearly. Usage & insights must remain focused on provider analytics; payment/cancellation banners and restored premium history must remain readable after downgrade. Confirm legacy rows use the request-ID/unavailable-query fallback, metadata-only mode does not retain the query, and PII redaction also applies to ledger context. Repeat the submission against the API to confirm backend enforcement is still authoritative.

Do not complete a live-money Checkout during automated or staging validation.

## Processing and retry behavior

The endpoint caps raw payloads at 1 MiB. Invalid or missing signatures return `400 invalid_webhook_signature` before an event row is written. Billing-disabled mode returns `503 billing_not_configured`; missing database mode returns `501 billing_database_required`.

For a verified event, the service hashes the raw payload and inserts its Stripe event ID as `received` in a committed transaction. A short row-lock transaction rejects terminal duplicates or marks valid unknown events `ignored`, then closes before any Stripe API retrieval. A final row-lock transaction rechecks the event, commits the Subscription/period update, and marks it `processed`. Processed or ignored sequential duplicates return `200` without repeating provider retrieval or lifecycle changes; concurrent duplicates can perform an extra read but only one can mutate local state. A failed attempt records a safe failure code and returns non-2xx so the same event can be retried under the row lock.

Subscription events compare `event.created` with `last_provider_event_at`; older snapshots cannot overwrite newer state. Checkout and invoice events retrieve the current Subscription before applying access. Repeated renewal events reuse the `(billing_account_id, starts_at)` usage-period row. A same-period plan/end change updates that row in place and preserves its counters.

`invoice.payment_failed` sets a grace deadline only if the retrieved Subscription still reports `past_due`. `invoice.paid` clears stale grace through the active provider snapshot. `customer.subscription.deleted` updates the stored lifecycle and delegates the effective Free/cancel-at-period-end result to `subscription_service.py`; it never deletes the billing account, usage history, or conversations.

## Reconciliation boundary

`server.billing.webhook_service.reconcile_billing_account()` can retrieve all current Subscriptions for one persisted Customer and synchronize the single live lifecycle. It rejects more than one live Subscription for manual review and downgrades a stale local snapshot when Stripe authoritatively lists none.

The helper is intentionally not exposed as HTTP. The current repository has tenant authentication but no administrator authorization suitable for cross-account billing repair. Add a route only with a reviewed admin-auth primitive, audit logging, account scoping, rate limiting, and tests; never treat the existing tenant-scoped `/v1/admin` namespace as administrator proof.

## Disable and recover

Set `BILLING_ENABLED=false` to stop hosted-session and webhook processing and fall back to valid Cortex grants, then Free. Revoke Cortex grants separately through the operator CLI when required. Stripe will continue retrying non-2xx webhook deliveries, so use this only as a reviewed emergency control and restore processing within the provider retry window. Do not delete the endpoint or signing secret during an incident unless rotation is the incident response.

For duplicate, failed, stale, unknown-Price, multiple-live-subscription, or signing-secret incidents, follow `docs/runbooks/subscription-incidents.md`. Hosted-session errors remain:

- `409 stripe_customer_required`: no persisted Customer exists for Portal; use Checkout for a first purchase or reviewed reconciliation.
- `502 billing_provider_unavailable`: Stripe rejected or could not complete a hosted-session request.
- Startup configuration error: verify secret/Price prefixes, all server redirect URLs, the endpoint-specific signing secret, and any explicit API version.
