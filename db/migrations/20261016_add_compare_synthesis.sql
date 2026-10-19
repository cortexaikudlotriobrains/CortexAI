-- Additive Analyze v2 provenance and automatic-analysis authorization.
BEGIN;
ALTER TABLE cortex_analysis_runs
    ADD COLUMN IF NOT EXISTS structured_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS context_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS compare_analysis_intents (
    request_group_id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('waiting', 'queued', 'processing', 'complete', 'failed')),
    automatic BOOLEAN NOT NULL DEFAULT TRUE,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    context_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    reservation_payload JSONB,
    model TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens > 0),
    analysis_id UUID REFERENCES cortex_analysis_runs(id) ON DELETE SET NULL,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_compare_analysis_intents_pending
    ON compare_analysis_intents(state, updated_at) WHERE state IN ('queued', 'processing');
-- Safe to rerun after the initial Analyze migration; progress is display metadata.
ALTER TABLE compare_analysis_intents ADD COLUMN IF NOT EXISTS progress_stage TEXT;
COMMIT;
