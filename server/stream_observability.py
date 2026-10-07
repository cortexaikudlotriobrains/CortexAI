"""Structured lifecycle logging for NDJSON streaming responses."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


class StreamLogContext:
    """Track stream body progress after StreamingResponse has returned headers."""

    def __init__(
        self,
        *,
        stream_name: str,
        request_id: str,
        provider: str = "",
        model: str = "",
        research_mode: bool | None = None,
        target_count: int | None = None,
        request_group_id: str = "",
    ) -> None:
        self.stream_name = str(stream_name or "stream").strip() or "stream"
        self.request_id = str(request_id or "").strip()
        self.provider = str(provider or "").strip()
        self.model = str(model or "").strip()
        self.research_mode = research_mode
        self.target_count = target_count
        self.request_group_id = str(request_group_id or "").strip()
        self.started = time.perf_counter()
        self.started_at = datetime.now(timezone.utc)
        self.events_sent = 0
        self.bytes_sent_estimate = 0
        self.activity_elapsed_ms: dict[str, int] = {}
        self.provider_started_elapsed_ms: int | None = None
        self.first_provider_event_elapsed_ms: int | None = None

    def mark_provider_started(self) -> None:
        if self.provider_started_elapsed_ms is None:
            self.provider_started_elapsed_ms = self._elapsed_ms()

    def mark_provider_event(self) -> None:
        if self.first_provider_event_elapsed_ms is None:
            self.first_provider_event_elapsed_ms = self._elapsed_ms()

    def mark_activity(self, event_type: str) -> None:
        """Record the first occurrence of a normalized lifecycle event."""

        normalized = str(event_type or "").strip().upper()
        if not normalized or normalized in self.activity_elapsed_ms:
            return
        self.activity_elapsed_ms[normalized] = self._elapsed_ms()

    def record_event(self, payload: str | bytes) -> str | bytes:
        self.events_sent += 1
        if isinstance(payload, bytes):
            self.bytes_sent_estimate += len(payload)
        else:
            self.bytes_sent_estimate += len(str(payload).encode("utf-8"))
        return payload

    def log(
        self, suffix: str, *, level: str = "info", terminal_reason: str = "", **fields: Any
    ) -> None:
        extra_fields: dict[str, Any] = {
            "event": f"{self.stream_name}.stream.{suffix}",
            "request_id": self.request_id,
            "elapsed_ms": int((time.perf_counter() - self.started) * 1000),
            "events_sent": self.events_sent,
            "bytes_sent_estimate": self.bytes_sent_estimate,
        }
        if self.provider:
            extra_fields["provider"] = self.provider
        if self.model:
            extra_fields["model"] = self.model
        if self.research_mode is not None:
            extra_fields["research_mode"] = bool(self.research_mode)
        if self.target_count is not None:
            extra_fields["target_count"] = int(self.target_count)
        if self.request_group_id:
            extra_fields["request_group_id"] = self.request_group_id
        if terminal_reason:
            extra_fields["terminal_reason"] = str(terminal_reason)
            extra_fields.update(self._timing_fields())

        for key, value in fields.items():
            if value is None or value == "":
                continue
            extra_fields[key] = value

        message = f"{self.stream_name} stream {suffix}"
        if level == "warning":
            logger.warning(message, extra={"extra_fields": extra_fields})
        elif level == "error":
            logger.error(message, extra={"extra_fields": extra_fields})
        elif level == "exception":
            logger.exception(message, extra={"extra_fields": extra_fields})
        else:
            logger.info(message, extra={"extra_fields": extra_fields})

    def _timing_fields(self) -> dict[str, Any]:
        first_activity = self._first_elapsed(
            "THINKING_STARTED",
            "SEARCH_STARTED",
            "FILE_SEARCH_STARTED",
            "FILE_READING_STARTED",
            "TOOL_STARTED",
            "MCP_TOOL_STARTED",
            "CODE_EXECUTION_STARTED",
            "ANALYZING_RESULTS",
            "ANSWER_STARTED",
            "ANSWER_DELTA",
        )
        first_answer = self.activity_elapsed_ms.get("ANSWER_DELTA")
        total_ms = int((time.perf_counter() - self.started) * 1000)
        result: dict[str, Any] = {
            "request_started_at": self._iso_at(0),
            "total_response_time_ms": total_ms,
        }
        timestamp_fields = {
            "provider_started_at": self.provider_started_elapsed_ms,
            "first_provider_event_at": self.first_provider_event_elapsed_ms,
            "first_reasoning_event_at": self.activity_elapsed_ms.get("THINKING_STARTED"),
            "first_search_event_at": self._first_elapsed("SEARCH_STARTED", "FILE_SEARCH_STARTED"),
            "first_answer_token_at": first_answer,
            "answer_completed_at": self.activity_elapsed_ms.get("ANSWER_COMPLETED"),
            "request_completed_at": self._first_elapsed(
                "REQUEST_COMPLETED", "REQUEST_FAILED", "REQUEST_CANCELLED"
            ),
        }
        for key, elapsed in timestamp_fields.items():
            if elapsed is not None:
                result[key] = self._iso_at(elapsed)
        if first_activity is not None:
            result["time_to_first_activity_ms"] = first_activity
        if first_answer is not None:
            result["time_to_first_token_ms"] = first_answer
            result["time_to_first_useful_output_ms"] = first_answer
        return result

    def _first_elapsed(self, *event_types: str) -> int | None:
        values = [
            self.activity_elapsed_ms[event_type]
            for event_type in event_types
            if event_type in self.activity_elapsed_ms
        ]
        return min(values) if values else None

    def _elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started) * 1000)

    def _iso_at(self, elapsed_ms: int) -> str:
        return (
            (self.started_at + timedelta(milliseconds=elapsed_ms))
            .isoformat()
            .replace("+00:00", "Z")
        )
