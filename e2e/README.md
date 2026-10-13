# CortexAI Live E2E

This package contains the live full-stack browser suite and the frontend-only responsive browser suites for CortexAI.

## What lives here

- Playwright specs and fixtures
- E2E-only server bootstrap
- DB and API helpers
- run-state, cleanup, and artifact helpers

Nothing in this folder is used by the API image or the frontend deployment artifact.

## Key files

- `global-setup.mjs`: starts the E2E server, waits for readiness, and writes shared run state.
- `global-teardown.mjs`: replays cleanup and shuts down the shared server process.
- `fixtures/live-e2e.mjs`: creates the `liveApp` fixture every spec uses.
- `helpers/api.mjs`: HTTP polling and history-cleanup requests.
- `helpers/db.mjs`: direct Postgres assertions and cleanup backstop; case snapshots match both prompt markers and E2E request IDs so optimizer rewrites remain discoverable and removable.
- `helpers/network.mjs`: request-id injection plus optional fault headers.
- `helpers/ui.mjs`: browser actions and stream-observation helpers; readiness waits for the prompt input and the desktop `Workspace mode` navigation within `E2E_FRONTEND_READY_TIMEOUT_MS`, with no error alerts.
- `server/run_e2e_server.py`: E2E-only FastAPI bootstrap.
- `server/fault_injection.py`: request-scoped fallback fault injection for special-path tests.
- `server/stream_tuning.py`: makes local streaming easier to observe in browser assertions.
- `responsive/mobile/`: phone-specific navigation, composer, attachment, model-picker, history, and response-layout coverage.
- `responsive/desktop-ipad/`: desktop and iPad portrait/landscape responsive coverage.
- `playwright.mobile.config.mjs`: independently runnable mobile suite.
- `playwright.desktop-ipad.config.mjs`: independently runnable desktop/iPad suite.

## Setup

1. Copy [e2e/.env.example](/C:/Users/14169/PycharmProjects/PythonProject/OpenAIProject/e2e/.env.example) to `e2e/.env` or export the same variables in your shell.
2. Point `E2E_DATABASE_URL` at the PostgreSQL instance you want the suite to verify.
3. Apply every `db/migrations/*.sql` file to that database in filename order. The E2E server runs the same startup schema preflight as production and will stop before browser tests if required generation, billing, or cache-accounting columns are missing.
4. Set a dedicated `E2E_API_KEY`.
5. Optionally override `E2E_DEV_SESSION_LOGIN_TOKEN`; the harness uses it only to mint a local session cookie for browser-scoped routes.
6. Keep provider secrets in the repo root `.env` or export them in your shell.
7. Install the React dependencies with `npm ci --prefix frontend-react`.

## Commands

```bash
npm install --prefix e2e
npm run --prefix e2e install:browsers
npm run --prefix e2e test
```

The live `test` command requires the E2E database, API key, and provider configuration described above. Its global setup builds `frontend-react/dist`, starts FastAPI with that React build as the browser target, and enables the production-blocked dev-session endpoint only inside the E2E server process. The E2E bootstrap forces the guarded local `unrestricted` subscription profile so serial provider cases still exercise reservation/settlement without exhausting a shared Free or paid usage period.

Responsive UI suites do not require PostgreSQL, provider keys, or a running backend. They start Vite directly and mock the frontend API contracts:

```bash
npm run --prefix e2e test:mobile
npm run --prefix e2e test:desktop-ipad
```

The mobile suite covers 320px and 390px phone layouts. The desktop/iPad suite covers 1440px desktop, 1024px iPad landscape, and 820px iPad portrait behavior. The shared responsive fixture can switch its mocked effective plan and catalogue ordering so Free/Plus/Pro Ask and Compare default coverage verifies plan eligibility without pretending that the live unrestricted harness represents paid-plan isolation. Each command can be run independently.

Mobile composer containment checks measure the feature row and its buttons in one browser layout snapshot so asynchronous font swaps cannot mix old container bounds with new button bounds. The one-pixel containment tolerance remains unchanged.

Useful variants:

```bash
npm run --prefix e2e test:headed
npm run --prefix e2e test:debug
npm run --prefix e2e test:report
```

To verify live startup and UI readiness without submitting a provider prompt:

```bash
npm run --prefix e2e test:headed -- specs/00.app-readiness.spec.mjs
```

This smoke check uses the same live database and authentication prerequisites as the full suite. Global setup opens a headless readiness probe before the headed test browser starts.

## Playwright MCP (option 1)

The repo now includes local Playwright MCP startup scripts so AI tools can drive a browser session in addition to the normal `@playwright/test` suite.

Start MCP over stdio (default):

```bash
npm run --prefix e2e mcp
```

Headless variant:

```bash
npm run --prefix e2e mcp:headless
```

SSE transport variant (for MCP clients that connect over HTTP):

```bash
npm run --prefix e2e mcp:sse
```

Example client config (stdio):

```json
{
  "mcpServers": {
    "playwright-local": {
      "command": "npm",
      "args": ["run", "--prefix", "e2e", "mcp:headless"]
    }
  }
}
```

PyCharm fallback (if npm Run Configuration is unavailable):

1. Create a Python Run Configuration.
2. Script path: `scripts/run_playwright_mcp.py`
3. Working directory: project root
4. Parameters:
   - `--mode headed` (see browser)
   - `--mode headless` (no browser window)
   - `--mode sse` (HTTP/SSE transport on `127.0.0.1:8931`)

## Notes

- The suite runs serially on purpose.
- Every test attempt gets a unique `case_id`.
- Cleanup uses both the stable E2E owner and the per-attempt `case_id`.
- Fallback injection is only enabled inside the E2E server bootstrap.
