# CortexAI Work architecture

## Boundary and ownership

Work is an additive execution mode beside Ask and Compare. The React route,
Zustand store, API module, FastAPI routes, application services, repository, and
provider adapter are all Work-owned. Existing authentication, subscription,
unified AI credits, sessions, uploads, private object storage, and logging are
shared platform services. Work never starts Claude Code, Cowork, a shell, or a
local agent subprocess.

The authoritative state is split deliberately:

- PostgreSQL owns Cortex session/run status, ordered public events, approvals,
  tool-call audit, user ownership, file references, connection snapshots,
  provider IDs, billing reservations, and reconciliation leases.
- Anthropic Managed Agents owns the remote execution environment and continues
  running if the browser or API process disconnects.
- Private S3 owns user inputs and imported deliverables; a provider file ID or
  URL is never a public download authority.
- The browser is a resumable observer/controller. It owns no durable run state.

## Start-to-deliver flow

1. React uploads inputs through the existing Cortex file pipeline and receives
   owned `uploaded_files` IDs.
2. `POST /v1/work/sessions` creates a common `sessions(mode=work)` row plus a
   specialized `work_sessions` row. Its optional title is normalized to one
   line, with Unicode control/format characters removed, before persistence.
3. `POST /v1/work/sessions/{id}/runs` validates auth, ownership, entitlement,
   active-run/connection/file limits, web rollout, and the requested credit
   ceiling. The request's `web_mode` is `auto`, `on`, or `off`; the backend
   resolves Auto from current-information intent and snapshots both the
   requested and effective state. `Idempotency-Key` is the idempotency key.
   React retains the session created in step 2 before requesting this run, so a
   structured denial can retry in place. The sidebar displays only sessions
   with a run and does not present a zero-run shell as executed work. React
   immediately replaces the landing composer with a `Starting work` workspace
   during this request, but it does not invent a run ID or offer cancellation
   until the backend returns the durable run.
4. A short DB transaction reserves the maximum credits, creates the run and
   initial event, attaches files, and snapshots selected connections. It closes
   before any external I/O.
5. The provider adapter uploads input bytes server-side, creates or reuses the
   remote session, retrieves its resolved Agent snapshot, mounts all prior
   session resources after provider-session recovery, applies the provider
   budget, updates Web/MCP tools in place, and sends the user instruction. A
   session is replaced only when its immutable vault-resource set changes; the
   replacement receives a bounded PostgreSQL-backed transcript of prior user
   instructions and visible outcomes before the current instruction. It
   defensively normalizes the title again before remote session
   creation so a retry also repairs sessions stored before title normalization.
   The snapshot's model and Agent ID/version are persisted before the task is
   sent. Billing canonicalizes that model through the Cortex registry and fails
   closed if identity is missing, unknown, or contains multiple models.
6. `GET /stream` first replays `work_events` after `Last-Event-ID`, then claims a
   short PostgreSQL reconciliation lease, fetches provider events/usage, and
   appends normalized idempotent events. SSE comments keep the edge connection
   alive but do not alter the durable event sequence. If a status refresh sees
   a terminal run before React has consumed the corresponding stream events,
   React fetches and merges every event after its current sequence before it
   closes the stream. The same lease-protected reconciliation also runs in an
   application background worker, so browser presence is never required.
7. Tool-use events create high-signal audit rows. READ is confirmed silently.
   Unknown and sensitive actions become a persisted inline approval. WRITE can
   use an exact saved tool+connection grant for this Work session; destructive,
   external communication, financial, and deployment actions always ask.
8. Completion settles independent cumulative deltas for normal input, cache
   reads, cache writes, output, Managed Agent active time, and web searches.
   The provider's USD `list_cost` delta is an additional settlement floor, so a
   provider pricing change or reconstruction gap cannot silently underbill.
   Malformed/non-USD provider cost data leaves the reservation open for
   investigation. Unused reserved credits are released by the existing billing
   service. At 32,000 run-output tokens the reconciler sends a one-time
   finalization instruction. At the server-owned 40,000 ceiling it sends a
   one-time provider interrupt and records `output_limit_reached` after the
   remote run stops. Provider snapshots are sampled, so the terminal observed
   total can be above the threshold by output produced between polls.
9. When artifact rollout is enabled, output files are listed from the provider
   session recorded on the originating run, validated, downloaded server-side,
   stored through Cortex object storage, and registered as owned
   `uploaded_files` plus `work_run_files(role=artifact)` rows. Input and
   non-downloadable entries are skipped, failures are isolated per file, and a
   terminal run's artifact-list request retries missing imports idempotently.
10. Open/download links call an authenticated Cortex route that checks both run
    and file ownership and returns `private, no-store` bytes.

The browser activity rail is a projection of these durable events, not a raw
provider trace. It hides unlabeled internal progress events, allows only the
latest visible event to animate while the run is nonterminal, and settles every
visible marker plus all plan steps when the run completes.

The browser also loads all runs with `GET /v1/work/sessions/{id}/runs` and
hydrates each run's events and artifacts. The session surface is therefore a
chronological transcript: a follow-up appends a new prompt/outcome while prior
results and deliverables remain visible on desktop and mobile.

A follow-up creates a new Work run under the same Work session and uses the
same provider session when available. Web/MCP tool changes update that session
instead of replacing it; only incompatible vault-resource changes force a new
provider session and bounded visible-transcript replay. Billing uses the prior cumulative usage
snapshot as the baseline so earlier tokens/runtime are not charged twice. The
new reservation is also added to the provider session's cumulative list-cost
cap. Provider event IDs observed before the new run are baselined so prior
terminal events cannot complete the follow-up. When the earlier turn stopped at
`budget_reached`, updating the provider budget resumes that turn automatically;
Cortex does not race it with a new `user.message`.

