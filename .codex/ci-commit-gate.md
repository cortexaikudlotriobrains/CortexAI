# Local Commit and CI Parity Gates

`.github/workflows/ci.yml` is the source of truth. The repository-managed Git
hooks apply its quality policy automatically instead of relying on documentation
or IDE behavior.

## Install once per clone

After installing `requirements-dev.txt`, use the project interpreter:

```powershell
venv\Scripts\python.exe -m pre_commit install --install-hooks --hook-type pre-commit --hook-type pre-push
```

Use `.venv` or set `CORTEX_CI_PYTHON` when the project interpreter has another
location. Confirm both managed hooks exist with `git config --get core.hooksPath`
and `.git/hooks/pre-commit` / `.git/hooks/pre-push`. `pre-commit` preserves an
unmanaged existing hook in migration mode; review that hook before relying on
the combined result.

## Automatic stages

- `pre-commit` exports and checks the exact staged tree. It blocks on Gitleaks
  8.30.1, Ruff, changed-file MyPy, staged test files, or the fast component-boundary
  regression. Pytest uses a private temporary directory inside the gate workspace,
  avoiding failures caused by inaccessible user-wide pytest temp roots on Windows.
  Black runs and reports as advisory, matching CI. Unstaged and ignored `.env`/log
  files cannot mask or contaminate the result.
- `pre-push` fetches the target base, scans the exact committed `HEAD` tree, then
  runs every locally runnable blocking backend/React/build job from an exported
  clean snapshot selected by the same path boundaries as `ci.yml`. Uncommitted
  changes cannot mask a committed failure. Its pytest jobs use the same private
  temp-root isolation. A failure blocks the push.
- The default target is `origin/develop`. Set `CORTEX_CI_BASE_REF` for a PR that
  targets another branch.
- Use a Python 3.12 project environment for exact parity. Gitleaks is downloaded
  once into the clone's Git tool cache and verified against the official release
  checksum.
- Do not bypass hooks with `--no-verify` or `SKIP` for a normal handoff. When the
  Docker CLI or daemon is unavailable, the hook reports the API image build as
  deferred and lets GitHub Actions remain authoritative for that job. Set
  `CORTEX_CI_REQUIRE_DOCKER=1` to make an unavailable Docker environment block
  the push. When Docker is available, an image build or inspection failure always
  blocks the push.

## Select the applicable jobs

The pre-push hook compares the final branch with its target (normally
`origin/develop`) and matches the path filters in `.github/workflows/ci.yml`:

- Always run the security secrets scan.
- Run backend quality and the API image build for backend, database, or shared
  changes.
- Run React responsive quality and the frontend artifact build for React or
  shared changes.
- A shared change runs both backend and React paths.

## Manual equivalent

Normally the hooks invoke these commands. For diagnosis, they can be run
directly:

```powershell
venv\Scripts\python.exe scripts\run_local_ci.py pre-commit
venv\Scripts\python.exe scripts\run_local_ci.py pre-push
```

## Backend quality details

Use Python 3.12 with `requirements.txt` and `requirements-dev.txt` installed.
Build the changed-file list from added or modified Python files only:

```powershell
$pythonChangedFiles = @(
  git diff --name-only --diff-filter=AM origin/develop...HEAD -- '*.py'
)

if ($pythonChangedFiles.Count -gt 0) {
  python -m ruff check $pythonChangedFiles
  python -m black --check $pythonChangedFiles  # advisory in CI
  python -m mypy --explicit-package-bases --follow-imports=skip $pythonChangedFiles
}
```

Run the blocking backend suites and dependency audit:

```powershell
python -m pytest tests/test_baseline_safety_rails.py tests/test_provider_catalog.py tests/test_client_registry_and_schema_dynamic.py tests/test_fastapi_contract_and_guardrails.py tests/test_dynamic_provider_discovery_e2e.py tests/test_component_boundaries.py tests/test_model_rate_cards.py tests/test_database_pricing_model_registry.py -q
python scripts/models.py check
python -m pytest tests/test_model_catalog_tool.py tests/test_new_model_credits.py tests/test_registry_pricing_alignment.py tests/test_model_registry_capabilities.py tests/test_credit_calculator.py -q
pip-audit -r requirements.txt -r requirements-dev.txt
docker build -f Dockerfile.api -t cortexai-api:ci .
```

The hook runs the Docker command automatically when the CLI and daemon are
available; otherwise it marks the image job as deferred to GitHub Actions. Black
is advisory in CI; run and report it, but it does not determine CI success.

## React responsive quality and artifact details

Use Node.js 20. In a clean checkout, run:

```powershell
npm ci --prefix frontend-react
npm ci --prefix e2e
Push-Location e2e
npx playwright install chromium
Pop-Location
npm run --prefix frontend-react test
npm run --prefix frontend-react lint
npm run --prefix frontend-react build
npm run --prefix e2e test:mobile
npm run --prefix e2e test:desktop-ipad
python scripts/build_frontend_artifact.py
```

## Security secrets scan details

The hook uses Gitleaks `8.30.1`, matching CI, against an exported tracked-tree
snapshot equivalent to a clean checkout:

```powershell
gitleaks version
gitleaks dir . --no-banner --redact --exit-code 1
```

Do not add allowlist entries merely to make the scan pass. Remove real secrets;
for a verified false positive, prefer wording or fixture changes that retain the
intended meaning without resembling a credential assignment.
