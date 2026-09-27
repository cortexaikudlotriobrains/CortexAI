import importlib
import sys
import time
from typing import Any

from config.cache_optimization import (
    cache_friendly_prompt_ordering_enabled,
    claude_prompt_cache_enabled,
)
from config.web_search import load_native_web_search_config

anthropic: Any
try:
    import anthropic as anthropic_sdk

    anthropic = anthropic_sdk
    _anthropic_import_error: Exception | None = None
except Exception as exc:
    anthropic = None
    _anthropic_import_error = exc

from models.unified_response import TokenUsage, UnifiedResponse
from tools.web.provider_metadata import (
    build_web_search_metadata,
    field_value,
    normalize_web_sources,
    sequence_value,
)
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)


def _resolve_anthropic_module():
    """Load anthropic lazily so runtime installs can recover without code changes."""
    global anthropic, _anthropic_import_error

    if anthropic is not None:
        return anthropic

    try:
        anthropic = importlib.import_module("anthropic")
        _anthropic_import_error = None
        return anthropic
    except Exception as exc:
        _anthropic_import_error = exc
        raise ValueError(
            "anthropic package is not available in the active interpreter "
            f"({sys.executable}). Install dependencies into this interpreter with "
            f"\"{sys.executable}\" -m pip install -r requirements.txt. "
            f"Import error: {exc}"
        ) from exc


