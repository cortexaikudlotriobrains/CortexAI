# CortexAI Work infrastructure readiness gate

Date: 2026-08-20  
Branch baseline: `Cortex-Work` at `ebe9a6cdb2a1a0766e321a6a576c079ba5ae299c`, identical to `origin/Subscription-Model` when this audit was performed.

## Decision

The repository is ready for an additive Work implementation, but a production rollout is **not approved from repository evidence alone**. The repository contains the application containers, PostgreSQL contract, private-S3 direct-upload implementation, health endpoints, structured logging, and deployment runbooks. It intentionally contains no Terraform, CloudFormation, CDK, or other source of truth for the live EC2/ALB/CloudFront/WAF/IAM/Secrets Manager topology. The production owner must capture the unresolved evidence below before enabling Work.

The architecture keeps long-running execution at Anthropic Managed Agents. FastAPI creates/reconciles provider sessions, persists normalized state in PostgreSQL, and streams durable database events to browsers. A browser connection, Uvicorn process, or EC2 instance is never the owner of a run. No Redis, Celery, Temporal, Kubernetes, or new worker platform is required for V1.

## 1. Existing infrastructure reused unchanged

- PostgreSQL remains the system of record. Work uses additive tables and short transactions; provider calls, S3 transfers, and SSE waits never hold database transactions open.
- Existing `sessions` rows remain the common history/navigation identity. Work adds `mode=work` and specialized Work tables rather than a second session system.
- The existing private S3 attachment bucket, presigned POST control plane, `uploaded_files` metadata, ingestion, deletion queue, and object-storage abstraction are reused for Work inputs and imported artifacts.
- Cognito/session-cookie authentication, request ownership resolution, `X-Request-ID`, plan resolution, unified AI-credit ledger, health endpoints, JSON logging, React/Vite build, and current `/v1/*` API routing boundary are reused.
- The API remains a modular FastAPI application. `Dockerfile.api` and `run_server.py` currently start one Uvicorn process per container/host invocation; a production process manager may run more than one process, but Work correctness does not depend on a singleton process.

## 2. Required configuration changes

- Add the Work feature flags and provider settings documented in `.env.example`. Keep `CORTEX_WORK_ENABLED=false` until database migration, provider resources, edge behavior, monitoring, and smoke tests pass.
- Configure the official Anthropic Managed Agents identifiers and API credential through the deployment secret mechanism. Do not use a Claude Code or Cowork subprocess.
- Create a cache-disabled CloudFront behavior that routes `/v1/work/*` and `/v1/tools/*` to the API origin and forwards authorization/session cookies, required request headers, query strings, and `Last-Event-ID`.
- For `text/event-stream`, disable response buffering/caching/transformation. If an API-side nginx proxy exists outside this repository, set `proxy_buffering off`, `proxy_cache off`, a read timeout above the edge idle timeout, and preserve `X-Accel-Buffering: no`. The tracked `nginx.conf` serves static React assets only and is not an API reverse proxy.
- Emit an application SSE heartbeat every 15 seconds by default. CloudFront's origin response timeout measures the gap between response packets, so the heartbeat must remain below the narrowest CloudFront/ALB/nginx timeout. Do not set a finite CloudFront response-completion timeout for the SSE behavior; AWS documents that an unset completion timeout has no maximum. Verify the live configuration rather than relying on defaults.
- If an ALB is present, retain or raise its idle timeout above the heartbeat interval and verify target deregistration/draining. AWS documents a 60-second default idle timeout and notes that HTTP/2 PING frames do not reset it; Work sends actual SSE bytes.

## 3. AWS permissions

The production workload identity is unresolved in repository evidence. Record `aws sts get-caller-identity` for the API workload role and attach only:

- the existing attachment-prefix S3 permissions already required by the direct-upload runbook (`PutObject`, `GetObject`, and queued `DeleteObject`, plus KMS permissions only when SSE-KMS is used);
- `secretsmanager:GetSecretValue` for the exact Cortex Work secret ARNs if Secrets Manager injection/runtime lookup is used;
- CloudWatch log/metric permissions only for the selected agent/collector;
- no browser IAM credentials and no public S3 access.

Anthropic Managed Agents is an external HTTPS API; confirm outbound TCP 443/DNS from the API subnets and restrict egress according to the organization's approved proxy/firewall policy. No inbound provider connection is required unless the optional signed Anthropic webhook endpoint is enabled.

## 4. Secret handling

