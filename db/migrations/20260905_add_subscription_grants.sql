BEGIN;

-- Cortex access is independent of provider subscription/payment history.
CREATE TABLE IF NOT EXISTS public.subscription_grants (
    id uuid PRIMARY KEY,
    billing_account_id uuid NOT NULL REFERENCES public.billing_accounts(id),
    plan_code varchar(64) NOT NULL,
    status varchar(32) NOT NULL DEFAULT 'active',
    starts_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    granted_by text NOT NULL,
    reason text NOT NULL,
    revoked_at timestamptz NULL,
    revoked_by text NULL,
    revocation_reason text NULL,
    created_at timestamptz NOT NULL DEFAULT NOW(),
    updated_at timestamptz NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_subscription_grants_plan CHECK (plan_code IN ('plus', 'pro')),
    CONSTRAINT ck_subscription_grants_dates CHECK (expires_at > starts_at),
    CONSTRAINT ck_subscription_grants_status CHECK (status IN ('active', 'expired', 'revoked')),
    CONSTRAINT ck_subscription_grants_audit CHECK (btrim(granted_by) <> '' AND btrim(reason) <> ''),
    CONSTRAINT ck_subscription_grants_revocation CHECK (
        (status = 'revoked' AND revoked_at IS NOT NULL AND
         revoked_by IS NOT NULL AND btrim(revoked_by) <> '' AND
         revocation_reason IS NOT NULL AND btrim(revocation_reason) <> '') OR
        (status <> 'revoked' AND revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL)
    )
);

-- Time cannot appear in an immutable index predicate. Issuance retires expired
-- rows under the billing-account lock before inserting their replacement.
CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_grants_one_active_per_account
    ON public.subscription_grants (billing_account_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS ix_subscription_grants_account_time
    ON public.subscription_grants (billing_account_id, status, starts_at, expires_at);

ALTER TABLE public.usage_periods
    ADD COLUMN IF NOT EXISTS subscription_grant_id uuid NULL
        REFERENCES public.subscription_grants(id);

-- Keep the existing non-grant economic key; grants have their own immutable
-- source identity, even when a change happens at precisely the same instant.
ALTER TABLE public.usage_periods DROP CONSTRAINT IF EXISTS uq_usage_period_account_start;
CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_period_account_start
    ON public.usage_periods (billing_account_id, starts_at)
    WHERE subscription_grant_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_period_grant_start
    ON public.usage_periods (subscription_grant_id, starts_at)
    WHERE subscription_grant_id IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'public.usage_periods'::regclass
          AND conname = 'ck_usage_period_single_source') THEN
        ALTER TABLE public.usage_periods ADD CONSTRAINT ck_usage_period_single_source
            CHECK (subscription_id IS NULL OR subscription_grant_id IS NULL);
    END IF;
END $$;

COMMIT;
