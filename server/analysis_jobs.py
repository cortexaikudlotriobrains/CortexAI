"""Server-owned synthesis authorization, canonical billing and append-only runs."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder

from config.cache_optimization import CORTEX_ANALYSIS_POLICY_VERSION, cortex_analysis_reuse_enabled
from db import create_cortex_analysis_run, get_compare_analysis_sources, list_cortex_analysis_runs
from db import analysis_repository as jobs
from db import record_cache_reuse_event
from server import analysis_evidence, cortex_analysis as synthesis, persistence
from server import privacy
from server.billing.enforcement_service import (
    BillableModelUsage,
    ReservedModelEstimate,
    ReservedRequestUsage,
    resolve_model_target,
)
from server.billing.errors import enforcement_http_exception
from utils.logger import get_logger

logger = get_logger(__name__)
_tasks: set[asyncio.Task] = set()


def track_task(task: asyncio.Task) -> asyncio.Task:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def preflight() -> None:
    try:
        jobs.require_synthesis_schema()
        if privacy.is_metadata_only():
            raise RuntimeError("Analysis needs stored response text")
        if not str(os.getenv("OPENAI_API_KEY") or "").strip():
            raise RuntimeError("Analysis provider is not configured")
        resolve_model_target(provider="openai", model=synthesis.configured_analysis_model())
    except Exception as exc:
        raise HTTPException(
            503,
            detail={
                "code": "cortex_analysis_unavailable",
                "message": "Analysis is temporarily unavailable. Please try again later.",
            },
        ) from exc


def initial_reservation_input(question: str, context: Any, output_tokens: list[int]) -> str:
    # Response text and bounded metadata for up to eight supplied links per answer.
    return synthesis.reservation_input(question, [], context) + (
        "x" * (sum(output_tokens) * 8 + len(output_tokens) * 24000)
    )


def _serialize_reservation(reservation: ReservedRequestUsage) -> dict[str, Any]:
    payload = jsonable_encoder(asdict(reservation))
    payload["input_text"] = privacy.sanitize_user_message_for_storage(reservation.input_text)
    return payload


def _restore_reservation(payload: dict[str, Any]) -> ReservedRequestUsage:
    values = dict(payload)
    values["reservation_id"] = UUID(values["reservation_id"])
    values["reset_at"] = datetime.fromisoformat(values["reset_at"].replace("Z", "+00:00"))
    values["allowed_billing_classes"] = frozenset(values["allowed_billing_classes"])
    values["model_estimates"] = tuple(
        ReservedModelEstimate(**item) for item in values.get("model_estimates", [])
    )
    values["smart_candidates"] = ()
    return ReservedRequestUsage(**values)


def _reserve(
    user_id: UUID, key: str, input_text: str, question: str, *, model: str, output_tokens: int
) -> ReservedRequestUsage:
    try:
        return persistence.reserve_subscription_usage(
            user_id=user_id,
            request_id=key,
            operation_type="cortex_analysis",
            model_targets=(resolve_model_target(provider="openai", model=model),),
            research_enabled=False,
            input_text=input_text,
            initial_query=question,
            max_output_tokens=output_tokens,
        )
    except Exception as exc:
        raise enforcement_http_exception(exc) from exc


def authorize_initial(
    *, user_id: UUID, group_id: UUID, question: str, context: Any, output_tokens: list[int]
) -> None:
    preflight()
    context = analysis_evidence.bounded_context(context)
    context = [
        {
            "role": item["role"],
            "content": privacy.sanitize_user_message_for_storage(item["content"]),
        }
        for item in context
    ]
    model = synthesis.configured_analysis_model()
    maximum = synthesis.analysis_output_tokens()
    reservation = _reserve(
        user_id,
        f"compare-analysis:{group_id}:initial",
        initial_reservation_input(question, context, output_tokens),
        question,
        model=model,
        output_tokens=maximum,
    )
    maximum = (
        min(maximum, reservation.model_estimates[0].output_tokens)
        if reservation.model_estimates
        else maximum
    )
    try:
        with persistence.db_uow() as db:
            jobs.create_intent(
                db,
                request_group_id=group_id,
                user_id=user_id,
                state="waiting",
                automatic=True,
                context_snapshot=context,
                reservation_payload=_serialize_reservation(reservation),
                model=model,
                policy_version=CORTEX_ANALYSIS_POLICY_VERSION,
                max_output_tokens=maximum,
            )
    except BaseException:
        persistence.release_subscription_usage(
            reservation=reservation, reason="analysis_authorization_failed"
        )
        raise


def abandon_initial(user_id: UUID, group_id: UUID) -> None:
    with persistence.db_uow() as db:
        intent = jobs.get_intent(db, user_id, group_id)
        if not intent or intent["state"] not in {"waiting", "queued"}:
            return
        claimed = jobs.transition(
            db,
            user_id,
            group_id,
            expected=("waiting", "queued"),
            state="failed",
            error_code="compare_failed",
        )
    if claimed and intent.get("reservation_payload"):
        persistence.release_subscription_usage(
            reservation=_restore_reservation(intent["reservation_payload"]), reason="compare_failed"
        )


async def continue_initial(user_id: UUID, group_id: UUID) -> None:
    """Called by request orchestration/regeneration, never by a read/view endpoint."""
    with persistence.db_uow(commit_on_success=False) as db:
        intent = jobs.get_intent(db, user_id, group_id)
        payload = get_compare_analysis_sources(db, user_id, group_id)
    if not intent or not intent["automatic"] or intent["state"] != "waiting" or not payload:
        return
    if intent.get("cancel_requested"):
        abandon_initial(user_id, group_id)
        return
    sources, _ = synthesis.normalize_analysis_sources(payload["responses"])
    if len(sources) < 2:
        # Keep the authorization for a direct individual response retry, free the hold.
        with persistence.db_uow() as db:
            released = jobs.transition(
                db,
                user_id,
                group_id,
                expected=("waiting",),
                state="waiting",
                reservation_payload=None,
                error_code="insufficient_compare_responses",
            )
        if released and intent.get("reservation_payload"):
            persistence.release_subscription_usage(
                reservation=_restore_reservation(intent["reservation_payload"]),
                reason="insufficient_compare_responses",
            )
        return
    with persistence.db_uow() as db:
        queued = jobs.transition(
            db, user_id, group_id, expected=("waiting",), state="queued", error_code=None
        )
    if queued:
        await process_intent(user_id, group_id)


def _settle(reservation: ReservedRequestUsage, result: Any, model: str) -> None:
    usage = result.token_usage if hasattr(result, "token_usage") else result
    persistence.finalize_subscription_usage(
        reservation=reservation,
        successful_targets=(),
        model_usages=(
            BillableModelUsage(
                provider="openai",
                model=model,
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                output_text=getattr(result, "recommended_answer", getattr(result, "text", "")),
                provider_cost_usd=result.estimated_cost,
                pricing_snapshot=result.pricing_snapshot,
                pricing_version=result.pricing_version,
                usage_estimated=bool(getattr(result, "usage_estimated", False)),
            ),
        ),
        research_provider_credits_used=0,
    )


async def process_intent(user_id: UUID, group_id: UUID) -> dict[str, Any] | None:
    with persistence.db_uow() as db:
        claimed = jobs.transition(db, user_id, group_id, expected=("queued",),
                                  state="processing", progress_stage="comparing")
        intent = jobs.get_intent(db, user_id, group_id) if claimed else None
    if intent is None:
        return None
    reservation = None
    provider_returned = False
    try:
        with persistence.db_uow(commit_on_success=False) as db:
            payload = get_compare_analysis_sources(db, user_id, group_id)
        if payload is None:
            raise ValueError("Owned Compare run was not found")
        sources, failed_count = synthesis.normalize_analysis_sources(payload["responses"])
        if len(sources) < 2:
            raise ValueError("At least two successful responses are required")
        context = analysis_evidence.bounded_context(intent.get("context_snapshot"))
        question = str(payload.get("question") or "")
        model = intent["model"]
        reservation = (
            _restore_reservation(intent["reservation_payload"])
            if intent.get("reservation_payload")
            else _reserve(
                user_id,
                f"compare-analysis:{group_id}:{uuid4()}",
                synthesis.reservation_input(question, sources, context),
                question,
                model=model,
                output_tokens=int(intent["max_output_tokens"]),
            )
        )
        with persistence.db_uow() as db:
            jobs.transition(
                db,
                user_id,
                group_id,
                expected=("processing",),
                state="processing",
                reservation_payload=_serialize_reservation(reservation),
            )
        maximum = int(intent["max_output_tokens"])
        if reservation.model_estimates:
            maximum = min(maximum, reservation.model_estimates[0].output_tokens)
        def report_progress(stage: str) -> None:
            # Presentation metadata must never interrupt paid work or settlement.
            try:
                with persistence.db_uow() as db:
                    jobs.transition(db, user_id, group_id, expected=("processing",),
                                    state="processing", progress_stage=stage)
            except Exception:
                logger.warning("Analysis progress update unavailable", exc_info=True)

        generated = await asyncio.to_thread(
            synthesis.analyze_responses,
            question=question,
            sources=sources,
            context=context,
            model=model,
            max_output_tokens=maximum,
            on_progress=report_progress,
        )
        provider_returned = True
        _settle(reservation, generated, model)
        with persistence.db_uow() as db:
            current = jobs.get_intent(db, user_id, group_id)
            if not current or current["state"] != "processing" or current.get("cancel_requested"):
                return None
            analysis_id = create_cortex_analysis_run(
                db,
                user_id=user_id,
                session_id=UUID(str(payload["session_id"])),
                request_group_id=group_id,
                model=model,
                source_fingerprint=synthesis.source_fingerprint(sources, context),
                source_snapshot=synthesis.source_snapshot(sources),
                recommended_answer=generated.recommended_answer,
                agreements=[],
                disagreements=[],
                disagreement_note=None,
                unique_insights=[],
                confidence_level="limited",
                confidence_reason=generated.confidence_reason,
                verify_items=[],
                high_stakes_domain=None,
                combined_response_count=len(generated.structured_result.get("passages") or sources),
                failed_response_count=failed_count,
                prompt_tokens=generated.prompt_tokens,
                completion_tokens=generated.completion_tokens,
                total_tokens=generated.total_tokens,
                estimated_cost=generated.estimated_cost,
                analysis_policy_version=intent["policy_version"],
                cached_input_tokens=generated.cached_input_tokens,
                cache_write_tokens=generated.cache_write_tokens,
                reasoning_tokens=generated.reasoning_tokens,
                pricing_snapshot=generated.pricing_snapshot,
                pricing_version=generated.pricing_version,
                structured_result=generated.structured_result,
                context_snapshot=context,
            )
            jobs.transition(
                db,
                user_id,
                group_id,
                expected=("processing",),
                state="complete",
                progress_stage=None,
                analysis_id=analysis_id,
                reservation_payload=None,
                error_code=None,
            )
        with persistence.db_uow(commit_on_success=False) as db:
            return next(
                run
                for run in list_cortex_analysis_runs(db, user_id, request_group_id=group_id)
                if run["analysis_id"] == str(analysis_id)
            )
    except Exception as exc:
        billable = getattr(exc, "billable_response", None)
        if reservation is None and intent.get("reservation_payload"):
            reservation = _restore_reservation(intent["reservation_payload"])
        if reservation is not None:
            try:
                if billable is not None:
                    _settle(reservation, billable, intent["model"])
                elif not provider_returned:
                    persistence.release_subscription_usage(
                        reservation=reservation, reason="cortex_analysis_failed"
                    )
            except Exception:
                logger.exception("Analysis usage reconciliation failed")
        with persistence.db_uow() as db:
            jobs.transition(
                db,
                user_id,
                group_id,
                expected=("processing",),
                state="failed",
                progress_stage=None,
                error_code="cortex_analysis_failed",
            )
        logger.warning("Authorized synthesis failed", exc_info=True)
        return None


async def create_manual(
    user_id: UUID, group_id: UUID, *, regenerate: bool = False, request_id: str | None = None
) -> dict[str, Any]:
    preflight()
    with persistence.db_uow(commit_on_success=False) as db:
        payload = get_compare_analysis_sources(db, user_id, group_id)
        intent = jobs.get_intent(db, user_id, group_id)
        runs = list_cortex_analysis_runs(db, user_id, request_group_id=group_id)
    if payload is None:
        raise HTTPException(
            404,
            detail={"code": "compare_run_not_found", "message": "The Analyze run was not found."},
        )
    sources, _ = synthesis.normalize_analysis_sources(payload["responses"])
    if len(sources) < 2:
        raise HTTPException(
            409,
            detail={
                "code": "insufficient_compare_responses",
                "message": "At least two successful responses are required.",
            },
        )
    context = analysis_evidence.bounded_context(
        intent.get("context_snapshot") if intent else payload.get("prior_context", [])
    )
    fingerprint = synthesis.source_fingerprint(sources, context)

    def record_reuse(reused: bool) -> None:
        try:
            with persistence.db_uow() as db:
                record_cache_reuse_event(
                    db,
                    user_id=user_id,
                    request_id=request_id or f"analysis:{group_id}:{uuid4()}",
                    operation_type="cortex_analysis",
                    reused=reused,
                )
        except Exception:
            logger.warning("Synthesis reuse telemetry unavailable", exc_info=True)

    for run in runs:
        if (
            not regenerate
            and (bool(intent and intent["automatic"]) or cortex_analysis_reuse_enabled())
            and run["source_fingerprint"] == fingerprint
            and run["model"] == synthesis.configured_analysis_model()
            and run.get("analysis_policy_version") == CORTEX_ANALYSIS_POLICY_VERSION
        ):
            record_reuse(True)
            return run
    record_reuse(False)
    if intent and intent["state"] in {"queued", "processing"}:
        raise HTTPException(
            409, detail={"code": "analysis_in_progress", "message": "Analysis is already running."}
        )
    reservation = _reserve(
        user_id,
        f"compare-analysis:{group_id}:{uuid4()}",
        synthesis.reservation_input(payload["question"], sources, context),
        payload["question"],
        model=synthesis.configured_analysis_model(),
        output_tokens=synthesis.analysis_output_tokens(),
    )
    try:
        with persistence.db_uow() as db:
            values = dict(
                state="queued",
                cancel_requested=False,
                model=synthesis.configured_analysis_model(),
                policy_version=CORTEX_ANALYSIS_POLICY_VERSION,
                max_output_tokens=synthesis.analysis_output_tokens(),
                reservation_payload=_serialize_reservation(reservation),
                error_code=None,
            )
            if intent:
                if not jobs.transition(
                    db, user_id, group_id, expected=("waiting", "complete", "failed"), **values
                ):
                    raise HTTPException(409, detail="Analysis is already running.")
            else:
                jobs.create_intent(
                    db,
                    user_id=user_id,
                    request_group_id=group_id,
                    automatic=False,
                    context_snapshot=context,
                    **values,
                )
    except BaseException:
        persistence.release_subscription_usage(
            reservation=reservation, reason="analysis_already_running"
        )
        raise
    task = track_task(asyncio.create_task(process_intent(user_id, group_id)))
    result = await asyncio.shield(task)
    # A queue worker may have won the same atomic claim. Wait for that run,
    # without another reservation or provider request.
    for _ in range(1200):
        if result is not None:
            break
        with persistence.db_uow(commit_on_success=False) as db:
            current = jobs.get_intent(db, user_id, group_id)
            if current and current["state"] == "complete":
                result = next(
                    (
                        run
                        for run in list_cortex_analysis_runs(db, user_id, request_group_id=group_id)
                        if run["analysis_id"] == str(current["analysis_id"])
                    ),
                    None,
                )
                break
        if not current or current["state"] not in {"queued", "processing"}:
            break
        await asyncio.sleep(0.25)
    if result is None:
        raise HTTPException(
            502,
            detail={
                "code": "cortex_analysis_failed",
                "message": "Analysis couldn't finish. Your previous answers are still available.",
            },
        )
    return result


async def worker(stop: asyncio.Event) -> None:
    """Resume queued authorized analysis after process startup; never create intents."""
    while not stop.is_set():
        try:
            with persistence.db_uow() as db:
                jobs.expire_interrupted(db)
                pending = jobs.pending_intents(db)
            for intent in pending:
                if intent["state"] == "waiting":
                    await continue_initial(intent["user_id"], intent["request_group_id"])
                else:
                    await process_intent(intent["user_id"], intent["request_group_id"])
        except Exception:
            logger.warning("Synthesis queue unavailable", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            pass
