"""Provider-neutral bridge for live SDK events and answer text.

Provider adapters use this internal observer only when an HTTP streaming route
explicitly opts in.  The observer keeps raw SDK payloads inside the adapter
boundary and exposes only safe Cortex activity signals plus answer deltas.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock
from typing import Any, Callable

from models.cortex_activity import (
    CortexActivitySignal,
    CortexActivityType,
    ProviderEventNormalizer,
)


class ProviderStreamCancelled(RuntimeError):
    """Raised inside a provider worker after its owning HTTP stream closes."""


@dataclass(frozen=True)
class ProviderTextDelta:
    """A real incremental text fragment emitted by a provider SDK."""

    text: str
    provider: str | None = None
    model: str | None = None


class ProviderStreamObserver:
    """Translate one provider stream into safe activity and text callbacks."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        activity_callback: Callable[[CortexActivitySignal], None],
        text_callback: Callable[[ProviderTextDelta], None],
        cancelled: Event | None = None,
    ) -> None:
        self.provider = str(provider or "").strip().lower()
        self.model = str(model or "").strip()
        self._activity_callback = activity_callback
        self._text_callback = text_callback
        self._cancelled = cancelled or Event()
        self._locally_cancelled = Event()
        self._normalizer = ProviderEventNormalizer(self.provider, model=self.model)
        self._lock = Lock()
        self._answer_started = False
        self._provider_event_count = 0
        self._text_delta_count = 0

    @property
    def has_emitted(self) -> bool:
        with self._lock:
            return bool(self._provider_event_count or self._text_delta_count)

    @property
    def has_emitted_text(self) -> bool:
        with self._lock:
            return self._text_delta_count > 0

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set() or self._locally_cancelled.is_set()

    def cancel(self) -> None:
        self._locally_cancelled.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise ProviderStreamCancelled("Provider stream was cancelled")

    def emit_provider_event(self, raw_event: Any) -> None:
        """Normalize a raw SDK event; unknown and terminal events are ignored."""

        self.raise_if_cancelled()
        signals = self._normalizer.normalize(raw_event)
        if not signals:
            return
        with self._lock:
            self._provider_event_count += 1
        for signal in signals:
            self._activity_callback(signal)

    def emit_text(self, text: Any) -> None:
        """Publish a provider-supplied text delta without altering whitespace."""

        self.raise_if_cancelled()
        if text is None:
            return
        delta = str(text)
        if not delta:
            return

        with self._lock:
            first_delta = not self._answer_started
            if first_delta:
                self._answer_started = True
            self._text_delta_count += 1

        if first_delta:
            self._activity_callback(
                CortexActivitySignal(
                    CortexActivityType.ANSWER_STARTED,
                    provider=self.provider or None,
                    model=self.model or None,
                )
            )
            self._activity_callback(
                CortexActivitySignal(
                    CortexActivityType.ANSWER_DELTA,
                    provider=self.provider or None,
                    model=self.model or None,
                )
            )
        self._text_callback(
            ProviderTextDelta(
                text=delta,
                provider=self.provider or None,
                model=self.model or None,
            )
        )