- Store `ANTHROPIC_API_KEY`, OAuth client secrets, OAuth refresh tokens, and any connector credentials in the existing deployment secret channel (prefer Secrets Manager or secure environment injection). Never persist raw credentials in Work tables, logs, events, frontend runtime config, or provider snapshots.
- Database rows contain opaque `credential_reference`, provider vault IDs, and redacted metadata only.
- OAuth uses a short-lived, single-use, user-bound state record; callback URLs are allowlisted per configured connector.
- The React build receives feature availability and non-secret catalog metadata only. `.env.example` contains placeholders, never real values.

## 5. Reverse-proxy and SSE implications

- `GET /v1/work/runs/{run_id}/events` is the durable catch-up source; `GET /stream` is an optimization. Reconnect uses the stored sequence/`Last-Event-ID`, so edge or browser disconnects do not lose progress.
- Start/follow-up/approve/deny/cancel are short control requests with idempotency keys. Provider execution continues remotely after the response.
- Return `Cache-Control: no-cache, no-transform`, `Content-Type: text/event-stream`, `Connection: keep-alive`, and `X-Accel-Buffering: no`; flush a comment/heartbeat at least every 15 seconds.
- CloudFront origin response timeout defaults to 30 seconds and is measured between packets. Set it above the heartbeat interval for the API origin. Leave response-completion timeout unset for the SSE behavior. If an ALB is present, set its idle timeout above the same heartbeat margin.
- A rolling deploy may terminate a live SSE connection; the browser reconnects and catches up from PostgreSQL. Graceful shutdown need only drain current HTTP requests, not wait for provider runs to finish.

## 6. WAF implications

- Keep WAF enabled for all Work routes. Work control JSON and SSE requests are small; there is no basis for a blanket body-size exception.
- Enable WAF logging and add a route-specific exception only when the exact terminating rule and label prove a false positive. Scope any exception to the precise method/path and retain rate limits and authentication protections.
- File bytes continue to go directly from the browser to private S3. They must not traverse CloudFront/WAF/FastAPI. Work artifact downloads use short-lived authorized URLs or the existing authenticated file route.
- Provider webhooks, if enabled, require HTTPS, signature verification on the raw body, replay protection, and a narrow request-size/rate policy.

## 7. S3 implications

- Reuse `ATTACHMENTS_S3_BUCKET`, region, prefix, encryption policy, and workload role. Work inputs reference existing `uploaded_files`; no second upload bucket is introduced.
- Import provider artifacts into the existing object namespace and create normal `uploaded_files` metadata so existing ownership, lifecycle, download, deletion, and retention behavior applies.
- Do not proxy large input or artifact bytes through the Work API. Stream provider downloads into S3 with bounded memory/disk use and verify size/type before finalizing metadata.
- Re-run the production checks in `docs/runbooks/direct-s3-attachment-rollout.md`: Block Public Access, exact CORS origin/methods, bucket/IAM/KMS policy, presigned constraints, lifecycle, audit events, and cleanup-owner evidence.

## 8. Database implications

- Apply both additive Work migrations before the application deploy. The base migration expands the `sessions.mode` constraint and creates Work sessions, runs, ordered events, run files, tool connections, run connections, tool calls, approvals, OAuth state, and synchronization leases. The 2026-08-29 migration adds server-owned output ceilings, finalization/interrupt markers, resolved provider/Agent/billing identity, and the `output_limit_reached` status.
- Unique request/provider event constraints make retries idempotent. Ordered event sequence is allocated in a short row-locked transaction.
- No transaction spans an Anthropic, MCP, OAuth, or S3 call. Active-run reconciliation is lease-based in PostgreSQL so multiple Uvicorn processes cannot become authoritative owners.
- Size indexes for user/session history, active runs, event replay, pending approvals, and connector ownership. Monitor autovacuum, connection-pool saturation, lock waits, and event-table growth. Establish an event-retention/archive decision before production volume, without deleting records required for billing/audit.
- Back up PostgreSQL and verify point-in-time recovery before rollout. A code rollback retains additive Work rows and disables the feature flag; do not roll back by dropping tables.

## 9. Deployment implications

1. Apply the migration and verify schema preflight.
2. Create/configure Anthropic Managed Agent and environment resources; store IDs and secret references.
3. Confirm API egress, S3/IAM/KMS, Cognito callback/session behavior, and exact CloudFront/WAF/ALB values.
4. Deploy API with Work disabled; run `/health`, `/health/runtime`, schema, auth, plan, S3, and fake-provider smoke checks.
5. Deploy the React build and verify that existing Ask/Compare/Research/Optimize/Cortex Analysis/billing flows are unchanged.
6. Enable Work for an internal Pro account, then Plus, while retaining the server-side kill switch.
7. Validate disconnect/reconnect, API restart, multi-process duplicate prevention, approval timeout, cancel, credit settlement, artifact import, connector denial, and feature-flag rollback.

