import time
from typing import Any

import openai

from config.cache_optimization import (
    cache_friendly_prompt_ordering_enabled,
    grok_prompt_cache_enabled,
)
from config.web_search import load_native_web_search_config
from models.unified_response import NormalizedError, UnifiedResponse
from tools.web.provider_metadata import (
    build_web_search_metadata,
    extract_responses_web_search,
    field_value,
)
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)


class GrokClient(BaseAIClient):
    """
    Grok API client returning UnifiedResponse.

    Uses OpenAI SDK with custom base URL since Grok API is OpenAI-compatible.
    All responses are normalized to UnifiedResponse format.
    """

    def __init__(self, api_key: str, model_name: str = "grok-4-latest", **kwargs):
        """
        Initialize the Grok client.

        Args:
            api_key: The Grok API key (from X.AI)
            model_name: The name of the model to use (default: grok-4-latest)
            **kwargs: Additional keyword arguments
        """
        super().__init__(api_key, model_name=model_name, **kwargs)
        self.client = openai.OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
        self.model_name = model_name
        self.cost_calculator = CostCalculator(model_type="grok", model_name=model_name)

    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        """
        Get a completion from the Grok API.

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

        model = kwargs.get("model", self.model_name)
        cache_context = self._resolve_cache_context(
            kwargs, provider="grok", model=model
        )
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens", 2048)
        reasoning_setting = str(kwargs.get("reasoning_mode") or "").strip().lower()
        reasoning_mode = (
            str(kwargs.get("reasoning_effort") or "").strip()
            if reasoning_setting not in {"none", "off", "disabled"}
            else None
        ) or None
        attachments = self._normalize_inference_attachments(kwargs.pop("attachments", None))
        web_search_policy = kwargs.pop("web_search_policy", {}) or {}
        kwargs.pop("web_search_executor", None)
        web_config = load_native_web_search_config()
        web_search_enabled = (
            web_config.provider_enabled("grok")
            and str(web_search_policy.get("mode") or "off") != "off"
        )

        try:
            normalized_messages = self._normalize_input(prompt=prompt, messages=messages)
            normalized_messages, binary_attachments = self._merge_text_attachments_into_messages(
                normalized_messages,
                attachments,
            )
            unsupported_attachments = [
                attachment["mime_type"]
                for attachment in binary_attachments
                if not str(attachment["mime_type"]).startswith("image/")
            ]
            if unsupported_attachments:
                error = NormalizedError(
                    code="bad_request",
                    message=(
                        "Grok adapter currently supports image attachments only. "
                        f"Unsupported MIME types: {', '.join(sorted(set(unsupported_attachments)))}"
                    ),
                    provider="grok",
                    retryable=False,
                )
                return self._create_error_response(
                    request_id=request_id,
                    error=error,
                    latency_ms=self._measure_latency(start_time),
                    model=model,
                )

            request_payload = {
                "model": model,
                "messages": self._build_chat_messages(
                    normalized_messages, attachments=binary_attachments
                ),
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if reasoning_mode:
                request_payload["reasoning_effort"] = reasoning_mode
            if cache_context.enabled and grok_prompt_cache_enabled():
                request_payload["extra_headers"] = {
                    "x-grok-conv-id": cache_context.cache_scope_key
                }
            adaptive_retry = None
            endpoint = "chat.completions"

            try:
                if web_search_enabled:
                    cap = max(1, min(3, int(web_search_policy.get("max_operations") or 3)))
                    response_payload: dict[str, Any] = {
                        "model": model,
                        "input": self._build_responses_messages(
                            normalized_messages,
                            attachments=binary_attachments,
                            web_instruction=(
                                f"Use no more than {cap} web searches. "
                                + (
                                    "Web search is required for this request."
                                    if str(web_search_policy.get("mode") or "auto")
                                    == "required"
                                    else "Search only when current or externally verifiable information is needed."
                                )
                            ),
                        ),
                        "tools": [{"type": "web_search"}],
                        "tool_choice": (
                            "required"
                            if str(web_search_policy.get("mode") or "auto") == "required"
                            else "auto"
                        ),
                        "max_output_tokens": max_tokens,
                        "max_tool_calls": cap,
                        "parallel_tool_calls": False,
                        "extra_body": {"max_turns": cap},
                    }
                    if temperature is not None:
                        response_payload["temperature"] = temperature
                    if reasoning_mode:
                        response_payload["reasoning"] = {"effort": reasoning_mode}
                    response = self.client.responses.create(**response_payload)
                    endpoint = "responses"
                else:
                    response = self.client.chat.completions.create(**request_payload)
            except Exception as request_exc:
                if web_search_enabled:
                    raise
                dropped_param, retry_payload = self._build_retry_payload_without_unsupported_parameter(
                    request_payload,
                    request_exc,
                    safe_parameters={
                        "temperature",
                        "top_p",
                        "presence_penalty",
                        "frequency_penalty",
                        "max_tokens",
                        "reasoning_effort",
                        "extra_headers",
                    },
                )
                if retry_payload is not None and dropped_param is not None:
                    logger.warning(
                        "Retrying Grok request without unsupported parameter",
                        extra={
                            "extra_fields": {
                                "request_id": request_id,
                                "model": model,
                                "retry_reason": "unsupported_parameter",
                                "dropped_param": dropped_param,
                            }
                        },
                    )
                    response = self.client.chat.completions.create(**retry_payload)
                    adaptive_retry = {
                        "dropped_param": dropped_param,
                        "retry_reason": "unsupported_parameter",
                        "endpoint": "chat.completions",
                    }
                else:
                    raise

            latency_ms = self._measure_latency(start_time)

            if endpoint == "responses":
                text = self._extract_responses_text(response)
                text, search_operations, search_sources = extract_responses_web_search(
                    response,
                    text=text,
                )
                search_operations = min(search_operations, cap)
            else:
                text = response.choices[0].message.content or ""
                search_operations = 0
                search_sources = []

            # Extract token usage
            token_usage = self._openai_compatible_token_usage(
                response.usage if hasattr(response, "usage") else None
            )
            served_model = self._served_model(getattr(response, "model", model), model)

            # Calculate cost
            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("grok", served_model)
            )
            cost = calculator.calculate_cost(
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
                cached_input_tokens=token_usage.cached_input_tokens,
                cache_write_tokens=token_usage.cache_write_tokens,
                reasoning_tokens=token_usage.reasoning_tokens,
            )
            estimated_cost = cost["total_cost"]

            # Normalize finish reason
            finish_reason = self._normalize_finish_reason(
                (
                    field_value(response, "finish_reason")
                    if endpoint == "responses"
                    else (response.choices[0].finish_reason if response.choices else None)
                ),
                provider="grok",
            )

            # Build raw response if requested
            raw = None
            if save_full and endpoint == "responses":
                raw = (
                    response.model_dump()
                    if hasattr(response, "model_dump")
                    else (response if isinstance(response, dict) else None)
                )
            elif save_full:
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

            logger.info(
                "Grok completion successful",
                extra={
                    "extra_fields": {
                        "request_id": request_id,
                        "model": model,
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
                    provider="grok",
                    backend="web_search",
                    requested_mode=str(web_search_policy.get("requested_mode") or "off"),
                    effective_mode=(
                        str(web_search_policy.get("mode") or "off")
                        if web_search_enabled
                        else "off"
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
                provider="grok",
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
                    reasoning_mode=reasoning_mode,
                ),
            )

        except Exception as e:
            latency_ms = self._measure_latency(start_time)
            error = self._normalize_error(e, provider="grok")

            logger.error(
                f"Grok completion failed: {error.code}",
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

    @classmethod
    def _build_chat_messages(
        cls,
        normalized_messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not attachments:
            return normalized_messages

        out: list[dict[str, Any]] = []
        last_user_idx = None
        for idx, message in enumerate(normalized_messages):
            if str(message.get("role") or "").strip().lower() == "user":
                last_user_idx = idx

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
    def _build_responses_messages(
        cls,
        normalized_messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]],
        web_instruction: str,
    ) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": [{"type": "input_text", "text": web_instruction}]}
        ]
        last_user_idx = max(
            (
                index
                for index, message in enumerate(normalized_messages)
                if str(message.get("role") or "").lower() == "user"
            ),
            default=-1,
        )
        for index, message in enumerate(normalized_messages):
            role = str(message.get("role") or "user").lower()
            content_type = "output_text" if role == "assistant" else "input_text"
            content: list[dict[str, Any]] = [
                {"type": content_type, "text": cls._normalize_message_text(message)}
            ]
            if index == last_user_idx:
                for attachment in attachments:
                    content.append(
                        {
                            "type": "input_image",
                            "image_url": (
                                f"data:{attachment['mime_type']};base64,"
                                f"{attachment['data_base64']}"
                            ),
                        }
                    )
            messages.append({"role": role, "content": content})
        return messages

    @staticmethod
    def _extract_responses_text(response: Any) -> str:
        output_text = field_value(response, "output_text")
        if output_text:
            return str(output_text)
        chunks: list[str] = []
        for item in field_value(response, "output", []) or []:
            for part in field_value(item, "content", []) or []:
                if str(field_value(part, "type", "") or "") in {"output_text", "text"}:
                    chunks.append(str(field_value(part, "text", "") or ""))
        return "".join(chunks).strip()

    @classmethod
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        """
        List all available Grok models.

        Args:
            api_key: The Grok API key
            **kwargs: Additional parameters
                - current_model: The currently selected model (will be highlighted)
        """
        try:
            if not api_key:
                logger.warning("API key not provided for listing Grok models")
                print("API key not provided. Cannot list available models.")
                return

            client = openai.OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
            current_model = kwargs.get("current_model", "grok-4-latest")

            # Get the list of available models
            models = client.models.list()

            logger.info(
                "Listed available Grok models",
                extra={
                    "extra_fields": {
                        "model_count": len(models.data),
                        "current_model": current_model,
                    }
                },
            )

            print("\n=== Available Grok Models ===")
            for model in sorted(models.data, key=lambda x: x.id):
                prefix = "* " if model.id == current_model else "  "
                # Add description for known models
                description = ""
                if model.id == "grok-4-latest":
                    description = " (Latest Grok model)"
                print(f"{prefix}{model.id}{description}")
            print("* = currently selected\n")

        except Exception as e:
            logger.error(
                f"Error listing available Grok models: {e!s}",
                extra={"extra_fields": {"error_type": type(e).__name__}},
            )
            print(f"Error listing available models: {e!s}")
