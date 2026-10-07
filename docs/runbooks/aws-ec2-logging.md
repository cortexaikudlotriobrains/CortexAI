# AWS EC2 Linux Logging Runbook

## Purpose

This runbook describes where to find CortexAI logs on AWS EC2 Linux hosts and how to trace incidents end-to-end.

It is optimized for:

- file upload failures (`POST /v1/files/upload`)
- Tavily/web-search connectivity issues
- CloudFront/WAF vs origin diagnosis

## Time And Correlation Basics

- CloudFront access logs are in UTC.
- CortexAI structured logs use UTC timestamps (`timestamp` field).
- Always carry one of these IDs through triage:
  - `request_id` (API response header `X-Request-ID`)
  - CloudFront `x-edge-request-id`
  - CloudFront forwarded request header `X-Amz-Cf-Id` (if forwarded)

## Logging Configuration To Use In EC2

Recommended environment values:

```ini
LOG_LEVEL=INFO
LOG_DESTINATION=both
LOG_CONSOLE_LEVEL=INFO
LOG_DIR=logs
LOG_NOISY_LIBRARIES_LEVEL=WARNING
```

Tavily diagnostics (recommended):

```ini
TAVILY_NETWORK_DIAGNOSTICS_HOST=api.tavily.com
TAVILY_NETWORK_DIAGNOSTICS_PORT=443
TAVILY_NETWORK_DIAGNOSTICS_INTERVAL_SECONDS=300
TAVILY_NETWORK_DIAGNOSTICS_TIMEOUT_SECONDS=2
```

## Where Logs Are Stored

### 1) CortexAI file logs

With `LOG_DESTINATION=file|both`, logs are written under repo-root `logs/`:

- `logs/app.log` (INFO+)
- `logs/error.log` (ERROR+)
- `logs/debug.log` (DEBUG+, only when `LOG_LEVEL=DEBUG`)

Example path on EC2:

- `/home/ubuntu/OpenAIProject/logs/app.log`

### 2) Systemd/journald logs (stdout/stderr)

With `LOG_DESTINATION=stdout|both`, JSON logs are visible in journal.

Common commands:

```bash
sudo systemctl status cortexai
sudo journalctl -u cortexai -n 200 --no-pager
sudo journalctl -u cortexai -f
```

Time-window example (UTC):

```bash
sudo journalctl -u cortexai \
  --since "2026-04-14 01:00:00 UTC" \
  --until "2026-04-14 01:10:00 UTC" \
  --no-pager
```

### 3) Docker logs (if containerized)

```bash
docker ps
docker logs <container_name_or_id> --since 30m
docker logs -f <container_name_or_id>
```

### 4) Nginx / reverse proxy logs (if used)

Common locations:

- `/var/log/nginx/access.log`
- `/var/log/nginx/error.log`

## CloudFront/WAF vs Origin: Fast Decision Tree

1. Check CloudFront log line.
2. Search CortexAI for `upload.route.received` (or `http.request.start`) around same UTC timestamp.
3. Interpret result:
   - If not found: blocked before origin (CloudFront behavior, WAF rule, edge policy).
   - If found: request reached app; continue with application event chain.

If CloudFront ID forwarding is enabled, correlate directly with:

- request header: `X-Amz-Cf-Id`
- app field: `edge_headers.x_amz_cf_id` in `upload.route.received`

## Streaming Incident Workflow (`POST /v1/chat/stream`, `/v1/compare/stream`)

Use this when browsers report errors such as `ERR_HTTP2_PROTOCOL_ERROR 200 (OK)`.

1. Capture the browser console request id and server `X-Request-ID`.
2. Search app logs for `chat.stream.*` or `compare.stream.*` with that `request_id`.
3. Interpret the stream body event chain:
   - no `*.stream.opened`: failure happened before the app began streaming.
   - `*.stream.start_event_sent` then `*.stream.client_disconnected`: browser, CDN, load balancer, or proxy closed the stream.
   - repeated `*.stream.heartbeat_sent` before completion: provider work was still running and the backend kept the response body active.
   - inspect ordered `*.stream.activity` records by `sequence`, `activity_type`, and optional `target_index`; a Compare target may fail while later indexed targets continue normally.
   - if activity stops before `answer_started`, compare `provider_started_at`, `first_provider_event_at`, `first_search_event_at`, and `first_answer_event_at`; if answer activity began but the browser has no useful text, inspect line delivery and `time_to_first_useful_output_ms`.
   - long gap between `*.stream.provider_call_started` and `*.stream.provider_call_completed` with no heartbeat events: provider latency or idle timeout is likely.
   - `*.stream.done_sent` exists but browser failed: inspect CloudFront/Nginx/ALB HTTP/2 framing, buffering, compression, and read/send timeouts.

Activity logs and public stream envelopes intentionally omit hidden reasoning,
raw tool arguments, provider exception bodies, and provider-only identifiers.
Use correlated provider/research logs for diagnostics; do not expand public
activity metadata while investigating an incident.

## Browser Refresh / Blink Workflow

Use this when the React page appears to refresh without the user pressing reload.

