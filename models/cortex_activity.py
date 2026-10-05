"""Provider-neutral activity events used by Cortex streaming surfaces.

The public contract intentionally contains only safe, user-meaningful state. Raw
provider payloads, reasoning text, tool arguments, and provider identifiers stay
inside the adapter layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping


class CortexActivityType(StrEnum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    REQUEST_IN_PROGRESS = "REQUEST_IN_PROGRESS"
    THINKING_STARTED = "THINKING_STARTED"
    THINKING_COMPLETED = "THINKING_COMPLETED"
    SEARCH_STARTED = "SEARCH_STARTED"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    FILE_SEARCH_STARTED = "FILE_SEARCH_STARTED"
    FILE_SEARCH_COMPLETED = "FILE_SEARCH_COMPLETED"
    FILE_READING_STARTED = "FILE_READING_STARTED"
    FILE_READING_COMPLETED = "FILE_READING_COMPLETED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    MCP_TOOL_STARTED = "MCP_TOOL_STARTED"
    MCP_TOOL_COMPLETED = "MCP_TOOL_COMPLETED"
    CODE_EXECUTION_STARTED = "CODE_EXECUTION_STARTED"
    CODE_EXECUTION_COMPLETED = "CODE_EXECUTION_COMPLETED"
    ANALYZING_RESULTS = "ANALYZING_RESULTS"
    ANSWER_STARTED = "ANSWER_STARTED"
    ANSWER_DELTA = "ANSWER_DELTA"
    ANSWER_COMPLETED = "ANSWER_COMPLETED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
    REQUEST_COMPLETED = "REQUEST_COMPLETED"
    REQUEST_FAILED = "REQUEST_FAILED"
    REQUEST_CANCELLED = "REQUEST_CANCELLED"


_PHASE_BY_TYPE: dict[CortexActivityType, str] = {
    CortexActivityType.REQUEST_RECEIVED: "starting",
    CortexActivityType.REQUEST_IN_PROGRESS: "preparing",
    CortexActivityType.THINKING_STARTED: "thinking",
    CortexActivityType.THINKING_COMPLETED: "thinking",
    CortexActivityType.SEARCH_STARTED: "searching",
    CortexActivityType.SEARCH_COMPLETED: "reviewing",
    CortexActivityType.FILE_SEARCH_STARTED: "searching_files",
    CortexActivityType.FILE_SEARCH_COMPLETED: "reviewing",
    CortexActivityType.FILE_READING_STARTED: "reading_files",
    CortexActivityType.FILE_READING_COMPLETED: "reviewing",
    CortexActivityType.TOOL_STARTED: "using_tool",
    CortexActivityType.TOOL_COMPLETED: "reviewing",
    CortexActivityType.MCP_TOOL_STARTED: "using_connected_service",
    CortexActivityType.MCP_TOOL_COMPLETED: "reviewing",
    CortexActivityType.CODE_EXECUTION_STARTED: "running_code",
    CortexActivityType.CODE_EXECUTION_COMPLETED: "reviewing",
    CortexActivityType.ANALYZING_RESULTS: "reviewing",
    CortexActivityType.ANSWER_STARTED: "answering",
    CortexActivityType.ANSWER_DELTA: "answering",
    CortexActivityType.ANSWER_COMPLETED: "finalizing",
    CortexActivityType.WAITING_FOR_USER: "waiting",
    CortexActivityType.WAITING_FOR_APPROVAL: "waiting_for_approval",
    CortexActivityType.REQUEST_COMPLETED: "completed",
    CortexActivityType.REQUEST_FAILED: "failed",
    CortexActivityType.REQUEST_CANCELLED: "cancelled",
}

_SAFE_METADATA_KEYS = frozenset(
    {
        "source_count",
        "success",
        "reused",
        "tool_kind",
        "target_index",
    }
)


@dataclass(frozen=True)
class CortexActivitySignal:
    event_type: CortexActivityType
    provider: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def phase(self) -> str:
        return _PHASE_BY_TYPE[self.event_type]

    @property
    def display_message(self) -> str:
        return activity_display_message(self.event_type, self.metadata)


ActivityCallback = Callable[[CortexActivitySignal], None]


def sanitize_activity_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded, non-sensitive values used by the public activity UI."""

    if not metadata:
        return {}
    safe: dict[str, Any] = {}
    for key in _SAFE_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            safe[key] = max(0, value)
        elif isinstance(value, str) and key == "tool_kind":
            safe[key] = _safe_tool_kind(value)
    return safe