The repository CI builds/tests images but has no production deployment job. The production deployment mechanism, host/service manager, process count, target health check, graceful-stop timeout, CloudFront invalidation, and rollback owner remain deployment-ticket evidence.

## 10. Monitoring and alerting implications

Add structured Work event families and dashboards for:

- run creation, provider-session creation, requested/effective Web mode, resolved provider/billing model identity, status transitions, terminal status, age, and active runs by plan;
- reconciliation claims, background-cycle lag, lease contention, provider poll/stream/webhook failures, duplicate provider events, event replay lag, output-finalize/interrupt counts, output-ceiling overshoot, and SSE reconnects/disconnects;
- approval requested/approved/denied/expired latency and blocked action attempts;
- connector health, OAuth failures, SSRF/allowlist denials, MCP/action-tool calls, and redacted error classes;
- reserved/settled/released credits, budget utilization, credit-limit stops, provider/runtime/token usage, and reconciliation drift;
- artifact download/import/validation/S3 failure and orphan cleanup;
- database pool pressure, lock waits, active-run age, and stale leases.

Alert on provider authentication/configuration failure, oldest active run over the plan/runtime threshold, repeated reconciliation failure, stale approval, unreleased reservation, S3 import failure, SSE 5xx/504 increase, database saturation, and WAF blocks on Work control routes. Correlate every signal by `request_id`, `run_id`, `session_id`, provider session ID hash, and user/account ID hash; never log instructions, file contents, tokens, OAuth values, or raw credentials.

## Recovery matrix

| Failure | Durable truth | Recovery |
|---|---|---|
| Browser closes or network drops | PostgreSQL events + remote provider session | Reopen `/stream` with last sequence; catch up through `/events` |
| API/Uvicorn/EC2 restarts | PostgreSQL run state + remote provider session | The next authenticated run/event/stream request claims a DB lease and lists provider events |
| Multiple API processes reconcile the same run | PostgreSQL lease and unique provider-event IDs | One lease holder performs provider I/O; duplicates become no-ops |
| CloudFront/ALB/nginx closes SSE | PostgreSQL ordered events | Browser reconnects; tune heartbeat/edge timeouts from logs, not by extending run ownership |
| Anthropic returns transient error/rate limit | Persisted run/provider IDs and retry metadata | Bounded backoff; keep run recoverable and surface normalized status |
| Approval times out or is denied | Approval/tool-call rows | On the next reconciliation, record the terminal tool decision and deny the provider tool request |
| Credit budget is exhausted | Billing reservation/ledger + run budget snapshot | Let the provider-enforced cap pause the session, persist `credit_limit_reached`, settle actual usage, release remainder; never emit polling interrupts |
| Artifact import fails | Provider file reference + Work run-file row | Retry idempotently; never expose provider URL or mark the artifact ready early |
| Deployment rollback | Feature flag + additive schema | Disable Work and deploy previous app; retain rows for audit/reconciliation |

## Production evidence still required

- EC2/ECS/service topology, Uvicorn/process-manager command and worker count.
- CloudFront distribution/behavior policies, origin response and completion timeouts, and cache settings.
- ALB target group, health check, idle timeout, deregistration delay, security groups, and subnet egress.
- WAF web ACL rules/log destination and a clean Work-route smoke trace.
- Workload-role ARN and reviewed IAM/KMS/Secrets Manager policies.
- S3 bucket/region/prefix, Block Public Access, CORS, encryption, lifecycle, and audit evidence.
- RDS engine/version, connection limit, backups/PITR, migration runner, and pool sizing multiplied by actual process count.
- Cognito app-client callback/logout URLs and cookie behavior at the production host.
- Production secret names/rotation owners (names only, never secret values).
- Deployment/rollback owner, CloudWatch dashboard/alarms, provider resource IDs, and runbook drill result.

References: [CloudFront origin timeouts](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesOrigin.html), [ALB connection idle timeout](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/edit-load-balancer-attributes.html), [AWS WAF request-body inspection](https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-setting-body-inspection-limit.html), and `docs/runbooks/direct-s3-attachment-rollout.md`.
