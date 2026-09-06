"""Pure decisions for per-run Cortex Work output guardrails."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkOutputGuardrailDecision:
    finalize: bool
    interrupt: bool
    limit_reached: bool


def resolve_output_guardrail(
    *,
    output_tokens: int,
    max_output_tokens: int,
    finalize_threshold: int,
    provider_interruptible: bool,
    finalize_already_requested: bool,
    interrupt_already_requested: bool,
) -> WorkOutputGuardrailDecision:
    used = max(0, int(output_tokens))
    maximum = max(1, int(max_output_tokens))
    finalize_at = min(max(1, int(finalize_threshold)), maximum - 1)
    limit_reached = used >= maximum
    return WorkOutputGuardrailDecision(
        finalize=bool(
            used >= finalize_at
            and not limit_reached
            and provider_interruptible
            and not finalize_already_requested
        ),
        interrupt=bool(
            limit_reached and provider_interruptible and not interrupt_already_requested
        ),
        limit_reached=limit_reached,
    )
