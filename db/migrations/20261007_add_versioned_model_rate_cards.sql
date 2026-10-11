-- Additive: no historical response/credit rewrite and no invented rate references.
BEGIN;
CREATE TABLE IF NOT EXISTS public.model_rate_cards (
    id uuid PRIMARY KEY,
    provider text NOT NULL,
    canonical_model_id text NOT NULL,
    provider_model_id text NOT NULL,
    processing_mode text NOT NULL DEFAULT 'standard',
    source text NOT NULL CHECK (source IN ('LEGACY_CORTEX', 'LITELLM', 'CORTEX_MANUAL')),
    source_reference text NOT NULL,
    source_version text NOT NULL,
    fingerprint text NOT NULL,
    currency text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
    input_price_per_million numeric(30,15) NOT NULL CHECK (input_price_per_million >= 0),
    output_price_per_million numeric(30,15) NOT NULL CHECK (output_price_per_million >= 0),
    pricing_components jsonb NOT NULL CHECK (jsonb_typeof(pricing_components) = 'object'),
    effective_from timestamptz NOT NULL,
    effective_to timestamptz,
    status text NOT NULL CHECK (status IN ('approved', 'candidate', 'rejected')),
    review_reason text,
    override_actor text,
    override_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_verified_at timestamptz NOT NULL,
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    CHECK (source <> 'CORTEX_MANUAL' OR (override_actor IS NOT NULL AND override_reason IS NOT NULL
           AND length(override_actor) > 0 AND length(override_reason) > 0))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_rate_cards_open
    ON public.model_rate_cards (provider, canonical_model_id, processing_mode, source)
    WHERE status = 'approved' AND effective_to IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_model_rate_cards_candidate
    ON public.model_rate_cards (provider, canonical_model_id, processing_mode, source, fingerprint)
    WHERE status = 'candidate';
CREATE INDEX IF NOT EXISTS ix_model_rate_cards_lookup
    ON public.model_rate_cards (provider, canonical_model_id, processing_mode, effective_from DESC);
CREATE TABLE IF NOT EXISTS public.pricing_catalog_observations (
    source text NOT NULL,
    source_model_id text NOT NULL,
    provider text NOT NULL,
    canonical_model_id text,
    source_version text NOT NULL,
    last_seen_at timestamptz NOT NULL,
    missing_since timestamptz,
    pricing_available boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (source, source_model_id)
);
CREATE TABLE IF NOT EXISTS public.pricing_sync_runs (
    id uuid PRIMARY KEY,
    source text NOT NULL,
    source_version text,
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    status text NOT NULL CHECK (status IN ('completed', 'failed')),
    summary jsonb NOT NULL DEFAULT '{}'::jsonb
);
ALTER TABLE public.llm_responses
    ADD COLUMN IF NOT EXISTS rate_card_id uuid REFERENCES public.model_rate_cards(id),
    ADD COLUMN IF NOT EXISTS usage_calculated_provider_cost_usd numeric(30,15);
-- Prevent approved financial history from being edited/deleted by an accidental update.
CREATE OR REPLACE FUNCTION public.protect_approved_rate_card() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'approved' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Approved rate-card history cannot be deleted';
        END IF;
        IF (to_jsonb(NEW) - 'effective_to' - 'last_verified_at') IS DISTINCT FROM
           (to_jsonb(OLD) - 'effective_to' - 'last_verified_at') OR
           (OLD.effective_to IS NOT NULL AND NEW.effective_to IS DISTINCT FROM OLD.effective_to) THEN
            RAISE EXCEPTION 'Approved rate-card financial history is immutable';
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS protect_approved_rate_card ON public.model_rate_cards;
CREATE TRIGGER protect_approved_rate_card BEFORE UPDATE OR DELETE ON public.model_rate_cards
    FOR EACH ROW EXECUTE FUNCTION public.protect_approved_rate_card();
COMMIT;
