# Playbook: Add Feature End-To-End

## Use When

- You are adding a new user-visible capability that touches multiple layers (API, orchestration, persistence, UI, or docs).

## Inputs To Confirm

- Feature goal and non-goals.
- API contract changes (new route, fields, or behavior changes).
- Data persistence impact (new tables/columns/indexes or none).
- Rollout constraints (backward compatibility and flags).

## Execution Phases

1. Plan impact by layer:
   - route/schema
   - orchestration/service logic
   - persistence/reporting
   - frontend behavior
   - tests/docs
2. Implement backend contracts first:
   - update `server/schemas/`
   - implement/adjust route(s) in `server/routes/`
   - wire in `server/app.py` if needed
3. Implement business logic:
   - update `orchestrator/` and/or `server/*.py` services
   - keep routes thin and isolate logic in services
4. Apply persistence updates when needed:
   - add migration in `db/migrations/`
   - reflect schema in `db/tables.py` and `db/repository.py`
5. Update UI/e2e surfaces (if user-visible):
   - `frontend-react/` updates for React UI
   - `e2e/` assertions for the new flow
6. Update documentation and examples:
   - `README.md`
   - `docs/postman/CortexAI_B2B.postman_collection.json` when API contract changed

## Validation Checklist

- Targeted tests for changed area.
- Contract/regression tests for touched routes.
- `python -m pytest -q`
- `python scripts/release_gate.py` for pre-handoff confidence.
- `npm run --prefix frontend-react build` when React UI is touched.
- `npm run --prefix e2e test` when end-user flow or streaming UX changed.
- For a user-requested commit, verify the blocking hooks in
  [ci-commit-gate.md](./ci-commit-gate.md) are installed and do not bypass them.

## Done Criteria

- Feature works through intended entrypoint(s).
- No regressions in existing API contracts.
- Docs reflect shipped behavior.
- Migration and rollout plan included if schema changed.

## Analyze/Compare execution checks

For the automatic synthesis path, verify combined estimate/preauthorization,
server-owned initial intent idempotency, continuation after navigation,
insufficient/failed-model retries, actual usage settlement, revision snapshots,
failed-update recovery and paid-call prevention on read/view changes. Run the
Analyze mobile and desktop/iPad browser checks and apply the synthesis migration
before starting a PostgreSQL runtime. See `docs/COMPARE_MODE_GUIDE.md`.
