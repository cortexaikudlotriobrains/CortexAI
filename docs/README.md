# Docs Index

Use this index to quickly find the current source of truth and avoid stale implementation snapshots.

## Current Source-of-Truth Docs

- `../README.md` - primary project/runtime guide
- `FASTAPI_README.md` - API request/response contracts and guardrails
- `PROJECT_MAP.md` - "where to change X" code navigation map
- `SMART_ROUTING_DIAGRAM.md` - smart routing execution flow
- `LOGGING.md` - structured logging behavior, redaction, and operations guidance
- `COMPARE_MODE_GUIDE.md` - compare-mode behavior and persistence notes
- `TAVILY_INTEGRATION.md` - web research/Tavily integration behavior
- `runbooks/db-migrations.md` - migration authoring/apply/rollback runbook
- `runbooks/model-updates.md` - adding, hiding and retiring models: credit ladder, plan placement, weekly `Model catalog` workflow and database-pricing deploy steps
- `runbooks/model-pricing.md` - provider rate cards, LiteLLM sync and the database-pricing preflight
- `runbooks/aws-ec2-logging.md` - AWS EC2 Linux logging and incident triage runbook
- `runbooks/direct-s3-attachment-rollout.md` - repository/AWS ownership boundary, private-S3 CORS/IAM/KMS/WAF prerequisites, controlled rollout, smoke tests, troubleshooting, and rollback
- `UNIFIED_RESPONSE_CONTRACT.md` - provider adapter response contract
- `postman/CortexAI_B2B.postman_collection.json` - API collection/examples
- `SRED_TECHNICAL_NARRATIVE_2026_DRAFT.md` - SR&ED technical narrative draft from git history (uncertainty/experiments/validation)

## Historical/Snapshot Docs

These are kept for implementation history and audit context, not day-to-day onboarding:

- `DATABASE_INTEGRATION_COMPLETE.md`
- `REFACTORING_SUMMARY.md`

When details conflict, prefer `../README.md` and `FASTAPI_README.md`.

---

Last updated: 2026-08-11