class ClaudeClient(BaseAIClient):
    """
    Anthropic Claude client returning UnifiedResponse.

    All responses are normalized to UnifiedResponse format.
    """

    def __init__(self, api_key: str, model_name: str = "claude-sonnet-4-5", **kwargs):
        anthropic_module = _resolve_anthropic_module()
        super().__init__(api_key, model_name=model_name, **kwargs)
        self.client = anthropic_module.Anthropic(api_key=api_key)
        self.model_name = model_name
        self.cost_calculator = CostCalculator(model_type="claude", model_name=model_name)

    def _convert_messages_to_claude_format(
        self,
        messages: list[dict[str, Any]],
        *,
        attachments: list[dict[str, Any]] | None = None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """
        Convert normalized messages into Anthropic Messages API shape.
        """
        system_instruction = None
        claude_messages: list[dict[str, Any]] = []
        attachments = attachments or []

        last_user_index = None
        for idx, msg in enumerate(messages):
            if str(msg.get("role", "")).strip().lower() == "user":
                last_user_index = idx

        for idx, msg in enumerate(messages):
            role = str(msg.get("role", "user")).strip().lower()
            content = self._normalize_message_text(msg)

            if role == "system":
                if system_instruction is None:
                    system_instruction = content
                continue

            if role not in {"user", "assistant"}:
                role = "user"

            content_blocks: list[dict[str, Any]] = []
            if content and not cache_friendly_prompt_ordering_enabled():
                content_blocks.append({"type": "text", "text": content})

            if attachments and last_user_index is not None and idx == last_user_index:
                for attachment in attachments:
                    mime_type = attachment["mime_type"]
                    if mime_type.startswith("image/"):
                        content_blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": attachment["data_base64"],
                                },
                            }
                        )

                    elif mime_type == "application/pdf":
                        content_blocks.append(
                            {
                                "type": "document",
                                "title": attachment.get("filename") or "file.pdf",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": attachment["data_base64"],
                                },
                            }
                        )

            if content and cache_friendly_prompt_ordering_enabled():
                content_blocks.append({"type": "text", "text": content})

            claude_messages.append(
                {
                    "role": role,
                    "content": content_blocks or [{"type": "text", "text": ""}],
                }
            )

        if not claude_messages:
            claude_messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]

        return system_instruction, claude_messages

    def _extract_text(self, response: Any) -> str:
        parts = getattr(response, "content", None) or []
        chunks: list[str] = []
        for part in parts:
            part_type = getattr(part, "type", "")
            if part_type == "text":
                chunks.append(str(getattr(part, "text", "") or ""))
        return "".join(chunks).strip()

    @classmethod
    def _extract_text_and_web_search(
        cls, response: Any
    ) -> tuple[str, int, list[dict[str, str]]]:
        parts = sequence_value(field_value(response, "content", []))
        raw_sources: list[dict[str, str]] = []
        operations = 0
        for part in parts:
            part_type = str(field_value(part, "type", "") or "").lower()
            if part_type == "server_tool_use" and str(
                field_value(part, "name", "") or ""
            ).lower() == "web_search":
                operations += 1
            if part_type == "web_search_tool_result":
                for result in sequence_value(field_value(part, "content", [])):
                    url = str(field_value(result, "url", "") or "")
                    if url:
                        raw_sources.append(
                            {
                                "url": url,
                                "title": str(field_value(result, "title", "") or ""),
                            }
                        )
            for citation in sequence_value(field_value(part, "citations", [])):
                url = str(field_value(citation, "url", "") or "")
                if url:
                    raw_sources.append(
                        {
                            "url": url,
                            "title": str(field_value(citation, "title", "") or ""),
                        }
                    )

        usage = field_value(response, "usage")
        server_usage = field_value(usage, "server_tool_use")
        try:
            reported = int(field_value(server_usage, "web_search_requests", 0) or 0)
        except (TypeError, ValueError):
            reported = 0
        operations = max(operations, reported)
        sources = normalize_web_sources(raw_sources, limit=8)
        source_index = {
            source["url"].casefold().rstrip("/"): index + 1
            for index, source in enumerate(sources)
        }
        chunks: list[str] = []
        for part in parts:
            if str(field_value(part, "type", "") or "").lower() != "text":
                continue
            chunk = str(field_value(part, "text", "") or "")
            citations = []
            for citation in sequence_value(field_value(part, "citations", [])):
                number = source_index.get(
                    str(field_value(citation, "url", "") or "").casefold().rstrip("/")
                )
                if number is not None and number not in citations:
                    citations.append(number)
            marker = "".join(f"[{number}]" for number in citations)
            chunks.append(f"{chunk}{marker}" if marker and marker not in chunk else chunk)
        return "".join(chunks).strip(), operations, sources

    @staticmethod
    def _supports_custom_temperature(model: str, reasoning_mode: str) -> bool:
        """Return whether Anthropic accepts a non-default temperature here."""
        normalized_model = str(model or "").strip().lower()
        normalized_mode = str(reasoning_mode or "").strip().lower()
        if normalized_mode not in {"", "none", "off", "disabled"}:
            return False
        # Claude 5 requires default sampling even when thinking is disabled.
        # Current 4.5/4.6 models still accept custom sampling when thinking is off.
        return any(marker in normalized_model for marker in ("-4-5", "-4-6"))

    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        request_id = self._resolve_request_id_from_kwargs(kwargs)
        start_time = time.time()

        requested_model = kwargs.get("model")
        model = (
            requested_model
            if isinstance(requested_model, str)
            else str(self.model_name or "claude-sonnet-4-5")
        )
        cache_context = self._resolve_cache_context(
            kwargs, provider="claude", model=model
        )
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens", 2048)
        reasoning_mode = str(kwargs.get("reasoning_mode") or "").strip().lower()
        reasoning_effort = str(kwargs.get("reasoning_effort") or "").strip().lower()
        attachments = self._normalize_inference_attachments(kwargs.pop("attachments", None))
        web_search_policy = kwargs.pop("web_search_policy", {}) or {}
        kwargs.pop("web_search_executor", None)
        web_config = load_native_web_search_config()
        web_search_enabled = (
            web_config.provider_enabled("claude")
            and str(web_search_policy.get("mode") or "off") != "off"
        )

        try:
            normalized_messages = self._normalize_input(prompt=prompt, messages=messages)
            normalized_messages, binary_attachments = self._merge_text_attachments_into_messages(
                normalized_messages,
                attachments,
            )
            system_instruction, claude_messages = self._convert_messages_to_claude_format(
                normalized_messages,
                attachments=binary_attachments,
            )

            request_payload: dict[str, Any] = {
                "model": model,
                "messages": claude_messages,
                "max_tokens": max_tokens,
            }
            if temperature is not None and self._supports_custom_temperature(
                model, reasoning_mode
            ):
                request_payload["temperature"] = temperature
            elif temperature is not None:
                logger.info(
                    "Omitting incompatible Claude temperature",
                    extra={
                        "extra_fields": {
                            "request_id": request_id,
                            "model": model,
                            "reasoning_mode": reasoning_mode or "provider_default",
                        }
                    },
                )
            if system_instruction:
                request_payload["system"] = system_instruction
            if cache_context.enabled and claude_prompt_cache_enabled():
                request_payload["cache_control"] = {"type": "ephemeral"}
            if reasoning_mode == "adaptive":
                request_payload["thinking"] = {"type": "adaptive"}
            elif reasoning_mode == "disabled":
                request_payload["thinking"] = {"type": "disabled"}
            if reasoning_mode == "adaptive" and reasoning_effort not in {"", "none"}:
                request_payload["output_config"] = {"effort": reasoning_effort}
            if web_search_enabled:
                request_payload["tools"] = [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": max(
                            1,
                            min(3, int(web_search_policy.get("max_operations") or 3)),
                        ),
                    }
                ]
                request_payload["tool_choice"] = {
                    "type": (
                        "any"
                        if str(web_search_policy.get("mode") or "auto") == "required"
                        else "auto"
                    )
                }

            adaptive_retry = None
            try:
                response = self.client.messages.create(**request_payload)
            except Exception as request_exc:
                dropped_param, retry_payload = self._build_retry_payload_without_unsupported_parameter(
                    request_payload,
                    request_exc,
                    safe_parameters={
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "cache_control",
                    },
                )
                if retry_payload is not None and dropped_param is not None:
                    logger.warning(
                        "Retrying Claude request without unsupported parameter",
                        extra={
                            "extra_fields": {
                                "request_id": request_id,
                                "model": model,
                                "retry_reason": "unsupported_parameter",
                                "dropped_param": dropped_param,
                            }
                        },
                    )
                    response = self.client.messages.create(**retry_payload)
                    adaptive_retry = {
                        "dropped_param": dropped_param,
                        "retry_reason": "unsupported_parameter",
                        "endpoint": "messages.create",
                    }
                else:
                    raise
            latency_ms = self._measure_latency(start_time)

            text, search_operations, search_sources = self._extract_text_and_web_search(response)
            usage = getattr(response, "usage", None)
            normal_input_tokens = self._usage_int(getattr(usage, "input_tokens", 0))
            cached_input_tokens = self._usage_int(
                getattr(usage, "cache_read_input_tokens", 0)
            )
            cache_write_tokens = self._usage_int(
                getattr(usage, "cache_creation_input_tokens", 0)
            )
            prompt_tokens = normal_input_tokens + cached_input_tokens + cache_write_tokens
            completion_tokens = self._usage_int(getattr(usage, "output_tokens", 0))
            token_usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
            )

            served_model = self._served_model(getattr(response, "model", model), model)

            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("claude", served_model)
            )
            cost = calculator.calculate_cost(
                token_usage.prompt_tokens,
                token_usage.completion_tokens,
                cached_input_tokens=token_usage.cached_input_tokens,
                cache_write_tokens=token_usage.cache_write_tokens,
            )
            estimated_cost = cost["total_cost"]

            finish_reason = self._normalize_finish_reason(
                getattr(response, "stop_reason", None),
                provider="claude",
            )

            raw = None
            if save_full:
                raw = {
                    "id": getattr(response, "id", None),
                    "model": getattr(response, "model", model),
                    "stop_reason": getattr(response, "stop_reason", None),
                    "usage": {
                        "input_tokens": prompt_tokens,
                        "output_tokens": completion_tokens,
                        "total_tokens": token_usage.total_tokens,
                    },
                }

            logger.info(
                "Claude completion successful",
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
                "endpoint": "messages.create",
                "pricing_unknown": bool(cost.get("pricing_unknown", False)),
                **build_web_search_metadata(
                    provider="claude",
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
                provider="claude",
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
            error = self._normalize_error(e, provider="claude")

            logger.error(
                f"Claude completion failed: {error.code}",
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
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        try:
            anthropic_module = _resolve_anthropic_module()
            if not api_key:
                logger.warning("API key not provided for listing Claude models")
                print("API key not provided. Cannot list available models.")
                return

            current_model = kwargs.get("current_model", "claude-sonnet-4-5")
            client = anthropic_module.Anthropic(api_key=api_key)

            fallback_models = [
                "claude-3-5-haiku-latest",
                "claude-3-5-sonnet-latest",
                "claude-sonnet-4",
                "claude-sonnet-4-5",
                "claude-opus-4-5",
            ]
            model_ids: list[str] = []

            try:
                if hasattr(client, "models") and hasattr(client.models, "list"):
                    listed = client.models.list()
                    if hasattr(listed, "data"):
                        model_ids = [
                            str(getattr(model, "id", "") or "")
                            for model in listed.data
                            if str(getattr(model, "id", "") or "").strip()
                        ]
                    else:
                        model_ids = [
                            str(getattr(model, "id", "") or "")
                            for model in listed
                            if str(getattr(model, "id", "") or "").strip()
                        ]
            except Exception:
                model_ids = []

            if not model_ids:
                model_ids = fallback_models

            logger.info(
                "Listed available Claude models",
                extra={
                    "extra_fields": {
                        "model_count": len(model_ids),
                        "current_model": current_model,
                    }
                },
            )

            print("\n=== Available Claude Models ===")
            for model_id in sorted(model_ids):
                prefix = "* " if model_id == current_model else "  "
                print(f"{prefix}{model_id}")
            print("* = currently selected\n")

        except Exception as e:
            logger.error(
                f"Error listing available Claude models: {e!s}",
                extra={"extra_fields": {"error_type": type(e).__name__}},
            )
            print(f"Error listing available models: {e!s}")