def activity_display_message(
    event_type: CortexActivityType,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    if event_type == CortexActivityType.REQUEST_RECEIVED:
        return "Starting…"
    if event_type == CortexActivityType.REQUEST_IN_PROGRESS:
        return "Preparing your response…"
    if event_type == CortexActivityType.THINKING_STARTED:
        return "Thinking…"
    if event_type == CortexActivityType.THINKING_COMPLETED:
        return "Reviewing the result…"
    if event_type == CortexActivityType.SEARCH_STARTED:
        return "Searching the web…"
    if event_type == CortexActivityType.SEARCH_COMPLETED:
        if metadata.get("success") is False:
            return "Search unavailable; continuing…"
        count = metadata.get("source_count")
        if isinstance(count, int) and count > 0:
            return f"Reviewing {count} source{'s' if count != 1 else ''}…"
        return "Reviewing sources…"
    if event_type == CortexActivityType.FILE_SEARCH_STARTED:
        return "Searching your files…"
    if event_type == CortexActivityType.FILE_READING_STARTED:
        return "Reading your files…"
    if event_type in {
        CortexActivityType.FILE_SEARCH_COMPLETED,
        CortexActivityType.FILE_READING_COMPLETED,
        CortexActivityType.TOOL_COMPLETED,
        CortexActivityType.MCP_TOOL_COMPLETED,
        CortexActivityType.CODE_EXECUTION_COMPLETED,
        CortexActivityType.ANALYZING_RESULTS,
    }:
        return "Reviewing the results…"
    if event_type == CortexActivityType.CODE_EXECUTION_STARTED:
        return "Analyzing the data…"
    if event_type == CortexActivityType.MCP_TOOL_STARTED:
        return "Using a connected service…"
    if event_type == CortexActivityType.TOOL_STARTED:
        return safe_tool_activity_message(str(metadata.get("tool_kind") or ""))
    if event_type == CortexActivityType.ANSWER_STARTED:
        return "Writing the answer…"
    if event_type == CortexActivityType.ANSWER_DELTA:
        return "Answering…"
    if event_type == CortexActivityType.ANSWER_COMPLETED:
        return "Finalizing…"
    if event_type == CortexActivityType.WAITING_FOR_USER:
        return "Waiting for your input…"
    if event_type == CortexActivityType.WAITING_FOR_APPROVAL:
        return "Waiting for your approval…"
    if event_type == CortexActivityType.REQUEST_COMPLETED:
        return "Complete"
    if event_type == CortexActivityType.REQUEST_FAILED:
        return "Response failed"
    if event_type == CortexActivityType.REQUEST_CANCELLED:
        return "Generation stopped"
    return "Working…"


def safe_tool_activity_message(tool_name: str, *, mcp: bool = False) -> str:
    """Translate tool identifiers without exposing implementation-specific names."""

    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    if normalized in {"web_search", "search", "tavily", "tavily_search"}:
        return "Searching the web…"
    if normalized in {"web_read", "web_fetch", "fetch_url", "browser"}:
        return "Reading a web page…"
    if normalized in {"file_read", "read", "read_file", "get_file_contents"}:
        return "Reading project files…"
    if normalized in {"file_search", "grep", "glob", "find", "search_files"}:
        return "Searching project files…"
    if normalized in {"python", "code", "code_execution", "shell", "terminal"}:
        return "Running code…"
    if mcp:
        return "Using a connected service…"
    return "Using a tool…"


def _safe_tool_kind(tool_name: str) -> str:
    """Reduce a technical tool identifier to a bounded public category."""

    normalized = str(tool_name or "").strip().lower().replace("-", "_")
    if any(token in normalized for token in ("web_search", "tavily")):
        return "web_search"
    if any(token in normalized for token in ("web_fetch", "fetch_url", "browser")):
        return "web_read"
    if any(token in normalized for token in ("read_file", "get_file_contents")):
        return "file_read"
    if normalized in {"read"}:
        return "file_read"
    if any(token in normalized for token in ("search_files", "grep", "glob")):
        return "file_search"
    if any(token in normalized for token in ("code", "python", "shell", "terminal")):
        return "code"
    return "tool"


def normalize_provider_event(
    provider: str,
    raw_event: Mapping[str, Any] | object,
) -> CortexActivitySignal | None:
    """Compatibility wrapper returning the first normalized safe signal."""

    signals = ProviderEventNormalizer(provider).normalize(raw_event)
    return signals[0] if signals else None


class ProviderEventNormalizer:
    """Stateful provider event translator with no raw-payload passthrough."""

    def __init__(self, provider: str, *, model: str = "") -> None:
        self.provider = str(provider or "").strip().lower()
        self.model = str(model or "").strip() or None
        self._blocks: dict[int, tuple[str, str]] = {}
        self._steps: dict[int, str] = {}

    def normalize(
        self,
        raw_event: Mapping[str, Any] | object,
    ) -> list[CortexActivitySignal]:
        data = _as_mapping(raw_event)
        raw_type = str(
            _field(data, "type")
            or _field(data, "event_type")
            or _field(data, "event")
            or ""
        ).strip().lower()
        model = str(_field(data, "model") or self.model or "").strip() or None

        if self.provider in {"openai", "grok", "xai"}:
            signal = _normalize_openai_compatible(raw_type, self.provider, model)
            return [signal] if signal else []
        if self.provider in {"anthropic", "claude"}:
            return self._normalize_anthropic_event(raw_type, data, model)
        if self.provider in {"gemini", "google"}:
            return self._normalize_gemini_event(raw_type, data, model)
        if self.provider == "deepseek":
            signal = _normalize_deepseek(raw_type, data, self.provider, model)
            return [signal] if signal else []
        return []

    def _normalize_anthropic_event(
        self,
        raw_type: str,
        data: Mapping[str, Any],
        model: str | None,
    ) -> list[CortexActivitySignal]:
        if raw_type == "message_start":
            return [_signal(CortexActivityType.REQUEST_IN_PROGRESS, self.provider, model)]
        if raw_type == "message_stop":
            return []

        index = _safe_index(_field(data, "index"))
        block = _as_mapping(_field(data, "content_block"))
        delta = _as_mapping(_field(data, "delta"))
        block_type = str(_field(block, "type") or _field(delta, "type") or "").lower()
        tool_name = str(_field(block, "name") or "").lower()

        if raw_type == "content_block_start" and index is not None:
            self._blocks[index] = (block_type, tool_name)
        elif index is not None and not block_type and index in self._blocks:
            block_type, tool_name = self._blocks[index]

        if raw_type == "content_block_stop":
            stored_type, stored_name = (
                self._blocks.pop(index, (block_type, tool_name))
                if index is not None
                else (block_type, tool_name)
            )
            if stored_type in {"thinking", "thinking_delta"}:
                return [_signal(CortexActivityType.THINKING_COMPLETED, self.provider, model)]
            if stored_type in {"tool_use", "server_tool_use"}:
                event_type = (
                    CortexActivityType.SEARCH_COMPLETED
                    if "search" in stored_name
                    else CortexActivityType.TOOL_COMPLETED
                )
                return [_signal(event_type, self.provider, model, {"tool_kind": stored_name})]
            return []

        if block_type in {"thinking", "thinking_delta"}:
            return [_signal(CortexActivityType.THINKING_STARTED, self.provider, model)]
        if block_type in {"tool_use", "server_tool_use"}:
            event_type = (
                CortexActivityType.SEARCH_STARTED
                if "search" in tool_name
                else CortexActivityType.TOOL_STARTED
            )
            return [_signal(event_type, self.provider, model, {"tool_kind": tool_name})]
        if block_type == "web_search_tool_result":
            return [_signal(CortexActivityType.SEARCH_COMPLETED, self.provider, model)]
        return []

    def _normalize_gemini_event(
        self,
        raw_type: str,
        data: Mapping[str, Any],
        model: str | None,
    ) -> list[CortexActivitySignal]:
        normalized = raw_type.replace(".", "_")
        if normalized in {"interaction_created", "interaction_status_update"}:
            return [_signal(CortexActivityType.REQUEST_IN_PROGRESS, self.provider, model)]
        if normalized in {"interaction_completed", "interaction_failed"}:
            return []

        step = _as_mapping(_field(data, "step"))
        delta = _as_mapping(_field(data, "delta"))
        step_type = str(
            _field(step, "type")
            or _field(delta, "type")
            or (
                normalized
                if normalized
                in {
                    "thought",
                    "google_search_call",
                    "google_search_result",
                    "file_search_call",
                    "file_search_result",
                    "retrieval_call",
                    "retrieval_result",
                    "function_call",
                    "function_result",
                    "mcp_server_tool_call",
                    "mcp_server_tool_result",
                    "google_maps_call",
                    "google_maps_result",
                    "url_context_call",
                    "url_context_result",
                    "processing_call",
                    "processing_result",
                    "code_execution_call",
                    "code_execution_result",
                }
                else ""
            )
        ).lower()
        index = _safe_index(_field(data, "index"))
        if raw_type == "step.start" and index is not None:
            self._steps[index] = step_type
        elif raw_type == "step.stop" and index is not None:
            step_type = self._steps.pop(index, step_type)

        starts = {
            "thought": CortexActivityType.THINKING_STARTED,
            "google_search_call": CortexActivityType.SEARCH_STARTED,
            "google_search_result": CortexActivityType.SEARCH_COMPLETED,
            "file_search_call": CortexActivityType.FILE_SEARCH_STARTED,
            "file_search_result": CortexActivityType.FILE_SEARCH_COMPLETED,
            "retrieval_call": CortexActivityType.FILE_SEARCH_STARTED,
            "retrieval_result": CortexActivityType.FILE_SEARCH_COMPLETED,
            "function_call": CortexActivityType.TOOL_STARTED,
            "function_result": CortexActivityType.TOOL_COMPLETED,
            "mcp_server_tool_call": CortexActivityType.MCP_TOOL_STARTED,
            "mcp_server_tool_result": CortexActivityType.MCP_TOOL_COMPLETED,
            "google_maps_call": CortexActivityType.TOOL_STARTED,
            "google_maps_result": CortexActivityType.TOOL_COMPLETED,
            "url_context_call": CortexActivityType.TOOL_STARTED,
            "url_context_result": CortexActivityType.TOOL_COMPLETED,
            "processing_call": CortexActivityType.THINKING_STARTED,
            "processing_result": CortexActivityType.THINKING_COMPLETED,
            "code_execution_call": CortexActivityType.CODE_EXECUTION_STARTED,
            "code_execution_result": CortexActivityType.CODE_EXECUTION_COMPLETED,
        }
        stops = {
            "thought": CortexActivityType.THINKING_COMPLETED,
            "google_search_call": CortexActivityType.SEARCH_COMPLETED,
            "file_search_call": CortexActivityType.FILE_SEARCH_COMPLETED,
            "retrieval_call": CortexActivityType.FILE_SEARCH_COMPLETED,
            "function_call": CortexActivityType.TOOL_COMPLETED,
            "mcp_server_tool_call": CortexActivityType.MCP_TOOL_COMPLETED,
            "google_maps_call": CortexActivityType.TOOL_COMPLETED,
            "url_context_call": CortexActivityType.TOOL_COMPLETED,
            "processing_call": CortexActivityType.THINKING_COMPLETED,
            "code_execution_call": CortexActivityType.CODE_EXECUTION_COMPLETED,
        }
        event_type = stops.get(step_type) if raw_type == "step.stop" else starts.get(step_type)
        return [_signal(event_type, self.provider, model)] if event_type else []


def _signal(
    event_type: CortexActivityType,
    provider: str,
    model: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> CortexActivitySignal:
    return CortexActivitySignal(
        event_type=event_type,
        provider=provider or None,
        model=model,
        metadata=sanitize_activity_metadata(metadata),
    )


def _normalize_openai_compatible(
    raw_type: str,
    provider: str,
    model: str | None,
) -> CortexActivitySignal | None:
    exact = {
        "response.created": CortexActivityType.REQUEST_IN_PROGRESS,
        "response.queued": CortexActivityType.REQUEST_IN_PROGRESS,
        "response.in_progress": CortexActivityType.REQUEST_IN_PROGRESS,
        "response.web_search_call.in_progress": CortexActivityType.SEARCH_STARTED,
        "response.web_search_call.searching": CortexActivityType.SEARCH_STARTED,
        "response.web_search_call.completed": CortexActivityType.SEARCH_COMPLETED,
        "response.file_search_call.in_progress": CortexActivityType.FILE_SEARCH_STARTED,
        "response.file_search_call.searching": CortexActivityType.FILE_SEARCH_STARTED,
        "response.file_search_call.completed": CortexActivityType.FILE_SEARCH_COMPLETED,
        "response.code_interpreter_call.in_progress": CortexActivityType.CODE_EXECUTION_STARTED,
        "response.code_interpreter_call.interpreting": CortexActivityType.CODE_EXECUTION_STARTED,
        "response.code_interpreter_call.completed": CortexActivityType.CODE_EXECUTION_COMPLETED,
    }
    event_type = exact.get(raw_type)
    if event_type is None and "reasoning" in raw_type:
        event_type = (
            CortexActivityType.THINKING_COMPLETED
            if raw_type.endswith((".done", ".completed"))
            else CortexActivityType.THINKING_STARTED
        )
    return _signal(event_type, provider, model) if event_type else None


def _normalize_deepseek(
    raw_type: str,
    data: Mapping[str, Any],
    provider: str,
    model: str | None,
) -> CortexActivitySignal | None:
    if data.get("reasoning_content") or "reasoning" in raw_type:
        return _signal(CortexActivityType.THINKING_STARTED, provider, model)
    if data.get("tool_calls"):
        return _signal(CortexActivityType.TOOL_STARTED, provider, model)
    if data.get("content") or "output_text.delta" in raw_type:
        return _signal(CortexActivityType.ANSWER_DELTA, provider, model)
    finish_reason = str(data.get("finish_reason") or "").lower()
    if finish_reason:
        event_type = (
            CortexActivityType.REQUEST_FAILED
            if finish_reason in {"error", "content_filter"}
            else CortexActivityType.ANSWER_COMPLETED
        )
        return _signal(event_type, provider, model)
    return _normalize_openai_compatible(raw_type, provider, model)


def _object_fields(value: object) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in (
        "type",
        "event",
        "event_type",
        "model",
        "index",
        "step",
        "content_block",
        "delta",
        "reasoning_content",
        "tool_calls",
        "content",
        "name",
        "text",
        "interaction",
        "data",
        "usage",
        "status",
        "finish_reason",
    ):
        if hasattr(value, name):
            result[name] = getattr(value, name)
    return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return {}
    return _object_fields(value)


def _field(value: Mapping[str, Any] | object, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _safe_index(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
