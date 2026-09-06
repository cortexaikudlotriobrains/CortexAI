-- CortexAI Work: authoritative provider identity, Web policy audit, and output guardrails.

BEGIN;

ALTER TABLE public.work_runs
    ADD COLUMN IF NOT EXISTS max_output_tokens bigint NOT NULL DEFAULT 40000,
    ADD COLUMN IF NOT EXISTS actual_output_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_finalize_requested_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS output_limit_interrupt_requested_at timestamptz NULL,
    ADD COLUMN IF NOT EXISTS provider_model_id text NULL,
    ADD COLUMN IF NOT EXISTS billing_model_id text NULL,
    ADD COLUMN IF NOT EXISTS billing_model_source text NULL,
    ADD COLUMN IF NOT EXISTS provider_agent_id text NULL,
    ADD COLUMN IF NOT EXISTS provider_agent_version integer NULL;

ALTER TABLE public.work_runs
    DROP CONSTRAINT IF EXISTS ck_work_runs_max_output_tokens,
    ADD CONSTRAINT ck_work_runs_max_output_tokens CHECK (max_output_tokens > 0),
    DROP CONSTRAINT IF EXISTS ck_work_runs_actual_output_tokens,
    ADD CONSTRAINT ck_work_runs_actual_output_tokens CHECK (actual_output_tokens >= 0),
    DROP CONSTRAINT IF EXISTS ck_work_runs_provider_agent_version,
    ADD CONSTRAINT ck_work_runs_provider_agent_version
        CHECK (provider_agent_version IS NULL OR provider_agent_version > 0);

ALTER TABLE public.work_runs
    DROP CONSTRAINT IF EXISTS ck_work_runs_status;

ALTER TABLE public.work_runs
    ADD CONSTRAINT ck_work_runs_status CHECK (
        status IN (
            'created', 'planning', 'running', 'waiting_for_approval',
            'completed', 'failed', 'cancelled', 'budget_exhausted',
            'output_limit_reached'
        )
    );

COMMIT;
