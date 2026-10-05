"""Thread-safe bridge from orchestration callbacks to NDJSON activity events."""

from __future__ import annotations

import asyncio
import concurrent.futures
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
from typing import Any, Callable

from models.cortex_activity import (
    CortexActivitySignal,
    CortexActivityType,
    sanitize_activity_metadata,
)
from models.provider_stream import ProviderStreamObserver, ProviderTextDelta


ActivityStreamItem = tuple[int | None, CortexActivitySignal | ProviderTextDelta]


class ActivityStream:
    """Queue normalized signals from provider worker threads for one request stream."""

    def __init__(
        self,
        *,
        request_id: str,
        conversation_id: str | None,
        mode: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.request_id = str(request_id or "").strip()
        self.conversation_id = str(conversation_id or "").strip() or None
        self.mode = str(mode or "").strip() or "chat"
        self.loop = loop
        self.queue: asyncio.Queue[ActivityStreamItem] = asyncio.Queue(maxsize=256)
        self.sequence_number = 0
        self._last_signature_by_index: dict[int | None, tuple[Any, ...]] = {}
        self._cancelled = Event()

    def callback(
        self,
        *,
        index: int | None,
        provider: str = "",
        model: str = "",
    ) -> Callable[[CortexActivitySignal], None]:
        def emit(signal: CortexActivitySignal) -> None:
            resolved = replace(
                signal,
                provider=signal.provider or str(provider or "").strip() or None,
                model=signal.model or str(model or "").strip() or None,
            )
            self.publish(resolved, index=index)

        return emit

    def publish(self, signal: CortexActivitySignal, *, index: int | None = None) -> None:
        self._publish_item((index, signal))

    def publish_text(self, delta: ProviderTextDelta, *, index: int | None = None) -> None:
        self._publish_item((index, delta))

    def _publish_item(self, item: ActivityStreamItem) -> None:
        if self._cancelled.is_set():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is self.loop:
            self.queue.put_nowait(item)
            return

        try:
            pending = asyncio.run_coroutine_threadsafe(self.queue.put(item), self.loop)
            while not self._cancelled.is_set():
                try:
                    pending.result(timeout=0.25)
                    return
                except concurrent.futures.TimeoutError:
                    continue
                except (concurrent.futures.CancelledError, RuntimeError):
                    return
            pending.cancel()
        except RuntimeError:
            # A synchronous provider worker may finish after its HTTP stream was
            # cancelled and the owning test/event loop was closed.
            return

    def observer(
        self,
        *,
        index: int | None,
        provider: str,
        model: str,
    ) -> ProviderStreamObserver:
        return ProviderStreamObserver(
            provider=provider,
            model=model,
            activity_callback=self.callback(index=index, provider=provider, model=model),
            text_callback=lambda delta: self.publish_text(delta, index=index),
            cancelled=self._cancelled,
        )

    def cancel(self) -> None:
        self._cancelled.set()

    def publish_type(
        self,
        event_type: CortexActivityType,
        *,
        index: int | None = None,
        provider: str = "",
        model: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.publish(
            CortexActivitySignal(
                event_type=event_type,
                provider=str(provider or "").strip() or None,
                model=str(model or "").strip() or None,
                metadata=sanitize_activity_metadata(metadata),
            ),
            index=index,
        )

    async def get(self) -> ActivityStreamItem:
        return await self.queue.get()

    def drain(self) -> list[ActivityStreamItem]:
        items: list[ActivityStreamItem] = []
        while True:
            try:
                items.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    def serialize(
        self,
        signal: CortexActivitySignal,
        *,
        index: int | None,
    ) -> dict[str, Any] | None:
        metadata = sanitize_activity_metadata(signal.metadata)
        signature = (
            signal.event_type.value,
            signal.provider,
            signal.model,
            tuple(sorted(metadata.items())),
        )
        if self._last_signature_by_index.get(index) == signature:
            return None
        self._last_signature_by_index[index] = signature
        self.sequence_number += 1
        activity = {
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "provider": signal.provider,
            "model": signal.model,
            "mode": self.mode,
            "event_type": signal.event_type.value,
            "phase": signal.phase,
            "display_message": signal.display_message,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sequence_number": self.sequence_number,
            "metadata": metadata,
        }
        payload: dict[str, Any] = {"type": "activity", "activity": activity}
        if index is not None:
            payload["index"] = index
        return payload