## Provider-neutral events

Public event payloads are stable Cortex contracts: `run_created`, `planning`,
`plan_created`, `progress`, `tool_started`, `tool_completed`,
`approval_required`, `approval_resolved`, `file_created`, `run_completed`,
`run_failed`, `budget_exhausted`, `output_finalizing`, and
`output_limit_reached`. They contain display copy and bounded,
redacted summaries. Provider thinking, raw tool output, credentials, cookies,
tokens, and authorization headers are excluded.

Every event has a per-run sequence allocated from a locked run row. Provider
event IDs are unique per run. Replay after sequence N is therefore ordered and
duplicate provider delivery is a no-op.

## Approval protocol

- Built-in `read`, `glob`, `grep`, and enabled `web_search`/`web_fetch` use
  provider `always_allow`; they are read-only within the explicitly mounted or
  enabled task surface and do not pause the session.
- Built-in `bash`, `write`, and `edit` inherit provider `always_ask`. MCP tools
  also default to `always_ask`; Cortex classifies each request, silently
  confirms READ, and persists/requests approval for writes and sensitive work.
- Approval lookup joins approval -> run -> Work session -> user.
- A pending row is changed with a compare-and-set update. A replay gets 409.
- Pending approvals older than `CORTEX_WORK_APPROVAL_TIMEOUT_SECONDS` expire on
  the next reconciliation and are denied at the provider. A failed provider
  denial reopens the approval so a later reconciliation can retry safely.
- The provider receives allow/deny before the DB run resumes. If more pending
  approvals exist, the run remains `waiting_for_approval` and React shows one
  card at a time.
- Remembering is accepted only for `WRITE` with a concrete connection. The
  grant is exact `{connection_id, tool_name}` and scoped to this Work session.
  Sensitive action classes ignore remembered WRITE grants.
- Approval payload fields are redacted before persistence and rendering.

## Recovery and failure behavior

| Failure | User-visible behavior | Recovery source |
|---|---|---|
| Browser/SSE disconnect or terminal status ahead of the stream cursor | React catches up before closing or on reconnect | ordered PostgreSQL events |
| API restart/deploy | Browser reconnects; run keeps executing remotely | provider session ID + DB lease |
| Provider session missing or immutable vault set changed | a later run creates a replacement, remounts inputs, and replays bounded visible context | Cortex files + Work session runs/events |
| Duplicate start | original run is returned; conflicting reuse is 409 | request ID constraint |
| Tool approval wait | inline card; no side effect before confirmation | approval/tool-call rows |
| Budget reached | provider pauses itself; status becomes Budget reached without a client interrupt | run ceiling + usage snapshot |
| Output limit reached | Agent is asked to finalize at 32K; provider is interrupted at 40K and the distinct partial-result status is retained | background reconciler + usage snapshot |
| Artifact import error | completed outcome remains; successful files stay visible and terminal artifact listing retries missing files | originating provider session + idempotent per-file import |
| Feature rollback | Work navigation disappears and routes reject new control operations | server feature flag |

No DB transaction is held while waiting on Anthropic, MCP, OAuth, S3, or SSE.
No single API process is the run owner. A user who returns later triggers
reconciliation and can recover the remote outcome even when no browser remained
connected during execution.

The application runs an always-on lease-based Cortex reconciliation worker by
default. Provider execution remains remote, while Cortex imports events,
expires approvals, enforces the output ceiling, imports artifacts, and settles
credits without an authenticated browser request. Multiple API processes may
run the loop because PostgreSQL leases serialize each run. Remembered WRITE
grants are stored and enforced, but a dedicated settings UI/API for reviewing
and revoking them is not yet included.

## Security and retention

All identifiers are opaque but never treated as authorization. Remote MCP URLs
must use public HTTPS on port 443, may not contain credentials, and are resolved
server-side to reject local/private/reserved addresses. OAuth state is hashed,
short-lived, single-use, and user-bound. OAuth tokens are stored in AWS Secrets
Manager references; provider vault IDs are opaque configuration, not secrets.

Work rows are retained with the account's billing/audit history. Work artifacts
reuse the existing file deletion/TTL queue. OAuth states and expired leases are
safe maintenance targets. Before high-volume production, the owner must set an
archive/retention policy for `work_events` and tool-call summaries that preserves
records needed for billing, approval audit, and incident investigation.

## Canonical implementation map

- React: `frontend-react/src/pages/WorkPage.tsx`, `components/work/`,
  `api/work.ts`, `store/workStore.ts`
- HTTP contracts: `server/routes/work.py`, `server/routes/tools.py`,
  `server/schemas/work.py`
- Application/runtime: `server/work/`
- Provider boundary: `server/work/provider.py`,
  `server/work/anthropic_provider.py`
- Persistence: `db/work_repository.py`, `db/tables.py`,
  `db/migrations/20260820_add_cortex_work_mode.sql`,
  `db/migrations/20260829_add_work_web_output_and_model_identity.sql`
- Plans: `config/subscription_plans.yaml`
- Operations: `docs/runbooks/cortex-work.md`,
  `docs/work/00-infrastructure-readiness.md`

The adapter follows the current Managed Agents beta contract and the MCP client
uses Streamable HTTP protocol `2025-06-18`. Re-verify both primary specifications
before a provider SDK or connector-protocol upgrade:

- https://platform.claude.com/docs/en/managed-agents/quickstart
- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
