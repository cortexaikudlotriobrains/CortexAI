import time

import openai

from models.unified_response import NormalizedError, UnifiedResponse
from utils.cost_calculator import CostCalculator
from utils.logger import get_logger

from .base_client import BaseAIClient

logger = get_logger(__name__)


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

            try:
                response = self.client.chat.completions.create(**request_payload)
            except Exception as request_exc:
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
                    response = self.client.chat.completions.create(**retry_payload)
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

            # Extract token usage
            token_usage = self._openai_compatible_token_usage(
                response.usage if hasattr(response, "usage") else None
            )
            served_model = self._served_model(getattr(response, "model", model), model)

            # Calculate cost
            calculator = (
                self.cost_calculator
                if self.cost_calculator.model_name == served_model
                else CostCalculator("deepseek", served_model)
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
                metadata=(
                    {
                        "endpoint": "chat.completions",
                        "adaptive_retry": adaptive_retry,
                        "pricing_unknown": bool(cost.get("pricing_unknown", False)),
                    }
                    if adaptive_retry
                    else {
                        "endpoint": "chat.completions",
                        "pricing_unknown": bool(cost.get("pricing_unknown", False)),
                    }
                ),
                raw=raw,
                **self._response_audit_fields(
                    served_model=served_model,
                    cost=cost,
                    reasoning_mode="thinking" if thinking_enabled else "none",
                ),
            )

        except Exception as e:
            latency_ms = self._measure_latency(start_time)
            error = self._normalize_error(e, provider="deepseek")

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

            return self._create_error_response(
                request_id=request_id, error=error, latency_ms=latency_ms, model=model
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
