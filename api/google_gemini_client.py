import os
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from google import genai

from config.cache_optimization import cache_friendly_prompt_ordering_enabled
from config.web_search import load_native_web_search_config
from models.unified_response import TokenUsage, UnifiedResponse
from tools.web.provider_metadata import (
    build_web_search_metadata,
    field_value,
    insert_numbered_citations,
    normalize_web_sources,
    sequence_value,
)
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)


class GeminiClient(BaseAIClient):
    """
    Google Gemini API client returning UnifiedResponse.

    Uses google.genai package.
    All responses are normalized to UnifiedResponse format.
    """

    def __init__(self, api_key: str, model_name: str = "gemini-1.5-flash", **kwargs):
        """
        Initialize the Gemini client.

        Args:
            api_key: The Google Gemini API key
            model_name: The name of the model to use (default: gemini-1.5-flash)
            **kwargs: Additional keyword arguments
        """
        super().__init__(api_key, model_name=model_name, **kwargs)

        if not api_key:
            raise ValueError("API key is required for Gemini")

        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.cost_calculator = CostCalculator(model_type="gemini", model_name=model_name)

    def _convert_messages_to_gemini_format(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """
        Convert standard messages format to Gemini's format.

        Gemini expects:
        - system_instruction (optional): merged system prompts
        - contents: list of messages with role and parts

        Role mapping:
        - "system" -> merged into system_instruction
        - "user" -> role="user"
        - "assistant" -> role="model"

        Args:
            messages: List of message dicts with 'role' and 'content' keys

        Returns:
            Tuple of (system_instruction, gemini_contents)
        """
        system_instruction_parts: list[str] = []
        gemini_contents = []
        attachments = attachments or []

        last_user_index = None
        for idx, msg in enumerate(messages):
            if str(msg.get("role", "")).strip().lower() == "user":
                last_user_index = idx

        for idx, msg in enumerate(messages):
            role = msg.get("role", "user")
            content = self._normalize_message_text(msg)

            if role == "system":
                if content:
                    system_instruction_parts.append(content)
                continue

            # Map roles: user->user, assistant->model
            gemini_role = "model" if role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            if content and not cache_friendly_prompt_ordering_enabled():
                parts.append({"text": content})

            if attachments and last_user_index is not None and idx == last_user_index:
                for attachment in attachments:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": attachment["mime_type"],
                                "data": attachment["data_base64"],
                            }
                        }
                    )

            if content and cache_friendly_prompt_ordering_enabled():
                parts.append({"text": content})

            gemini_contents.append({"role": gemini_role, "parts": parts or [{"text": ""}]})

        system_instruction = "\n\n".join(system_instruction_parts) or None
        return system_instruction, gemini_contents

    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        """
        Get a completion from the Gemini API.

        Args:
            prompt: (Legacy) Single string prompt - converted to messages format
            messages: (Multi-turn) List of message dicts with 'role' and 'content' keys
            save_full: If True, include raw provider response in response.raw
            **kwargs: Additional parameters:
                - model: Override the default model for this call
                - temperature: Controls randomness (0.0 to 1.0)
                - max_output_tokens: Maximum number of tokens to generate

        Returns:
            UnifiedResponse: Normalized response object

        IMPORTANT: Never raises exceptions - returns UnifiedResponse with error instead
        """
        request_id = self._resolve_request_id_from_kwargs(kwargs)
        start_time = time.time()
        stream_observer = kwargs.pop("_stream_observer", None)

        model_name = kwargs.get("model", self.model_name)
        self._resolve_cache_context(kwargs, provider="gemini", model=model_name)
        temperature = kwargs.get("temperature", 0.7)
        # Keep route-level max_tokens clamp compatible with Gemini's max_output_tokens naming.
        max_output_tokens = kwargs.get("max_output_tokens", kwargs.get("max_tokens", 2048))
        reasoning_mode = str(kwargs.get("reasoning_mode") or "").strip().lower()
        reasoning_effort = str(kwargs.get("reasoning_effort") or "").strip().lower()
        attachments = self._normalize_inference_attachments(kwargs.pop("attachments", None))
        web_search_policy = kwargs.pop("web_search_policy", {}) or {}
        kwargs.pop("web_search_executor", None)
        web_config = load_native_web_search_config()
        web_search_enabled = (
            web_config.provider_enabled("gemini")
            and str(web_search_policy.get("mode") or "off") != "off"
        )

        try:
            # Normalize input to messages format
            normalized_messages = self._normalize_input(prompt=prompt, messages=messages)
            normalized_messages, binary_attachments = self._merge_text_attachments_into_messages(
                normalized_messages,
                attachments,
            )

            # Convert to Gemini format
            system_instruction, gemini_contents = self._convert_messages_to_gemini_format(
                normalized_messages,
                attachments=binary_attachments,
            )

            # Build config
            config = {
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            }
            if system_instruction:
                config["system_instruction"] = system_instruction
            if reasoning_mode == "none":
                config["thinking_config"] = {"thinking_budget": 0}
            elif reasoning_effort and reasoning_effort != "none":
                config["thinking_config"] = {"thinking_level": reasoning_effort}

            adaptive_retry = None
            endpoint = "models.generate_content"
            try:
                if web_search_enabled:
                    cap = max(1, min(3, int(web_search_policy.get("max_operations") or 3)))
                    web_instruction = f"Use no more than {cap} Google Search queries. " + (
                        "Google Search is required for this request."
                        if str(web_search_policy.get("mode") or "auto") == "required"
                        else "Search only when current or externally verifiable information is needed."
                    )
                    interaction_config: dict[str, Any] = {
                        "max_output_tokens": max_output_tokens,
                    }
                    # Google Search is a server-side tool in Interactions v2. Do
                    # not send the legacy allowed_tools selector: SDK 2.x maps it
                    # to allowed_function_names, which only accepts declared
                    # client functions. The plain "any" selector is also unsafe
                    # here because it forces another tool call instead of allowing
                    # the model to produce its final answer after searching.
                    # Gemini 3.5 rejects legacy sampling controls on Interactions
                    # requests. Keep native-search payloads model-compatible instead
                    # of turning an otherwise valid Google Search request into a 400.
                    if not str(model_name).strip().lower().startswith("gemini-3.5"):
                        interaction_config["temperature"] = temperature
                    if reasoning_effort and reasoning_effort != "none":
                        interaction_config["thinking_level"] = reasoning_effort
                    interaction_payload = {
                        "model": model_name,
                        "input": self._build_interactions_input(
                            normalized_messages,
                            attachments=binary_attachments,
                        ),
                        "system_instruction": "\n\n".join(
                            part for part in (system_instruction, web_instruction) if part
                        ),
                        "tools": [{"type": "google_search"}],
                        "generation_config": interaction_config,
                        "store": False,
                    }
                    response = self._create_interaction(
                        interaction_payload,
                        stream_observer,
                    )
                    endpoint = "interactions.create"
                else:
                    response = self._generate_content(
                        model=model_name,
                        contents=gemini_contents,
                        config=config,
                        stream_observer=stream_observer,
                    )
            except Exception as request_exc:
                if stream_observer is not None and stream_observer.has_emitted:
                    raise
                if web_search_enabled:
                    raise
                dropped_param, retry_config = (
                    self._build_retry_payload_without_unsupported_parameter(
                        config,
                        request_exc,
                        safe_parameters={"temperature", "max_output_tokens", "top_p", "top_k"},
                    )
                )
                if retry_config is not None and dropped_param is not None:
                    logger.warning(
                        "Retrying Gemini request without unsupported parameter",
                        extra={
                            "extra_fields": {
                                "request_id": request_id,
                                "model": model_name,
                                "retry_reason": "unsupported_parameter",
                                "dropped_param": dropped_param,
                            }
                        },
                    )
                    response = self._generate_content(
                        model=model_name,
                        contents=gemini_contents,
                        config=retry_config,
                        stream_observer=stream_observer,
                    )
                    adaptive_retry = {
                        "dropped_param": dropped_param,
                        "retry_reason": "unsupported_parameter",
                        "endpoint": "models.generate_content",
                    }
                else:
                    raise

            latency_ms = self._measure_latency(start_time)

            # Extract text
            if endpoint == "interactions.create":
                text, search_operations, search_sources = self._extract_interaction_search(response)
            else:
                text = response.text if hasattr(response, "text") else ""
                text, search_operations, search_sources = self._extract_grounding(
                    response,
                    text=str(text or ""),
                )

            # Extract token usage
            token_usage = TokenUsage()
            if endpoint == "interactions.create":
                usage = field_value(response, "usage")
                prompt_tokens = self._usage_int(field_value(usage, "total_input_tokens", 0))
                output_tokens = self._usage_int(field_value(usage, "total_output_tokens", 0))
                reasoning_tokens = self._usage_int(
                    field_value(
                        usage,
                        "total_thought_tokens",
                        field_value(usage, "total_reasoning_tokens", 0),
                    )
                )
                cached_input_tokens = self._usage_int(field_value(usage, "total_cached_tokens", 0))
                total_tokens = self._usage_int(field_value(usage, "total_tokens", 0))
                # Interactions reports answer and thought tokens separately;
                # Cortex completion_tokens includes both billable partitions.
                completion_tokens = output_tokens + reasoning_tokens
                token_usage = TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens or prompt_tokens + completion_tokens,
                    cached_input_tokens=cached_input_tokens,
                    reasoning_tokens=reasoning_tokens,
                )
            elif hasattr(response, "usage_metadata"):
                usage_metadata = response.usage_metadata
                cached_input_tokens = self._usage_int(
                    getattr(usage_metadata, "cached_content_token_count", 0)
                )
                reasoning_tokens = self._usage_int(
                    getattr(usage_metadata, "thoughts_token_count", 0)
                )
                completion_tokens = (
                    self._usage_int(getattr(usage_metadata, "candidates_token_count", 0))
                    + reasoning_tokens
                )
                prompt_tokens = self._usage_int(getattr(usage_metadata, "prompt_token_count", 0))
                total_tokens = self._usage_int(getattr(usage_metadata, "total_token_count", 0))
                if total_tokens <= 0:
                    total_tokens = prompt_tokens + completion_tokens
                token_usage = TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cached_input_tokens=cached_input_tokens,
                    reasoning_tokens=reasoning_tokens,
                )

            served_model = self._served_model(
                (
                    field_value(response, "model", model_name)
                    if endpoint == "interactions.create"
                    else getattr(response, "model_version", model_name)
                ),
                model_name,
            )

            # Calculate cost
            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("gemini", served_model)
            )
            cost = calculator.calculate_cost(
                request_at=datetime.fromtimestamp(start_time, timezone.utc),
                prompt_tokens=token_usage.prompt_tokens,
                completion_tokens=token_usage.completion_tokens,
                cached_input_tokens=token_usage.cached_input_tokens,
                reasoning_tokens=token_usage.reasoning_tokens,
            )
            estimated_cost = cost["total_cost"]

            # Extract finish reason from Gemini response
            finish_reason_raw = None
            if endpoint == "interactions.create":
                finish_reason_raw = field_value(response, "status")
            elif hasattr(response, "candidates") and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, "finish_reason"):
                    finish_reason_raw = str(candidate.finish_reason)

            # Normalize finish reason
            finish_reason = self._normalize_finish_reason(
                (
                    "stop"
                    if str(finish_reason_raw or "").lower() == "completed"
                    else finish_reason_raw
                ),
                provider="gemini",
            )

            # Build raw response if requested
            raw = None
            if save_full and endpoint == "interactions.create":
                raw = (
                    response.model_dump()
                    if hasattr(response, "model_dump")
                    else (response if isinstance(response, dict) else None)
                )
            elif save_full:
                raw = {
                    "text": text,
                    "usage_metadata": (
                        {
                            "prompt_token_count": token_usage.prompt_tokens,
                            "candidates_token_count": token_usage.completion_tokens,
                            "total_token_count": token_usage.total_tokens,
                        }
                        if hasattr(response, "usage_metadata")
                        else None
                    ),
                    "candidates": (
                        [{"finish_reason": finish_reason_raw}]
                        if hasattr(response, "candidates")
                        else []
                    ),
                }

            logger.info(
                "Gemini completion successful",
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "model": model_name,
                        "latency_ms": latency_ms,
                        "tokens": token_usage.total_tokens,
                        "cost": estimated_cost,
                    }
                },
            )

            metadata = {
                "endpoint": endpoint,
                "pricing_unknown": bool(cost.get("pricing_unknown", False)),
                **build_web_search_metadata(
                    request_at=datetime.fromtimestamp(start_time, timezone.utc),
                    provider="gemini",
                    backend="google_search",
                    requested_mode=str(web_search_policy.get("requested_mode") or "off"),
                    effective_mode=(
                        str(web_search_policy.get("mode") or "off") if web_search_enabled else "off"
                    ),
                    operations=search_operations,
                    sources=search_sources,
                ),
            }
            if adaptive_retry:
                metadata["adaptive_retry"] = adaptive_retry

            return UnifiedResponse(
                request_id=request_id,
                text=text,
                provider="gemini",
                model=served_model,
                latency_ms=latency_ms,
                token_usage=token_usage,
                estimated_cost=estimated_cost,
                finish_reason=finish_reason,
                error=None,
                metadata=metadata,
                raw=raw,
                **self._response_audit_fields(
                    served_model=served_model,
                    cost=cost,
                    reasoning_mode=reasoning_mode or None,
                ),
            )

        except Exception as e:
            latency_ms = self._measure_latency(start_time)
            error = self._normalize_error(e, provider="gemini")

            logger.error(
                f"Gemini completion failed: {error.code}",
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "model": model_name,
                        "error_code": error.code,
                        "error_message": error.message,
                        "retryable": error.retryable,
                    }
                },
            )

            return self._create_error_response(
                request_id=request_id, error=error, latency_ms=latency_ms, model=model_name
            )

    def _create_interaction(
        self,
        payload: dict[str, Any],
        stream_observer: Any,
    ) -> Any:
        if stream_observer is None:
            return self.client.interactions.create(**payload)

        final_interaction = None
        streamed_steps: dict[int, dict[str, Any]] = {}
        streamed_text: list[str] = []
        stream = self.client.interactions.create(**payload, stream=True)
        try:
            for wrapped_event in stream:
                event = field_value(wrapped_event, "data", wrapped_event)
                stream_observer.emit_provider_event(event)
                event_type = str(field_value(event, "event_type", "") or "").lower()
                if event_type == "step.start":
                    index = self._stream_step_index(event)
                    step = self._stream_payload(field_value(event, "step"))
                    current = streamed_steps.setdefault(index, {})
                    private = {
                        key: current[key]
                        for key in ("_text_parts", "_annotations")
                        if key in current
                    }
                    current.update(step)
                    current.update(private)
                elif event_type == "step.delta":
                    index = self._stream_step_index(event)
                    delta = field_value(event, "delta")
                    delta_type = str(field_value(delta, "type", "") or "").lower()
                    current = streamed_steps.setdefault(index, {})
                    if not current.get("type"):
                        current["type"] = (
                            "model_output"
                            if delta_type in {"text", "text_annotation_delta"}
                            else delta_type
                        )
                    if delta_type == "text":
                        text = str(field_value(delta, "text", "") or "")
                        if text:
                            streamed_text.append(text)
                            current.setdefault("_text_parts", []).append(text)
                            stream_observer.emit_text(text)
                    elif delta_type == "text_annotation_delta":
                        annotations = current.setdefault("_annotations", [])
                        for annotation in sequence_value(field_value(delta, "annotations", [])):
                            annotations.append(self._stream_payload(annotation) or annotation)
                    else:
                        delta_payload = self._stream_payload(delta)
                        for key, value in delta_payload.items():
                            if key != "type" and value is not None:
                                current[key] = value
                if event_type == "interaction.completed":
                    final_interaction = field_value(event, "interaction")
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        stream_observer.raise_if_cancelled()
        if final_interaction is None:
            raise RuntimeError("Gemini interaction stream ended without a completed interaction")

        # Interactions streaming completion events contain a partial interaction.
        # In particular, successful events may include status and usage while
        # omitting every step. Rebuild the response from the streamed lifecycle so
        # final extraction, persistence, and billing see the same answer that the
        # observer already delivered to the client.
        interaction = self._stream_payload(final_interaction)
        if not sequence_value(interaction.get("steps", [])):
            interaction["steps"] = [
                self._finalize_streamed_step(streamed_steps[index])
                for index in sorted(streamed_steps)
            ]
        if streamed_text and not str(interaction.get("output_text") or ""):
            interaction["output_text"] = "".join(streamed_text)
        return interaction

    @staticmethod
    def _stream_step_index(event: Any) -> int:
        try:
            return int(field_value(event, "index", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _stream_payload(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(mode="python", by_alias=True, exclude_none=True)
            if isinstance(dumped, Mapping):
                return dict(dumped)
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, Mapping):
            return dict(attributes)
        return {}

    @staticmethod
    def _finalize_streamed_step(step: dict[str, Any]) -> dict[str, Any]:
        finalized = {
            key: value for key, value in step.items() if key not in {"_text_parts", "_annotations"}
        }
        text = "".join(str(part) for part in step.get("_text_parts", []))
        if text:
            content = list(sequence_value(finalized.get("content", [])))
            if not any(
                str(field_value(item, "type", "") or "").lower() == "text"
                and str(field_value(item, "text", "") or "")
                for item in content
            ):
                text_content: dict[str, Any] = {"type": "text", "text": text}
                annotations = list(step.get("_annotations", []))
                if annotations:
                    text_content["annotations"] = annotations
                content.append(text_content)
            finalized["content"] = content
        return finalized

    def _generate_content(
        self,
        *,
        model: str,
        contents: list[dict[str, Any]],
        config: dict[str, Any],
        stream_observer: Any,
    ) -> Any:
        if stream_observer is None:
            return self.client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        text_parts: list[str] = []
        last_chunk = None
        stream = self.client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        )
        try:
            for chunk in stream:
                stream_observer.emit_provider_event(chunk)
                try:
                    chunk_text = field_value(chunk, "text", "")
                except (AttributeError, TypeError, ValueError):
                    chunk_text = ""
                if chunk_text:
                    text_parts.append(str(chunk_text))
                    stream_observer.emit_text(chunk_text)
                last_chunk = chunk
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

        stream_observer.raise_if_cancelled()
        if last_chunk is None:
            raise RuntimeError("Gemini content stream ended without a response")
        return SimpleNamespace(
            text="".join(text_parts),
            usage_metadata=field_value(last_chunk, "usage_metadata"),
            model_version=field_value(last_chunk, "model_version", model),
            candidates=field_value(last_chunk, "candidates", []),
            prompt_feedback=field_value(last_chunk, "prompt_feedback"),
        )

    @staticmethod
    def _extract_grounding(
        response: Any,
        *,
        text: str,
    ) -> tuple[str, int, list[dict[str, str]]]:
        candidates = sequence_value(field_value(response, "candidates", []))
        if not candidates:
            return text, 0, []
        grounding = field_value(candidates[0], "grounding_metadata")
        queries = sequence_value(field_value(grounding, "web_search_queries", []))
        chunks = sequence_value(field_value(grounding, "grounding_chunks", []))
        raw_sources: list[dict[str, str]] = []
        chunk_urls: list[str] = []
        for chunk in chunks:
            web = field_value(chunk, "web", {})
            url = str(field_value(web, "uri", "") or field_value(web, "url", "") or "")
            title = str(field_value(web, "title", "") or "")
            chunk_urls.append(url)
            if url:
                raw_sources.append({"url": url, "title": title})
        sources = normalize_web_sources(raw_sources, limit=8)
        annotations: list[dict[str, Any]] = []
        for support in sequence_value(field_value(grounding, "grounding_supports", [])):
            segment = field_value(support, "segment", {})
            end_index = field_value(segment, "end_index")
            for raw_index in sequence_value(field_value(support, "grounding_chunk_indices", [])):
                try:
                    url = chunk_urls[int(raw_index)]
                except (TypeError, ValueError, IndexError):
                    continue
                if url:
                    annotations.append({"url": url, "end_index": end_index})
        operations = len(queries)
        if operations == 0 and sources:
            operations = 1
        return insert_numbered_citations(text, annotations, sources), operations, sources

    @classmethod
    def _build_interactions_input(
        cls,
        normalized_messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        last_user_index = max(
            (
                index
                for index, message in enumerate(normalized_messages)
                if str(message.get("role") or "").strip().lower() == "user"
            ),
            default=-1,
        )
        for index, message in enumerate(normalized_messages):
            role = str(message.get("role") or "user").strip().lower()
            if role == "system":
                continue
            content: list[dict[str, Any]] = [
                {"type": "text", "text": cls._normalize_message_text(message)}
            ]
            if index == last_user_index:
                for attachment in attachments:
                    mime_type = str(attachment.get("mime_type") or "")
                    content.append(
                        {
                            "type": "image" if mime_type.startswith("image/") else "document",
                            "mime_type": mime_type,
                            "data": attachment.get("data_base64") or "",
                        }
                    )
            turns.append(
                {
                    "type": "model_output" if role == "assistant" else "user_input",
                    "content": content,
                }
            )
        return turns

    @classmethod
    def _extract_interaction_search(
        cls,
        response: Any,
    ) -> tuple[str, int, list[dict[str, str]]]:
        steps = sequence_value(field_value(response, "steps", []))
        outputs = sequence_value(field_value(response, "outputs", []))
        items = list(steps) if steps else list(outputs)
        operations = 0
        text_blocks: list[tuple[str, list[Any]]] = []
        raw_sources: list[dict[str, str]] = []

        def collect_content(content_items: Any) -> None:
            nonlocal operations
            for content in sequence_value(content_items):
                content_type = str(field_value(content, "type", "") or "").lower()
                if content_type == "google_search_call":
                    arguments = field_value(content, "arguments", {})
                    queries = sequence_value(field_value(arguments, "queries", []))
                    operations += len([query for query in queries if str(query or "").strip()])
                    continue
                if content_type != "text":
                    continue
                block_text = str(field_value(content, "text", "") or "")
                annotations = list(sequence_value(field_value(content, "annotations", [])))
                text_blocks.append((block_text, annotations))
                for annotation in annotations:
                    url = str(
                        field_value(annotation, "url", "")
                        or field_value(annotation, "uri", "")
                        or field_value(annotation, "source", "")
                        or ""
                    ).strip()
                    if not url.startswith(("http://", "https://")):
                        continue
                    raw_sources.append(
                        {
                            "url": url,
                            "title": str(field_value(annotation, "title", "") or url),
                        }
                    )

        for item in items:
            item_type = str(field_value(item, "type", "") or "").lower()
            if item_type == "google_search_call":
                arguments = field_value(item, "arguments", {})
                queries = sequence_value(field_value(arguments, "queries", []))
                operations += len([query for query in queries if str(query or "").strip()])
            elif item_type == "google_search_result":
                for result in sequence_value(field_value(item, "result", [])):
                    url = str(field_value(result, "url", "") or "").strip()
                    if url:
                        raw_sources.append(
                            {
                                "url": url,
                                "title": str(field_value(result, "title", "") or url),
                            }
                        )
            elif item_type in {"model_output", "turn"}:
                collect_content(field_value(item, "content", []))
            else:
                collect_content([item])

        # Interactions v2 reports authoritative grounding-tool counts in usage.
        # Retain query-level step counting when present, but do not lose billing
        # telemetry if a response omits the call arguments.
        usage = field_value(response, "usage")
        reported_operations = 0
        for tool_count in sequence_value(field_value(usage, "grounding_tool_count", [])):
            if str(field_value(tool_count, "type", "") or "").lower() != "google_search":
                continue
            reported_operations += cls._usage_int(field_value(tool_count, "count", 0))
        operations = max(operations, reported_operations)

        sources = normalize_web_sources(raw_sources, limit=8)
        source_index = {
            source["url"].casefold().rstrip("/"): index + 1 for index, source in enumerate(sources)
        }
        rendered_blocks: list[str] = []
        for block_text, annotations in text_blocks:
            normalized_annotations: list[dict[str, Any]] = []
            for annotation in annotations:
                url = str(
                    field_value(annotation, "url", "")
                    or field_value(annotation, "uri", "")
                    or field_value(annotation, "source", "")
                    or ""
                ).strip()
                if url.casefold().rstrip("/") not in source_index:
                    continue
                raw_end = field_value(annotation, "end_index")
                try:
                    byte_end = int(raw_end)
                    char_end = len(block_text.encode("utf-8")[:byte_end].decode("utf-8", "ignore"))
                except (TypeError, ValueError):
                    char_end = len(block_text)
                normalized_annotations.append({"url": url, "end_index": char_end})
            rendered_blocks.append(
                insert_numbered_citations(block_text, normalized_annotations, sources)
            )

        text = "".join(rendered_blocks).strip()
        if not text:
            text = str(field_value(response, "output_text", "") or "")
        if operations == 0 and sources:
            operations = 1
        return text, operations, sources

    @classmethod
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        """
        List all available Gemini models.

        Args:
            api_key: The Google Gemini API key
            **kwargs: Additional parameters
                - current_model: The currently selected model (will be highlighted)
        """
        try:
            if not api_key:
                logger.warning("API key not provided for listing Gemini models")
                print("API key not provided. Cannot list available models.")
                return

            client = genai.Client(api_key=api_key)
            current_model = kwargs.get("current_model", "gemini-1.5-flash")

            # Get the list of available models
            models = client.models.list()

            model_list = list(models)
            logger.info(
                "Listed available Gemini models",
                extra={
                    "extra_fields": {"model_count": len(model_list), "current_model": current_model}
                },
            )

            print("\n=== Available Gemini Models ===")
            for model in model_list:
                # Check if model supports content generation
                if (
                    hasattr(model, "supported_generation_methods")
                    and "generateContent" in model.supported_generation_methods
                ):
                    prefix = "* " if model.name == current_model else "  "
                    print(
                        f"{prefix}{model.name} (supports: {', '.join(model.supported_generation_methods)})"
                    )
                elif hasattr(model, "name"):
                    # Fallback if supported_generation_methods is not available
                    prefix = "* " if model.name == current_model else "  "
                    print(f"{prefix}{model.name}")
            print("* = currently selected\n")

        except Exception as e:
            logger.error(
                f"Error listing available Gemini models: {e!s}",
                extra={"extra_fields": {"error_type": type(e).__name__}},
            )
            print(f"Error listing available models: {e!s}")


# Example usage
if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.getenv("GOOGLE_GEMINI_API_KEY")
    model_name = os.getenv("DEFAULT_GEMINI_MODEL", "gemini-1.5-flash")

    if not api_key:
        print("Error: GOOGLE_GEMINI_API_KEY not found in environment variables")
    else:
        client = GeminiClient(api_key=api_key, model_name=model_name)
        response = client.get_completion("Hello, how can I help you today?")
        print(f"Text: {response.text}")
        print(f"Tokens: {response.token_usage.total_tokens}")
        print(f"Cost: ${response.estimated_cost:.6f}")
