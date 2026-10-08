import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import openai

from config.cache_optimization import (
    cache_friendly_prompt_ordering_enabled,
    openai_extended_prompt_cache_enabled,
    openai_prompt_cache_enabled,
)
from config.web_search import load_native_web_search_config
from models.unified_response import TokenUsage, UnifiedResponse
from tools.web.provider_metadata import (
    build_web_search_metadata,
    extract_responses_web_search,
)
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)

if TYPE_CHECKING:
    from orchestrator.cache_context import CacheContext


class OpenAIClient(BaseAIClient):
    """
    OpenAI API client returning UnifiedResponse.

    All responses are normalized to UnifiedResponse format.
    No provider-specific response objects are exposed.
    """

    def __init__(self, api_key: str, model_name: str = "gpt-3.5-turbo", **kwargs):
        """
        Initialize the OpenAI client.

        Args:
            api_key: The OpenAI API key
            model_name: The name of the model to use (default: gpt-3.5-turbo)
            **kwargs: Additional keyword arguments
        """
        super().__init__(api_key, model_name=model_name, **kwargs)
        self.client = openai.OpenAI(api_key=api_key)
        self.model_name = model_name
        self.cost_calculator = CostCalculator(model_type="openai", model_name=model_name)

    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        """
        Get a completion from the OpenAI API.

        Args:
            prompt: (Legacy) Single string prompt - converted to messages format
            messages: (Multi-turn) List of message dicts with 'role' and 'content' keys
            save_full: If True, include raw provider response in response.raw
            **kwargs: Additional parameters:
                - model: Override the default model for this call
                - temperature: Controls randomness (0.0 to 2.0)
                - max_tokens: Maximum number of tokens to generate

        Returns:
            UnifiedResponse: Normalized response object

        IMPORTANT: Never raises exceptions - returns UnifiedResponse with error instead
        """
        request_id = self._resolve_request_id_from_kwargs(kwargs)
        start_time = time.time()
        stream_observer = kwargs.pop("_stream_observer", None)

        model = kwargs.get("model", self.model_name)
        cache_context = self._resolve_cache_context(
            kwargs, provider="openai", model=model
        )
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens", 2048)
        max_completion_tokens = kwargs.get("max_completion_tokens")
        reasoning_mode = str(kwargs.get("reasoning_mode") or "").strip().lower()
        reasoning_effort = str(kwargs.get("reasoning_effort") or "").strip().lower()
        response_format = kwargs.get("response_format")
        attachments = self._normalize_inference_attachments(kwargs.pop("attachments", None))
        web_search_policy = kwargs.pop("web_search_policy", {}) or {}
        kwargs.pop("web_search_executor", None)
        web_config = load_native_web_search_config()
        web_search_enabled = (
            web_config.provider_enabled("openai")
            and str(web_search_policy.get("mode") or "off") != "off"
        )

        try:
            # Normalize input to messages format
            normalized_messages = self._normalize_input(prompt=prompt, messages=messages)
            normalized_messages, binary_attachments = self._merge_text_attachments_into_messages(
                normalized_messages,
                attachments,
            )
            response, endpoint_used, adaptive_retry = self._create_openai_completion(
                request_id=request_id,
                model=model,
                normalized_messages=normalized_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                max_completion_tokens=max_completion_tokens,
                response_format=response_format,
                attachments=binary_attachments,
                reasoning_mode=reasoning_mode,
                reasoning_effort=reasoning_effort,
                cache_context=cache_context,
                web_search_policy=web_search_policy if web_search_enabled else None,
                stream_observer=stream_observer,
            )

            latency_ms = self._measure_latency(start_time)

            if endpoint_used == "responses":
                text = self._extract_responses_text(response)
                text, search_operations, search_sources = extract_responses_web_search(
                    response,
                    text=text,
                )
                token_usage = self._extract_responses_usage(response)
                served_model = self._served_model(self._field(response, "model", model), model)
                finish_reason = self._normalize_finish_reason(
                    self._extract_responses_finish_reason(response),
                    provider="openai",
                )
                raw = self._serialize_raw_response(response) if save_full else None
            else:
                search_operations = 0
                search_sources = []
                # Extract text
                text = self._extract_chat_completions_text(response)

                # Extract token usage
                token_usage = self._openai_compatible_token_usage(
                    response.usage if hasattr(response, "usage") else None
                )
                served_model = self._served_model(getattr(response, "model", model), model)

                # Normalize finish reason
                finish_reason = self._normalize_finish_reason(
                    response.choices[0].finish_reason if response.choices else None,
                    provider="openai",
                )

                # Build raw response if requested
                raw = None
                if save_full:
                    raw = {
                        "id": response.id,
                        "object": response.object,
                        "created": response.created,
                        "model": response.model,
                        "choices": [
                            {
                                "index": choice.index,
                                "message": {
                                    "role": choice.message.role,
                                    "content": choice.message.content,
                                },
                                "finish_reason": choice.finish_reason,
                            }
                            for choice in response.choices
                        ],
                        "usage": (
                            {
                                "prompt_tokens": response.usage.prompt_tokens,
                                "completion_tokens": response.usage.completion_tokens,
                                "total_tokens": response.usage.total_tokens,
                            }
                            if hasattr(response, "usage")
                            else None
                        ),
                    }

            # Calculate cost
            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("openai", served_model)
            )
            cost = calculator.calculate_cost(
                request_at=datetime.fromtimestamp(start_time, timezone.utc),
                prompt_tokens=token_usage.prompt_tokens,
                completion_tokens=token_usage.completion_tokens,
                cached_input_tokens=token_usage.cached_input_tokens,
                cache_write_tokens=token_usage.cache_write_tokens,
                reasoning_tokens=token_usage.reasoning_tokens,
            )
            estimated_cost = cost["total_cost"]

            metadata = {"endpoint": endpoint_used}
            metadata["pricing_unknown"] = bool(cost.get("pricing_unknown", False))
            metadata.update(
                build_web_search_metadata(
                    request_at=datetime.fromtimestamp(start_time, timezone.utc),
                    provider="openai",
                    backend="web_search",
                    requested_mode=str(web_search_policy.get("requested_mode") or "off"),
                    effective_mode=(
                        str(web_search_policy.get("mode") or "off")
                        if web_search_enabled
                        else "off"
                    ),
                    operations=search_operations,
                    sources=search_sources,
                )
            )
            if adaptive_retry:
                metadata["adaptive_retry"] = adaptive_retry

            logger.info(
                "OpenAI completion successful",
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "model": model,
                        "endpoint": endpoint_used,
                        "latency_ms": latency_ms,
                        "tokens": token_usage.total_tokens,
                        "cost": estimated_cost,
                    }
                },
            )

            return UnifiedResponse(
                request_id=request_id,
                text=text,
                provider="openai",
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
            error = self._normalize_error(e, provider="openai")

            logger.error(
                f"OpenAI completion failed: {error.code}",
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "model": model,
                        "error_code": error.code,
                        "error_message": error.message,
                        "retryable": error.retryable,
                    }
                },
            )

            return self._create_error_response(
                request_id=request_id, error=error, latency_ms=latency_ms, model=model
            )

    def _create_openai_completion(
        self,
        *,
        request_id: str,
        model: str,
        normalized_messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int,
        max_completion_tokens: int | None,
        response_format: dict[str, Any] | None,
        attachments: list[dict[str, Any]],
        reasoning_mode: str,
        reasoning_effort: str,
        cache_context: "CacheContext",
        web_search_policy: dict[str, Any] | None = None,
        stream_observer: Any = None,
    ) -> tuple[Any, str, dict[str, Any] | None]:
        if web_search_policy or self._should_use_responses_api_for_model(
            model, attachments=attachments
        ):
            payload = self._build_responses_payload(
                model=model,
                normalized_messages=normalized_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                max_completion_tokens=max_completion_tokens,
                attachments=attachments,
                reasoning_mode=reasoning_mode,
                reasoning_effort=reasoning_effort,
                web_search_policy=web_search_policy,
            )
            self._apply_prompt_cache_options(payload, cache_context)
            return self._create_openai_response_with_compat_retries(
                request_id=request_id,
                model=model,
                payload=payload,
                stream_observer=stream_observer,
            )

        request_payload = {
            "model": model,
            "messages": self._build_chat_completions_messages(
                normalized_messages, attachments=attachments
            ),
        }
        if temperature is not None:
            request_payload["temperature"] = temperature
        if response_format is not None:
            request_payload["response_format"] = response_format
        if reasoning_effort and reasoning_effort != "none":
            request_payload["reasoning_effort"] = reasoning_effort
        if max_completion_tokens is not None:
            request_payload["max_completion_tokens"] = max_completion_tokens
        else:
            request_payload["max_tokens"] = max_tokens
        if stream_observer is not None:
            request_payload["stream_options"] = {"include_usage": True}
        self._apply_prompt_cache_options(request_payload, cache_context)

        try:
            return (
                self._create_chat_completion(request_payload, stream_observer),
                "chat.completions",
                None,
            )
        except Exception as request_exc:
            if stream_observer is not None and stream_observer.has_emitted:
                raise
            # OpenAI newer model families reject max_tokens and require max_completion_tokens.
            if (
                "max_tokens" in request_payload
                and self._should_retry_with_max_completion_tokens(request_exc)
            ):
                retry_payload = dict(request_payload)
                retry_payload["max_completion_tokens"] = retry_payload.pop("max_tokens")
                logger.warning(
                    "Retrying OpenAI request with max_completion_tokens",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "model": model,
                            "retry_reason": "unsupported_max_tokens_parameter",
                        }
                    },
                )
                return self._create_chat_completion(
                    retry_payload, stream_observer
                ), "chat.completions", {
                    "dropped_param": "max_tokens",
                    "replacement_param": "max_completion_tokens",
                    "retry_reason": "unsupported_max_tokens_parameter",
                    "endpoint": "chat.completions",
                }

            if self._should_retry_with_responses_api(request_exc):
                payload = self._build_responses_payload(
                    model=model,
                    normalized_messages=normalized_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_completion_tokens=max_completion_tokens,
                    attachments=attachments,
                    reasoning_mode=reasoning_mode,
                    reasoning_effort=reasoning_effort,
                    web_search_policy=None,
                )
                self._apply_prompt_cache_options(payload, cache_context)
                logger.warning(
                    "Retrying OpenAI request with responses API",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "model": model,
                            "retry_reason": "chat_endpoint_incompatible_with_model",
                        }
                    },
                )
                return self._create_openai_response_with_compat_retries(
                    request_id=request_id,
                    model=model,
                    payload=payload,
                    stream_observer=stream_observer,
                )

            dropped_param, retry_payload = self._build_retry_payload_without_unsupported_parameter(
                request_payload,
                request_exc,
                safe_parameters={
                    "temperature",
                    "top_p",
                    "presence_penalty",
                    "frequency_penalty",
                    "max_tokens",
                    "max_completion_tokens",
                    "response_format",
                    "reasoning_effort",
                    "stream_options",
                    "prompt_cache_key",
                    "prompt_cache_retention",
                },
            )
            if retry_payload is not None and dropped_param is not None:
                logger.warning(
                    "Retrying OpenAI chat completion without unsupported parameter",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "model": model,
                            "retry_reason": "unsupported_parameter",
                            "dropped_param": dropped_param,
                        }
                    },
                )
                return self._create_chat_completion(
                    retry_payload, stream_observer
                ), "chat.completions", {
                    "dropped_param": dropped_param,
                    "retry_reason": "unsupported_parameter",
                    "endpoint": "chat.completions",
                }

            raise

    @staticmethod
    def _should_use_responses_api_for_model(
        model: str,
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> bool:
        if attachments:
            return True
        model_norm = (model or "").strip().lower()
        return "codex" in model_norm or model_norm.startswith("gpt-5.6")

    @staticmethod
    def _should_retry_with_responses_api(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            "not a chat model" in message
            or "not supported in the v1/chat/completions endpoint" in message
            or (
                "chat/completions" in message
                and "responses" in message
                and "endpoint" in message
            )
        )

    @staticmethod
    def _build_responses_payload(
        *,
        model: str,
        normalized_messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int,
        max_completion_tokens: int | None,
        attachments: list[dict[str, Any]],
        reasoning_mode: str,
        reasoning_effort: str,
        web_search_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": (
                OpenAIClient._build_responses_input_messages(
                    normalized_messages, attachments=attachments
                )
                if attachments
                else normalized_messages
            ),
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_completion_tokens is not None:
            payload["max_output_tokens"] = max_completion_tokens
        else:
            payload["max_output_tokens"] = max_tokens
        reasoning: dict[str, Any] = {}
        if reasoning_effort:
            reasoning["effort"] = reasoning_effort
        if reasoning_mode == "pro":
            reasoning["mode"] = "pro"
        if reasoning:
            payload["reasoning"] = reasoning
        if web_search_policy:
            payload["tools"] = [{"type": "web_search"}]
            payload["tool_choice"] = (
                "required"
                if str(web_search_policy.get("mode") or "auto") == "required"
                else "auto"
            )
            payload["max_tool_calls"] = max(
                1,
                min(3, int(web_search_policy.get("max_operations") or 3)),
            )
            payload["include"] = ["web_search_call.action.sources"]
        return payload

    @staticmethod
    def _apply_prompt_cache_options(
        payload: dict[str, Any], cache_context: "CacheContext"
    ) -> None:
        if not cache_context.enabled or not openai_prompt_cache_enabled():
            return
        payload["prompt_cache_key"] = cache_context.cache_scope_key
        if openai_extended_prompt_cache_enabled():
            payload["prompt_cache_retention"] = "24h"

    @staticmethod
    def _last_user_message_index(messages: list[dict[str, Any]]) -> int | None:
        idx = None
        for i, message in enumerate(messages):
            role = str(message.get("role") or "").strip().lower()
            if role == "user":
                idx = i
        return idx

    @staticmethod
    def _responses_text_content_type(role: str) -> str:
        return "output_text" if role == "assistant" else "input_text"

    @classmethod
    def _build_chat_completions_messages(
        cls,
        normalized_messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not attachments:
            return normalized_messages

        out: list[dict[str, Any]] = []
        last_user_idx = cls._last_user_message_index(normalized_messages)
        for idx, message in enumerate(normalized_messages):
            role = str(message.get("role") or "user").strip().lower()
            text = cls._normalize_message_text(message)
            if last_user_idx is not None and idx == last_user_idx:
                content_parts: list[dict[str, Any]] = []
                for attachment in attachments:
                    data_uri = (
                        f"data:{attachment['mime_type']};base64,{attachment['data_base64']}"
                    )
                    content_parts.append(
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    )
                if text:
                    text_block = {"type": "text", "text": text}
                    if cache_friendly_prompt_ordering_enabled():
                        content_parts.append(text_block)
                    else:
                        content_parts.insert(0, text_block)
                out.append({"role": role, "content": content_parts or [{"type": "text", "text": ""}]})
            else:
                out.append({"role": role, "content": text})
        return out

    @classmethod
    def _build_responses_input_messages(
        cls,
        normalized_messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        last_user_idx = cls._last_user_message_index(normalized_messages)

        for idx, message in enumerate(normalized_messages):
            role = str(message.get("role") or "user").strip().lower()
            text = cls._normalize_message_text(message)
            text_content_type = cls._responses_text_content_type(role)
            content_items: list[dict[str, Any]] = []
            text_block = {"type": text_content_type, "text": text} if text else None
            if text_block and not cache_friendly_prompt_ordering_enabled():
                content_items.append(text_block)

            if attachments and last_user_idx is not None and idx == last_user_idx:
                for attachment in attachments:
                    mime_type = attachment["mime_type"]
                    data_uri = f"data:{mime_type};base64,{attachment['data_base64']}"
                    if mime_type.startswith("image/"):
                        content_items.append({"type": "input_image", "image_url": data_uri})
                    elif mime_type == "application/pdf":
                        content_items.append(
                            {
                                "type": "input_file",
                                "filename": attachment.get("filename") or "file.pdf",
                                "file_data": data_uri,
                            }
                        )

            if text_block and cache_friendly_prompt_ordering_enabled():
                content_items.append(text_block)

            if not content_items:
                content_items.append({"type": text_content_type, "text": ""})

            out.append({"role": role, "content": content_items})

        return out

    def _create_openai_response_with_compat_retries(
        self,
        *,
        request_id: str,
        model: str,
        payload: dict[str, Any],
        stream_observer: Any = None,
    ) -> tuple[Any, str, dict[str, Any] | None]:
        try:
            return self._create_responses_completion(payload, stream_observer), "responses", None
        except Exception as exc:
            if stream_observer is not None and stream_observer.has_emitted:
                raise
            dropped_param, retry_payload = self._build_retry_payload_without_unsupported_parameter(
                payload,
                exc,
                safe_parameters={
                    "temperature",
                    "top_p",
                    "presence_penalty",
                    "frequency_penalty",
                    "max_output_tokens",
                    "max_completion_tokens",
                    "reasoning_effort",
                    "reasoning",
                    "prompt_cache_key",
                    "prompt_cache_retention",
                },
            )
            if retry_payload is not None and dropped_param is not None:
                logger.warning(
                    "Retrying OpenAI responses request without unsupported parameter",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "model": model,
                            "retry_reason": "unsupported_parameter",
                            "dropped_param": dropped_param,
                        }
                    },
                )
                return self._create_responses_completion(
                    retry_payload, stream_observer
                ), "responses", {
                    "dropped_param": dropped_param,
                    "retry_reason": "unsupported_parameter",
                    "endpoint": "responses",
                }
            raise

    def _create_responses_completion(
        self,
        payload: dict[str, Any],
        stream_observer: Any,
    ) -> Any:
        if stream_observer is None:
            return self.client.responses.create(**payload)

        with self.client.responses.stream(**payload) as stream:
            for event in stream:
                stream_observer.emit_provider_event(event)
                if str(self._field(event, "type", "")).lower() == "response.output_text.delta":
                    stream_observer.emit_text(self._field(event, "delta", ""))
            stream_observer.raise_if_cancelled()
            return stream.get_final_response()

    def _create_chat_completion(
        self,
        payload: dict[str, Any],
        stream_observer: Any,
    ) -> Any:
        if stream_observer is None:
            return self.client.chat.completions.create(**payload)

        with self.client.chat.completions.stream(**payload) as stream:
            for event in stream:
                stream_observer.emit_provider_event(event)
                event_type = str(self._field(event, "type", "")).lower()
                if event_type == "content.delta":
                    stream_observer.emit_text(self._field(event, "delta", ""))
            stream_observer.raise_if_cancelled()
            return stream.get_final_completion()

    @staticmethod
    def _field(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    @classmethod
    def _extract_chat_completions_text(cls, response: Any) -> str:
        if not getattr(response, "choices", None):
            return ""

        first_choice = response.choices[0]
        message = cls._field(first_choice, "message")
        content = cls._field(message, "content")

        if isinstance(content, str) and content.strip():
            return content

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                part_type = str(cls._field(item, "type", "") or "").lower()
                if part_type in {"text", "output_text"}:
                    part_text = cls._field(item, "text") or cls._field(item, "content")
                    if part_text:
                        parts.append(str(part_text))
            combined = "".join(parts).strip()
            if combined:
                return combined

        refusal_text = cls._field(message, "refusal")
        if refusal_text:
            return str(refusal_text)

        return ""

    @classmethod
    def _extract_responses_text(cls, response: Any) -> str:
        output_text = cls._field(response, "output_text")
        if output_text:
            return str(output_text)

        output_items = cls._field(response, "output", [])
        collected: list[str] = []
        for item in output_items or []:
            content_items = cls._field(item, "content", [])
            for part in content_items or []:
                part_type = str(cls._field(part, "type", "") or "").lower()
                if part_type not in {"output_text", "text"}:
                    continue
                text = cls._field(part, "text")
                if text:
                    collected.append(str(text))

        return "".join(collected).strip()

    @classmethod
    def _extract_responses_usage(cls, response: Any) -> TokenUsage:
        usage = cls._field(response, "usage")
        return cls._openai_compatible_token_usage(usage)

    @classmethod
    def _extract_responses_finish_reason(cls, response: Any) -> str | None:
        finish_reason = cls._field(response, "finish_reason")
        if finish_reason:
            return str(finish_reason)

        incomplete = cls._field(response, "incomplete_details")
        incomplete_reason = str(cls._field(incomplete, "reason", "") or "").lower()
        if "max_output_tokens" in incomplete_reason or "max_tokens" in incomplete_reason:
            return "length"

        status = str(cls._field(response, "status", "") or "").lower()
        if status == "completed":
            return "stop"
        if status in {"failed", "incomplete", "cancelled"}:
            return "error"
        return None

    @staticmethod
    def _serialize_raw_response(response: Any) -> dict[str, Any] | None:
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump()
            except Exception:
                return None
        if isinstance(response, dict):
            return response
        return None

    @staticmethod
    def _should_retry_with_max_completion_tokens(exc: Exception) -> bool:
        """
        Detect OpenAI 400 errors indicating max_tokens is unsupported for the selected model.
        """
        message = str(exc).lower()
        return (
            "unsupported parameter" in message
            and "max_tokens" in message
            and "max_completion_tokens" in message
        )

    @classmethod
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        """
        List all available OpenAI models.

        Args:
            api_key: The OpenAI API key
            **kwargs: Additional parameters
                - current_model: The currently selected model (will be highlighted)
        """
        try:
            client = openai.OpenAI(api_key=api_key) if api_key else None
            if not client:
                logger.warning("API key not provided for listing models")
                print("API key not provided. Cannot list available models.")
                return

            current_model = kwargs.get("current_model", "gpt-5.1-codex")

            # Get the list of available models
            models = client.models.list()

            logger.info(
                "Listed available OpenAI models",
                extra={
                    "extra_fields": {
                        "model_count": len(models.data),
                        "current_model": current_model,
                    }
                },
            )

            print("\n=== Available OpenAI Models ===")
            for model in sorted(models.data, key=lambda x: x.id):
                prefix = "* " if model.id == current_model else "  "
                print(f"{prefix}{model.id}")
            print("* = currently selected\n")

        except Exception as e:
            logger.error(
                f"Error listing available models: {e!s}",
                extra={"extra_fields": {"error_type": type(e).__name__}},
            )
            print(f"Error listing available models: {e!s}")
