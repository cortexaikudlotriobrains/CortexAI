# Generation Budget Rollout

## Preconditions

1. Back up the target PostgreSQL database.
2. Apply all migrations through `20260807_add_cache_aware_credit_accounting.sql` with the
   schema-owner connection.
3. Restart the API so SQLAlchemy reflection sees the added columns.
4. Confirm the model registry and pricing alignment tests pass.

```powershell
psql "$env:MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260804_add_generation_budget_audit.sql
psql "$env:MIGRATION_DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/20260807_add_cache_aware_credit_accounting.sql
venv\Scripts\python.exe -m pytest tests/test_generation_policy.py tests/test_unified_response_contract.py tests/test_model_registry_capabilities.py tests/test_registry_pricing_alignment.py -q
```

## Staged enablement

Deploy first with `GENERATION_BUDGET_POLICY_ENABLED=false`. This verifies the migrated
schema and response metadata while retaining Quick/2K execution. Then enable the flag
on staging. Run browser Ask and two-target Compare probes for a normal standard model,
GPT-5.6 Terra, Claude Sonnet, Claude Opus/Fable, and an explicitly detailed task. Also
run API probes for each explicit profile, and verify:

- React sends `generation.profile=auto` plus its selected reasoning value, exposes no output-token profile or live hold estimate, and defaults Smart to Auto;
- manual Ask starts at the selected model's lowest supported reasoning level, Compare enables only shared levels, and Smart manual levels never route to an incompatible fallback;
- `/v1/model-options` publishes normalized reasoning levels for every selectable model and provider-specific Maximum/Low aliases resolve correctly;
- normal standard/economical Auto calls request 4K, advanced reasoning calls request 8K, and premium or deterministically complex/detailed calls request 12K;
- Auto never selects 32K on the first call;
- response `generation_budget.effective_max_output_tokens` matches provider kwargs;
- the billing authorization uses that same value for every target;
- natural stops are `complete` and length stops are `incomplete/token_limit`;
- partial text survives reload and Retry with more room uses the recommended profile;
- Auto 4K/8K retries use Deep/12K and Auto 12K retries use Extended/32K;
- unused temporary credit holds are released after settlement;
- oversized explicit custom limits return `422 invalid_generation_budget`.
- every selectable Claude model completes a default request without provider parameter rejection;
- Claude 4.6/5 adaptive requests omit custom temperature and manual-budget-only Claude 4.5 requests resolve to normal generation.

Promote the enabled setting by API fleet or environment only after those checks pass.
Watch incomplete rate by provider/model/profile, empty-visible-text length stops,
authorization denials, estimated-versus-settled credits, provider bad-request rate,
and retry success rate.

## Database verification

```sql
SELECT generation_policy_version, generation_profile, effective_max_output_tokens,
       effective_reasoning_mode, effective_reasoning_effort, count(*)
FROM public.llm_requests
WHERE created_at >= now() - interval '1 hour'
GROUP BY 1, 2, 3, 4, 5
ORDER BY 1, 2, 3;

SELECT completion_status, stop_cause, count(*)
FROM public.llm_responses r
JOIN public.llm_requests q ON q.id = r.llm_request_id
WHERE q.created_at >= now() - interval '1 hour'
GROUP BY 1, 2
ORDER BY 1, 2;
```

## Rollback

Set `GENERATION_BUDGET_POLICY_ENABLED=false` and restart/replace API processes. Do not
roll back the additive migration: older application versions ignore the new columns,
and keeping them preserves audit history. Confirm a new Auto browser request is
recorded with its public profile and executed at the rollback Quick/2K ceiling, then
investigate before re-enabling.

Use the switch for unexpected provider parameter rejection, excessive authorization
denials, materially elevated latency/cost, or a mismatch between provider and billing
ceilings. Provider outages alone should use the existing provider incident workflow.
