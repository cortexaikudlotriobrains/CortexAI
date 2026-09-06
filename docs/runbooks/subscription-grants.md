# Cortex-issued subscription grants

`BILLING_ENABLED` controls Stripe hosted billing. Keep it `false` while Stripe
is disabled: Checkout, Portal and webhook processing return
`503 billing_not_configured`, and Stripe secrets are unnecessary. Entitlements
and unified-credit accounting remain enforced. Users without a valid Cortex
grant resolve to Free.

Effective access follows this order: the existing guarded local override;
valid paid Stripe state when billing is enabled; a valid Cortex grant; Free.
The local override retains its existing environment/billing restrictions.
Stripe `trialing` does not grant paid access. Existing payment grace and
cancel-at-period-end rules remain unchanged. A valid Stripe subscription wins
even if its plan is lower than the grant. Grant expiry continues running while
Stripe has precedence; falling back to an existing grant cycle reuses its
counters instead of granting another allowance.

## Deploy

1. Take the normal backup and validate the migration in staging.
2. Drain/stop API replicas and workers that write usage periods. The migration
   replaces the account/start unique constraint with a partial index; the old
   application's `ON CONFLICT` statement is incompatible with that index.
   This release requires a coordinated cutover, not mixed application versions.
3. Apply `db/migrations/20260905_add_subscription_grants.sql` after all preceding
   migrations, with the table-owner connection. See [DB migrations](db-migrations.md)
   for verification SQL. Grant the runtime role `SELECT` on `subscription_grants`;
   grant the trusted operator role `SELECT, INSERT, UPDATE` on it and the normal
   billing account/period permissions used by the service. Existing default
   privileges may already provide these rights; verify them explicitly.
4. Deploy/restart the API and workers to refresh reflected tables, and deploy
   the React build. Keep `BILLING_ENABLED=false` and production dev overrides
   unset. Billing schema preflight must pass before serving requests.
5. Use the operator CLI for selected users. Verify `/v1/entitlements`,
   `/v1/billing/subscription`, pricing, and Work access with a granted test user
   and an ungranted Free user.

No production migration or grant is applied automatically. There is no grant
HTTP endpoint, no Stripe-row conversion, and no fake provider identity.

## Operator commands

Run from the repository root on a trusted server with `DATABASE_URL` targeting
the intended environment and an authorized operator DB role. The script loads
the root `.env` without overriding exported environment variables. DB access is
the authorization boundary; `--actor` is an audit assertion, not authentication.
Use individually attributable operator credentials and restrict execution to
authorized staff. Do not put DB credentials into the command line or tickets.

```powershell
python scripts/manage_subscription_grant.py inspect --email user@example.com
python scripts/manage_subscription_grant.py grant --email user@example.com --plan plus --days 90 --actor operator@example.com --reason early_beta
python scripts/manage_subscription_grant.py grant --email another@example.com --plan pro --expires-at 2026-12-05T12:00:00Z --actor operator@example.com --reason early_beta
python scripts/manage_subscription_grant.py change --email user@example.com --plan pro --days 90 --actor operator@example.com --reason beta_upgrade
python scripts/manage_subscription_grant.py change --email user@example.com --plan plus --days 30 --actor operator@example.com --reason beta_downgrade
python scripts/manage_subscription_grant.py revoke --email user@example.com --actor operator@example.com --reason beta_ended
```

In the Windows checkout use `venv\Scripts\python.exe` instead of `python`.
Use `--user-id <existing UUID>` instead of `--email` when needed. Email lookup
is exact and case-insensitive; missing or ambiguous matches fail without
creating a user. All writes require a nonempty reason and actor. Issuance and
change require positive days or a future ISO-8601 expiry with a timezone.
Grant starts are immediate. Future rows are ignored by effective resolution;
the CLI does not schedule grants.

`grant` rejects an existing open grant; use `change` to explicitly replace it.
An elapsed open row is marked expired before new issuance. Changes/revocations
preserve the original reason and actor and record separate revocation evidence.
Account locks and the one-active-row index prevent conflicting simultaneous
grants. The entire command commits once or rolls back. `inspect` reads the open
lifecycle row and the time-valid candidate without creating an account/period;
the effective grant candidate can still be overridden by valid Stripe access.

## Monthly usage and audit

Each grant references the existing Plus or Pro catalogue; no plan numbers or
feature rules are copied into grants. All models, request/file limits, Compare,
Work concurrency/budgets, connector, custom MCP and action-tool eligibility use
the normal entitlement services and operator feature flags.

A September 5 to December 5 grant has September 5–October 5,
October 5–November 5 and November 5–December 5 periods. Each uses the normal
monthly AI-credit allowance. Anniversaries use the original UTC timestamp:
January 31 becomes February 28 (29 in leap years), then March 31. The final
period is clipped to expiry. Creation is lazy on access; unused months do not
accumulate or carry credits forward. The API's existing period-end/`renews_at`
fields describe the usage reset for grants, not a payment or Stripe renewal.

Grant periods carry `subscription_grant_id` and no `subscription_id`; Free
periods have neither, and Stripe periods retain `subscription_id`. A change
creates a new grant and a fresh full allowance immediately, closes the prior
grant period, and preserves its dates, usage, reservations and credit ledger.
Use changes deliberately: they are an operator-authorized allowance reset.
Already reserved work can finish settlement against its original period after
revocation; new access resolves normally. Returning to Free within the same
month reuses that month's existing counters.

Granted API state is `source=cortex_grant`, `status=active`, null provider and
provider subscription ID, no pending cancellation, and `can_manage=false`.
Pricing shows the granted current plan even when hosted billing is disabled.
Billing describes CortexAI-provided access and usage resets without payment
management actions.

For access rollback, revoke grants through the CLI. Retain grant/period history
and the additive schema. Rolling back application binaries requires restoring
compatibility with the new usage-period indexes; do not start an unmodified old
build or delete grants/periods to restore the old constraint. Prefer a forward
application fix. Disabling Stripe alone does not revoke Cortex grants.
