# Subscription Incident Response

Use this runbook for Stripe webhook delivery, subscription lifecycle, and paid-period incidents. Never paste secrets, full webhook payloads, payment data, browser tokens, hosted URLs, or user prompts into tickets or logs.

## First response

1. Record the request ID, Stripe event ID, event type, billing account ID, stored status, and timestamps. Use identifiers only in restricted operational systems.
2. Check Stripe Workbench delivery history and the matching `billing_webhook_events` row. `processed` and `ignored` are terminal; `failed` is retryable; a long-lived `received` row indicates interrupted processing.
3. Compare the local Subscription ID, Customer ID, configured Price mapping, status, period bounds, grace deadline, and `last_provider_event_at` with Stripe Dashboard state. Do not copy the full event payload.
4. Keep `BILLING_ENABLED=true` when the endpoint can process safely so Stripe retries can recover automatically. Disable it only for a reviewed security or data-integrity incident.

## Invalid signatures

- Confirm the request reached `POST /v1/billing/webhook` without a proxy/body parser rewriting bytes.
- Confirm `Stripe-Signature` is forwarded intact and server clocks are synchronized.
- Confirm `STRIPE_WEBHOOK_SECRET` belongs to this exact endpoint and mode. Stripe CLI and Dashboard secrets are not interchangeable.
- Rotate the endpoint secret in Stripe and secret management if exposure is suspected. During rotation, account for Stripe's overlapping-secret window and validate a signed test event before retiring the old secret.

Invalid signatures are rejected before persistence. Never weaken timestamp tolerance or bypass SDK verification to clear an incident.

## Failed or stuck events

- Correct transient database/provider availability or reviewed Price configuration first.
- Replay the same event from Stripe Workbench. The original event ID and payload hash allow the `failed` row to retry; a processed/ignored row returns success without repeating state changes.
- If a row remains `received` after an interrupted worker, replaying the same event safely acquires its row lock and resumes processing.
- If the same event ID appears with a different payload hash, stop and investigate endpoint integrity; do not edit the hash or event ID.

## Unknown Price or duplicate live subscriptions

- An unknown Price fails conservatively and does not trust event metadata. Verify `STRIPE_PLUS_MONTHLY_PRICE_ID`, `STRIPE_PRO_MONTHLY_PRICE_ID`, the plan catalogue, and the actual recurring Price on the Subscription.
- More than one provider-live Subscription for one Customer/account requires manual Stripe review. Cancel the unintended Subscription in Stripe according to refund/support policy, wait for its verified lifecycle event, then replay/reconcile.
- Never delete a database uniqueness constraint or reassign Customer/Subscription IDs to force processing.

## Stale or out-of-order state

- Older subscription snapshots are marked processed without overwriting a newer `last_provider_event_at` state.
- Invoice and Checkout handlers retrieve the current Subscription, so an old invoice event cannot independently restore paid access.
- If local state still differs, confirm Stripe's current Subscription first. The internal `reconcile_billing_account()` helper exists for future authorized operational tooling but is not an HTTP endpoint because administrator authorization is not yet implemented.

## Usage-period or counter concerns

- A Stripe period is identified by billing account plus period start. Duplicate invoice/subscription events reuse it and do not reset counters.
- Same-period plan/end changes preserve the period row and existing counters.
- Do not reset counters manually. Capture the period ID, counter totals, related reservation states, and provider event IDs for engineering review.
- For leaked metering reservations unrelated to Stripe delivery, inspect
  `billing.reservation_cleanup.*` and heartbeat logs first. Confirm
  `ENABLE_BILLING_RESERVATION_CLEANUP_WORKER=true`, the default five-minute
  interval, and the 30-minute stale threshold. The worker uses activity
  heartbeats and locked rows; do not change webhook rows or counters manually.

## Payment failure and cancellation

- Grace is set only when a fresh Stripe Subscription remains `past_due`; the default is `SUBSCRIPTION_PAYMENT_GRACE_DAYS=3`.
- After grace expiry, `subscription_service.py` resolves Free without deleting history.
- Period-end cancellation remains paid only through its stored end when lifecycle fields support that policy; final deletion/immediate cancellation resolves Free.
- Never delete sessions, messages, files, or usage history during a downgrade incident.

## Closeout

Confirm a new signed test event succeeds, failed/received rows have reached a terminal state, `/v1/entitlements` matches Stripe lifecycle policy, one usage period exists for each provider period start, counters were preserved, and no secret or full payload entered logs or incident artifacts.

## Cortex grants

`BILLING_ENABLED=false` stops Stripe, but valid Cortex grants retain their plan access. Inspect the existing user with `python scripts/manage_subscription_grant.py inspect --email user@example.com`; check lifecycle status, UTC start/expiry and current period source. Use the [grant runbook](subscription-grants.md) for changes/revocations and monthly resets. A valid enabled Stripe subscription takes precedence. Never convert Stripe/trial rows to grants or edit usage history to repair access.
