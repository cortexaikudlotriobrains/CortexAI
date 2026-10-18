"""
MultiModelOrchestrator - Concurrent execution of prompts across multiple AI clients.

Provides both async and sync interfaces for running the same prompt against
multiple providers/models in parallel with per-call timeout handling.
"""

import asyncio
import concurrent.futures
import uuid
from datetime import datetime, timezone

from api.base_client import BaseAIClient
from orchestrator.cache_context import stable_context_digest
from orchestrator.compare_history import select_compare_messages
from models.multi_unified_response import MultiUnifiedResponse
from models.unified_response import NormalizedError, TokenUsage, UnifiedResponse
from utils.logger import get_logger

logger = get_logger(__name__)


class MultiModelOrchestrator:
    """
    Orchestrates parallel API calls to multiple AI clients.

    Example usage:
        orchestrator = MultiModelOrchestrator()
        clients = [openai_client, gemini_client, deepseek_client]
        result = orchestrator.get_comparisons_sync("What is Python?", clients)
        for resp in result.responses:
            print(f"{resp.provider}/{resp.model}: {resp.text[:100]}")
    """

    def __init__(self, default_timeout_s: float = 60.0):
        """
        Initialize the orchestrator.

        Args:
            default_timeout_s: Default timeout in seconds for each API call
        """
        self.default_timeout_s = default_timeout_s

    def _create_timeout_response(
        self, client: BaseAIClient, request_id: str, latency_ms: int
    ) -> UnifiedResponse:
        """
        Create a UnifiedResponse for timeout errors.

        Mirrors the pattern from BaseAIClient._create_error_response().
        """
        error = NormalizedError(
            code="timeout",
            message=f"Request timed out after {self.default_timeout_s}s",
            provider=client.provider_name,
            retryable=True,
            details={"timeout_seconds": self.default_timeout_s},
        )
        return UnifiedResponse(
            request_id=request_id,
            text="",
            provider=client.provider_name,
            model=client.model_name or "unknown",
            latency_ms=latency_ms,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=error,
            metadata={},
        )

    def _create_exception_response(
        self, client: BaseAIClient, request_id: str, exception: Exception, latency_ms: int
    ) -> UnifiedResponse:
        """
        Create a UnifiedResponse for unexpected exceptions.

        Uses the same error normalization pattern as BaseAIClient.
        """
        error = NormalizedError(
            code="unknown",
            message=f"Unexpected error: {exception!s}",
            provider=client.provider_name,
            retryable=False,
            details={"exception_type": type(exception).__name__},
        )
        return UnifiedResponse(
            request_id=request_id,
            text="",
            provider=client.provider_name,
            model=client.model_name or "unknown",
            latency_ms=latency_ms,
            token_usage=TokenUsage(),
            estimated_cost=0.0,
            finish_reason="error",
            error=error,
            metadata={},
        )

    async def _safe_call(
        self, client: BaseAIClient, prompt: str, timeout_s: float, **kwargs
    ) -> UnifiedResponse:
        """
        Safely call a client with timeout handling.

        Wraps the synchronous get_completion in an executor and applies timeout.
        Returns UnifiedResponse with error on timeout or unexpected exception.

        Note: If 'messages' is present in kwargs, it will be used instead of 'prompt'.
        """
        request_id = str(kwargs.get("request_id") or "").strip() or str(uuid.uuid4())
        start_time = asyncio.get_event_loop().time()

        try:
            # Extract messages from kwargs if present
            messages = kwargs.get("messages")

            # Determine which call pattern to use
            if messages is not None:
                # Use messages directly (for research-injected context or conversation history)
                kwargs2 = dict(kwargs)
                kwargs2.pop("messages", None)
                messages = select_compare_messages(
                    messages, prompt=prompt, provider=client.provider_name,
                    model=getattr(client, "requested_model_name", None) or client.model_name or "",
                )
                if isinstance(kwargs2.get("_cache_scope"), dict):
                    kwargs2["_cache_scope"] = {
                        **kwargs2["_cache_scope"],
                        "stable_context_hash": stable_context_digest(messages[:-1]),
                    }

                # DO NOT pass prompt= when using messages
                call_fn = lambda: client.get_completion(messages=messages, **kwargs2)
            else:
                # Use prompt (original behavior)
                call_fn = lambda: client.get_completion(prompt=prompt, **kwargs)

            # Run sync get_completion in thread pool
            loop = asyncio.get_event_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(None, call_fn), timeout=timeout_s
            )
            return response

        except asyncio.TimeoutError:
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            logger.warning(
                f"Timeout for {client.provider_name}/{client.model_name}",
                extra={
                    "extra_fields": {
                        "event": "provider.call.timeout",
                        "request_id": request_id,
                        "provider": client.provider_name,
                        "model": client.model_name,
                        "timeout_s": timeout_s,
                        "latency_ms": elapsed_ms,
                    }
                },
            )
            return self._create_timeout_response(client, request_id, elapsed_ms)

        except Exception as e:
            elapsed_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            logger.error(
                f"Unexpected error for {client.provider_name}/{client.model_name}: {e}",
                extra={
                    "extra_fields": {
                        "event": "provider.call.exception",
                        "request_id": request_id,
                        "provider": client.provider_name,
                        "model": client.model_name,
                        "error": str(e),
                        "error_type": type(e).__name__,
                        "latency_ms": elapsed_ms,
                    }
                },
            )
            return self._create_exception_response(client, request_id, e, elapsed_ms)

    async def get_comparisons(
        self,
        prompt: str,
        clients: list[BaseAIClient],
        timeout_s: float | None = None,
        request_group_id: str | None = None,
        **kwargs,
    ) -> MultiUnifiedResponse:
        """
        Execute prompt against multiple clients concurrently.

        Args:
            prompt: The prompt to send to all clients
            clients: List of AI clients to query
            timeout_s: Per-call timeout in seconds (defaults to self.default_timeout_s)
            request_group_id: Optional caller-provided group id to correlate logs/results
            **kwargs: Additional arguments passed to each client's get_completion

        Returns:
            MultiUnifiedResponse containing all responses in input order
        """
        timeout = timeout_s or self.default_timeout_s
        request_group_id = request_group_id or str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)

        logger.info(
            f"Starting comparison with {len(clients)} clients",
            extra={
                "extra_fields": {
                    "request_group_id": request_group_id,
                    "client_count": len(clients),
                    "timeout_s": timeout,
                }
            },
        )

        # Create tasks for all clients
        base_request_id = str(kwargs.get("request_id") or "").strip()
        per_client_generation = kwargs.pop("_per_client_generation", {}) or {}
        tasks = []
        for idx, client in enumerate(clients):
            call_kwargs = dict(kwargs)
            requested_model = getattr(client, "requested_model_name", None)
            client_key = f"{client.provider_name}:{requested_model or client.model_name}"
            client_generation = (
                per_client_generation.get(client_key, {})
                if isinstance(per_client_generation, dict)
                else {}
            )
            if isinstance(client_generation, dict):
                call_kwargs.update(client_generation)
            if base_request_id:
                call_kwargs["request_id"] = f"{base_request_id}:cmp:{idx}"
            tasks.append(self._safe_call(client, prompt, timeout, **call_kwargs))

        # Run all concurrently - no return_exceptions since _safe_call handles errors
        responses = await asyncio.gather(*tasks)

        result = MultiUnifiedResponse(
            request_group_id=request_group_id, created_at=created_at, responses=tuple(responses)
        )

        logger.info(
            f"Comparison complete: {result.success_count} success, {result.error_count} errors",
            extra={
                "extra_fields": {
                    "request_group_id": request_group_id,
                    "success_count": result.success_count,
                    "error_count": result.error_count,
                    "total_cost": result.total_cost,
                    "total_tokens": result.total_tokens,
                }
            },
        )

        return result

    def get_comparisons_sync(
        self,
        prompt: str,
        clients: list[BaseAIClient],
        timeout_s: float | None = None,
        request_group_id: str | None = None,
        **kwargs,
    ) -> MultiUnifiedResponse:
        """
        Synchronous wrapper for get_comparisons.

        Handles the case where an event loop is already running by
        executing in a separate thread with its own loop.

        Args:
            prompt: The prompt to send to all clients
            clients: List of AI clients to query
            timeout_s: Per-call timeout in seconds (defaults to self.default_timeout_s)
            request_group_id: Optional caller-provided group id to correlate logs/results
            **kwargs: Additional arguments passed to each client's get_completion

        Returns:
            MultiUnifiedResponse containing all responses in input order
        """
        try:
            # Check if loop is already running
            asyncio.get_running_loop()
            # Loop is running - execute in separate thread
            return self._run_in_new_thread(
                prompt,
                clients,
                timeout_s,
                request_group_id=request_group_id,
                **kwargs,
            )
        except RuntimeError:
            # No running loop - use asyncio.run directly
            return asyncio.run(
                self.get_comparisons(
                    prompt,
                    clients,
                    timeout_s,
                    request_group_id=request_group_id,
                    **kwargs,
                )
            )

    def _run_in_new_thread(
        self,
        prompt: str,
        clients: list[BaseAIClient],
        timeout_s: float | None,
        request_group_id: str | None = None,
        **kwargs,
    ) -> MultiUnifiedResponse:
        """
        Run the async comparison in a new thread with its own event loop.

        Used when called from within an existing event loop.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                asyncio.run,
                self.get_comparisons(
                    prompt,
                    clients,
                    timeout_s,
                    request_group_id=request_group_id,
                    **kwargs,
                ),
            )
            return future.result()