1. Search app logs for `frontend.diagnostic` near the user-reported timestamp.
2. Interpret the latest `boot` event:
   - `details.wasDiscarded=true`: Chrome discarded or recovered the tab.
   - `details.navigationType="reload"` with a recent `beforeunload`: user/browser reload or app-initiated navigation.
   - `details.navigationType="navigate"` with `fresh_login` in `details.searchKeys`: Cognito/login redirect path.
   - prior `longtask` events: main-thread saturation before the reload.
   - prior `error` or `unhandledrejection`: frontend runtime failure; inspect the sanitized message and current release artifact.
3. Correlate the same timestamp with `chat.stream.*` or `compare.stream.*` request logs. If `client_disconnected` appears immediately before the next `boot`, inspect CDN/proxy/browser behavior before assuming the backend completed normally.

### Pattern: Cognito auth redirect loop (repeating `fresh_login` boot events)

Symptoms:

- Multiple `boot` events in `frontend.diagnostic` within seconds of each other.
- Each `boot` has `details.navigationType="navigate"` and `details.searchKeys` includes `fresh_login`.
- No successful `/v1/whoami` in between (or 401 responses).

Root cause:

The token exchange between the backend and Cognito is failing (usually redirect URI mismatch).
When the backend runs behind CloudFront or an ALB without proxy header support enabled, `request.base_url` returns `http://…`.
The computed `redirect_uri` (`http://…/auth`) does not match the `https://…/auth` registered in the Cognito App Client, so every token exchange fails and the browser is redirected back to the login page immediately.

Remediation:

1. Set `COGNITO_REDIRECT_URI=https://app.example.com/auth` explicitly in the environment.
2. Ensure `ENABLE_PROXY_HEADERS=true` (default) so the server trusts `X-Forwarded-Proto` from CloudFront/ALB.
3. Confirm `request.base_url` resolves as HTTPS by checking the `auth.dev_login.*` or `http.request.start` log events for the `/auth` path and verifying the scheme.

### Pattern: Automatic refresh on mobile / tab resume

Symptoms:

- `boot` event immediately after `pagehide` with `details.persisted=false` or after a long gap (tab was discarded).
- `details.wasDiscarded=true` in the new `boot` event.
- User appears logged out after resume.

Root cause:

The `cortex_session` cookie had no `max_age` (was a session cookie) and was cleared when the browser session ended or Chrome discarded the low-memory tab. On desktop browsers that restore tabs across restarts, the session cookie is lost. On mobile, background→foreground transitions frequently clear session state.

Remediation:

- Verify `SESSION_MAX_AGE_SECONDS` is set (default 604800 = 7 days).
- If users are still losing sessions after tab restores, increase the value or investigate browser cookie storage policies.

### Pattern: Safari auto-reload every 8–10 minutes (bfcache eviction)

Symptoms (confirmed in production logs, July 2026):

- Repeated `boot` events with `details.navigationType="reload"` on a roughly 8–10 minute cadence.
- Each `boot` is preceded by `pagehide` with `details.persisted=false`, fired **while `visibilityState` is already "hidden"** (i.e. the tab was in the background when the browser killed it).
- `details.wasDiscarded=false` — Chrome's tab discard API is not involved; this is Safari's own background-tab eviction.
- User agent is Safari/WebKit (`AppleWebKit/605.x`).
- After each reload, `/v1/whoami` returns 200 — the session is still valid.

Root cause:

Any `beforeunload` event listener — even a no-op — makes the page ineligible for the browser's back/forward cache (bfcache). Without bfcache, Safari cannot suspend a backgrounded tab cheaply; instead it keeps the tab's full JavaScript context in memory. Under background memory pressure (typically after 8–10 minutes with no user interaction), Safari evicts the tab and triggers a full reload when the user switches back. The `beforeunload` event fires in the background just before eviction, which is why `pagehide.persisted=false` is always observed.

The `beforeunload` listener that previously existed in `bootDiagnostics.ts` was the trigger. **This listener has been removed as of the July 2026 fix.** `pagehide` covers all the same unload events and is bfcache-compatible.

How to verify the fix is in effect:

Search `frontend.diagnostic` logs for the next `pagehide` events:
- `details.persisted=true` → page entered bfcache successfully; Safari will restore without a reload.
- `details.persisted=false` → page did not enter bfcache; investigate other bfcache blockers (open IndexedDB transactions, `Cache-Control: no-store` on the HTML, unresolved `fetch()` calls, etc.).

Remediation if reloads persist after the fix is deployed:

1. Check browser developer tools → Application → Back/Forward Cache for "Not eligible" reasons.
2. Ensure the HTML response does not set `Cache-Control: no-store` (it currently sets `no-cache`, which is bfcache-safe).
3. Ensure streaming fetch responses are not left open when the page goes to background.
4. Consider adding a `pageshow` handler that re-initializes auth state when `event.persisted=true` (bfcache restore), to keep session freshness without a reload.

## Upload Incident Workflow (`POST /v1/files/upload`)

### Step 1: confirm route arrival

Search for:

- `event=upload.route.received`

Key fields:

