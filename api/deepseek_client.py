import json
import time
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Callable

import openai

from config.web_search import load_native_web_search_config
from models.unified_response import NormalizedError, TokenUsage, UnifiedResponse
from tools.web.provider_metadata import build_web_search_metadata, normalize_web_sources
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)


class _DeepSeekWebSearchLoopError(RuntimeError):
    def __init__(
        self,
        cause: Exception,
        *,
        token_usage: TokenUsage,
        operations: int,
        sources: list[dict[str, str]],
        usage_estimated: bool,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.token_usage = token_usage
        self.operations = operations
        self.sources = sources
        self.usage_estimated = usage_estimated


class DeepSeekClient(BaseAIClient):
    """
    DeepSeek API client returning UnifiedResponse.

    Uses OpenAI SDK with custom base URL since DeepSeek API is OpenAI-compatible.
    All responses are normalized to UnifiedResponse format.
    """

    def __init__(self, api_key: str, model_name: str = "deepseek-chat", **kwargs):
        """
        Initialize the DeepSeek client.

        Args:
            api_key: The DeepSeek API key
            model_name: The name of the model to use (default: deepseek-chat)
                Options:
                - "deepseek-chat" (V3.2): Best for general chat and discussion
                - "deepseek-reasoner" (R1): Best for reasoning, math, and coding tasks
            **kwargs: Additional keyword arguments
        """
        super().__init__(api_key, model_name=model_name, **kwargs)
        self.client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
        self.model_name = model_name
        self.cost_calculator = CostCalculator(model_type="deepseek", model_name=model_name)

    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        """
        Get a completion from the DeepSeek API.

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
        self._resolve_cache_context(kwargs, provider="deepseek", model=model)
        temperature = kwargs.get("temperature", 0.7)
        max_tokens = kwargs.get("max_tokens", 2048)
        reasoning_mode = str(kwargs.get("reasoning_mode") or "thinking").strip().lower()
        reasoning_effort = str(kwargs.get("reasoning_effort") or "high").strip().lower()
        attachments = self._normalize_inference_attachments(kwargs.pop("attachments", None))
        web_search_policy = kwargs.pop("web_search_policy", {}) or {}
        web_search_executor = kwargs.pop("web_search_executor", None)
        web_config = load_native_web_search_config()
        web_search_enabled = (
            web_config.provider_enabled("deepseek")
            and str(web_search_policy.get("mode") or "off") != "off"
            and callable(web_search_executor)
        )

        try:
            # Normalize input to messages format
            normalized_messages = self._normalize_input(prompt=prompt, messages=messages)
            normalized_messages, binary_attachments = self._merge_text_attachments_into_messages(
                normalized_messages,
                attachments,
            )
            if binary_attachments:
                error = NormalizedError(
                    code="bad_request",
                    message=(
                        "DeepSeek models in this gateway do not support binary attachment inputs. "
                        "Use an image/PDF-capable model (OpenAI/Gemini/Claude/Grok), or send "
                        "text-extracted attachments."
                    ),
                    provider="deepseek",
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
                "messages": normalized_messages,
                "max_tokens": max_tokens,
            }
            thinking_enabled = reasoning_mode not in {"none", "off", "disabled"}
            request_payload["extra_body"] = {
                "thinking": {"type": "enabled" if thinking_enabled else "disabled"}
            }
            if thinking_enabled:
                if reasoning_effort in {"xhigh", "max"}:
                    request_payload["reasoning_effort"] = "max"
                elif reasoning_effort in {"minimal", "low"}:
                    request_payload["reasoning_effort"] = "low"
                else:
                    request_payload["reasoning_effort"] = "high"
            else:
                request_payload["temperature"] = temperature
            adaptive_retry = None
            search_operations = 0
            search_sources: list[dict[str, str]] = []
            search_usage_estimated = False
            provider_calls: list[dict[str, Any]] = []
            provider_started_at = datetime.fromtimestamp(start_time, timezone.utc)

            try:
                if web_search_enabled:
                    (
                        response,
                        token_usage,
                        search_operations,
                        search_sources,
                        search_usage_estimated,
                        provider_calls,
                    ) = self._run_web_search_loop(
                        request_payload=request_payload,
                        policy=web_search_policy,
                        executor=web_search_executor,
                    )
                else:
                    provider_started_at = datetime.now(timezone.utc)
                    response = self.client.chat.completions.create(**request_payload)
                    token_usage = self._openai_compatible_token_usage(
                        response.usage if hasattr(response, "usage") else None
                    )
            except Exception as request_exc:
                if web_search_enabled:
                    raise
                dropped_param, retry_payload = (
                    self._build_retry_payload_without_unsupported_parameter(
                        request_payload,
                        request_exc,
                        safe_parameters={
                            "temperature",
                            "top_p",
                            "presence_penalty",
                            "frequency_penalty",
                            "max_tokens",
                        },
                    )
                )
                if retry_payload is not None and dropped_param is not None:
                    logger.warning(
                        "Retrying DeepSeek request without unsupported parameter",
                        extra={
                            "extra_fields": {
                                "request_id": request_id,
                                "model": model,
                                "retry_reason": "unsupported_parameter",
                                "dropped_param": dropped_param,
                            }
                        },
                    )
                    provider_started_at = datetime.now(timezone.utc)
                    response = self.client.chat.completions.create(**retry_payload)
                    token_usage = self._openai_compatible_token_usage(
                        response.usage if hasattr(response, "usage") else None
                    )
                    adaptive_retry = {
                        "dropped_param": dropped_param,
                        "retry_reason": "unsupported_parameter",
                        "endpoint": "chat.completions",
                    }
                else:
                    raise

            latency_ms = self._measure_latency(start_time)

            # Extract text
            text = response.choices[0].message.content or ""

            served_model = self._served_model(getattr(response, "model", model), model)

            # Calculate cost
            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("deepseek", served_model)
            )
            cost = (
                calculator.calculate_calls(provider_calls)
                if provider_calls
                else calculator.calculate_cost(
                    request_at=provider_started_at,
                    prompt_tokens=token_usage.prompt_tokens,
                    completion_tokens=token_usage.completion_tokens,
                    cached_input_tokens=token_usage.cached_input_tokens,
                    cache_write_tokens=token_usage.cache_write_tokens,
                    reasoning_tokens=token_usage.reasoning_tokens,
                )
            )
            estimated_cost = cost["total_cost"]

            # Normalize finish reason
            finish_reason = self._normalize_finish_reason(
                response.choices[0].finish_reason if response.choices else None, provider="deepseek"
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

            logger.info(
                "DeepSeek completion successful",
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
                "endpoint": "chat.completions",
                "pricing_unknown": bool(cost.get("pricing_unknown", False)),
                **build_web_search_metadata(
                    request_at=datetime.fromtimestamp(start_time, timezone.utc),
                    provider="deepseek",
                    backend="tavily",
                    requested_mode=str(web_search_policy.get("requested_mode") or "off"),
                    effective_mode=(
                        str(web_search_policy.get("mode") or "off") if web_search_enabled else "off"
                    ),
                    operations=search_operations,
                    sources=search_sources,
                    usage_estimated=search_usage_estimated,
                ),
            }
            if adaptive_retry:
                metadata["adaptive_retry"] = adaptive_retry

            return UnifiedResponse(
                request_id=request_id,
                text=text,
                provider="deepseek",
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
                    reasoning_mode="thinking" if thinking_enabled else "none",
                ),
            )

        except Exception as e:
            latency_ms = self._measure_latency(start_time)
            error_source = e.cause if isinstance(e, _DeepSeekWebSearchLoopError) else e
            error = self._normalize_error(error_source, provider="deepseek")

            logger.error(
                f"DeepSeek completion failed: {error.code}",
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

            error_response = self._create_error_response(
                request_id=request_id, error=error, latency_ms=latency_ms, model=model
            )
            if isinstance(e, _DeepSeekWebSearchLoopError):
                return replace(
                    error_response,
                    token_usage=e.token_usage,
                    metadata=build_web_search_metadata(
                        request_at=datetime.fromtimestamp(start_time, timezone.utc),
                        provider="deepseek",
                        backend="tavily",
                        requested_mode=str(web_search_policy.get("requested_mode") or "off"),
                        effective_mode=str(web_search_policy.get("mode") or "off"),
                        operations=e.operations,
                        sources=e.sources,
                        status="error",
                        usage_estimated=e.usage_estimated,
                        error=error.code,
                    ),
                )
            return error_response

    def _run_web_search_loop(
        self,
        *,
        request_payload: dict[str, Any],
        policy: dict[str, Any],
        executor: Callable[[str], dict[str, Any]],
    ) -> tuple[Any, TokenUsage, int, list[dict[str, str]], bool, list[dict[str, Any]]]:
        """Run the bounded client-side tool loop used only by DeepSeek."""
        cap = max(1, min(3, int(policy.get("max_operations") or 3)))
        payload = dict(request_payload)
        messages = [dict(message) for message in payload.get("messages", [])]
        tool = {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": (
                    "Search the web for current or externally verifiable information. "
                    "Use returned numbered sources in the final answer as [n]."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 500}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }
        payload["tools"] = [tool]
        payload["tool_choice"] = (
            "required" if str(policy.get("mode") or "auto") == "required" else "auto"
        )
        total_usage = TokenUsage()
        operations = 0
        attempts = 0
        usage_estimated = False
        source_candidates: list[dict[str, str]] = []
        final_response: Any = None
        provider_calls: list[dict[str, Any]] = []

        for _round in range(cap + 1):
            payload["messages"] = messages
            try:
                provider_started_at = datetime.now(timezone.utc)
                final_response = self.client.chat.completions.create(**payload)
            except Exception as exc:
                raise _DeepSeekWebSearchLoopError(
                    exc,
                    token_usage=total_usage,
                    operations=operations,
                    sources=normalize_web_sources(source_candidates, limit=8),
                    usage_estimated=usage_estimated,
                ) from exc
            call_usage = self._openai_compatible_token_usage(
                final_response.usage if hasattr(final_response, "usage") else None
            )
            total_usage = self._add_usage(total_usage, call_usage)
            provider_calls.append(
                {
                    "model": self._served_model(
                        getattr(final_response, "model", payload["model"]), payload["model"]
                    ),
                    "request_at": provider_started_at,
                    "prompt_tokens": call_usage.prompt_tokens,
                    "completion_tokens": call_usage.completion_tokens,
                    "cached_input_tokens": call_usage.cached_input_tokens,
                    "cache_write_tokens": call_usage.cache_write_tokens,
                    "reasoning_tokens": call_usage.reasoning_tokens,
                }
            )
            choices = getattr(final_response, "choices", None) or []
            if not choices:
                break
            assistant = choices[0].message
            tool_calls = list(getattr(assistant, "tool_calls", None) or [])
            if not tool_calls:
                break

            assistant_message = (
                assistant.model_dump(exclude_none=True)
                if hasattr(assistant, "model_dump")
                else {
                    "role": "assistant",
                    "content": getattr(assistant, "content", None),
                    "tool_calls": tool_calls,
                }
            )
            messages.append(assistant_message)
            for call in tool_calls:
                call_id = str(getattr(call, "id", "") or "")
                function = getattr(call, "function", None)
                name = str(getattr(function, "name", "") or "")
                if name != "search_web" or attempts >= cap:
                    result = {"ok": False, "error": "web_search_operation_limit_reached"}
                else:
                    try:
                        arguments = json.loads(str(getattr(function, "arguments", "{}") or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        arguments = {}
                    query = str(arguments.get("query") or "").strip()[:500]
                    attempts += 1
                    if query:
                        try:
                            result = executor(query)
                        except Exception as exc:
                            result = {
                                "ok": False,
                                "error": f"search_failed:{type(exc).__name__}",
                                "sources": [],
                                "provider_credits_used": 0,
                                "provider_credits_estimated": False,
                            }
                    else:
                        result = {
                            "ok": False,
                            "error": "search_query_required",
                            "sources": [],
                            "provider_credits_used": 0,
                            "provider_credits_estimated": False,
                        }
                    if int(result.get("provider_credits_used") or 0) > 0:
                        operations += 1
                    usage_estimated = usage_estimated or bool(
                        result.get("provider_credits_estimated")
                    )
                    for source in result.get("sources", []) or []:
                        if isinstance(source, dict):
                            source_candidates.append(source)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )

            if attempts > 0 and "tools" in payload:
                payload["tool_choice"] = "auto"

            if attempts >= cap:
                payload.pop("tools", None)
                payload.pop("tool_choice", None)
                messages.append(
                    {
                        "role": "system",
                        "content": "Web-search limit reached. Answer now using the returned sources.",
                    }
                )

        if final_response is None:
            raise RuntimeError("DeepSeek web-search loop produced no response")
        return (
            final_response,
            total_usage,
            operations,
            normalize_web_sources(source_candidates, limit=8),
            usage_estimated,
            provider_calls,
        )

    @staticmethod
    def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=left.prompt_tokens + right.prompt_tokens,
            completion_tokens=left.completion_tokens + right.completion_tokens,
            total_tokens=left.total_tokens + right.total_tokens,
            cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
            cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
            reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        )

    @classmethod
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        """
        List all available DeepSeek models.

        Args:
            api_key: The DeepSeek API key
            **kwargs: Additional parameters
                - current_model: The currently selected model (will be highlighted)
        """
        try:
            if not api_key:
                logger.warning("API key not provided for listing DeepSeek models")
                print("API key not provided. Cannot list available models.")
                return

            client = openai.OpenAI(api_key=api_key, base_url="https://api.deepseek.com/v1")
            current_model = kwargs.get("current_model", "deepseek-chat")

            # Get the list of available models
            models = client.models.list()

            logger.info(
                "Listed available DeepSeek models",
                extra={
                    "extra_fields": {
                        "model_count": len(models.data),
                        "current_model": current_model,
                    }
                },
            )

            print("\n=== Available DeepSeek Models ===")
            for model in sorted(models.data, key=lambda x: x.id):
                prefix = "* " if model.id == current_model else "  "
                # Add description for known models
                description = ""
                if model.id == "deepseek-chat":
                    description = " (V3.2 - General chat & discussion)"
                elif model.id == "deepseek-reasoner":
                    description = " (R1 - Advanced reasoning & coding)"
                print(f"{prefix}{model.id}{description}")
            print("* = currently selected\n")

        except Exception as e:
            logger.error(
                f"Error listing available DeepSeek models: {e!s}",
                extra={"extra_fields": {"error_type": type(e).__name__}},
            )
            print(f"Error listing available models: {e!s}")
