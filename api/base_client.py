"""
Base AI Client with Unified Response Contract

All provider clients MUST inherit from this class and return UnifiedResponse.
This enforces the "locked" contract across all providers.
"""

import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING
import re

from config.cache_optimization import cache_friendly_prompt_ordering_enabled
from models.unified_response import FinishReason, NormalizedError, TokenUsage, UnifiedResponse
from utils.logger import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    from orchestrator.cache_context import CacheContext


class BaseAIClient(ABC):
    """
    Abstract base class for AI model clients.

    All provider-specific clients MUST:
    1. Inherit from this class
    2. Implement get_completion() returning UnifiedResponse
    3. Use provided helpers for timing, request_id, error normalization
    4. Never raise exceptions - return UnifiedResponse with error instead
    """

    def __init__(self, api_key: str, **kwargs):
        """
        Initialize the AI client.

        Args:
            api_key: API key for the AI service
            **kwargs: Additional model-specific parameters
        """
        self.api_key = api_key
        self.model_name = kwargs.get("model_name")
        self.requested_model_name = kwargs.get("requested_model_name") or self.model_name
        model_identity = kwargs.get("model_identity")
        self.model_identity = dict(model_identity) if isinstance(model_identity, dict) else {}
        self.provider_name = self.__class__.__name__.replace("Client", "").lower()

    @abstractmethod
    def get_completion(
        self,
        prompt: str | None = None,
        *,
        messages: list | None = None,
        save_full: bool = False,
        **kwargs,
    ) -> UnifiedResponse:
        """
        Get a completion from the AI model.

        THIS IS THE LOCKED CONTRACT. All providers MUST return UnifiedResponse.

        Args:
            prompt: (Legacy) Single string prompt - converted to [{"role": "user", "content": prompt}]
            messages: (Multi-turn) List of message dicts with 'role' and 'content' keys.
                     Format: [{"role": "system|user|assistant", "content": str}, ...]
            save_full: If True, include raw provider response in response.raw
            **kwargs: Additional parameters for the API call

        Returns:
            UnifiedResponse: Normalized response object

        IMPORTANT:
        - If messages is provided, use it as the full conversation context
        - If messages is None but prompt is provided, convert prompt to messages format
        - If both are None, return UnifiedResponse with error
        - NEVER raise exceptions - catch all errors and return UnifiedResponse with error
        - Use helper methods: _generate_request_id(), _measure_latency(), _normalize_error()
        - Fill all required fields (text, provider, model, token_usage, etc.)
        - Use CostCalculator for estimated_cost
        """
        pass

    @classmethod
    @abstractmethod
    def list_available_models(cls, api_key: str = None, **kwargs) -> None:
        """
        List all available models for this client.

        Args:
            api_key: Optional API key (if not provided during initialization)
            **kwargs: Additional parameters for the API call
        """
        pass

    # ============================================================
    # HELPER METHODS - Use these in provider implementations
    # ============================================================

    def _normalize_input(
        self, prompt: str | None = None, messages: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """
        Normalize input to messages format.

        Converts legacy prompt parameter to messages format for backward compatibility.

        Args:
            prompt: Single string prompt (legacy)
            messages: List of message dicts (new multi-turn format)

        Returns:
            List of message dicts in standard format

        Raises:
            ValueError: If both prompt and messages are None
        """
        if messages is not None:
            # Use provided messages
            if not isinstance(messages, list):
                raise ValueError("messages must be a list")
            return messages

        if prompt is not None:
            # Convert prompt to messages format
            if not isinstance(prompt, str):
                raise ValueError("prompt must be a string")
            return [{"role": "user", "content": prompt}]

        # Neither provided
        raise ValueError("Either 'prompt' or 'messages' must be provided")

    @staticmethod
    def _resolve_cache_context(
        kwargs: dict[str, Any],
        *,
        provider: str,
        model: str,
    ) -> "CacheContext":
        from orchestrator.cache_context import CacheContext, build_cache_context

        supplied = kwargs.pop("cache_context", None)
        scope = kwargs.pop("_cache_scope", None)
        if isinstance(supplied, CacheContext):
            return supplied
        if not isinstance(scope, dict):
            return CacheContext()
        try:
            return build_cache_context(
                scope_id=str(scope.get("scope_id") or ""),
                provider=provider,
                model=model,
                mode=str(scope.get("mode") or "ask"),
                stable_context_hash=str(scope.get("stable_context_hash") or ""),
                retention_policy=str(scope.get("retention_policy") or "ephemeral"),
            )
        except Exception:
            # Cache affinity is an optimization and must never block inference.
            return CacheContext()

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        """Extract plain text from provider-neutral message content."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        text_chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip().lower()
            if block_type in {"text", "input_text", "output_text"}:
                value = block.get("text")
                if value is None:
                    value = block.get("content")
                if value:
                    text_chunks.append(str(value))
        return "".join(text_chunks).strip()

    @classmethod
    def _normalize_message_text(cls, message: dict[str, Any]) -> str:
        return cls._extract_text_from_content(message.get("content"))

    @staticmethod
    def _normalize_inference_attachments(attachments: Any) -> list[dict[str, Any]]:
        """Normalize attachment payloads passed from API layer to provider clients."""
        if not attachments:
            return []
        if not isinstance(attachments, list):
            raise ValueError("attachments must be a list when provided")

        normalized: list[dict[str, Any]] = []
        for idx, item in enumerate(attachments):
            if not isinstance(item, dict):
                raise ValueError(f"attachments[{idx}] must be an object")

            mime_type = str(item.get("mime_type") or "").strip().lower()
            data_base64 = str(item.get("data_base64") or "").strip()
            extracted_text = str(item.get("extracted_text") or "").strip()
            if not mime_type:
                raise ValueError(f"attachments[{idx}].mime_type is required")
            if not data_base64 and not extracted_text:
                raise ValueError(
                    f"attachments[{idx}] must include either data_base64 or extracted_text"
                )

            normalized.append(
                {
                    "file_id": str(item.get("file_id") or ""),
                    "filename": str(item.get("filename") or "file"),
                    "mime_type": mime_type,
                    "data_base64": data_base64,
                    "extracted_text": extracted_text,
                    "usage_role": str(item.get("usage_role") or "primary"),
                    "transform_mode": str(item.get("transform_mode") or "auto"),
                    "order_index": int(item.get("order_index", idx)),
                }
            )
        return normalized

    @classmethod
    def _merge_text_attachments_into_messages(
        cls,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """
        Merge extracted text attachments into the last user message.

        Returns:
            (messages_with_text_context, binary_attachments_only)
        """
        if not attachments:
            return messages, []

        text_attachments = [item for item in attachments if str(item.get("extracted_text") or "").strip()]
        binary_attachments = [item for item in attachments if str(item.get("data_base64") or "").strip()]
        if not text_attachments:
            return messages, binary_attachments

        text_block = cls._build_text_attachment_context(text_attachments)
        if not text_block:
            return messages, binary_attachments

        merged_messages: list[dict[str, Any]] = [dict(message) for message in (messages or [])]
        last_user_index = None
        for idx, message in enumerate(merged_messages):
            role = str(message.get("role") or "").strip().lower()
            if role == "user":
                last_user_index = idx

        if last_user_index is None:
            merged_messages.append({"role": "user", "content": text_block})
            return merged_messages, binary_attachments

        current = merged_messages[last_user_index]
        current_content = current.get("content")
        if isinstance(current_content, str):
            combined = current_content.strip()
            if combined:
                combined = (
                    f"{text_block}\n\n{combined}"
                    if cache_friendly_prompt_ordering_enabled()
                    else f"{combined}\n\n{text_block}"
                )
            else:
                combined = text_block
            current["content"] = combined
            return merged_messages, binary_attachments

        if isinstance(current_content, list):
            next_content = list(current_content)
            block = {"type": "text", "text": text_block}
            if cache_friendly_prompt_ordering_enabled():
                next_content.insert(0, block)
            else:
                next_content.append(block)
            current["content"] = next_content
            return merged_messages, binary_attachments

        current["content"] = f"{cls._extract_text_from_content(current_content)}\n\n{text_block}".strip()
        return merged_messages, binary_attachments

    @staticmethod
    def _build_text_attachment_context(attachments: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for attachment in attachments:
            filename = str(attachment.get("filename") or "file").strip() or "file"
            mime_type = str(attachment.get("mime_type") or "").strip().lower()
            extracted_text = str(attachment.get("extracted_text") or "").strip()
            if not extracted_text:
                continue
            parts.append(
                f"[Attachment: {filename} ({mime_type})]\n{extracted_text}"
            )
        if not parts:
            return ""
        return "Attachment context:\n\n" + "\n\n".join(parts)

    def _generate_request_id(self) -> str:
        """
        Generate a unique request ID for tracking.

        Returns:
            UUID string
        """
        return str(uuid.uuid4())

    def _resolve_request_id_from_kwargs(self, kwargs: dict[str, Any]) -> str:
        """
        Pop caller-provided request_id from kwargs when present.

        This lets API-layer correlation ids flow through provider clients while
        preserving backward compatibility for call sites that don't provide one.
        """
        provided = str(kwargs.pop("request_id", "") or "").strip()
        return provided or self._generate_request_id()

    @staticmethod
    def _usage_field(value: Any, name: str, default: Any = 0) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    @staticmethod
    def _usage_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _served_model(value: Any, fallback: str) -> str:
        return value.strip() if isinstance(value, str) and value.strip() else fallback

    @classmethod
    def _openai_compatible_token_usage(cls, usage: Any) -> TokenUsage:
        """Normalize OpenAI-compatible usage, including cache/reasoning details."""

        if usage is None:
            return TokenUsage()
        prompt_tokens = cls._usage_int(cls._usage_field(usage, "prompt_tokens", 0))
        if prompt_tokens <= 0:
            prompt_tokens = cls._usage_int(cls._usage_field(usage, "input_tokens", 0))
        completion_tokens = cls._usage_int(
            cls._usage_field(usage, "completion_tokens", 0)
        )
        if completion_tokens <= 0:
            completion_tokens = cls._usage_int(cls._usage_field(usage, "output_tokens", 0))
        prompt_details = cls._usage_field(
            usage,
            "prompt_tokens_details",
            cls._usage_field(usage, "input_tokens_details", None),
        )
        completion_details = cls._usage_field(
            usage,
            "completion_tokens_details",
            cls._usage_field(usage, "output_tokens_details", None),
        )
        cached_tokens = cls._usage_int(
            cls._usage_field(
                prompt_details,
                "cached_tokens",
                cls._usage_field(usage, "prompt_cache_hit_tokens", 0),
            )
            or 0
        )
        cache_write_tokens = cls._usage_int(
            cls._usage_field(
                prompt_details,
                "cache_write_tokens",
                cls._usage_field(usage, "cache_creation_input_tokens", 0),
            )
            or 0
        )
        reasoning_tokens = cls._usage_int(
            cls._usage_field(completion_details, "reasoning_tokens", 0) or 0
        )
        total_tokens = cls._usage_int(cls._usage_field(usage, "total_tokens", 0))
        if total_tokens <= 0:
            total_tokens = prompt_tokens + completion_tokens
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    def _response_audit_fields(
        self,
        *,
        served_model: str,
        cost: dict[str, Any],
        reasoning_mode: str | None = None,
    ) -> dict[str, Any]:
        requested_model = str(self.requested_model_name or self.model_name or served_model)
        return {
            "requested_model": requested_model,
            "served_model": served_model,
            "pricing_model": str(cost.get("pricing_model") or served_model),
            "model_lifecycle_status": str(
                self.model_identity.get("lifecycle_status") or "ACTIVE"
            ),
            "alias_redirected": bool(
                self.model_identity.get("alias_redirected", False)
                or requested_model != served_model
            ),
            "replacement_model": (
                str(self.model_identity.get("replacement_model") or "").strip() or None
            ),
            "migration_reason": (
                str(self.model_identity.get("migration_reason") or "").strip() or None
            ),
            "reasoning_mode": (
                reasoning_mode
                or str(self.model_identity.get("default_reasoning_mode") or "").strip()
                or None
            ),
            "pricing_rule_applied": str(cost.get("pricing_rule_applied") or "") or None,
            "pricing_unknown": bool(cost.get("pricing_unknown", False)),
            "pricing_snapshot": {
                **dict(cost.get("cost_audit") or {}),
                "rule_id": cost.get("pricing_rule_applied"),
                "pricing_version": cost.get("pricing_version"),
                "processing_mode": cost.get("processing_mode"),
                "long_context_applied": bool(cost.get("long_context_applied", False)),
                "source_url": cost.get("source_url"),
                "source_verified_at": cost.get("source_verified_at"),
                "rates_per_1m": dict(cost.get("rates_per_1m") or {}),
            },
            "pricing_version": str(cost.get("pricing_version") or "") or None,
        }

    def _measure_latency(self, start_time: float) -> int:
        """
        Calculate request latency in milliseconds.

        Args:
            start_time: Start time from time.time()

        Returns:
            Latency in milliseconds
        """
        return int((time.time() - start_time) * 1000)

    @staticmethod
    def _extract_http_status_code(message: str) -> int | None:
        match = re.search(r"\b(400|401|403|408|429|500|502|503|504)\b", message or "")
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    @staticmethod
    def _safe_provider_error_message(kind: str) -> str:
        messages = {
            "timeout": "The provider request timed out. Please retry in a moment.",
            "auth": "Provider authentication failed. Check the configured API key.",
            "rate_limited": "The provider is rate limiting requests. Please retry in a moment.",
            "quota_exceeded": "Provider quota is exhausted. Check billing or API key limits.",
            "bad_request": "The provider rejected the request. Check the selected model and request settings.",
            "transient_capacity": "This model is temporarily busy. Try again shortly or switch to another model.",
            "provider_5xx": "The provider is temporarily unavailable. Please retry in a moment.",
            "unknown": "The provider request failed unexpectedly. Please retry or choose another model.",
        }
        return messages.get(kind, messages["unknown"])

    @classmethod
    def _classify_exception(
        cls,
        exception: Exception,
    ) -> tuple[str, str, bool]:
        """
        Return (normalized_code, provider_error_kind, retryable).

        The normalized code stays within the public UnifiedResponse contract while
        `kind` gives routes/UI a stable, provider-agnostic display signal.
        """
        exc_str = str(exception).lower()
        status_code = cls._extract_http_status_code(exc_str)

        # Timeout errors
        if (
            status_code in {408, 504}
            or "timeout" in exc_str
            or "timed out" in exc_str
            or "deadline exceeded" in exc_str
        ):
            return "timeout", "timeout", True

        # Authentication errors
        if (
            status_code in {401, 403}
            or "unauthorized" in exc_str
            or "forbidden" in exc_str
            or "api key" in exc_str
            or "authentication" in exc_str
            or "permission denied" in exc_str
        ):
            return "auth", "auth", False

        # Hard quota/billing errors are commonly encoded as 429 but are not
        # fixed by an immediate retry.
        quota_phrases = (
            "insufficient quota",
            "quota exceeded",
            "exceeded your current quota",
            "billing",
            "hard limit",
        )
        if any(phrase in exc_str for phrase in quota_phrases):
            return "rate_limit", "quota_exceeded", False

        # Rate limit errors
        if (
            status_code == 429
            or "rate limit" in exc_str
            or "rate_limit" in exc_str
            or "too many requests" in exc_str
            or "resource exhausted" in exc_str
        ):
            return "rate_limit", "rate_limited", True

        # Bad request errors
        if status_code == 400 or "bad request" in exc_str or "invalid" in exc_str:
            return "bad_request", "bad_request", False

        transient_phrases = (
            "high demand",
            "overloaded",
            "temporarily busy",
            "temporarily unavailable",
            "temporary unavailable",
            "service unavailable",
            "currently unavailable",
            "currently experiencing",
            "try again later",
            "server busy",
            "capacity",
            "unavailable",
        )

        # Provider errors (5xx)
        if status_code in {500, 502, 503, 504} or "server error" in exc_str:
            kind = (
                "transient_capacity"
                if status_code == 503 or any(phrase in exc_str for phrase in transient_phrases)
                else "provider_5xx"
            )
            return "provider_error", kind, True

        if any(phrase in exc_str for phrase in transient_phrases):
            return "provider_error", "transient_capacity", True

        return "unknown", "unknown", False

    def _normalize_error(
        self, exception: Exception, provider: str | None = None
    ) -> NormalizedError:
        """
        Normalize provider-specific exceptions into standard error codes.

        Error Code Mapping:
        - timeout: Request timeout, connection timeout
        - auth: 401, 403, API key errors
        - rate_limit: 429, rate limit exceeded
        - bad_request: 400, invalid parameters
        - provider_error: 500, 502, 503, 504
        - unknown: All other errors

        Args:
            exception: The exception to normalize
            provider: Provider name (defaults to self.provider_name)

        Returns:
            NormalizedError with appropriate code and retryable flag
        """
        provider = provider or self.provider_name
        exc_type = type(exception).__name__
        exc_str = str(exception).lower()
        status_code = self._extract_http_status_code(exc_str)
        code, kind, retryable = self._classify_exception(exception)
        details: dict[str, Any] = {"exception_type": exc_type, "kind": kind}
        if status_code is not None:
            details["status_code"] = status_code

        return NormalizedError(
            code=code,
            message=self._safe_provider_error_message(kind),
            provider=provider,
            retryable=retryable,
            details=details,
        )

    def _normalize_finish_reason(
        self, provider_reason: str | None, provider: str | None = None
    ) -> FinishReason:
        """
        Normalize provider-specific finish reasons into standard codes.

        Standard Finish Reasons:
        - "stop": Natural completion
        - "length": Max tokens reached
        - "tool": Function/tool call
        - "content_filter": Content policy violation
        - "error": Request failed
        - None: Unknown/not provided

        Args:
            provider_reason: Provider-specific finish reason
            provider: Provider name (for logging)

        Returns:
            Normalized finish reason or None
        """
        if not provider_reason:
            return None

        reason_lower = provider_reason.lower()

        # Natural completion
        if reason_lower in ("stop", "end_turn", "complete", "finished"):
            return "stop"

        # Max tokens/length
        if (
            "length" in reason_lower
            or "max_tokens" in reason_lower
            or "token_limit" in reason_lower
        ):
            return "length"

        # Tool/function call
        if "tool" in reason_lower or "function" in reason_lower:
            return "tool"

        # Content filter
        if "content_filter" in reason_lower or "safety" in reason_lower or "policy" in reason_lower:
            return "content_filter"

        # Error
        if "error" in reason_lower:
            return "error"

        # Unknown - log for debugging
        logger.debug(
            f"Unknown finish reason from {provider or self.provider_name}: {provider_reason}",
            extra={"extra_fields": {"provider_reason": provider_reason}},
        )
        return None

    def _create_error_response(
        self, request_id: str, error: NormalizedError, latency_ms: int = 0, model: str | None = None
    ) -> UnifiedResponse:
        """
        Create a UnifiedResponse for error cases.

        Helper to ensure consistent error responses across all providers.

        Args:
            request_id: Request ID
            error: Normalized error
            latency_ms: Request latency
            model: Model name (defaults to self.model_name)

        Returns:
            UnifiedResponse with error details
        """
        return UnifiedResponse(
            request_id=request_id,
            text="",
            provider=self.provider_name,
            model=model or self.model_name or "unknown",
            requested_model=self.requested_model_name or model or self.model_name or "unknown",
            served_model=model or self.model_name or "unknown",
            pricing_model=(
                str(self.model_identity.get("pricing_model") or "")
                or model
                or self.model_name
                or "unknown"
            ),
            model_lifecycle_status=str(
                self.model_identity.get("lifecycle_status") or "UNKNOWN"
            ),
            alias_redirected=bool(self.model_identity.get("alias_redirected", False)),
            replacement_model=(
                str(self.model_identity.get("replacement_model") or "").strip() or None
            ),
            migration_reason=(
                str(self.model_identity.get("migration_reason") or "").strip() or None
            ),
            latency_ms=latency_ms,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=error,
            metadata={},
        )

    @staticmethod
    def _extract_unsupported_parameter(exception: Exception) -> str | None:
        """
        Try to parse unsupported parameter name from provider error messages.
        """
        message = str(exception)
        patterns = (
            r"Unsupported parameter:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?",
            r"Unknown parameter:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?",
            r"Unrecognized request argument supplied:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?",
            r"Parameter\s+['\"]?([A-Za-z0-9_.-]+)['\"]?\s+is not supported",
            r"Unsupported argument:\s*['\"]?([A-Za-z0-9_.-]+)['\"]?",
        )
        for pattern in patterns:
            match = re.search(pattern, message, flags=re.IGNORECASE)
            if match:
                return str(match.group(1) or "").strip().lower() or None
        return None

    def _build_retry_payload_without_unsupported_parameter(
        self,
        payload: dict[str, Any],
        exception: Exception,
        *,
        safe_parameters: set[str] | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """
        Return (dropped_param, retry_payload) when adaptive retry is safe, else (None, None).
        """
        unsupported = self._extract_unsupported_parameter(exception)
        if not unsupported:
            return None, None

        allowed = safe_parameters or {
            "temperature",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "reasoning_effort",
        }
        if unsupported not in allowed:
            return None, None

        key_lookup = {str(key).strip().lower(): key for key in payload.keys()}
        matching_key = key_lookup.get(unsupported)
        if matching_key is None:
            return None, None

        retry_payload = dict(payload)
        retry_payload.pop(matching_key, None)
        return str(matching_key), retry_payload