- `auth_mode`
- `has_x_api_key_header`
- `has_authorization_header`
- `content_length_header`
- `edge_headers` (CloudFront/proxy context)

### Step 2: check payload parsing

Search for:

- `event=upload.route.payload.read`

Key fields:

- `payload_size_bytes`
- `content_length_matches_payload`

### Step 3: find rejection/success point

Possible terminal route events:

- `upload.route.success`
- `upload.route.rejected`
- `upload.route.rejected.empty_body`
- `upload.route.exception`

Service-level validation events:

- `upload.feature.disabled`
- `upload.db.disabled`
- `upload.validation.invalid_mime`
- `upload.validation.unsupported_mime`
- `upload.validation.empty_payload`
- `upload.validation.file_too_large`
- `upload.auth.resolved`

Auth-related failures:

- `auth.failed`
- `auth.config.missing`

## Tavily/Web Search Incident Workflow

### Core Tavily events

- `research.dispatch`
- `research.search.resolver`
- `research.search.start|success|failure|empty`
- `research.qna.start|success|failure`
- `research.context.ready|failure`

Resolver fields to check when search quality looks poor:

- `enhanced_search_enabled`
- `chunks_per_source`
- `category`
- `topic_sent`
- `time_range`
- `country_detected`
- `country_sent`
- `domain_rule`
- `include_domain_count`
- `source_content_lengths`, `credits_used`, and `credits_estimated` on `research.search.success`

### Network diagnostics event

- `research.network.diagnostics`

Key fields:

- `status` (`ok` or `degraded`)
- `diagnostic_host`
- `diagnostic_port`
- `dns_ok`, `dns_latency_ms`, `dns_error_type`
- `tcp_ok`, `tcp_latency_ms`, `tcp_error_type`, `tcp_error_errno`
- proxy flags: `http_proxy_present`, `https_proxy_present`, `no_proxy_present`

### Tavily failure classification

On `research.search.failure` and `research.qna.failure`, use:

- `error_kind`
- `error_type`
- `error_message`
- `error_chain_types`
- `network_diag_status`

Common `error_kind` values:

- `dns_resolution_failed`
- `timeout`
- `connection_refused`
- `proxy_error`
- `tls_error`
- `auth_forbidden`
- `rate_limited`
- `provider_server_error`

## Handy Linux Commands (JSON Logs)

### Filter app.log by event

```bash
grep '"event":"upload.route.rejected"' logs/app.log | tail -n 50
```

### Filter by request id

```bash
grep '"request_id":"<request-id>"' logs/app.log
```

### jq filtering

```bash
jq -c 'select(.event=="research.search.failure")' logs/app.log | tail -n 50
```

```bash
jq -c 'select(.event=="research.network.diagnostics" and .status=="degraded")' logs/app.log
```

### Combined upload chain view

```bash
jq -c 'select(.request_id=="<request-id>") | {timestamp,event,status_code,error_code,error_kind,payload_size_bytes,content_length_header}' logs/app.log
```

## CloudWatch Logs Insights Query Examples

If journald/stdout is shipped to CloudWatch, adapt field names to your log group parser.

Upload failures:

```sql
fields @timestamp, event, request_id, status_code, error_code, path, auth_mode
| filter event like /upload\.route\.(rejected|exception)/ or event like /upload\.validation\./
| sort @timestamp desc
| limit 200
```

Tavily network failures:

```sql
fields @timestamp, event, error_kind, error_type, network_diag_status, dns_ok, tcp_ok, tcp_error_errno
| filter event in ["research.search.failure","research.qna.failure","research.network.diagnostics"]
| sort @timestamp desc
| limit 200
```

## Practical Root-Cause Patterns

### Pattern: CloudFront 403 with no origin logs

Symptoms:

- CloudFront shows `sc-status=403`
- no `upload.route.received` in app logs

Likely layer:

- WAF rule, CloudFront behavior/policy, edge-level block.

### Pattern: `auth.failed` then `upload.route.rejected`

Symptoms:

- request reached origin but auth header missing/invalid.

Likely layer:

- forwarded header policy (missing `X-API-Key` / `Authorization`) or client auth bug.

### Pattern: Tavily failures with `dns_resolution_failed`

Symptoms:

- `research.search.failure` with `error_kind=dns_resolution_failed`
- `research.network.diagnostics` has `dns_ok=false`

Likely layer:

- EC2 DNS resolver/VPC DNS/security egress restrictions.

### Pattern: Tavily failures with `timeout` and diagnostics `tcp_ok=false`

Likely layer:

- SG/NACL/route egress restrictions, proxy misconfiguration, or external network path.

## What To Capture In Incident Tickets

- UTC timestamp window.
- `request_id` and/or `x-edge-request-id`.
- `event` sequence for that request.
- relevant fields:
  - upload: `status_code`, `error_code`, `auth_mode`, `content_length_matches_payload`, `edge_headers.x_amz_cf_id`
  - Tavily: `error_kind`, `dns_ok`, `tcp_ok`, `tcp_error_errno`, proxy flags
- whether request reached origin (`upload.route.received` present or absent).

---

Last updated: 2026-05-23
