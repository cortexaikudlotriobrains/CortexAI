# Logging Documentation

## Overview

The API uses structured JSON logging (`utils/logger.py`) designed for Linux/EC2 operations:

- request correlation via `X-Request-ID` + async-safe context propagation
- configurable file/stdout log destinations
- rotating file logs to control disk usage
- structured event names (`event`) for filtering and alerting

Base fields on every log line:

- `timestamp`
- `level`
- `logger`
- `message`
- `module`
- `function`
- `line`

Application-specific fields are attached through `extra={"extra_fields": {...}}`.

## Destination Strategy

`LOG_DESTINATION` controls where logs go:

- `file`: write rotating files only (default)
- `stdout`: write JSON lines to stdout only
- `both`: write to rotating files and stdout

Defaults:

- file logs under `<repo>/logs/`
- `app.log` (INFO+)
- `error.log` (ERROR+)
- `debug.log` (DEBUG+, only when `LOG_LEVEL=DEBUG`)

EC2 recommendation:

- `LOG_DESTINATION=both`
- ship stdout with CloudWatch Agent / ECS / systemd collectors
- keep local rotating files for on-box forensics

## Configuration

Environment variables:

```ini
LOG_LEVEL=INFO
LOG_DESTINATION=both
LOG_CONSOLE_LEVEL=INFO
LOG_DIR=logs
LOG_FILE_MAX_BYTES=10485760
LOG_FILE_BACKUP_COUNT=5
LOG_NOISY_LIBRARIES_LEVEL=WARNING
```

Notes:

- relative `LOG_DIR` values are resolved from repository root
- `LOG_NOISY_LIBRARIES_LEVEL` applies to `httpx`, `urllib3`, `boto3`, `botocore`
- backward compatibility: if `LOG_DESTINATION` is unset, `LOG_TO_CONSOLE=true` implies `LOG_DESTINATION=both`

## Request Correlation

- Incoming `X-Request-ID` is accepted when it matches `[A-Za-z0-9._:-]{1,128}`.
- Invalid/missing IDs are replaced with generated UUIDs.
- Response always includes `X-Request-ID`.
- Request ID is injected into logs through context filter, so nested service logs automatically correlate.

HTTP lifecycle events:

- `http.request.start`
- `http.request.complete`
- `http.request.exception`

## Event Families

Tavily / research:

- `research.provider.selected`
- `research.init.failed` (ERROR when configured research cannot start, e.g. missing dependency)
- `research.init.unavailable` (WARNING when research is not configured/available)
- `research.network.diagnostics`
- `research.cache.hit|bypass`
- `research.dispatch`
- `research.query.rewritten`
- `research.search.resolver`
- `research.resolver.config.warning`
- `research.search.start|success|failure|empty`
- `research.qna.start|success|failure`
- `research.context.ready|failure`

Tavily resolver logs include:

- enhanced resolver state (`enhanced_search_enabled`, `chunks_per_source`)
- category and option decisions (`category`, `topic_sent`, `time_range`, `country_detected`, `country_sent`, `domain_rule`, `include_domain_count`)
- on search success, result summary fields (`result_count`, `source_content_lengths`, `credits_used`, `credits_estimated`)

Tavily failure logs include:

- `error_kind` classifier (`dns_resolution_failed`, `timeout`, `proxy_error`, `auth_forbidden`, `rate_limited`, etc.)
- exception chain type list (`error_chain_types`)
- latest network diagnostic snapshot (`network_diag_status`, `network_diag_age_ms`)

Attachments and object storage:

- `upload.route.received|payload.read|success|rejected|rejected.empty_body|exception`
- `upload.route.rejected.auth_mode` (API-key auth blocked on session-scoped attachment routes)
- `chat.route.rejected.auth_mode` (API-key auth blocked on session-scoped chat routes)
- `compare.route.rejected.auth_mode` (API-key auth blocked on session-scoped compare routes)
- `upload.received|deduplicated|completed`
- `upload.storage.put.start|success|failure`
- `upload.metadata.persisted`
- `upload.ingestion.start|success|failure|deferred`
- `upload.ingestion.deferred.start|read_failure|finalized`
- `upload.auth.resolved`
- `storage.client.initialized`
- `storage.put.start|success|failure`
- `storage.get.success|failure`
- `storage.delete.success|failure`

