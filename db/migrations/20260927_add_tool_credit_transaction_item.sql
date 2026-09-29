-- Add provider-native tool usage as an explicit immutable billing line item.

BEGIN;

ALTER TABLE public.credit_transactions
    DROP CONSTRAINT IF EXISTS ck_credit_transactions_item_type;

ALTER TABLE public.credit_transactions
    ADD CONSTRAINT ck_credit_transactions_item_type CHECK (
        item_type IN ('model', 'research', 'tool', 'adjustment')
    );

COMMIT;