Governance and preflight:

- `usage.cap.checked|exceeded`
- `rate_limit.checked`
- `api.preflight.success|rejected`
- `byok.resolve.success`

Provider orchestration and resilience:

- `provider.call.timeout|exception`
- `circuit.failure.recorded`
- `circuit.transition.open|closed`
- `circuit.open.blocked`
- `circuit.success.reset`
- `circuit.reset.all`

Streaming request bodies:

- `chat.stream.opened|start_event_sent|activity|provider_call_started|provider_call_completed|response_done_sent|done_sent|exception|client_disconnected`
- `compare.stream.opened|start_event_sent|activity|provider_call_started|provider_call_completed|response_done_sent|done_sent|exception|client_disconnected`

Stream logs are emitted from inside the `StreamingResponse` body generator, after
HTTP `200` headers may already have been returned. They include `request_id`,
elapsed time, emitted event count, estimated bytes emitted, research mode, and
provider/model fields when available. Every visible normalized progress event is
also logged as `*.stream.activity` with `activity_type`, `sequence`, and optional
`target_index`. Terminal stream records include request/provider/first-event,
first-reasoning/search/answer, answer-complete, and request-complete timestamps,
plus time to first activity, time to first useful output, and total duration.

Authentication diagnostics:

- `auth.failed`
- `auth.config.missing`

`auth.*` logs include request method/path and auth-header presence booleans.

## Privacy and Safety

- Auth-bearing headers are redacted before auth logging (`X-API-Key`, `Authorization`).
- Sensitive text is not logged in clear form for research/upload observability paths:
  - Tavily prompt/query fields are hashed (`prompt_hash`, `query_hash`)
  - filenames/object keys are hashed (`filename_hash`, `object_key_hash`, `storage_key_hash`)
- Activity telemetry never includes hidden reasoning text, raw tool arguments,
  provider exception bodies, or provider-only identifiers. Public activity
  metadata is allowlisted by `models/cortex_activity.py`.
- Client-facing attachment errors remain sanitized; detailed failures are available in server logs.
- Upload route logs include CloudFront/proxy context fields when forwarded by infrastructure (`X-Amz-Cf-Id`, `X-Forwarded-*`, viewer hints) to speed edge-origin triage.
- Direct upload control-plane logs distinguish `upload.intent.created`,
  `upload.completion.pending|rejected|verified|idempotent`,
  `upload.ingestion.*`, `upload.status.lookup.success`, and
  `upload.cleanup.worker.started|cycle.completed|cycle.failed`. The browser-to-S3
  byte transfer is intentionally absent from server logs; use browser network
  evidence and approved S3 object data events without copying signed form fields.

## EC2 Troubleshooting Workflow

1. Capture `X-Request-ID` from API response.
2. Filter by request id in file logs:
```bash
grep '"request_id":"<request-id>"' logs/app.log
```
3. If using stdout shipping, filter in system logs:
```bash
journalctl -u cortexai -f | grep '"request_id":"<request-id>"'
```
4. Follow event progression:
   - `http.request.start`
   - preflight/governance events
   - research/upload/provider events
   - stream body events for `/v1/chat/stream` and `/v1/compare/stream`
   - `http.request.complete` (or `http.request.exception`)

Streaming failure interpretation:

- No `*.stream.opened`: the request likely died before the app began streaming.
- `*.stream.start_event_sent` followed by `*.stream.client_disconnected`: browser, proxy, CDN, or load balancer closed the stream.
- `*.stream.activity` stops before `answer_started`: inspect the last
  `activity_type` and provider timing fields to separate research/provider delay
  from output delivery delay.
- Long gap between `*.stream.provider_call_started` and `*.stream.provider_call_completed`: provider latency or an idle timeout is likely.
- `*.stream.done_sent` in app logs but browser reports a protocol error: inspect CDN/proxy HTTP/2 framing, buffering, compression, and timeout behavior.

## Related Files

- `utils/logger.py`
- `server/middleware.py`
- `server/stream_observability.py`
- `server/persistence.py`
- `server/files_service.py`
- `server/object_storage.py`
- `server/circuit_breaker.py`
- `tools/web/tavily_client.py`
- `tools/web/tavily_service.py`
- `docs/runbooks/aws-ec2-logging.md`

---

Last updated: 2026-05-23
